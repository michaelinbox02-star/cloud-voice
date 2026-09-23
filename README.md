# Cloud Voice Studio

A Windows desktop client for AI voice conversion backed by a disposable Linux
NVIDIA GPU worker. The desktop handles audio devices, transport and the
interface. Every model runs on the rented GPU.

## What works today

- **Desktop client** — Tauri 2, React and TypeScript. GPU setup wizard, SSH
  test, one-click worker install, voice library and a voice-to-voice studio
  with waveform preview, A/B playback and WAV/FLAC/MP3 export.
- **Worker control plane** — authenticated REST API for health, system status,
  voice profiles, conversion jobs and artifact delivery, with a SQLite record
  of every voice and job.
- **Seed-VC engine** — zero-shot voice conversion from a short reference clip,
  running in its own CUDA container pinned to the final upstream commit.
- **Provisioning** — `scripts/bootstrap.sh` turns a clean Ubuntu 24.04 server
  with an NVIDIA driver into a working worker, generating credentials and
  validating GPU containers along the way.

Verified on a Tesla V100-SXM3-32GB (driver 580.178.04): a 12.5-second clip
converted in 11.0 seconds at 10 diffusion steps, 3.1 GB peak VRAM.

## Not built yet

Realtime streaming over WebRTC, virtual microphone routing, RVC v2 inference
and training, Kokoro text to speech, and library backup or restore. Those
screens exist in the interface and say so rather than presenting dead controls.

## Layout

| Path | Purpose |
| --- | --- |
| `desktop/` | Tauri 2 + React desktop client |
| `worker/api/` | Control plane: voices, jobs, artifacts |
| `worker/seed/` | Seed-VC inference engine container |
| `scripts/` | Bootstrap, GPU smoke test, API integration test |
| `docs/` | Architecture and operational notes |

## Requirements

Desktop: Windows 10/11, WebView2, and an SSH key that can log in to the worker.

Worker: Ubuntu 24.04, an NVIDIA GPU with a working driver, and passwordless
sudo for the provisioning account.

## Running the desktop app

```powershell
cd desktop
npm install
npm run tauri dev
```

Then open **Server**, enter the host, port, username and private key path, test
the connection, and install. The credential is stored in Windows Credential
Manager, and the management port is reached through an SSH tunnel instead of
being exposed publicly.

## Verifying a worker

```bash
bash scripts/bootstrap.sh                 # provision on the GPU host
bash scripts/seed-smoke.sh <ssh-target>   # one real conversion, end to end
bash scripts/api-smoke.sh <ssh-target>    # full API round trip
```

## Repository rules

Never commit credentials, private keys, recordings, datasets or model weights.
Models download straight onto the GPU server and stay there. The `.gitignore`
covers the common cases; check every commit before pushing.
