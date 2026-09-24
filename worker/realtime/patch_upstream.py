"""Remove the upstream VAD output gate from the pinned realtime fork.

The fork infers audio from a window ending at the current input, then returns
audio from before its right-context tail. Its VAD decision uses the current
input instead of that delayed output. A short word can therefore be muted in
full even though inference produced it. The worker keeps the upstream model and
SOLA path, but omits the unused VAD load and per-block computation as well.
"""

from pathlib import Path


source = Path("/opt/seed-vc-realtime/realtime_vc_engine.py")
model_load = '        self.vad_model = AutoModel(model="fsmn-vad", model_revision="v2.0.4", disable_pbar=True, log_level="ERROR", disable_update=True)\n'
vad_start = "        # VAD Processing\n"
preprocessing_start = "        # Preprocessing\n"
before = '''        # Only apply VAD muting during voice conversion mode
        if self.config.function == "vc" and not self.vad_speech_detected:
             infer_wav = torch.zeros_like(self.input_wav[self.extra_frame :])
'''
after = '''        # The VAD state describes current input, while infer_wav is delayed by
        # extra_time_right. Gating here discards short words and phrase endings.
'''
contents = source.read_text()
if any(contents.count(snippet) != 1 for snippet in (model_load, vad_start, preprocessing_start, before)):
    raise SystemExit("Pinned realtime upstream changed; VAD patch needs review")
contents = contents.replace(model_load, "        # Server conversion preserves all speech without a VAD gate.\n")
start = contents.index(vad_start)
end = contents.index(preprocessing_start, start)
contents = contents[:start] + "        # Preserve every input block, including short words and phrase endings.\n\n" + contents[end:]
source.write_text(contents.replace(before, after))
