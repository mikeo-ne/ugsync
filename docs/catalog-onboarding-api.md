# Catalog onboarding API and migration runner

**Status:** pilot service increment 2. This is a server-side, authenticated
HTTP API for enrolling catalog metadata (organizations, parties, works,
recordings, releases, and draft split sheets). It is **not** part of the public
GitHub Pages site: it has no browser UI, no user login, and no payment data.

## What it provides

| Capability | Where |
| --- | --- |
| PostgreSQL migration runner with a checksum ledger | `src/kla_sync/db/migrations.py` |
| Validated onboarding document model | `src/kla_sync/catalog/models.py` |
| Onboarding service (one transaction per batch) | `src/kla_sync/catalog/service.py` |
| PostgreSQL and in-memory stores | `src/kla_sync/catalog/store.py` |
| Bearer-token authenticated WSGI API | `src/kla_sync/http_api/` |

## Migration runner

Migrations are the ordered `migrations/*.sql` files. Each is applied exactly
once, in order, inside a transaction that also records its SHA-256 checksum in
the `kla_schema_migrations` ledger table.

```bash
# Plan only — shows pending/skipped/applied, changes nothing.
kla-sync migrate --dry-run

# Apply to the configured database.
kla-sync migrate

# Targeting Supabase (migration 002 requires the Supabase auth schema).
kla-sync migrate --require supabase
```

Safety properties:

- **Checksum drift fails loud.** A recorded migration whose file changed
  content is reported as an error and blocks all further migrations — never
  silently re-run.
- **Environment-gated files.** Migration `002_supabase_rls.sql` declares
  `-- @requires supabase` and is skipped on plain PostgreSQL. A pending
  migration *after* a skipped one is an error, so the chain never silently
  splits.
- **Atomic per migration.** The ledger row is spliced into the migration's
  transaction; a failed migration rolls back both schema and ledger and stays
  re-runnable.

## Running the API

```bash
pip install -e '.[production]'   # adds psycopg; stdlib WSGI needs nothing else

# Apply migrations first:
kla-sync migrate

# Then serve (production uses a real WSGI server, e.g. gunicorn):
export KLA_SYNC_CATALOG_API_TOKEN="$(kla-sync catalog-api --print-dev-token)"
gunicorn \
  --module kla_sync.cli \
  --bind 0.0.0.0:8080 \
  # see "Serving" below for the WSGI app factory
```

For a zero-dependency local demo with an in-memory store (data is **not**
persisted and a warning is printed):

```bash
kla-sync catalog-api --memory --dev-token dev-token
```

## Authentication

Every endpoint except `GET /healthz` requires:

```
Authorization: Bearer <KLA_SYNC_CATALOG_API_TOKEN>
```

Tokens are compared in constant time, are never logged, and are server-to-server
credentials provisioned via the deployment secret manager. The reviewer/dispute
dashboard (a later increment) uses Supabase Auth with per-user roles instead.

## Endpoints

### `GET /healthz` — unauthenticated liveness

```json
{ "status": "ok", "service": "kla-sync-catalog-api", "version": "0.1.0" }
```

### `POST /v1/catalog/onboard` — validate and persist a batch

One transaction: organizations → parties → works → releases → recordings →
artist/work/release links → contributors → **draft** split sheets. A duplicate
ISRC/ISWC/UPC/registration number, a bad split, or a failed insert rolls back
the entire batch.

Headers:

- `Authorization: Bearer …`
- `Content-Type: application/json`
- `Idempotency-Key: <uuid>` — replays return the original result
- `X-Request-ID: <uuid>` — echoed into audit events (optional)
- `X-Actor-ID: <uuid>` — acting user/worker (optional)

Request body (see the worked example below):

| Field | Rule |
| --- | --- |
| `catalog_name` | non-empty; unique per owner organization |
| `owner_local_id` | must reference an `organizations[].local_id` |
| `organizations[]` | `legal_name`, `organization_type` in the schema enum, optional `registration_number`, `contact_email` |
| `parties[]` | `party_kind` `individual`/`organization`; organizations must link `organization_local_id`; individuals must not |
| `works[]` | `title`, optional `iswc` (normalized to `T-xxx.xxx.xxx-x`), `language_code` ISO-639-3, `contributors[]` |
| `releases[]` | `title`, `release_type`, optional 8–14 digit `upc_ean` |
| `recordings[]` | `title`, `duration_seconds` > 0, optional `isrc` (normalized), `audio_sha256` (64 hex), `artist_credits[]`, `work_local_ids[]`, `release_local_ids[]` |
| `split_sheets[]` | `right_type` + `asset_local_id`; `master` sheets reference a recording, others a work; `lines[]` of `{party_local_id, role, share_basis_points}` |

