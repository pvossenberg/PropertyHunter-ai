from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Any, Optional


CONFIDENCE_LEVELS = {"high", "medium", "low", "unknown"}


@dataclass
class DataProvenance:
    source_name: Optional[str] = None
    source_url: Optional[str] = None
    retrieved_at: Optional[str] = None
    confidence: str = "unknown"
    raw_value: Any = None
    normalized_value: Any = None

    def __post_init__(self) -> None:
        if self.confidence not in CONFIDENCE_LEVELS:
            self.confidence = "unknown"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_value(cls, raw_value: Any, normalized_value: Any = None, source_name: Optional[str] = None, source_url: Optional[str] = None, confidence: str = "unknown") -> "DataProvenance":
        return cls(
            source_name=source_name,
            source_url=source_url,
            retrieved_at=datetime.now(timezone.utc).isoformat(),
            confidence=confidence,
            raw_value=raw_value,
            normalized_value=normalized_value,
        )


def build_provenance_record(
    source_name: Optional[str],
    source_url: Optional[str],
    raw_value: Any,
    normalized_value: Any,
    confidence: str = "unknown",
) -> dict[str, Any]:
    return DataProvenance(
        source_name=source_name,
        source_url=source_url,
        retrieved_at=datetime.now(timezone.utc).isoformat(),
        confidence=confidence,
        raw_value=raw_value,
        normalized_value=normalized_value,
    ).to_dict()
