# RunPod / GPU cloud — Blender + FastAPI plate renderer
#
# Build:
#   docker build -t plate-render:latest --build-arg BLENDER_VERSION=5.0.1 .
#
# Run (local GPU test — one port; same as RunPod when PORT=80):
#   docker run --rm -p 8000:8000 --gpus all -e PORT=8000 plate-render:latest
#
# RunPod Serverless often sets PORT=PORT_HEALTH=80 (single exposed port). One Uvicorn serves
# GET /ping and the full API on PORT (see api_server.py).

FROM ubuntu:22.04
ENV DEBIAN_FRONTEND=noninteractive

# EEVEE needs the host NVIDIA *graphics* stack in-container. The default nvidia-container
# injection is often only compute → Blender falls back to slow CPU/Mesa paths (~minutes vs ~seconds).
ENV NVIDIA_VISIBLE_DEVICES=all
ENV NVIDIA_DRIVER_CAPABILITIES=graphics,utility,compute

# Few layers: system deps first (cache-friendly), then Blender, then app last.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    xz-utils \
    ffmpeg \
    python3 \
    python3-pip \
    libgl1 \
    libegl1 \
    libgles2 \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender1 \
    libxi6 \
    libxxf86vm1 \
    libxfixes3 \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# https://download.blender.org/release/ — adjust if Blender bumps minor
ARG BLENDER_VERSION=5.0.1
ARG BLENDER_RELEASE=5.0
RUN set -eux \
    && curl -fsSL \
      "https://download.blender.org/release/Blender${BLENDER_RELEASE}/blender-${BLENDER_VERSION}-linux-x64.tar.xz" \
      -o /tmp/blender.tar.xz \
    && tar -xJf /tmp/blender.tar.xz -C /opt \
    && mv "/opt/blender-${BLENDER_VERSION}-linux-x64" /opt/blender \
    && rm /tmp/blender.tar.xz \
    && ln -sf /opt/blender/blender /usr/local/bin/blender

ENV BLENDER_EXE=/usr/local/bin/blender
ENV BLEND_FILE=/app/plate2.blend
WORKDIR /app

COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

# Change least often last for layer cache during dev
COPY render_plate.py api_server.py ./
# plate2.blend is Git LFS. RunPod/GitHub build contexts often pass only the pointer (~130 bytes).
# Clone the repo and `git lfs pull` so /app/plate2.blend is the real file (override for forks).
ARG BLEND_GIT_REPO=https://github.com/jov4n/runpod-blender.git
ARG BLEND_GIT_REF=main
RUN set -eux \
    && apt-get update \
    && apt-get install -y --no-install-recommends git git-lfs \
    && git lfs install \
    && git clone --depth 1 --branch "${BLEND_GIT_REF}" "${BLEND_GIT_REPO}" /tmp/blendsrc \
    && cd /tmp/blendsrc && git lfs pull \
    && python3 -c "import os; p='plate2.blend'; s=os.path.getsize(p); assert s>10_000_000, 'plate2.blend too small (LFS blob missing?)'" \
    && install -m0644 plate2.blend /app/plate2.blend \
    && cd / && rm -rf /tmp/blendsrc /root/.cache \
    && apt-get purge -y git git-lfs \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

ENV PORT=8000
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD sh -c 'curl -fsS "http://127.0.0.1:${PORT:-8000}/ping" >/dev/null || exit 1'

# Single process on PORT (RunPod commonly injects PORT=80).
CMD ["sh", "-c", "exec python3 -m uvicorn api_server:app --host 0.0.0.0 --port ${PORT:-8000}"]
