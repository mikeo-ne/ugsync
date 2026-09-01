"""Pilot radio source configuration.

The radio pilot covers 4–8 *agreed* streams across Kampala, Jinja, Mbarara and
Gulu. A source record carries only an agreed code, display name, region, and
class — never a live stream URL or credential (those stay in the deployment
secret manager and are referenced at capture time, per the edge contract).

``PilotSource`` rows are loaded from JSON (or the built-in reference template),
so an operator can substitute the stations actually under pilot MoU without
changing code.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

PILOT_REGIONS = frozenset({"Kampala", "Jinja", "Mbarara", "Gulu"})
SOURCE_CLASSES = frozenset({"fm_stream", "online_stream"})


@dataclass(frozen=True, slots=True)
class PilotSource:
    source_code: str
    display_name: str
    region: str
    source_class: str = "fm_stream"
    agreed: bool = True
    notes: str | None = None

    def __post_init__(self) -> None:
        if not self.source_code or not self.source_code.strip():
            raise ValueError("source_code is required")
        if not self.display_name or not self.display_name.strip():
            raise ValueError("display_name is required")
        if self.region not in PILOT_REGIONS:
            raise ValueError(f"region must be one of {sorted(PILOT_REGIONS)}")
        if self.source_class not in SOURCE_CLASSES:
            raise ValueError(f"source_class must be one of {sorted(SOURCE_CLASSES)}")


# A small reference roster for local development and report templates. Real
# pilot stations are substituted via a JSON file of agreed sources; names here
# are illustrative placeholders, not confirmed partners.
_REFERENCE_SOURCES: tuple[PilotSource, ...] = (
    PilotSource("kampala-radio-01", "Kampala Pilot FM A", "Kampala",
                notes="illustrative placeholder - substitute the agreed station"),
    PilotSource("kampala-radio-02", "Kampala Online Stream B", "Kampala", "online_stream",
                notes="illustrative placeholder"),
    PilotSource("jinja-radio-01", "Jinja Pilot FM", "Jinja",
                notes="illustrative placeholder"),
    PilotSource("mbarara-radio-01", "Mbarara Pilot FM", "Mbarara",
                notes="illustrative placeholder"),
    PilotSource("gulu-radio-01", "Gulu Pilot FM", "Gulu",
                notes="illustrative placeholder"),
)


def pilot_sources_template() -> tuple[PilotSource, ...]:
    """The built-in illustrative roster (4 regions, 5 sources)."""

    return _REFERENCE_SOURCES


def load_pilot_sources(path: str | Path | None = None) -> tuple[PilotSource, ...]:
    """Load agreed pilot sources from a JSON list, or return the template."""

    if path is None:
        return pilot_sources_template()
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise TypeError("pilot sources file must contain a JSON array")
    return tuple(_source_from_dict(item) for item in data)


def _source_from_dict(raw: object) -> PilotSource:
    if not isinstance(raw, dict):
        raise TypeError("each source must be a JSON object")
    return PilotSource(
        source_code=str(raw["source_code"]),
        display_name=str(raw["display_name"]),
        region=str(raw["region"]),
        source_class=str(raw.get("source_class", "fm_stream")),
        agreed=bool(raw.get("agreed", True)),
        notes=raw.get("notes"),
    )


def sources_as_json(sources: Sequence[PilotSource]) -> str:
    return json.dumps([asdict(s) for s in sources], indent=2)


def agreed_source_codes(sources: Iterable[PilotSource]) -> frozenset[str]:
    return frozenset(s.source_code for s in sources if s.agreed)
