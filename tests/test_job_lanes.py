"""The resource lanes must let CPU synthesis pass a long GPU job."""

from __future__ import annotations

import sys
import threading
import time
import types
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

# Windows has no fcntl; the executor behavior is still testable there. Linux
# exercises the real shared-volume lock as well.
if sys.platform == "win32":
    sys.modules["fcntl"] = types.SimpleNamespace(LOCK_EX=2, LOCK_UN=8, flock=lambda *_: None)

from worker.api.app import config, jobs  # noqa: E402


class JobLaneTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = TemporaryDirectory()
        self.addCleanup(self.root.cleanup)
        self.patch_root = mock.patch.object(config, "DATA_ROOT", Path(self.root.name))
        self.patch_outputs = mock.patch.object(config, "OUTPUTS_DIR", Path(self.root.name) / "outputs")
        self.patch_root.start()
        self.patch_outputs.start()
        self.addCleanup(self.patch_root.stop)
        self.addCleanup(self.patch_outputs.stop)

    def test_cpu_tts_completes_while_gpu_training_is_running(self) -> None:
        training_started = threading.Event()
        release_training = threading.Event()
        tts_done = threading.Event()
        jobs_by_id = {
            "training": {"id": "training", "kind": "train", "lane": "gpu", "params": {}},
            "speech": {"id": "speech", "kind": "tts", "lane": "cpu", "params": {}},
        }

        def training(job, params):
            training_started.set()
            self.assertTrue(release_training.wait(3))
            artifact = Path(self.root.name) / "model.pth"
            artifact.write_bytes(b"model")
            return {"output_path": str(artifact), "artifact_kind": "rvc-model"}

        def speech(job, params, work):
            artifact = work / "speech.wav"
            artifact.write_bytes(b"audio")
            return {"output_path": str(artifact)}

        def execute(sql, values):
            if "status = 'succeeded'" in sql and values[-1] == "speech":
                tts_done.set()

        with mock.patch.object(jobs.db, "get_job", side_effect=jobs_by_id.get), \
             mock.patch.object(jobs.db, "execute", side_effect=execute), \
             mock.patch.object(jobs, "_update"), \
             mock.patch.object(jobs, "_run_training", side_effect=training), \
             mock.patch.object(jobs, "_run_tts", side_effect=speech):
            try:
                training_future = jobs.submit("training")
                self.assertTrue(training_started.wait(3))
                speech_future = jobs.submit("speech")
                self.assertTrue(tts_done.wait(3), "CPU TTS waited for the GPU training job")
            finally:
                release_training.set()
            training_future.result(timeout=3)
            speech_future.result(timeout=3)

    def test_gpu_jobs_remain_serial(self) -> None:
        active = 0
        peak = 0
        lock = threading.Lock()
        completed = threading.Event()
        finished = 0
        jobs_by_id = {
            name: {"id": name, "kind": "convert", "lane": "gpu", "params": {}}
            for name in ("first", "second")
        }

        def conversion(job, params, work):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.1)
            with lock:
                active -= 1
            artifact = work / "output.wav"
            artifact.write_bytes(b"audio")
            return {"output_path": str(artifact)}

        def execute(sql, values):
            nonlocal finished
            if "status = 'succeeded'" in sql:
                with lock:
                    finished += 1
                    if finished == 2:
                        completed.set()

        with mock.patch.object(jobs.db, "get_job", side_effect=jobs_by_id.get), \
             mock.patch.object(jobs.db, "execute", side_effect=execute), \
             mock.patch.object(jobs, "_update"), \
             mock.patch.object(jobs, "_run_conversion", side_effect=conversion):
            first = jobs.submit("first")
            second = jobs.submit("second")
            self.assertTrue(completed.wait(3))
            first.result(timeout=3)
            second.result(timeout=3)
        self.assertEqual(peak, 1)

    def test_encoding_moves_to_cpu_and_releases_gpu_lane(self) -> None:
        encoding_started = threading.Event()
        release_encoding = threading.Event()
        second_done = threading.Event()
        first_done = threading.Event()
        jobs_by_id = {
            "first": {"id": "first", "kind": "convert", "lane": "gpu", "params": {"output_format": "mp3"}},
            "second": {"id": "second", "kind": "convert", "lane": "gpu", "params": {}},
        }

        def conversion(job, params, work):
            artifact = work / "output.wav"
            artifact.write_bytes(b"audio")
            return {"output_path": str(artifact)}

        def encode(source, target_format):
            if target_format == "wav":
                return source
            encoding_started.set()
            self.assertTrue(release_encoding.wait(3))
            return source.with_suffix(".mp3")

        def execute(sql, values):
            if "status = 'succeeded'" in sql:
                (first_done if values[-1] == "first" else second_done).set()

        with mock.patch.object(jobs.db, "get_job", side_effect=jobs_by_id.get), \
             mock.patch.object(jobs.db, "execute", side_effect=execute), \
             mock.patch.object(jobs, "_update"), \
             mock.patch.object(jobs, "_run_conversion", side_effect=conversion), \
             mock.patch.object(jobs, "transcode", side_effect=encode):
            try:
                first = jobs.submit("first")
                self.assertTrue(encoding_started.wait(3))
                second = jobs.submit("second")
                self.assertTrue(second_done.wait(3), "Encoding held the GPU lane")
            finally:
                release_encoding.set()
            first.result(timeout=3)
            second.result(timeout=3)
            self.assertTrue(first_done.wait(3))


if __name__ == "__main__":
    unittest.main()
