"""Background jobs scheduled by resource, with a shared GPU lease for realtime."""

from __future__ import annotations

import json
import fcntl
import shutil
import subprocess
from contextlib import contextmanager, nullcontext
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

from . import config, db, engines

_executors = {
    "gpu": ThreadPoolExecutor(max_workers=1, thread_name_prefix="cloud-voice-gpu"),
    "cpu": ThreadPoolExecutor(max_workers=2, thread_name_prefix="cloud-voice-cpu"),
    "io": ThreadPoolExecutor(max_workers=1, thread_name_prefix="cloud-voice-io"),
}


@contextmanager
def gpu_lease():
    """Coordinate GPU jobs with the realtime container through the data volume."""
    with (config.DATA_ROOT / "gpu.lock").open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def run_in_lane(lane: str, work):
    """Run synchronous backup or restore work in its resource lane."""
    return _executors[lane].submit(work).result()


def _update(job_id: str, **fields: object) -> None:
    assignments = ", ".join(f"{name} = ?" for name in fields)
    db.execute(f"UPDATE jobs SET {assignments} WHERE id = ?", (*fields.values(), job_id))


def transcode(source: Path, target_format: str) -> Path:
    """Convert a finished WAV artifact into a requested delivery format."""
    if target_format == "wav":
        return source
    destination = source.with_suffix(f".{target_format}")
    codec = {"mp3": ["-codec:a", "libmp3lame", "-q:a", "2"], "flac": ["-codec:a", "flac"]}[target_format]
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(source), *codec, str(destination)],
        check=True,
        capture_output=True,
        timeout=900,
    )
    return destination


def _convert_seed(job_id: str, job: dict, voice: dict, params: dict, source: Path, target: Path) -> dict:
    result = engines.seed_convert(
        {
            "source_path": str(source),
            "target_path": voice["reference_audio"],
            "output_path": str(target),
            "diffusion_steps": params.get("diffusion_steps", 30),
            "length_adjust": params.get("length_adjust", 1.0),
            "similarity_cfg_rate": params.get("similarity_cfg_rate", 0.7),
            "intelligibility_cfg_rate": params.get("intelligibility_cfg_rate", 0.7),
            "top_p": params.get("top_p", 0.9),
            "temperature": params.get("temperature", 1.0),
            "repetition_penalty": params.get("repetition_penalty", 1.0),
        }
    )
    return {
        "inference_seconds": result.get("inference_seconds"),
        "audio_seconds": result.get("audio_seconds"),
        "realtime_factor": result.get("realtime_factor"),
        "peak_vram_mib": result.get("peak_vram_mib"),
        "sample_rate": result.get("sample_rate"),
    }


def _convert_rvc(job: dict, voice: dict, params: dict, source: Path, target: Path) -> dict:
    if not voice.get("model_path"):
        raise RuntimeError("This RVC voice has no model file on the worker.")
    result = engines.rvc_convert(
        {
            "model_path": voice["model_path"],
            "index_path": voice.get("index_path"),
            "input_path": str(source),
            "output_path": str(target),
            "pitch": params.get("pitch", 0),
            "f0_method": params.get("f0_method", "rmvpe"),
            "index_rate": params.get("index_rate", 0.75),
            "protect": params.get("protect", 0.33),
            "rms_mix_rate": params.get("rms_mix_rate", 1.0),
            "resample_sr": params.get("resample_sr", 0),
        }
    )
    return {
        "inference_seconds": result.get("inference_seconds"),
        "audio_seconds": result.get("audio_seconds"),
        "realtime_factor": result.get("realtime_factor"),
        "sample_rate": result.get("sample_rate"),
    }


def run_job(job_id: str) -> None:
    job = db.get_job(job_id)
    if job is None:
        return

    try:
        params = job["params"]
        kind = job["kind"]
        work = Path(config.OUTPUTS_DIR) / job_id
        work.mkdir(parents=True, exist_ok=True)

        with gpu_lease() if job["lane"] == "gpu" else nullcontext():
            _update(job_id, status="running", started_at=db.now(), progress=0.05)
            if kind == "tts":
                metrics = _run_tts(job, params, work)
            elif kind == "train":
                metrics = _run_training(job, params)
            else:
                metrics = _run_conversion(job, params, work)

        _update(job_id, progress=0.9)
        # Encoding is CPU work. Hand it off so the GPU lane can start its next
        # model request while MP3/FLAC is being written.
        if job["lane"] == "gpu" and params.get("output_format", "wav") != "wav" and kind != "train":
            future = _executors["cpu"].submit(_finish_job, job_id, metrics, params)
            future.add_done_callback(lambda completed: _report_failure(job_id, completed))
        else:
            _finish_job(job_id, metrics, params)
    except Exception as error:  # noqa: BLE001 - surfaced to the client as job status
        _update(job_id, status="failed", error=str(error)[:2000], finished_at=db.now())


