from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Optional


DEVELOPMENT_TYPES = {
    "housing",
    "commercial",
    "mixed_use",
    "infrastructure",
    "public_transport",
    "public_space",
    "retail",
    "hospitality",
    "environmental",
    "zoning_change",
    "other",
}
DEVELOPMENT_STATUSES = {"proposed", "under_consultation", "approved", "in_progress", "completed", "cancelled", "unknown"}
IMPACT_DIRECTIONS = {"positive", "negative", "mixed", "neutral", "unknown"}
CONFIDENCE_LEVELS = {"high", "medium", "low", "unknown"}


@dataclass
class AreaDevelopmentRecord:
    title: Optional[str] = None
    development_type: str = "other"
    description: Optional[str] = None
    status: str = "unknown"
    announcement_date: Optional[date] = None
    expected_start_date: Optional[date] = None
    expected_completion_date: Optional[date] = None
    authority: Optional[str] = None
    source_name: Optional[str] = None
    source_url: Optional[str] = None
    distance_to_property_meters: Optional[float] = None
    expected_impact: Optional[str] = None
    impact_direction: str = "unknown"
    confidence: str = "unknown"
    notes: Optional[str] = None
    is_mock_data: bool = True
    retrieved_at: Optional[datetime] = None

    def __post_init__(self) -> None:
        if self.development_type not in DEVELOPMENT_TYPES:
            self.development_type = "other"
        if self.status not in DEVELOPMENT_STATUSES:
            self.status = "unknown"
        if self.impact_direction not in IMPACT_DIRECTIONS:
            self.impact_direction = "unknown"
        if self.confidence not in CONFIDENCE_LEVELS:
            self.confidence = "unknown"

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "development_type": self.development_type,
            "description": self.description,
            "status": self.status,
            "announcement_date": self.announcement_date.isoformat() if isinstance(self.announcement_date, date) else None,
            "expected_start_date": self.expected_start_date.isoformat() if isinstance(self.expected_start_date, date) else None,
            "expected_completion_date": self.expected_completion_date.isoformat() if isinstance(self.expected_completion_date, date) else None,
            "authority": self.authority,
            "source_name": self.source_name,
            "source_url": self.source_url,
            "distance_to_property_meters": self.distance_to_property_meters,
            "expected_impact": self.expected_impact,
            "impact_direction": self.impact_direction,
            "confidence": self.confidence,
            "notes": self.notes,
            "is_mock_data": self.is_mock_data,
            "retrieved_at": self.retrieved_at.isoformat() if isinstance(self.retrieved_at, datetime) else None,
        }
