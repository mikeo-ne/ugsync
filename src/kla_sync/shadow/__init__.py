"""Controlled radio shadow-monitoring pilot pipeline.

This package produces the **non-financial** weekly reconciliation report
described in the pilot plan: observed candidate plays by station, station-log
agreement and exceptions, unmatched local-repertoire candidates for catalog
outreach, and reviewed false-positive/negative outcomes with source-specific
precision metrics.

A shadow run never moves money, never changes a split or tariff, and never
treats a candidate as payable. Its output is a report for the joint steering
group.
"""

from .reconciliation import StationLogEntry, reconcile_with_station_log
from .report import ShadowReport, render_markdown
from .service import ShadowMonitoringService
from .sources import PilotSource, load_pilot_sources, pilot_sources_template

__all__ = [
    "PilotSource",
    "ShadowMonitoringService",
    "ShadowReport",
    "StationLogEntry",
    "load_pilot_sources",
    "pilot_sources_template",
    "reconcile_with_station_log",
    "render_markdown",
]
