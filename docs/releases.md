# Build and release record

## 2026-09-25 — stabilization

Worker revision `4edfcc0e78e6319da30b919fa77f8cbc734f7da7`.

### Desktop artifact

| Field | Value |
| --- | --- |
| Path | `desktop/src-tauri/target/release/cloud-voice-studio.exe` |
| Size | 11,325,952 bytes |
| Modified | 2026-09-25 20:22:45 |
| SHA-256 | `6790C8DE792E979F97987729526C9346771D3B5A6A06A420C2B5F44695C9F3A8` |
| Installer pin | `WORKER_RELEASE = 4edfcc0e78e6319da30b919fa77f8cbc734f7da7` |

Verified: size stable across repeated reads, carries the new pin and not the
previous one, and the window opens.

### Worker deployment

Host `77.104.167.148`, SSH port `42407`.

- Checkout moved from `3d0d5aa` to `4edfcc0`.
- Only the **realtime** image was rebuilt and its container recreated. API,
  Seed-VC, RVC and TTS kept their existing uptime, confirming they were not
  restarted.
- No realtime session was active at the moment of the restart (health reported
  `state: idle`).
- `GET /v1/system` afterwards: `seed-vc`, `rvc`, `tts` and `realtime` all
  `ready`.
- Realtime health: `status: ready`, `state: idle`, `inbound_media: tunnel`.
  This host has no relay configured and no public address on its interface, so
  the tunnel is the correct transport here.

### Verification performed

- `python -m unittest discover -s tests -v` — 10/10 passed.
- `python -m py_compile worker/realtime/service.py` — passed.
- `npm run build` (desktop) — passed before the packaging step.
- Deployed container contains the new gathering deadline and 504 error path.

### Build environment note

The first `npm run app:portable` attempt crashed the Rust compiler with
`STATUS_ACCESS_VIOLATION` (exit code `0xc0000005`) while compiling the crate —
not a code error. A retry with no changes succeeded. The host reported 15.8 GB
of RAM with about 6 GB free at the time. If this recurs, clear the crate's
incremental artifacts (`cargo clean -p cloud-voice-studio`) before investigating
source changes.

### Outstanding acceptance tests

Not yet performed; both need the desktop application driven by hand and, for the
second, a microphone:

1. Job isolation across workers — see `NEXT-AGENT-INSTRUCTIONS.md`.
2. Realtime on this host over the tunnel, including the final word after Stop.

### Open defect found after deployment

The tunnelled realtime session closes with WebSocket `1011 keepalive ping
timeout`, and the session reports `0 blocks` — the worker received no audio.
Diagnosed but deliberately not fixed. Full evidence, the two candidate causes and
the tests that separate them are in
[`docs/realtime-tunnel-keepalive.md`](realtime-tunnel-keepalive.md).
