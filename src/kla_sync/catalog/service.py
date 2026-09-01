"""Catalog onboarding service.

The service is storage-agnostic (it talks to a :class:`~kla_sync.catalog.store.
CatalogStore`) so the same validated onboarding flow runs against PostgreSQL in
production and the in-memory store in tests. Every onboarding batch is one
transaction: a duplicate ISRC, a bad split, or a failed insert rolls the whole
batch back.

Activating a split sheet enforces the 10,000-basis-points rule *before* the
database trigger, records the approver, and supersede the prior active sheet for
the same asset/right — mirroring the split-sheet lifecycle in the data model.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from .errors import CatalogConflictError, CatalogStateError, CatalogValidationError
from .models import (
    OnboardingDocument,
    SplitSheetDocument,
    ValidationProblem,
    parse_onboarding_document,
)
from .store import AuditEvent, CatalogContext, CatalogStore

SPLIT_TOTAL_BASIS_POINTS = 10_000


@dataclass(frozen=True, slots=True)
class OnboardingResult:
    catalog_id: str
    organization_ids: dict[str, str]
    party_ids: dict[str, str]
    artist_ids: dict[str, str]
    work_ids: dict[str, str]
    release_ids: dict[str, str]
    recording_ids: dict[str, str]
    split_sheet_ids: dict[str, str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "catalog_id": self.catalog_id,
            "organizations": self.organization_ids,
            "parties": self.party_ids,
            "artists": self.artist_ids,
            "works": self.work_ids,
            "releases": self.release_ids,
            "recordings": self.recording_ids,
            "split_sheets": self.split_sheet_ids,
        }


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


class CatalogService:
    """Application service for catalog onboarding and split-sheet lifecycle."""

    def __init__(self, store: CatalogStore) -> None:
        self._store = store

    # ------------------------------------------------------------------
    # Onboarding
    # ------------------------------------------------------------------

    def onboard(
        self,
        payload: object,
        *,
        idempotency_key: str | None = None,
        actor_id: str | None = None,
        request_id: str | None = None,
    ) -> OnboardingResult:
        """Validate ``payload`` and persist the whole batch atomically."""

        try:
            document = parse_onboarding_document(payload)
        except ValidationProblem as problem:
            raise CatalogValidationError(problem.errors) from problem

        if idempotency_key:
            _validate_uuid(idempotency_key, "Idempotency-Key")

        with self._store.transaction() as tx:
            if idempotency_key:
                existing = self._store.begin_onboarding(tx, idempotency_key)
                if existing is not None:
                    return self._result_from_response(existing.get("response", existing))

            context = self._persist(document, tx, actor_id=actor_id, request_id=request_id)
            result = self._build_result(context)

            self._store.write_audit(
                tx,
                AuditEvent(
                    action="catalog.onboarded",
                    entity_type="catalog",
                    entity_id=result.catalog_id,
                    metadata={
                        "organizations": len(document.organizations),
                        "parties": len(document.parties),
                        "recordings": len(document.recordings),
                        "works": len(document.works),
                        "releases": len(document.releases),
                        "split_sheets_draft": len(document.split_sheets),
                    },
                    actor_id=actor_id,
                    catalog_id=result.catalog_id,
                    request_id=request_id,
                ),
            )

            if idempotency_key:
                self._store.finish_onboarding(tx, idempotency_key, result.as_dict())

        return result

    def _persist(
        self,
        document: OnboardingDocument,
        tx: Any,
        *,
        actor_id: str | None,
        request_id: str | None,
    ) -> CatalogContext:
        # 1. Owner organization, then any other organizations.
        organization_ids: dict[str, str] = {}
        owner_pk: str | None = None
        for org in document.organizations:
            org_id = self._store.create_organization(
                tx,
                legal_name=org.legal_name,
                organization_type=org.organization_type,
                trading_name=org.trading_name,
                registration_number=org.registration_number,
                country_code=org.country_code,
                contact_email=org.contact_email,
            )
            organization_ids[org.local_id] = org_id
            if org.local_id == document.owner_local_id:
                owner_pk = org_id
        if owner_pk is None:  # validated already, defensive
            raise CatalogValidationError("owner_local_id did not resolve to an organization")

        catalog_id = self._store.create_catalog(
            tx, owner_org_id=owner_pk, name=document.catalog_name,
            actor_id=actor_id, request_id=request_id,
        )

        # 2. Rights parties (individuals and organizations).
        party_ids: dict[str, str] = {}
        for party in document.parties:
            party_ids[party.local_id] = self._store.create_party(
                tx,
                party_kind=party.party_kind,
                legal_name=party.legal_name,
                organization_id=(
                    organization_ids[party.organization_local_id]
                    if party.organization_local_id
                    else None
                ),
                stage_or_trading_name=party.stage_or_trading_name,
            )

        # 3. Works and releases.
        work_ids = {
            work.local_id: self._store.create_work(
                tx,
                catalog_id=catalog_id,
                title=work.title,
                alternate_titles=work.alternate_titles,
                iswc=work.iswc,
                language_code=work.language_code,
            )
            for work in document.works
        }
        release_ids = {
            release.local_id: self._store.create_release(
                tx,
                catalog_id=catalog_id,
                title=release.title,
                release_type=release.release_type,
                upc_ean=release.upc_ean,
            )
            for release in document.releases
        }

        # 4. Recordings, artist credits, work and release links.
        artist_pk_to_id: dict[str, str] = {}
        recording_ids: dict[str, str] = {}
        for recording in document.recordings:
            recording_id = self._store.create_recording(
                tx,
                catalog_id=catalog_id,
                title=recording.title,
                isrc=recording.isrc,
                duration_seconds=Decimal(str(recording.duration_seconds)),
                explicit=recording.explicit,
                audio_sha256=recording.audio_sha256,
                fingerprint_schema_id=recording.fingerprint_schema_id,
            )
            recording_ids[recording.local_id] = recording_id

            for credit in recording.artist_credits:
                party_pk = party_ids[credit.party_local_id]
                if party_pk not in artist_pk_to_id:
                    artist_pk_to_id[party_pk] = self._store.create_artist(
                        tx,
                        party_id=party_pk,
                        stage_name=credit.stage_name,
                        country_code=credit.country_code,
                    )
                self._store.link_recording_artist(
                    tx,
                    recording_id=recording_id,
                    artist_id=artist_pk_to_id[party_pk],
                    artist_role=credit.artist_role,
                    display_order=credit.display_order,
                )

            for index, work_ref in enumerate(recording.work_local_ids):
                self._store.link_recording_work(
                    tx,
                    recording_id=recording_id,
                    work_id=work_ids[work_ref],
                    is_primary=(index == 0),
                )
            for release_ref in recording.release_local_ids:
                self._store.link_release_recording(
                    tx,
                    release_id=release_ids[release_ref],
                    recording_id=recording_id,
                    track_number=recording.release_track_numbers.get(release_ref, 1),
                )

        # 5. Work contributors.
        for work in document.works:
            for contributor in work.contributors:
                self._store.link_work_contributor(
                    tx,
                    work_id=work_ids[work.local_id],
                    party_id=party_ids[contributor.party_local_id],
                    contributor_role=contributor.contributor_role,
                    display_order=contributor.display_order,
                )

        # 6. Draft split sheets (activation is a separate, governed step).
        split_sheet_ids: dict[tuple[str, str], str] = {}
        for sheet in document.split_sheets:
            recording_pk = (
                recording_ids.get(sheet.asset_local_id) if sheet.right_type == "master" else None
            )
            work_pk = work_ids.get(sheet.asset_local_id) if sheet.right_type != "master" else None
            sheet_id = self._store.create_split_sheet(
                tx,
                catalog_id=catalog_id,
                recording_id=recording_pk,
                work_id=work_pk,
                right_type=sheet.right_type,
                version=1,
                status="draft",
                source_document_key=sheet.source_document_key,
            )
            for line in sheet.lines:
                self._store.add_split_line(
                    tx,
                    split_sheet_id=sheet_id,
                    party_id=party_ids[line.party_local_id],
                    role=line.role,
                    share_basis_points=line.share_basis_points,
                )
            split_sheet_ids[(sheet.right_type, sheet.asset_local_id)] = sheet_id

        return CatalogContext(
            catalog_id=catalog_id,
            organization_ids=organization_ids,
            party_ids=party_ids,
            work_ids=work_ids,
            release_ids=release_ids,
            recording_ids=recording_ids,
            split_sheet_ids=split_sheet_ids,
        )

    @staticmethod
    def _build_result(context: CatalogContext) -> OnboardingResult:
        artist_ids: dict[str, str] = {}  # exposed per credit party in a later increment
        return OnboardingResult(
            catalog_id=context.catalog_id,
            organization_ids=dict(context.organization_ids),
            party_ids=dict(context.party_ids),
            artist_ids=artist_ids,
            work_ids=dict(context.work_ids),
            release_ids=dict(context.release_ids),
            recording_ids=dict(context.recording_ids),
            split_sheet_ids={
                f"{right_type}:{asset_local_id}": sheet_id
                for (right_type, asset_local_id), sheet_id in context.split_sheet_ids.items()
            },
        )

    @staticmethod
    def _result_from_response(response: Any) -> OnboardingResult:
        if not isinstance(response, dict) or "catalog_id" not in response:
            raise CatalogConflictError("stored onboarding response is malformed")
        return OnboardingResult(
            catalog_id=response["catalog_id"],
            organization_ids=response.get("organizations", {}),
            party_ids=response.get("parties", {}),
            artist_ids=response.get("artists", {}),
            work_ids=response.get("works", {}),
            release_ids=response.get("releases", {}),
            recording_ids=response.get("recordings", {}),
            split_sheet_ids=response.get("split_sheets", {}),
        )

    # ------------------------------------------------------------------
    # Split sheet lifecycle
    # ------------------------------------------------------------------

    def validate_split_sheet(self, sheet: SplitSheetDocument, *, party_ids: set[str]) -> list[str]:
        """Pre-persist check used by both onboarding and the activation step."""

        errors: list[str] = []
        total = sum(line.share_basis_points for line in sheet.lines)
        if total != SPLIT_TOTAL_BASIS_POINTS:
            errors.append(
                f"{sheet.right_type} sheet for '{sheet.asset_local_id}' totals {total} basis "
                f"points; an active sheet must total exactly {SPLIT_TOTAL_BASIS_POINTS}"
            )
        for line in sheet.lines:
            if line.party_local_id not in party_ids:
                errors.append(f"split line party '{line.party_local_id}' is not onboarded")
        return errors

    def activate_split_sheet(
        self,
        split_sheet_id: str,
        *,
        approver_party_id: str,
        actor_id: str | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Approve a draft split sheet, enforcing the 10,000-bp rule and audit."""

        _validate_uuid(split_sheet_id, "split_sheet_id")
        _validate_uuid(approver_party_id, "approver_party_id")

        with self._store.transaction() as tx:
            sheet = self._store.get_split_sheet(tx, split_sheet_id)
            if sheet.get("status") != "draft":
                raise CatalogStateError(
                    f"split sheet {split_sheet_id} is '{sheet.get('status')}'; only drafts activate"
                )
            total = int(sheet.get("total_basis_points", 0))
            if total != SPLIT_TOTAL_BASIS_POINTS:
                raise CatalogStateError(
                    f"split sheet {split_sheet_id} totals {total} basis points; activation "
                    f"requires exactly {SPLIT_TOTAL_BASIS_POINTS} (100%)"
                )
            approved_at = _now_iso()
            self._store.activate_split_sheet(
                tx,
                split_sheet_id=split_sheet_id,
                approver_party_id=approver_party_id,
                approved_at=approved_at,
            )
            self._store.write_audit(
                tx,
                AuditEvent(
                    action="split_sheet.activated",
                    entity_type="split_sheet",
                    entity_id=split_sheet_id,
                    metadata={
                        "right_type": sheet.get("right_type"),
                        "version": sheet.get("version"),
                        "total_basis_points": total,
                        "line_count": len(sheet.get("lines", ())),
                    },
                    actor_id=actor_id or approver_party_id,
                    catalog_id=sheet.get("catalog_id"),
                    request_id=request_id,
                ),
            )
            refreshed = self._store.get_split_sheet(tx, split_sheet_id)
        return refreshed

    def get_split_sheet(self, split_sheet_id: str) -> dict[str, Any]:
        _validate_uuid(split_sheet_id, "split_sheet_id")
        with self._store.transaction() as tx:
            return self._store.get_split_sheet(tx, split_sheet_id)


def _validate_uuid(value: str, field_name: str) -> None:
    try:
        UUID(str(value))
    except (ValueError, AttributeError) as error:
        raise CatalogValidationError(f"{field_name}: must be a UUID") from error
