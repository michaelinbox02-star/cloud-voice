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
- **Realtime voice** — live Seed-VC streaming over WebRTC from the desktop
  microphone to the GPU and back into a virtual audio cable, with device
  selection, headphone monitoring and measured per-block telemetry.
- **RVC v2** — import a `.pth` and `.index`, or train one on the worker: slicing,
  pitch tracking, feature extraction, fine-tuning and the retrieval index all run
  on the GPU.
- **Text to speech** — Kokoro reads the script and can pass the result through
  any stored voice, exported as WAV, FLAC or MP3.
- **Backup and restore** — the whole voice library, including RVC models, moves
  between workers in one archive.
- **Provisioning** — `scripts/bootstrap.sh` turns a clean Ubuntu 24.04 server
  with an NVIDIA driver into a working worker, generating credentials and
  validating GPU containers along the way.

Verified on a Tesla V100-SXM3-32GB (driver 580.178.04):

| Measurement | Result |
| --- | --- |
| Offline conversion, 12.5 s clip at 10 steps | 11.0 s, 3.1 GB peak VRAM |
| Realtime per-block inference (balanced) | 134 ms mean, 138 ms p95, 240 ms budget |
| Realtime WebRTC round trip | 47 blocks, zero dropped frames |
| RVC v2 training, 27 slices | preprocess through index in about 5 minutes |
| RVC v2 inference, 12.5 s clip | 13.5 s at 40 kHz |
| Kokoro TTS, 60 characters | 4.0 s of speech in 8.1 s warm |
| Backup round trip | 4.0 MB archive, 2 voices restored |

## Not built yet

RVC realtime streaming (offline RVC works), a packaged installer, and tuning of
the training defaults.

Realtime streaming needs a virtual audio cable on the desktop: the converted
audio is played into it and the call app selects its other end as the
microphone. VB-CABLE and Voicemeeter both work, and the app detects either.

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
npm run app:dev
```

Then open **Server**, enter the host, port, username and private key path, test
the connection, and install. The credential is stored in Windows Credential
Manager, and the management port is reached through an SSH tunnel instead of
being exposed publicly.

## Building the desktop app

```powershell
cd desktop
npm run app:build       # release binary plus NSIS installer
npm run app:portable    # release binary only, no installer
```

The release binary lands in `desktop/src-tauri/target/release/`. The NSIS
installer needs a normal Windows environment: this project was developed in a
sandboxed session where `makensis` took minutes on an empty script, so the
installer is configured and ready but was not produced there. Build it on a
regular machine or in CI.

## Verifying a worker

```bash
bash scripts/bootstrap.sh                 # provision on the GPU host
bash scripts/seed-smoke.sh <ssh-target>   # one real conversion, end to end
bash scripts/api-smoke.sh <ssh-target>    # full API round trip
bash scripts/realtime-smoke.sh <ssh-target>      # streaming pipeline, block by block
bash scripts/realtime-rtc-smoke.sh <ssh-target>  # WebRTC streaming end to end
```

## Repository rules

Never commit credentials, private keys, recordings, datasets or model weights.
Models download straight onto the GPU server and stay there. The `.gitignore`
covers the common cases; check every commit before pushing.
