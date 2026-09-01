"""Shared test fixtures/factories for catalog tests."""

from __future__ import annotations


def valid_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "catalog_name": "Kampala Pilot Catalog",
        "owner_local_id": "org-label",
        "organizations": [
            {
                "local_id": "org-label",
                "legal_name": "Kampala Record Label Ltd",
                "organization_type": "label",
                "registration_number": "80020001234567",
                "contact_email": "catalog@example.org",
            }
        ],
        "parties": [
            {"local_id": "p-producer", "party_kind": "individual", "legal_name": "Producer Namara"},
            {
                "local_id": "p-artist",
                "party_kind": "individual",
                "legal_name": "Artist Ssali",
                "stage_or_trading_name": "Ssali",
            },
            {
                "local_id": "p-label",
                "party_kind": "organization",
                "legal_name": "Kampala Record Label Ltd",
                "organization_local_id": "org-label",
            },
        ],
        "works": [
            {
                "local_id": "w-1",
                "title": "Obulungi Buno",
                "iswc": "t-012.345.678-9",
                "language_code": "lug",
                "contributors": [
                    {"party_local_id": "p-artist", "contributor_role": "composer"},
                ],
            }
        ],
        "releases": [
            {"local_id": "r-1", "title": "Obulungi Buno", "release_type": "single"}
        ],
        "recordings": [
            {
                "local_id": "rec-1",
                "title": "Obulungi Buno (radio mix)",
                "duration_seconds": 213.5,
                "isrc": "ugxyz2400001",
                "audio_sha256": "a" * 64,
                "artist_credits": [
                    {
                        "party_local_id": "p-artist",
                        "artist_role": "primary",
                        "stage_name": "Ssali",
                        "display_order": 1,
                    },
                    {
                        "party_local_id": "p-producer",
                        "artist_role": "producer",
                        "stage_name": "Namara Beats",
                        "display_order": 2,
                    },
                ],
                "work_local_ids": ["w-1"],
                "release_local_ids": ["r-1"],
                "release_track_numbers": {"r-1": 1},
            }
        ],
        "split_sheets": [
            {
                "right_type": "master",
                "asset_local_id": "rec-1",
                "source_document_key": "pilot/splits/rec-1-signed.pdf",
                "lines": [
                    {"party_local_id": "p-producer", "role": "producer", "share_basis_points": 5000},
                    {"party_local_id": "p-artist", "role": "performer", "share_basis_points": 3000},
                    {"party_local_id": "p-label", "role": "label", "share_basis_points": 2000},
                ],
            }
        ],
    }
    payload.update(overrides)
    return payload
