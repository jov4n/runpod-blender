"""
Plate Renderer API Server
=========================
Async jobs (recommended for RunPod / cold starts / long renders):
  POST /jobs       — upload image + params → { job_id, status, poll_url } immediately
  GET  /jobs/{id}  — { status, error?, created_at, download_url? }
  GET  /jobs/{id}/download — MP4/GIF when status is completed

Legacy synchronous (blocks until render finishes — short timeouts risky):
  POST /render     — same form fields; returns file directly (timeout RENDER_TIMEOUT_SEC)

Install:  pip install fastapi uvicorn python-multipart
Run:      python api_server.py
          uvicorn api_server:app --host 0.0.0.0 --port 8000

Examples:
  curl -s -X POST http://localhost:8000/jobs -F "sku=D9820" -F "image=@design.jpg" | jq .
  curl -s http://localhost:8000/jobs/<job_id>
  curl -o out.mp4 http://localhost:8000/jobs/<job_id>/download
"""
import json
import os
import re
import shutil
import subprocess
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse

app = FastAPI(title="Plate Renderer API")


@app.get("/ping")
async def ping():
    """Optional convenience on main PORT; RunPod LB probes PORT_HEALTH (see lb_health.py)."""
    return JSONResponse({"status": "healthy"})


BLENDER_EXE = os.environ.get(
    "BLENDER_EXE",
    r"C:\Program Files\Blender Foundation\Blender 5.0\blender.exe",
)
BASE_DIR = Path(__file__).parent
BLEND_FILE = Path(os.environ.get("BLEND_FILE", str(BASE_DIR / "plate2.blend")))
RENDER_SCRIPT = BASE_DIR / "render_plate.py"
TEMP_DIR = BASE_DIR / "tmp_renders"
TEMP_DIR.mkdir(exist_ok=True)

RENDER_TIMEOUT_SEC = int(os.environ.get("RENDER_TIMEOUT_SEC", "3600"))
VALID_SKUS = {"D9820", "D9609", "D9727"}

_JOB_ID_RE = re.compile(r"^[a-f0-9]{12}$")


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _status_path(job_dir: Path) -> Path:
    return job_dir / "_status.json"


def _write_status(job_dir: Path, data: dict) -> None:
    data = {**data, "updated_at": _utc_iso()}
    p = _status_path(job_dir)
    tmp = job_dir / "_status.json.tmp"
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(p)


def _read_status(job_dir: Path) -> dict | None:
    p = _status_path(job_dir)
    if not p.is_file():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def _run_blender_job(
    job_id: str,
    sku: str,
    input_path: Path,
    output_path: Path,
    resolution: int,
    samples: int,
    fps: int,
    duration: float,
    engine: str,
) -> None:
    job_dir = TEMP_DIR / job_id
    try:
        _write_status(
            job_dir,
            {
                "job_id": job_id,
                "status": "processing",
                "sku": sku,
                "message": "Blender render running",
            },
        )
        cmd = [
            str(BLENDER_EXE),
            "--background",
            str(BLEND_FILE),
            "--python",
            str(RENDER_SCRIPT),
            "--",
            "--sku",
            sku,
            "--image",
            str(input_path),
            "--output",
            str(output_path),
            "--engine",
            engine,
            "--resolution",
            str(resolution),
            "--samples",
            str(samples),
            "--fps",
            str(fps),
            "--duration",
            str(duration),
        ]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=RENDER_TIMEOUT_SEC,
        )
        fmt = output_path.suffix.lower().lstrip(".") or "mp4"
        actual = output_path
        if not actual.exists():
            mp4_fallback = job_dir / "render.mp4"
            if mp4_fallback.exists():
                actual = mp4_fallback
                fmt = "mp4"

        if result.returncode != 0:
            err = (result.stderr or "")[-8000:]
            _write_status(
                job_dir,
                {
                    "job_id": job_id,
                    "status": "failed",
                    "sku": sku,
                    "error": "Blender exited non-zero",
                    "stderr_tail": err,
                },
            )
            return
        if not actual.exists():
            _write_status(
                job_dir,
                {
                    "job_id": job_id,
                    "status": "failed",
                    "sku": sku,
                    "error": "Render produced no output file",
                },
            )
            return

        rel = actual.name
        _write_status(
            job_dir,
            {
                "job_id": job_id,
                "status": "completed",
                "sku": sku,
                "format": fmt,
                "output_file": rel,
                "download_url": f"/jobs/{job_id}/download",
            },
        )
    except subprocess.TimeoutExpired:
        _write_status(
            job_dir,
            {
                "job_id": job_id,
                "status": "failed",
                "sku": sku,
                "error": f"Render timed out after {RENDER_TIMEOUT_SEC}s",
            },
        )
    except Exception as e:
        _write_status(
            job_dir,
            {
                "job_id": job_id,
                "status": "failed",
                "sku": sku,
                "error": str(e),
            },
        )


