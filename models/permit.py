from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional


PERMIT_STATUSES = {"pending", "granted", "rejected", "withdrawn", "revoked", "lapsed", "outside_scope", "unknown"}


@dataclass
class PermitRecord:
    application_date: Optional[date] = None
    decision_date: Optional[date] = None
    permit_type: Optional[str] = None
    description: Optional[str] = None
    status: str = "unknown"
    reference_number: Optional[str] = None
    authority: Optional[str] = None
    source: Optional[str] = None
    source_url: Optional[str] = None
    confidence: str = "unknown"
    affects_investment_case: bool = False
    investment_relevance: Optional[str] = None
    notes: Optional[str] = None

    def __post_init__(self) -> None:
        if self.status not in PERMIT_STATUSES:
            self.status = "unknown"
        if not isinstance(self.confidence, str) or not self.confidence.strip():
            self.confidence = "unknown"

    def to_dict(self) -> dict[str, object]:
        return {
            "application_date": self.application_date.isoformat() if isinstance(self.application_date, date) else None,
            "decision_date": self.decision_date.isoformat() if isinstance(self.decision_date, date) else None,
            "permit_type": self.permit_type,
            "description": self.description,
            "status": self.status,
            "reference_number": self.reference_number,
            "authority": self.authority,
            "source": self.source,
            "source_url": self.source_url,
            "confidence": self.confidence,
            "affects_investment_case": self.affects_investment_case,
            "investment_relevance": self.investment_relevance,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, data: object) -> "PermitRecord":
        if isinstance(data, cls):
            return data
        if not isinstance(data, dict):
            return cls()

        application_date = data.get("application_date")
        if isinstance(application_date, str):
            try:
                application_date = date.fromisoformat(application_date)
            except ValueError:
                application_date = None

        decision_date = data.get("decision_date")
        if isinstance(decision_date, str):
            try:
                decision_date = date.fromisoformat(decision_date)
            except ValueError:
                decision_date = None

        return cls(
            application_date=application_date if isinstance(application_date, date) else None,
            decision_date=decision_date if isinstance(decision_date, date) else None,
            permit_type=data.get("permit_type"),
            description=data.get("description"),
            status=data.get("status") or "unknown",
            reference_number=data.get("reference_number"),
            authority=data.get("authority"),
            source=data.get("source"),
            source_url=data.get("source_url"),
            confidence=data.get("confidence") or "unknown",
            affects_investment_case=bool(data.get("affects_investment_case", False)),
            investment_relevance=data.get("investment_relevance"),
            notes=data.get("notes"),
        )
