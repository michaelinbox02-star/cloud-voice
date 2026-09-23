# Architecture

## Why the split exists

Voice conversion needs CUDA, PyTorch and multi-gigabyte weights. None of that
belongs on a laptop. The desktop keeps the parts that must be local — audio
devices, the interface and the user's own transport — and treats the GPU as
replaceable hardware.

## Components

### Desktop client (`desktop/`)

Tauri 2 shell with a React and TypeScript interface.

- Provisions a worker over SSH and pins the checkout to a verified revision.
- Opens an SSH local port forward to the worker API, so the management port
  stays on loopback at the server and needs no inbound firewall rule.
- Keeps the worker credential in Windows Credential Manager and performs API
  calls in Rust. The webview never receives the token.
- Copies a chosen file into the app cache for waveform rendering and playback
  through the Tauri asset protocol, while uploading from the original path.

### Control plane (`worker/api/`)

FastAPI service owning voices, jobs and artifacts.

| Endpoint | Purpose |
| --- | --- |
| `GET /v1/health` | Liveness plus GPU inventory |
| `GET /v1/system` | GPU, disk, engine status, voice count |
| `GET` / `POST /v1/voices` | List and create voice profiles |
| `GET /v1/voices/{id}/reference` | Fetch the reference clip |
| `DELETE /v1/voices/{id}` | Remove a voice and its files |
| `POST /v1/conversions` | Upload source audio and queue a job |
| `GET /v1/jobs` and `GET /v1/jobs/{id}` | Job status, metrics and errors |
| `GET /v1/jobs/{id}/audio` | Download the finished artifact |
| `POST /v1/engines/seed-vc/warmup` | Load models ahead of the first job |

Jobs run on a single-worker executor because there is one GPU, and the engine
serialises model access as well. A restart marks in-flight jobs failed instead
of leaving the interface waiting forever.

### Engines (`worker/seed/`)

Each model family gets its own container so incompatible dependency sets never
meet. The Seed-VC engine is pinned to the final commit of `Plachtaa/seed-vc`
and exposes `POST /v1/convert` plus a warmup endpoint.

Containers share two volumes: `voice_data` for profiles, uploads and outputs,
and `seed_checkpoints` for model weights. Weights are fetched directly on the
GPU host and persist across container recreation.

## Provisioning

`scripts/bootstrap.sh` validates Ubuntu 24.04 and the NVIDIA driver, installs
Docker and the NVIDIA Container Toolkit when missing, verifies that a CUDA
container can see the GPU, writes credentials to `.env` with mode 600, brings
the stack up and waits for the control plane to answer.

## Performance

Measured on a Tesla V100-SXM3-32GB with Seed-VC V2 at 10 diffusion steps:

| Metric | Value |
| --- | --- |
| Inference, 12.5 s clip | 11.0 s |
| Real-time factor | 0.88 |
| Peak VRAM | 3.1 GB |
| Cold model load | about 80 s |

These numbers say nothing about network latency or live conversion, which will
be measured separately once WebRTC streaming exists.

## Security posture

- The worker API binds to `127.0.0.1` on the GPU host and is reached through an
  SSH tunnel, so it is never exposed publicly.
- Every endpoint requires a bearer token, and the engine services require their
  own separate token.
- Uploads are bounded by size and extension, and engine paths are validated
  against the shared data volume.
- Secrets live in `.env` on the server and in Windows Credential Manager on the
  desktop.

## Roadmap

1. Realtime RVC streaming over WebRTC with measured latency.
2. Realtime Seed-VC streaming.
3. Virtual microphone routing and device management.
4. RVC v2 import, inference and remote training.
5. Kokoro text to speech routed through a selected voice.
6. Library backup, restore and fresh-server recovery.
7. Integration tests against a freshly provisioned server.
