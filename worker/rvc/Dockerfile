# RVC v2 inference and training, pinned to the maintained upstream.
FROM python:3.12-slim-bookworm
ARG TORCH_VERSION=2.7.1
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/cu118
ARG RVC_CUDA_VARIANT=cu118

ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HUB_DISABLE_TELEMETRY=1 \
    RVC_REVISION=81eed5e8f68b6bed1789f682fe78cdd324495afc \
    OPENBLAS_NUM_THREADS=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ffmpeg libsndfile1 libportaudio2 curl \
    && rm -rf /var/lib/apt/lists/*

RUN python -m pip install \
    torch==${TORCH_VERSION}+${RVC_CUDA_VARIANT} torchaudio==${TORCH_VERSION}+${RVC_CUDA_VARIANT} \
    --index-url ${TORCH_INDEX_URL}

COPY requirements.txt /tmp/requirements.txt
COPY requirements-cu118.txt requirements-cu128.txt /tmp/
RUN python -m pip install --no-cache-dir -r /tmp/requirements.txt -r /tmp/requirements-${RVC_CUDA_VARIANT}.txt

RUN git clone https://github.com/RVC-Project/Retrieval-based-Voice-Conversion-WebUI.git /opt/rvc \
    && cd /opt/rvc \
    && git checkout --detach "${RVC_REVISION}" \
    && rm -rf /opt/rvc/.git

COPY assets.py service.py /opt/rvc/

WORKDIR /opt/rvc
EXPOSE 8792
CMD ["uvicorn", "service:app", "--host", "0.0.0.0", "--port", "8792"]
