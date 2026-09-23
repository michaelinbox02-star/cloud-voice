"""SQLite persistence for voice profiles and jobs.

The dataset is small (hundreds of rows), so a single connection guarded by a
lock is simpler and safer than a connection pool.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from typing import Any

from . import config

_lock = threading.Lock()
_connection: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS voices (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    engine TEXT NOT NULL,
    description TEXT,
    language TEXT,
    reference_audio TEXT,
    model_path TEXT,
    index_path TEXT,
    settings_json TEXT NOT NULL DEFAULT '{}',
    size_bytes INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    engine TEXT NOT NULL,
    voice_id TEXT,
    params_json TEXT NOT NULL DEFAULT '{}',
    input_path TEXT,
    output_path TEXT,
    error TEXT,
    progress REAL NOT NULL DEFAULT 0,
    metrics_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def connection() -> sqlite3.Connection:
    global _connection
    if _connection is None:
        config.DATA_ROOT.mkdir(parents=True, exist_ok=True)
        _connection = sqlite3.connect(config.DATABASE_PATH, check_same_thread=False)
        _connection.row_factory = sqlite3.Row
        _connection.executescript(SCHEMA)
        _connection.commit()
    return _connection


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    record = dict(row)
    for key in ("settings_json", "params_json", "metrics_json"):
        if key in record:
            record[key.removesuffix("_json")] = json.loads(record.pop(key) or "{}")
    return record


def execute(sql: str, params: tuple = ()) -> None:
    with _lock:
        database = connection()
        database.execute(sql, params)
        database.commit()


def query_one(sql: str, params: tuple = ()) -> dict[str, Any] | None:
    with _lock:
        row = connection().execute(sql, params).fetchone()
    return _row_to_dict(row) if row else None


def query_all(sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    with _lock:
        rows = connection().execute(sql, params).fetchall()
    return [_row_to_dict(row) for row in rows]


def create_voice(
    *,
    voice_id: str,
    name: str,
    engine: str,
    description: str | None,
    language: str | None,
    reference_audio: str | None,
    model_path: str | None = None,
    index_path: str | None = None,
    settings: dict[str, Any] | None = None,
    size_bytes: int = 0,
) -> dict[str, Any]:
    timestamp = now()
    execute(
        "INSERT INTO voices (id, name, engine, description, language, reference_audio, model_path,"
        " index_path, settings_json, size_bytes, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            voice_id,
            name,
            engine,
            description,
            language,
            reference_audio,
            model_path,
            index_path,
            json.dumps(settings or {}),
            size_bytes,
            timestamp,
            timestamp,
        ),
    )
    return get_voice(voice_id)  # type: ignore[return-value]


def get_voice(voice_id: str) -> dict[str, Any] | None:
    return query_one("SELECT * FROM voices WHERE id = ?", (voice_id,))


def list_voices() -> list[dict[str, Any]]:
    return query_all("SELECT * FROM voices ORDER BY created_at DESC")


def delete_voice(voice_id: str) -> None:
    execute("DELETE FROM voices WHERE id = ?", (voice_id,))


def create_job(*, kind: str, engine: str, voice_id: str | None, params: dict[str, Any]) -> dict[str, Any]:
    job_id = new_id("job")
    execute(
        "INSERT INTO jobs (id, kind, status, engine, voice_id, params_json, created_at)"
        " VALUES (?, ?, 'queued', ?, ?, ?, ?)",
        (job_id, kind, engine, voice_id, json.dumps(params), now()),
    )
    return get_job(job_id)  # type: ignore[return-value]


def get_job(job_id: str) -> dict[str, Any] | None:
    return query_one("SELECT * FROM jobs WHERE id = ?", (job_id,))


def list_jobs(limit: int = 50) -> list[dict[str, Any]]:
    return query_all("SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,))


def fail_orphaned_jobs() -> int:
    """Mark jobs left running by a restart as failed so the UI never hangs."""
    stale = query_all("SELECT id FROM jobs WHERE status IN ('queued', 'running')")
    for job in stale:
        execute(
            "UPDATE jobs SET status = 'failed', error = ?, finished_at = ? WHERE id = ?",
            ("Worker restarted while this job was in progress. Re-run it.", now(), job["id"]),
        )
    return len(stale)
