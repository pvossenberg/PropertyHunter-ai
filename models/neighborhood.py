from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional


CONFIDENCE_LEVELS = {"high", "medium", "low", "unknown"}


@dataclass
class NeighborhoodProfile:
    neighborhood_name: Optional[str] = None
    district_name: Optional[str] = None
    municipality: Optional[str] = None
    province: Optional[str] = None
    country: str = "Nederland"
    population: Optional[int] = None
    population_growth_percentage: Optional[float] = None
    household_count: Optional[int] = None
    average_household_size: Optional[float] = None
    age_distribution: dict[str, float] = field(default_factory=dict)
    household_composition: dict[str, float] = field(default_factory=dict)
    average_income: Optional[float] = None
    owner_occupied_percentage: Optional[float] = None
    rental_percentage: Optional[float] = None
    social_rental_percentage: Optional[float] = None
    business_count: Optional[int] = None
    dominant_business_sectors: list[str] = field(default_factory=list)
    vacancy_indicator: Optional[float] = None
    crime_indicator: Optional[float] = None
    livability_indicator: Optional[float] = None
    public_transport_score: Optional[int] = None
    amenities_score: Optional[int] = None
    retail_score: Optional[int] = None
    hospitality_score: Optional[int] = None
    education_score: Optional[int] = None
    healthcare_score: Optional[int] = None
    green_space_score: Optional[int] = None
    source_name: Optional[str] = None
    source_url: Optional[str] = None
    retrieved_at: Optional[datetime] = None
    confidence: str = "unknown"
    data_quality_warnings: list[str] = field(default_factory=list)
    is_mock_data: bool = True

    def __post_init__(self) -> None:
        if self.confidence not in CONFIDENCE_LEVELS:
            self.confidence = "unknown"

    def to_dict(self) -> dict[str, Any]:
        return {
            "neighborhood_name": self.neighborhood_name,
            "district_name": self.district_name,
            "municipality": self.municipality,
            "province": self.province,
            "country": self.country,
            "population": self.population,
            "population_growth_percentage": self.population_growth_percentage,
            "household_count": self.household_count,
            "average_household_size": self.average_household_size,
            "age_distribution": dict(self.age_distribution),
            "household_composition": dict(self.household_composition),
            "average_income": self.average_income,
            "owner_occupied_percentage": self.owner_occupied_percentage,
            "rental_percentage": self.rental_percentage,
            "social_rental_percentage": self.social_rental_percentage,
            "business_count": self.business_count,
            "dominant_business_sectors": list(self.dominant_business_sectors),
            "vacancy_indicator": self.vacancy_indicator,
            "crime_indicator": self.crime_indicator,
            "livability_indicator": self.livability_indicator,
            "public_transport_score": self.public_transport_score,
            "amenities_score": self.amenities_score,
            "retail_score": self.retail_score,
            "hospitality_score": self.hospitality_score,
            "education_score": self.education_score,
            "healthcare_score": self.healthcare_score,
            "green_space_score": self.green_space_score,
            "source_name": self.source_name,
            "source_url": self.source_url,
            "retrieved_at": self.retrieved_at.isoformat() if isinstance(self.retrieved_at, datetime) else None,
            "confidence": self.confidence,
            "data_quality_warnings": list(self.data_quality_warnings),
            "is_mock_data": self.is_mock_data,
        }
