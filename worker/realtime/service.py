"""WebRTC transport for the Seed-VC realtime engine.

One GPU means one live session at a time, so a single runtime owns the model and
re-points it at whichever reference voice the active session asked for.

Sessions are minted by the control plane, which writes a short-lived ticket into
the shared data volume. This service serves signalling on loopback only (the
desktop reaches it through its SSH tunnel) while media travels over UDP straight
to the host's public address, which is why the container uses host networking.
"""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import hmac
import json
import os
import socket
import time
from contextlib import asynccontextmanager, suppress
from fractions import Fraction
from pathlib import Path

import av
import numpy as np
from aiortc import MediaStreamTrack, RTCConfiguration, RTCIceServer, RTCPeerConnection, RTCSessionDescription
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from engine import StreamingConverter, resample

os.chdir("/opt/seed-vc-realtime")

DATA_ROOT = Path(os.environ.get("CLOUD_VOICE_DATA_ROOT", "/data")).resolve()
TICKET_DIR = DATA_ROOT / "realtime-sessions"

OUTPUT_RATE = 48000
OUTPUT_FRAME_SAMPLES = 960  # 20 ms of Opus
MAX_QUEUED_FRAMES = 40

LAST_VOICE = DATA_ROOT / "realtime-last-voice.json"
STATUS_FILE = DATA_ROOT / "realtime-health.json"
_warm_state: dict[str, str | None] = {"status": "warming", "detail": None}


@asynccontextmanager
async def lifespan(_: FastAPI):
    warm_task = asyncio.create_task(_warm_on_boot())
    status_task = asyncio.create_task(_publish_status())
    yield
    warm_task.cancel()
    status_task.cancel()
    with suppress(asyncio.CancelledError):
        await warm_task
    with suppress(asyncio.CancelledError):
        await status_task
    STATUS_FILE.unlink(missing_ok=True)


app = FastAPI(title="Cloud Voice Studio Realtime", version="0.1.0", docs_url=None, redoc_url=None, lifespan=lifespan)


class GpuLease:
    def __init__(self) -> None:
        DATA_ROOT.mkdir(parents=True, exist_ok=True)
        # This container runs as root while the API runs as uid 10001, so the
        # file is forced world-writable. Without this, whichever container
        # created it first locked the other out with a plain 0644.
        path = DATA_ROOT / "gpu.lock"
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o666)
        os.close(descriptor)
        os.chmod(path, 0o666)
        self.handle = path.open("r+b")

    def acquire(self) -> bool:
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            self.close()
            return False

    def close(self) -> None:
        if not self.handle.closed:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()


async def ensure_runtime(ticket: dict) -> "StreamingConverter":
    _warm_state["status"] = "warming"
    try:
        converter = await runtime.ensure(ticket)
    except Exception as error:
        _warm_state["status"] = "unavailable"
        _warm_state["detail"] = str(error)[:500]
        raise
    _warm_state["status"] = "ready"
    _warm_state["detail"] = None
    LAST_VOICE.write_text(json.dumps({key: ticket.get(key) for key in (
        "reference_path", "preset", "diffusion_steps", "inference_cfg_rate"
    )}))
    return converter


async def _warm_on_boot() -> None:
    try:
        if not LAST_VOICE.is_file():
            _warm_state["status"] = "ready"
            return
        ticket = json.loads(LAST_VOICE.read_text())
        if not Path(ticket.get("reference_path", "")).is_file():
            _warm_state["status"] = "ready"
            return
        lease = GpuLease()
        try:
            await asyncio.to_thread(fcntl.flock, lease.handle.fileno(), fcntl.LOCK_EX)
            await ensure_runtime(ticket)
        finally:
            lease.close()
    except Exception as error:  # noqa: BLE001 - health reports the failure
        _warm_state["status"] = "unavailable"
        _warm_state["detail"] = str(error)[:500]
        print(f"[realtime] warmup failed: {error}", flush=True)


