# Cloud Voice Studio — next agent instructions

This is the only current handoff. It replaces `handoff.md` and `HANDOFF-2026-09-24.md`, which named expired GPU hosts and mixed verified facts with speculative work.

## User objective

Stabilize the existing product before adding features. Preserve working voice conversion, make job state safe when the user changes GPU workers, and make realtime transport predictable. Do not expand the branded microphone, TURN, tunnel, or scheduling work until the relevant failure is reproduced on the current host.

## Current repository and worker

- Local branch: `main`, three commits ahead of `origin/main` after this documentation update. The foundational worker/code commit is `4edfcc0e78e6319da30b919fa77f8cbc734f7da7`; `ac624f2` pins the installer to it. These commits are local and have not been pushed.
- Repository: `https://github.com/michaelinbox02-star/cloud-voice`.
- Current GPU access: `ssh -p 42407 -i C:\Users\USER\.ssh\ai-avatar-gpu root@77.104.167.148`.
- The command supplied by the user included `-L 8080:localhost:8080`; Cloud Voice does not use port 8080. The desktop opens its own API and realtime forwards. Do not redesign ports around 8080.
- Hardware verified read-only: one NVIDIA GeForce RTX 3090, 24,576 MiB VRAM, compute capability 8.6, driver 580.105.08, about 118 GiB disk free.
- Network verified read-only: the instance has private address `10.0.2.15`; the realtime health endpoint reports `inbound_media=tunnel`.
- Worker state verified read-only: checkout `/root/cloud-voice` at `3d0d5aa`; API, Seed-VC, realtime, RVC, and TTS containers are running; API is healthy; realtime is idle and ready.
- No TURN server is configured on this worker. Automatic transport must therefore select the SSH tunnel. Direct WebRTC is expected to fail on this NAT host and is not an acceptance test here.
- The SSH command without an identity failed with `Permission denied (publickey)`. Always include the project key above.

Do not run `scripts/bootstrap.sh`, recreate all containers, or press **Install on GPU** merely to inspect the host. Those actions interrupt services and can move the checkout. Deploy only after the local changes are committed, the installer pin is correct, and the user is ready for a short interruption.

## Foundational corrections already made in the current worktree

### 1. Job state is scoped to the connected worker

Files: `desktop/src/jobStore.ts`, `desktop/src/main.tsx`, and `desktop/src/pages/ServerPage.tsx`.

- The old global local-storage key allowed jobs created on one GPU to be polled forever after connecting to another GPU.
- Jobs now load from `cloud-voice-jobs:<username>@<host>:<port>` only after a connection succeeds.
- Disconnecting clears the active in-memory scope and stops polling without deleting the saved per-worker history.
- A confirmed HTTP 404 changes the missing job to terminal `failed` state with an explanatory error, so it cannot generate an endless request storm.
- Startup no longer polls unscoped legacy jobs. The old `cloud-voice-jobs` entry is intentionally ignored; it may be removed later as a one-time cleanup but must never be migrated to an arbitrary worker.

### 2. Non-trickle ICE waits for complete candidates

Files: `desktop/src/pages/RealtimePage.tsx` and `worker/realtime/service.py`.

- This application sends one SDP offer and one SDP answer; it has no trickle-candidate channel.
- The desktop now waits up to 15 seconds for `iceGatheringState === "complete"` after `setLocalDescription()` and before sending its offer.
- The worker answer timeout is increased from 8 to 15 seconds. It returns HTTP 504 instead of returning incomplete SDP if candidate gathering never completes.
- A duplicated and contradictory realtime comment was removed.
- These changes repair WebRTC for a future directly reachable or TURN-configured worker. They do not change the current RTX 3090 host's correct Automatic choice, which is the SSH tunnel.

### 3. Obsolete documentation removed

The two old handoff files were removed because their host addresses, revisions, artifact hashes, and deployment claims no longer describe the current system. Useful findings are preserved in this file.

## Validation already completed

- `python -m unittest discover -s tests -v`: 10/10 tests passed, covering job lanes and the realtime silence gate.
- `python -m py_compile worker/realtime/service.py`: passed.
- `npm run build` from `desktop`: TypeScript and Vite production build passed. The first sandboxed attempt failed only because Node could not traverse `C:\Users\USER`; rerunning outside the sandbox passed.
- `git diff --check`: passed.
- No deployment, container rebuild, portable executable rebuild, or remote push has been performed for these corrections. The code correction is committed locally, and `WORKER_RELEASE` is pinned to its full SHA. Automatic approval review rejected a direct push to default branch `main`; obtain explicit user approval before publishing there, or use a separate branch/PR if the user directs that workflow.

