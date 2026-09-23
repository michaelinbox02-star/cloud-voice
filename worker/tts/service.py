"""Kokoro text to speech engine.

Produces base speech only. Routing that speech through a Seed-VC or RVC voice is
the control plane's job, so this service stays a single, replaceable component.
"""

from __future__ import annotations

import hmac
import os
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

import numpy as np
import soundfile as sf
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

DATA_ROOT = Path(os.environ.get("CLOUD_VOICE_DATA_ROOT", "/data")).resolve()
ENGINE_TOKEN = os.environ.get("CLOUD_VOICE_ENGINE_TOKEN", "")
MAX_CHARACTERS = 5000

@asynccontextmanager
async def lifespan(_: FastAPI):
    threading.Thread(target=_warm_background, name="tts-warmup", daemon=True).start()
    yield


app = FastAPI(title="Cloud Voice Studio TTS Engine", version="0.1.0", docs_url=None, redoc_url=None, lifespan=lifespan)

_pipelines: dict[str, object] = {}
_lock = threading.Lock()
_state: dict[str, str | None] = {"status": "warming", "detail": None}


def require_token(authorization: str | None = Header(default=None)) -> None:
    supplied = authorization.removeprefix("Bearer ") if authorization else ""
    if not ENGINE_TOKEN or not hmac.compare_digest(supplied, ENGINE_TOKEN):
        raise HTTPException(status_code=401, detail="Invalid engine credential")


def pipeline(lang_code: str):
    """Load the Kokoro pipeline for a language once per process."""
    if lang_code not in _pipelines:
        from kokoro import KPipeline

        _pipelines[lang_code] = KPipeline(lang_code=lang_code)
    return _pipelines[lang_code]


def warmup_pipeline() -> None:
    with _lock:
        if _state["status"] == "ready":
            return
        _state["status"] = "warming"
        try:
            # The default voice can be fetched lazily, so exercise it once too.
            for _ in pipeline("a")("Ready.", voice="af_heart"):
                pass
        except Exception as error:
            _state["status"] = "unavailable"
            _state["detail"] = str(error)[:500]
            raise
        _state["status"] = "ready"
        _state["detail"] = None


def _warm_background() -> None:
    try:
        warmup_pipeline()
    except Exception as error:  # noqa: BLE001 - health reports the failure
        print(f"[tts] warmup failed: {error}", flush=True)


class SpeechRequest(BaseModel):
    text: str
    output_path: str
    voice: str = Field(default="af_heart")
    speed: float = Field(default=1.0, gt=0.4, le=2.0)
    lang_code: str = Field(default="a", pattern="^[abefhijpz]$")


@app.get("/health", dependencies=[Depends(require_token)])
def health() -> dict:
    return {
        "status": _state["status"],
        "detail": _state["detail"],
        "loaded": bool(_pipelines),
        "capabilities": ["kokoro-tts"],
    }


@app.post("/v1/warmup", dependencies=[Depends(require_token)])
def warmup() -> dict:
    warmup_pipeline()
    return health()


@app.post("/v1/speech", dependencies=[Depends(require_token)])
def speech(request: SpeechRequest) -> dict:
    text = request.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Text is empty")
    if len(text) > MAX_CHARACTERS:
        raise HTTPException(status_code=400, detail=f"Text exceeds {MAX_CHARACTERS} characters")

    output = Path(request.output_path)
    if not output.is_absolute() or not output.resolve().is_relative_to(DATA_ROOT):
        raise HTTPException(status_code=400, detail="Output path must be inside the data volume")
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)

    with _lock:
        started = time.perf_counter()
        generator = pipeline(request.lang_code)
        chunks: list[np.ndarray] = []
        sample_rate = 24000
        for result in generator(text, voice=request.voice, speed=request.speed):
            audio = getattr(result, "audio", None)
            if audio is None and isinstance(result, (tuple, list)) and len(result) >= 3:
                audio = result[2]
            if audio is None:
                continue
            chunks.append(np.asarray(audio, dtype=np.float32).reshape(-1))
        elapsed = time.perf_counter() - started

    if not chunks:
        raise HTTPException(status_code=500, detail="Kokoro produced no audio for this text")

    audio = np.concatenate(chunks)
    sf.write(output, audio, sample_rate)
    duration = len(audio) / sample_rate
    return {
        "output_path": str(output),
        "sample_rate": sample_rate,
        "audio_seconds": round(duration, 3),
        "synthesis_seconds": round(elapsed, 3),
        "characters": len(text),
    }