async def _publish_status() -> None:
    while True:
        try:
            DATA_ROOT.mkdir(parents=True, exist_ok=True)
            staging = STATUS_FILE.with_suffix(".tmp")
            staging.write_text(json.dumps({**health(), "updated_at": time.time()}))
            staging.replace(STATUS_FILE)
        except OSError as error:
            print(f"[realtime] status write failed: {error}", flush=True)
        await asyncio.sleep(2)


def frame_to_mono(frame: av.AudioFrame) -> np.ndarray:
    """Decode any PyAV audio frame into mono float32 in [-1, 1].

    PyAV's AudioFrame has no `reformat`, unlike VideoFrame, so the dtype and
    channel layout have to be handled here.
    """
    array = frame.to_ndarray()
    channels = frame.layout.nb_channels
    if frame.format.is_planar:
        data = array.mean(axis=0)
    else:
        data = array.reshape(-1, channels).mean(axis=1)
    name = frame.format.name
    if name.startswith("s16"):
        return data.astype(np.float32) / 32768.0
    if name.startswith("s32"):
        return data.astype(np.float32) / 2147483648.0
    if name == "u8":
        return (data.astype(np.float32) - 128.0) / 128.0
    return data.astype(np.float32)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def media_reachability() -> str:
    """Report whether WebRTC media can reach this host directly.

    Rented GPUs are often behind NAT with only SSH forwarded, in which case an
    inbound UDP connection is impossible and the tunnelled transport is the only
    one that can work. The address the kernel picks for an outbound route is the
    cheapest reliable way to tell a public host from a NATed one.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("8.8.8.8", 80))
        address = probe.getsockname()[0]
    except OSError:
        return "unknown"
    finally:
        probe.close()
    octets = address.split(".")
    if len(octets) != 4:
        return "unknown"
    first, second = int(octets[0]), int(octets[1])
    is_private = (
        first == 10
        or (first == 172 and 16 <= second <= 31)
        or (first == 192 and second == 168)
        or first == 127
    )
    return "tunnel" if is_private else "direct"


def read_ticket(session_id: str, token: str) -> dict:
    if not session_id.replace("-", "").isalnum():
        raise HTTPException(status_code=400, detail="Invalid session id")
    path = TICKET_DIR / f"{session_id}.json"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Unknown or expired session")
    try:
        ticket = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise HTTPException(status_code=500, detail=f"Unreadable session ticket: {error}") from error
    if not hmac.compare_digest(ticket.get("token_sha256", ""), token_hash(token)):
        raise HTTPException(status_code=401, detail="Invalid session token")
    if time.time() > float(ticket.get("expires_at", 0)):
        path.unlink(missing_ok=True)
        raise HTTPException(status_code=410, detail="Session expired")
    reference = Path(ticket.get("reference_path", ""))
    if not reference.is_file():
        raise HTTPException(status_code=409, detail="The reference voice is no longer on this worker")
    return ticket


class OfferRequest(BaseModel):
    session_id: str
    token: str
    sdp: str
    type: str


class Runtime:
    """Owns the single GPU pipeline and serialises reconfiguration."""

    def __init__(self) -> None:
        self.converter: StreamingConverter | None = None
        self.signature: tuple | None = None
        self.lock = asyncio.Lock()

    async def ensure(self, ticket: dict) -> StreamingConverter:
        signature = (
            ticket["reference_path"],
            ticket.get("preset", "balanced"),
            ticket.get("diffusion_steps"),
            float(ticket.get("inference_cfg_rate", 0.7)),
        )
        async with self.lock:
            if self.converter is not None and self.signature == signature:
                return self.converter
            if self.converter is None:
                self.converter = await asyncio.to_thread(
                    StreamingConverter,
                    ticket["reference_path"],
                    preset=ticket.get("preset", "balanced"),
                    diffusion_steps=ticket.get("diffusion_steps"),
                    inference_cfg_rate=float(ticket.get("inference_cfg_rate", 0.7)),
                )
            else:
                converter = self.converter
                await asyncio.to_thread(
                    converter.configure,
                    ticket["reference_path"],
                    preset=ticket.get("preset", "balanced"),
                    diffusion_steps=ticket.get("diffusion_steps"),
                    inference_cfg_rate=float(ticket.get("inference_cfg_rate", 0.7)),
                )
            self.signature = signature
            return self.converter

    def status(self) -> dict:
        if self.converter is None:
            return {"loaded": False}
        return {
            "loaded": True,
            "model_rate": self.converter.sample_rate,
            "block_samples": self.converter.block_frame,
            "block_seconds": round(self.converter.block_seconds, 4),
            "lookahead_seconds": self.converter.settings["extra_time_right"],
        }


runtime = Runtime()
active: "LiveSession | None" = None


class ConvertedTrack(MediaStreamTrack):
    """Converted audio, paced by the rate at which the pipeline produces it."""

    kind = "audio"

    def __init__(self) -> None:
        super().__init__()
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=MAX_QUEUED_FRAMES)
        self.timestamp = 0
        self.dropped = 0

    def push(self, samples: np.ndarray) -> None:
        for start in range(0, len(samples), OUTPUT_FRAME_SAMPLES):
            chunk = samples[start : start + OUTPUT_FRAME_SAMPLES]
            if len(chunk) < OUTPUT_FRAME_SAMPLES:
                chunk = np.pad(chunk, (0, OUTPUT_FRAME_SAMPLES - len(chunk)))
            # aiortc's Opus encoder asserts on s16: sending fltp frames raises
            # inside the encoder thread and silently ends the send loop.
            frame = av.AudioFrame(format="s16", layout="mono", samples=len(chunk))
            frame.sample_rate = OUTPUT_RATE
            pcm = np.clip(chunk, -1.0, 1.0)
            frame.planes[0].update((pcm * 32767.0).astype(np.int16).tobytes())
            frame.pts = self.timestamp
            frame.time_base = Fraction(1, OUTPUT_RATE)
            self.timestamp += len(chunk)
            if self.queue.full():
                try:
                    self.queue.get_nowait()
                    self.dropped += 1
                except asyncio.QueueEmpty:
                    pass
            self.queue.put_nowait(frame)

    async def recv(self) -> av.AudioFrame:
        return await self.queue.get()


class LiveSession:
    def __init__(self, session_id: str, ticket: dict, lease: GpuLease) -> None:
        self.id = session_id
        self.ticket = ticket
        self.lease = lease
        self.pc: RTCPeerConnection | None = None
        self.track = ConvertedTrack()
        self.task: asyncio.Task | None = None
        self.inference_ms: list[float] = []
        self.converter = None
        self.state = "created"
        self.started_at = time.time()
        self.blocks = 0

    async def run(self, incoming: MediaStreamTrack) -> None:
        converter = await runtime.ensure(self.ticket)
        self.converter = converter
        model_rate = converter.sample_rate
        block = converter.block_frame
        needed_input = round(block * OUTPUT_RATE / model_rate)

        self.state = "streaming"
        print(f"[realtime] streaming started: model_rate={model_rate} block={block}", flush=True)
        buffer = np.zeros(0, dtype=np.float32)
        inbound = 0

        while True:
            frame = await incoming.recv()
            inbound += 1
            if inbound <= 2 or inbound % 100 == 0:
                print(
                    f"[realtime] inbound frames={inbound} buffer={len(buffer)} samples={frame.samples}",
                    flush=True,
                )
            try:
                if inbound == 1:
                    print(
                        f"[realtime] inbound format={frame.format.name} rate={frame.sample_rate}"
                        f" layout={frame.layout.name}",
                        flush=True,
                    )
                buffer = np.concatenate((buffer, frame_to_mono(frame)))
            except Exception as error:  # noqa: BLE001
                print(f"[realtime] frame decode failed: {error!r}", flush=True)
                continue

            while len(buffer) >= needed_input:
                chunk = buffer[:needed_input]
                buffer = buffer[needed_input:]
                resampled = resample(chunk, OUTPUT_RATE, model_rate)
                if len(resampled) < block:
                    resampled = np.pad(resampled, (0, block - len(resampled)))
                elif len(resampled) > block:
                    resampled = resampled[:block]
                began = time.perf_counter()
                converted = await asyncio.to_thread(converter.process, resampled)
                self.inference_ms.append((time.perf_counter() - began) * 1000)
                del self.inference_ms[:-200]
                self.blocks += 1
                if self.blocks <= 3 or self.blocks % 25 == 0:
                    print(
                        f"[realtime] blocks={self.blocks} infer_ms={self.inference_ms[-1]:.0f}"
                        f" queued={self.track.queue.qsize()}",
                        flush=True,
                    )
                self.track.push(resample(converted, model_rate, OUTPUT_RATE))

    async def convert_chunk(self, chunk: np.ndarray) -> np.ndarray:
        """Convert one chunk of 48 kHz mono audio for the tunnelled transport.

        The WebRTC path accumulates frames from a track; this takes whole chunks
        straight from a socket. Both share the same converter, so the silence gate
        and preset geometry apply identically.
        """
        converter = self.converter
        model_rate = converter.sample_rate
        block = converter.block_frame
        resampled = resample(chunk, OUTPUT_RATE, model_rate)
        if len(resampled) < block:
            resampled = np.pad(resampled, (0, block - len(resampled)))
        elif len(resampled) > block:
            resampled = resampled[:block]
        began = time.perf_counter()
        converted = await asyncio.to_thread(converter.process, resampled)
        self.inference_ms.append((time.perf_counter() - began) * 1000)
        del self.inference_ms[:-200]
        self.blocks += 1
        return resample(converted, model_rate, OUTPUT_RATE)


@app.get("/health")
def health() -> dict:
    return {
        "status": _warm_state["status"],
        "detail": _warm_state["detail"],
        "inbound_media": media_reachability(),
        "runtime": runtime.status(),
        "active_session": active.id if active else None,
        "state": active.state if active else "idle",
        "capabilities": ["realtime-seed-vc"],
    }


@app.post("/v1/offer")
async def offer(request: OfferRequest) -> dict:
    ticket = read_ticket(request.session_id, request.token)
    global active
    if active is not None:
        if active.pc is not None:
            await active.pc.close()
        active.lease.close()
        active = None
    lease = GpuLease()
    if not lease.acquire():
        raise HTTPException(status_code=409, detail="GPU is busy with another job or realtime session")
    try:
        converter = await ensure_runtime(ticket)
    except Exception:
        lease.close()
        raise

    pc = RTCPeerConnection(
        RTCConfiguration(iceServers=[RTCIceServer(urls=["stun:stun.l.google.com:19302"])])
    )
    session = LiveSession(request.session_id, ticket, lease)
    session.pc = pc

    active = session

    @pc.on("track")
    def on_track(track: MediaStreamTrack) -> None:
        print(f"[realtime] incoming track: kind={track.kind}", flush=True)
        if track.kind == "audio":
            if session.task is not None:
                session.task.cancel()
            session.task = asyncio.ensure_future(session.run(track))

    @pc.on("connectionstatechange")
    async def on_state() -> None:
        print(f"[realtime] state={pc.connectionState} ice={pc.iceConnectionState}", flush=True)
        if pc.connectionState in {"failed", "closed", "disconnected"}:
            session.state = pc.connectionState
            if session.task is not None:
                session.task.cancel()
            session.lease.close()

    # Consume the offer first, then attach our outgoing audio to the transceiver
    # the offer created. Adding the track before setRemoteDescription appends a
    # second m-line that the client never asked for, and the answer is rejected.
    try:
        await pc.setRemoteDescription(RTCSessionDescription(sdp=request.sdp, type=request.type))
        audio_transceivers = [t for t in pc.getTransceivers() if t.kind == "audio"]
        if not audio_transceivers:
            raise HTTPException(status_code=400, detail="The client offered no audio track")
        for transceiver in audio_transceivers:
            if transceiver.sender.track is None:
                pc.addTrack(session.track)
        print(
            "[realtime] transceivers: "
            + ", ".join(f"{t.kind}/{t.direction}/track={t.sender.track is not None}" for t in pc.getTransceivers()),
            flush=True,
        )

        answer = await pc.createAnswer()
        await pc.setLocalDescription(answer)

        deadline = time.time() + 8
        while pc.iceGatheringState != "complete" and time.time() < deadline:
            await asyncio.sleep(0.1)

        return {
            "sdp": pc.localDescription.sdp,
            "type": pc.localDescription.type,
            "model_rate": converter.sample_rate,
            "block_seconds": round(converter.block_seconds, 4),
            "lookahead_seconds": converter.settings["extra_time_right"],
            "output_sample_rate": OUTPUT_RATE,
        }
    except Exception:
        await pc.close()
        lease.close()
        active = None
        raise


@app.post("/v1/sessions/{session_id}/close")
async def close(session_id: str) -> dict:
    global active
    if active is not None and active.id == session_id:
        if active.task is not None:
            active.task.cancel()
        if active.pc is not None:
            await active.pc.close()
        active.lease.close()
        active = None
    return {"closed": session_id}


@app.websocket("/v1/stream")
async def stream(websocket: WebSocket) -> None:
    """Tunnelled audio transport: 48 kHz mono int16 in, converted int16 out.

    WebRTC needs inbound UDP, which a rented GPU behind NAT cannot provide. This
    runs over the desktop's existing SSH tunnel instead, so realtime works
    wherever SSH works. The client paces itself in real time, and each block it
    sends produces exactly one converted block back.
    """
    session_id = websocket.query_params.get("session_id", "")
    token = websocket.query_params.get("token", "")
    await websocket.accept()
    try:
        ticket = read_ticket(session_id, token)
    except HTTPException as error:
        await websocket.close(code=4401, reason=str(error.detail))
        return

    lease = GpuLease()
    if not lease.acquire():
        await websocket.close(code=4409, reason="Worker is busy with another job")
        return

    global active
    session = LiveSession(session_id, ticket, lease)
    session.state = "connecting"
    if active is not None and active.pc is not None:
        await active.pc.close()
    active = session

    try:
        converter = await runtime.ensure(ticket)
        session.converter = converter
        needed = round(converter.block_frame * OUTPUT_RATE / converter.sample_rate)
        session.state = "streaming"
        print(
            f"[realtime] tunnel session {session_id}: model_rate={converter.sample_rate}"
            f" block={converter.block_frame} needed={needed}",
            flush=True,
        )
        buffer = np.zeros(0, dtype=np.float32)
        while True:
            payload = await websocket.receive_bytes()
            samples = np.frombuffer(payload, dtype="<i2").astype(np.float32) / 32768.0
            buffer = np.concatenate((buffer, samples))
            while len(buffer) >= needed:
                chunk = buffer[:needed]
                buffer = buffer[needed:]
                converted = await session.convert_chunk(chunk)
                clipped = np.clip(converted, -1.0, 1.0)
                await websocket.send_bytes((clipped * 32767.0).astype("<i2").tobytes())
    except WebSocketDisconnect:
        pass
    except Exception as error:  # noqa: BLE001 - report, never kill the service
        print(f"[realtime] tunnel session failed: {error!r}", flush=True)
    finally:
        session.state = "closed"
        lease.close()
        if active is session:
            active = None


@app.get("/v1/sessions/{session_id}/stats")
def stats(session_id: str) -> dict:
    if active is None or active.id != session_id:
        return {"state": "idle", "blocks": 0}
    samples = active.inference_ms[-40:]
    return {
        "state": active.state,
        "blocks": active.blocks,
        "inference_ms_mean": round(float(np.mean(samples)), 1) if samples else None,
        "inference_ms_p95": round(float(np.percentile(samples, 95)), 1) if samples else None,
        "queued_frames": active.track.queue.qsize(),
        "dropped_frames": active.track.dropped,
        "uptime_seconds": round(time.time() - active.started_at, 1),
        "connection_state": active.pc.connectionState if active.pc else None,
        # Lets the desktop show whether the worker is passing speech or holding
        # the gate closed on silence.
        "gate_open": active.converter.gate_open if active.converter else None,
    }
