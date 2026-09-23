import os
import urllib.request


request = urllib.request.Request(
    "http://127.0.0.1:8765/v1/health",
    headers={"Authorization": f"Bearer {os.environ['CLOUD_VOICE_API_TOKEN']}"},
)
with urllib.request.urlopen(request, timeout=5) as response:
    if response.status != 200:
        raise SystemExit(1)
