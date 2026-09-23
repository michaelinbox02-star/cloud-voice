"""Cloud Voice Studio worker control plane.

Owns voice profiles, uploads, job records and artifacts. Model arithmetic is
delegated to isolated engine services over the internal Docker network.
"""

from __future__ import annotations

import json
import time
import shutil
import subprocess
import tarfile
import zipfile
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import config, db, engines, jobs, realtime
from .security import require_token

ENGINES = {"seed-vc", "rvc"}


@asynccontextmanager
async def lifespan(_: FastAPI):
    config.ensure_directories()
    db.fail_orphaned_jobs()
    yield


app = FastAPI(
    title="Cloud Voice Studio Worker",
    version="0.2.0",
    docs_url=None,
    redoc_url=None,
    lifespan=lifespan,
)


def gpu_info() -> list[dict[str, str]]:
    result = subprocess.run(
        ["nvidia-smi", "--query-gpu=name,driver_version,memory.total,memory.used", "--format=csv,noheader,nounits"],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    if result.returncode != 0:
        return []
    devices = []
    for line in result.stdout.splitlines():
        values = [value.strip() for value in line.split(",")]
        if len(values) == 4:
            devices.append(
                {
                    "name": values[0],
                    "driver": values[1],
                    "memory_total_mib": values[2],
                    "memory_used_mib": values[3],
                }
            )
    return devices


def disk_info() -> dict[str, int]:
    usage = shutil.disk_usage(config.DATA_ROOT)
    return {
        "total_gib": round(usage.total / 1024**3, 1),
        "used_gib": round(usage.used / 1024**3, 1),
        "free_gib": round(usage.free / 1024**3, 1),
    }


def save_upload(
    upload: UploadFile,
    destination: Path,
    allowed: frozenset[str] | None = None,
) -> int:
    """Stream an upload to disk, rejecting unexpected types and oversize files."""
    suffix = Path(upload.filename or "").suffix.lower()
    permitted = allowed if allowed is not None else config.AUDIO_SUFFIXES
    if suffix not in permitted:
        raise HTTPException(status_code=400, detail=f"Unsupported file type: {suffix or 'unknown'}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with destination.open("wb") as handle:
        while chunk := upload.file.read(1024 * 1024):
            written += len(chunk)
            if written > config.MAX_UPLOAD_BYTES:
                handle.close()
                destination.unlink(missing_ok=True)
                raise HTTPException(
                    status_code=413,
                    detail=f"Upload exceeds the {config.MAX_UPLOAD_BYTES // (1024 * 1024)} MiB limit.",
                )
            handle.write(chunk)
    if written == 0:
        destination.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="Uploaded file was empty.")
    return written


@app.get("/v1/health", dependencies=[Depends(require_token)])
def health() -> dict:
    gpus = gpu_info()
    return {
        "status": "ready" if gpus else "degraded",
        "time": datetime.now(timezone.utc).isoformat(),
        "gpus": gpus,
        "capabilities": ["health", "voices", "conversion"],
    }


@app.get("/v1/system", dependencies=[Depends(require_token)])
def system() -> dict:
    statuses: dict[str, dict] = {}
    for name, probe in (("seed-vc", engines.seed_health), ("rvc", engines.rvc_health), ("tts", engines.tts_health)):
        try:
            status = probe()
            if status.get("status") not in {"ready", "warming", "unavailable"}:
                status["status"] = "unavailable"
            statuses[name] = status
        except engines.EngineError as error:
            statuses[name] = {"status": "unavailable", "detail": str(error)}
    realtime_status = config.DATA_ROOT / "realtime-health.json"
    try:
        reported = json.loads(realtime_status.read_text())
        if time.time() - float(reported["updated_at"]) > 10:
            raise ValueError("Realtime status is stale")
        statuses["realtime"] = reported
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        statuses["realtime"] = {"status": "unavailable", "detail": "Realtime engine is not reporting"}
    return {
        "gpus": gpu_info(),
        "compute_capability": config.COMPUTE_CAPABILITY,
        "cuda_variant": config.CUDA_VARIANT,
        "driver_version": config.DRIVER_VERSION,
        "disk": disk_info(),
        "engines": statuses,
        "voices": len(db.list_voices()),
    }


@app.post("/v1/engines/seed-vc/warmup", dependencies=[Depends(require_token)])
def warmup_seed() -> dict:
    try:
        return engines.seed_warmup()
    except engines.EngineError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error


@app.post("/v1/engines/rvc/warmup", dependencies=[Depends(require_token)])
def warmup_rvc() -> dict:
    try:
        return engines.rvc_warmup()
    except engines.EngineError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error


@app.post("/v1/engines/tts/warmup", dependencies=[Depends(require_token)])
def warmup_tts() -> dict:
    try:
        return engines.tts_warmup()
    except engines.EngineError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error


class RealtimeRequest(BaseModel):
    voice_id: str
    preset: str = "balanced"
    diffusion_steps: int | None = None
    inference_cfg_rate: float = 0.7


@app.post("/v1/realtime/sessions", dependencies=[Depends(require_token)], status_code=201)
def create_realtime_session(request: RealtimeRequest) -> dict:
    voice = db.get_voice(request.voice_id)
    if voice is None:
        raise HTTPException(status_code=404, detail="Voice not found.")
    if voice["engine"] != "seed-vc":
        raise HTTPException(status_code=400, detail="Realtime currently supports Seed-VC voices.")
    if not voice.get("reference_audio"):
        raise HTTPException(status_code=409, detail="This voice has no reference audio on the worker.")
    if request.preset not in {"low-latency", "balanced", "quality"}:
        raise HTTPException(status_code=400, detail="preset must be low-latency, balanced or quality.")
    return realtime.create_ticket(
        voice=voice,
        preset=request.preset,
        diffusion_steps=request.diffusion_steps,
        inference_cfg_rate=request.inference_cfg_rate,
    )


@app.delete("/v1/realtime/sessions/{session_id}", dependencies=[Depends(require_token)])
def delete_realtime_session(session_id: str) -> dict:
    realtime.delete_ticket(session_id)
    return {"deleted": session_id}


@app.get("/v1/voices", dependencies=[Depends(require_token)])
def list_voices() -> dict:
    return {"voices": db.list_voices()}


@app.post("/v1/voices", dependencies=[Depends(require_token)], status_code=201)
def create_voice(
    name: str = Form(...),
    engine: str = Form(...),
    description: str | None = Form(default=None),
    language: str | None = Form(default=None),
    settings: str | None = Form(default=None),
    reference: UploadFile | None = File(default=None),
) -> dict:
    engine = engine.strip().lower()
    if engine not in ENGINES:
        raise HTTPException(status_code=400, detail=f"Engine must be one of: {', '.join(sorted(ENGINES))}")
    name = name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="A voice name is required.")
    try:
        parsed_settings = json.loads(settings) if settings else {}
    except json.JSONDecodeError as error:
        raise HTTPException(status_code=400, detail=f"settings must be valid JSON: {error}") from error

    if engine == "seed-vc" and reference is None:
        raise HTTPException(status_code=400, detail="Seed-VC voices need a reference recording.")
    if engine == "rvc":
        raise HTTPException(status_code=501, detail="RVC voices are not available yet.")

    voice_id = db.new_id("voice")
    stored_reference: str | None = None
    size = 0
    if reference is not None:
        suffix = Path(reference.filename or "reference.wav").suffix.lower()
        destination = config.VOICES_DIR / voice_id / f"reference{suffix}"
        size = save_upload(reference, destination)
        stored_reference = str(destination)

    voice = db.create_voice(
        voice_id=voice_id,
        name=name,
        engine=engine,
        description=description,
        language=language,
        reference_audio=stored_reference,
        settings=parsed_settings,
        size_bytes=size,
    )
    return voice


