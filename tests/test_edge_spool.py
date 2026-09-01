from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from kla_sync.ingestion.edge import (
    CaptureSource,
    EdgeSpool,
    FFmpegCaptureConfig,
    build_ffmpeg_capture_command,
    redact_capture_command,
)


class EdgeSpoolTests(unittest.TestCase):
    def test_capture_file_survives_retry_then_acknowledgement(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            chunk_file = root / "chunk.wav"
            chunk_file.write_bytes(b"not real wav content for queue test")
            spool = EdgeSpool(root / "spool.sqlite3")
            start = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)
            queued = spool.enqueue_file(
                "kampala-01",
                chunk_file,
                start,
                start + timedelta(seconds=30),
                now=start,
            )
            self.assertGreater(spool.queued_bytes(), 0)

            claimed = spool.claim_due(now=start + timedelta(seconds=1))
            self.assertEqual(claimed.chunk_id, queued.chunk_id)
            self.assertEqual(claimed.attempts, 1)
            spool.mark_retry(
                claimed.chunk_id,
                "network unavailable",
                retry_after=timedelta(seconds=5),
                now=start + timedelta(seconds=1),
            )
            self.assertIsNone(spool.claim_due(now=start + timedelta(seconds=4)))

            retry = spool.claim_due(now=start + timedelta(seconds=7))
            self.assertEqual(retry.chunk_id, queued.chunk_id)
            self.assertEqual(retry.attempts, 2)
            spool.mark_uploaded(retry.chunk_id, "receipt-123")
            self.assertEqual(spool.queued_bytes(), 0)

    def test_ffmpeg_input_is_redacted_in_diagnostics(self) -> None:
        command = build_ffmpeg_capture_command(
            CaptureSource("gulu-01", "https://user:secret@example.test/live", "Gulu listener"),
            FFmpegCaptureConfig(Path("captures")),
        )
        safe = redact_capture_command(command)
        self.assertIn("<redacted-input-url>", safe)
        self.assertNotIn("https://user:secret@example.test/live", safe)


if __name__ == "__main__":
    unittest.main()
