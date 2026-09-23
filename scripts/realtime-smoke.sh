#!/usr/bin/env bash
# Run a real recording through the realtime streaming pipeline, block by block,
# and report per-block inference time. Does not use WebRTC or a sound device.
# Usage: bash scripts/realtime-smoke.sh [user@gpu-host]
set -euo pipefail

remote="${1:-}"
repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ -n "${remote}" ]]; then
  exec ssh "${remote}" "cd \"\$HOME/cloud-voice\" && bash scripts/realtime-smoke.sh"
fi

docker_cmd=(docker)
if ! docker info >/dev/null 2>&1; then
  docker_cmd=(sudo docker)
fi

"${docker_cmd[@]}" build -t cloud-voice-realtime:dev "${repo_dir}/worker/realtime"

"${docker_cmd[@]}" run --rm --gpus all \
  -v cloud-voice-realtime-checkpoints:/opt/seed-vc-realtime/checkpoints \
  -v cloud-voice-realtime-data:/data \
  cloud-voice-realtime:dev \
  python - <<'PY'
import time
import numpy as np
import soundfile as sf

from engine import StreamingConverter, resample

reference = "examples/reference/s1p1.wav"
source = "examples/source/source_s1.wav"

converter = StreamingConverter(reference, preset="balanced")
rate = converter.sample_rate
block = converter.block_frame
print(f"model rate={rate} block={block} ({converter.block_seconds:.3f}s)")

audio, sr = sf.read(source, dtype="float32")
if audio.ndim > 1:
    audio = audio.mean(axis=1)
audio = resample(audio, sr, rate)

output = []
times = []
for start in range(0, len(audio) - block, block):
    chunk = audio[start : start + block]
    began = time.perf_counter()
    output.append(converter.process(chunk))
    times.append(time.perf_counter() - began)

converted = np.concatenate(output)
np.save("/tmp/converted_blocks.npy", converted)
sf.write("/data/realtime-smoke-out.wav", converted, rate)

durations = np.array(times[2:])  # skip warmup blocks
print(f"blocks={len(times)} audio={len(converted)/rate:.2f}s")
print(f"per-block inference: mean={durations.mean()*1000:.0f}ms p95={np.percentile(durations,95)*1000:.0f}ms max={durations.max()*1000:.0f}ms")
print(f"block budget={converter.block_seconds*1000:.0f}ms realtime_factor={(durations.mean()/converter.block_seconds):.2f}")
rms = float(np.sqrt((converted**2).mean()))
print(f"output rms={rms:.5f}")
raise SystemExit(0 if rms > 1e-3 else 1)
PY

echo "Realtime engine smoke test passed."