@app.get("/v1/voices/{voice_id}", dependencies=[Depends(require_token)])
def get_voice(voice_id: str) -> dict:
    voice = db.get_voice(voice_id)
    if voice is None:
        raise HTTPException(status_code=404, detail="Voice not found.")
    return voice


@app.get("/v1/voices/{voice_id}/reference", dependencies=[Depends(require_token)])
def get_voice_reference(voice_id: str) -> FileResponse:
    voice = db.get_voice(voice_id)
    if voice is None or not voice.get("reference_audio"):
        raise HTTPException(status_code=404, detail="Reference audio not found.")
    path = Path(voice["reference_audio"])
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Reference audio file is missing on disk.")
    return FileResponse(path, filename=path.name)


@app.delete("/v1/voices/{voice_id}", dependencies=[Depends(require_token)])
def delete_voice(voice_id: str) -> dict:
    voice = db.get_voice(voice_id)
    if voice is None:
        raise HTTPException(status_code=404, detail="Voice not found.")
    directory = config.VOICES_DIR / voice_id
    if directory.is_dir():
        shutil.rmtree(directory, ignore_errors=True)
    db.delete_voice(voice_id)
    return {"deleted": voice_id}


@app.post("/v1/conversions", dependencies=[Depends(require_token)], status_code=202)
def create_conversion(
    voice_id: str = Form(...),
    engine: str = Form(default="seed-vc"),
    params: str | None = Form(default=None),
    source: UploadFile = File(...),
) -> dict:
    voice = db.get_voice(voice_id)
    if voice is None:
        raise HTTPException(status_code=404, detail="Voice not found.")
    if engine.strip().lower() != voice["engine"]:
        raise HTTPException(
            status_code=400,
            detail=f"Voice '{voice['name']}' was created for {voice['engine']}, not {engine}.",
        )
    try:
        parsed = json.loads(params) if params else {}
    except json.JSONDecodeError as error:
        raise HTTPException(status_code=400, detail=f"params must be valid JSON: {error}") from error

    output_format = str(parsed.get("output_format", "wav")).lower()
    if output_format not in {"wav", "mp3", "flac"}:
        raise HTTPException(status_code=400, detail="output_format must be wav, mp3, or flac.")
    parsed["output_format"] = output_format

    job = db.create_job(kind="convert", engine=voice["engine"], lane="gpu", voice_id=voice_id, params=parsed)
    suffix = Path(source.filename or "source.wav").suffix.lower()
    destination = config.UPLOADS_DIR / job["id"] / f"source{suffix}"
    save_upload(source, destination)
    db.execute("UPDATE jobs SET input_path = ? WHERE id = ?", (str(destination), job["id"]))
    jobs.submit_conversion(job["id"])
    return db.get_job(job["id"])  # type: ignore[return-value]


