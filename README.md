# KLA-Sync

KLA-Sync is an offline-tolerant foundation for music monitoring, robust audio
fingerprinting, DJ-mix attribution, split-sheet management, and auditable
royalty calculation for the Ugandan music ecosystem.

It is designed around difficult real-world conditions: pitch/tempo-adjusted DJ
sets, live voiceovers and ambient noise, intermittent cellular connectivity at
venues/taxis, fragmented recording/composition rights, and wallet-based
settlement. The repository starts with executable reference components and a
production architecture—not a claim that any external registry, CMO, MTN, or
Airtel integration is already approved or live.

## What is included

| Area | Deliverable |
| --- | --- |
| Robust fingerprinting | Pure-Python FFT fallback plus optional NumPy acceleration; relative log-frequency landmark hashes; ±10% timing-scale candidate search and offset voting |
| Fingerprint index service | Store-backed matcher (`InMemoryLandmarkStore` for pilots, Redis hot-shard adapter for production) with schema-id isolation and the same tempo-aware voting as the reference index |
| Authenticated ingestion API | Edge devices authenticate per request with HMAC signatures; chunk manifests are verified, receipted idempotently, matched to candidate detections, and audited |
| DJ-mix segmentation | Acoustic novelty features plus repeated match-window fusion; preserves beat-match/crossfade overlap instead of inventing hard cuts |
| Offline edge ingestion | Shell-safe FFmpeg command builder and SQLite-backed capture outbox with hash, retry, and acknowledgement handling |
| Rights and finance | PostgreSQL/Supabase schema for ISRC/ISWC, recordings, works, contributors, versioned 10,000-bp splits, detections, rates, allocations, payment accounts, payouts, and audit events |
| Royalty engine | Deterministic `Decimal` formula and largest-remainder whole-UGX rounding that preserves totals |
| Integration safety | Explicit URSB/registry and mobile-money adapter contracts; no guessed live API endpoints or stored raw wallet numbers |
| Catalog onboarding API | Authenticated server-side WSGI API for validated catalog/split onboarding; one transaction per batch, idempotency keys, audit events, and draft split activation |
| PostgreSQL migration runner | Ordered SQL migrations with an applied-migrations ledger, SHA-256 checksum-drift detection, and environment-gated Supabase files |
| Architecture & business | Mermaid diagrams, data-flow design, bounded autonomous-worker prompts, and Uganda pilot/partnership concept paper |

## Repository layout

```text
src/kla_sync/
  audio/          WAV I/O, DSP, fingerprinting, DJ-mix segmentation
  ingestion/      Offline edge capture and SQLite spool
  royalties/      Split validation and royalty calculation
  integrations/   Safe registry and wallet-provider contracts
  catalog/        Validated onboarding documents, stores, and service
  db/             PostgreSQL migration runner with checksum ledger
  http_api/       Authenticated catalog onboarding WSGI API
  matching/       Fingerprint index service + in-memory/Redis stores
  ingestion_api/  Device HMAC auth, chunk manifests, ingestion WSGI API
  review/         Reviewer/dispute portal: roles, decisions, disputes
  shadow/         Radio shadow-monitoring pilot: weekly non-financial reconciliation
  cli.py          Diagnostic + operations CLI
migrations/
  001_core_schema.sql       Portable PostgreSQL core model
  002_supabase_rls.sql      Supabase Auth/RLS baseline (requires --require supabase)
  003_integrity_guards.sql  Cross-table financial and evidence checks
  004_onboarding_api.sql    Onboarding idempotency ledger
  005_detection_disputes.sql Reviewer disputes + reviewer role
 docs/
  architecture.md
  autonomous-service-prompts.md
  uganda-go-to-market-and-partnership.md
tests/            Dependency-free unit tests
```

## Quick start

The reference package has no mandatory third-party Python dependency. It runs on
Python 3.11+; install the `production` extra on worker hosts for NumPy and
service adapters.

```bash
cd /home/user/ugsync
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[dev,production]'
ruff check src tests scripts
python scripts/validate_migrations.py
python -m unittest discover -s tests -v
```

For a dependency-free local check without creating a virtual environment:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

### Inspect a PCM WAV fingerprint

Capture/convert source audio to uncompressed mono-compatible PCM WAV with
FFmpeg, then run the diagnostic CLI:

```bash
kla-sync fingerprint path/to/capture.wav
# or: PYTHONPATH=src python3 -m kla_sync.cli fingerprint path/to/capture.wav
```

The result includes a schema ID, duration, peak count, and landmark count. Keep
the schema ID unchanged across catalog enrollment and query workers.

### Segment a DJ mix using matcher evidence

Create evidence from your matching service:

```json
[
  {
    "track_id": "recording-uuid-a",
    "started_at_seconds": 0,
    "ended_at_seconds": 30,
    "confidence": 0.88
  },
  {
    "track_id": "recording-uuid-b",
    "started_at_seconds": 26,
    "ended_at_seconds": 58,
    "confidence": 0.82
  }
]
```

Then fuse it with acoustic novelty features:

```bash
kla-sync segment path/to/mix.wav evidence.json
```

Overlaps in the output are intentional evidence of a crossfade/beat-match.
Apply an agreed policy before turning those seconds into royalty amounts.

### Calculate a royalty allocation

