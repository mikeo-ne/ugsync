# Fingerprint index service and authenticated ingestion API

**Status:** pilot service increment 3. This document covers the matcher-side
landmark index service and the authenticated API that edge nodes use to upload
chunk manifests. Both are server-side services — they are not part of the
public GitHub Pages site and never accept raw audio over the public edge plane.

## Components

| Component | Module |
| --- | --- |
| Landmark index service (enroll/query) | `src/kla_sync/matching/service.py` |
| Inverted-index store (in-memory + Redis) | `src/kla_sync/matching/store.py` |
| Edge device HMAC authentication | `src/kla_sync/ingestion_api/device_auth.py` |
| Chunk manifest validation | `src/kla_sync/ingestion_api/manifests.py` |
| Ingestion service (receipt + match hand-off) | `src/kla_sync/ingestion_api/service.py` |
| Receipt stores (in-memory + PostgreSQL) | `src/kla_sync/ingestion_api/stores.py` |
| WSGI HTTP API | `src/kla_sync/ingestion_api/http.py` |

## Fingerprint index service

`LandmarkIndexService` wraps the same relative-landmark contract as the
reference `InMemoryFingerprintIndex` but reads through a `LandmarkIndexStore`
so the voting logic runs unchanged over either backend:

- **`InMemoryLandmarkStore`** — backed by the reference inverted index; used by
  workers, tests, and single-process pilots.
- **`RedisLandmarkStore`** — production hot shard (redis client injected; part
  of the `production` extra). Matching lookups are exact-key `SMEMBERS` on
  `kla:fp:{schema_id}:{ratio_bin}:{delta}` — never an unbounded scan. A
  per-track set (`kla:fp:trackkeys:{schema}:{track}`) records which buckets
  hold a track's occurrences so a replacement or removal deletes precisely, and
  a hash stores each track's registered landmark count for coverage.

All keys are partitioned by the fingerprint `schema_id`, so different
`FingerprintConfig` versions never mix. The service rejects enrollment or
queries under a different schema.

`vote_candidates()` performs the tempo-aware scaled-offset consensus across
the 0.90–1.10 timing scales and returns explainable `MatchCandidate`s with vote
count, query/track coverage, scale, offset, and an **uncalibrated**
`confidence_hint`. These are candidate detections, never payout approvals.

## Edge device authentication

Each edge node holds a random per-device secret (provisioned by the deployment
secret manager; only the device id is public). A request is signed by an
HMAC-SHA256 over a fixed canonical string:

```text
v1
<device_id>
<ISO-8601 timestamp>
<request path>
<SHA-256 of the exact request body>
```

Headers: `X-Device-Id`, `X-Timestamp`, `X-Signature: v1:<hex>`.

- The timestamp must be within a 5-minute replay window.
- Each `(device, timestamp, body)` signature is single-use within the window.
- The signature binds the body hash, so any tampering invalidates it.
- Comparison uses `hmac.compare_digest`; failures return a generic 401 without
  revealing which factor failed.

Edge workers build these headers with
`build_signed_request(device=..., method=..., path=..., body=...)`.

## Chunk manifests

The uploaded unit is a compact manifest — **no raw audio**:

```json
{
  "edge_chunk_id": "f0c1...uuid",
  "source_code": "kampala-radio-01",
  "started_at": "2026-09-01T10:00:00+03:00",
  "ended_at": "2026-09-01T10:00:30+03:00",
  "content_sha256": "…64 hex…",
  "byte_count": 12345,
  "fingerprint_schema_id": "kla-landmark-ratio-v1:…",
  "capture_policy": "hashes_only",
  "landmarks": [
    {"anchor_frame": 10, "frequency_ratio_bin": 3, "delta_frames": 4}
  ]
}
```

Validation: UUID chunk id, ordered timestamps, 64-hex content hash, schema id,
`capture_policy` (`hashes_only` forbids an object key; `encrypted_audio`
requires one), and integer landmarks (`anchor_frame >= 0`,
`delta_frames >= 1`; `frequency_ratio_bin` may be negative). All field errors
are returned at once.

## Endpoints

| Method & path | Auth plane | Purpose |
| --- | --- | --- |
| `GET /healthz` | none | liveness + schema id + indexed track count |
| `POST /v1/ingest/chunks` | device HMAC | verify manifest, record a receipt, run matching |
| `POST /v1/index/tracks/<id>/landmarks` | bearer token | enroll/replace a catalog recording's fingerprints |

### Ingest flow

1. Verify the device signature and freshness/replay.
2. Validate the manifest; reject unknown `source_code` (422).
3. If the chunk's schema matches the index, run candidate matching and persist
   `detection_events` with status **`candidate`** for the review queue.
4. Record an append-only `capture.ingested` audit event.
5. Return a `receipt_id`, landmark count, and candidate summary.

`edge_chunk_id` is the idempotency key: a retried upload (new signature, same
chunk id) returns `status: "replayed"` with the original receipt.

A schema mismatch is accepted for receipt but skips matching
(`schema_compatible: false`) — an edge worker on an older/different
`FingerprintConfig` must not produce cross-schema matches.

## Running the reference server

```bash
# Demo, in-memory, with a seeded demo device/source:
KLA_SYNC_CATALOG_API_TOKEN=... \
  kla-sync ingest-api --memory --source-code kampala-radio-01 \
                      --device-id edge-demo-01
```

Production wires the same WSGI app to `PostgresIngestionStore` and
`RedisLandmarkStore`, provisions devices from the secret manager, and runs it
behind gunicorn/gunicorn-style WSGI hosting over TLS. The reference server is
explicitly in-memory.

## Security boundaries

- **No raw audio on the edge plane.** The default policy is `hashes_only`;
  encrypted audio requires a policy-approved object key handled out-of-band.
- **Two trust planes.** Devices use HMAC; the catalog/index plane uses the
  server bearer token. Browser clients never call either.
- **Candidate, not verdict.** Persisted detections start as `candidate` and
  only reach settlement through the reviewer/dispute workflow and royalty
  gates.
- **Schema isolation.** A `schema_id` mismatch never matches.
