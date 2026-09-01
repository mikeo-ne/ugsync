"""Persistence boundary for catalog onboarding.

``CatalogStore`` is a Protocol so the service can run against PostgreSQL in
production (``PostgresCatalogStore``, psycopg v3, imported lazily) and against
an in-memory store in tests and local demos. Every mutating method receives an
open transaction context; the service decides commit boundaries.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol
from uuid import uuid4
from zoneinfo import ZoneInfo

from .errors import (
    CatalogConflictError,
    CatalogError,
    CatalogNotFoundError,
    CatalogStateError,
)

KAMPALA = ZoneInfo("Africa/Kampala")


def _today_campala() -> str:
    return datetime.now(tz=KAMPALA).date().isoformat()


@dataclass(frozen=True, slots=True)
class CatalogContext:
    catalog_id: str
    organization_ids: dict[str, str]
    party_ids: dict[str, str]
    work_ids: dict[str, str]
    release_ids: dict[str, str]
    recording_ids: dict[str, str]
    split_sheet_ids: dict[tuple[str, str], str]


@dataclass(frozen=True, slots=True)
class AuditEvent:
    action: str
    entity_type: str
    entity_id: str | None
    metadata: dict[str, Any]
    actor_id: str | None = None
    catalog_id: str | None = None
    request_id: str | None = None


class CatalogStore(Protocol):
    @contextmanager
    def transaction(self) -> Iterator[Any]: ...

    def begin_onboarding(self, tx: Any, idempotency_key: str) -> dict[str, Any] | None: ...

    def finish_onboarding(
        self,
        tx: Any,
        idempotency_key: str,
        result: dict[str, Any],
    ) -> None: ...

    def create_catalog(
        self, tx: Any, *, owner_org_id: str, name: str, actor_id: str | None, request_id: str | None
    ) -> str: ...

    def create_organization(
        self,
        tx: Any,
        *,
        legal_name: str,
        organization_type: str,
        trading_name: str | None,
        registration_number: str | None,
        country_code: str,
        contact_email: str | None,
    ) -> str: ...

    def create_party(
        self,
        tx: Any,
        *,
        party_kind: str,
        legal_name: str,
        organization_id: str | None,
        stage_or_trading_name: str | None,
    ) -> str: ...

    def create_artist(
        self,
        tx: Any,
        *,
        party_id: str,
        stage_name: str,
        country_code: str,
    ) -> str: ...

    def create_work(
        self,
        tx: Any,
        *,
        catalog_id: str,
        title: str,
        alternate_titles: tuple[str, ...],
        iswc: str | None,
        language_code: str,
    ) -> str: ...

    def create_release(
        self,
        tx: Any,
        *,
        catalog_id: str,
        title: str,
        release_type: str,
        upc_ean: str | None,
    ) -> str: ...

    def create_recording(
        self,
        tx: Any,
        *,
        catalog_id: str,
        title: str,
        isrc: str | None,
        duration_seconds: Decimal,
        explicit: bool,
        audio_sha256: str | None,
        fingerprint_schema_id: str | None,
    ) -> str: ...

    def link_recording_artist(
        self,
        tx: Any,
        *,
        recording_id: str,
        artist_id: str,
        artist_role: str,
        display_order: int,
    ) -> None: ...

    def link_recording_work(
        self, tx: Any, *, recording_id: str, work_id: str, is_primary: bool
    ) -> None: ...

    def link_release_recording(
        self, tx: Any, *, release_id: str, recording_id: str, track_number: int
    ) -> None: ...

    def link_work_contributor(
        self,
        tx: Any,
        *,
        work_id: str,
        party_id: str,
        contributor_role: str,
        display_order: int,
    ) -> None: ...

    def create_split_sheet(
        self,
        tx: Any,
        *,
        catalog_id: str,
        recording_id: str | None,
        work_id: str | None,
        right_type: str,
        version: int,
        status: str,
        source_document_key: str | None,
    ) -> str: ...

    def add_split_line(
        self,
        tx: Any,
        *,
        split_sheet_id: str,
        party_id: str,
        role: str,
        share_basis_points: int,
    ) -> str: ...

    def get_split_sheet(self, tx: Any, split_sheet_id: str) -> dict[str, Any]: ...

    def activate_split_sheet(
        self,
        tx: Any,
        *,
        split_sheet_id: str,
        approver_party_id: str,
        approved_at: str,
    ) -> None: ...

    def write_audit(self, tx: Any, event: AuditEvent) -> None: ...


# ---------------------------------------------------------------------------
# In-memory reference store (tests, local demos; same semantics as Postgres)
# ---------------------------------------------------------------------------


class InMemoryCatalogStore:
    """Deterministic, dependency-free store enforcing the same unique rules."""

    def __init__(self) -> None:
        self.organizations: dict[str, dict[str, Any]] = {}
        self.parties: dict[str, dict[str, Any]] = {}
        self.artists: dict[str, dict[str, Any]] = {}
        self.catalogs: dict[str, dict[str, Any]] = {}
        self.works: dict[str, dict[str, Any]] = {}
        self.releases: dict[str, dict[str, Any]] = {}
        self.recordings: dict[str, dict[str, Any]] = {}
        self.recording_artists: list[dict[str, Any]] = []
        self.recording_works: list[dict[str, Any]] = []
        self.release_recordings: list[dict[str, Any]] = []
        self.work_contributors: list[dict[str, Any]] = []
        self.split_sheets: dict[str, dict[str, Any]] = {}
        self.split_lines: dict[str, list[dict[str, Any]]] = {}
        self.audit_events: list[dict[str, Any]] = []
        self.onboardings: dict[str, dict[str, Any]] = {}
        self._snapshot: dict[str, Any] | None = None

    @contextmanager
    def transaction(self) -> Iterator[dict[str, dict[str, Any]]]:
        import copy

        self._snapshot = copy.deepcopy(self.__dict__)
        tx: dict[str, dict[str, Any]] = {}
        try:
            yield tx
        except Exception:
            self.__dict__.update(copy.deepcopy(self._snapshot))
            raise
        finally:
            self._snapshot = None

    def _new_id(self) -> str:
        return str(uuid4())

    def begin_onboarding(self, tx: Any, idempotency_key: str) -> dict[str, Any] | None:
        return self.onboardings.get(idempotency_key)

    def finish_onboarding(self, tx: Any, idempotency_key: str, result: dict[str, Any]) -> None:
        self.onboardings[idempotency_key] = result

    def create_organization(
        self,
        tx: Any,
        *,
        legal_name: str,
        organization_type: str,
        trading_name: str | None,
        registration_number: str | None,
        country_code: str,
        contact_email: str | None,
    ) -> str:
        for org in self.organizations.values():
            if (
                registration_number is not None
                and org["registration_number"] == registration_number
                and org["country_code"] == country_code
            ):
                raise CatalogConflictError(
                    f"organization registration number {registration_number} already onboarded"
                )
        org_id = self._new_id()
        self.organizations[org_id] = {
            "id": org_id,
            "legal_name": legal_name,
            "organization_type": organization_type,
            "trading_name": trading_name,
            "registration_number": registration_number,
            "country_code": country_code,
            "contact_email": contact_email,
        }
        return org_id

    def create_party(
        self,
        tx: Any,
        *,
        party_kind: str,
        legal_name: str,
        organization_id: str | None,
        stage_or_trading_name: str | None,
    ) -> str:
        party_id = self._new_id()
        self.parties[party_id] = {
            "id": party_id,
            "party_kind": party_kind,
            "legal_name": legal_name,
            "organization_id": organization_id,
            "stage_or_trading_name": stage_or_trading_name,
        }
        return party_id

    def create_artist(
        self, tx: Any, *, party_id: str, stage_name: str, country_code: str
    ) -> str:
        for artist in self.artists.values():
            if artist["party_id"] == party_id and artist["stage_name"].lower() == stage_name.lower():
                return artist["id"]  # same party/stage name is idempotent per schema UNIQUE
        artist_id = self._new_id()
        self.artists[artist_id] = {
            "id": artist_id,
            "party_id": party_id,
            "stage_name": stage_name,
            "country_code": country_code,
        }
        return artist_id

    def create_catalog(
        self,
        tx: Any,
        *,
        owner_org_id: str,
        name: str,
        actor_id: str | None,
        request_id: str | None,
    ) -> str:
        for catalog in self.catalogs.values():
            if catalog["owner_organization_id"] == owner_org_id and catalog["name"] == name:
                raise CatalogConflictError(f"catalog '{name}' already exists for this organization")
        catalog_id = self._new_id()
        self.catalogs[catalog_id] = {
            "id": catalog_id,
            "owner_organization_id": owner_org_id,
            "name": name,
            "is_active": True,
        }
        return catalog_id

    def create_work(
        self,
        tx: Any,
        *,
        catalog_id: str,
        title: str,
        alternate_titles: tuple[str, ...],
        iswc: str | None,
        language_code: str,
    ) -> str:
        if iswc and any(w["iswc"] == iswc for w in self.works.values()):
            raise CatalogConflictError(f"ISWC {iswc} is already registered to another work")
        work_id = self._new_id()
        self.works[work_id] = {
            "id": work_id,
            "catalog_id": catalog_id,
            "title": title,
            "alternate_titles": list(alternate_titles),
            "iswc": iswc,
            "language_code": language_code,
        }
        return work_id

    def create_release(
        self,
        tx: Any,
        *,
        catalog_id: str,
        title: str,
        release_type: str,
        upc_ean: str | None,
    ) -> str:
        if upc_ean and any(r["upc_ean"] == upc_ean for r in self.releases.values()):
            raise CatalogConflictError(f"UPC/EAN {upc_ean} is already used by another release")
        release_id = self._new_id()
        self.releases[release_id] = {
            "id": release_id,
            "catalog_id": catalog_id,
            "title": title,
            "release_type": release_type,
            "upc_ean": upc_ean,
        }
        return release_id

    def create_recording(
        self,
        tx: Any,
        *,
        catalog_id: str,
        title: str,
        isrc: str | None,
        duration_seconds: Decimal,
        explicit: bool,
        audio_sha256: str | None,
        fingerprint_schema_id: str | None,
    ) -> str:
        if isrc and any(r["isrc"] == isrc for r in self.recordings.values()):
            raise CatalogConflictError(f"ISRC {isrc} is already registered to another recording")
        if audio_sha256 and any(r["audio_sha256"] == audio_sha256 for r in self.recordings.values()):
            raise CatalogConflictError("an identical audio file is already enrolled")
        recording_id = self._new_id()
        self.recordings[recording_id] = {
            "id": recording_id,
            "catalog_id": catalog_id,
            "title": title,
            "isrc": isrc,
            "duration_seconds": duration_seconds,
            "explicit": explicit,
            "audio_sha256": audio_sha256,
            "fingerprint_schema_id": fingerprint_schema_id,
        }
        return recording_id

    def link_recording_artist(
        self, tx: Any, *, recording_id: str, artist_id: str, artist_role: str, display_order: int
    ) -> None:
        self.recording_artists.append(
            {
                "recording_id": recording_id,
                "artist_id": artist_id,
                "artist_role": artist_role,
                "display_order": display_order,
            }
        )

    def link_recording_work(
        self, tx: Any, *, recording_id: str, work_id: str, is_primary: bool
    ) -> None:
        self.recording_works.append(
            {"recording_id": recording_id, "work_id": work_id, "is_primary_work": is_primary}
        )

    def link_release_recording(
        self, tx: Any, *, release_id: str, recording_id: str, track_number: int
    ) -> None:
        self.release_recordings.append(
            {"release_id": release_id, "recording_id": recording_id, "track_number": track_number}
        )

    def link_work_contributor(
        self, tx: Any, *, work_id: str, party_id: str, contributor_role: str, display_order: int
    ) -> None:
        self.work_contributors.append(
            {
                "work_id": work_id,
                "party_id": party_id,
                "contributor_role": contributor_role,
                "display_order": display_order,
            }
        )

    def _next_split_version(self, tx: Any, *, recording_id: str | None, work_id: str | None, right_type: str) -> int:
        versions = [
            sheet["version"]
            for sheet in self.split_sheets.values()
            if sheet["right_type"] == right_type
            and sheet["recording_id"] == recording_id
            and sheet["work_id"] == work_id
        ]
        return max(versions, default=0) + 1

    def create_split_sheet(
        self,
        tx: Any,
        *,
        catalog_id: str,
        recording_id: str | None,
        work_id: str | None,
        right_type: str,
        version: int,
        status: str,
        source_document_key: str | None,
    ) -> str:
        sheet_id = self._new_id()
        self.split_sheets[sheet_id] = {
            "id": sheet_id,
            "catalog_id": catalog_id,
            "recording_id": recording_id,
            "work_id": work_id,
            "right_type": right_type,
            "version": version,
            "status": status,
            "valid_from": _today_campala(),
            "valid_to": None,
            "source_document_key": source_document_key,
            "approved_at": None,
            "approved_by_party_id": None,
        }
        self.split_lines[sheet_id] = []
        return sheet_id

    def add_split_line(
        self, tx: Any, *, split_sheet_id: str, party_id: str, role: str, share_basis_points: int
    ) -> str:
        line_id = self._new_id()
        self.split_lines[split_sheet_id].append(
            {
                "id": line_id,
                "split_sheet_id": split_sheet_id,
                "party_id": party_id,
                "role": role,
                "share_basis_points": share_basis_points,
            }
        )
        return line_id

    def get_split_sheet(self, tx: Any, split_sheet_id: str) -> dict[str, Any]:
        sheet = self.split_sheets.get(split_sheet_id)
        if sheet is None:
            raise CatalogNotFoundError(f"split sheet {split_sheet_id} not found")
        lines = self.split_lines.get(split_sheet_id, [])
        total = sum(line["share_basis_points"] for line in lines)
        return {**sheet, "lines": list(lines), "total_basis_points": total}

    def activate_split_sheet(
        self,
        tx: Any,
        *,
        split_sheet_id: str,
        approver_party_id: str,
        approved_at: str,
    ) -> None:
        sheet = self.split_sheets.get(split_sheet_id)
        if sheet is None:
            raise CatalogNotFoundError(f"split sheet {split_sheet_id} not found")
        if sheet["status"] != "draft":
            raise CatalogStateError(f"split sheet {split_sheet_id} is {sheet['status']}, not draft")
        total = sum(line["share_basis_points"] for line in self.split_lines.get(split_sheet_id, []))
        if total != 10_000:
            raise CatalogStateError(
                f"split sheet {split_sheet_id} totals {total} basis points; must be exactly 10000"
            )
        # Supersede previous active sheet for the same asset/right (one active at a time).
        for other in self.split_sheets.values():
            if (
                other["id"] != split_sheet_id
                and other["status"] == "active"
                and other["recording_id"] == sheet["recording_id"]
                and other["work_id"] == sheet["work_id"]
                and other["right_type"] == sheet["right_type"]
            ):
                other["status"] = "superseded"
                other["valid_to"] = sheet["valid_from"]
        sheet["status"] = "active"
        sheet["approved_at"] = approved_at
        sheet["approved_by_party_id"] = approver_party_id

    def write_audit(self, tx: Any, event: AuditEvent) -> None:
        self.audit_events.append(
            {
                "id": self._new_id(),
                "catalog_id": event.catalog_id,
                "actor_id": event.actor_id,
                "action": event.action,
                "entity_type": event.entity_type,
                "entity_id": event.entity_id,
                "request_id": event.request_id,
                "metadata": dict(event.metadata),
            }
        )


# ---------------------------------------------------------------------------
# PostgreSQL store
# ---------------------------------------------------------------------------


class PostgresCatalogStore:
    """Catalog store backed by the core PostgreSQL schema (psycopg v3)."""

    def __init__(self, connection: Any) -> None:
        self._conn = connection

    @contextmanager
    def transaction(self) -> Iterator[Any]:
        # psycopg v3: commits on clean exit, rolls back on exception.
        with self._conn.transaction():
            yield self._conn

    def begin_onboarding(self, tx: Any, idempotency_key: str) -> dict[str, Any] | None:
        with self._cursor(tx) as cursor:
            cursor.execute(
                "SELECT response FROM onboarding_requests WHERE idempotency_key = %s",
                (idempotency_key,),
            )
            row = cursor.fetchone()
        return row[0] if row else None

    def finish_onboarding(self, tx: Any, idempotency_key: str, result: dict[str, Any]) -> None:
        from psycopg.types.json import Jsonb

        with self._cursor(tx) as cursor:
            cursor.execute(
                """
                INSERT INTO onboarding_requests (idempotency_key, status, response)
                VALUES (%s, 'completed', %s)
                ON CONFLICT (idempotency_key) DO UPDATE SET response = EXCLUDED.response
                """,
                (idempotency_key, Jsonb(result)),
            )

    def create_organization(
        self,
        tx: Any,
        *,
        legal_name: str,
        organization_type: str,
        trading_name: str | None,
        registration_number: str | None,
        country_code: str,
        contact_email: str | None,
    ) -> str:
        with self._cursor(tx) as cursor:
            try:
                cursor.execute(
                    """
                    INSERT INTO organizations
                        (legal_name, trading_name, organization_type, registration_number,
                         country_code, contact_email)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (
                        legal_name,
                        trading_name,
                        organization_type,
                        registration_number,
                        country_code,
                        contact_email,
                    ),
                )
            except Exception as error:
                raise self._translate(error) from error
            return str(cursor.fetchone()[0])

    def create_party(
        self,
        tx: Any,
        *,
        party_kind: str,
        legal_name: str,
        organization_id: str | None,
        stage_or_trading_name: str | None,
    ) -> str:
        with self._cursor(tx) as cursor:
            cursor.execute(
                """
                INSERT INTO rights_parties
                    (party_kind, organization_id, legal_name, stage_or_trading_name)
                VALUES (%s, %s, %s, %s)
                RETURNING id
                """,
                (party_kind, organization_id, legal_name, stage_or_trading_name),
            )
            return str(cursor.fetchone()[0])

    def create_artist(
        self, tx: Any, *, party_id: str, stage_name: str, country_code: str
    ) -> str:
        with self._cursor(tx) as cursor:
            cursor.execute(
                """
                INSERT INTO artists (party_id, stage_name, country_code)
                VALUES (%s, %s, %s)
                RETURNING id
                """,
                (party_id, stage_name, country_code),
            )
            return str(cursor.fetchone()[0])

    def create_catalog(
        self,
        tx: Any,
        *,
        owner_org_id: str,
        name: str,
        actor_id: str | None,
        request_id: str | None,
    ) -> str:
        with self._cursor(tx) as cursor:
            try:
                cursor.execute(
                    """
                    INSERT INTO catalogs (owner_organization_id, name)
                    VALUES (%s, %s)
                    RETURNING id
                    """,
                    (owner_org_id, name),
                )
            except Exception as error:
                raise self._translate(error) from error
            return str(cursor.fetchone()[0])

    def create_work(
        self,
        tx: Any,
        *,
        catalog_id: str,
        title: str,
        alternate_titles: tuple[str, ...],
        iswc: str | None,
        language_code: str,
    ) -> str:
        with self._cursor(tx) as cursor:
            try:
                cursor.execute(
                    """
                    INSERT INTO music_works (catalog_id, title, alternate_titles, iswc, language_code)
                    VALUES (%s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (catalog_id, title, list(alternate_titles), iswc, language_code),
                )
            except Exception as error:
                raise self._translate(error) from error
            return str(cursor.fetchone()[0])

    def create_release(
        self,
        tx: Any,
        *,
        catalog_id: str,
        title: str,
        release_type: str,
        upc_ean: str | None,
    ) -> str:
        with self._cursor(tx) as cursor:
            try:
                cursor.execute(
                    """
                    INSERT INTO releases (catalog_id, title, release_type, upc_ean)
                    VALUES (%s, %s, %s, %s)
                    RETURNING id
                    """,
                    (catalog_id, title, release_type, upc_ean),
                )
            except Exception as error:
                raise self._translate(error) from error
            return str(cursor.fetchone()[0])

    def create_recording(
        self,
        tx: Any,
        *,
        catalog_id: str,
        title: str,
        isrc: str | None,
        duration_seconds: Decimal,
        explicit: bool,
        audio_sha256: str | None,
        fingerprint_schema_id: str | None,
    ) -> str:
        with self._cursor(tx) as cursor:
            try:
                cursor.execute(
                    """
                    INSERT INTO recordings
                        (catalog_id, title, isrc, duration_seconds, explicit,
                         audio_sha256, fingerprint_schema_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                    """,
                    (catalog_id, title, isrc, duration_seconds, explicit, audio_sha256, fingerprint_schema_id),
                )
            except Exception as error:
                raise self._translate(error) from error
            return str(cursor.fetchone()[0])

    def link_recording_artist(
        self, tx: Any, *, recording_id: str, artist_id: str, artist_role: str, display_order: int
    ) -> None:
        with self._cursor(tx) as cursor:
            cursor.execute(
                """
                INSERT INTO recording_artists (recording_id, artist_id, artist_role, display_order)
                VALUES (%s, %s, %s, %s)
                """,
                (recording_id, artist_id, artist_role, display_order),
            )

    def link_recording_work(
        self, tx: Any, *, recording_id: str, work_id: str, is_primary: bool
    ) -> None:
        with self._cursor(tx) as cursor:
            cursor.execute(
                """
                INSERT INTO recording_works (recording_id, work_id, is_primary_work)
                VALUES (%s, %s, %s)
                """,
                (recording_id, work_id, is_primary),
            )

    def link_release_recording(
        self, tx: Any, *, release_id: str, recording_id: str, track_number: int
    ) -> None:
        with self._cursor(tx) as cursor:
            cursor.execute(
                """
                INSERT INTO release_recordings (release_id, recording_id, track_number)
                VALUES (%s, %s, %s)
                """,
                (release_id, recording_id, track_number),
            )

    def link_work_contributor(
        self, tx: Any, *, work_id: str, party_id: str, contributor_role: str, display_order: int
    ) -> None:
        with self._cursor(tx) as cursor:
            cursor.execute(
                """
                INSERT INTO work_contributors (work_id, party_id, contributor_role, display_order)
                VALUES (%s, %s, %s, %s)
                """,
                (work_id, party_id, contributor_role, display_order),
            )

    def create_split_sheet(
        self,
        tx: Any,
        *,
        catalog_id: str,
        recording_id: str | None,
        work_id: str | None,
        right_type: str,
        version: int,
        status: str,
        source_document_key: str | None,
    ) -> str:
        with self._cursor(tx) as cursor:
            cursor.execute(
                """
                INSERT INTO split_sheets
                    (catalog_id, recording_id, work_id, right_type, version, status,
                     source_document_key)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (catalog_id, recording_id, work_id, right_type, version, status, source_document_key),
            )
            return str(cursor.fetchone()[0])

    def add_split_line(
        self, tx: Any, *, split_sheet_id: str, party_id: str, role: str, share_basis_points: int
    ) -> str:
        with self._cursor(tx) as cursor:
            cursor.execute(
                """
                INSERT INTO split_lines (split_sheet_id, party_id, role, share_basis_points)
                VALUES (%s, %s, %s, %s)
                RETURNING id
                """,
                (split_sheet_id, party_id, role, share_basis_points),
            )
            return str(cursor.fetchone()[0])

    def get_split_sheet(self, tx: Any, split_sheet_id: str) -> dict[str, Any]:
        with self._cursor(tx) as cursor:
            cursor.execute(
                """
                SELECT s.id, s.catalog_id, s.recording_id, s.work_id, s.right_type,
                       s.version, s.status, s.source_document_key, s.approved_at,
                       s.approved_by_party_id,
                       COALESCE((SELECT SUM(share_basis_points)
                                   FROM split_lines WHERE split_sheet_id = s.id), 0) AS total
                  FROM split_sheets s
                 WHERE s.id = %s
                """,
                (split_sheet_id,),
            )
            row = cursor.fetchone()
            if row is None:
                raise CatalogNotFoundError(f"split sheet {split_sheet_id} not found")
            cursor.execute(
                """
                SELECT id, party_id, role, share_basis_points
                  FROM split_lines WHERE split_sheet_id = %s
                 ORDER BY created_at, id
                """,
                (split_sheet_id,),
            )
            lines = [
                {
                    "id": str(line_id),
                    "party_id": str(party_id),
                    "role": role,
                    "share_basis_points": int(share),
                }
                for line_id, party_id, role, share in cursor.fetchall()
            ]
        return {
            "id": str(row[0]),
            "catalog_id": str(row[1]),
            "recording_id": str(row[2]) if row[2] else None,
            "work_id": str(row[3]) if row[3] else None,
            "right_type": row[4],
            "version": row[5],
            "status": row[6],
            "source_document_key": row[7],
            "approved_at": row[8].isoformat() if row[8] else None,
            "approved_by_party_id": str(row[9]) if row[9] else None,
            "total_basis_points": int(row[10]),
            "lines": lines,
        }

    def activate_split_sheet(
        self,
        tx: Any,
        *,
        split_sheet_id: str,
        approver_party_id: str,
        approved_at: str,
    ) -> None:
        with self._cursor(tx) as cursor:
            cursor.execute(
                "SELECT status FROM split_sheets WHERE id = %s", (split_sheet_id,)
            )
            row = cursor.fetchone()
            if row is None:
                raise CatalogNotFoundError(f"split sheet {split_sheet_id} not found")
            if row[0] != "draft":
                raise CatalogStateError(f"split sheet {split_sheet_id} is {row[0]}, not draft")
            # Exactly one active sheet per asset/right: retire the predecessor.
            cursor.execute(
                """
                UPDATE split_sheets previous
                   SET status = 'superseded',
                       valid_to = CURRENT_DATE
                  FROM split_sheets current_sheet
                 WHERE previous.id <> current_sheet.id
                   AND previous.status = 'active'
                   AND previous.recording_id IS NOT DISTINCT FROM current_sheet.recording_id
                   AND previous.work_id IS NOT DISTINCT FROM current_sheet.work_id
                   AND previous.right_type = current_sheet.right_type
                   AND current_sheet.id = %s
                """,
                (split_sheet_id,),
            )
            cursor.execute(
                """
                UPDATE split_sheets
                   SET status = 'active', approved_at = %s, approved_by_party_id = %s
                 WHERE id = %s
                """,
                (approved_at, approver_party_id, split_sheet_id),
            )

    def write_audit(self, tx: Any, event: AuditEvent) -> None:
        from psycopg.types.json import Jsonb

        with self._cursor(tx) as cursor:
            cursor.execute(
                """
                INSERT INTO audit_events
                    (catalog_id, actor_id, action, entity_type, entity_id, request_id, metadata)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    event.catalog_id,
                    event.actor_id,
                    event.action,
                    event.entity_type,
                    event.entity_id,
                    event.request_id,
                    Jsonb(event.metadata),
                ),
            )

    @staticmethod
    def _translate(error: Exception) -> CatalogError:
        message = str(error)
        if "unique" in message.lower() or "duplicate" in message.lower():
            return CatalogConflictError("duplicate catalog identifier (ISRC/ISWC/UPC/registration)")
        if "foreign key" in message.lower():
            return CatalogNotFoundError("referenced entity does not exist")
        return CatalogConflictError(message)
