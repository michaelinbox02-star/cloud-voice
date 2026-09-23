#!/usr/bin/env bash
# End-to-end WebRTC check: mint a session, stream a file through the worker over
# real UDP media, and verify the converted audio that comes back.
# Usage: bash scripts/realtime-rtc-smoke.sh [user@gpu-host]
set -euo pipefail

remote="${1:-}"
repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ -n "${remote}" ]]; then
  exec ssh "${remote}" "cd \"\$HOME/cloud-voice\" && bash scripts/realtime-rtc-smoke.sh"
fi

cd "${repo_dir}"
api="http://127.0.0.1:8765"
token="$(sed -n 's/^CLOUD_VOICE_API_TOKEN=//p' .env)"
if [[ -z "${token}" ]]; then
  echo "No worker credential in .env. Run scripts/bootstrap.sh first." >&2
  exit 1
fi

docker_cmd=(docker)
if ! docker info >/dev/null 2>&1; then
  docker_cmd=(sudo docker)
fi

voice_id="$(curl -sf -H "Authorization: Bearer ${token}" "${api}/v1/voices" |
  python3 -c 'import json,sys; voices=json.load(sys.stdin)["voices"]; print(next((v["id"] for v in voices if v["engine"]=="seed-vc"), ""))')"
if [[ -z "${voice_id}" ]]; then
  echo "No Seed-VC voice on the worker. Run scripts/api-smoke.sh first." >&2
  exit 1
fi

# Copy a known source clip into the realtime volume: the engine image ships the
# upstream example audio, and the client reads it from there.
"${docker_cmd[@]}" run --rm --user "$(id -u):$(id -g)" \
  -v cloud-voice-realtime-data:/out cloud-voice-seed:dev \
  sh -c 'cp examples/source/source_s1.wav /out/rtc-source.wav'

echo "Streaming a real file through the worker over WebRTC..."
# The client runs inside the realtime image (it has aiortc) with host
# networking, so the media path matches a remote desktop client.
"${docker_cmd[@]}" run --rm --network host \
  -e "CLOUD_VOICE_API_TOKEN=${token}" \
  -e "CLOUD_VOICE_VOICE_ID=${voice_id}" \
  -e "CLOUD_VOICE_SOURCE=/data/rtc-source.wav" \
  -e "CLOUD_VOICE_OUTPUT=/data/realtime-rtc-out.wav" \
  -e "CLOUD_VOICE_SECONDS=12" \
  -v cloud-voice-realtime-data:/data \
  -v "${repo_dir}/worker/realtime/client_test.py:/opt/seed-vc-realtime/client_test.py:ro" \
  cloud-voice-realtime:dev \
  python client_test.py

"${docker_cmd[@]}" run --rm -v cloud-voice-realtime-data:/data cloud-voice-realtime:dev \
  python -c "import os,sys; p='/data/realtime-rtc-out.wav'; size=os.path.getsize(p) if os.path.exists(p) else 0; print('artifact bytes:', size); sys.exit(0 if size > 100000 else 1)"
echo "Realtime WebRTC smoke test passed."
