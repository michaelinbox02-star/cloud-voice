"""Runtime configuration for the worker control plane."""

from __future__ import annotations

import os
from pathlib import Path


def _path(name: str, default: str) -> Path:
    return Path(os.environ.get(name, default)).resolve()


DATA_ROOT: Path = _path("CLOUD_VOICE_DATA_ROOT", "/data")
VOICES_DIR: Path = DATA_ROOT / "voices"
UPLOADS_DIR: Path = DATA_ROOT / "uploads"
OUTPUTS_DIR: Path = DATA_ROOT / "outputs"
DATABASE_PATH: Path = DATA_ROOT / "cloud-voice.sqlite3"

API_TOKEN: str = os.environ.get("CLOUD_VOICE_API_TOKEN", "")
ENGINE_TOKEN: str = os.environ.get("CLOUD_VOICE_ENGINE_TOKEN", "")
SEED_ENGINE_URL: str = os.environ.get("CLOUD_VOICE_SEED_URL", "http://seed:8790")

MAX_UPLOAD_BYTES: int = int(os.environ.get("CLOUD_VOICE_MAX_UPLOAD_BYTES", str(512 * 1024 * 1024)))
AUDIO_SUFFIXES: frozenset[str] = frozenset({".wav", ".flac", ".mp3", ".m4a", ".ogg", ".opus", ".aac"})


def ensure_directories() -> None:
    for directory in (DATA_ROOT, VOICES_DIR, UPLOADS_DIR, OUTPUTS_DIR):
        directory.mkdir(parents=True, exist_ok=True)
