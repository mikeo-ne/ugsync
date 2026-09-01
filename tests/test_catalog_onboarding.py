from __future__ import annotations

import unittest

from kla_sync.catalog import (
    CatalogConflictError,
    CatalogService,
    CatalogStateError,
    CatalogValidationError,
)
from kla_sync.catalog.models import parse_onboarding_document
from kla_sync.catalog.store import InMemoryCatalogStore
from tests._factories import valid_payload as _valid_payload


class OnboardingValidationTests(unittest.TestCase):
    def test_valid_payload_parses_and_normalizes_identifiers(self) -> None:
        doc = parse_onboarding_document(_valid_payload())
        self.assertEqual(doc.catalog_name, "Kampala Pilot Catalog")
        self.assertEqual(doc.works[0].iswc, "T-012.345.678-9")
        self.assertEqual(doc.recordings[0].isrc, "UGXYZ2400001")
        self.assertEqual(doc.recordings[0].duration_seconds, 213.5)

    def test_collects_multiple_field_errors(self) -> None:
        payload = _valid_payload(catalog_name="", owner_local_id="missing-org")
        with self.assertRaises(CatalogValidationError) as ctx:
            CatalogService(InMemoryCatalogStore()).onboard(payload)
        joined = " | ".join(ctx.exception.errors)
        self.assertIn("catalog_name", joined)
        self.assertIn("owner_local_id", joined)

    def test_bad_isrc_and_iswc_are_rejected(self) -> None:
        payload = _valid_payload()
        payload["recordings"][0]["isrc"] = "not-an-isrc"  # type: ignore[index]
        payload["works"][0]["iswc"] = "xyz"  # type: ignore[index]
        with self.assertRaises(CatalogValidationError) as ctx:
            CatalogService(InMemoryCatalogStore()).onboard(payload)
        joined = " ".join(ctx.exception.errors)
        self.assertIn("isrc", joined)
        self.assertIn("iswc", joined)

    def test_split_sheet_exceeding_100_percent_is_rejected(self) -> None:
        payload = _valid_payload()
        payload["split_sheets"][0]["lines"] = [  # type: ignore[index]
            {"party_local_id": "p-producer", "role": "producer", "share_basis_points": 9000},
            {"party_local_id": "p-label", "role": "label", "share_basis_points": 2000},
        ]
        with self.assertRaises(CatalogValidationError) as ctx:
            CatalogService(InMemoryCatalogStore()).onboard(payload)
        self.assertTrue(any("10000" in error for error in ctx.exception.errors))

    def test_master_sheet_must_reference_a_recording(self) -> None:
        payload = _valid_payload()
        payload["split_sheets"][0]["asset_local_id"] = "w-1"  # type: ignore[index]
        with self.assertRaises(CatalogValidationError) as ctx:
            CatalogService(InMemoryCatalogStore()).onboard(payload)
        self.assertTrue(any("recording" in error for error in ctx.exception.errors))

    def test_composition_sheet_references_a_work(self) -> None:
        payload = _valid_payload()
        payload["split_sheets"].append(  # type: ignore[union-attr]
            {
                "right_type": "composition",
                "asset_local_id": "w-1",
                "lines": [
                    {"party_local_id": "p-artist", "role": "writer", "share_basis_points": 10000}
                ],
            }
        )
        doc = parse_onboarding_document(payload)
        self.assertEqual(len(doc.split_sheets), 2)

    def test_unknown_party_reference_is_rejected(self) -> None:
        payload = _valid_payload()
        payload["split_sheets"][0]["lines"][0]["party_local_id"] = "p-ghost"  # type: ignore[index]
        with self.assertRaises(CatalogValidationError):
            CatalogService(InMemoryCatalogStore()).onboard(payload)

    def test_individual_party_cannot_link_organization(self) -> None:
        payload = _valid_payload()
        payload["parties"][0]["organization_local_id"] = "org-label"  # type: ignore[index]
        with self.assertRaises(CatalogValidationError) as ctx:
            CatalogService(InMemoryCatalogStore()).onboard(payload)
        self.assertTrue(any("individual" in error for error in ctx.exception.errors))


class OnboardingServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemoryCatalogStore()
        self.service = CatalogService(self.store)

    def test_onboard_persists_entities_and_draft_split_sheet(self) -> None:
        result = self.service.onboard(_valid_payload())
        self.assertTrue(result.catalog_id)
        self.assertEqual(len(result.organization_ids), 1)
        self.assertEqual(len(result.party_ids), 3)
        self.assertEqual(len(result.recording_ids), 1)
        self.assertEqual(len(result.split_sheet_ids), 1)
        # Sheet is created as draft, not active.
        sheet_id = next(iter(result.split_sheet_ids.values()))
        sheet = self.service.get_split_sheet(sheet_id)
        self.assertEqual(sheet["status"], "draft")
        self.assertEqual(sheet["total_basis_points"], 10000)
        self.assertEqual(len(sheet["lines"]), 3)

    def test_duplicate_isrc_conflicts(self) -> None:
        self.service.onboard(_valid_payload())
        with self.assertRaises(CatalogConflictError):
            self.service.onboard(_valid_payload(catalog_name="Second Catalog"))

    def test_idempotent_key_returns_same_result(self) -> None:
        first = self.service.onboard(_valid_payload(), idempotency_key="12345678-1234-1234-1234-1234567890ab")
        second = self.service.onboard(_valid_payload(), idempotency_key="12345678-1234-1234-1234-1234567890ab")
        self.assertEqual(first.catalog_id, second.catalog_id)
        self.assertEqual(len(self.store.catalogs), 1)

    def test_failed_batch_rolls_back_entire_transaction(self) -> None:
        payload = _valid_payload()
        # Two recordings sharing one ISRC -> conflict mid-batch.
        payload["recordings"].append(dict(payload["recordings"][0]))  # type: ignore[union-attr]
        payload["recordings"][1]["local_id"] = "rec-2"  # type: ignore[index]
        payload["recordings"][1]["title"] = "Duplicate ISRC track"  # type: ignore[index]
        with self.assertRaises(CatalogConflictError):
            self.service.onboard(payload)
        self.assertEqual(self.store.catalogs, {})
        self.assertEqual(self.store.recordings, {})

    def test_audit_event_written_for_onboarding(self) -> None:
        self.service.onboard(_valid_payload())
        actions = [event["action"] for event in self.store.audit_events]
        self.assertIn("catalog.onboarded", actions)


class SplitActivationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemoryCatalogStore()
        self.service = CatalogService(self.store)
        self.result = self.service.onboard(_valid_payload())

    def _sheet_id(self) -> str:
        return next(iter(self.result.split_sheet_ids.values()))

    def test_activate_draft_sheet(self) -> None:
        sheet_id = self._sheet_id()
        approver = self.result.party_ids["p-label"]
        sheet = self.service.activate_split_sheet(sheet_id, approver_party_id=approver)
        self.assertEqual(sheet["status"], "active")
        self.assertEqual(sheet["approved_by_party_id"], approver)
        actions = [event["action"] for event in self.store.audit_events]
        self.assertIn("split_sheet.activated", actions)

    def test_incomplete_sheet_cannot_activate(self) -> None:
        # Build a draft that totals only 60%.
        payload = _valid_payload(catalog_name="Incomplete Splits")
        payload["organizations"][0]["registration_number"] = "80020009999999"  # type: ignore[index]
        payload["recordings"][0]["isrc"] = "ugaaa2400099"  # type: ignore[index]
        payload["recordings"][0]["audio_sha256"] = "b" * 64  # type: ignore[index]
        payload["recordings"][0]["local_id"] = "rec-9"  # type: ignore[index]
        payload["works"][0]["iswc"] = None  # type: ignore[index]
        payload["releases"][0]["local_id"] = "r-9"  # type: ignore[index]
        payload["releases"][0]["upc_ean"] = None  # type: ignore[index]
        payload["recordings"][0]["release_local_ids"] = ["r-9"]  # type: ignore[index]
        payload["recordings"][0]["release_track_numbers"] = {"r-9": 1}  # type: ignore[index]
        payload["split_sheets"][0]["asset_local_id"] = "rec-9"  # type: ignore[index]
        payload["split_sheets"][0]["lines"] = [  # type: ignore[index]
            {"party_local_id": "p-label", "role": "label", "share_basis_points": 6000}
        ]
        result = self.service.onboard(payload)
        sheet_id = next(iter(result.split_sheet_ids.values()))
        with self.assertRaises(CatalogStateError) as ctx:
            self.service.activate_split_sheet(sheet_id, approver_party_id=result.party_ids["p-label"])
        self.assertIn("10000", str(ctx.exception))

    def test_activating_new_version_supersedes_previous(self) -> None:
        sheet_id = self._sheet_id()
        approver = self.result.party_ids["p-label"]
        self.service.activate_split_sheet(sheet_id, approver_party_id=approver)
        # A corrected replacement sheet (same asset/right) would be version 2;
        # emulate it by activating a fresh draft through the store directly.
        recording_pk = next(iter(self.result.recording_ids.values()))
        catalog_pk = self.result.catalog_id
        with self.store.transaction() as tx:
            new_sheet = self.store.create_split_sheet(
                tx,
                catalog_id=catalog_pk,
                recording_id=recording_pk,
                work_id=None,
                right_type="master",
                version=2,
                status="draft",
                source_document_key=None,
            )
            self.store.add_split_line(
                tx, split_sheet_id=new_sheet, party_id=self.result.party_ids["p-producer"],
                role="producer", share_basis_points=10000,
            )
        self.service.activate_split_sheet(new_sheet, approver_party_id=approver)
        self.assertEqual(self.store.split_sheets[sheet_id]["status"], "superseded")
        self.assertEqual(self.store.split_sheets[new_sheet]["status"], "active")


if __name__ == "__main__":
    unittest.main()
