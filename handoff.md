# Handoff — Cloud Voice Studio

Supersedes the 2026-09-24 handoff. Written at repo revision `f0940c3`.

**No fixes were applied while writing this.** Two live problems are documented
with evidence and hypotheses so the next agent can verify before changing code.

## Current state

| Item | Value |
| --- | --- |
| Repository | `https://github.com/michaelinbox02-star/cloud-voice`, branch `main` |
| Desktop artifact | `desktop/src-tauri/target/release/cloud-voice-studio.exe`, SHA-256 `AB7081966907E1A833F673134E2FC71F1CCFE0D18AC9DD9C48CABD71CB04B3AA` |
| Installer pin | `WORKER_RELEASE` in `desktop/src-tauri/src/lib.rs` = `3d0d5aa0a6e73e5a8d2adc72627ec54d695705f0` |
| Worker | `142.112.39.215` (Montréal, 4090, 49 GB RAM, 178 GB disk) |
| Worker SSH | port **41999** was open at the last check; port **41358** refused. The provider appears to remap ports, so confirm before assuming the box is gone. |
| Worker checkout | `~/cloud-voice`, detached at the pinned revision |

Secrets are **not** in the repo:

- Worker secrets live in `~/cloud-voice/.env` on the host (mode 600), including
  the TURN relay credentials for the Metered account.
- The Vast.ai API key was stored at `%TEMP%\vast.key` on the desktop machine
  only. It is not in the repository and not in any config file.

## Issue 1 — operations are slower than before

Reported as: conversions, TTS and training take noticeably longer than they did
before the warm-start / lane update.

### Evidence gathered

- `worker/api/app/jobs.py:133` wraps **every GPU-lane job** in a blocking
  exclusive lock: `with gpu_lease() if job["lane"] == "gpu" else nullcontext():`
- `worker/realtime/service.py:477` and `:581` acquire the **same** `GpuLease`
  for the whole lifetime of a live session, releasing it only in `finally`
  (`:545`, `:620`).
- Consequence: **while a realtime session is live, every offline conversion and
  every training job blocks** until the stream ends. Before this update there was
  no cross-container lease, so offline work proceeded alongside realtime.
- The API log shows the desktop polling a job that does not exist:
  `GET /v1/jobs/job_762086a03bdf4b9b` → **404**, repeatedly, many times a minute.

### Hypotheses (verify before changing anything)

1. **GPU lease contention is the primary cause.** A live realtime session holds
   the lease, so offline jobs wait. Confirm by starting a conversion while a
   realtime session is live and observing whether it sits `queued` until the
   stream stops. If so, the fix is to scope the lease to model access rather than
   the whole job, or to give realtime a shared/priority lease.
2. **Stale job polling.** `desktop/src/jobStore.ts` persists jobs to
   `localStorage` and polls anything still `queued`/`running` every 2.5 s
   (`POLL_MS`), swallowing errors so a job that no longer exists is retried
   forever. After moving to a new worker, stale IDs from the previous worker are
   polled indefinitely — visible as the 404 storm above — and the UI can show a
   job stuck as running when it is not.
3. Secondary: `desktop/src/pages/RealtimePage.tsx` polls session stats every
   1.5 s during a live session, and `GET /v1/system` now probes all three engines
   per call (the desktop polls it every 30 s).

### Not yet checked

- Real per-job timings from the database on a healthy worker (`db.list_jobs()`),
  comparing lane wait time against engine time.
- Whether the RVC/TTS images were rebuilt recently, which would make their first
  job slow for unrelated reasons.
- Whether `worker/rvc` re-downloads assets on each run (commit `3895df2` made RVC
  wait for assets before inference and training).

## Issue 2 — realtime: "No audio path was established within 25 seconds"

The screenshot shows the Realtime page live, the transport dropdown set to
**WebRTC — direct, lowest latency**, the relay note displayed, and:

> No audio path was established within 25 seconds. The worker is reachable for
> control but the media connection did not come up.

Stream health read `Waiting for audio`, `0 blocks · queue 0`, with the link
indicator connecting or failed. That message is the diagnostic added in this
update. The session negotiated — the 120 ms block size came back in the answer —
but no media flowed.

### Evidence gathered

- The worker's realtime log around the failure contains no `[realtime] state=` or
  `[realtime] streaming started` lines, only repeated
  `GET /v1/sessions/{id}/stats` polls. No media path was established.
- The relay itself is **known good**: with the Metered credentials the worker
  allocates `typ relay` candidates on UDP 80, UDP 443 and TCP 80, verified
  directly on the worker.
- So the failure is in ICE negotiation between the desktop and the worker, not in
  relay availability.

### Hypotheses (verify before changing anything)

1. **The desktop sends its offer before ICE gathering finishes, and nothing is
   trickled.** `desktop/src/pages/RealtimePage.tsx:418-425` does
   `createOffer()` → `setLocalDescription()` → immediately reads
   `pc.localDescription.sdp` and sends it. There is no `onicecandidate` handler
   forwarding candidates to the worker. The worker therefore receives an offer
   with no remote candidates and cannot start connectivity checks.
   **Check:** log the number of `candidate:` lines in the offer the worker
   receives. If it is zero, this is the cause.
