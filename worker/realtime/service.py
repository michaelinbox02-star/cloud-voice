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
import hashlib
import hmac
import json
import os
import time
from fractions import Fraction
from pathlib import Path

import av
import numpy as np
from aiortc import MediaStreamTrack, RTCConfiguration, RTCIceServer, RTCPeerConnection, RTCSessionDescription
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from engine import StreamingConverter, resample

os.chdir("/opt/seed-vc-realtime")

DATA_ROOT = Path(os.environ.get("CLOUD_VOICE_DATA_ROOT", "/data")).resolve()
TICKET_DIR = DATA_ROOT / "realtime-sessions"

OUTPUT_RATE = 48000
OUTPUT_FRAME_SAMPLES = 960  # 20 ms of Opus
MAX_QUEUED_FRAMES = 40

app = FastAPI(title="Cloud Voice Studio Realtime", version="0.1.0", docs_url=None, redoc_url=None)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


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
            frame = av.AudioFrame(format="fltp", layout="mono", samples=len(chunk))
            frame.sample_rate = OUTPUT_RATE
            frame.planes[0].update(np.ascontiguousarray(chunk, dtype=np.float32).tobytes())
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
    def __init__(self, session_id: str, ticket: dict) -> None:
        self.id = session_id
        self.ticket = ticket
        self.pc: RTCPeerConnection | None = None
        self.track = ConvertedTrack()
        self.task: asyncio.Task | None = None
        self.inference_ms: list[float] = []
        self.state = "created"
        self.started_at = time.time()
        self.blocks = 0

    async def run(self, incoming: MediaStreamTrack) -> None:
        converter = await runtime.ensure(self.ticket)
        model_rate = converter.sample_rate
        block = converter.block_frame
        needed_input = round(block * OUTPUT_RATE / model_rate)

        self.state = "streaming"
        buffer = np.zeros(0, dtype=np.float32)

        while True:
            frame = await incoming.recv()
            frame = frame.reformat(format="fltp")
            data = frame.to_ndarray()
            data = data.mean(axis=0) if frame.layout.nb_channels > 1 else data.reshape(-1)
            buffer = np.concatenate((buffer, data.astype(np.float32)))

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
                self.track.push(resample(converted, model_rate, OUTPUT_RATE))


@app.get("/health")
def health() -> dict:
    return {
        "status": "ready",
        "runtime": runtime.status(),
        "active_session": active.id if active else None,
        "state": active.state if active else "idle",
        "capabilities": ["realtime-seed-vc"],
    }


@app.post("/v1/offer")
async def offer(request: OfferRequest) -> dict:
    ticket = read_ticket(request.session_id, request.token)
    converter = await runtime.ensure(ticket)

    pc = RTCPeerConnection(
        RTCConfiguration(iceServers=[RTCIceServer(urls=["stun:stun.l.google.com:19302"])])
    )
    session = LiveSession(request.session_id, ticket)
    session.pc = pc
    pc.addTrack(session.track)

    global active
    if active is not None and active.pc is not None:
        await active.pc.close()
    active = session

    @pc.on("track")
    def on_track(track: MediaStreamTrack) -> None:
        if track.kind == "audio":
            if session.task is not None:
                session.task.cancel()
            session.task = asyncio.ensure_future(session.run(track))

    @pc.on("connectionstatechange")
    async def on_state() -> None:
        if pc.connectionState in {"failed", "closed", "disconnected"}:
            session.state = pc.connectionState
            if session.task is not None:
                session.task.cancel()

    await pc.setRemoteDescription(RTCSessionDescription(sdp=request.sdp, type=request.type))
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
        "output_sample_rate": OUTPUT_RATE,
    }


@app.post("/v1/sessions/{session_id}/close")
async def close(session_id: str) -> dict:
    global active
    if active is not None and active.id == session_id:
        if active.task is not None:
            active.task.cancel()
        if active.pc is not None:
            await active.pc.close()
        active = None
    return {"closed": session_id}


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
    }
