"""Validated onboarding documents.

These dataclasses mirror the catalog side of the PostgreSQL core schema
(organizations, rights_parties, artists, music_works, releases, recordings,
split_sheets/split_lines).  Validation here is intentionally permissive about
*ownership* — ISRC/ISWC syntax is checked, never rights ownership, which
remains an approved catalog/CMO workflow.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

ORGANIZATION_TYPES = frozenset(
    {"cmo", "label", "publisher", "venue_operator", "broadcaster", "technology_partner", "other"}
)
PARTY_KINDS = frozenset({"individual", "organization"})
ARTIST_ROLES = frozenset({"primary", "featured", "remixer", "producer"})
CONTRIBUTOR_ROLES = frozenset({"composer", "lyricist", "arranger", "publisher", "administrator"})
RIGHT_TYPES = frozenset({"master", "composition", "performance", "mechanical"})
RELEASE_TYPES = frozenset({"single", "ep", "album", "compilation", "other"})

ISRC_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{3}\d{7}$")
ISWC_CANONICAL_RE = re.compile(r"^T-\d{3}\.\d{3}\.\d{3}-\d$")
# A musical work ISWC holds ten digits: three groups of three plus a check digit.
ISWC_DIGITS_RE = re.compile(r"^\d{10}$")
COUNTRY_RE = re.compile(r"^[A-Z]{2}$")
LANGUAGE_RE = re.compile(r"^[a-z]{3}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

SPLIT_TOTAL_BASIS_POINTS = 10_000
MAX_NAME_LENGTH = 400


class ValidationProblem(Exception):
    """Collects every field error so an onboarding call fails with the full list."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("; ".join(errors))


def _require_text(value: object, field_name: str, errors: list[str], *, max_length: int = MAX_NAME_LENGTH) -> str | None:
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{field_name}: required non-empty string")
        return None
    text = value.strip()
    if len(text) > max_length:
        errors.append(f"{field_name}: exceeds {max_length} characters")
    return text


def _optional_text(value: object, field_name: str, errors: list[str]) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{field_name}: must be a non-empty string when provided")
        return None
    return value.strip()


def _require_ref(value: object, field_name: str, refs: dict[str, object], errors: list[str]) -> str | None:
    ref = _require_text(value, field_name, errors)
    if ref is not None and ref not in refs:
        errors.append(f"{field_name}: references unknown local id '{ref}'")
    return ref


def _normalize_isrc(value: object, field_name: str, errors: list[str]) -> str | None:
    raw = _optional_text(value, field_name, errors)
    if raw is None:
        return None
    candidate = raw.upper().replace(" ", "").replace("-", "")
    if not ISRC_RE.match(candidate):
        errors.append(f"{field_name}: '{raw}' is not a valid 12-character ISRC")
        return None
    return candidate


def _normalize_iswc(value: object, field_name: str, errors: list[str]) -> str | None:
    raw = _optional_text(value, field_name, errors)
    if raw is None:
        return None
    if ISWC_CANONICAL_RE.match(raw):
        return raw
    digits = re.sub(r"[^0-9]", "", raw)
    if ISWC_DIGITS_RE.match(digits):
        return f"T-{digits[0:3]}.{digits[3:6]}.{digits[6:9]}-{digits[9]}"
    errors.append(f"{field_name}: '{raw}' is not a valid ISWC (T-xxx.xxx.xxx-x)")
    return None


@dataclass(frozen=True, slots=True)
class OrganizationDocument:
    local_id: str
    legal_name: str
    organization_type: str
    trading_name: str | None = None
    registration_number: str | None = None
    country_code: str = "UG"
    contact_email: str | None = None


@dataclass(frozen=True, slots=True)
class PartyDocument:
    local_id: str
    party_kind: str
    legal_name: str
    organization_local_id: str | None = None
    stage_or_trading_name: str | None = None


@dataclass(frozen=True, slots=True)
class ArtistCredit:
    party_local_id: str
    artist_role: str
    stage_name: str
    display_order: int = 1
    country_code: str = "UG"


@dataclass(frozen=True, slots=True)
class WorkContributor:
    party_local_id: str
    contributor_role: str
    display_order: int = 1


@dataclass(frozen=True, slots=True)
class WorkDocument:
    local_id: str
    title: str
    language_code: str = "und"
    iswc: str | None = None
    alternate_titles: tuple[str, ...] = ()
    contributors: tuple[WorkContributor, ...] = ()


@dataclass(frozen=True, slots=True)
class ReleaseDocument:
    local_id: str
    title: str
    release_type: str = "single"
    upc_ean: str | None = None