@app.get("/v1/jobs", dependencies=[Depends(require_token)])
def list_jobs(limit: int = 50) -> dict:
    return {"jobs": db.list_jobs(limit=max(1, min(limit, 200)))}


@app.get("/v1/jobs/{job_id}", dependencies=[Depends(require_token)])
def get_job(job_id: str) -> dict:
    job = db.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    return job


@app.get("/v1/jobs/{job_id}/audio", dependencies=[Depends(require_token)])
def get_job_audio(job_id: str) -> FileResponse:
    job = db.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    if job["status"] != "succeeded" or not job.get("output_path"):
        raise HTTPException(status_code=409, detail=f"Job is {job['status']}; no audio is available yet.")
    path = Path(job["output_path"])
    if not path.is_file():
        raise HTTPException(status_code=410, detail="The output file is no longer on the worker.")
    return FileResponse(path, filename=f"{job_id}{path.suffix}")


@app.delete("/v1/jobs/{job_id}", dependencies=[Depends(require_token)])
def delete_job(job_id: str) -> dict:
    job = db.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found.")
    if job["status"] == "running":
        raise HTTPException(status_code=409, detail="Job is still running.")
    jobs.remove_job_files(job)
    db.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
    return {"deleted": job_id}


class TtsRequest(BaseModel):
    text: str
    voice_id: str | None = None
    tts_voice: str = "af_heart"
    lang_code: str = "a"
    speed: float = 1.0
    output_format: str = "wav"
    diffusion_steps: int | None = None


@app.post("/v1/tts", dependencies=[Depends(require_token)], status_code=202)
def create_tts(request: TtsRequest) -> dict:
    if not request.text.strip():
        raise HTTPException(status_code=400, detail="Text is required.")
    params = request.model_dump()
    if request.voice_id:
        voice = db.get_voice(request.voice_id)
        if voice is None:
            raise HTTPException(status_code=404, detail="Voice not found.")
    job = db.create_job(
        kind="tts",
        engine="tts",
        lane="gpu" if request.voice_id else "cpu",
        voice_id=request.voice_id,
        params={k: v for k, v in params.items() if v is not None},
    )
    jobs.submit(job["id"])
    return db.get_job(job["id"])  # type: ignore[return-value]


