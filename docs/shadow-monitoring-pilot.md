# Radio shadow-monitoring pilot (increment 5)

The shadow-monitoring pilot runs the capture → fingerprint → match → review
pipeline (increments 3–4) against a **small, agreed set of radio and online
streams** and produces a **weekly, non-financial** reconciliation report.
Nothing in this increment moves money, changes tariffs, or alters splits. It
measures whether the detection pipeline is accurate enough — station by
station — to ever be trusted in a later, separately approved settlement flow.

## What the pilot is for

- Agree **4–8 sources** across the four anchor regions (Kampala, Jinja,
  Mbarara, Gulu) with the stations/platforms concerned.
- Capture fingerprints (hashes only; no raw audio on the public edge plane)
  and run them through the fingerprint index from increment 3.
- Feed candidate detections through the reviewer/dispute dashboard from
  increment 4; reviewer decisions are the ground-truth labels for scoring.
- Cross-check candidates against each station's own playlist log.
- Produce a weekly steering-group report with **per-source** accuracy
  scorecards, reconciliation exceptions, and a list of **unmatched local
  repertoire** for catalog outreach.

Threshold changes during the pilot are **calibration**, not approval gates.
A candidate detection remains evidence (`status = candidate`) and never
becomes payable without human/governance review, an active split, a governed
tariff, and finance approval.

## Inputs

All inputs are plain JSON files for the weekly offline run; the service also
accepts a live matcher callable when wired to the increment-3 index.

| Input | Shape | Notes |
| --- | --- | --- |
| Sources | JSON list of `{source_code, display_name, region, source_class, agreed, notes}` | Regions: `Kampala`, `Jinja`, `Mbarara`, `Gulu`; classes: `fm_stream`, `online_stream`. Holds **no stream URLs or credentials**. `load_pilot_sources(None)` returns an illustrative template. |
| Chunks | JSON list of chunk manifests | Same schema as the ingestion API (`edge_chunk_id`, `source_code`, `started_at`, `ended_at`, `content_sha256`, `byte_count`, `fingerprint_schema_id`, `landmarks`, ...). Invalid chunks are warned and skipped. |
| Candidates (optional) | JSON map `edge_chunk_id -> [{recording_id, votes, confidence_hint, tempo_scale, title, artist}]` | Offline replay of matcher output. In live runs a `matcher` callable produces these from each manifest. |
| Station logs | JSON map `source_code -> [{aired_at, title, artist, recording_id?, isrc?}]` | The station's own playlist log. |
| Reviews (optional) | JSON map `edge_chunk_id -> verified|rejected|candidate|disputed` | Reviewer decisions from increment 4; drive precision. |
| Catalog (optional) | JSON map `recording_id -> {title, artist, isrc}` | Used for identity matching and for classifying log gaps as known vs. unknown. |

## Reconciliation rules

- A candidate is **confirmed by the station log** when a log entry matches by
  `recording_id`, then ISRC (case-insensitive), then a normalized
  title+artist key, and the play time is within **±90 seconds**
  (`DEFAULT_TOLERANCE_SECONDS`). Otherwise it is `candidate_not_in_log`.
- Log entries with no corresponding candidate are split into:
  - **Possible false negatives** — the entry identifies a recording **already
    in the catalog** (recording id, ISRC, or normalized title+artist) that the
    matcher missed. These feed recall improvement.
  - **Unmatched local repertoire** — the entry is **not in the catalog** (e.g.
  an unsigned/local artist). These are **catalog outreach gaps, not false
    negatives**: the matcher cannot match a recording that does not exist in
    the index. They are listed for the catalog team to onboard rightsholders.

## Scoring (per source, never blended)

Each source gets its own `SourceScorecard`:

- **Precision** = verified / (verified + rejected), computed **only from
  reviewed candidates**. Unreviewed candidates are excluded from the
  denominator; with zero reviews precision is `null`/`n/a`, never 0%.
- **Log agreement rate** = confirmed candidates / total candidates for that
  source (`null` when the source had no candidates).
- **Possible false negatives** and **local repertoire gaps** counts.
- Slices break reviewed precision down by **confidence band**
  (high ≥ 0.6, medium ≥ 0.35, low), **tempo** (unshifted |Δ| < 0.025,
  sped-up, slowed-down), and **region**.

Scores are reported per source so a weak station cannot hide behind a blended
average, and no source-level number is presented as a system-wide guarantee.

## The report

- `report_id`: `shadow-YYYYMMDD-<hex8>`, `report_kind =
  radio_shadow_reconciliation`, `non_financial = true`.
- Sections: per-source scorecards, review outcomes
  (verified / rejected / candidate / disputed), unmatched local repertoire
  outreach table, station-log reconciliation exceptions, and the candidate
  play list (evidence, not payable).
- The Markdown rendering is built for the steering group and is asserted to
  contain **no** payout, wallet, tariff, or split terms; the JSON payload
  round-trips for archival.
- Warnings are collected for: chunks that fail manifest validation, and
  chunks referencing sources that are **not in the agreed pilot set** — those
  chunks are excluded entirely.

## Running the weekly report

```bash
kla-sync shadow-report \
  --period-start 2026-08-24T00:00:00Z \
  --period-end   2026-08-31T00:00:00Z \
  --sources       pilot-sources.json \
  --chunks        week-chunks.json \
  --candidates    week-candidates.json \
  --station-logs  week-logs.json \
  --reviews       week-reviews.json \
  --catalog       catalog-snapshot.json \
  --out-json      shadow-report.json \
  --out-markdown  shadow-report.md
```

Omit `--sources` to use the illustrative template, and `--candidates` when a
live matcher is wired in code via `ShadowMonitoringService.generate_report(...,
matcher=...)`.

## Boundaries

- **No money movement, tariffs, or splits** appear anywhere in the report.
- **No raw audio and no stream credentials** — sources carry only codes and
  display names; chunks are fingerprint manifests under the `hashes_only`
  capture policy.
- **Non-agreed sources are excluded**, not silently scored.
- **Unmatched local works are outreach items**, never counted as matcher
  failures.
- Threshold tweaks during the pilot are calibration only; production
  settlement remains a separate, explicitly approved increment.
