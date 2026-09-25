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
    "low-latency": {"block_time": 0.12, "diffusion_steps": 5, "extra_time_ce": 1.5, "extra_time": 0.3, "extra_time_right": 0.12},
    "balanced": {"block_time": 0.24, "diffusion_steps": 8, "extra_time_ce": 2.5, "extra_time": 0.5, "extra_time_right": 0.24},
    "quality": {"block_time": 0.4, "diffusion_steps": 12, "extra_time_ce": 3.0, "extra_time": 0.6, "extra_time_right": 0.4},
}


def _args(gpu: int, fp16: bool) -> argparse.Namespace:
    return argparse.Namespace(gpu=gpu, fp16=fp16, checkpoint_path=None, config_path=None)


def gate_geometry(
    *,
    sample_rate: int,
    block_frame: int,
    extra_time_right: float,
    hangover_ms: float,
    fade_ms: float,
) -> tuple[int, int, int]:
    """Block counts the silence gate needs for a preset.

    Kept separate from the converter so it can be tested without loading models;
    a plain name error here previously reached the worker because every test
    skipped this path.
    """
    block_seconds = max(block_frame / float(sample_rate), 1e-6)
    delay_blocks = max(1, int(round(extra_time_right / block_seconds)))
    hangover_blocks = max(1, int(round((hangover_ms / 1000.0) / block_seconds)))
    fade_samples = min(
        max(1, int(round((fade_ms / 1000.0) * sample_rate))), block_frame
    )
    return delay_blocks, hangover_blocks, fade_samples


class SilenceGate:
    """Suppress model output generated from silence, without clipping speech.

    The pipeline emits audio that entered `delay_blocks` earlier, so the gate
    decision has to be delayed to match. Deciding from the *current* input is what
    made the upstream gate discard short words and phrase endings; having no gate
    at all lets the model vocalise babble and repeated syllables during silence.
    Neither is acceptable, so the decision is delayed rather than dropped.
    """

    def __init__(
        self,
        *,
        threshold: float,
        delay_blocks: int,
        hangover_blocks: int,
        fade_samples: int,
    ) -> None:
        self.threshold = threshold
        self.fade_samples = max(1, fade_samples)
        # Start closed: the engine's buffers are zeroed, so the first block it
        # returns is its rendering of silence no matter what the microphone is
        # doing. Speech fed in now is emitted `delay_blocks` later, gated by the
        # decision recorded from this input.
        self._delay = [False] * max(1, delay_blocks)
        self._hangover_blocks = max(1, hangover_blocks)
        self._silence_blocks = 0
        # Closed gate means zero gain from the start, so the first block is
        # exactly silent rather than fading out of nothing.
        self._gain = 0.0
        self.open = True

    @staticmethod
    def blocks(sample_rate: int, block_frame: int, seconds: float) -> int:
        block_seconds = max(block_frame / float(sample_rate), 1e-6)
        return max(1, int(round(seconds / block_seconds)))

    def gate(self, output: np.ndarray, input_block: np.ndarray) -> np.ndarray:
        """Return `output` gated by the decision recorded `delay_blocks` ago."""
        decision = self._delay.pop(0)
        self.open = decision
        gated = self._apply(output, decision)

        energy = float(np.sqrt(np.mean(np.square(input_block)))) if input_block.size else 0.0
        if energy >= self.threshold:
            self._silence_blocks = 0
            self._delay.append(True)
        else:
            self._silence_blocks += 1
            # Hangover keeps trailing consonants and endings audible.
            self._delay.append(self._silence_blocks <= self._hangover_blocks)
        return gated

    def _apply(self, output: np.ndarray, decision: bool) -> np.ndarray:
        target = 1.0 if decision else 0.0
        if self._gain == target:
            return np.zeros_like(output) if target == 0.0 else output
        ramp = min(self.fade_samples, output.shape[0])
        output[:ramp] *= np.linspace(self._gain, target, ramp, dtype=np.float32)
        if ramp < output.shape[0]:
            # Multiplying by zero is what makes the closed gate exactly silent;
            # zeroing the block outright would erase the fade just applied.
            output[ramp:] *= target
        self._gain = target
        return output


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
        silence_gate: bool = True,
        gate_threshold_db: float = -45.0,
        gate_hangover_ms: float = 300.0,
        gate_fade_ms: float = 20.0,
    ):
        settings = dict(PRESETS.get(preset, PRESETS["balanced"]))
        if diffusion_steps is not None:
            settings["diffusion_steps"] = diffusion_steps
        if block_time is not None:
            settings["block_time"] = block_time
        self.settings = settings
        self.gate_enabled = silence_gate
        self.gate_threshold = 10.0 ** (gate_threshold_db / 20.0)
        self.gate_hangover_ms = gate_hangover_ms
        self.gate_fade_ms = gate_fade_ms
        self.gate_open = True

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
            extra_time_right=settings["extra_time_right"],
            sr_type="sr_model",
            function="vc",
        )
        engine.get_device_channels = lambda: 1
        engine.get_device_samplerate = lambda: engine.model_set[-1]["sampling_rate"]
        with self._lock:
            engine._prepare_buffers()

        # The block just returned corresponds to input from `extra_time_right`
        # earlier: the window ends at the current input, but the engine emits the
        # audio that sat that far before the end. The gate therefore has to use a
        # past decision, or the first block of every utterance is silenced - the
        # defect that led to removing the upstream gate entirely.
        (
            self.gate_delay_blocks,
            self.gate_hangover_blocks,
            self.gate_fade_samples,
        ) = gate_geometry(
            sample_rate=self.sample_rate,
            block_frame=self.block_frame,
            extra_time_right=settings["extra_time_right"],
            hangover_ms=self.gate_hangover_ms,
            fade_ms=self.gate_fade_ms,
        )
        self._gate = SilenceGate(
            threshold=self.gate_threshold,
            delay_blocks=self.gate_delay_blocks,
            hangover_blocks=self.gate_hangover_blocks,
            fade_samples=self.gate_fade_samples,
        )
        self.gate_open = True

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
        if not self.gate_enabled:
            return output
        gated = self._gate.gate(output, block)
        self.gate_open = self._gate.open
        return gated

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
