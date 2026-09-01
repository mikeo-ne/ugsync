# Reviewer/dispute dashboard backend

**Status:** pilot increment 4. The dashboard is the **portal plane** — a
separate trust boundary from the catalog and ingestion APIs. In production it
sits behind Supabase Auth with per-catalog membership roles (migration 002);
the reference server uses a seeded token directory exercising the identical
role checks. It exposes only redacted detection evidence — never raw audio
keys, wallet references, or party PII.

## Roles and separation of duties

| Role | May read queue | May verify/reject | May open dispute | May resolve dispute |
| --- | --- | --- | --- | --- |
| `viewer` (creator/rightsholder) | ✔ | ✘ | ✔ | ✘ |
| `catalog_editor` | ✔ | ✘ | ✔ | ✘ |
| `reviewer` | ✔ | ✔ | ✔ | ✘ |
| `finance_reviewer` | ✔ | ✘ | ✔ | ✔ |
| `catalog_admin` | ✔ | ✔ | ✔ | ✔ |

The person who first triages a detection cannot also adjudicate a dispute about
it: dispute **resolution** is restricted to `finance_reviewer` / `catalog_admin`,
enforcing separation of duties. In production the membership roles come from the
Supabase `catalog_members` table; migration 005 adds a dedicated `reviewer`
enum value where that type exists.

## Detection lifecycle

```text
candidate ──reviewer decision──▶ verified
candidate ──reviewer decision──▶ rejected
candidate | verified ──dispute──▶ disputed
disputed ──finance upholds─────▶ rejected   (amount stays held)
disputed ──finance dismisses────▶ verified   (amount may proceed)
```

- A **candidate** is matcher evidence; only a reviewer/admin decision or an
  approved policy can move it to `verified`.
- Opening a dispute moves the detection to `disputed`. The disputed royalty
  amount is **held** — never silently redistributed — matching the creator
  promise in the partnership paper.
- At most one **open** dispute exists per detection (enforced by a partial
  unique index in migration 005).
- Every transition is written to `audit_events` in the same transaction.

## Endpoints

| Method & path | Role | Purpose |
| --- | --- | --- |
| `GET /v1/review/detections` | any | paged queue; filter `status`, `source`, `recording`, `limit`, `offset` |
| `GET /v1/review/detections/<uuid>` | any | redacted evidence for one detection |
| `POST /v1/review/detections/<uuid>/decision` | reviewer/admin | `{decision: "verified"\|"rejected", note}` (note ≥ 10 chars) |
| `POST /v1/review/detections/<uuid>/disputes` | any | `{reason, detail}`; only from candidate/verified |
| `GET /v1/review/disputes?status=` | any | list disputes |
| `POST /v1/review/disputes/<uuid>/resolve` | finance/admin | `{resolution: "upheld"\|"dismissed", note}` |

Dispute reasons: `wrong_identity`, `wrong_duration`, `wrong_source`,
`wrong_split`, `duplicate`, `other`.

## Evidence shown (redacted)

The detection view contains matcher/schema metadata and review state only:

```json
{
  "id": "…", "capture_chunk_id": "…",
  "source": {"id": "…", "code": "kampala-radio-01"},
  "recording_id": "…", "duration_seconds": 30.0,
  "evidence": {
    "matcher_version": "kla-landmark-ratio-v1:…",
    "matched_hash_count": 42, "match_confidence": 0.61,
    "tempo_scale": 1.0, "offset_seconds": 0.0
  },
  "status": "candidate", "review_note": null,
  "dispute": {"open": false, "status": null}
}
```

It deliberately omits `encrypted_object_key`, wallet/ciphertext columns,
stream secret references, and personal data — consistent with the RLS baseline
that fails raw capture, payment, and PII tables closed to browser clients.

## Running the reference server

```bash
kla-sync review-api --seed-demo --port 8082
```

This prints demo bearer tokens for each role and seeds a small fictional
queue (no real data). Point the dashboard frontend at this JSON backend.
Production deploys the same WSGI app with `PostgresReviewStore` behind the
Supabase-Auth-verifying BFF over TLS.

## Components

| Component | Module |
| --- | --- |
| Role model + lifecycle rules | `src/kla_sync/review/service.py` |
| Redacted evidence / dispute models | `src/kla_sync/review/models.py` |
| Stores (in-memory + PostgreSQL) | `src/kla_sync/review/store.py` |
| Portal token auth (dev) | `src/kla_sync/review/auth.py` |
| WSGI backend | `src/kla_sync/review/http.py` |
| Disputes table + reviewer role | `migrations/005_detection_disputes.sql` |
