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
COPY plate2.blend ./plate2.blend

ENV PORT=8000
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD sh -c 'curl -fsS "http://127.0.0.1:${PORT:-8000}/ping" >/dev/null || exit 1'

# Single process on PORT (RunPod commonly injects PORT=80).
CMD ["sh", "-c", "exec python3 -m uvicorn api_server:app --host 0.0.0.0 --port ${PORT:-8000}"]
