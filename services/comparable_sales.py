from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from statistics import median
from typing import Protocol

from models.comparable_sale import ComparableProperty


def _safe_float(value) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _price_per_m2_from_values(price, area_m2) -> float | None:
    price_value = _safe_float(price)
    area_value = _safe_float(area_m2)
    if price_value is None or area_value is None or area_value <= 0:
        return None
    return round(price_value / area_value, 2)


class ComparableSalesProvider(Protocol):
    def get_comparables(self, subject_property: dict) -> list[ComparableProperty]:
        ...


class PlaceholderComparableSalesProvider:
    def get_comparables(self, subject_property: dict) -> list[ComparableProperty]:
        subject_area = _safe_float(subject_property.get("surface_m2"))
        subject_price = _safe_float(subject_property.get("asking_price"))
        city = str(subject_property.get("city") or "Onbekend").strip() or "Onbekend"

        base_area = subject_area if subject_area is not None and subject_area > 0 else 95.0
        base_price_per_m2 = _price_per_m2_from_values(subject_price, subject_area)
        if base_price_per_m2 is None:
            base_price_per_m2 = 3800.0

        distances = [240, 390, 620, 870, 1200]
        area_offsets = [-12, -4, 0, 7, 15]
        multipliers = [0.92, 0.97, 1.00, 1.05, 1.12]
        results: list[ComparableProperty] = []

        for index, multiplier in enumerate(multipliers, start=1):
            area = max(35.0, base_area + area_offsets[index - 1])
            sold_price = round(base_price_per_m2 * multiplier * area, 0)
            asking_price = round(sold_price * 1.03, 0)
            results.append(
                ComparableProperty(
                    address=f"Comparable {index}, {city}",
                    distance_meters=float(distances[index - 1]),
                    living_area_m2=round(area, 1),
                    asking_price=asking_price,
                    sold_price=sold_price,
                    sold_date=date.today() - timedelta(days=index * 37),
                )
            )

        return results


class ComparableSalesService:
    def __init__(self, provider: ComparableSalesProvider | None = None):
        self.provider = provider or PlaceholderComparableSalesProvider()

    def get_comparables(self, subject_property: dict) -> list[ComparableProperty]:
        subject_price_per_m2 = _price_per_m2_from_values(subject_property.get("asking_price"), subject_property.get("surface_m2"))
        comparables = self.provider.get_comparables(subject_property)
        enriched: list[ComparableProperty] = []

        for item in comparables:
            current = ComparableProperty.from_dict(item)
            comparable_price_per_m2 = current.price_per_m2
            if comparable_price_per_m2 is None:
                comparable_price_per_m2 = _price_per_m2_from_values(current.reference_price(), current.living_area_m2)

            difference = None
            if comparable_price_per_m2 is not None and subject_price_per_m2 not in (None, 0):
                difference = round(((comparable_price_per_m2 - subject_price_per_m2) / subject_price_per_m2) * 100.0, 2)

            enriched.append(
                replace(
                    current,
                    price_per_m2=comparable_price_per_m2,
                    difference_with_subject_pct=difference,
                )
            )

        return enriched

    def sort_comparables(self, comparables: list[ComparableProperty], sort_key: str, descending: bool = False) -> list[ComparableProperty]:
        key_map = {
            "address": lambda item: str(item.address or ""),
            "distance_meters": lambda item: _safe_float(item.distance_meters) if _safe_float(item.distance_meters) is not None else float("inf"),
            "living_area_m2": lambda item: _safe_float(item.living_area_m2) if _safe_float(item.living_area_m2) is not None else float("inf"),
            "asking_price": lambda item: _safe_float(item.asking_price) if _safe_float(item.asking_price) is not None else float("inf"),
            "sold_price": lambda item: _safe_float(item.sold_price) if _safe_float(item.sold_price) is not None else float("inf"),
            "sold_date": lambda item: item.sold_date or date.min,
            "price_per_m2": lambda item: _safe_float(item.price_per_m2) if _safe_float(item.price_per_m2) is not None else float("inf"),
            "difference_with_subject_pct": lambda item: _safe_float(item.difference_with_subject_pct) if _safe_float(item.difference_with_subject_pct) is not None else float("inf"),
        }
        key_func = key_map.get(sort_key, key_map["distance_meters"])
        return sorted(comparables, key=key_func, reverse=descending)

    def build_table_rows(self, comparables: list[ComparableProperty]) -> list[dict]:
        rows = []
        for item in comparables:
            rows.append(
                {
                    "Address": item.address or "Onbekend",
                    "Distance (meters)": round(item.distance_meters, 1) if _safe_float(item.distance_meters) is not None else None,
                    "Living area (m²)": round(item.living_area_m2, 1) if _safe_float(item.living_area_m2) is not None else None,
                    "Asking price": round(item.asking_price, 0) if _safe_float(item.asking_price) is not None else None,
                    "Sold price": round(item.sold_price, 0) if _safe_float(item.sold_price) is not None else None,
                    "Sold date": item.sold_date.isoformat() if isinstance(item.sold_date, date) else None,
                    "Price per m²": round(item.price_per_m2, 2) if _safe_float(item.price_per_m2) is not None else None,
                    "Difference with subject (%)": round(item.difference_with_subject_pct, 2) if _safe_float(item.difference_with_subject_pct) is not None else None,
                }
            )
        return rows

    def calculate_summary(self, comparables: list[ComparableProperty]) -> dict:
        values = [item.price_per_m2 for item in comparables if _safe_float(item.price_per_m2) is not None]
        numeric_values = [float(value) for value in values]
        if not numeric_values:
            return {
                "average_price_per_m2": None,
                "median_price_per_m2": None,
                "lowest_comparable": None,
                "highest_comparable": None,
            }

        sorted_items = sorted(
            [item for item in comparables if _safe_float(item.price_per_m2) is not None],
            key=lambda item: float(item.price_per_m2),
        )
        return {
            "average_price_per_m2": round(sum(numeric_values) / len(numeric_values), 2),
            "median_price_per_m2": round(float(median(numeric_values)), 2),
            "lowest_comparable": sorted_items[0],
            "highest_comparable": sorted_items[-1],
        }

    def calculate_valuation(self, *, subject_asking_price, subject_surface_m2, comparables: list[ComparableProperty]) -> dict:
        summary = self.calculate_summary(comparables)
        median_price_per_m2 = summary.get("median_price_per_m2")
        surface = _safe_float(subject_surface_m2)
        asking = _safe_float(subject_asking_price)

        estimated_market_value = None
        if median_price_per_m2 is not None and surface is not None and surface > 0:
            estimated_market_value = round(float(median_price_per_m2) * surface, 0)

        recommended_max_bid = None
        if estimated_market_value is not None:
            recommended_max_bid = round(estimated_market_value * 0.95, 0)

        negotiation_margin_pct = None
        if asking is not None and asking > 0 and recommended_max_bid is not None:
            negotiation_margin_pct = round(((asking - recommended_max_bid) / asking) * 100.0, 2)

        return {
            "estimated_market_value": estimated_market_value,
            "recommended_max_bid": recommended_max_bid,
            "negotiation_margin_pct": negotiation_margin_pct,
            "summary": summary,
        }