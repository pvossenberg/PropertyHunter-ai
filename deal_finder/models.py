from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class NormalizedListing:
    source_name: str
    source_url: str
    external_listing_id: str | None = None
    title: str | None = None
    address: str | None = None
    city: str | None = None
    asking_price: float | None = None
    surface_m2: float | None = None
    property_type: str | None = None
    description: str | None = None
    listing_status: str = "active"
    raw_payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class DeduplicationResult:
    matched_listing_id: str | None = None
    matched_property_id: str | None = None
    match_method: str = "none"
    confidence: float = 0.0
    warnings: list[str] = field(default_factory=list)


@dataclass
class ListingChangeResult:
    changed: bool
    change_type: str
    snapshot_id: str | None = None


@dataclass
class RankingResult:
    candidate_score: int
    priority: str
    reason_codes: list[str] = field(default_factory=list)
    missing_data_warnings: list[str] = field(default_factory=list)
    component_scores: dict[str, int | None] = field(default_factory=dict)
    confidence_score: int = 0
    strengths: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    missing_information: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    calculated_metrics: dict[str, Any] = field(default_factory=dict)
    scenario_inputs: dict[str, Any] = field(default_factory=dict)


@dataclass
class ScanRunSummary:
    source_id: str
    status: str = "running"
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: datetime | None = None
    items_found: int = 0
    items_new: int = 0
    items_changed: int = 0
    error_message: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
