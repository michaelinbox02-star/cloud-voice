"""GPU test for the tunnelled transport and the silence gate.

Runs inside the realtime container on the worker. It streams a real recording
through the WebSocket transport followed by two seconds of digital silence, then
asserts two things that have both regressed before:

* speech survives the round trip, and
* silence comes back silent, rather than as the babble and repeated syllables a
  zero-shot model invents when it is fed noise.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import urllib.request

import numpy as np
import soundfile as sf
import websockets

API = os.environ.get("CLOUD_VOICE_API", "http://127.0.0.1:8765")
STREAM = os.environ.get("CLOUD_VOICE_STREAM", "ws://127.0.0.1:8791/v1/stream")
TOKEN = os.environ["CLOUD_VOICE_API_TOKEN"]
VOICE_ID = os.environ.get("CLOUD_VOICE_VOICE_ID", "")
SOURCE = os.environ.get("CLOUD_VOICE_SOURCE", "/data/tunnel-source.wav")
OUTPUT = os.environ.get("CLOUD_VOICE_OUTPUT", "/data/tunnel-out.wav")
PRESET = os.environ.get("CLOUD_VOICE_PRESET", "low-latency")
RATE = 48000
FRAME = 960  # 20 ms
SILENCE_SECONDS = 2.0


def mint_session() -> dict:
    request = urllib.request.Request(
        f"{API}/v1/realtime/sessions",
        data=json.dumps({"voice_id": VOICE_ID, "preset": PRESET}).encode(),
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=300) as response:
        return json.loads(response.read().decode())


def load_source() -> np.ndarray:
    audio, rate = sf.read(SOURCE, dtype="float32")
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    if rate != RATE:
        import librosa

        audio = librosa.resample(audio, orig_sr=rate, target_sr=RATE)
    return audio.astype(np.float32)


async def run() -> int:
    ticket = mint_session()
    speech = load_source()
    silence = np.zeros(int(RATE * SILENCE_SECONDS), dtype=np.float32)
    payload = np.concatenate((speech, silence))
    speech_samples = len(speech)

    url = (
        f"{STREAM}?session_id={ticket['session_id']}&token={ticket['token']}"
    )
    received: list[np.ndarray] = []

    async with websockets.connect(url, max_size=None) as socket:

        async def send() -> None:
            for start in range(0, len(payload), FRAME):
                chunk = payload[start : start + FRAME]
                if len(chunk) < FRAME:
                    chunk = np.pad(chunk, (0, FRAME - len(chunk)))
                pcm = np.clip(chunk, -1.0, 1.0)
                await socket.send((pcm * 32767.0).astype("<i2").tobytes())
                await asyncio.sleep(FRAME / RATE)

        async def receive() -> None:
            while True:
                data = await socket.recv()
                received.append(np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0)

        receiver = asyncio.ensure_future(receive())
        await send()
        await asyncio.sleep(1.0)
        receiver.cancel()

    if not received:
        print("FAIL: no audio returned from the tunnelled transport")
        return 1

    converted = np.concatenate(received)
    sf.write(OUTPUT, converted, RATE)

    # A small part of the tail belongs to the speech that was still in flight.
    boundary = speech_samples - int(RATE * 0.5)
    speech_region = converted[:boundary]
    silence_region = converted[speech_samples + int(RATE * 0.5) :] if len(converted) > speech_samples else np.zeros(1)

    speech_rms = float(np.sqrt(np.mean(np.square(speech_region)))) if speech_region.size else 0.0
    silence_peak = float(np.max(np.abs(silence_region))) if silence_region.size else 0.0
    silence_rms = float(np.sqrt(np.mean(np.square(silence_region)))) if silence_region.size else 0.0

    print(f"returned {len(converted)/RATE:.2f}s of audio")
    print(f"speech region: rms={speech_rms:.4f}")
    print(f"silence region ({silence_region.size/RATE:.2f}s): rms={silence_rms:.6f} peak={silence_peak:.6f}")

    ok = True
    if speech_rms < 1e-3:
        print("FAIL: speech did not survive the round trip")
        ok = False
    if silence_peak > 0.02:
        print("FAIL: the gate let audible output through during silence")
        ok = False
    print("PASS" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