@dataclass(frozen=True, slots=True)
class RecordingDocument:
    local_id: str
    title: str
    duration_seconds: float
    isrc: str | None = None
    explicit: bool = False
    audio_sha256: str | None = None
    fingerprint_schema_id: str | None = None
    artist_credits: tuple[ArtistCredit, ...] = ()
    work_local_ids: tuple[str, ...] = ()
    release_local_ids: tuple[str, ...] = ()
    release_track_numbers: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SplitLineDocument:
    party_local_id: str
    role: str
    share_basis_points: int


@dataclass(frozen=True, slots=True)
class SplitSheetDocument:
    right_type: str
    asset_local_id: str  # recording local id for master; work local id otherwise
    lines: tuple[SplitLineDocument, ...]
    source_document_key: str | None = None


@dataclass(frozen=True, slots=True)
class OnboardingDocument:
    catalog_name: str
    owner_local_id: str
    organizations: tuple[OrganizationDocument, ...] = ()
    parties: tuple[PartyDocument, ...] = ()
    artists: tuple[ArtistCredit, ...] = ()
    works: tuple[WorkDocument, ...] = ()
    releases: tuple[ReleaseDocument, ...] = ()
    recordings: tuple[RecordingDocument, ...] = ()
    split_sheets: tuple[SplitSheetDocument, ...] = ()


def _parse_organization(raw: object, errors: list[str]) -> OrganizationDocument | None:
    if not isinstance(raw, dict):
        errors.append("organizations[]: expected object")
        return None
    local_id = _require_text(raw.get("local_id"), "organization.local_id", errors)
    legal_name = _require_text(raw.get("legal_name"), "organization.legal_name", errors)
    org_type = _require_text(raw.get("organization_type"), "organization.organization_type", errors)
    if org_type is not None and org_type not in ORGANIZATION_TYPES:
        errors.append(f"organization.organization_type: '{org_type}' not in {sorted(ORGANIZATION_TYPES)}")
    country = raw.get("country_code", "UG")
    if not isinstance(country, str) or not COUNTRY_RE.match(country):
        errors.append("organization.country_code: expected ISO-3166 alpha-2, e.g. 'UG'")
        country = "UG"
    email = raw.get("contact_email")
    if email is not None and (not isinstance(email, str) or not EMAIL_RE.match(email) or len(email) > 254):
        errors.append("organization.contact_email: invalid email address")
        email = None
    if local_id is None:
        return None
    return OrganizationDocument(
        local_id=local_id,
        legal_name=legal_name or "",
        organization_type=org_type or "other",
        trading_name=_optional_text(raw.get("trading_name"), "organization.trading_name", errors),
        registration_number=_optional_text(
            raw.get("registration_number"), "organization.registration_number", errors
        ),
        country_code=country,
        contact_email=email,
    )


def _parse_party(raw: object, org_ids: set[str], errors: list[str]) -> PartyDocument | None:
    if not isinstance(raw, dict):
        errors.append("parties[]: expected object")
        return None
    local_id = _require_text(raw.get("local_id"), "party.local_id", errors)
    legal_name = _require_text(raw.get("legal_name"), "party.legal_name", errors)
    kind = _require_text(raw.get("party_kind"), "party.party_kind", errors)
    if kind is not None and kind not in PARTY_KINDS:
        errors.append(f"party.party_kind: '{kind}' not in {sorted(PARTY_KINDS)}")
    org_ref = raw.get("organization_local_id")
    if org_ref is not None:
        org_ref = _require_text(org_ref, "party.organization_local_id", errors)
        if org_ref is not None and org_ref not in org_ids:
            errors.append(f"party.organization_local_id: unknown organization '{org_ref}'")
    if kind == "individual" and org_ref is not None:
        errors.append("party.organization_local_id: individual parties must not link an organization")
    if kind == "organization" and org_ref is None:
        errors.append("party.organization_local_id: organization parties must link an organization")
    if local_id is None:
        return None
    return PartyDocument(
        local_id=local_id,
        party_kind=kind or "individual",
        legal_name=legal_name or "",
        organization_local_id=org_ref,
        stage_or_trading_name=_optional_text(
            raw.get("stage_or_trading_name"), "party.stage_or_trading_name", errors
        ),
    )


