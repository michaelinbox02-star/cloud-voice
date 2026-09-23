# Seed-VC offline voice conversion engine.
#
# Pinned to the final commit of the canonical upstream (Plachtaa/seed-vc,
# archived 2025-11-21). The realtime fork used for streaming forked from this
# same revision, so offline and realtime share one upstream lineage.
FROM pytorch/pytorch:2.4.0-cuda12.1-cudnn9-runtime@sha256:68c022c2f4627943a6f3e574cfd2c8ae4256210d5f66ae2b117942e0a8d4fa9d

ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HUB_DISABLE_TELEMETRY=1 \
    HF_HOME=/opt/seed-vc/checkpoints/hf \
    SEED_VC_REVISION=51383efd921027683c89e5348211d93ff12ac2a8

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ffmpeg libsndfile1 curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/requirements.txt
RUN python -m pip install --no-cache-dir -r /tmp/requirements.txt

RUN git clone https://github.com/Plachtaa/seed-vc.git /opt/seed-vc \
    && cd /opt/seed-vc \
    && git checkout --detach "${SEED_VC_REVISION}" \
    && rm -rf /opt/seed-vc/.git

COPY service.py /opt/seed-vc/service.py

WORKDIR /opt/seed-vc
EXPOSE 8790
CMD ["uvicorn", "service:app", "--host", "0.0.0.0", "--port", "8790"]
