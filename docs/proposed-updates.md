# Two proposed updates

Prepared for handoff. Both are scoped to the existing architecture and cite
measurements taken on real hardware during development.

---

# Update 1 — Warm start and per-engine scheduling

## Goal

Remove the two things that make every feature feel slow: engines that are cold
after a restart, and a single job queue that lets one heavy job block unrelated
work.

## Evidence

Measured on the V100 worker:

| Operation | Cold | Warm |
| --- | --- | --- |
| Seed-VC offline conversion | about 80 s before the first block | 11 s for a 12.5 s clip |
| Seed-VC realtime, first `Go live` | about 60 s inside the offer | a few seconds |
| Kokoro TTS | 24.8 s | 8.1 s |

All three engines load their models lazily on first use and nothing warms them
after a deploy or a crash-restart. `scripts/bootstrap.sh` restarts every
container on each deploy, so this cost is paid repeatedly, not once.

Separately, `worker/api/app/jobs.py` runs a single `ThreadPoolExecutor(max_workers=1)`.
A five-minute RVC training job therefore blocks a TTS job, even though TTS runs
on CPU in a different container and needs no GPU at all.

## Proposed change

1. **Warm on boot.** Each engine service starts a background task at startup that
   loads its models and flips a `ready` flag. Expose it on each engine's
   `/health` and surface it through `GET /v1/system`.
   - `worker/seed/service.py`: reuse `ensure_engine()` in a startup task.
   - `worker/realtime/service.py`: reuse `Runtime.ensure()` with the most recently
     used voice, or skip when no voice exists.
   - `worker/tts/service.py`: build the `KPipeline` at startup.
2. **Keep warm.** The API already exposes
   `POST /v1/engines/seed-vc/warmup`; call it automatically after the control
   plane becomes healthy, and add the equivalent for RVC and TTS.
3. **Per-engine lanes.** Replace the single executor with one lane per resource:
   - `gpu` lane, workers = 1 — Seed-VC offline, Seed-VC realtime, RVC convert,
     RVC train. These genuinely share the GPU and must stay serialised.
   - `cpu` lane, workers = 2 — TTS, transcoding, backup packaging.
   - `io` lane, workers = 1 — restore operations.
   Each job declares its lane when created.
4. **Report state honestly.** While an engine is warming, `GET /v1/system`
   returns `status: "warming"` and the desktop shows it, rather than letting the
   first request appear to hang for a minute.

## Acceptance criteria

- After a fresh deploy, the first offline conversion completes in under 20 s of
  wall clock (warm engine), not 90 s.
- A TTS job started during RVC training succeeds without waiting for it.
- Two GPU jobs still never run concurrently (verify by starting two conversions
  and confirming the second waits).
- `/v1/system` reports `ready` / `warming` / `unavailable` per engine, and the
  desktop reflects it.

## Out of scope

Reducing the realtime pipeline's inherent `2 × block_time` latency; that is
algorithmic and belongs to a different change.

---

# Update 2 — GPU capability detection and CUDA variant selection

## Goal

Run on any current NVIDIA GPU, including Blackwell and anything newer than the
pinned CUDA build, and fail early and clearly when the driver is too old.

## Evidence

Every engine is pinned to a single CUDA generation:

| Engine | Base | Torch |
| --- | --- | --- |
| `worker/seed` | `pytorch/pytorch:2.4.0-cuda12.1-cudnn9-runtime` | 2.4.0 + cu121 |
| `worker/realtime` | same image digest | 2.4.0 + cu121 |
| `worker/rvc` | `python:3.12-slim-bookworm` | 2.7.1 + cu118 |
| `worker/tts` | `python:3.11-slim-bookworm` | 2.4.0 CPU |

PyTorch's cu121 builds ship kernels up to `sm_90` (Hopper). They contain no
`sm_100` / `sm_120` kernels, so an RTX 50-series or B200 worker will fail at
model load. The RVC engine is worse: CUDA 11.8 predates Blackwell entirely.

This worked on a V100 (`sm_70`) and an RTX 4090 (`sm_89`) and will work on A100,
H100, L4, T4, A40 and P100. It will **not** work on Blackwell.

## Proposed change

1. **Detect before building.** In `scripts/bootstrap.sh`, after the existing
   `nvidia-smi` check:
   - read compute capability (`nvidia-smi --query-gpu=compute_cap` where
     available, otherwise probe with a tiny CUDA container),
   - read the driver's maximum CUDA version from `nvidia-smi`,
   - pick a variant: `cu121` for `sm_70`–`sm_90`, `cu128` (or newer) for
     `sm_100`+.
   - Fail fast with a specific message when the driver is below the minimum for
     the chosen variant (CUDA 12.1 needs driver 525.60+, CUDA 12.8 needs 570+),
     naming the required version rather than failing later inside a container.
2. **Parameterise the images.** Add `ARG TORCH_VERSION` and `ARG TORCH_INDEX_URL`
   to `worker/seed/Dockerfile`, `worker/realtime/Dockerfile` and
   `worker/rvc/Dockerfile`; pass them from `compose.yaml` using values written
   into `.env` by the bootstrap. Keep the current values as defaults so existing
   hosts behave identically.
3. **Keep both base images warm.** Two PyTorch bases are ~7 GB each. Pull only
   the variant in use, and record which one in `.env` so redeploys do not
   re-resolve it.
4. **Report it.** Include `compute_capability`, `cuda_variant` and the driver
   version in `GET /v1/system` and in the desktop's Server panel.

## Acceptance criteria

- On an RTX 4090 or V100 worker: byte-identical behaviour to today (same base
  digest, same torch versions).
- On a Blackwell worker (`sm_120`): installation selects `cu128`, and both a
  Seed-VC offline conversion and an RVC conversion succeed.
- On a host with a driver too old for the detected GPU: bootstrap exits before
  building anything, naming the minimum driver version.
- `GET /v1/system` reports the detected capability and variant.

## Notes for the implementer

- Prefer failing at detection over failing inside the container; the current
  failure mode is a CUDA "no kernel image is available" error deep in a job.
- `pytorch/pytorch` publishes both `-cuda12.1-cudnn9-runtime` and
  `-cuda12.8-cudnn9-runtime` tags; pin by digest as the existing Dockerfiles do.
- Anything that changes the engine images must also bump `WORKER_RELEASE` in
  `desktop/src-tauri/src/lib.rs`, which pins the revision the installer checks
  out.

---

## Why these two

Update 1 is the only change that speeds up *every* feature at once, because the
cold-start and queueing costs are paid by all of them.

Update 2 is the only one that removes a hard ceiling: today the software simply
cannot run on the newest GPUs, and that will get worse as providers rotate
inventory. Both are contained, and neither requires re-architecting the
desktop-to-worker design.