def _parse_contributor(raw: object, errors: list[str]) -> WorkContributor | None:
    if not isinstance(raw, dict):
        errors.append("contributors[]: expected object")
        return None
    party = _require_text(raw.get("party_local_id"), "contributor.party_local_id", errors)
    role = _require_text(raw.get("contributor_role"), "contributor.contributor_role", errors)
    if role is not None and role not in CONTRIBUTOR_ROLES:
        errors.append(f"contributor.contributor_role: '{role}' not in {sorted(CONTRIBUTOR_ROLES)}")
    order = raw.get("display_order", 1)
    if not isinstance(order, int) or isinstance(order, bool) or order < 1:
        errors.append("contributor.display_order: positive integer required")
        order = 1
    return WorkContributor(party_local_id=party or "", contributor_role=role or "", display_order=order)


def _parse_work(raw: object, errors: list[str]) -> WorkDocument | None:
    if not isinstance(raw, dict):
        errors.append("works[]: expected object")
        return None
    local_id = _require_text(raw.get("local_id"), "work.local_id", errors)
    title = _require_text(raw.get("title"), "work.title", errors)
    language = raw.get("language_code", "und")
    if not isinstance(language, str) or not LANGUAGE_RE.match(language):
        errors.append("work.language_code: expected ISO-639-3 code, e.g. 'lug' or 'und'")
        language = "und"
    iswc = _normalize_iswc(raw.get("iswc"), "work.iswc", errors)
    alternates = raw.get("alternate_titles", [])
    if not isinstance(alternates, list) or not all(isinstance(a, str) and a.strip() for a in alternates):
        errors.append("work.alternate_titles: expected array of non-empty strings")
        alternates = []
    contributors_raw = raw.get("contributors", [])
    if not isinstance(contributors_raw, list):
        errors.append("work.contributors: expected array")
        contributors_raw = []
    contributors = tuple(c for c in (_parse_contributor(item, errors) for item in contributors_raw) if c)
    if local_id is None:
        return None
    return WorkDocument(
        local_id=local_id,
        title=title or "",
        language_code=language,
        iswc=iswc,
        alternate_titles=tuple(a.strip() for a in alternates),
        contributors=contributors,
    )


def _parse_release(raw: object, errors: list[str]) -> ReleaseDocument | None:
    if not isinstance(raw, dict):
        errors.append("releases[]: expected object")
        return None
    local_id = _require_text(raw.get("local_id"), "release.local_id", errors)
    title = _require_text(raw.get("title"), "release.title", errors)
    release_type = raw.get("release_type", "single")
    if not isinstance(release_type, str) or release_type not in RELEASE_TYPES:
        errors.append(f"release.release_type: '{release_type}' not in {sorted(RELEASE_TYPES)}")
        release_type = "single"
    upc = _optional_text(raw.get("upc_ean"), "release.upc_ean", errors)
    if upc is not None and not re.match(r"^\d{8,14}$", upc):
        errors.append("release.upc_ean: expected 8–14 digits")
    if local_id is None:
        return None
    return ReleaseDocument(
        local_id=local_id, title=title or "", release_type=release_type, upc_ean=upc
    )


def _parse_artist_credit(raw: object, errors: list[str]) -> ArtistCredit | None:
    if not isinstance(raw, dict):
        errors.append("artist_credits[]: expected object")
        return None
    party = _require_text(raw.get("party_local_id"), "artist_credit.party_local_id", errors)
    role = _require_text(raw.get("artist_role"), "artist_credit.artist_role", errors)
    if role is not None and role not in ARTIST_ROLES:
        errors.append(f"artist_credit.artist_role: '{role}' not in {sorted(ARTIST_ROLES)}")
    stage_name = _require_text(raw.get("stage_name"), "artist_credit.stage_name", errors)
    order = raw.get("display_order", 1)
    if not isinstance(order, int) or isinstance(order, bool) or order < 1:
        errors.append("artist_credit.display_order: positive integer required")
        order = 1
    country = raw.get("country_code", "UG")
    if not isinstance(country, str) or not COUNTRY_RE.match(country):
        errors.append("artist_credit.country_code: expected ISO-3166 alpha-2")
        country = "UG"
    return ArtistCredit(
        party_local_id=party or "",
        artist_role=role or "",
        stage_name=stage_name or "",
        display_order=order,
        country_code=country,
    )


