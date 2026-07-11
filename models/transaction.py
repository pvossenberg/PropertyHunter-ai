from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional


TRANSACTION_TYPES = {"sale", "auction_sale", "transfer", "merger_or_split", "unknown"}


@dataclass
class PropertyTransaction:
    transaction_date: Optional[date] = None
    transaction_type: str = "unknown"
    transaction_price: Optional[float] = None
    price_status: str = "unknown"
    buyer_type: Optional[str] = None
    seller_type: Optional[str] = None
    source: Optional[str] = None
    source_url: Optional[str] = None
    confidence: str = "unknown"
    notes: Optional[str] = None

    def __post_init__(self) -> None:
        if self.transaction_type not in TRANSACTION_TYPES:
            self.transaction_type = "unknown"
        if not isinstance(self.confidence, str) or not self.confidence.strip():
            self.confidence = "unknown"

    def to_dict(self) -> dict[str, object]:
        return {
            "transaction_date": self.transaction_date.isoformat() if isinstance(self.transaction_date, date) else None,
            "transaction_type": self.transaction_type,
            "transaction_price": self.transaction_price,
            "price_status": self.price_status,
            "buyer_type": self.buyer_type,
            "seller_type": self.seller_type,
            "source": self.source,
            "source_url": self.source_url,
            "confidence": self.confidence,
            "notes": self.notes,
        }

    @classmethod
    def from_dict(cls, data: object) -> "PropertyTransaction":
        if isinstance(data, cls):
            return data
        if not isinstance(data, dict):
            return cls()

        parsed_date = data.get("transaction_date")
        if isinstance(parsed_date, str):
            try:
                parsed_date = date.fromisoformat(parsed_date)
            except ValueError:
                parsed_date = None

        return cls(
            transaction_date=parsed_date if isinstance(parsed_date, date) else None,
            transaction_type=data.get("transaction_type") or "unknown",
            transaction_price=data.get("transaction_price"),
            price_status=data.get("price_status") or "unknown",
            buyer_type=data.get("buyer_type"),
            seller_type=data.get("seller_type"),
            source=data.get("source"),
            source_url=data.get("source_url"),
            confidence=data.get("confidence") or "unknown",
            notes=data.get("notes"),
        )
