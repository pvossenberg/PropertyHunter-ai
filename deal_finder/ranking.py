from __future__ import annotations

from typing import Any

from deal_finder.models import NormalizedListing, RankingResult


def rank_listing(listing: NormalizedListing, context: dict[str, Any] | None = None) -> RankingResult:
    context = context or {}

    score = 0
    reasons: list[str] = []
    missing: list[str] = []

    if listing.asking_price is not None:
        if listing.asking_price <= 350000:
            score += 20
            reasons.append("lower_asking_price")
        elif listing.asking_price <= 600000:
            score += 12
            reasons.append("mid_asking_price")
        else:
            score += 4
            reasons.append("higher_asking_price")
    else:
        missing.append("asking_price_missing")

    price_per_m2 = context.get("price_per_m2")
    if isinstance(price_per_m2, (int, float)):
        if price_per_m2 <= 3500:
            score += 18
            reasons.append("competitive_price_per_m2")
        elif price_per_m2 <= 5000:
            score += 10
            reasons.append("reasonable_price_per_m2")
        else:
            score += 3
            reasons.append("high_price_per_m2")
    else:
        missing.append("price_per_m2_missing")

    days_on_market = context.get("days_on_market")
    if isinstance(days_on_market, (int, float)):
        if days_on_market >= 120:
            score += 12
            reasons.append("long_time_on_market")
        elif days_on_market >= 45:
            score += 7
            reasons.append("moderate_time_on_market")
    else:
        missing.append("days_on_market_missing")

    price_reductions = context.get("price_reduction_count")
    if isinstance(price_reductions, (int, float)) and price_reductions > 0:
        score += min(12, int(price_reductions) * 4)
        reasons.append("price_reduction_signal")
    else:
        missing.append("price_reduction_data_missing")

    investment_score = context.get("investment_score")
    if isinstance(investment_score, (int, float)):
        score += max(0, min(25, int(investment_score / 4)))
        reasons.append("investment_score_used")
    else:
        missing.append("investment_score_missing")

    if listing.property_type:
        if "appartement" in listing.property_type.lower() or "woning" in listing.property_type.lower():
            score += 6
            reasons.append("known_property_type")
        else:
            score += 3
            reasons.append("other_property_type")
    else:
        missing.append("property_type_missing")

    if listing.city:
        score += 5
        reasons.append("city_present")
    else:
        missing.append("city_missing")

    filled_fields = 0
    tracked_fields = [listing.title, listing.address, listing.city, listing.asking_price, listing.surface_m2, listing.property_type, listing.description]
    for value in tracked_fields:
        if value not in (None, ""):
            filled_fields += 1
    completeness_ratio = filled_fields / len(tracked_fields)
    score += int(completeness_ratio * 10)
    reasons.append("data_completeness")

    score = max(0, min(100, score))
    priority = _priority_from_score(score)
    return RankingResult(candidate_score=score, priority=priority, reason_codes=reasons, missing_data_warnings=sorted(set(missing)))


def _priority_from_score(score: int) -> str:
    if score >= 85:
        return "urgent"
    if score >= 70:
        return "high"
    if score >= 45:
        return "medium"
    return "low"
