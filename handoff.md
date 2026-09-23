# Handoff

State as of worker revision `630270b`, desktop `main`.

## What is working, and how it was verified

Everything below was run against the real GPU worker (Tesla V100-SXM3-32GB,
driver 580.178.04), not simulated.

| Capability | Evidence |
| --- | --- |
| Provisioning | `scripts/bootstrap.sh` on a clean Ubuntu 24.04 host installs Docker/NVIDIA runtime, generates credentials, starts all five services and passes health checks |
| Seed-VC offline | 12.5 s clip converted in 11.0 s, 3.1 GB peak VRAM |
| Seed-VC realtime over WebRTC | 46 blocks of 240 ms, 158 ms mean inference, **0 dropped frames**, 11.6 s of converted audio returned |
| RVC v2 training | 27 slices, preprocess → F0 → HuBERT → train → index in ~5 min; produced a 53 MB `.pth` and 13 MB `.index` |
| RVC v2 inference | 12.46 s of audio in 13.5 s at 40 kHz with the model it had just trained |
| Kokoro TTS | 4.0 s of speech from 60 characters in 8.1 s warm (CPU), 24.8 s cold while the model downloaded |
| Backup / restore | 4.0 MB archive exported, restored, 2 voices round-tripped intact |

Listen to the results: `samples/seed-smoke-out.wav`, `samples/seed-smoke-source.wav`,
`samples/rvc-smoke-out.wav`.

## Where things live

- Desktop client: `desktop/` (Tauri 2 + React). Release binary at
  `desktop/src-tauri/target/release/cloud-voice-studio.exe`.
- Control plane: `worker/api/` — voices, jobs, artifacts, TTS, training, backup.
- Engines, one container each: `worker/seed/` (Seed-VC offline), `worker/realtime/`
  (Seed-VC streaming over WebRTC), `worker/rvc/` (RVC inference + training),
  `worker/tts/` (Kokoro).
- Worker host: `riftuser@66.172.10.245`, checkout in `~/cloud-voice`, deployed at
  the revision pinned by `WORKER_RELEASE` in `desktop/src-tauri/src/lib.rs`.
- Secrets: `.env` on the worker (mode 600) and Windows Credential Manager on the
  desktop. Nothing sensitive is committed.

## Verification commands

```bash
bash scripts/bootstrap.sh                          # provision
bash scripts/seed-smoke.sh <ssh-target>            # offline Seed-VC
bash scripts/api-smoke.sh <ssh-target>             # full API round trip
bash scripts/realtime-smoke.sh <ssh-target>        # streaming pipeline, block by block
bash scripts/realtime-rtc-smoke.sh <ssh-target>    # WebRTC end to end
```

## Known gaps and sharp edges

1. **Nobody has looked at the UI.** It type-checks and the binary launches, but no
   human or vision tool has confirmed the screens render correctly.
2. **WebView2 microphone access is unverified on the user's machine.** wry only
   auto-grants clipboard, so the app now passes
   `--use-fake-ui-for-media-stream` in `additionalBrowserArgs`. If Go Live still
   fails with a permission error, the fallback is native capture with `cpal`.
3. **VB-CABLE must be installed** for realtime routing, and Windows hides device
   names until the app holds microphone permission. The Realtime page now explains
   both cases inline.
4. **No NSIS installer.** `makensis` takes minutes even on an empty script inside
   the sandbox. Use `npm run app:portable`, or build the installer on a normal
   machine (`npm run app:build`).
5. **Realtime is Seed-VC only.** RVC realtime would be lower latency; the RVC
   repo ships `infer/rtrvc.py` for it but it needs a separate streaming transport
   and its own session wiring.
6. **Latency is unmeasured from a real desktop.** The worker is in Los Angeles.
   Blocking is 2 × block_time plus inference plus one network leg. Expect roughly
   450–500 ms on the low-latency preset and 700–800 ms on balanced from the East
   Coast.
7. **TTS runs on CPU** (~2× realtime for short text). Move it to CUDA or batch
   sentences if that matters.
8. **Training dataset upload is a zip.** A folder picker that zips client-side
   would be friendlier, and progress is coarse (0.1 → 0.85 → 1.0) because the
   engine only reports per stage.
9. **One job at a time.** The executor is single-worker by design (one GPU), so a
   long training run blocks conversions until it finishes.
10. **RVC training defaults** (200 epochs, batch 8) are untuned for quality; the
    smoke test used 60 epochs on 27 slices.

## Recommended next steps

1. Run the desktop app, walk every page, and fix whatever looks wrong.
2. Confirm realtime end to end from the desktop: mic → worker → virtual cable →
   Discord.
3. Add RVC realtime streaming for lower latency.
4. Build and publish the NSIS installer on a non-sandboxed machine.
5. Add per-stage training progress (parse the trainer's stdout for epoch lines).