def _parse_recording(raw: object, errors: list[str]) -> RecordingDocument | None:
    if not isinstance(raw, dict):
        errors.append("recordings[]: expected object")
        return None
    local_id = _require_text(raw.get("local_id"), "recording.local_id", errors)
    title = _require_text(raw.get("title"), "recording.title", errors)
    duration = raw.get("duration_seconds")
    if not isinstance(duration, (int, float)) or isinstance(duration, bool) or not 0 < float(duration) <= 60 * 60 * 6:
        errors.append("recording.duration_seconds: positive number of seconds (max 6h) required")
        duration = 1.0
    isrc = _normalize_isrc(raw.get("isrc"), "recording.isrc", errors)
    explicit = bool(raw.get("explicit", False))
    audio_sha = _optional_text(raw.get("audio_sha256"), "recording.audio_sha256", errors)
    if audio_sha is not None and not SHA256_RE.match(audio_sha.lower()):
        errors.append("recording.audio_sha256: expected 64 lowercase hex characters")
        audio_sha = None
    elif audio_sha is not None:
        audio_sha = audio_sha.lower()
    schema_id = _optional_text(
        raw.get("fingerprint_schema_id"), "recording.fingerprint_schema_id", errors
    )
    credits_raw = raw.get("artist_credits", [])
    if not isinstance(credits_raw, list):
        errors.append("recording.artist_credits: expected array")
        credits_raw = []
    credits = tuple(c for c in (_parse_artist_credit(item, errors) for item in credits_raw) if c)
    work_ids = raw.get("work_local_ids", [])
    release_ids = raw.get("release_local_ids", [])
    if not isinstance(work_ids, list) or not all(isinstance(w, str) and w.strip() for w in work_ids):
        errors.append("recording.work_local_ids: expected array of local ids")
        work_ids = []
    if not isinstance(release_ids, list) or not all(isinstance(r, str) and r.strip() for r in release_ids):
        errors.append("recording.release_local_ids: expected array of local ids")
        release_ids = []
    track_numbers = raw.get("release_track_numbers", {})
    if not isinstance(track_numbers, dict):
        errors.append("recording.release_track_numbers: expected object mapping release id to track number")
        track_numbers = {}
    if local_id is None:
        return None
    return RecordingDocument(
        local_id=local_id,
        title=title or "",
        duration_seconds=float(duration),
        isrc=isrc,
        explicit=explicit,
        audio_sha256=audio_sha,
        fingerprint_schema_id=schema_id,
        artist_credits=credits,
        work_local_ids=tuple(work_ids),
        release_local_ids=tuple(release_ids),
        release_track_numbers={str(k): int(v) for k, v in track_numbers.items() if isinstance(v, int)},
    )


def _parse_split_sheet(raw: object, errors: list[str]) -> SplitSheetDocument | None:
    if not isinstance(raw, dict):
        errors.append("split_sheets[]: expected object")
        return None
    right_type = _require_text(raw.get("right_type"), "split_sheet.right_type", errors)
    if right_type is not None and right_type not in RIGHT_TYPES:
        errors.append(f"split_sheet.right_type: '{right_type}' not in {sorted(RIGHT_TYPES)}")
    asset_id = _require_text(raw.get("asset_local_id"), "split_sheet.asset_local_id", errors)
    lines_raw = raw.get("lines", [])
    if not isinstance(lines_raw, list) or not lines_raw:
        errors.append("split_sheet.lines: at least one split line is required")
        lines_raw = []
    lines: list[SplitLineDocument] = []
    for index, item in enumerate(lines_raw):
        if not isinstance(item, dict):
            errors.append(f"split_sheet.lines[{index}]: expected object")
            continue
        party = _require_text(item.get("party_local_id"), f"split_sheet.lines[{index}].party_local_id", errors)
        role = _require_text(item.get("role"), f"split_sheet.lines[{index}].role", errors)
        share = item.get("share_basis_points")
        if not isinstance(share, int) or isinstance(share, bool) or not 0 < share <= SPLIT_TOTAL_BASIS_POINTS:
            errors.append(
                f"split_sheet.lines[{index}].share_basis_points: integer in 1..{SPLIT_TOTAL_BASIS_POINTS} required"
            )
            share = 0
        lines.append(SplitLineDocument(party_local_id=party or "", role=role or "", share_basis_points=share))
    source_key = _optional_text(
        raw.get("source_document_key"), "split_sheet.source_document_key", errors
    )
    return SplitSheetDocument(
        right_type=right_type or "master",
        asset_local_id=asset_id or "",
        lines=tuple(lines),
        source_document_key=source_key,
    )


