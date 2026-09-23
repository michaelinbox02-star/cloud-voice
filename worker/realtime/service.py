"""WebRTC transport for the Seed-VC realtime engine.

One GPU means one live session at a time, so a single runtime owns the model and
re-points it at whichever reference voice the active session asked for.

Sessions are minted by the control plane with a short-lived token; media travels
over UDP directly to this host, while signalling stays on loopback behind the
desktop's SSH tunnel.
"""

from __future__ import annotations

import asyncio
import hmac
import os
import secrets
import time
from fractions import Fraction
from pathlib import Path

import av
import numpy as np
from aiortc import MediaStreamTrack, RTCConfiguration, RTCIceServer, RTCPeerConnection, RTCSessionDescription
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from engine import StreamingConverter, resample

os.chdir("/opt/seed-vc-realtime")

DATA_ROOT = Path(os.environ.get("CLOUD_VOICE_DATA_ROOT", "/data")).resolve()
ENGINE_TOKEN = os.environ.get("CLOUD_VOICE_ENGINE_TOKEN", "")
SESSION_TTL_SECONDS = int(os.environ.get("CLOUD_VOICE_REALTIME_TTL", "900"))

OUTPUT_RATE = 48000
OUTPUT_FRAME_SAMPLES = 960  # 20 ms of Opus
PREFILL_FRAMES = 4
MAX_QUEUED_FRAMES = 40

app = FastAPI(title="Cloud Voice Studio Realtime", version="0.1.0", docs_url=None, redoc_url=None)


def require_engine_token(authorization: str | None = Header(default=None)) -> None:
    supplied = authorization.removeprefix("Bearer ") if authorization else ""
    if not ENGINE_TOKEN or not hmac.compare_digest(supplied, ENGINE_TOKEN):
        raise HTTPException(status_code=401, detail="Invalid engine credential")


def resolve_reference(raw: str) -> Path:
    path = Path(raw)
    if not path.is_absolute():
        raise HTTPException(status_code=400, detail="Reference path must be absolute")
    resolved = path.resolve()
    if not resolved.is_relative_to(DATA_ROOT) or not resolved.is_file():
        raise HTTPException(status_code=400, detail="Reference audio is not on this worker")
    return resolved


class SessionRequest(BaseModel):
    reference_path: str
    preset: str = Field(default="balanced")
    diffusion_steps: int | None = Field(default=None, ge=1, le=50)
    inference_cfg_rate: float = Field(default=0.7, ge=0.0, le=2.0)


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
        self.loading = False

    async def ensure(self, request: SessionRequest) -> StreamingConverter:
        signature = (request.reference_path, request.preset, request.diffusion_steps, request.inference_cfg_rate)
        async with self.lock:
            if self.converter is not None and self.signature == signature:
                return self.converter
            self.loading = True
            try:
                if self.converter is None:
                    self.converter = await asyncio.to_thread(
                        StreamingConverter,
                        request.reference_path,
                        preset=request.preset,
                        diffusion_steps=request.diffusion_steps,
                        inference_cfg_rate=request.inference_cfg_rate,
                    )
                else:
                    converter = self.converter
                    await asyncio.to_thread(
                        converter.configure,
                        request.reference_path,
                        preset=request.preset,
                        diffusion_steps=request.diffusion_steps,
                        inference_cfg_rate=request.inference_cfg_rate,
                    )
                self.signature = signature
            finally:
                self.loading = False
            return self.converter

    def status(self) -> dict:
        if self.converter is None:
            return {"loaded": False}
        return {
            "loaded": True,
            "reference": self.signature[0] if self.signature else None,
            "model_rate": self.converter.sample_rate,
            "block_samples": self.converter.block_frame,
            "block_seconds": round(self.converter.block_seconds, 4),
        }


runtime = Runtime()
sessions: dict[str, dict] = {}
active: dict | None = None


class ConvertedTrack(MediaStreamTrack):
    """Audio produced by the conversion pipeline, paced by arrival."""

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
            frame.planes[0].update(chunk.astype(np.float32).tobytes())
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


