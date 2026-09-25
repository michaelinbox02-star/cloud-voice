# PR #1 description (draft)

Text prepared for the pull request body. Paste it in, or use it to review what
the branch actually delivers. Evidence is attributed by who ran it.

---

## What this changes

Implements both updates from `docs/proposed-updates.md`, plus two defects found
while testing them.

### Update 1 — warm start and per-engine scheduling

- Engines preload their models at container start and expose
  `ready` / `warming` / `unavailable` through `/v1/system`.
- `/v1/engines/{seed-vc,rvc,tts}/warmup` let the control plane warm an engine on
  demand.
- Job scheduling is split into lanes: one `gpu` lane (Seed-VC offline, Seed-VC
  realtime, RVC convert, RVC train), a `cpu` lane for TTS and transcoding, and an
  `io` lane for backup and restore.
- A `gpu.lock` file in the shared volume coordinates the API's GPU lane with the
  realtime container, which is a separate process.
- Audio encoding moves to the CPU lane so the GPU lane is released before the
  file is transcoded.

### Update 2 — GPU capability detection and CUDA variants

- `scripts/bootstrap.sh` reads compute capability (via
  `nvidia-smi --query-gpu=compute_cap`, with a `libcuda` fallback in
  `scripts/gpu_capability.py`) and the driver's maximum CUDA version.
- `cu121` is selected for `sm_70`–`sm_90`; `cu128` for anything newer, which
  brings Blackwell into scope.
- Hosts below `sm_70`, mixed-generation hosts, and drivers older than the
  selected variant requires are rejected before anything is built, naming the
  required version.
- The selected variant, compute capability and driver version are written to
  `.env`, passed to the image builds as build arguments, and reported by
  `/v1/system`.

### Defects fixed

- **Realtime dropped or truncated short speech.** The pinned upstream engine
  returned audio delayed by `extra_time_right`, so its VAD gate silenced output
  for input that had already stopped. `worker/realtime/patch_upstream.py` applies
  a checked patch at image build (it fails the build if the upstream text
  changes), and each preset now sets its own right context and diffusion steps.
  Stopping the stream keeps the track alive briefly so the final word plays out.
- **Voice-to-voice rejected RVC voices.** The conversion page sent `seed-vc` for
  a trained RVC voice, producing HTTP 400. The desktop now sends the selected
  voice's own engine and shows engine-appropriate controls, and the API treats
  the stored voice's engine as authoritative so older builds keep working.

## Evidence

Verified by the implementing agent on the worker:

| Check | Result |
| --- | --- |
| Realtime short-utterance GPU test, all three presets | Low latency 83 ms mean / 0.84 s active / peak RMS 0.419; Balanced 114 ms / 0.96 s / 0.315; Quality 157 ms / 0.80 s / 0.293 |
| Old Low latency baseline on the same clip | silence, peak 0, no active blocks |
| RVC conversion via `/v1/conversions` with `engine=seed-vc` | HTTP 202, job `job_9332c47ef5824299` succeeded |
| `python -m unittest discover -s tests -v` | 3 lane tests pass |

Verified independently on the branch tip:

| Check | Result |
| --- | --- |
| `npm run build` (desktop) | passes |
| `python -m compileall -q worker scripts tests` | passes |
| GPU selection matrix, real bootstrap logic against stubbed hardware | 4090 `sm_89` → cu121; V100 `sm_70` → cu121; H100 `sm_90` → cu121; 5090 `sm_120` → cu128; B200 `sm_100` → cu128; 5090 on driver 560 → rejected naming driver 570.26; P100 `sm_60` → rejected as unsupported; mixed `8.9 + 12.0` → rejected |

## Not verified — do not treat as passed

- **No Blackwell or old-driver host was available.** The selection *logic* is
  verified, but a real `sm_120` worker running a `cu128` build has not been
  exercised. Nothing here proves the cu128 images actually run on a 5090.
- **First offline conversion under 20 s after a fresh deploy** — needs a full
  worker deployment with all five images rebuilt. The last deployment rebuilt
  only the API and realtime containers.
- **TTS progressing while RVC training runs**, and **strict GPU serialisation
  including realtime**, are covered by unit tests but not by a live GPU run.
- **Desktop warming-state display** has not been seen on a worker in the warming
  state.
- **Live microphone → WebRTC → virtual cable audio, and final-word behaviour
  after Stop**, have not been listened to. The smoke test measures activity and
  onset, not perceived quality or latency.
- **The updated voice-to-voice UI** has not been visually validated.

## How to verify the remainder

1. Run `scripts/bootstrap.sh` on the active worker so all five images match this
   branch, then time the first offline conversion.
2. Start RVC training, then start a TTS job, and confirm the TTS job completes
   without waiting. Start two conversions and confirm the second waits.
3. Open the desktop app, go live in each preset, and listen for complete starts
   and endings — particularly Stop immediately after the last word.
4. Convert a real speech file with the trained RVC voice.
5. On Blackwell and on a too-old driver, confirm the bootstrap selection and
   early failure.

## Notes for review

- `desktop/src-tauri/src/lib.rs` pins `WORKER_RELEASE`; any change to worker code
  needs that pin bumped or the installer will deploy an older revision.
- The branch is a draft for exactly the reasons above.
- Portable Windows build from this revision (`85b73dd`):
  `desktop/src-tauri/target/release/cloud-voice-studio.exe`, 11,761,664 bytes,
  SHA-256 `53760389176E508A899C64D81CBCB22DF57845044603AD9F4F0FCE8A86A845FB`.
  It launches, and the size and hash are stable across repeated reads.
  The size and hash recorded in the 2026-09-24 handoff
  (11,157,504 / `A069C923…`) do not match that file, so treat the values above as
  the reference for this revision.

  Caution for anyone re-hashing this artifact: an earlier reading in this session
  returned a shorter file with the same modification time, because the build was
  still writing when it was read. Confirm the size is unchanged across two reads
  several seconds apart before recording a hash.
