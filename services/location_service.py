from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Protocol

from models.area_development import AreaDevelopmentRecord
from models.government_record import GovernmentRecord
from models.neighborhood import NeighborhoodProfile
from models.news_record import NewsRecord
from services.data_provenance import DataProvenance


@dataclass(frozen=True)
class LocationResponseMeta:
    """Metadata envelope for location-service responses."""

    address: str
    source_name: str
    source_url: str | None
    retrieved_at: str
    confidence: str
    is_mock_data: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "address": self.address,
            "source_name": self.source_name,
            "source_url": self.source_url,
            "retrieved_at": self.retrieved_at,
            "confidence": self.confidence,
            "is_mock_data": self.is_mock_data,
        }


class LocationDataProvider(Protocol):
    """Provider contract for neighborhood/location intelligence.

    Real providers can later bridge to CBS, BAG, municipal publications,
    official announcements, and news APIs without changing service method signatures.
    """

    def get_neighborhood_profile(self, normalized_address: str) -> NeighborhoodProfile | None:
        """Return neighborhood profile for a normalized address."""

    def get_area_developments(self, normalized_address: str) -> list[AreaDevelopmentRecord]:
        """Return area development records near the property."""

    def get_government_records(self, normalized_address: str) -> list[GovernmentRecord]:
        """Return municipal/provincial governance records for the location."""

    def get_local_news(self, normalized_address: str) -> list[NewsRecord]:
        """Return relevant local news records for the location."""