@app.post("/jobs", status_code=202)
async def create_job(
    sku: str = Form(..., description="Product SKU: D9820, D9609, or D9727"),
    image: UploadFile = File(..., description="Design image (JPG/PNG)"),
    format: str = Form("mp4", description="Output format: mp4 or gif"),
    resolution: int = Form(1080),
    samples: int = Form(16),
    fps: int = Form(24),
    duration: float = Form(5.0),
    engine: str = Form("eevee", description="eevee (GPU) or cycles (CPU / headless)"),
):
    sku_u = sku.upper()
    if sku_u not in VALID_SKUS:
        raise HTTPException(400, f"Invalid SKU. Valid: {sorted(VALID_SKUS)}")

    fmt = format.lower()
    if fmt not in ("mp4", "gif"):
        raise HTTPException(400, "Format must be 'mp4' or 'gif'")

    eng = engine.lower()
    if eng not in ("eevee", "cycles"):
        raise HTTPException(400, "engine must be 'eevee' or 'cycles'")

    job_id = uuid.uuid4().hex[:12]
    job_dir = TEMP_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    ext_map = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
    ext = ext_map.get(image.content_type, Path(image.filename).suffix or ".jpg")
    input_path = job_dir / f"design{ext}"
    output_path = job_dir / f"render.{fmt}"

    with open(input_path, "wb") as f:
        shutil.copyfileobj(image.file, f)

    _write_status(
        job_dir,
        {
            "job_id": job_id,
            "status": "queued",
            "sku": sku_u,
            "message": "Job accepted; starting Blender when worker is available",
            "created_at": _utc_iso(),
        },
    )

    t = threading.Thread(
        target=_run_blender_job,
        kwargs={
            "job_id": job_id,
            "sku": sku_u,
            "input_path": input_path,
            "output_path": output_path,
            "resolution": resolution,
            "samples": samples,
            "fps": fps,
            "duration": duration,
            "engine": eng,
        },
        daemon=True,
    )
    t.start()

    return JSONResponse(
        status_code=202,
        content={
            "job_id": job_id,
            "status": "queued",
            "poll_url": f"/jobs/{job_id}",
            "download_url_when_ready": f"/jobs/{job_id}/download",
            "message": "Poll GET /jobs/{job_id} until status is completed, then GET download URL.",
        },
    )


@app.get("/jobs/{job_id}")
async def get_job(job_id: str):
    if not _JOB_ID_RE.match(job_id):
        raise HTTPException(400, "Invalid job_id")
    job_dir = TEMP_DIR / job_id
    if not job_dir.is_dir():
        raise HTTPException(404, "Job not found")
    st = _read_status(job_dir)
    if not st:
        return JSONResponse(
            {
                "job_id": job_id,
                "status": "unknown",
                "message": "Job directory exists but no status yet",
            }
        )
    return JSONResponse(st)


@app.get("/jobs/{job_id}/download")
async def download_job(job_id: str):
    if not _JOB_ID_RE.match(job_id):
        raise HTTPException(400, "Invalid job_id")
    job_dir = TEMP_DIR / job_id
    if not job_dir.is_dir():
        raise HTTPException(404, "Job not found")
    st = _read_status(job_dir)
    if not st:
        raise HTTPException(404, "No status for job")
    status = st.get("status")
    if status == "failed":
        raise HTTPException(
            409,
            detail={"error": st.get("error", "failed"), "stderr_tail": st.get("stderr_tail")},
        )
    if status != "completed":
        # 503 = not ready yet; client should poll GET /jobs/{job_id} (same as RunPod cold start + render)
        raise HTTPException(
            status_code=503,
            detail={
                "status": status,
                "message": "Job not finished yet; poll GET /jobs/{job_id} until status is completed, then retry download.",
            },
            headers={"Retry-After": "5"},
        )
    name = st.get("output_file", "render.mp4")
    path = job_dir / name
    if not path.is_file():
        raise HTTPException(404, "Output file missing")
    sku = st.get("sku", "render")
    ext = path.suffix.lower().lstrip(".") or "mp4"
    media = "video/mp4" if ext == "mp4" else "image/gif"
    return FileResponse(path, media_type=media, filename=f"{sku}_render.{ext}")


