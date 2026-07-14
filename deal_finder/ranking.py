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

    investment_points, investment_reason = _score_investment_signals(context)
    if investment_points is not None:
        score += investment_points
        reasons.append(investment_reason)
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


def _score_investment_signals(context: dict[str, Any]) -> tuple[int | None, str]:
    direct_score = _extract_direct_investment_score(context)
    if direct_score is not None:
        # Direct investment score is expected on a 0-100 scale and maps to max 25 ranking points.
        normalized = max(0.0, min(100.0, float(direct_score)))
        return int(round((normalized / 100.0) * 25.0)), "investment_score_used"

    yield_pct = _extract_yield_percentage(context)
    if yield_pct is None:
        return None, "investment_score_missing"

    if yield_pct >= 10.0:
        return 25, "yield_signal_strong"
    if yield_pct >= 8.0:
        return 21, "yield_signal_good"
    if yield_pct >= 6.0:
        return 16, "yield_signal_moderate"
    if yield_pct >= 4.5:
        return 10, "yield_signal_weak"
    if yield_pct > 0:
        return 5, "yield_signal_low"
    return 0, "yield_signal_none"


def _extract_direct_investment_score(context: dict[str, Any]) -> float | None:
    candidates = [
        context.get("investment_score"),
        context.get("score"),
        (context.get("analysis") or {}).get("investment_score") if isinstance(context.get("analysis"), dict) else None,
    ]
    for value in candidates:
        if isinstance(value, (int, float)):
            return float(value)
    return None


def _extract_yield_percentage(context: dict[str, Any]) -> float | None:
    for key in ("gross_yield", "yield", "cap_rate", "roi"):
        value = context.get(key)
        if not isinstance(value, (int, float)):
            continue
        pct = float(value)
        # Normalize ratio inputs (e.g. 0.065) into percentages.
        if 0 < pct <= 1:
            pct *= 100.0
        return pct
    return None
