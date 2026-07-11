from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Optional


GOVERNING_BODIES = {
    "municipal_council",
    "executive_board",
    "council_committee",
    "province",
    "water_authority",
    "national_government",
    "unknown",
}
CONFIDENCE_LEVELS = {"high", "medium", "low", "unknown"}


@dataclass
class GovernmentRecord:
    meeting_date: Optional[date] = None
    governing_body: str = "unknown"
    document_type: Optional[str] = None
    title: Optional[str] = None
    summary: Optional[str] = None
    status: Optional[str] = None
    themes: list[str] = field(default_factory=list)
    geographic_scope: Optional[str] = None
    source_url: Optional[str] = None
    relevance_to_property: Optional[str] = None
    expected_investment_impact: Optional[str] = None
    confidence: str = "unknown"
    is_mock_data: bool = True
    source_name: Optional[str] = None
    retrieved_at: Optional[datetime] = None

    def __post_init__(self) -> None:
        if self.governing_body not in GOVERNING_BODIES:
            self.governing_body = "unknown"
        if self.confidence not in CONFIDENCE_LEVELS:
            self.confidence = "unknown"

    def to_dict(self) -> dict[str, Any]:
        return {
            "meeting_date": self.meeting_date.isoformat() if isinstance(self.meeting_date, date) else None,
            "governing_body": self.governing_body,
            "document_type": self.document_type,
            "title": self.title,
            "summary": self.summary,
            "status": self.status,
            "themes": list(self.themes),
            "geographic_scope": self.geographic_scope,
            "source_url": self.source_url,
            "relevance_to_property": self.relevance_to_property,
            "expected_investment_impact": self.expected_investment_impact,
            "confidence": self.confidence,
            "is_mock_data": self.is_mock_data,
            "source_name": self.source_name,
            "retrieved_at": self.retrieved_at.isoformat() if isinstance(self.retrieved_at, datetime) else None,
        }
