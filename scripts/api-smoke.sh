#!/usr/bin/env bash
# End-to-end check of the worker API: create a voice, convert audio, fetch the
# result. Runs on the GPU worker itself.
# Usage: bash scripts/api-smoke.sh [user@gpu-host]
set -euo pipefail

remote="${1:-}"
repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ -n "${remote}" ]]; then
  exec ssh "${remote}" "cd \"\$HOME/cloud-voice\" && bash scripts/api-smoke.sh"
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

work="$(mktemp -d)"
trap 'rm -rf "${work}"' EXIT

# The reference and source clips ship inside the engine image. Run as the
# current user so the extracted files stay readable and removable.
"${docker_cmd[@]}" run --rm --user "$(id -u):$(id -g)" -v "${work}":/host cloud-voice-seed:dev sh -c \
  'cp examples/reference/s1p1.wav /host/reference.wav && cp examples/source/source_s1.wav /host/source.wav'

json_field() {
  python3 -c "import json,sys; print(json.load(sys.stdin)[sys.argv[1]])" "$1"
}

echo "== health =="
curl -sf -H "Authorization: Bearer ${token}" "${api}/v1/health" | python3 -m json.tool

echo "== system =="
curl -sf -H "Authorization: Bearer ${token}" "${api}/v1/system" | python3 -m json.tool

echo "== create voice =="
voice_json="$(curl -sf -H "Authorization: Bearer ${token}" \
  -F "name=Smoke Voice" \
  -F "engine=seed-vc" \
  -F "description=Created by scripts/api-smoke.sh" \
  -F "reference=@${work}/reference.wav" \
  "${api}/v1/voices")"
voice_id="$(printf '%s' "${voice_json}" | json_field id)"
echo "voice_id=${voice_id}"

echo "== start conversion =="
job_json="$(curl -sf -H "Authorization: Bearer ${token}" \
  -F "voice_id=${voice_id}" \
  -F "engine=seed-vc" \
  -F 'params={"diffusion_steps":10,"output_format":"wav"}' \
  -F "source=@${work}/source.wav" \
  "${api}/v1/conversions")"
job_id="$(printf '%s' "${job_json}" | json_field id)"
echo "job_id=${job_id}"

echo "== wait for job =="
status="queued"
for _ in $(seq 1 300); do
  job_json="$(curl -sf -H "Authorization: Bearer ${token}" "${api}/v1/jobs/${job_id}")"
  status="$(printf '%s' "${job_json}" | json_field status)"
  if [[ "${status}" == "succeeded" || "${status}" == "failed" ]]; then
    break
  fi
  sleep 3
done
printf '%s\n' "${job_json}" | python3 -m json.tool
if [[ "${status}" != "succeeded" ]]; then
  echo "Conversion did not succeed (status=${status})." >&2
  exit 1
fi

echo "== fetch artifact =="
curl -sf -H "Authorization: Bearer ${token}" -o "${work}/converted.wav" "${api}/v1/jobs/${job_id}/audio"
ls -l "${work}/converted.wav"

"${docker_cmd[@]}" run --rm -v "${work}":/host cloud-voice-seed:dev python -c "
import soundfile as sf, numpy as np
a, sr = sf.read('/host/converted.wav')
if a.ndim > 1:
    a = a.mean(axis=1)
rms = float(np.sqrt((a ** 2).mean()))
print('converted:', 'sr=', sr, 'seconds=', round(len(a) / sr, 2), 'rms=', round(rms, 5))
raise SystemExit(0 if rms > 1e-3 else 1)
"
echo "Worker API smoke test passed."
