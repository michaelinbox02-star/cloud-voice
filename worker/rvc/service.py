"""RVC v2 engine: offline conversion plus remote training.

Both run inside the pinned upstream checkout, driven through its documented CLI
so the behaviour matches the project's own scripts rather than a reimplementation.
"""

from __future__ import annotations

import json
import os
import random
import shutil
import subprocess
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

import assets as asset_tools

os.chdir("/opt/rvc")

DATA_ROOT = Path(os.environ.get("CLOUD_VOICE_DATA_ROOT", "/data")).resolve()
ENGINE_TOKEN = os.environ.get("CLOUD_VOICE_ENGINE_TOKEN", "")
AUDIO_SUFFIXES = {".wav", ".flac", ".mp3", ".m4a", ".ogg", ".opus", ".aac"}

@asynccontextmanager
async def lifespan(_: FastAPI):
    threading.Thread(target=_warm_background, name="rvc-warmup", daemon=True).start()
    yield


app = FastAPI(title="Cloud Voice Studio RVC Engine", version="0.1.0", docs_url=None, redoc_url=None, lifespan=lifespan)
_warm_lock = threading.Lock()
_state: dict[str, str | None] = {"status": "warming", "detail": None}


def warmup_assets() -> dict:
    with _warm_lock:
        _state["status"] = "warming"
        try:
            result = asset_tools.prepare(training=True)
        except Exception as error:
            _state["status"] = "unavailable"
            _state["detail"] = str(error)[:500]
            raise
        _state["status"] = "ready"
        _state["detail"] = None
        return result


def _warm_background() -> None:
    try:
        warmup_assets()
    except Exception as error:  # noqa: BLE001 - health reports the failure
        print(f"[rvc] warmup failed: {error}", flush=True)


def require_token(authorization: str | None = Header(default=None)) -> None:
    supplied = authorization.removeprefix("Bearer ") if authorization else ""
    import hmac

    if not ENGINE_TOKEN or not hmac.compare_digest(supplied, ENGINE_TOKEN):
        raise HTTPException(status_code=401, detail="Invalid engine credential")


def inside_data(raw: str, *, must_exist: bool, suffix_ok: bool = False) -> Path:
    path = Path(raw)
    if not path.is_absolute():
        raise HTTPException(status_code=400, detail=f"Path must be absolute: {raw}")
    resolved = path.resolve()
    if not resolved.is_relative_to(DATA_ROOT):
        raise HTTPException(status_code=400, detail=f"Path escapes the data volume: {raw}")
    if suffix_ok and resolved.suffix.lower() not in AUDIO_SUFFIXES:
        raise HTTPException(status_code=400, detail=f"Unsupported audio extension: {resolved.suffix}")
    if must_exist and not resolved.is_file():
        raise HTTPException(status_code=404, detail=f"File not found: {resolved}")
    return resolved