## Required next steps, in order

### 1. Publish only with explicit direction

The GPU cannot fetch `4edfcc0` until these local commits exist on GitHub. Ask the user to choose and explicitly authorize either a push to `main` or publication on a separate branch/PR. Do not deploy before the chosen remote contains `4edfcc0`.

### 2. Rebuild the portable desktop application

From `desktop` run:

```powershell
npm run app:portable
```

The artifact must be `desktop/src-tauri/target/release/cloud-voice-studio.exe`. Record its size, modification time, and SHA-256 in this file or a release note. Do not claim the executable is updated based only on `npm run build`; that command builds the web frontend, not the Tauri `.exe`.

### 3. Deploy only the changed worker service

The current correction changes only `worker/realtime` on the GPU. Use the current SSH target and the pinned worker commit:

```bash
cd /root/cloud-voice
git fetch origin main
git checkout --detach 4edfcc0e78e6319da30b919fa77f8cbc734f7da7
docker compose --env-file .env -f worker/compose.yaml build realtime
docker compose --env-file .env -f worker/compose.yaml up -d --no-deps --no-build --force-recreate realtime
docker compose --env-file .env -f worker/compose.yaml ps realtime
```

This briefly ends an active realtime session. Check that no session is active before restarting. Do not rebuild API, Seed-VC, RVC, or TTS for this correction.

### 4. Perform the two acceptance tests

**Job isolation:** connect to the RTX 3090 using the rebuilt desktop, start one short job, and verify the browser storage key includes `root@77.104.167.148:42407`. A fabricated or previously saved missing job may return 404 once, but must become `failed` and stop polling. Disconnecting must stop polling; reconnecting to the same host may restore its actual active jobs.

**Realtime on this host:** leave Transport on **Automatic**. Confirm the UI selects/reports the tunnel path, speak one word and one sentence, and verify the worker's block count increases and converted audio returns. Stop immediately after the last word and listen for a complete ending. Do not use **WebRTC — direct** as the test on this NAT host because it has no TURN relay.

## Work after the foundations pass

### Measure scheduling before changing the GPU lease

The shared `/data/gpu.lock` is deliberate protection for a single 24 GB GPU. Realtime currently holds it for the lifetime of a session, so offline GPU conversion and RVC training queue until realtime ends. Removing or weakening the lock without memory measurements can cause concurrent model loads, out-of-memory errors, and corrupted user experience.

Measure these cases and record created/start/finish times plus VRAM:

1. Seed-VC conversion with no realtime session.
2. The same conversion while realtime is active; it should remain queued, not fail.
3. TTS while realtime is active; because TTS is on the CPU lane, it should complete without waiting for the GPU lease.
4. Two offline GPU jobs; the second should wait for the first.

If the complaint is only that queued work looks frozen, add explicit API/UI state such as `waiting_for_gpu` and show which operation owns the GPU. Keep mutual exclusion. Consider concurrent GPU work only after measuring peak combined VRAM below a conservative limit and adding a deterministic admission policy; do not simply shorten the file-lock scope around individual inference calls because models remain resident between calls.

### Validate warm start on the current hardware

After a deliberate full deployment, wait for `/v1/system` to report Seed-VC ready, then time the first offline conversion. The target from `docs/proposed-updates.md` is under 20 seconds. Separate queue wait from inference time. Do not call the criterion passed based on unit tests or a warm second request.

### Leave optional work dormant

- TURN support is useful only when valid relay credentials are configured. The current worker has none, and the tunnel is the supported path. Do not add public/default TURN credentials.
- The branded virtual microphone is optional polish. Do not modify Windows registry behavior or expand it until core audio conversion and transport pass end to end.
- Blackwell/CUDA 12.8 cannot be accepted on this RTX 3090. Keep capability-selection tests, but state that real Blackwell verification remains outstanding.
- Do not add RVC realtime, new transports, UI redesigns, or installer features during this stabilization pass.

## Important historical facts that remain valid

- The delayed silence gate fix and short-utterance work have unit/GPU evidence and should not be reverted casually.
- The RVC voice mismatch fix makes the stored voice engine authoritative and should remain.
- The worker-scoped job fix addresses a demonstrated 404 polling storm after GPU changes.
- TURN allocation was verified on an older host, but those credentials are not present on the current host and must not be copied into source control.
