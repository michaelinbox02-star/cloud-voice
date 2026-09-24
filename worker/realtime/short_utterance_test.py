"""GPU smoke test: a short utterance must survive every realtime preset."""

from __future__ import annotations

import time

import numpy as np
import soundfile as sf

from engine import PRESETS, StreamingConverter, resample


reference = "examples/reference/s1p1.wav"
source, source_rate = sf.read("examples/source/source_s1.wav", dtype="float32")
if source.ndim > 1:
    source = source.mean(axis=1)

converter = StreamingConverter(reference, preset="quality")
rate = converter.sample_rate
source = resample(source, source_rate, rate)
speech_samples = round(0.72 * rate)
starts = range(0, len(source) - speech_samples, round(0.1 * rate))
start = max(starts, key=lambda index: float(np.mean(source[index : index + speech_samples] ** 2)))
speech = source[start : start + speech_samples]

for preset in PRESETS:
    converter.configure(reference, preset=preset)
    block = converter.block_frame
    input_audio = np.concatenate(
        (np.zeros(4 * block, dtype=np.float32), speech, np.zeros(12 * block, dtype=np.float32))
    )
    output_blocks: list[np.ndarray] = []
    elapsed_ms: list[float] = []
    for offset in range(0, len(input_audio), block):
        chunk = input_audio[offset : offset + block]
        if len(chunk) < block:
            chunk = np.pad(chunk, (0, block - len(chunk)))
        began = time.perf_counter()
        output_blocks.append(converter.process(chunk))
        elapsed_ms.append((time.perf_counter() - began) * 1000)

    block_rms = np.array([float(np.sqrt(np.mean(chunk ** 2))) for chunk in output_blocks])
    active = np.flatnonzero(block_rms > 0.01)
    active_seconds = len(active) * converter.block_seconds
    print(
        f"{preset}: block={converter.block_seconds * 1000:.0f}ms "
        f"mean={np.mean(elapsed_ms[3:]):.0f}ms p95={np.percentile(elapsed_ms[3:], 95):.0f}ms "
        f"active={active_seconds:.2f}s peak_rms={block_rms.max():.3f}",
        flush=True,
    )
    if active_seconds < 0.6 * len(speech) / rate:
        raise SystemExit(f"{preset} lost the short utterance")
    if active[0] > 7:
        raise SystemExit(f"{preset} delayed the short utterance by too many blocks")

print("Short utterance passed for every realtime preset.")