Validation notes:

- **ISRC/ISWC syntax is checked, never ownership.** Rights ownership remains an
  approved catalog/CMO workflow.
- Split lines use integer basis points. Drafts may be incomplete (they cannot
  pay anything until activated); lines over 10,000 total are rejected.
- All field errors are returned at once with HTTP 422.

### `GET /v1/split-sheets/<uuid>` — fetch a sheet with line total

Returns status, version, approver, lines, and `total_basis_points`.

### `POST /v1/split-sheets/<uuid>/activate` — approve a draft

Body: `{ "approver_party_id": "<uuid>" }`. Activation:

1. requires the sheet to be a `draft`;
2. enforces the exact **10,000 basis points** total before the database trigger
   does;
3. marks any prior active sheet for the same asset/right as `superseded`;
4. records the approver, timestamp, and an append-only audit event.

## Error format

```json
{ "error": { "code": "validation_failed", "message": "...", "details": ["work.iswc: ..."] } }
```

| HTTP | When |
| --- | --- |
| 401 | missing/malformed/incorrect bearer token |
| 404 | unknown route or split sheet |
| 409 | duplicate identifier or sheet not in a draft state |
| 413/415 | body over 1 MiB / wrong content type |
| 422 | field or cross-reference validation failure |

## Example payload

```json
{
  "catalog_name": "Kampala Pilot Catalog",
  "owner_local_id": "org-label",
  "organizations": [
    { "local_id": "org-label", "legal_name": "Kampala Record Label Ltd",
      "organization_type": "label", "registration_number": "80020001234567" }
  ],
  "parties": [
    { "local_id": "p-producer", "party_kind": "individual", "legal_name": "Producer Namara" },
    { "local_id": "p-artist", "party_kind": "individual", "legal_name": "Artist Ssali",
      "stage_or_trading_name": "Ssali" },
    { "local_id": "p-label", "party_kind": "organization",
      "legal_name": "Kampala Record Label Ltd", "organization_local_id": "org-label" }
  ],
  "works": [
    { "local_id": "w-1", "title": "Obulungi Buno", "iswc": "T-012.345.678-9",
      "language_code": "lug",
      "contributors": [{ "party_local_id": "p-artist", "contributor_role": "composer" }] }
  ],
  "releases": [{ "local_id": "r-1", "title": "Obulungi Buno", "release_type": "single" }],
  "recordings": [
    { "local_id": "rec-1", "title": "Obulungi Buno (radio mix)", "duration_seconds": 213.5,
      "isrc": "UGXYZ2400001", "audio_sha256": "aaaa…(64 hex)",
      "artist_credits": [
        { "party_local_id": "p-artist", "artist_role": "primary", "stage_name": "Ssali" },
        { "party_local_id": "p-producer", "artist_role": "producer", "stage_name": "Namara Beats" }
      ],
      "work_local_ids": ["w-1"], "release_local_ids": ["r-1"],
      "release_track_numbers": { "r-1": 1 } }
  ],
  "split_sheets": [
    { "right_type": "master", "asset_local_id": "rec-1",
      "lines": [
        { "party_local_id": "p-producer", "role": "producer", "share_basis_points": 5000 },
        { "party_local_id": "p-artist", "role": "performer", "share_basis_points": 3000 },
        { "party_local_id": "p-label", "role": "label", "share_basis_points": 2000 }
      ] }
  ]
}
```

## Serving under a production WSGI server

The app factory reads `DATABASE_URL` (PostgreSQL via psycopg) and
`KLA_SYNC_CATALOG_API_TOKEN` from the environment:

```python
# gunicorn 'kla_sync.http_api.wsgi:app'
from kla_sync.http_api.wsgi import build_default_app
app, _token = build_default_app()
```

## Security and privacy boundaries

- No wallet numbers, payment data, credentials, or raw audio are accepted or
  stored by this API.
- The request body is capped at 1 MiB; responses set `Cache-Control: no-store`.
- Audit events are written for catalog onboarding and split activation with
  actor, request id, and non-sensitive metadata.
- The `onboarding_requests` table (migration 004) backs idempotency and is never
  exposed to browser clients.
