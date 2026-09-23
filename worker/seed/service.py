"""Seed-VC conversion engine service.

Runs on the GPU worker only. The control plane (worker/api) owns job records,
uploads and artifacts; this service owns the model and does the arithmetic.
"""

from __future__ import annotations

import argparse
import hmac
import os
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

os.chdir("/opt/seed-vc")

DATA_ROOT = Path(os.environ.get("CLOUD_VOICE_DATA_ROOT", "/data")).resolve()
ALLOWED_SUFFIXES = {".wav", ".flac", ".mp3", ".m4a", ".ogg", ".opus", ".aac"}
MAX_DIFFUSION_STEPS = 100

@asynccontextmanager
async def lifespan(_: FastAPI):
    threading.Thread(target=_warm_background, name="seed-warmup", daemon=True).start()
    yield


app = FastAPI(title="Cloud Voice Studio Seed-VC Engine", version="0.1.0", docs_url=None, redoc_url=None, lifespan=lifespan)

_engine_lock = threading.Lock()
_convert_lock = threading.Lock()
_state: dict[str, object] = {"loaded": False, "load_seconds": None, "status": "warming", "detail": None}


def require_token(authorization: str | None = Header(default=None)) -> None:
    expected = os.environ.get("CLOUD_VOICE_ENGINE_TOKEN", "")
    supplied = authorization.removeprefix("Bearer ") if authorization else ""
    if not expected or not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="Invalid engine credential")


def resolve_audio(raw: str, *, must_exist: bool) -> Path:
    path = Path(raw)
    if not path.is_absolute():
        raise HTTPException(status_code=400, detail=f"Path must be absolute: {raw}")
    resolved = path.resolve()
    if not resolved.is_relative_to(DATA_ROOT):
        raise HTTPException(status_code=400, detail=f"Path escapes the data volume: {raw}")
    if resolved.suffix.lower() not in ALLOWED_SUFFIXES:
        raise HTTPException(status_code=400, detail=f"Unsupported audio extension: {resolved.suffix}")
    if must_exist and not resolved.is_file():
        raise HTTPException(status_code=404, detail=f"Audio file not found: {resolved}")
    return resolved


def build_args(**overrides: object) -> argparse.Namespace:
    values: dict[str, object] = {
        "ar_checkpoint_path": None,
        "cfm_checkpoint_path": None,
        "compile": False,
        "diffusion_steps": 30,
        "length_adjust": 1.0,
        "intelligibility_cfg_rate": 0.7,
        "similarity_cfg_rate": 0.7,
        "top_p": 0.9,
        "temperature": 1.0,
        "repetition_penalty": 1.0,
        "convert_style": False,
        "anonymization_only": False,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def load_engine(args: argparse.Namespace) -> None:
    """Load V2 models once. Not reentrant; callers serialise with _engine_lock."""
    import inference_v2

    started = time.perf_counter()
    inference_v2.load_v2_models(args)
    _state["loaded"] = True
    _state["load_seconds"] = round(time.perf_counter() - started, 2)


def ensure_engine(args: argparse.Namespace) -> None:
    if _state["loaded"]:
        return
    with _engine_lock:
        if not _state["loaded"]:
            _state["status"] = "warming"
            try:
                load_engine(args)
            except Exception as error:
                _state["status"] = "unavailable"
                _state["detail"] = str(error)[:500]
                raise
            _state["status"] = "ready"
            _state["detail"] = None


def _warm_background() -> None:
    try:
        ensure_engine(build_args())
    except Exception as error:  # noqa: BLE001 - health reports the failure
        print(f"[seed] warmup failed: {error}", flush=True)


class ConvertRequest(BaseModel):
    source_path: str
    target_path: str
    output_path: str
    diffusion_steps: int = Field(default=30, ge=1, le=MAX_DIFFUSION_STEPS)
    length_adjust: float = Field(default=1.0, gt=0.1, le=4.0)
    similarity_cfg_rate: float = Field(default=0.7, ge=0.0, le=2.0)
    intelligibility_cfg_rate: float = Field(default=0.7, ge=0.0, le=2.0)
    top_p: float = Field(default=0.9, gt=0.0, le=1.0)
    temperature: float = Field(default=1.0, gt=0.0, le=2.0)
    repetition_penalty: float = Field(default=1.0, ge=1.0, le=2.0)


@app.get("/health", dependencies=[Depends(require_token)])
def health() -> dict:
    cuda_available = None
    device = "loading" if _state["status"] == "warming" else "unknown"
    if _state["loaded"]:
        import torch

        cuda_available = torch.cuda.is_available()
        device = torch.cuda.get_device_name(0) if cuda_available else "cpu"

    return {
        "status": _state["status"],
        "detail": _state["detail"],
        "models_loaded": bool(_state["loaded"]),
        "model_load_seconds": _state["load_seconds"],
        "cuda_available": cuda_available,
        "device": device,
        "capabilities": ["seed-vc-v2-offline"],
    }


@app.post("/v1/warmup", dependencies=[Depends(require_token)])
def warmup() -> dict:
    ensure_engine(build_args())
    return {"status": "ready", "model_load_seconds": _state["load_seconds"]}


@app.post("/v1/convert", dependencies=[Depends(require_token)])
def convert(request: ConvertRequest) -> dict:
    import inference_v2
    import soundfile as sf
    import torch

    source = resolve_audio(request.source_path, must_exist=True)
    target = resolve_audio(request.target_path, must_exist=True)
    output = Path(request.output_path)
    if not output.is_absolute() or not output.resolve().is_relative_to(DATA_ROOT):
        raise HTTPException(status_code=400, detail="Output path must be inside the data volume")
    output = output.resolve()
    if output.suffix.lower() != ".wav":
        raise HTTPException(status_code=400, detail="Output must be a .wav path")
    output.parent.mkdir(parents=True, exist_ok=True)

    args = build_args(
        diffusion_steps=request.diffusion_steps,
        length_adjust=request.length_adjust,
        similarity_cfg_rate=request.similarity_cfg_rate,
        intelligibility_cfg_rate=request.intelligibility_cfg_rate,
        top_p=request.top_p,
        temperature=request.temperature,
        repetition_penalty=request.repetition_penalty,
    )

    ensure_engine(args)

    # One GPU, one conversion at a time, so peak VRAM stays predictable.
    with _convert_lock:
        torch.cuda.reset_peak_memory_stats()
        started = time.perf_counter()
        result = inference_v2.convert_voice_v2(str(source), str(target), args)
        elapsed = time.perf_counter() - started

    if result is None:
        raise HTTPException(status_code=500, detail="Seed-VC returned no audio for this input")

    sample_rate, audio = result
    sf.write(output, audio, sample_rate)
    duration = float(len(audio)) / float(sample_rate)

    return {
        "output_path": str(output),
        "sample_rate": int(sample_rate),
        "audio_seconds": round(duration, 3),
        "inference_seconds": round(elapsed, 3),
        "realtime_factor": round(elapsed / duration, 3) if duration > 0 else None,
        "peak_vram_mib": round(torch.cuda.max_memory_allocated() / (1024 * 1024), 1),
    }
