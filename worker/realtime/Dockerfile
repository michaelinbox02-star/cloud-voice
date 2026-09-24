# Seed-VC realtime streaming engine with a WebRTC transport.
#
# Pinned to the maintained realtime fork, which is itself derived from the final
# commit of Plachtaa/seed-vc.
ARG PYTORCH_BASE_IMAGE=pytorch/pytorch:2.4.0-cuda12.1-cudnn9-runtime@sha256:68c022c2f4627943a6f3e574cfd2c8ae4256210d5f66ae2b117942e0a8d4fa9d
FROM ${PYTORCH_BASE_IMAGE}
ARG TORCH_VERSION=2.4.0
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cu121

ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HUB_DISABLE_TELEMETRY=1 \
    HF_HOME=/opt/seed-vc-realtime/checkpoints/hf \
    REALTIME_REVISION=58d0ca372cc76f60a798d9f84611f053f9029f3a

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ffmpeg libsndfile1 libportaudio2 curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/requirements.txt
RUN python -m pip install --no-cache-dir -r /tmp/requirements.txt
RUN python -c "import torch; assert torch.__version__.split('+')[0] == '${TORCH_VERSION}'; assert '${TORCH_INDEX_URL}'.startswith('https://download.pytorch.org/whl/')"

RUN git clone https://github.com/jiaheguo521/seed-vc-realtime.git /opt/seed-vc-realtime \
    && cd /opt/seed-vc-realtime \
    && git checkout --detach "${REALTIME_REVISION}" \
    && rm -rf /opt/seed-vc-realtime/.git

COPY engine.py service.py patch_upstream.py short_utterance_test.py /opt/seed-vc-realtime/
RUN python /opt/seed-vc-realtime/patch_upstream.py \
    && rm /opt/seed-vc-realtime/patch_upstream.py

WORKDIR /opt/seed-vc-realtime
EXPOSE 8791
# Signalling is reachable only from the host's loopback; the desktop reaches it
# through its SSH tunnel, and media uses UDP gathered on the host interfaces.
CMD ["uvicorn", "service:app", "--host", "127.0.0.1", "--port", "8791"]
