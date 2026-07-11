from __future__ import annotations

from dataclasses import dataclass, field
from typing import List


@dataclass
class InvestmentProfile:
    name: str = "Standaard profiel"
    max_purchase_price: float | None = None
    min_surface_m2: float | None = None
    max_price_per_m2: float | None = None
    min_gross_yield: float | None = None
    preferred_cities: List[str] = field(default_factory=list)
    preferred_property_types: List[str] = field(default_factory=list)
    require_transformation_potential: bool = False
    require_residential_zoning: bool = False
    weight_location: float = 0.25
    weight_price: float = 0.25
    weight_yield: float = 0.2
    weight_transformation: float = 0.15
    weight_risk: float = 0.15
