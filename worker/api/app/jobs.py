"""Background job execution.

One GPU, one conversion at a time: the executor is deliberately single-worker
and the engine service serialises model access as well.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import config, db, engines

_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="cloud-voice-job")


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


def run_conversion(job_id: str) -> None:
    job = db.get_job(job_id)
    if job is None:
        return

    _update(job_id, status="running", started_at=db.now(), progress=0.05)
    try:
        params = job["params"]
        voice = db.get_voice(job["voice_id"]) if job["voice_id"] else None
        if voice is None:
            raise RuntimeError("The selected voice no longer exists.")
        if not voice.get("reference_audio"):
            raise RuntimeError("This voice has no reference audio on the worker.")

        output_wav = Path(config.OUTPUTS_DIR) / job_id / "output.wav"
        payload = {
            "source_path": job["input_path"],
            "target_path": voice["reference_audio"],
            "output_path": str(output_wav),
            "diffusion_steps": params.get("diffusion_steps", 30),
            "length_adjust": params.get("length_adjust", 1.0),
            "similarity_cfg_rate": params.get("similarity_cfg_rate", 0.7),
            "intelligibility_cfg_rate": params.get("intelligibility_cfg_rate", 0.7),
            "top_p": params.get("top_p", 0.9),
            "temperature": params.get("temperature", 1.0),
            "repetition_penalty": params.get("repetition_penalty", 1.0),
        }

        _update(job_id, progress=0.15)
        result = engines.seed_convert(payload)
        _update(job_id, progress=0.9)

        delivered = transcode(output_wav, params.get("output_format", "wav"))
        metrics = {
            "inference_seconds": result.get("inference_seconds"),
            "audio_seconds": result.get("audio_seconds"),
            "realtime_factor": result.get("realtime_factor"),
            "peak_vram_mib": result.get("peak_vram_mib"),
            "sample_rate": result.get("sample_rate"),
            "output_format": delivered.suffix.lstrip("."),
        }
        db.execute(
            "UPDATE jobs SET status = 'succeeded', output_path = ?, progress = 1.0,"
            " finished_at = ?, error = NULL, metrics_json = ? WHERE id = ?",
            (str(delivered), db.now(), json.dumps(metrics), job_id),
        )
    except Exception as error:  # noqa: BLE001 - surfaced to the client as job status
        _update(
            job_id,
            status="failed",
            error=str(error)[:2000],
            finished_at=db.now(),
        )


def submit_conversion(job_id: str) -> None:
    _executor.submit(run_conversion, job_id)


def remove_job_files(job: dict) -> None:
    for key in ("input_path", "output_path"):
        path = job.get(key)
        if not path:
            continue
        parent = Path(path).parent
        if parent.is_relative_to(Path(config.DATA_ROOT)) and parent.name == job["id"]:
            shutil.rmtree(parent, ignore_errors=True)
