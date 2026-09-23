#!/usr/bin/env bash
# Build the Seed-VC engine image and run one real conversion on the GPU.
# Usage:
#   bash scripts/seed-smoke.sh                 # run on this host
#   bash scripts/seed-smoke.sh user@gpu-host   # run on a remote GPU worker
set -euo pipefail

remote="${1:-}"
repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ -n "${remote}" ]]; then
  exec ssh "${remote}" "cd \"\$HOME/cloud-voice\" && bash scripts/seed-smoke.sh"
fi

token="${CLOUD_VOICE_ENGINE_TOKEN:-smoke-test-token}"
docker_cmd=(docker)
if ! docker info >/dev/null 2>&1; then
  docker_cmd=(sudo docker)
fi

"${docker_cmd[@]}" build -t cloud-voice-seed:dev "${repo_dir}/worker/seed"

"${docker_cmd[@]}" rm -f cloud-voice-seed-smoke >/dev/null 2>&1 || true

"${docker_cmd[@]}" run -d --name cloud-voice-seed-smoke --gpus all \
  -e "CLOUD_VOICE_ENGINE_TOKEN=${token}" \
  -v cloud-voice-seed-checkpoints:/opt/seed-vc/checkpoints \
  -v cloud-voice-seed-data:/data \
  -p 127.0.0.1:8790:8790 \
  cloud-voice-seed:dev

cleanup() {
  "${docker_cmd[@]}" rm -f cloud-voice-seed-smoke >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "Waiting for the engine to answer on /health..."
for _ in $(seq 1 60); do
  if curl -sf -H "Authorization: Bearer ${token}" http://127.0.0.1:8790/health >/dev/null; then
    break
  fi
  sleep 2
done

curl -sf -H "Authorization: Bearer ${token}" http://127.0.0.1:8790/health
echo

"${docker_cmd[@]}" exec cloud-voice-seed-smoke bash -lc '
  set -e
  mkdir -p /data/uploads/smoke /data/outputs/smoke
  cp examples/source/source_s1.wav /data/uploads/smoke/source.wav
  cp examples/reference/s1p1.wav /data/uploads/smoke/target.wav
'

echo "Running a real conversion (first run downloads checkpoints from Hugging Face)..."
curl -sf --max-time 3600 -H "Authorization: Bearer ${token}" -H "Content-Type: application/json" \
  -X POST http://127.0.0.1:8790/v1/convert \
  -d '{"source_path":"/data/uploads/smoke/source.wav","target_path":"/data/uploads/smoke/target.wav","output_path":"/data/outputs/smoke/out.wav","diffusion_steps":10}'
echo

"${docker_cmd[@]}" run --rm -v cloud-voice-seed-data:/data alpine:3.20 \
  sh -c 'ls -l /data/outputs/smoke && test -s /data/outputs/smoke/out.wav'
echo "Seed-VC smoke test passed."