class MockLocationDataProvider:
    """Mock provider with realistic but non-verified location data."""

    def __init__(self) -> None:
        now = datetime.now(timezone.utc)

        self._profiles: dict[str, NeighborhoodProfile] = {
            "keizersgracht 123 amsterdam": NeighborhoodProfile(
                neighborhood_name="Grachtengordel-Zuid",
                district_name="Centrum",
                municipality="Amsterdam",
                province="Noord-Holland",
                population=14800,
                population_growth_percentage=1.4,
                household_count=9100,
                average_household_size=1.63,
                age_distribution={"0-17": 10.5, "18-34": 26.7, "35-64": 42.1, "65+": 20.7},
                household_composition={"single": 58.0, "couple": 28.0, "family": 14.0},
                average_income=51200,
                owner_occupied_percentage=34.0,
                rental_percentage=66.0,
                social_rental_percentage=18.0,
                business_count=2350,
                dominant_business_sectors=["hospitality", "professional_services", "retail"],
                vacancy_indicator=3.2,
                crime_indicator=5.8,
                livability_indicator=7.6,
                public_transport_score=88,
                amenities_score=92,
                retail_score=90,
                hospitality_score=94,
                education_score=74,
                healthcare_score=79,
                green_space_score=61,
                source_name="Mock CBS/BAG neighborhood blend",
                source_url=None,
                retrieved_at=now,
                confidence="medium",
                data_quality_warnings=["Mock profile. Validate indicators with official CBS neighborhood datasets."],
                is_mock_data=True,
            ),
            "stationsweg 1 lelystad": NeighborhoodProfile(
                neighborhood_name=None,
                district_name="Stationsgebied",
                municipality="Lelystad",
                province="Flevoland",
                source_name="Mock neighborhood lookup",
                source_url=None,
                retrieved_at=now,
                confidence="low",
                data_quality_warnings=["Neighborhood could not be matched with high confidence."],
                is_mock_data=True,
            ),
            "coolsingel 50 rotterdam": NeighborhoodProfile(
                neighborhood_name="Stadsdriehoek",
                district_name="Centrum",
                municipality="Rotterdam",
                province="Zuid-Holland",
                source_name="Mock CBS/BAG neighborhood blend",
                source_url=None,
                retrieved_at=now,
                confidence="low",
                data_quality_warnings=["Partial mock profile only; socioeconomic fields incomplete."],
                is_mock_data=True,
            ),
        }

        self._developments: dict[str, list[AreaDevelopmentRecord]] = {
            "keizersgracht 123 amsterdam": [
                AreaDevelopmentRecord(
                    title="Herinrichting tramhalte en looproutes Centrumring",
                    development_type="public_transport",
                    description="Betere OV-knooppunten en voetgangersdoorstroming in binnenstad.",
                    status="in_progress",
                    announcement_date=date(2025, 2, 10),
                    expected_start_date=date(2025, 9, 1),
                    expected_completion_date=date(2027, 6, 1),
                    authority="Gemeente Amsterdam",
                    source_name="Mock municipal mobility program",
                    source_url=None,
                    distance_to_property_meters=420,
                    expected_impact="Betere bereikbaarheid en passantenstromen.",
                    impact_direction="positive",
                    confidence="medium",
                    notes="Mock development record; validate timeline in official project notices.",
                    is_mock_data=True,
                    retrieved_at=now,
                ),
                AreaDevelopmentRecord(
                    title="Nachtelijke kadeversterking fase 2",
                    development_type="infrastructure",
                    description="Versterking kademuur met tijdelijke verkeershinder.",
                    status="approved",
                    announcement_date=date(2025, 5, 8),
                    expected_start_date=date(2026, 1, 15),
                    expected_completion_date=date(2026, 11, 1),
                    authority="Gemeente Amsterdam",
                    source_name="Mock municipal infrastructure bulletin",
                    source_url=None,
                    distance_to_property_meters=180,
                    expected_impact="Tijdelijke hinder kan verhuurbaarheid op korte termijn drukken.",
                    impact_direction="negative",
                    confidence="low",
                    notes="Mock record; scope and nuisance assumptions must be verified.",
                    is_mock_data=True,
                    retrieved_at=now,
                ),
            ],
            "coolsingel 50 rotterdam": [],
            "stationsweg 1 lelystad": [],
        }

        self._government_records: dict[str, list[GovernmentRecord]] = {
            "keizersgracht 123 amsterdam": [
                GovernmentRecord(
                    meeting_date=date(2025, 6, 19),
                    governing_body="municipal_council",
                    document_type="raadsvoorstel",
                    title="Actualisatie binnenstadstransformatie en functiemenging",
                    summary="Voorstel over verruiming gemengd gebruik in delen van de binnenstad.",
                    status="under_review",
                    themes=["zoning", "mixed_use", "housing"],
                    geographic_scope="Amsterdam Centrum",
                    source_url=None,
                    relevance_to_property="Kan toekomstige gebruiksmogelijkheden verruimen.",
                    expected_investment_impact="Potentieel positief, afhankelijk van definitieve besluitvorming.",
                    confidence="low",
                    is_mock_data=True,
                    source_name="Mock gemeenteraadsstukken feed",
                    retrieved_at=now,
                )
            ],
            "coolsingel 50 rotterdam": [],
            "stationsweg 1 lelystad": [],
        }

        self._news_records: dict[str, list[NewsRecord]] = {
            "keizersgracht 123 amsterdam": [
                NewsRecord(
                    publication_date=date(2025, 7, 3),
                    publisher="Het Lokale Handelsblad",
                    title="Meer internationale vraag naar centrumwoningen",
                    summary="Makelaars signaleren aanhoudende vraag in centrumsegment.",
                    source_url=None,
                    geographic_scope="Amsterdam Centrum",
                    themes=["housing_market", "international_demand"],
                    sentiment="positive",
                    investment_relevance="Positief voor exit-liquiditeit in middellange termijn.",
                    confidence="low",
                    is_mock_data=True,
                    source_name="Mock local news stream",
                    retrieved_at=now,
                )
            ],
            "coolsingel 50 rotterdam": [],
            "stationsweg 1 lelystad": [],
        }

    def get_neighborhood_profile(self, normalized_address: str) -> NeighborhoodProfile | None:
        return self._profiles.get(normalized_address)

    def get_area_developments(self, normalized_address: str) -> list[AreaDevelopmentRecord]:
        return list(self._developments.get(normalized_address, []))

    def get_government_records(self, normalized_address: str) -> list[GovernmentRecord]:
        return list(self._government_records.get(normalized_address, []))

    def get_local_news(self, normalized_address: str) -> list[NewsRecord]:
        return list(self._news_records.get(normalized_address, []))


