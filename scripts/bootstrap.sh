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

# Resolve the CUDA image before installing Docker or building anything. Query
# every GPU: a mixed host needs a wheel that supports all of its devices.
driver_version="$(nvidia-smi --query-gpu=driver_version --format=csv,noheader,nounits | head -1 | tr -d '[:space:]')"
compute_caps="$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader,nounits 2>/dev/null || true)"
if [[ -z "${compute_caps}" || "${compute_caps}" == *"Not Supported"* ]]; then
  if ! command -v python3 >/dev/null 2>&1; then
    echo "nvidia-smi cannot report compute capability; install python3 for the CUDA driver probe." >&2
    exit 1
  fi
  compute_caps="$(python3 "${repo_dir}/scripts/gpu_capability.py")"
fi
if [[ -z "${driver_version}" || -z "${compute_caps}" ]]; then
  echo "Could not detect NVIDIA driver version and GPU compute capability." >&2
  exit 1
fi

version_at_least() {
  [[ "$(printf '%s\n%s\n' "$1" "$2" | sort -V | head -1)" == "$2" ]]
}

cuda_variant=""
while IFS= read -r cap; do
  cap="$(printf '%s' "${cap}" | tr -d '[:space:]')"
  if [[ ! "${cap}" =~ ^[0-9]+\.[0-9]+$ ]]; then
    echo "Invalid GPU compute capability: '${cap}'." >&2
    exit 1
  fi
  major="${cap%%.*}"
  minor="${cap#*.}"
  if (( major < 7 )); then
    echo "GPU sm_${major}${minor} is unsupported; compute capability 7.0 or newer is required." >&2
    exit 1
  fi
  selected="cu121"
  if (( major > 9 || (major == 9 && minor > 0) )); then selected="cu128"; fi
  if [[ -n "${cuda_variant}" && "${cuda_variant}" != "${selected}" ]]; then
    echo "Mixed GPU generations need different PyTorch builds; use GPUs from one CUDA variant on this worker." >&2
    exit 1
  fi
  cuda_variant="${selected}"
done <<< "${compute_caps}"

if [[ "${cuda_variant}" == "cu128" ]]; then
  minimum_driver="570.26"
  minimum_cuda="12.8"
  torch_version="2.7.1"
  torch_index_url="https://download.pytorch.org/whl/cu128"
  pytorch_base="pytorch/pytorch:2.7.1-cuda12.8-cudnn9-runtime@sha256:c16f4c749e2d9e96878875cdf6cc45cddda1d1a36fddd371dd6f2360f1b6e2a2"
  cuda_test_image="nvidia/cuda:12.8.1-base-ubuntu24.04"
else
  minimum_driver="530.30.02"
  minimum_cuda="12.1"
  torch_version="2.4.0"
  torch_index_url="https://download.pytorch.org/whl/cu121"
  pytorch_base="pytorch/pytorch:2.4.0-cuda12.1-cudnn9-runtime@sha256:68c022c2f4627943a6f3e574cfd2c8ae4256210d5f66ae2b117942e0a8d4fa9d"
  cuda_test_image="nvidia/cuda:12.1.1-base-ubuntu22.04"
fi

if ! version_at_least "${driver_version}" "${minimum_driver}"; then
  echo "GPU requires ${cuda_variant}, but driver ${driver_version} is too old; install NVIDIA driver ${minimum_driver} or newer." >&2
  exit 1
fi
max_cuda="$(nvidia-smi | sed -nE 's/.*CUDA Version: ([0-9]+\.[0-9]+).*/\1/p' | head -1)"
if [[ -n "${max_cuda}" ]] && ! version_at_least "${max_cuda}" "${minimum_cuda}"; then
  echo "Driver reports CUDA ${max_cuda}; ${cuda_variant} requires CUDA ${minimum_cuda} (driver ${minimum_driver} or newer)." >&2
  exit 1
fi
compute_capability="$(printf '%s\n' "${compute_caps}" | sort -Vu | paste -sd, -)"
printf 'GPU compute capability: %s; driver: %s; selected: %s\n' "${compute_capability}" "${driver_version}" "${cuda_variant}"

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

"${SUDO[@]}" docker run --rm --gpus all "${cuda_test_image}" nvidia-smi -L

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
set_env() {
  local name="$1" value="$2"
  sed -i "/^${name}=/d" "${repo_dir}/.env"
  printf '%s=%s\n' "${name}" "${value}" >> "${repo_dir}/.env"
}
set_env CLOUD_VOICE_CUDA_VARIANT "${cuda_variant}"
set_env CLOUD_VOICE_COMPUTE_CAPABILITY "${compute_capability}"
set_env CLOUD_VOICE_DRIVER_VERSION "${driver_version}"
set_env PYTORCH_BASE_IMAGE "${pytorch_base}"
set_env TORCH_VERSION "${torch_version}"
set_env TORCH_INDEX_URL "${torch_index_url}"
if [[ "${cuda_variant}" == "cu128" ]]; then
  set_env RVC_TORCH_VERSION "2.7.1"
  set_env RVC_TORCH_INDEX_URL "https://download.pytorch.org/whl/cu128"
  set_env RVC_CUDA_VARIANT "cu128"
else
  set_env RVC_TORCH_VERSION "2.7.1"
  set_env RVC_TORCH_INDEX_URL "https://download.pytorch.org/whl/cu118"
  set_env RVC_CUDA_VARIANT "cu118"
fi
chmod 600 "${repo_dir}/.env"

cd "${repo_dir}"
"${SUDO[@]}" docker compose --env-file .env -f worker/compose.yaml up -d --build

api_token="$(sed -n 's/^CLOUD_VOICE_API_TOKEN=//p' .env)"
api_ready="no"
for _ in {1..40}; do
  if curl --silent --fail -H "Authorization: Bearer ${api_token}" http://127.0.0.1:8765/v1/health; then
    printf '\nControl plane health check passed.\n'
    api_ready="yes"
    break
  fi
  sleep 3
done
if [[ "${api_ready}" != "yes" ]]; then
  echo "Control plane failed to become healthy; inspect docker compose logs." >&2
  exit 1
fi

# Startup tasks perform the first load. These requests also retry a failed load
# after the API is reachable, without making provisioning wait for downloads.
for engine in seed-vc rvc tts; do
  nohup curl --silent --fail --max-time 1800 \
    -H "Authorization: Bearer ${api_token}" \
    -X POST "http://127.0.0.1:8765/v1/engines/${engine}/warmup" \
    >/dev/null 2>&1 </dev/null &
  disown
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

printf '\nControl plane: healthy\nSeed-VC responding: %s\nRealtime responding: %s\n' "${seed_ready}" "${realtime_ready}"
if [[ "${seed_ready}" != "yes" || "${realtime_ready}" != "yes" ]]; then
  printf 'Engines download models on first start. Check the service logs with: docker compose -f worker/compose.yaml logs\n'
fi
