#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source /etc/os-release
if [[ "${ID}" != "ubuntu" || "${VERSION_ID}" != "24.04" ]]; then
  echo "Cloud Voice Studio currently supports Ubuntu 24.04 GPU workers." >&2
  exit 1
fi
if ! command -v nvidia-smi >/dev/null || ! nvidia-smi -L >/dev/null; then
  echo "An NVIDIA driver and working GPU are required." >&2
  exit 1
fi

if ! command -v docker >/dev/null; then
  sudo apt-get update
  sudo apt-get install -y docker.io docker-compose-v2
  sudo systemctl enable --now docker
fi

if ! sudo docker compose version >/dev/null 2>&1; then
  sudo apt-get update
  sudo apt-get install -y docker-compose-v2
fi

if ! command -v nvidia-container-cli >/dev/null; then
  sudo apt-get update
  sudo apt-get install -y curl ca-certificates gnupg
  curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey |
    sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
  curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list |
    sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' |
    sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list >/dev/null
  sudo apt-get update
  sudo apt-get install -y nvidia-container-toolkit
  sudo nvidia-ctk runtime configure --runtime=docker
  sudo systemctl restart docker
fi

sudo docker run --rm --gpus all nvidia/cuda:12.8.1-base-ubuntu24.04 nvidia-smi -L

# Create any missing secret without disturbing the ones already in use.
umask 077
touch "${repo_dir}/.env"
ensure_secret() {
  local name="$1"
  local current
  current="$(sed -n "s/^${name}=//p" "${repo_dir}/.env")"
  if [[ -z "${current}" ]]; then
    printf '%s=%s\n' "${name}" "$(openssl rand -hex 32)" >> "${repo_dir}/.env"
  fi
}
ensure_secret CLOUD_VOICE_API_TOKEN
ensure_secret CLOUD_VOICE_ENGINE_TOKEN
chmod 600 "${repo_dir}/.env"

cd "${repo_dir}"
sudo docker compose -f worker/compose.yaml up -d --build

api_token="$(sed -n 's/^CLOUD_VOICE_API_TOKEN=//p' .env)"
for _ in {1..40}; do
  if curl --silent --fail -H "Authorization: Bearer ${api_token}" http://127.0.0.1:8765/v1/health; then
    printf '\nControl plane health check passed.\n'
    break
  fi
  sleep 3
done

# Engine containers pull multi-gigabyte checkpoints on first start. Report
# status without blocking, so provisioning still succeeds on a slow link.
engine_token="$(sed -n 's/^CLOUD_VOICE_ENGINE_TOKEN=//p' .env)"
for _ in {1..10}; do
  if curl --silent --fail -H "Authorization: Bearer ${engine_token}" http://127.0.0.1:8790/health; then
    printf '\nSeed-VC engine is answering.\n'
    exit 0
  fi
  sleep 3
done
printf '\nControl plane is healthy. The Seed-VC engine is still starting; check: sudo docker compose -f worker/compose.yaml logs seed\n'