@app.post("/render")
async def render_sync(
    sku: str = Form(..., description="Product SKU: D9820, D9609, or D9727"),
    image: UploadFile = File(..., description="Design image (JPG/PNG)"),
    format: str = Form("mp4", description="Output format: mp4 or gif"),
    resolution: int = Form(1080, description="Square resolution"),
    samples: int = Form(16, description="Render samples"),
    fps: int = Form(24),
    duration: float = Form(5.0),
    engine: str = Form("eevee"),
):
    """
    Synchronous render (blocks until done). Prefer POST /jobs for RunPod / long renders.
    """
    sku_u = sku.upper()
    if sku_u not in VALID_SKUS:
        raise HTTPException(400, f"Invalid SKU. Valid: {sorted(VALID_SKUS)}")

    fmt = format.lower()
    if fmt not in ("mp4", "gif"):
        raise HTTPException(400, "Format must be 'mp4' or 'gif'")

    eng = engine.lower()
    if eng not in ("eevee", "cycles"):
        raise HTTPException(400, "engine must be 'eevee' or 'cycles'")

    job_id = uuid.uuid4().hex[:12]
    job_dir = TEMP_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    ext_map = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
    ext = ext_map.get(image.content_type, Path(image.filename).suffix or ".jpg")
    input_path = job_dir / f"design{ext}"
    output_path = job_dir / f"render.{fmt}"

    try:
        with open(input_path, "wb") as f:
            shutil.copyfileobj(image.file, f)

        cmd = [
            str(BLENDER_EXE),
            "--background",
            str(BLEND_FILE),
            "--python",
            str(RENDER_SCRIPT),
            "--",
            "--sku",
            sku_u,
            "--image",
            str(input_path),
            "--output",
            str(output_path),
            "--engine",
            eng,
            "--resolution",
            str(resolution),
            "--samples",
            str(samples),
            "--fps",
            str(fps),
            "--duration",
            str(duration),
        ]

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=RENDER_TIMEOUT_SEC,
        )

        if result.returncode != 0:
            raise HTTPException(
                500,
                f"Blender render failed:\n{(result.stderr or '')[-2000:]}",
            )

        actual_output = output_path
        if not actual_output.exists():
            mp4_fallback = job_dir / "render.mp4"
            if mp4_fallback.exists():
                actual_output = mp4_fallback
                fmt = "mp4"
            else:
                raise HTTPException(500, "Render produced no output file")

        media_type = "video/mp4" if fmt == "mp4" else "image/gif"
        return FileResponse(
            actual_output,
            media_type=media_type,
            filename=f"{sku_u}_render.{fmt}",
            headers={"X-Job-Id": job_id},
        )

    except subprocess.TimeoutExpired:
        raise HTTPException(504, f"Render timed out ({RENDER_TIMEOUT_SEC}s)")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/skus")
async def list_skus():
    return {
        "D9820": {"plate": "P1", "description": "Plate 1 (Revolve)"},
        "D9609": {"plate": "P2", "description": "Plate 2 (Bowl)"},
        "D9727": {"plate": "P3", "description": "Plate 3 (Flat)"},
    }


@app.delete("/cleanup")
async def cleanup():
    """Remove all temp render files."""
    if TEMP_DIR.exists():
        count = sum(1 for _ in TEMP_DIR.rglob("*") if _.is_file())
        shutil.rmtree(TEMP_DIR)
        TEMP_DIR.mkdir(exist_ok=True)
        return {"deleted_files": count}
    return {"deleted_files": 0}


if __name__ == "__main__":
    import uvicorn

    print(f"Blender: {BLENDER_EXE}")
    print(f"Blend:   {BLEND_FILE}")
    print(f"Script:  {RENDER_SCRIPT}")
    print(f"Timeout: {RENDER_TIMEOUT_SEC}s")
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
