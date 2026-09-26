# Open issue — tunnelled realtime closes with `1011 keepalive ping timeout`

Reported 2026-09-25 on the RTX 3090 worker (`77.104.167.148:42407`).

**Status: instrumented locally; live classification pending.** The mechanism is
confirmed, but the trigger still needs one desktop run. The client now reports
socket state, audio-context state, microphone frames sent, converted messages
received and API-poll health. Do not patch the keepalive timeout first — see
"Do not do this" below.

## Symptom

The Realtime page shows, while on the tunnel transport (no relay configured on
this host):

> The tunnelled audio connection closed (1011). keepalive ping timeout

Stream health at the same moment: `Waiting for audio`, `0 blocks · queue 0`,
link `tunnel`. Other operations were reported as working well and faster.

## Evidence gathered

1. **The worker accepted several tunnel sessions and then lost them.** The
   realtime container log shows repeated
   `WebSocket /v1/stream?... [accepted]` followed by
   `[realtime] tunnel session <id>: model_rate=22050 block=...` for sessions
   `c9d494be…` (block 2646, low latency), `90c92ae4…` and `f49068ea…` (block
   5292, balanced), and one `4eba1454…` with block 8820 (quality). Multiple
   presets were tried.
2. **The server cleaned up correctly** — afterwards `/health` reported
   `state: idle`, `active_session: None`, so the release path in `finally` ran.
3. **A healthy local client is not closed.** A `websockets` client inside the
   realtime container connected to `/v1/stream` with a valid session and held
   the socket open, idle, for 75 seconds: `RESULT: SURVIVED 75s without being
   closed`. Therefore the keepalive implementation is sound and there is no
   incompatibility between uvicorn 0.34.2 and websockets 17.1.
4. **The server received almost no audio.** The UI reported `0 blocks`. At the
   balanced preset the server needs 11,520 samples before it can produce one
   block. So the client sent fewer than that — the media path was not
   functioning, independent of the keepalive.
5. **Keepalive settings are defaults.** `docker inspect` shows the container
   command is `uvicorn service:app --host 127.0.0.1 --port 8791` with no
   `--ws-ping-*` flags, so uvicorn's defaults apply: ping every 20 s, and close
   with 1011 if no pong arrives within 20 s.

## Established

- The close came from the **server's keepalive**, not from the tunnel process,
  the SSH connection or the browser.
- A responsive client is never closed, so the 20 s window is not too short for a
  healthy peer.
- The failure is therefore either a **stalled data path** or a **client that
  stopped sending**, and in both cases the pong did not arrive for 20 s.
- The tunnel transport itself is not inherently broken: a previous Python client
  streamed 14.4 s of audio through it with speech intact on an earlier host.
  What has never been verified end to end is the **desktop's** tunnel
  implementation, which is the newer code.

## Candidate causes, ranked, each with its distinguishing test

### A. The tunnel stalled for more than 20 s (network blip)

The same SSH connection carries the API polling and the audio socket as separate
channels. A stall would freeze the audio channel while the UI continued to show
the last known state, because polling tolerates failures for three attempts
before declaring the worker offline.

**Test:** log tunnel liveness continuously. If `/v1/system` also failed around
the same time, the tunnel stalled and the fix is client-side reconnect with
surfaced state, not a keepalive change.

### B. The client never sent audio (audio graph not running)

If Chromium leaves the `AudioContext` suspended, `input.onaudioprocess` never
fires, the client sends nothing, the server produces no blocks — matching
`0 blocks` — while the socket stays open.

**Test:** count frames sent per second in the client and log the AudioContext
state. If the counter stays at zero, this is the cause.

## Code observations worth acting on separately

These are visible in `desktop/src/pages/RealtimePage.tsx` and are independent of
the two hypotheses:

1. **Double playback.** Lines 232-234 route the converted audio to *both* the
   `MediaStreamAudioDestinationNode` (which feeds the `<audio>` elements and
   their selected output device) *and* `context.destination` via `playhead`.
   Whichever device the `AudioContext` is using will also receive the audio, so
   with a virtual cable selected the user may hear doubled or echoed output.
2. **Link state is set before the socket opens.** `startTunnel` calls
   `setLinkState("tunnel")` right after constructing the WebSocket, not on
   `onopen`. The UI therefore reports the tunnel as the active link even if the
   socket never opens.
3. **`await context.resume()` is not verified.** The call is made, but its result
   is not checked and the context state is not reported, which is what makes
   hypothesis B invisible.

## Local correction implemented

`desktop/src/pages/RealtimePage.tsx` now:

- refuses to continue unless the `AudioContext` is actually `running`;
- reports socket and context state plus sent/received frame, byte and rate
  counters in a **Tunnel technical log** updated once per second;
- records successful and failed realtime-stats polls so an SSH/API stall can be
  distinguished from a stopped audio graph;
- reports the link as `connecting` until the WebSocket `open` event and fails
  clearly if it cannot open within ten seconds; and
- removes the direct `output -> context.destination` branch, leaving the
  selected virtual-cable and optional monitor `<audio>` elements as the only
  converted-audio outputs.

The frontend production build and the existing ten Python tests pass. The new
desktop behavior still needs a portable build and one live run before this issue
can be classified or closed.

## Do not do this

**Do not simply raise `--ws-ping-timeout`.** The keepalive is working as
designed, and the same conditions that made the pong late also stopped the audio
from flowing. A longer timeout would keep a broken session connected while the UI
still reports `0 blocks` — removing the only error message the user gets. Fix the
media path first; revisit the timeout only if evidence shows a healthy,
audio-carrying session being closed.

## Suggested order for the next agent

1. Build and run the updated portable desktop application.
2. Run one live tunnel session and read the **Tunnel technical log**.
3. Fix whichever cause the counters identify:
   - stalled tunnel → reconnect logic plus honest link state;
   - no frames sent → the audio graph (context state, processor wiring).
4. Re-test the two acceptance tests in `NEXT-AGENT-INSTRUCTIONS.md`, including
   stopping immediately after the last word.