```bash
kla-sync payout \
  --base-rate-ugx 1000 --weight 1.5 \
  --detected-seconds 90 --reference-seconds 180 \
  --split producer:producer:5000 \
  --split artist:performer:3000 \
  --split label:label:2000
```

The formula is:

```text
Gross royalty = base rate × venue/station weight × detected duration / reference duration
Allocation     = gross royalty × split share / 10,000
```

The calculator rejects split totals other than exactly 10,000 basis points and
uses whole-UGX largest-remainder rounding so recipient totals equal gross.

### Render a safe edge-capture command

```bash
kla-sync edge-command \
  --source-id kampala-radio-01 \
  --display-name "Kampala radio listener" \
  --input-url "https://stream.example/live"
```

The diagnostic output redacts the stream URL. Run the actual list-form command
through a supervised process manager (for example, `systemd`); do not use
`sh -c` or log credential-bearing URLs.

## Database migrations

Review migrations with your DBA/security lead before applying them. The core
migration uses PostgreSQL extensions `pgcrypto`, `citext`, and `btree_gist`.

Apply them with the built-in migration runner, which records each applied file
and its SHA-256 checksum in a `kla_schema_migrations` ledger, detects drift, and
gates the Supabase-specific RLS file:

```bash
# Plan only:
kla-sync migrate --dry-run
# Apply to $DATABASE_URL:
kla-sync migrate
# In a Supabase project (enables migration 002):
kla-sync migrate --require supabase
```

The runner wraps each migration in a transaction with its ledger row, so a
failed migration rolls back cleanly and stays re-runnable. Direct `psql -f`
application still works if you prefer to manage the ledger manually.

```bash
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/001_core_schema.sql
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/003_integrity_guards.sql
# Only in a Supabase project after the core migration and auth bootstrap:
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/002_supabase_rls.sql
```

## Catalog onboarding API

A server-side, bearer-token authenticated API enrolls catalog metadata and draft
split sheets in one transaction, with idempotency keys and audit events. It is
not part of the public website and carries no payment data.

```bash
# Generate a service token and run locally against PostgreSQL:
export KLA_SYNC_CATALOG_API_TOKEN="$(kla-sync catalog-api --print-dev-token)"
kla-sync migrate
kla-sync catalog-api --port 8080        # serves via the stdlib WSGI server

# Zero-dependency in-memory demo (not persisted):
kla-sync catalog-api --memory --dev-token dev-token
```

See [`docs/catalog-onboarding-api.md`](docs/catalog-onboarding-api.md) for the
endpoint contract, validation rules, and example payload.

`002_supabase_rls.sql` intentionally fails closed for browser access to raw
captures, detections, party PII, payment accounts, and payouts. Expose only
reviewed/redacted views after a threat-model review; backend workers use a
restricted server-side role.

## Important operational boundaries

- **Candidate match ≠ payable usage.** Human/review policy, dispute status,
  active split, tariff/source-weight version, and finance approval gate payout.
- **No raw wallet data in code or logs.** Store encrypted provider references,
  HMACs, and key IDs; use a secret manager for credentials.
- **No presumed partner API.** `integrations/` contains provider-neutral
  contracts and a sandbox gateway. Implement live URSB/CMO/telecom adapters only
  against an approved agreement and current official specification.
- **Reference algorithm ≠ production capacity.** The in-memory index is
  deterministic for validation. Use partitioned Redis/Elasticsearch/OpenSearch
  index workers for catalog-scale matching and benchmark actual hardware.
- **Measure locally.** Calibrate all candidate/review thresholds with labelled
  Ugandan recordings across stations, venues, genres, languages, pitch shifts,
  tempo shifts, chatter, and noise levels.

## Documentation

- [Technical architecture and data flows](docs/architecture.md)
- [Catalog, split-sheet, and payout database model](docs/database-model.md)
- [Catalog onboarding API and migration runner](docs/catalog-onboarding-api.md)
- [Fingerprint index service and authenticated ingestion API](docs/ingestion-and-fingerprint-index.md)
- [Reviewer/dispute dashboard backend](docs/reviewer-dispute-dashboard.md)
- [Radio shadow-monitoring pilot](docs/shadow-monitoring-pilot.md)
- [Autonomous microservice system prompts](docs/autonomous-service-prompts.md)
- [Uganda go-to-market and URSB/CMO partnership concept](docs/uganda-go-to-market-and-partnership.md)

## Suggested next implementation increments

1. Provision a controlled pilot catalog and labelled evaluation corpus.
2. Implement authenticated ingestion API + Redis/Elasticsearch index adapter
   using the `LandmarkHash` contract.
3. Add a review portal with Supabase Auth, role separation, evidence playback
   controls, and dispute workflow.
4. ~~Run shadow reports, publish source-specific accuracy metrics~~ — delivered
   as the [shadow-monitoring pilot](docs/shadow-monitoring-pilot.md): weekly
   non-financial per-source scorecards, station-log reconciliation, and local
   repertoire outreach lists.
5. Complete CMO/registry data-sharing agreements and telecom sandbox
   certification before enabling production synchronization or payouts.
6. Conduct a dry-run settlement only after explicit partner approval; no live
   money movement before then.

## Test status

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

The included tests cover fingerprint crop, 5% pitch-tempo, and moderate
voiceover-like tonal-noise candidate matching; segmentation overlap handling;
split arithmetic/rounding; WAV decoding; sandbox payout idempotency; and offline
spool retry semantics.
