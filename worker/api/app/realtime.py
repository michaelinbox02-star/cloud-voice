"""Short-lived realtime session tickets.

The control plane mints a ticket, writes it into the shared data volume and
hands the token to the desktop. The realtime engine validates that ticket, so
the long-lived API credential is never used for media and never leaves the
desktop's credential store.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from pathlib import Path

from . import config

TICKET_DIR: Path = config.DATA_ROOT / "realtime-sessions"
SESSION_TTL_SECONDS = 600


def _prune() -> None:
    now = time.time()
    for path in TICKET_DIR.glob("*.json"):
        try:
            if float(json.loads(path.read_text()).get("expires_at", 0)) < now:
                path.unlink(missing_ok=True)
        except (OSError, json.JSONDecodeError):
            path.unlink(missing_ok=True)


def create_ticket(
    *,
    voice: dict,
    preset: str,
    diffusion_steps: int | None,
    inference_cfg_rate: float,
) -> dict:
    TICKET_DIR.mkdir(parents=True, exist_ok=True)
    _prune()

    session_id = secrets.token_hex(8)
    token = secrets.token_urlsafe(24)
    expires_at = time.time() + SESSION_TTL_SECONDS
    ticket = {
        "session_id": session_id,
        "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
        "voice_id": voice["id"],
        "voice_name": voice["name"],
        "reference_path": voice["reference_audio"],
        "preset": preset,
        "diffusion_steps": diffusion_steps,
        "inference_cfg_rate": inference_cfg_rate,
        "expires_at": expires_at,
    }
    (TICKET_DIR / f"{session_id}.json").write_text(json.dumps(ticket))
    return {
        "session_id": session_id,
        "token": token,
        "expires_at": expires_at,
        "voice_id": voice["id"],
        "voice_name": voice["name"],
        "preset": preset,
    }


def delete_ticket(session_id: str) -> None:
    if session_id.replace("-", "").isalnum():
        (TICKET_DIR / f"{session_id}.json").unlink(missing_ok=True)