@app.post("/v1/rvc/assets/prepare", dependencies=[Depends(require_token)])
def prepare_rvc_assets(training: bool = False) -> dict:
    try:
        return engines.rvc_prepare(training)
    except engines.EngineError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error


@app.post("/v1/rvc/voices", dependencies=[Depends(require_token)], status_code=201)
def create_rvc_voice(
    name: str = Form(...),
    description: str | None = Form(default=None),
    settings: str | None = Form(default=None),
    model: UploadFile = File(...),
    index: UploadFile | None = File(default=None),
) -> dict:
    name = name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="A voice name is required.")
    if not (model.filename or "").lower().endswith(".pth"):
        raise HTTPException(status_code=400, detail="RVC models must be .pth files.")
    if index is not None and not (index.filename or "").lower().endswith(".index"):
        raise HTTPException(status_code=400, detail="RVC indexes must be .index files.")
    try:
        parsed_settings = json.loads(settings) if settings else {}
    except json.JSONDecodeError as error:
        raise HTTPException(status_code=400, detail=f"settings must be valid JSON: {error}") from error

    voice_id = db.new_id("voice")
    directory = config.VOICES_DIR / voice_id
    directory.mkdir(parents=True, exist_ok=True)

    model_path = directory / "model.pth"
    size = save_upload(model, model_path)
    index_path: str | None = None
    if index is not None:
        stored_index = directory / "model.index"
        size += save_upload(index, stored_index)
        index_path = str(stored_index)

    return db.create_voice(
        voice_id=voice_id,
        name=name,
        engine="rvc",
        description=description,
        language=None,
        reference_audio=None,
        model_path=str(model_path),
        index_path=index_path,
        settings=parsed_settings,
        size_bytes=size,
    )


class TrainingRequest(BaseModel):
    name: str
    voice_name: str | None = None
    epochs: int = 200
    batch_size: int = 8
    f0: bool = True
    sample_rate_option: str = "40k"


class WorkerVoiceRequest(BaseModel):
    name: str
    description: str | None = None
    model_path: str
    index_path: str | None = None
    settings: dict = {}


@app.post("/v1/rvc/voices/from-worker", dependencies=[Depends(require_token)], status_code=201)
def register_trained_voice(request: WorkerVoiceRequest) -> dict:
    """Register a model that already lives on the worker (for example, one this
    worker just trained) without copying the file across the network twice."""
    model = Path(request.model_path)
    if not model.is_file() or not model.is_relative_to(config.DATA_ROOT):
        raise HTTPException(status_code=404, detail="Model path is not on this worker.")
    index = Path(request.index_path) if request.index_path else None
    if index is not None and (not index.is_file() or not index.is_relative_to(config.DATA_ROOT)):
        raise HTTPException(status_code=404, detail="Index path is not on this worker.")

    voice_id = db.new_id("voice")
    directory = config.VOICES_DIR / voice_id
    directory.mkdir(parents=True, exist_ok=True)
    stored_model = directory / "model.pth"
    shutil.copyfile(model, stored_model)
    size = stored_model.stat().st_size
    stored_index: str | None = None
    if index is not None:
        target = directory / "model.index"
        shutil.copyfile(index, target)
        size += target.stat().st_size
        stored_index = str(target)

    return db.create_voice(
        voice_id=voice_id,
        name=request.name.strip() or "Trained voice",
        engine="rvc",
        description=request.description,
        language=None,
        reference_audio=None,
        model_path=str(stored_model),
        index_path=stored_index,
        settings=request.settings,
        size_bytes=size,
    )


