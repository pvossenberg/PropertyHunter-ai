from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional


@dataclass
class ComparableProperty:
    address: Optional[str] = None
    distance_meters: Optional[float] = None
    living_area_m2: Optional[float] = None
    asking_price: Optional[float] = None
    sold_price: Optional[float] = None
    sold_date: Optional[date] = None
    price_per_m2: Optional[float] = None
    difference_with_subject_pct: Optional[float] = None

    def reference_price(self) -> Optional[float]:
        if self.sold_price not in (None, ""):
            return float(self.sold_price)
        if self.asking_price not in (None, ""):
            return float(self.asking_price)
        return None

    def to_dict(self) -> dict[str, object]:
        return {
            "address": self.address,
            "distance_meters": self.distance_meters,
            "living_area_m2": self.living_area_m2,
            "asking_price": self.asking_price,
            "sold_price": self.sold_price,
            "sold_date": self.sold_date.isoformat() if isinstance(self.sold_date, date) else None,
            "price_per_m2": self.price_per_m2,
            "difference_with_subject_pct": self.difference_with_subject_pct,
        }

    @classmethod
    def from_dict(cls, payload: object) -> "ComparableProperty":
        if isinstance(payload, cls):
            return payload
        if not isinstance(payload, dict):
            return cls()

        sold_date = payload.get("sold_date")
        if isinstance(sold_date, str):
            try:
                sold_date = date.fromisoformat(sold_date)
            except ValueError:
                sold_date = None

        return cls(
            address=payload.get("address"),
            distance_meters=payload.get("distance_meters"),
            living_area_m2=payload.get("living_area_m2"),
            asking_price=payload.get("asking_price"),
            sold_price=payload.get("sold_price"),
            sold_date=sold_date if isinstance(sold_date, date) else None,
            price_per_m2=payload.get("price_per_m2"),
            difference_with_subject_pct=payload.get("difference_with_subject_pct"),
        )