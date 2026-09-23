"""Block-oriented wrapper around the Seed-VC realtime engine.

The upstream fork drives the pipeline from a PortAudio callback. Here the
transport is WebRTC, so this module feeds the same `_process_block` pipeline
from network audio and never opens a sound device. One block in gives exactly
one block out, which keeps the stream in step.
"""

from __future__ import annotations

import argparse
import threading

import librosa
import numpy as np
import torch

import realtime_vc_engine as upstream

PRESETS = {
    "low-latency": {"block_time": 0.12, "diffusion_steps": 6, "extra_time_ce": 1.5, "extra_time": 0.3},
    "balanced": {"block_time": 0.25, "diffusion_steps": 8, "extra_time_ce": 2.5, "extra_time": 0.5},
    "quality": {"block_time": 0.4, "diffusion_steps": 12, "extra_time_ce": 3.0, "extra_time": 0.6},
}


def _args(gpu: int, fp16: bool) -> argparse.Namespace:
    return argparse.Namespace(gpu=gpu, fp16=fp16, checkpoint_path=None, config_path=None)


class StreamingConverter:
    """Feeds fixed-size mono blocks through Seed-VC and returns converted blocks."""

    def __init__(
        self,
        reference_path: str,
        *,
        preset: str = "balanced",
        diffusion_steps: int | None = None,
        block_time: float | None = None,
        inference_cfg_rate: float = 0.7,
        gpu: int = 0,
        fp16: bool = True,
    ):
        settings = dict(PRESETS.get(preset, PRESETS["balanced"]))
        if diffusion_steps is not None:
            settings["diffusion_steps"] = diffusion_steps
        if block_time is not None:
            settings["block_time"] = block_time
        self.settings = settings

        # The constructor enumerates sound devices, which a GPU container has no
        # reason to own. Neutralise it before the instance is built.
        upstream.RealtimeVCEngine.update_devices = lambda self, hostapi_name=None: None
        self._engine = upstream.RealtimeVCEngine(_args(gpu, fp16))
        self._lock = threading.Lock()

        self.configure(
            reference_path,
            preset=preset,
            diffusion_steps=settings["diffusion_steps"],
            block_time=settings["block_time"],
            inference_cfg_rate=inference_cfg_rate,
        )

    def configure(
        self,
        reference_path: str,
        *,
        preset: str = "balanced",
        diffusion_steps: int | None = None,
        block_time: float | None = None,
        inference_cfg_rate: float = 0.7,
    ) -> None:
        """Point the pipeline at a reference voice and resize its buffers."""
        settings = dict(PRESETS.get(preset, PRESETS["balanced"]))
        if diffusion_steps is not None:
            settings["diffusion_steps"] = diffusion_steps
        if block_time is not None:
            settings["block_time"] = block_time
        self.settings = settings

        engine = self._engine
        engine.set_config(
            reference_audio_path=reference_path,
            diffusion_steps=settings["diffusion_steps"],
            block_time=settings["block_time"],
            inference_cfg_rate=inference_cfg_rate,
            extra_time_ce=settings["extra_time_ce"],
            extra_time=settings["extra_time"],
            sr_type="sr_model",
            function="vc",
        )
        engine.get_device_channels = lambda: 1
        engine.get_device_samplerate = lambda: engine.model_set[-1]["sampling_rate"]
        with self._lock:
            engine._prepare_buffers()

    @property
    def sample_rate(self) -> int:
        return int(self._engine.config.samplerate)

    @property
    def block_frame(self) -> int:
        return int(self._engine.block_frame)

    @property
    def block_seconds(self) -> float:
        return self.block_frame / self.sample_rate

    def process(self, block: np.ndarray) -> np.ndarray:
        """Convert exactly `block_frame` mono float32 samples at `sample_rate`."""
        if block.shape[0] != self.block_frame:
            raise ValueError(f"expected {self.block_frame} samples, received {block.shape[0]}")
        with self._lock:
            output = self._engine._process_block(np.ascontiguousarray(block, dtype=np.float32))
        return output

    def reset(self) -> None:
        with self._lock:
            self._engine._prepare_buffers()


def resample(audio: np.ndarray, source_rate: int, target_rate: int) -> np.ndarray:
    if source_rate == target_rate:
        return audio.astype(np.float32, copy=False)
    return librosa.resample(
        np.asarray(audio, dtype=np.float32), orig_sr=source_rate, target_sr=target_rate
    ).astype(np.float32, copy=False)


def peak_vram_mib() -> float:
    if not torch.cuda.is_available():
        return 0.0
    return round(torch.cuda.max_memory_allocated() / (1024 * 1024), 1)
