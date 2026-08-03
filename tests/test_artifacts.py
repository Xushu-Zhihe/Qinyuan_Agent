from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from qsubagent.config import ArtifactSettings
from qsubagent.runtime import ArtifactStore


class ArtifactTests(unittest.TestCase):
    def test_capture_is_bounded_and_preserves_head_tail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ArtifactStore(
                ArtifactSettings(
                    root=Path(directory),
                    memory_limit_bytes=20,
                    preview_bytes=10,
                    file_limit_bytes=50,
                    thread_limit_bytes=100,
                )
            )
            capture = store.create_capture("thread", "turn", "stdout.log")
            with capture:
                capture.write(b"0123456789" * 10)

            summary = capture.summary()
            self.assertEqual(capture.path.stat().st_size, 50)
            self.assertEqual(summary["total_bytes"], 100)
            self.assertTrue(summary["artifact_truncated"])
            self.assertEqual(summary["head"], "0123456789")
            self.assertEqual(summary["tail"], "0123456789")

    def test_read_byte_and_line_ranges(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ArtifactStore(ArtifactSettings(root=Path(directory)))
            capture = store.create_capture("thread", "turn", "stdout.log")
            with capture:
                capture.write(b"one\ntwo\nthree\n")
            reference = store.reference(capture.path)

            byte_result = store.read(reference, start_byte=4, max_bytes=3)
            line_result = store.read(reference, start_line=2, line_count=1)

            self.assertEqual(byte_result["content"], "two")
            self.assertEqual(line_result["content"], "two\n")

    def test_read_rejects_path_escape(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ArtifactStore(ArtifactSettings(root=Path(directory)))
            with self.assertRaises(ValueError):
                store.resolve_reference("../outside.txt")
