"""Small diagnostic CLI for the KLA-Sync reference components."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from .audio.fingerprint import FingerprintExtractor
from .audio.segmentation import DJMixSegmenter, MatchWindow
from .audio.wav import read_wav_mono
from .ingestion.edge import (
    CaptureSource,
    FFmpegCaptureConfig,
    build_ffmpeg_capture_command,
    redact_capture_command,
)
from .royalties.calculator import RoyaltyCalculator, SplitRecipient, UsageForRoyalty


def _json_default(value: object) -> object:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    raise TypeError(f"cannot serialize {type(value).__name__}")


def _print_json(payload: object) -> None:
    print(json.dumps(payload, default=_json_default, indent=2, sort_keys=True))


def _fingerprint_command(args: argparse.Namespace) -> int:
    extractor = FingerprintExtractor()
    audio = read_wav_mono(args.wav, target_rate=extractor.config.target_sample_rate)
    fingerprint = extractor.extract(audio)
    _print_json(
        {
            "schema_id": fingerprint.schema_id,
            "duration_seconds": round(fingerprint.duration_seconds, 3),
            "peak_count": len(fingerprint.peaks),
            "landmark_hash_count": len(fingerprint.hashes),
        }
    )
    return 0


def _segment_command(args: argparse.Namespace) -> int:
    evidence = json.loads(Path(args.evidence_json).read_text(encoding="utf-8"))
    if not isinstance(evidence, list):
        raise TypeError("evidence JSON must be an array of match windows")
    windows = tuple(
        MatchWindow(
            track_id=str(item["track_id"]),
            started_at_seconds=float(item["started_at_seconds"]),
            ended_at_seconds=float(item["ended_at_seconds"]),
            confidence=float(item["confidence"]),
        )
        for item in evidence
    )
    audio = read_wav_mono(args.wav)
    segmentation = DJMixSegmenter().segment_audio(audio.samples, audio.sample_rate, windows)
    _print_json(asdict(segmentation))
    return 0


def _parse_split(raw: str, position: int) -> SplitRecipient:
    try:
        party_id, role, share = raw.rsplit(":", 2)
    except ValueError as error:
        raise argparse.ArgumentTypeError("splits must use PARTY_ID:ROLE:BASIS_POINTS") from error
    return SplitRecipient(
        split_line_id=f"cli-{position}",
        party_id=party_id,
        role=role,
        share_basis_points=int(share),
    )


def _payout_command(args: argparse.Namespace) -> int:
    recipients = tuple(_parse_split(raw, index) for index, raw in enumerate(args.split, start=1))
    usage = UsageForRoyalty(
        usage_event_id=args.usage_id,
        right_type=args.right_type,
        base_rate_ugx=Decimal(args.base_rate_ugx),
        venue_or_station_weight=Decimal(args.weight),
        detected_duration_seconds=Decimal(args.detected_seconds),
        reference_duration_seconds=Decimal(args.reference_seconds),
    )
    calculation = RoyaltyCalculator().calculate(usage, recipients)
    _print_json(asdict(calculation))
    return 0


def _edge_command(args: argparse.Namespace) -> int:
    command = build_ffmpeg_capture_command(
        CaptureSource(args.source_id, args.input_url, args.display_name),
        FFmpegCaptureConfig(Path(args.output_directory), segment_seconds=args.segment_seconds),
    )
    _print_json({"command": redact_capture_command(command)})
    return 0


def _migrate_command(args: argparse.Namespace) -> int:
    import os

    from .db.migrations import MigrationError, connect, run_migrations

    dsn = args.database_url or os.environ.get("DATABASE_URL")
    if not dsn:
        raise SystemExit("Set DATABASE_URL or pass --database-url")
    enabled = tuple(name.strip() for name in (args.require or "").split(",") if name.strip())
    try:
        connection = connect(dsn)
        plan = run_migrations(
            connection,
            enabled_requirements=enabled,
            dry_run=args.dry_run,
        )
    except MigrationError as error:
        raise SystemExit(f"migration error: {error}") from error

    for item in plan.skipped:
        print(f"skip   {item.migration.filename}  ({item.reason})")
    for item in plan.pending:
        state = "would apply" if args.dry_run else "applied   "
        print(f"{state} {item.migration.filename}")
    for filename in plan.applied:
        print(f"ok     {filename}")
    if args.dry_run:
        print("dry run: no changes made")
    return 0


def _catalog_api_command(args: argparse.Namespace) -> int:
    import os
    from wsgiref.simple_server import WSGIRequestHandler, make_server

    from .http_api.wsgi import build_default_app

    if args.print_dev_token:
        from .http_api.auth import generate_token

        print(generate_token())
        return 0

    if args.memory and not os.environ.get("KLA_SYNC_CATALOG_API_TOKEN"):
        os.environ["KLA_SYNC_CATALOG_API_TOKEN"] = args.dev_token or ""
    if args.dev_token:
        os.environ["KLA_SYNC_CATALOG_API_TOKEN"] = args.dev_token

    app, _token = build_default_app(require_token=not args.memory)

    class _QuietHandler(WSGIRequestHandler):
        def log_message(self, fmt: str, *arguments: object) -> None:
            sys.stderr.write(f"[catalog-api] {self.address_string()} {fmt % arguments}\n")

    host, port = args.host, args.port
    server = make_server(host, port, app, handler_class=_QuietHandler)
    print(f"[catalog-api] listening on http://{host}:{port} (memory={args.memory})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[catalog-api] shutting down")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="KLA-Sync reference worker diagnostics")
    subparsers = parser.add_subparsers(dest="command", required=True)

    fingerprint = subparsers.add_parser("fingerprint", help="extract landmark count from PCM WAV")
    fingerprint.add_argument("wav", help="uncompressed PCM WAV path")
    fingerprint.set_defaults(handler=_fingerprint_command)

    segment = subparsers.add_parser("segment", help="fuse WAV novelty and matcher evidence")
    segment.add_argument("wav", help="uncompressed PCM WAV path")
    segment.add_argument("evidence_json", help="JSON array of matcher evidence windows")
    segment.set_defaults(handler=_segment_command)

    payout = subparsers.add_parser("payout", help="calculate an auditable royalty allocation")
    payout.add_argument("--usage-id", default="diagnostic-usage")
    payout.add_argument("--right-type", default="performance")
    payout.add_argument("--base-rate-ugx", required=True)
    payout.add_argument("--weight", required=True)
    payout.add_argument("--detected-seconds", required=True)
    payout.add_argument("--reference-seconds", required=True)
    payout.add_argument(
        "--split",
        action="append",
        required=True,
        metavar="PARTY_ID:ROLE:BASIS_POINTS",
        help="repeat until the split totals 10,000 basis points",
    )
    payout.set_defaults(handler=_payout_command)

    edge = subparsers.add_parser("edge-command", help="render a redacted FFmpeg capture command")
    edge.add_argument("--source-id", required=True)
    edge.add_argument("--display-name", required=True)
    edge.add_argument("--input-url", required=True)
    edge.add_argument("--output-directory", default="captures")
    edge.add_argument("--segment-seconds", default=30, type=int)
    edge.set_defaults(handler=_edge_command)

    migrate = subparsers.add_parser("migrate", help="apply PostgreSQL migrations with checksum ledger")
    migrate.add_argument("--database-url", help="PostgreSQL DSN (defaults to $DATABASE_URL)")
    migrate.add_argument(
        "--require",
        default="",
        help="comma-separated environment requirements to enable (e.g. 'supabase')",
    )
    migrate.add_argument("--dry-run", action="store_true", help="plan only; do not apply")
    migrate.set_defaults(handler=_migrate_command)

    catalog_api = subparsers.add_parser("catalog-api", help="run the catalog onboarding HTTP API")
    catalog_api.add_argument("--host", default="0.0.0.0")
    catalog_api.add_argument("--port", type=int, default=8080)
    catalog_api.add_argument(
        "--memory",
        action="store_true",
        help="use the in-memory store (local demo; data is not persisted)",
    )
    catalog_api.add_argument(
        "--dev-token",
        help="bearer token for local use (or set KLA_SYNC_CATALOG_API_TOKEN)",
    )
    catalog_api.add_argument(
        "--print-dev-token",
        action="store_true",
        help="print a strong generated bearer token and exit",
    )
    catalog_api.set_defaults(handler=_catalog_api_command)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
