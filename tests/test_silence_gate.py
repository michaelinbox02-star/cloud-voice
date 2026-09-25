"""Realtime silence gate behaviour.

These run without a GPU. They cover the two failure modes that have both shipped
in this project: a gate that clips the start of speech (the upstream gate), and
no gate at all (which lets the model vocalise babble during silence).
"""

import importlib.util
import pathlib
import sys
import types
import unittest

import numpy as np

ROOT = pathlib.Path(__file__).resolve().parent.parent


def load_gate():
    """Import the gate and its geometry helper without importing torch."""
    source = (ROOT / "worker" / "realtime" / "engine.py").read_text()
    start = source.index("def gate_geometry(")
    end = source.index("class StreamingConverter:")
    module = types.ModuleType("silence_gate_under_test")
    module.__dict__["np"] = np
    exec(compile(source[start:end], "engine.py", "exec"), module.__dict__)
    return module.SilenceGate, module.gate_geometry


SilenceGate, gate_geometry = load_gate()

BLOCK = 5292  # balanced preset at 22050 Hz
SPEECH = 0.05  # about -26 dBFS
SILENCE = 0.0005  # about -66 dBFS


def block(amplitude: float) -> np.ndarray:
    return np.full(BLOCK, amplitude, dtype=np.float32)


def make_gate(**overrides) -> "SilenceGate":
    settings = {
        "threshold": 10.0 ** (-45.0 / 20.0),
        "delay_blocks": 1,
        "hangover_blocks": 1,
        "fade_samples": 441,
    }
    settings.update(overrides)
    return SilenceGate(**settings)


class SilenceGateTests(unittest.TestCase):
    def test_silence_before_speech_is_silent(self):
        gate = make_gate()
        out = np.ones(BLOCK, dtype=np.float32)
        gated = gate.gate(out, block(SILENCE))
        self.assertEqual(float(np.max(np.abs(gated))), 0.0)

    def test_first_speech_block_is_not_clipped(self):
        """The regression that made the upstream gate unusable."""
        gate = make_gate()
        # Speech arrives on the very first block. The engine's buffers were empty,
        # so this first returned block is its rendering of silence and is gated...
        first = gate.gate(np.ones(BLOCK, dtype=np.float32), block(SPEECH))
        self.assertEqual(float(np.max(np.abs(first))), 0.0)
        # ...while the next block carries that speech and must not be clipped.
        second = gate.gate(np.ones(BLOCK, dtype=np.float32), block(SPEECH))
        self.assertGreater(float(np.max(np.abs(second))), 0.0)
        self.assertTrue(gate.open)

    def test_speech_passes_through_unchanged_once_open(self):
        gate = make_gate()
        for _ in range(4):
            gate.gate(np.ones(BLOCK, dtype=np.float32), block(SPEECH))
        out = np.ones(BLOCK, dtype=np.float32)
        gated = gate.gate(out, block(SPEECH))
        np.testing.assert_allclose(gated, out, rtol=1e-6)

    def test_silence_after_speech_fades_then_is_exactly_zero(self):
        gate = make_gate()
        for _ in range(4):
            gate.gate(np.ones(BLOCK, dtype=np.float32), block(SPEECH))
        # Two hangover blocks keep the tail of the phrase audible...
        gate.gate(np.ones(BLOCK, dtype=np.float32), block(SILENCE))
        gate.gate(np.ones(BLOCK, dtype=np.float32), block(SILENCE))
        # ...then the transition block fades rather than cutting...
        fading = gate.gate(np.ones(BLOCK, dtype=np.float32), block(SILENCE))
        self.assertGreater(float(np.max(np.abs(fading))), 0.0)
        self.assertFalse(gate.open)
        # ...and every block after that is exactly silent, with no residual floor.
        closed = gate.gate(np.ones(BLOCK, dtype=np.float32), block(SILENCE))
        self.assertEqual(float(np.max(np.abs(closed))), 0.0)

    def test_fade_is_monotonic_with_no_click(self):
        gate = make_gate(fade_samples=64)
        for _ in range(4):
            gate.gate(np.ones(BLOCK, dtype=np.float32), block(SPEECH))
        # Two hangover blocks keep the tail, then the gate closes.
        gate.gate(np.ones(BLOCK, dtype=np.float32), block(SILENCE))
        gate.gate(np.ones(BLOCK, dtype=np.float32), block(SILENCE))
        gated = gate.gate(np.ones(BLOCK, dtype=np.float32), block(SILENCE))
        head = gated[:64]
        self.assertFalse(gate.open)
        self.assertTrue(np.all(np.diff(head) <= 1e-6), "fade must not rise")
        self.assertLess(head[-1], head[0])

    def test_loud_room_noise_above_threshold_keeps_the_gate_open(self):
        gate = make_gate()
        loud = 10.0 ** (-30.0 / 20.0)
        for _ in range(6):
            gate.gate(np.ones(BLOCK, dtype=np.float32), block(loud))
        self.assertTrue(gate.open)

    def test_geometry_matches_every_preset(self):
        """Presets must delay by exactly the audio the pipeline holds back."""
        rate = 22050
        for name, block_time, right in (
            ("low-latency", 0.12, 0.12),
            ("balanced", 0.24, 0.24),
            ("quality", 0.4, 0.4),
        ):
            zc = rate // 50
            block_seconds = round(block_time * rate / zc) * zc / rate
            delay, hangover, fade = gate_geometry(
                sample_rate=rate,
                block_frame=round(block_time * rate / zc) * zc,
                extra_time_right=right,
                hangover_ms=300.0,
                fade_ms=20.0,
            )
            with self.subTest(preset=name):
                self.assertEqual(delay, max(1, round(right / block_seconds)))
                self.assertGreaterEqual(hangover, 1)
                self.assertGreater(fade, 0)
                self.assertLessEqual(fade, round(block_time * rate / zc) * zc)


if __name__ == "__main__":
    unittest.main()