class LocationService:
    """Engine 4 location analysis service independent from UI frameworks.

    The public API is stable and provider-agnostic, allowing later integration with
    official data sources without changing UI integration points.
    """

    def __init__(self, provider: LocationDataProvider | None = None) -> None:
        self._provider = provider or MockLocationDataProvider()

    def get_neighborhood_profile(self, address: str) -> dict[str, Any]:
        """Return a neighborhood profile response for the given address."""
        normalized = self._normalize_address(address)
        if normalized is None:
            return self._invalid_address_response(address)

        profile = self._provider.get_neighborhood_profile(normalized)
        warnings: list[str] = []

        if profile is None:
            warnings.append("No neighborhood profile found for this address in current provider data.")

        return {
            **self._meta(address, confidence=(profile.confidence if profile else "unknown")).to_dict(),
            "data_quality_warnings": self._mock_warning_prefix() + warnings + (profile.data_quality_warnings if profile else []),
            "neighborhood_profile": profile,
        }

    def get_area_developments(self, address: str) -> dict[str, Any]:
        """Return area development records around the provided address."""
        normalized = self._normalize_address(address)
        if normalized is None:
            return self._invalid_address_response(address)

        developments = self._provider.get_area_developments(normalized)
        warnings = [] if developments else ["No area developments found for this address in current provider data."]

        return {
            **self._meta(address).to_dict(),
            "data_quality_warnings": self._mock_warning_prefix() + warnings,
            "area_developments": developments,
        }

    def get_government_records(self, address: str) -> dict[str, Any]:
        """Return municipal/provincial governance records for the address."""
        normalized = self._normalize_address(address)
        if normalized is None:
            return self._invalid_address_response(address)

        records = self._provider.get_government_records(normalized)
        warnings = [] if records else ["No government records found for this address in current provider data."]

        return {
            **self._meta(address).to_dict(),
            "data_quality_warnings": self._mock_warning_prefix() + warnings,
            "government_records": records,
        }

    def get_local_news(self, address: str) -> dict[str, Any]:
        """Return local news records with potential investment relevance."""
        normalized = self._normalize_address(address)
        if normalized is None:
            return self._invalid_address_response(address)

        news = self._provider.get_local_news(normalized)
        warnings = [] if news else ["No local news records found for this address in current provider data."]

        return {
            **self._meta(address).to_dict(),
            "data_quality_warnings": self._mock_warning_prefix() + warnings,
            "local_news": news,
        }

    def get_location_summary(self, address: str) -> dict[str, Any]:
        """Return combined location analysis results for the address."""
        normalized = self._normalize_address(address)
        if normalized is None:
            return {
                **self._meta(address, confidence="unknown").to_dict(),
                "neighborhood_profile": None,
                "area_developments": [],
                "government_records": [],
                "local_news": [],
                "data_quality_warnings": self._mock_warning_prefix() + ["Invalid or empty address input."],
            }

        neighborhood_payload = self.get_neighborhood_profile(address)
        developments_payload = self.get_area_developments(address)
        government_payload = self.get_government_records(address)
        news_payload = self.get_local_news(address)

        warnings: list[str] = []
        for payload in (neighborhood_payload, developments_payload, government_payload, news_payload):
            warnings.extend(payload.get("data_quality_warnings", []))

        confidence = self._derive_confidence(
            neighborhood_payload.get("confidence", "unknown"),
            developments_payload.get("confidence", "unknown"),
            government_payload.get("confidence", "unknown"),
            news_payload.get("confidence", "unknown"),
        )

        return {
            **self._meta(address, confidence=confidence).to_dict(),
            "neighborhood_profile": neighborhood_payload.get("neighborhood_profile"),
            "area_developments": developments_payload.get("area_developments", []),
            "government_records": government_payload.get("government_records", []),
            "local_news": news_payload.get("local_news", []),
            "data_quality_warnings": self._unique_preserve_order(warnings),
        }

    def _meta(self, address: str, confidence: str = "low") -> LocationResponseMeta:
        provenance = DataProvenance.from_value(
            raw_value=address,
            normalized_value=self._normalize_address(address),
            source_name=self._provider.__class__.__name__,
            source_url=None,
            confidence=confidence if confidence in {"high", "medium", "low", "unknown"} else "unknown",
        )

        return LocationResponseMeta(
            address=address,
            source_name=provenance.source_name or "unknown",
            source_url=provenance.source_url,
            retrieved_at=provenance.retrieved_at or datetime.now(timezone.utc).isoformat(),
            confidence=provenance.confidence,
            is_mock_data=True,
        )

    def _normalize_address(self, address: str) -> str | None:
        if not isinstance(address, str) or not address.strip():
            return None
        return " ".join(address.lower().split())

    def _invalid_address_response(self, address: str) -> dict[str, Any]:
        return {
            **self._meta(address, confidence="unknown").to_dict(),
            "data_quality_warnings": self._mock_warning_prefix() + ["Invalid or empty address input."],
            "neighborhood_profile": None,
            "area_developments": [],
            "government_records": [],
            "local_news": [],
        }

    def _derive_confidence(self, *values: str) -> str:
        ordered = ["high", "medium", "low", "unknown"]
        for key in ordered:
            if key in values:
                return key
        return "unknown"

    def _mock_warning_prefix(self) -> list[str]:
        return ["Mock data only. Do not treat location outputs as verified facts."]

    def _unique_preserve_order(self, values: list[str]) -> list[str]:
        seen: set[str] = set()
        result: list[str] = []
        for value in values:
            if value not in seen:
                seen.add(value)
                result.append(value)
        return result