def run(command: list[str], timeout: int = 7200) -> tuple[str, float]:
    """Run a project CLI step and return its output and duration."""
    began = time.perf_counter()
    # The training scripts are invoked as files, so Python puts their own folder
    # on sys.path rather than the project root; give them the root explicitly.
    environment = {**os.environ, "PYTHONPATH": "/opt/rvc"}
    result = subprocess.run(
        command,
        cwd="/opt/rvc",
        env=environment,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    elapsed = time.perf_counter() - began
    output = (result.stdout or "") + (result.stderr or "")
    if result.returncode != 0:
        tail = "\n".join(output.strip().splitlines()[-12:])
        label = command[2] if len(command) > 2 and command[1] == "-m" else " ".join(command[:2])
        raise HTTPException(
            status_code=500,
            detail=f"{label} failed ({result.returncode}):\n{tail}",
        )
    return output, elapsed


def build_filelist(log_dir: Path, sample_rate: str, f0: bool, speaker_id: int = 0) -> int:
    """Write the manifest train.py reads, mirroring the WebUI's own layout.

    Each row is: ground-truth wav, content features, optional pitch tracks, and
    the speaker id. The WebUI builds this in memory and never writes it for a
    CLI run, so a pure CLI training run has to reproduce it.
    """
    gt = log_dir / "0_gt_wavs"
    features = log_dir / "3_feature768"
    f0_dir = log_dir / "2a_f0"
    f0_nsf = log_dir / "2b-f0nsf"
    mute_root = Path("/opt/rvc/logs/mute")

    names = sorted(path.stem for path in gt.glob("*.wav") if path.stem != "mute")
    if not names:
        raise HTTPException(status_code=500, detail="Preprocessing produced no audio slices")

    rows: list[str] = []
    for name in names:
        if f0:
            rows.append(
                f"{gt}/{name}.wav|{features}/{name}.npy|{f0_dir}/{name}.wav.npy|{f0_nsf}/{name}.wav.npy|{speaker_id}"
            )
        else:
            rows.append(f"{gt}/{name}.wav|{features}/{name}.npy|{speaker_id}")

    # Two silence rows keep the model from over-fitting breath noise.
    for _ in range(2):
        if f0:
            rows.append(
                f"{mute_root}/0_gt_wavs/mute{sample_rate}.wav|{mute_root}/3_feature768/mute.npy"
                f"|{mute_root}/2a_f0/mute.wav.npy|{mute_root}/2b-f0nsf/mute.wav.npy|{speaker_id}"
            )
        else:
            rows.append(
                f"{mute_root}/0_gt_wavs/mute{sample_rate}.wav|{mute_root}/3_feature768/mute.npy|{speaker_id}"
            )

    random.shuffle(rows)
    (log_dir / "filelist.txt").write_text("\n".join(rows))
    return len(names)


class ConvertRequest(BaseModel):
    model_path: str
    input_path: str
    output_path: str
    index_path: str | None = None
    pitch: int = Field(default=0, ge=-24, le=24)
    f0_method: str = Field(default="rmvpe", pattern="^(rmvpe|pm)$")
    index_rate: float = Field(default=0.75, ge=0.0, le=1.0)
    protect: float = Field(default=0.33, ge=0.0, le=0.5)
    rms_mix_rate: float = Field(default=1.0, ge=0.0, le=1.0)
    resample_sr: int = Field(default=0, ge=0, le=48000)


class TrainRequest(BaseModel):
    dataset_dir: str
    experiment: str
    epochs: int = Field(default=200, ge=10, le=1200)
    batch_size: int = Field(default=8, ge=1, le=32)
    f0: bool = True
    sample_rate_option: str = Field(default="40k", pattern="^(32k|40k|48k)$")


def asset_state() -> dict:
    return {
        "weights": sorted(p.name for p in (asset_tools.ASSETS / "weights").glob("*.pth")),
        "indices": sorted(p.name for p in (asset_tools.ASSETS / "indices").glob("*.index")),
        "inference_ready": all((asset_tools.ROOT / path).is_file() for _, path in asset_tools.INFERENCE_FILES),
        "training_ready": all((asset_tools.ROOT / path).is_file() for _, path in asset_tools.TRAINING_FILES),
    }


@app.get("/health", dependencies=[Depends(require_token)])
def health() -> dict:
    return {"status": _state["status"], "detail": _state["detail"], "assets": asset_state(), "capabilities": ["rvc-v2-convert", "rvc-v2-train"]}


@app.post("/v1/warmup", dependencies=[Depends(require_token)])
def warmup() -> dict:
    return {"status": "ready", "assets": warmup_assets()}


@app.post("/v1/assets/prepare", dependencies=[Depends(require_token)])
def prepare_assets(training: bool = False) -> dict:
    return asset_tools.prepare(training=training)


@app.post("/v1/convert", dependencies=[Depends(require_token)])
def convert(request: ConvertRequest) -> dict:
    model = Path(request.model_path)
    if not model.is_file():
        raise HTTPException(status_code=404, detail=f"Voice model not found: {model}")
    source = inside_data(request.input_path, must_exist=True, suffix_ok=True)
    output = inside_data(request.output_path, must_exist=False)
    output.parent.mkdir(parents=True, exist_ok=True)

    command = [
        "python",
        "-m",
        "infer.cli",
        "--model",
        str(model),
        "--input",
        str(source),
        "--output",
        str(output),
        "--pitch",
        str(request.pitch),
        "--f0-method",
        request.f0_method,
        "--index-rate",
        str(request.index_rate),
        "--protect",
        str(request.protect),
        "--rms-mix-rate",
        str(request.rms_mix_rate),
        "--resample-sr",
        str(request.resample_sr),
        "--overwrite",
    ]
    if request.index_path:
        index = Path(request.index_path)
        if not index.is_file():
            raise HTTPException(status_code=404, detail=f"Index not found: {index}")
        command += ["--index", str(index)]

    started = time.perf_counter()
    output_text, _ = run(command)
    elapsed = time.perf_counter() - started
    if not output.is_file():
        tail = "\n".join(output_text.strip().splitlines()[-8:])
        raise HTTPException(status_code=500, detail=f"RVC produced no output:\n{tail}")

    info = None
    try:
        import soundfile as sf

        frames = sf.info(str(output))
        info = {"sample_rate": frames.samplerate, "audio_seconds": round(frames.duration, 3)}
    except Exception:  # noqa: BLE001 - metrics are best effort
        pass

    return {
        "output_path": str(output),
        "inference_seconds": round(elapsed, 3),
        "realtime_factor": round(elapsed / info["audio_seconds"], 3) if info else None,
        **(info or {}),
    }


@app.post("/v1/train", dependencies=[Depends(require_token)])
def train(request: TrainRequest) -> dict:
    dataset = inside_data(request.dataset_dir, must_exist=False)
    if not dataset.is_dir():
        raise HTTPException(status_code=404, detail=f"Dataset directory not found: {dataset}")
    experiment = "".join(ch for ch in request.experiment if ch.isalnum() or ch in "-_")
    if not experiment:
        raise HTTPException(status_code=400, detail="Experiment name must contain letters or digits")

    asset_tools.prepare(training=True)

    log_dir = Path("/opt/rvc/logs") / experiment
    if log_dir.exists():
        shutil.rmtree(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    steps: list[dict] = []

    def step(name: str, command: list[str]) -> None:
        output, elapsed = run(command)
        steps.append({"step": name, "seconds": round(elapsed, 2), "tail": output.strip().splitlines()[-3:]})

    # 1. Slice and normalise the dataset.
    step(
        "preprocess",
        ["python", "-m", "train.preprocess", str(dataset), "40000", "8", str(log_dir), "False", "3.7"],
    )
    # 2. Pitch track, then 3. content features, both on the GPU.
    step(
        "extract_f0",
        ["python", "-m", "train.dataset.extract_f0", "cuda", "1", "0", "0", str(log_dir), "true"],
    )
    step(
        "extract_hubert",
        ["python", "-m", "train.dataset.extract_hubert_feature", "cuda:0", "1", "0", "0", str(log_dir), "v2", "true"],
    )

    # 4. train.py expects config.json beside filelist.txt. The WebUI picks a
    # shipped template: v2 uses configs/v1 for 40k and configs/v2 otherwise.
    template = "v1" if request.sample_rate_option == "40k" else "v2"
    template_path = Path("/opt/rvc/configs") / template / f"{request.sample_rate_option}.json"
    if not template_path.is_file():
        raise HTTPException(status_code=500, detail=f"Missing training template: {template_path}")
    config = json.loads(template_path.read_text())
    config.pop("speaker_info", None)
    (log_dir / "config.json").write_text(json.dumps(config, ensure_ascii=False, indent=4, sort_keys=True) + "\n")

    slices = build_filelist(log_dir, request.sample_rate_option, request.f0)
    steps.append({"step": "filelist", "slices": slices})

    pretrained = "/opt/rvc/assets/pretrained_v2/f0G40k.pth" if request.f0 else "/opt/rvc/assets/pretrained_v2/G40k.pth"

    # 5. Fine-tune. `-sw 1` also writes the inference-ready small model.
    step(
        "train",
        [
            "python",
            "-m",
            "train.train",
            "-e",
            experiment,
            "-sr",
            request.sample_rate_option,
            "-f0",
            "1" if request.f0 else "0",
            "-bs",
            str(request.batch_size),
            "-g",
            "0",
            "-te",
            str(request.epochs),
            "-se",
            "5",
            "-pg",
            pretrained,
            "-pd",
            "/opt/rvc/assets/pretrained_v2/f0D40k.pth" if request.f0 else "/opt/rvc/assets/pretrained_v2/D40k.pth",
            "-l",
            "0",
            "-c",
            "0",
            "-sw",
            "1",
            "-v",
            "v2",
        ],
    )
    # 6. Retrieval index over the extracted features.
    step(
        "train_index",
        ["python", "-m", "train.train_index", experiment, "v2", "/opt/rvc/assets/indices", "8", "single"],
    )

    weights = sorted((asset_tools.ASSETS / "weights").glob("*.pth"), key=lambda p: p.stat().st_mtime, reverse=True)
    indices = sorted((asset_tools.ASSETS / "indices").glob("*added*.index"), key=lambda p: p.stat().st_mtime, reverse=True)

    # Publish the artifacts into the shared data volume so the control plane and
    # the desktop can reach them without knowing this container's layout.
    published = DATA_ROOT / "trained" / experiment
    published.mkdir(parents=True, exist_ok=True)
    model_path = index_path = None
    if weights:
        model_path = published / weights[0].name
        shutil.copyfile(weights[0], model_path)
    if indices:
        index_path = published / indices[0].name
        shutil.copyfile(indices[0], index_path)

    return {
        "experiment": experiment,
        "model_path": str(model_path) if model_path else None,
        "index_path": str(index_path) if index_path else None,
        "steps": steps,
        "experiment_dir": str(log_dir),
    }