class Session:
    def __init__(self, session_id: str, request: SessionRequest) -> None:
        self.id = session_id
        self.request = request
        self.token = secrets.token_urlsafe(24)
        self.expires_at = time.time() + SESSION_TTL_SECONDS
        self.pc: RTCPeerConnection | None = None
        self.track = ConvertedTrack()
        self.task: asyncio.Task | None = None
        self.inference_ms: list[float] = []
        self.state = "created"
        self.error: str | None = None

    async def run(self, incoming: MediaStreamTrack) -> None:
        converter = await runtime.ensure(self.request)
        converters = converter
        model_rate = converter.sample_rate
        block = converter.block_frame
        needed_input = round(block * OUTPUT_RATE / model_rate)

        self.state = "streaming"
        buffer = np.zeros(0, dtype=np.float32)

        while True:
            frame = await incoming.recv()
            frame = frame.reformat(format="fltp")
            data = frame.to_ndarray()
            if frame.layout.nb_channels > 1:
                data = data.mean(axis=0)
            else:
                data = data.reshape(-1)
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
                converted = await asyncio.to_thread(converters.process, resampled)
                elapsed = (time.perf_counter() - began) * 1000
                self.inference_ms.append(elapsed)
                del self.inference_ms[:-200]
                self.track.push(resample(converted, model_rate, OUTPUT_RATE))


@app.get("/health", dependencies=[Depends(require_engine_token)])
def health() -> dict:
    return {
        "status": "ready",
        "runtime": runtime.status(),
        "active_session": active["session"].id if active else None,
        "capabilities": ["realtime-seed-vc"],
    }


@app.post("/v1/sessions", dependencies=[Depends(require_engine_token)])
def create_session(request: SessionRequest) -> dict:
    if request.preset not in {"low-latency", "balanced", "quality"}:
        raise HTTPException(status_code=400, detail="preset must be low-latency, balanced or quality")
    reference = resolve_reference(request.reference_path)

    global active
    if active is not None:
        previous = active["session"]
        if previous.pc is not None:
            asyncio.get_event_loop().create_task(previous.pc.close())
        active = None

    session_id = secrets.token_hex(8)
    session = Session(session_id, SessionRequest(
        reference_path=str(reference),
        preset=request.preset,
        diffusion_steps=request.diffusion_steps,
        inference_cfg_rate=request.inference_cfg_rate,
    ))
    sessions[session_id] = {"session": session}
    active = sessions[session_id]
    return {
        "session_id": session_id,
        "token": session.token,
        "expires_at": session.expires_at,
        "output_sample_rate": OUTPUT_RATE,
    }


def lookup(session_id: str, token: str) -> Session:
    entry = sessions.get(session_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Unknown session")
    session: Session = entry["session"]
    if not hmac.compare_digest(token, session.token):
        raise HTTPException(status_code=401, detail="Invalid session token")
    if time.time() > session.expires_at:
        raise HTTPException(status_code=410, detail="Session expired")
    return session


@app.post("/v1/offer")
async def offer(request: OfferRequest) -> dict:
    session = lookup(request.session_id, request.token)

    # Load or re-point the model before answering, so the client learns early if
    # the voice cannot be prepared.
    converter = await runtime.ensure(session.request)

    configuration = RTCConfiguration(
        iceServers=[RTCIceServer(urls=["stun:stun.l.google.com:19302"])]
    )
    pc = RTCPeerConnection(configuration)
    session.pc = pc
    pc.addTrack(session.track)

    @pc.on("track")
    def on_track(track: MediaStreamTrack) -> None:
        if track.kind != "audio":
            return
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

    # Wait for gathering so the client receives usable candidates in one shot.
    deadline = time.time() + 8
    while pc.iceGatheringState != "complete" and time.time() < deadline:
        await asyncio.sleep(0.1)

    return {
        "sdp": pc.localDescription.sdp,
        "type": pc.localDescription.type,
        "model_rate": converter.sample_rate,
        "block_seconds": round(converter.block_seconds, 4),
    }


@app.get("/v1/sessions/{session_id}/stats")
def stats(session_id: str, token: str) -> dict:
    session = lookup(session_id, token)
    samples = session.inference_ms[-40:]
    return {
        "state": session.state,
        "blocks": len(session.inference_ms),
        "inference_ms_mean": round(float(np.mean(samples)), 1) if samples else None,
        "inference_ms_p95": round(float(np.percentile(samples, 95)), 1) if samples else None,
        "block_seconds": round(session.request and runtime.converter.block_seconds, 4) if runtime.converter else None,
        "queued_frames": session.track.queue.qsize(),
        "dropped_frames": session.track.dropped,
        "error": session.error,
    }
