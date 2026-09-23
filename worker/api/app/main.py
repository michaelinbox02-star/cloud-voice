import hmac
import os
import subprocess
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, Header, HTTPException


app = FastAPI(title="Cloud Voice Studio Worker", version="0.1.0", docs_url=None, redoc_url=None)


def require_token(authorization: str | None = Header(default=None)) -> None:
    expected = os.environ.get("CLOUD_VOICE_API_TOKEN", "")
    supplied = authorization.removeprefix("Bearer ") if authorization else ""
    if not expected or not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="Invalid worker credential")


def gpu_info() -> list[dict[str, str]]:
    result = subprocess.run(
        ["nvidia-smi", "--query-gpu=name,driver_version,memory.total", "--format=csv,noheader,nounits"],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    if result.returncode != 0:
        return []
    return [
        {"name": values[0], "driver": values[1], "memory_mib": values[2]}
        for line in result.stdout.splitlines()
        if len(values := [value.strip() for value in line.split(",")]) == 3
    ]


@app.get("/v1/health", dependencies=[Depends(require_token)])
def health() -> dict:
    gpus = gpu_info()
    return {
        "status": "ready" if gpus else "degraded",
        "time": datetime.now(timezone.utc).isoformat(),
        "gpus": gpus,
        "capabilities": ["health"],
    }
