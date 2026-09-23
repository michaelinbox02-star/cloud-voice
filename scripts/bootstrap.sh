#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source /etc/os-release

# Some rented GPU instances hand you a root shell instead of a user with sudo.
if [[ "$(id -u)" -eq 0 ]]; then
  SUDO=()
elif command -v sudo >/dev/null 2>&1; then
  SUDO=(sudo)
else
  echo "This installer needs root or sudo privileges." >&2
  exit 1
fi

# systemd is absent on container-based hosts; restart the daemon however the
# platform allows, and never let that be fatal on its own.
enable_docker_service() {
  if command -v systemctl >/dev/null 2>&1 && [[ -d /run/systemd/system ]]; then
    "${SUDO[@]}" systemctl enable --now docker
  elif command -v service >/dev/null 2>&1; then
    "${SUDO[@]}" service docker start || true
  else
    echo "No service manager found; assuming the Docker daemon is already running." >&2
  fi
}

restart_docker_service() {
  if command -v systemctl >/dev/null 2>&1 && [[ -d /run/systemd/system ]]; then
    "${SUDO[@]}" systemctl restart docker
  elif command -v service >/dev/null 2>&1; then
    "${SUDO[@]}" service docker restart
  else
    echo "Docker config changed but no service manager is available; restart it manually if the GPU is not visible." >&2
  fi
}
# Every engine runs inside its own container, so the host distribution only has
# to provide a driver, Docker and the NVIDIA runtime. Ubuntu 24.04 is what this
# was built against; anything else Debian-family is accepted with a warning
# rather than refused.
case "${ID:-}" in
  ubuntu | debian) ;;
  *)
    echo "Cloud Voice Studio expects a Debian-family GPU worker (Ubuntu or Debian); found '${ID:-unknown}'." >&2
    exit 1
    ;;
esac
if [[ "${ID}" == "ubuntu" && "${VERSION_ID}" != "24.04" ]]; then
  echo "Note: developed on Ubuntu 24.04. Continuing on Ubuntu ${VERSION_ID}; the containers are unaffected." >&2
fi
if ! command -v nvidia-smi >/dev/null || ! nvidia-smi -L >/dev/null; then
  echo "An NVIDIA driver and working GPU are required." >&2
  exit 1
fi

install_docker_from_distro() {
  "${SUDO[@]}" apt-get update
  "${SUDO[@]}" apt-get install -y docker.io
  # docker-compose-v2 only exists from Ubuntu 23.04 onward; an older host falls
  # through to the upstream repository below.
  "${SUDO[@]}" apt-get install -y docker-compose-v2 || true
  enable_docker_service
}

install_docker_from_upstream() {
  echo "Installing Docker from the official repository..."
  "${SUDO[@]}" apt-get update
  "${SUDO[@]}" apt-get install -y ca-certificates curl gnupg
  "${SUDO[@]}" install -m 0755 -d /etc/apt/keyrings
  curl -fsSL "https://download.docker.com/linux/${ID}/gpg" |
    "${SUDO[@]}" gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  "${SUDO[@]}" chmod a+r /etc/apt/keyrings/docker.gpg
  printf 'deb [arch=%s signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/%s %s stable\n' \
    "$(dpkg --print-architecture)" "${ID}" "${VERSION_CODENAME}" |
    "${SUDO[@]}" tee /etc/apt/sources.list.d/docker.list >/dev/null
  "${SUDO[@]}" apt-get update
  "${SUDO[@]}" apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  enable_docker_service
}

if ! command -v docker >/dev/null 2>&1; then
  install_docker_from_distro || install_docker_from_upstream
fi

# Compose v2 is required: the stack uses `gpus:` and `shm_size:`.
if ! "${SUDO[@]}" docker compose version >/dev/null 2>&1; then
  install_docker_from_upstream
fi

if ! command -v nvidia-container-cli >/dev/null; then
  "${SUDO[@]}" apt-get update
  "${SUDO[@]}" apt-get install -y curl ca-certificates gnupg
  curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey |
    "${SUDO[@]}" gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
  curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list |
    sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' |
    "${SUDO[@]}" tee /etc/apt/sources.list.d/nvidia-container-toolkit.list >/dev/null
  "${SUDO[@]}" apt-get update
  "${SUDO[@]}" apt-get install -y nvidia-container-toolkit
  "${SUDO[@]}" nvidia-ctk runtime configure --runtime=docker
  restart_docker_service
fi

"${SUDO[@]}" docker run --rm --gpus all nvidia/cuda:12.8.1-base-ubuntu24.04 nvidia-smi -L

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
"${SUDO[@]}" docker compose -f worker/compose.yaml up -d --build

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
seed_ready="no"
for _ in {1..10}; do
  if curl --silent --fail -H "Authorization: Bearer ${engine_token}" http://127.0.0.1:8790/health >/dev/null; then
    seed_ready="yes"
    break
  fi
  sleep 3
done

realtime_ready="no"
for _ in {1..10}; do
  if curl --silent --fail http://127.0.0.1:8791/health >/dev/null; then
    realtime_ready="yes"
    break
  fi
  sleep 3
done

printf '\nControl plane: healthy\nSeed-VC engine: %s\nRealtime engine: %s\n' "${seed_ready}" "${realtime_ready}"
if [[ "${seed_ready}" != "yes" || "${realtime_ready}" != "yes" ]]; then
  printf 'Engines download models on first start. Check the service logs with: docker compose -f worker/compose.yaml logs\n'
fi
