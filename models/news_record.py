from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Optional


SENTIMENTS = {"positive", "negative", "mixed", "neutral", "unknown"}
CONFIDENCE_LEVELS = {"high", "medium", "low", "unknown"}


@dataclass
class NewsRecord:
    publication_date: Optional[date] = None
    publisher: Optional[str] = None
    title: Optional[str] = None
    summary: Optional[str] = None
    source_url: Optional[str] = None
    geographic_scope: Optional[str] = None
    themes: list[str] = field(default_factory=list)
    sentiment: str = "unknown"
    investment_relevance: Optional[str] = None
    confidence: str = "unknown"
    is_mock_data: bool = True
    source_name: Optional[str] = None
    retrieved_at: Optional[datetime] = None

    def __post_init__(self) -> None:
        if self.sentiment not in SENTIMENTS:
            self.sentiment = "unknown"
        if self.confidence not in CONFIDENCE_LEVELS:
            self.confidence = "unknown"

    def to_dict(self) -> dict[str, Any]:
        return {
            "publication_date": self.publication_date.isoformat() if isinstance(self.publication_date, date) else None,
            "publisher": self.publisher,
            "title": self.title,
            "summary": self.summary,
            "source_url": self.source_url,
            "geographic_scope": self.geographic_scope,
            "themes": list(self.themes),
            "sentiment": self.sentiment,
            "investment_relevance": self.investment_relevance,
            "confidence": self.confidence,
            "is_mock_data": self.is_mock_data,
            "source_name": self.source_name,
            "retrieved_at": self.retrieved_at.isoformat() if isinstance(self.retrieved_at, datetime) else None,
        }
