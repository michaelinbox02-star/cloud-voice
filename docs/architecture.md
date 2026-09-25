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
| `GET /v1/system` | GPU, driver, CUDA variant, disk, engine readiness, voice count |
| `GET` / `POST /v1/voices` | List and create voice profiles |
| `GET /v1/voices/{id}/reference` | Fetch the reference clip |
| `DELETE /v1/voices/{id}` | Remove a voice and its files |
| `POST /v1/conversions` | Upload source audio and queue a job |
| `GET /v1/jobs` and `GET /v1/jobs/{id}` | Job status, metrics and errors |
| `GET /v1/jobs/{id}/audio` | Download the finished artifact |
| `POST /v1/engines/seed-vc/warmup` | Load models ahead of the first job |
| `POST /v1/engines/rvc/warmup` | Fetch RVC inference and training assets |
| `POST /v1/engines/tts/warmup` | Load the default Kokoro pipeline and voice |

Jobs declare a resource lane in SQLite. The GPU lane has one worker for offline
conversion and training; CPU has two workers for TTS, encoding and backup
packaging; IO has one worker for restore. TTS routed through a stored voice uses
the GPU lane. A file lock in `voice_data` also excludes GPU jobs while a realtime
session is active. Realtime returns a busy error if a GPU job holds that lock.
After GPU conversion, MP3/FLAC encoding is handed to the CPU lane. A restart
marks in-flight jobs failed instead of leaving the interface waiting forever.

### Engines (`worker/seed/`)

Each model family gets its own container so incompatible dependency sets never
meet. The Seed-VC engine is pinned to the final commit of `Plachtaa/seed-vc`
and exposes `POST /v1/convert` plus a warmup endpoint.

Containers share `voice_data` for profiles, uploads, outputs and readiness,
and `seed_checkpoints` for model weights. Weights are fetched directly on the
GPU host and persist across container recreation.

### Realtime engine (`worker/realtime/`)

#### Realtime transport and NAT

A rented GPU is usually behind the provider's NAT, so it cannot receive inbound
UDP and ICE has no candidate pair to try. Three transports exist, in order of
quality:

1. **Direct WebRTC** — requires a public IP on the worker. Lowest latency.
2. **WebRTC through a relay (TURN)** — works behind NAT because both peers
   connect outbound to a public relay. Slightly higher latency than direct.
3. **Tunnel over SSH** — always works, but it is TCP, so it pays an extra round
   trip and head-of-line blocking.

The worker publishes its ICE configuration through `/health` so both peers
negotiate with the same servers. Configure a relay in `.env`:

```
CLOUD_VOICE_TURN_URLS=turn:your-relay.example.com:3478
CLOUD_VOICE_TURN_USERNAME=cloudvoice
CLOUD_VOICE_TURN_CREDENTIAL=<secret>
```

Notes learned the hard way:

- aiortc supports TURN over **UDP and TCP**, but not TURN over TLS, so a
  `turns:` URL will not be used by the worker.
- The relay must be reachable from *both* the worker and the desktop.
- Free anonymous relays are effectively gone; expect to run coturn on a small
  public VPS, or use a hosted service with an account.
- The defaults in `worker/realtime/service.py` point at a public free relay.
  It answers but rejects allocations, so a relay must be configured before
  WebRTC can be used on a NATed worker.

The Seed-VC realtime fork drives its pipeline from a PortAudio callback. Here
the transport is WebRTC instead: `engine.py` feeds the same `_process_block`
pipeline from network audio, one block in and one block out.

This container runs with host networking because aiortc gathers its own UDP
ports for media, which cannot be published from a bridge network. Signalling
still binds to `127.0.0.1` and is reached through the desktop's SSH tunnel,
while media flows over UDP to the host's public address.

Session authorisation is a short-lived ticket. The control plane mints it,
writes it into the shared data volume and returns the token to the desktop; the
realtime engine validates that ticket, so the long-lived worker credential is
never used for media.

At startup, realtime reloads the most recently used voice when its reference
still exists. It writes a short-lived status heartbeat to the shared volume;
the control plane reports the engine unavailable if that heartbeat stops.

### RVC engine (`worker/rvc/`)

Inference and training both run through the project's own CLIs so behaviour
matches upstream. Training is five sequenced stages driven by the control plane:
preprocess, F0 extraction, HuBERT feature extraction, `train.py`, then the
retrieval index.

Two details are not documented upstream and had to be reproduced from the
WebUI's code path:

- `config.json` is copied from the shipped templates (`configs/v1/40k.json` for
  40k, `configs/v2/*.json` otherwise), not from the pretrained checkpoint.
- `filelist.txt` is written in memory by the WebUI and never by a CLI run, so the
  engine builds it: ground-truth wav, feature `.npy`, the two pitch tracks, the
  speaker id, plus two silence rows.

The container needs `shm_size: 8gb`; Docker's 64 MB default kills the trainer's
dataloader workers mid-epoch.

### TTS engine (`worker/tts/`)

Kokoro on CPU, deliberately, so synthesis never competes with conversion for GPU
memory. The control plane optionally routes the synthesised audio through a
Seed-VC or RVC voice afterwards.

### Backup format

A gzipped tar of `voice_data`: the SQLite database plus the entire `voices/`
tree, which holds Seed-VC reference clips and RVC `.pth`/`.index` files. Restore
extracts to a staging directory, rejects unsafe paths, and merges over the
existing library.

## Provisioning

`scripts/bootstrap.sh` validates Ubuntu 24.04, detects compute capability and
driver support before installing Docker, selects a pinned CUDA 12.1 or 12.8
PyTorch image, and records that choice in `.env`. It then installs
Docker and the NVIDIA Container Toolkit when missing, verifies that a CUDA
container can see the GPU, writes credentials to `.env` with mode 600, brings
the stack up and waits for the control plane to answer. Engines warm their
models or assets on startup and report `warming`, `ready` or `unavailable`.

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