2. **The worker's answer may lack the relay candidate.** `offer()` in
   `worker/realtime/service.py` waits for ICE gathering with an approximately
   8-second deadline. TURN allocation plus gathering can exceed that on a loaded
   host; if gathering is truncated the answer carries only host/srflx candidates,
   which a desktop cannot use to reach a NAT'd worker.
   **Check:** log candidate types in the answer SDP and confirm `typ relay` is
   present before returning it.
3. **Race on ICE config.** `iceServers` is populated by a `useEffect` calling
   `realtimeHealth()`. If the user presses *Go live* before it resolves, the peer
   connection is built with STUN only. The relay note rendered in the screenshot,
   so config had arrived — but the race is real and cheap to close.

### What is already verified working

- The relay credentials are valid and the worker allocates relays on them.
- The worker publishes its ICE configuration through `/health` (`ice_servers`)
  and reports `inbound_media: tunnel` for this host, correctly detecting NAT.
- The tunnelled transport was verified earlier on a different host: 14.4 s
  round-trip with speech intact, and the silence gate produces exact zeros during
  silence.

## Updates made in this period

1. **Silence gate** (`worker/realtime/engine.py`): the upstream VAD gate had been
   removed entirely, so the model vocalised babble and repeated syllables during
   silence. Replaced with a gate whose decision is delayed by `extra_time_right`
   to match the pipeline's own latency, with a 300 ms hangover and a 20 ms fade.
   Covered by ten unit tests (`tests/test_silence_gate.py`) and verified on GPU
   (`silence region rms 0.000000`).
2. **Shared GPU lock permissions** (`worker/api/app/jobs.py`,
   `worker/realtime/service.py`): `/data/gpu.lock` was created 0644 by whichever
   container started first, so the API (uid 10001) hit `Permission denied`. Both
   sides now force 0666. Verified: a TTS job succeeded afterwards.
3. **Tunnelled realtime transport** (`worker/realtime/service.py`,
   `desktop/src/pages/RealtimePage.tsx`): WebSocket audio over the existing SSH
   tunnel for hosts with no inbound UDP. Verified end to end on a NAT'd host.
4. **TURN support**: worker and desktop share the ICE configuration published by
   the worker; transport selection prefers WebRTC when a relay exists and falls
   back to the tunnel otherwise. Defaults no longer point at the dead anonymous
   relay.
5. **Diagnostics**: transport selector, NAT note, link state, gate open/closed
   indicator, and the 25-second no-audio timeout that produced the screenshot.
6. **Job store** (`desktop/src/jobStore.ts`): jobs moved out of page state so they
   survive tab switches. This is also the source of the stale-job polling in
   Issue 1.
7. **Branded virtual microphone** (`desktop/src-tauri/src/virtualmic.rs`): renames
   the VB-CABLE capture endpoint to "Cloud Voice Microphone" through an elevated
   script, with restore. Verified against the real registry (dry run only;
   nothing was written).

## Verified vs unverified

**Verified:** silence gate on GPU; relay allocation with the Metered credentials;
lock permission fix; tunnel transport on a NAT'd host; branded-mic detection and
dry run on the real machine; provider NAT and UDP behaviour on three hosts.

**Not verified:** a complete live desktop → worker → desktop audio session on any
host; the acceptance criteria for warm start (first conversion under 20 s after a
fresh deploy), TTS running during training, and strict GPU serialisation;
Blackwell (`cu128`) on real hardware — only the selection logic was tested,
against stubbed hardware.

## Recommended next steps, in order

1. **Do not press "Install on GPU" with an older desktop build.** The installer
   pin decides which worker revision is deployed; an older build checks the host
   back to a revision without TURN support and silently rebuilds the realtime
   container without it. Any change to worker code needs `WORKER_RELEASE` in
   `desktop/src-tauri/src/lib.rs` bumped in the same commit.
2. Reproduce Issue 2 with candidate logging on both sides (hypotheses 1 and 2)
   before changing the ICE flow. The most likely single fix is to finish
   gathering on the desktop before sending the offer, or to trickle candidates,
   with a longer gathering deadline on the worker as the companion fix.
3. Reproduce Issue 1 by timing a conversion with and without a live realtime
   session, then decide whether the lease should cover the whole job or only
   model access.
4. Bound `jobStore.ts` so a job that 404s is dropped rather than polled forever,
   and clear persisted jobs when the connected host changes.
5. Only then re-run the outstanding acceptance criteria from
   `docs/proposed-updates.md` on a healthy worker.

## Access notes

- The desktop SSH key is `C:\Users\USER\.ssh\ai-avatar-gpu`; its public key is
  registered on the Vast.ai account, so Vast instances receive it automatically.
  Non-Vast providers may not, in which case key auth fails and the key must be
  added on their side.
- Three recent instances (`87.17.211.111`, `85.218.235.6`, `142.112.39.215`) are
  all behind NAT with a single forwarded port. `ip -4 addr show scope global`
  showing only `10.x`/`172.16-31.x`/`192.168.x` predicts this before installing.
- A provider that assigns a real public IP to the instance interface removes the
  need for both the tunnel and the relay — worth prioritising when renting.
