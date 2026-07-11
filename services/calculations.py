from __future__ import annotations

from datetime import date, datetime


def calculate_days_on_market(listed_since, reference_date=None):
    if listed_since in (None, ""):
        return None
    try:
        listed_date = listed_since if isinstance(listed_since, date) else datetime.fromisoformat(str(listed_since)).date()
    except (TypeError, ValueError):
        return None
    if reference_date is None:
        reference_date = date.today()
    try:
        reference = reference_date if isinstance(reference_date, date) else datetime.fromisoformat(str(reference_date)).date()
    except (TypeError, ValueError):
        return None
    if reference < listed_date:
        return None
    return (reference - listed_date).days


def calculate_price_per_m2(asking_price, surface_m2, asking_price_status=None):
    if asking_price is None or asking_price_status in {"on_request", "unknown"} or surface_m2 in (None, 0):
        return None
    try:
        return round(float(asking_price) / float(surface_m2), 2)
    except (TypeError, ValueError):
        return None


def calculate_gross_yield(annual_rent, asking_price, asking_price_status=None):
    if annual_rent is None or asking_price is None or asking_price_status in {"on_request", "unknown"} or asking_price in (0, "0"):
        return None
    try:
        return round((float(annual_rent) / float(asking_price)) * 100, 2)
    except (TypeError, ValueError):
        return None


def calculate_price_reduction(original_price, current_price):
    if original_price in (None, "") or current_price in (None, ""):
        return None
    try:
        original = float(original_price)
        current = float(current_price)
    except (TypeError, ValueError):
        return None
    if original <= 0 or current <= 0:
        return None
    if current >= original:
        return None
    return {
        "amount": round(original - current, 2),
        "percentage": round(((original - current) / original) * 100, 2),
    }


def calculate_price_change_since_last_transaction(current_price, previous_transaction_price):
    if current_price in (None, "") or previous_transaction_price in (None, ""):
        return None
    try:
        current = float(current_price)
        previous = float(previous_transaction_price)
    except (TypeError, ValueError):
        return None
    if previous <= 0 or current <= 0:
        return None
    return {
        "amount": round(current - previous, 2),
        "percentage": round(((current - previous) / previous) * 100, 2),
    }


def calculate_discount_percentage(object_price_per_m2, market_price_per_m2):
    if object_price_per_m2 in (None, 0) or market_price_per_m2 in (None, 0):
        return None
    try:
        return round(((float(market_price_per_m2) - float(object_price_per_m2)) / float(market_price_per_m2)) * 100, 2)
    except (TypeError, ValueError):
        return None
