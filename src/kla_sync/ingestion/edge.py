"""Offline-first primitives for Raspberry Pi / venue listener nodes.

Edge devices capture short, mono PCM chunks locally, fingerprint them, and queue
compact manifests/hashes for upload.  Raw audio upload is policy-controlled and
must never be assumed on a constrained MTN/Airtel cellular connection.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Protocol
from uuid import uuid4


@dataclass(frozen=True, slots=True)
class CaptureSource:
    """A configured stream source. Do not log a URL that contains credentials."""

    source_id: str
    input_url: str
    display_name: str

    def __post_init__(self) -> None:
        if not all((self.source_id.strip(), self.input_url.strip(), self.display_name.strip())):
            raise ValueError("source_id, input_url, and display_name are required")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", self.source_id):
            raise ValueError("source_id may contain only letters, digits, dot, underscore, and hyphen")


@dataclass(frozen=True, slots=True)
class FFmpegCaptureConfig:
    """Fixed analysis format shared with the fingerprint worker."""

    output_directory: Path
    segment_seconds: int = 30
    sample_rate: int = 11_025
    ffmpeg_binary: str = "ffmpeg"

    def __post_init__(self) -> None:
        if self.segment_seconds < 5:
            raise ValueError("segment_seconds must be at least 5")
        if self.sample_rate <= 0:
            raise ValueError("sample_rate must be positive")
        if not self.ffmpeg_binary.strip():
            raise ValueError("ffmpeg_binary is required")


def build_ffmpeg_capture_command(source: CaptureSource, config: FFmpegCaptureConfig) -> list[str]:
    """Build a shell-safe FFmpeg command for resilient stream segmentation.

    Invoke with ``subprocess.Popen(command)`` (never ``shell=True``). The output
    pattern is intentionally local; a spool worker manages upload/retry after a
    chunk closes.
    """

    # Keep a monotonically numbered filename. FFmpeg's strftime mode would
    # interpret ``%d`` as day-of-month rather than a segment sequence number.
    # The watcher records authoritative start/end timestamps in the manifest.
    output_path = config.output_directory / source.source_id / "chunk_%09d.wav"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    return [
        config.ffmpeg_binary,
        "-hide_banner",
        "-loglevel",
        "warning",
        "-nostdin",
        "-reconnect",
        "1",
        "-reconnect_streamed",
        "1",
        "-reconnect_delay_max",
        "30",
        "-i",
        source.input_url,
        "-vn",
        "-ac",
        "1",
        "-ar",
        str(config.sample_rate),
        "-c:a",
        "pcm_s16le",
        "-f",
        "segment",
        "-segment_time",
        str(config.segment_seconds),
        "-reset_timestamps",
        "1",
        "-strftime",
        "0",
        str(output_path),
    ]


@dataclass(frozen=True, slots=True)
class QueuedChunk:
    """Locally durable capture metadata; content remains on the device path."""

    chunk_id: str
    source_id: str
    local_path: Path
    sha256_hex: str
    started_at: datetime
    ended_at: datetime
    byte_count: int
    attempts: int


class ChunkUploader(Protocol):
    """A network adapter that uploads a manifest/hash or policy-approved audio."""

    def upload(self, chunk: QueuedChunk) -> str:
        """Return a durable remote receipt ID or raise a transient exception."""


class EdgeSpool:
    """SQLite-backed outbox that survives power and cellular outages.

    A device has one writer/worker process in the reference deployment. SQLite
    WAL mode still permits a status/UI reader while capture chunks are queued.
    """

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS capture_outbox (
                    chunk_id TEXT PRIMARY KEY,
                    source_id TEXT NOT NULL,
                    local_path TEXT NOT NULL,
                    sha256_hex TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    ended_at TEXT NOT NULL,
                    byte_count INTEGER NOT NULL CHECK (byte_count >= 0),
                    status TEXT NOT NULL CHECK (status IN ('pending', 'uploading', 'uploaded', 'retry')),
                    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
                    next_attempt_at TEXT NOT NULL,
                    remote_receipt TEXT,
                    last_error TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS capture_outbox_due_idx "
                "ON capture_outbox(status, next_attempt_at, created_at)"
            )

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def enqueue_file(
        self,
        source_id: str,
        local_path: str | Path,
        started_at: datetime,
        ended_at: datetime,
        *,
        chunk_id: str | None = None,
    ) -> QueuedChunk:
        """Hash and enqueue a fully closed capture file exactly once."""

        if ended_at <= started_at:
            raise ValueError("chunk end time must be after its start time")
        path = Path(local_path)
        if not path.is_file():
            raise FileNotFoundError(path)
        digest = self._sha256_file(path)
        byte_count = path.stat().st_size
        now = datetime.now(UTC)
        queued = QueuedChunk(
            chunk_id=chunk_id or str(uuid4()),
            source_id=source_id,
            local_path=path,
            sha256_hex=digest,
            started_at=self._as_utc(started_at),
            ended_at=self._as_utc(ended_at),
            byte_count=byte_count,
            attempts=0,
        )
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO capture_outbox (
                    chunk_id, source_id, local_path, sha256_hex, started_at, ended_at,
                    byte_count, status, attempts, next_attempt_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?)
                """,
                (
                    queued.chunk_id,
                    queued.source_id,
                    str(queued.local_path),
                    queued.sha256_hex,
                    queued.started_at.isoformat(),
                    queued.ended_at.isoformat(),
                    queued.byte_count,
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
        return queued

    def claim_due(self, *, now: datetime | None = None) -> QueuedChunk | None:
        """Atomically claim one pending/retry chunk for a single uploader worker."""

        current_time = self._as_utc(now or datetime.now(UTC))
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM capture_outbox
                WHERE status IN ('pending', 'retry') AND next_attempt_at <= ?
                ORDER BY created_at, chunk_id
                LIMIT 1
                """,
                (current_time.isoformat(),),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                """
                UPDATE capture_outbox
                SET status = 'uploading', attempts = attempts + 1, last_error = NULL
                WHERE chunk_id = ?
                """,
                (row["chunk_id"],),
            )
            return self._row_to_chunk(row, attempts=int(row["attempts"]) + 1)

    def mark_uploaded(self, chunk_id: str, remote_receipt: str) -> None:
        """Persist provider/server acknowledgement before deleting local audio."""

        if not remote_receipt.strip():
            raise ValueError("remote_receipt is required")
        with self._connection() as connection:
            updated = connection.execute(
                """
                UPDATE capture_outbox
                SET status = 'uploaded', remote_receipt = ?, last_error = NULL
                WHERE chunk_id = ? AND status = 'uploading'
                """,
                (remote_receipt, chunk_id),
            ).rowcount
            if updated != 1:
                raise KeyError(f"no uploading chunk found for {chunk_id}")

    def mark_retry(
        self,
        chunk_id: str,
        error: str,
        *,
        retry_after: timedelta | None = None,
        now: datetime | None = None,
    ) -> None:
        """Release a failed claim with exponential backoff capped at six hours."""

        current_time = self._as_utc(now or datetime.now(UTC))
        with self._connection() as connection:
            row = connection.execute(
                "SELECT attempts FROM capture_outbox WHERE chunk_id = ? AND status = 'uploading'",
                (chunk_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"no uploading chunk found for {chunk_id}")
            delay = retry_after or timedelta(seconds=min(6 * 60 * 60, 30 * (2 ** max(0, row["attempts"] - 1))))
            connection.execute(
                """
                UPDATE capture_outbox
                SET status = 'retry', next_attempt_at = ?, last_error = ?
                WHERE chunk_id = ?
                """,
                ((current_time + delay).isoformat(), error[:1_000], chunk_id),
            )

    def queued_bytes(self) -> int:
        """Return bytes not yet durably acknowledged by the server."""

        with self._connection() as connection:
            row = connection.execute(
                "SELECT COALESCE(SUM(byte_count), 0) AS total FROM capture_outbox WHERE status != 'uploaded'"
            ).fetchone()
        return int(row["total"])

    def pending_count(self) -> int:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS total FROM capture_outbox WHERE status IN ('pending', 'retry')"
            ).fetchone()
        return int(row["total"])

    @staticmethod
    def _sha256_file(path: Path) -> str:
        digest = sha256()
        with path.open("rb") as file_handle:
            for block in iter(lambda: file_handle.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware")
        return value.astimezone(UTC)

    @staticmethod
    def _row_to_chunk(row: sqlite3.Row, *, attempts: int | None = None) -> QueuedChunk:
        return QueuedChunk(
            chunk_id=str(row["chunk_id"]),
            source_id=str(row["source_id"]),
            local_path=Path(str(row["local_path"])),
            sha256_hex=str(row["sha256_hex"]),
            started_at=datetime.fromisoformat(str(row["started_at"])),
            ended_at=datetime.fromisoformat(str(row["ended_at"])),
            byte_count=int(row["byte_count"]),
            attempts=int(row["attempts"] if attempts is None else attempts),
        )


def flush_one(spool: EdgeSpool, uploader: ChunkUploader) -> bool:
    """Attempt one queued upload and preserve a retry record on failure."""

    chunk = spool.claim_due()
    if chunk is None:
        return False
    try:
        receipt = uploader.upload(chunk)
        if not receipt.strip():
            raise RuntimeError("uploader returned an empty receipt")
    except Exception as error:  # noqa: BLE001 - approved adapters may raise provider-specific transient errors.
        spool.mark_retry(chunk.chunk_id, f"{type(error).__name__}: {error}")
        return False
    spool.mark_uploaded(chunk.chunk_id, receipt)
    return True


def redact_capture_command(command: Sequence[str]) -> list[str]:
    """Return a log-safe representation that hides the FFmpeg input URL."""

    safe = list(command)
    try:
        input_index = safe.index("-i")
    except ValueError:
        return safe
    if input_index + 1 < len(safe):
        safe[input_index + 1] = "<redacted-input-url>"
    return safe
