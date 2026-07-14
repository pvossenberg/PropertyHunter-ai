from __future__ import annotations

from datetime import date, datetime


def _as_float(value):
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


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


def calculate_acquisition_costs(asking_price, acquisition_cost_percentage, asking_price_status=None):
    if asking_price_status in {"on_request", "unknown"}:
        return None
    price = _as_float(asking_price)
    percentage = _as_float(acquisition_cost_percentage)
    if price is None or percentage is None or price <= 0 or percentage < 0:
        return None
    return round(price * (percentage / 100.0), 2)


def calculate_total_initial_investment(asking_price, acquisition_costs, renovation_budget=0, asking_price_status=None):
    if asking_price_status in {"on_request", "unknown"}:
        return None
    price = _as_float(asking_price)
    costs = _as_float(acquisition_costs)
    renovation = _as_float(renovation_budget)
    if price is None or price <= 0:
        return None
    if costs is None:
        costs = 0.0
    if renovation is None:
        renovation = 0.0
    if costs < 0 or renovation < 0:
        return None
    return round(price + costs + renovation, 2)


def calculate_financing_amount(purchase_price, ltv_percentage, asking_price_status=None):
    if asking_price_status in {"on_request", "unknown"}:
        return None
    price = _as_float(purchase_price)
    ltv = _as_float(ltv_percentage)
    if price is None or ltv is None or price <= 0 or ltv < 0:
        return None
    return round(price * (ltv / 100.0), 2)


def calculate_annual_interest_cost(financing_amount, interest_rate_percentage):
    financing = _as_float(financing_amount)
    rate = _as_float(interest_rate_percentage)
    if financing is None or rate is None or financing < 0 or rate < 0:
        return None
    return round(financing * (rate / 100.0), 2)


def calculate_annual_cashflow_before_tax(annual_rent, annual_interest_cost):
    rent = _as_float(annual_rent)
    interest_cost = _as_float(annual_interest_cost)
    if rent is None or interest_cost is None:
        return None
    return round(rent - interest_cost, 2)


def calculate_required_rent_for_target_yield(total_initial_investment, target_gross_yield_percentage):
    total_investment = _as_float(total_initial_investment)
    target_yield = _as_float(target_gross_yield_percentage)
    if total_investment is None or target_yield is None or total_investment <= 0 or target_yield <= 0:
        return None
    return round(total_investment * (target_yield / 100.0), 2)


def calculate_maximum_purchase_price(annual_rent, target_gross_yield_percentage, acquisition_cost_percentage=0, renovation_budget=0):
    rent = _as_float(annual_rent)
    target_yield = _as_float(target_gross_yield_percentage)
    acquisition_pct = _as_float(acquisition_cost_percentage)
    renovation = _as_float(renovation_budget)
    if rent is None or target_yield is None or acquisition_pct is None or renovation is None:
        return None
    if rent <= 0 or target_yield <= 0 or acquisition_pct < 0 or renovation < 0:
        return None
    gross_multiple = 1.0 + (acquisition_pct / 100.0)
    if gross_multiple <= 0:
        return None
    target_investment = rent / (target_yield / 100.0)
    max_price = (target_investment - renovation) / gross_multiple
    if max_price <= 0:
        return None
    return round(max_price, 2)