def _finish_job(job_id: str, metrics: dict, params: dict) -> None:
    try:
        output = Path(metrics.pop("output_path"))
        if metrics.pop("artifact_kind", None) == "rvc-model":
            delivered = output
        else:
            delivered = transcode(output, params.get("output_format", "wav"))
            metrics["output_format"] = delivered.suffix.lstrip(".")
        db.execute(
            "UPDATE jobs SET status = 'succeeded', output_path = ?, progress = 1.0,"
            " finished_at = ?, error = NULL, metrics_json = ? WHERE id = ?",
            (str(delivered), db.now(), json.dumps(metrics), job_id),
        )
    except Exception as error:  # noqa: BLE001 - preserve an explicit job failure
        _update(job_id, status="failed", error=str(error)[:2000], finished_at=db.now())


def _report_failure(job_id: str, completed: Future) -> None:
    error = completed.exception()
    if error is not None:
        _update(job_id, status="failed", error=f"{type(error).__name__}: {error}"[:2000], finished_at=db.now())


def _run_conversion(job: dict, params: dict, work: Path) -> dict:
    voice = db.get_voice(job["voice_id"]) if job["voice_id"] else None
    if voice is None:
        raise RuntimeError("The selected voice no longer exists.")
    target = work / "output.wav"
    if job["engine"] == "rvc":
        metrics = _convert_rvc(job, voice, params, Path(job["input_path"]), target)
    else:
        if not voice.get("reference_audio"):
            raise RuntimeError("This voice has no reference audio on the worker.")
        metrics = _convert_seed(job["id"], job, voice, params, Path(job["input_path"]), target)
    return {"output_path": str(target), **metrics}


def _run_tts(job: dict, params: dict, work: Path) -> dict:
    base = work / "speech.wav"
    result = engines.tts_speech(
        {
            "text": params.get("text", ""),
            "voice": params.get("tts_voice", "af_heart"),
            "speed": params.get("speed", 1.0),
            "lang_code": params.get("lang_code", "a"),
            "output_path": str(base),
        }
    )
    metrics = {
        "synthesis_seconds": result.get("synthesis_seconds"),
        "characters": result.get("characters"),
        "sample_rate": result.get("sample_rate"),
    }

    voice_id = params.get("voice_id")
    if not voice_id:
        metrics["audio_seconds"] = result.get("audio_seconds")
        return {"output_path": str(base), **metrics}

    # Route the synthesised speech through the selected voice engine.
    voice = db.get_voice(voice_id)
    if voice is None:
        raise RuntimeError("The selected voice no longer exists.")
    converted = work / "output.wav"
    if voice["engine"] == "rvc":
        stage = _convert_rvc(job, voice, params, base, converted)
    else:
        if not voice.get("reference_audio"):
            raise RuntimeError("This voice has no reference audio on the worker.")
        stage = _convert_seed(job["id"], job, voice, params, base, converted)
    return {
        "output_path": str(converted),
        **metrics,
        "audio_seconds": stage.get("audio_seconds"),
        "inference_seconds": stage.get("inference_seconds"),
        "realtime_factor": stage.get("realtime_factor"),
        "engine": voice["engine"],
    }


def _run_training(job: dict, params: dict) -> dict:
    dataset = Path(config.DATASETS_DIR) / job["id"] / "dataset"
    if not dataset.is_dir():
        raise RuntimeError("The training dataset is missing on the worker.")
    _update(job["id"], progress=0.1)
    result = engines.rvc_train(
        {
            "dataset_dir": str(dataset),
            "experiment": params.get("experiment", job["id"]),
            "epochs": params.get("epochs", 200),
            "batch_size": params.get("batch_size", 8),
            "f0": params.get("f0", True),
            "sample_rate_option": params.get("sample_rate_option", "40k"),
        }
    )
    _update(job["id"], progress=0.85)
    return {
        "output_path": result.get("model_path") or "",
        "model_path": result.get("model_path"),
        "index_path": result.get("index_path"),
        "experiment": result.get("experiment"),
        "steps": result.get("steps"),
        "artifact_kind": "rvc-model",
    }


def submit(job_id: str) -> Future:
    job = db.get_job(job_id)
    if job is None:
        raise ValueError(f"Unknown job: {job_id}")
    future = _executors[job["lane"]].submit(run_job, job_id)

    future.add_done_callback(lambda completed: _report_failure(job_id, completed))
    return future


def submit_conversion(job_id: str) -> None:
    submit(job_id)


def remove_job_files(job: dict) -> None:
    for key in ("input_path", "output_path"):
        path = job.get(key)
        if not path:
            continue
        parent = Path(path).parent
        if parent.is_relative_to(Path(config.DATA_ROOT)) and parent.name == job["id"]:
            shutil.rmtree(parent, ignore_errors=True)
    dataset = Path(config.DATASETS_DIR) / job["id"]
    if dataset.is_dir():
        shutil.rmtree(dataset, ignore_errors=True)