@app.post("/v1/training", dependencies=[Depends(require_token)], status_code=202)
def create_training(
    dataset: UploadFile = File(...),
    name: str = Form(...),
    voice_name: str | None = Form(default=None),
    epochs: int = Form(default=200),
    batch_size: int = Form(default=8),
    f0: bool = Form(default=True),
    sample_rate_option: str = Form(default="40k"),
) -> dict:
    if not (dataset.filename or "").lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="Upload the dataset as a .zip of audio files.")
    experiment = "".join(ch for ch in name if ch.isalnum() or ch in "-_")[:48]
    if not experiment:
        raise HTTPException(status_code=400, detail="Training name must contain letters or digits.")

    job = db.create_job(
        kind="train",
        engine="rvc",
        lane="gpu",
        voice_id=None,
        params={
            "experiment": experiment,
            "voice_name": voice_name or name,
            "epochs": epochs,
            "batch_size": batch_size,
            "f0": f0,
            "sample_rate_option": sample_rate_option,
        },
    )

    archive = config.DATASETS_DIR / job["id"] / "dataset.zip"
    save_upload(dataset, archive, allowed=frozenset({".zip"}))
    target = config.DATASETS_DIR / job["id"] / "dataset"
    try:
        with zipfile.ZipFile(archive) as bundle:
            for member in bundle.namelist():
                resolved = (target / member).resolve()
                if not resolved.is_relative_to(target.resolve()):
                    raise HTTPException(status_code=400, detail="Archive contains an unsafe path.")
            bundle.extractall(target)
        archive.unlink(missing_ok=True)
    except zipfile.BadZipFile as error:
        raise HTTPException(status_code=400, detail=f"Dataset archive is not a valid zip: {error}") from error
    jobs.submit(job["id"])
    return db.get_job(job["id"])  # type: ignore[return-value]


@app.get("/v1/backup", dependencies=[Depends(require_token)])
def download_backup() -> FileResponse:
    """Package every user-owned artifact so a worker can be rebuilt elsewhere."""
    return jobs.run_in_lane("cpu", _package_backup)


def _package_backup() -> FileResponse:
    config.BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    for stale in config.BACKUPS_DIR.glob("cloud-voice-backup-*.tar.gz"):
        stale.unlink(missing_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    archive = config.BACKUPS_DIR / f"cloud-voice-backup-{stamp}.tar.gz"

    include: list[tuple[str, Path]] = [
        ("cloud-voice.sqlite3", config.DATABASE_PATH),
        ("voices", config.VOICES_DIR),
    ]
    with tarfile.open(archive, "w:gz") as bundle:
        bundle.add(str(config.DATABASE_PATH), arcname="cloud-voice.sqlite3")
        bundle.add(str(config.VOICES_DIR), arcname="voices")
    return FileResponse(archive, filename=archive.name, media_type="application/gzip")


@app.post("/v1/restore", dependencies=[Depends(require_token)])
def restore_backup(archive: UploadFile = File(...)) -> dict:
    return jobs.run_in_lane("io", lambda: _restore_backup(archive))


def _restore_backup(archive: UploadFile) -> dict:
    staged = config.BACKUPS_DIR / "restore-upload.tar.gz"
    save_upload(archive, staged, allowed=frozenset({".gz", ".tgz"}))
    stage_dir = config.BACKUPS_DIR / "restore"
    shutil.rmtree(stage_dir, ignore_errors=True)
    stage_dir.mkdir(parents=True, exist_ok=True)

    try:
        with tarfile.open(staged, "r:gz") as bundle:
            for member in bundle.getmembers():
                resolved = (stage_dir / member.name).resolve()
                if not resolved.is_relative_to(stage_dir.resolve()):
                    raise HTTPException(status_code=400, detail="Backup contains an unsafe path.")
            bundle.extractall(stage_dir)
    except tarfile.TarError as error:
        raise HTTPException(status_code=400, detail=f"Backup archive could not be read: {error}") from error
    finally:
        staged.unlink(missing_ok=True)

    restored_voices = 0
    source_db = stage_dir / "cloud-voice.sqlite3"
    if source_db.is_file():
        shutil.copyfile(source_db, config.DATABASE_PATH)

    source_voices = stage_dir / "voices"
    if source_voices.is_dir():
        for voice_dir in source_voices.iterdir():
            if not voice_dir.is_dir():
                continue
            destination = config.VOICES_DIR / voice_dir.name
            if destination.exists():
                shutil.rmtree(destination, ignore_errors=True)
            shutil.copytree(voice_dir, destination)
            restored_voices += 1

    shutil.rmtree(stage_dir, ignore_errors=True)
    return {"restored_voices": restored_voices, "voices": len(db.list_voices())}