def parse_onboarding_document(payload: object) -> OnboardingDocument:
    """Validate and normalize a JSON onboarding payload.

    Raises :class:`ValidationProblem` with *every* field error found.
    """

    errors: list[str] = []
    if not isinstance(payload, dict):
        raise ValidationProblem(["onboarding payload: expected a JSON object"])

    catalog_name = _require_text(payload.get("catalog_name"), "catalog_name", errors)
    owner = _require_text(payload.get("owner_local_id"), "owner_local_id", errors)

    def _collection(key: str, parser: Callable[[object, list[str]], object]) -> tuple[object, ...]:
        raw_items = payload.get(key, [])
        if not isinstance(raw_items, list):
            errors.append(f"{key}: expected array")
            return ()
        return tuple(item for item in (parser(raw, errors) for raw in raw_items) if item is not None)

    organizations = _collection("organizations", _parse_organization)
    org_ids = {org.local_id for org in organizations}
    if len(org_ids) != len(organizations):
        errors.append("organizations: duplicate local_id")

    parties = _collection("parties", lambda raw, errs: _parse_party(raw, org_ids, errs))
    party_ids = {party.local_id for party in parties}
    if len(party_ids) != len(parties):
        errors.append("parties: duplicate local_id")

    works = _collection("works", _parse_work)
    work_ids = {work.local_id for work in works}
    if len(work_ids) != len(works):
        errors.append("works: duplicate local_id")

    releases = _collection("releases", _parse_release)
    release_ids = {release.local_id for release in releases}
    if len(release_ids) != len(releases):
        errors.append("releases: duplicate local_id")

    recordings = _collection("recordings", _parse_recording)
    recording_ids = {recording.local_id for recording in recordings}
    if len(recording_ids) != len(recordings):
        errors.append("recordings: duplicate local_id")

    # Cross-reference validation (deduplicated so a missing field reports once).
    if owner is not None and owner not in org_ids:
        errors.append("owner_local_id: must reference an organization in this submission")

    artist_parties: set[str] = set()
    for recording in recordings:
        for credit in recording.artist_credits:
            if credit.party_local_id and credit.party_local_id not in party_ids:
                errors.append(
                    f"recording '{recording.local_id}': artist credit references unknown party "
                    f"'{credit.party_local_id}'"
                )
            else:
                artist_parties.add(credit.party_local_id)
        for work_ref in recording.work_local_ids:
            if work_ref not in work_ids:
                errors.append(f"recording '{recording.local_id}': unknown work_local_id '{work_ref}'")
        for release_ref in recording.release_local_ids:
            if release_ref not in release_ids:
                errors.append(
                    f"recording '{recording.local_id}': unknown release_local_id '{release_ref}'"
                )
            track_no = recording.release_track_numbers.get(release_ref)
            if track_no is not None and track_no < 1:
                errors.append(
                    f"recording '{recording.local_id}': release_track_numbers must be positive"
                )

    for work in works:
        for contributor in work.contributors:
            if contributor.party_local_id and contributor.party_local_id not in party_ids:
                errors.append(
                    f"work '{work.local_id}': contributor references unknown party "
                    f"'{contributor.party_local_id}'"
                )

    split_sheets = _collection("split_sheets", _parse_split_sheet)
    seen_sheet_assets: set[tuple[str, str]] = set()
    for index, sheet in enumerate(split_sheets):
        asset_pool = recording_ids if sheet.right_type == "master" else work_ids
        if sheet.asset_local_id not in asset_pool:
            kind = "recording" if sheet.right_type == "master" else "work"
            errors.append(
                f"split_sheets[{index}]: asset_local_id '{sheet.asset_local_id}' is not a "
                f"{kind} in this submission"
            )
        key = (sheet.right_type, sheet.asset_local_id)
        if key in seen_sheet_assets:
            errors.append(f"split_sheets[{index}]: duplicate {sheet.right_type} sheet for asset")
        seen_sheet_assets.add(key)
        parties_on_sheet = [line.party_local_id for line in sheet.lines if line.party_local_id]
        for party_ref in parties_on_sheet:
            if party_ref not in party_ids:
                errors.append(
                    f"split_sheets[{index}]: split line references unknown party '{party_ref}'"
                )
        if len(set(parties_on_sheet)) != len(parties_on_sheet):
            errors.append(f"split_sheets[{index}]: a party appears more than once on the sheet")
        total = sum(line.share_basis_points for line in sheet.lines)
        if total > SPLIT_TOTAL_BASIS_POINTS:
            errors.append(
                f"split_sheets[{index}]: lines total {total} basis points, exceeding "
                f"{SPLIT_TOTAL_BASIS_POINTS} (100%)"
            )
        # Drafts may be incomplete; activation enforces the exact 10,000 later.

    if errors:
        raise ValidationProblem(errors)

    return OnboardingDocument(
        catalog_name=catalog_name or "",
        owner_local_id=owner or "",
        organizations=organizations,
        parties=parties,
        artists=tuple(
            credit for recording in recordings for credit in recording.artist_credits
        ),
        works=works,
        releases=releases,
        recordings=recordings,
        split_sheets=split_sheets,
    )
