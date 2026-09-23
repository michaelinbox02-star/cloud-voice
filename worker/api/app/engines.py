"""Clients for the isolated per-model engine services."""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from . import config


class EngineError(RuntimeError):
    """Raised when an engine rejects or fails a request."""


def _post(url: str, payload: dict, timeout: int) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {config.ENGINE_TOKEN}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors="replace")
        raise EngineError(f"Engine returned HTTP {error.code}: {detail}") from error
    except urllib.error.URLError as error:
        raise EngineError(f"Engine unreachable: {error.reason}") from error


def _get(url: str, timeout: int = 15) -> dict:
    request = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {config.ENGINE_TOKEN}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as error:
        raise EngineError(f"Engine returned HTTP {error.code}") from error
    except urllib.error.URLError as error:
        raise EngineError(f"Engine unreachable: {error.reason}") from error


def seed_health() -> dict:
    return _get(f"{config.SEED_ENGINE_URL}/health")


def seed_warmup() -> dict:
    return _post(f"{config.SEED_ENGINE_URL}/v1/warmup", {}, timeout=1800)


def seed_convert(payload: dict) -> dict:
    return _post(f"{config.SEED_ENGINE_URL}/v1/convert", payload, timeout=3600)
