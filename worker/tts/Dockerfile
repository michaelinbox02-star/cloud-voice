# Kokoro text to speech. CPU inference keeps the GPU free for conversion.
FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HUB_DISABLE_TELEMETRY=1 \
    KOKORO_HOME=/opt/tts/models

RUN apt-get update \
    && apt-get install -y --no-install-recommends espeak-ng libsndfile1 curl \
    && rm -rf /var/lib/apt/lists/*

RUN python -m pip install --no-cache-dir torch==2.4.0 --index-url https://download.pytorch.org/whl/cpu

COPY requirements.txt /tmp/requirements.txt
RUN python -m pip install --no-cache-dir -r /tmp/requirements.txt

COPY service.py /opt/tts/service.py

WORKDIR /opt/tts
EXPOSE 8793
CMD ["uvicorn", "service:app", "--host", "0.0.0.0", "--port", "8793"]
