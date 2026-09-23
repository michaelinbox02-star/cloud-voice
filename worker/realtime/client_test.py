"""End-to-end WebRTC client used by the integration test.

Acts like the desktop: asks the control plane for a short-lived session, sends
an offer with recorded audio, receives the converted stream and writes it to
disk. Runs inside the realtime image with host networking so the media path is
exercised exactly as a remote client would exercise it.
"""

from __future__ import annotations

import asyncio
import fractions
import json
import os
import sys
import urllib.request
from pathlib import Path

import av
import numpy as np
import soundfile as sf
from aiortc import MediaStreamTrack, RTCPeerConnection, RTCSessionDescription
from aiortc.mediastreams import MediaStreamError

API = os.environ.get("CLOUD_VOICE_API", "http://127.0.0.1:8765")
SIGNALING = os.environ.get("CLOUD_VOICE_SIGNALING", "http://127.0.0.1:8791")
API_TOKEN = os.environ["CLOUD_VOICE_API_TOKEN"]
SOURCE = Path(os.environ.get("CLOUD_VOICE_SOURCE", "/data/test-source.wav"))
OUTPUT = Path(os.environ.get("CLOUD_VOICE_OUTPUT", "/data/realtime-rtc-out.wav"))
VOICE_ID = os.environ.get("CLOUD_VOICE_VOICE_ID", "")
PRESET = os.environ.get("CLOUD_VOICE_PRESET", "balanced")
SECONDS = float(os.environ.get("CLOUD_VOICE_SECONDS", "10"))


def post(url: str, payload: dict, token: str | None = None) -> dict:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=600) as response:
        return json.loads(response.read().decode())


class FileAudioTrack(MediaStreamTrack):
    """Plays a file at real time so the server sees a live microphone."""

    kind = "audio"

    def __init__(self, samples: np.ndarray, rate: int) -> None:
        super().__init__()
        self.samples = samples
        self.rate = rate
        self.index = 0
        self.timestamp = 0
        self.started = False

    async def recv(self) -> av.AudioFrame:
        if not self.started:
            self.started = True
            print("client: sender started", flush=True)
        samples = 960
        chunk = self.samples[self.index : self.index + samples]
        if len(chunk) < samples:
            chunk = np.pad(chunk, (0, samples - len(chunk)))
        self.index += samples
        frame = av.AudioFrame(format="s16", layout="mono", samples=samples)
        frame.sample_rate = self.rate
        pcm = np.clip(chunk, -1.0, 1.0)
        frame.planes[0].update((pcm * 32767.0).astype(np.int16).tobytes())
        frame.pts = self.timestamp
        frame.time_base = fractions.Fraction(1, self.rate)
        self.timestamp += samples
        if (self.timestamp // samples) % 100 == 0:
            print(f"client: sent {self.timestamp // samples} frames", flush=True)
        await asyncio.sleep(samples / self.rate)
        return frame

    @property
    def exhausted(self) -> bool:
        return self.index >= len(self.samples)


async def main() -> int:
    ticket = post(
        f"{API}/v1/realtime/sessions",
        {"voice_id": VOICE_ID, "preset": PRESET},
        API_TOKEN,
    )
    print(f"session={ticket['session_id']} voice={ticket['voice_name']} preset={ticket['preset']}", flush=True)

    audio, rate = sf.read(SOURCE, dtype="float32")
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    if rate != 48000:
        import librosa

        audio = librosa.resample(audio, orig_sr=rate, target_sr=48000)
    source_track = FileAudioTrack(audio, 48000)

    pc = RTCPeerConnection()
    pc.addTrack(source_track)

    remote: list[MediaStreamTrack] = []

    @pc.on("track")
    def on_track(track: MediaStreamTrack) -> None:
        remote.append(track)

    @pc.on("connectionstatechange")
    async def on_state() -> None:
        print(f"connection state: {pc.connectionState}", flush=True)

    await pc.setLocalDescription(await pc.createOffer())
    answer = post(
        f"{SIGNALING}/v1/offer",
        {
            "session_id": ticket["session_id"],
            "token": ticket["token"],
            "sdp": pc.localDescription.sdp,
            "type": pc.localDescription.type,
        },
    )
    await pc.setRemoteDescription(RTCSessionDescription(sdp=answer["sdp"], type=answer["type"]))
    print(f"negotiated: block={answer['block_seconds']}s model_rate={answer['model_rate']}", flush=True)

    # Wait for the transport to come up before judging the media path.
    deadline = asyncio.get_event_loop().time() + 20
    while pc.connectionState != "connected" and asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.2)
    if pc.connectionState != "connected":
        print(f"transport never connected: {pc.iceConnectionState} / {pc.connectionState}", flush=True)
        await pc.close()
        return 1

    if not remote:
        print("no remote audio track was attached", flush=True)
        await pc.close()
        return 1

    received: list[np.ndarray] = []
    track = remote[0]
    deadline = asyncio.get_event_loop().time() + SECONDS
    while asyncio.get_event_loop().time() < deadline:
        try:
            frame = await asyncio.wait_for(track.recv(), timeout=5)
        except (asyncio.TimeoutError, MediaStreamError) as error:
            print(f"receive stopped after {len(received)} frames: {type(error).__name__}", flush=True)
            break
        data = frame.reformat(format="fltp").to_ndarray()
        data = data.mean(axis=0) if frame.layout.nb_channels > 1 else data.reshape(-1)
        received.append(data.astype(np.float32))

    stats = json.loads(
        urllib.request.urlopen(
            f"{SIGNALING}/v1/sessions/{ticket['session_id']}/stats", timeout=30
        ).read()
    )
    await pc.close()

    if not received:
        print("no audio received from the worker", flush=True)
        return 1

    converted = np.concatenate(received)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    sf.write(OUTPUT, converted, 48000)
    rms = float(np.sqrt((converted**2).mean()))
    print(f"received {len(converted)/48000:.2f}s rms={rms:.5f}", flush=True)
    print(f"server stats: {json.dumps(stats)}", flush=True)
    return 0 if rms > 5e-3 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
