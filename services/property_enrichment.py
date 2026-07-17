from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from models.property import Property
from services.location_service import LocationService
from services.permit_service import PermitService
from services.public_data_service import DutchPublicDataService


def _safe_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class PropertyEnrichmentItem:
    enrichment_key: str
    value: Any
    source: str
    retrieval_date: str
    confidence_score: int
    success: bool = True
    error_message: str | None = None
    raw_payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "enrichment_key": self.enrichment_key,
            "value": self.value,
            "source": self.source,
            "retrieval_date": self.retrieval_date,
            "confidence_score": self.confidence_score,
            "success": self.success,
            "error_message": self.error_message,
            "raw_payload": self.raw_payload,
        }


@dataclass(frozen=True)
class PropertyEnrichmentResult:
    property_id: str | None
    property_source_url: str | None
    items: list[PropertyEnrichmentItem] = field(default_factory=list)
    started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    completed_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return {
            "property_id": self.property_id,
            "property_source_url": self.property_source_url,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "items": [item.to_dict() for item in self.items],
        }

    def to_property_updates(self) -> dict[str, Any]:
        updates: dict[str, Any] = {}
        for item in self.items:
            if item.enrichment_key == "public_data" and isinstance(item.value, dict):
                public_data = item.value
                updates.update(
                    {
                        "bag_id": public_data.get("bag_id"),
                        "bag_address_id": public_data.get("bag_address_id"),
                        "bag_verblijfsobject_id": public_data.get("bag_verblijfsobject_id"),
                        "bag_nummeraanduiding_id": public_data.get("bag_nummeraanduiding_id"),
                        "bag_pand_id": public_data.get("bag_pand_id"),
                        "bag_building_year": public_data.get("bag_building_year") or public_data.get("construction_year_bag"),
                        "construction_year_bag": public_data.get("construction_year_bag") or public_data.get("bag_building_year"),
                        "bag_usage_purpose": public_data.get("bag_usage_purpose") or public_data.get("usage_purpose"),
                        "usage_purpose": public_data.get("usage_purpose") or public_data.get("bag_usage_purpose"),
                        "bag_status": public_data.get("bag_status") or public_data.get("status"),
                        "bag_official_floor_area_m2": public_data.get("bag_official_floor_area_m2") or public_data.get("official_floor_area_m2"),
                        "official_floor_area_m2": public_data.get("official_floor_area_m2") or public_data.get("bag_official_floor_area_m2"),
                        "bag_coordinates_rd": public_data.get("bag_coordinates_rd"),
                        "bag_coordinates_ll": public_data.get("bag_coordinates_ll"),
                        "coordinates": public_data.get("coordinates") or {"rd": public_data.get("bag_coordinates_rd"), "ll": public_data.get("bag_coordinates_ll")},
                        "bag_postcode": public_data.get("bag_postcode"),
                        "bag_municipality": public_data.get("bag_municipality"),
                        "bag_retrieval_date": public_data.get("bag_retrieval_date") or public_data.get("retrieval_date"),
                        "bag_source": public_data.get("bag_source") or public_data.get("source"),
                        "bag_confidence_score": public_data.get("bag_confidence_score") or public_data.get("confidence_score"),
                        "bag_quality_flags": public_data.get("bag_quality_flags") or public_data.get("quality_flags") or [],
                        "funda_living_area_m2": public_data.get("funda_living_area_m2"),
                        "living_area_difference_m2": public_data.get("living_area_difference_m2"),
                        "living_area_difference_percentage": public_data.get("living_area_difference_percentage"),
                        "calculation_area_m2": public_data.get("calculation_area_m2"),
                        "calculation_area_source": public_data.get("calculation_area_source"),
                        "asking_price_per_m2": public_data.get("asking_price_per_m2"),
                        "woz_value_per_m2": public_data.get("woz_value_per_m2"),
                        "woz_object_number": public_data.get("woz_object_number"),
                        "latest_woz_value": public_data.get("latest_woz_value"),
                        "woz_valuation_year": public_data.get("woz_valuation_year"),
                        "woz_historical_values": public_data.get("woz_historical_values") or [],
                    }
                )
            elif item.enrichment_key == "bag_coordinates" and isinstance(item.value, dict):
                updates["bag_coordinates_rd"] = item.value.get("rd")
                updates["bag_coordinates_ll"] = item.value.get("ll")
            else:
                updates[item.enrichment_key] = item.value
        return updates


class PropertyEnrichmentEngine:
    def __init__(
        self,
        location_service: LocationService | None = None,
        permit_service: PermitService | None = None,
        public_data_service: DutchPublicDataService | None = None,
    ) -> None:
        self.location_service = location_service or LocationService()
        self.permit_service = permit_service or PermitService()
        self.public_data_service = public_data_service or DutchPublicDataService()

    def enrich(self, property_obj: Property) -> PropertyEnrichmentResult:
        started_at = datetime.now(timezone.utc).isoformat()
        address = self._address(property_obj)
        tasks = self._build_tasks(property_obj, address)
        items: list[PropertyEnrichmentItem] = []

        async def _run_all() -> list[tuple[str, Any]]:
            async_tasks = [self._safe_await(key, coroutine_factory) for key, coroutine_factory in tasks]
            return await asyncio.gather(*async_tasks)

        try:
            results = asyncio.run(_run_all())
        except RuntimeError:
            results = self._run_sync_fallback(tasks)

        for key, result in results:
            if key == "public_data" and isinstance(result, dict):
                items.extend(self._expand_public_data_item(result))
                continue
            items.append(self._result_to_item(key, result))

        return PropertyEnrichmentResult(
            property_id=property_obj.listing_id,
            property_source_url=property_obj.source_url,
            items=items,
            started_at=started_at,
            completed_at=datetime.now(timezone.utc).isoformat(),
        )

    def enrich_to_dict(self, property_obj: Property) -> dict[str, Any]:
        return self.enrich(property_obj).to_dict()

    def _build_tasks(self, property_obj: Property, address: str) -> list[tuple[str, Callable[[], Awaitable[Any]]]]:
        return [
            ("public_data", lambda: self._lookup_public_data(property_obj)),
            ("municipality", lambda: self._lookup_municipality(property_obj, address)),
            ("postal_code", lambda: self._lookup_postal_code(property_obj, address)),
            ("distance_to_city_center", lambda: self._distance_enrichment("distance_to_city_center", 2.6, "Estimated city centre distance")),
            ("distance_to_station", lambda: self._distance_enrichment("distance_to_station", 1.4, "Estimated station distance")),
            ("distance_to_supermarket", lambda: self._distance_enrichment("distance_to_supermarket", 0.7, "Estimated supermarket distance")),
            ("distance_to_highway", lambda: self._distance_enrichment("distance_to_highway", 3.8, "Estimated highway distance")),
            ("neighborhood_m2_price_average", lambda: self._lookup_neighborhood_price(property_obj, address)),
            ("street_m2_price_average", lambda: self._lookup_street_price(property_obj, address)),
            ("monument_status", lambda: self._lookup_monument_status(property_obj, address)),
            ("land_registry_information", lambda: self._land_registry_placeholder(property_obj, address)),
            ("permit_information", lambda: self._placeholder("permit_information", "Not yet implemented", "placeholder", 0, success=True)),
            ("zoning", lambda: self._placeholder("zoning", "Not yet implemented", "placeholder", 0, success=True)),
        ]

    async def _lookup_public_data(self, property_obj: Property) -> dict[str, Any]:
        bag_result, woz_result = await asyncio.gather(
            self.public_data_service.fetch_bag_snapshot(property_obj),
            self.public_data_service.fetch_woz_snapshot(property_obj),
            return_exceptions=True,
        )

        bag_snapshot = self._coerce_bag_snapshot(bag_result)
        woz_snapshot = self._coerce_woz_snapshot(woz_result)
        area_metrics = self._area_and_price_metrics(property_obj, bag_snapshot, woz_snapshot)
        return {
            "bag": bag_snapshot,
            "woz": woz_snapshot,
            "metrics": area_metrics,
            "retrieval_date": datetime.now(timezone.utc).isoformat(),
            "source": "PDOK BAG WFS + Kadaster WOZ-waardeloket",
            "confidence_score": min(int(bag_snapshot.get("confidence_score") or 0), int(woz_snapshot.get("confidence_score") or 0)),
        }

    def _coerce_bag_snapshot(self, result: Any) -> dict[str, Any]:
        if isinstance(result, dict):
            return result
        if isinstance(result, Exception):
            return {
                "bag_id": None,
                "bag_address_id": None,
                "bag_verblijfsobject_id": None,
                "bag_nummeraanduiding_id": None,
                "bag_pand_id": None,
                "bag_building_year": None,
                "construction_year_bag": None,
                "bag_usage_purpose": None,
                "usage_purpose": None,
                "bag_status": None,
                "bag_official_floor_area_m2": None,
                "official_floor_area_m2": None,
                "bag_coordinates_rd": None,
                "bag_coordinates_ll": None,
                "coordinates": None,
                "bag_postcode": None,
                "bag_municipality": None,
                "quality_flags": ["bag_match_not_found", "low_confidence_match"],
                "source": "PDOK BAG WFS",
                "retrieval_date": datetime.now(timezone.utc).isoformat(),
                "confidence_score": 0,
                "raw_payload": {"error": f"{type(result).__name__}: {result}"},
            }
        return {
            "bag_id": None,
            "bag_address_id": None,
            "bag_verblijfsobject_id": None,
            "bag_nummeraanduiding_id": None,
            "bag_pand_id": None,
            "bag_building_year": None,
            "construction_year_bag": None,
            "bag_usage_purpose": None,
            "usage_purpose": None,
            "bag_status": None,
            "bag_official_floor_area_m2": None,
            "official_floor_area_m2": None,
            "bag_coordinates_rd": None,
            "bag_coordinates_ll": None,
            "coordinates": None,
            "bag_postcode": None,
            "bag_municipality": None,
            "quality_flags": ["bag_match_not_found", "low_confidence_match"],
            "source": "PDOK BAG WFS",
            "retrieval_date": datetime.now(timezone.utc).isoformat(),
            "confidence_score": 0,
            "raw_payload": {},
        }

    def _area_and_price_metrics(self, property_obj: Property, bag_snapshot: dict[str, Any], woz_snapshot: dict[str, Any]) -> dict[str, Any]:
        funda_living_area = _safe_float(property_obj.surface_m2)
        bag_area = _safe_float(bag_snapshot.get("bag_official_floor_area_m2"))
        asking_price = _safe_float(property_obj.asking_price)
        woz_value = _safe_float(woz_snapshot.get("latest_woz_value"))
        bag_confidence = int(bag_snapshot.get("confidence_score") or 0)
        usage = str(bag_snapshot.get("bag_usage_purpose") or "").lower()
        has_residential_match = bool(usage and "woon" in usage)

        use_bag_area = bool(has_residential_match and bag_confidence >= 70 and bag_area not in (None, 0))
        calculation_area = bag_area if use_bag_area else funda_living_area
        calculation_source = "BAG" if use_bag_area else "Funda"

        diff_m2 = None
        diff_pct = None
        quality_flags = list(bag_snapshot.get("quality_flags") or [])
        if funda_living_area not in (None, 0) and bag_area not in (None, 0):
            diff_m2 = round(float(funda_living_area) - float(bag_area), 2)
            diff_pct = round((diff_m2 / float(bag_area)) * 100.0, 2)
            if abs(diff_pct) > 10.0 and "funda_bag_area_difference_gt_10_pct" not in quality_flags:
                quality_flags.append("funda_bag_area_difference_gt_10_pct")

        asking_price_per_m2 = None
        woz_value_per_m2 = None
        if calculation_area not in (None, 0):
            if asking_price is not None:
                asking_price_per_m2 = round(float(asking_price) / float(calculation_area), 2)
            if woz_value is not None:
                woz_value_per_m2 = round(float(woz_value) / float(calculation_area), 2)

        return {
            "funda_living_area_m2": funda_living_area,
            "bag_official_floor_area_m2": bag_area,
            "living_area_difference_m2": diff_m2,
            "living_area_difference_percentage": diff_pct,
            "calculation_area_m2": calculation_area,
            "calculation_area_source": calculation_source,
            "asking_price_per_m2": asking_price_per_m2,
            "woz_value_per_m2": woz_value_per_m2,
            "bag_quality_flags": quality_flags,
        }

    def _coerce_woz_snapshot(self, result: Any) -> dict[str, Any]:
        if isinstance(result, dict):
            return result
        if isinstance(result, Exception):
            return {
                "woz_object_number": None,
                "latest_woz_value": None,
                "woz_valuation_year": None,
                "woz_historical_values": [],
                "source": "Kadaster WOZ-waardeloket",
                "retrieval_date": datetime.now(timezone.utc).isoformat(),
                "confidence_score": 0,
                "raw_payload": {"error": f"{type(result).__name__}: {result}"},
            }
        return {
            "woz_object_number": None,
            "latest_woz_value": None,
            "woz_valuation_year": None,
            "woz_historical_values": [],
            "source": "Kadaster WOZ-waardeloket",
            "retrieval_date": datetime.now(timezone.utc).isoformat(),
            "confidence_score": 0,
            "raw_payload": {},
        }

    async def _safe_await(self, key: str, coroutine_factory: Callable[[], Awaitable[Any]]) -> tuple[str, Any]:
        try:
            value = coroutine_factory()
            if asyncio.iscoroutine(value) or isinstance(value, asyncio.Future):
                value = await value
            return key, value
        except Exception as error:
            return key, error

    def _run_sync_fallback(self, tasks: list[tuple[str, Callable[[], Awaitable[Any]]]]) -> list[tuple[str, Any]]:
        results: list[tuple[str, Any]] = []
        for key, coroutine_factory in tasks:
            try:
                value = coroutine_factory()
                if asyncio.iscoroutine(value) or isinstance(value, asyncio.Future):
                    value = asyncio.run(value)
                results.append((key, value))
            except Exception as error:
                results.append((key, error))
        return results

    async def _lookup_woz_value(self, property_obj: Property, address: str) -> dict[str, Any]:
        snapshot = await self.public_data_service.fetch_woz_snapshot(property_obj)
        return self._build_success_payload(
            value=snapshot.get("latest_woz_value"),
            source=str(snapshot.get("source") or "Kadaster WOZ-waardeloket"),
            confidence_score=int(snapshot.get("confidence_score") or 0),
            raw_payload=snapshot.get("raw_payload") or {},
            success=snapshot.get("latest_woz_value") is not None,
        )

    async def _lookup_woz_valuation_year(self, property_obj: Property, address: str) -> dict[str, Any]:
        snapshot = await self.public_data_service.fetch_woz_snapshot(property_obj)
        return self._build_success_payload(
            value=snapshot.get("woz_valuation_year"),
            source=str(snapshot.get("source") or "Kadaster WOZ-waardeloket"),
            confidence_score=int(snapshot.get("confidence_score") or 0),
            raw_payload=snapshot.get("raw_payload") or {},
            success=snapshot.get("woz_valuation_year") is not None,
        )

    async def _lookup_woz_historical_values(self, property_obj: Property, address: str) -> dict[str, Any]:
        snapshot = await self.public_data_service.fetch_woz_snapshot(property_obj)
        values = snapshot.get("woz_historical_values") or []
        return self._build_success_payload(
            value=values,
            source=str(snapshot.get("source") or "Kadaster WOZ-waardeloket"),
            confidence_score=int(snapshot.get("confidence_score") or 0),
            raw_payload=snapshot.get("raw_payload") or {},
            success=bool(values),
        )

    async def _lookup_bag_id(self, property_obj: Property, address: str) -> dict[str, Any]:
        snapshot = await self.public_data_service.fetch_bag_snapshot(property_obj)
        return self._build_success_payload(
            value=snapshot.get("bag_id"),
            source=str(snapshot.get("source") or "PDOK BAG WFS"),
            confidence_score=int(snapshot.get("confidence_score") or 0),
            raw_payload=snapshot.get("raw_payload") or {},
            success=snapshot.get("bag_id") is not None,
        )

    async def _lookup_bag_building_year(self, property_obj: Property, address: str) -> dict[str, Any]:
        snapshot = await self.public_data_service.fetch_bag_snapshot(property_obj)
        return self._build_success_payload(
            value=snapshot.get("bag_building_year"),
            source=str(snapshot.get("source") or "PDOK BAG WFS"),
            confidence_score=int(snapshot.get("confidence_score") or 0),
            raw_payload=snapshot.get("raw_payload") or {},
            success=snapshot.get("bag_building_year") is not None,
        )

    async def _lookup_bag_usage_purpose(self, property_obj: Property, address: str) -> dict[str, Any]:
        snapshot = await self.public_data_service.fetch_bag_snapshot(property_obj)
        return self._build_success_payload(
            value=snapshot.get("bag_usage_purpose"),
            source=str(snapshot.get("source") or "PDOK BAG WFS"),
            confidence_score=int(snapshot.get("confidence_score") or 0),
            raw_payload=snapshot.get("raw_payload") or {},
            success=bool(snapshot.get("bag_usage_purpose")),
        )

    async def _lookup_bag_official_floor_area(self, property_obj: Property, address: str) -> dict[str, Any]:
        snapshot = await self.public_data_service.fetch_bag_snapshot(property_obj)
        return self._build_success_payload(
            value=snapshot.get("bag_official_floor_area_m2"),
            source=str(snapshot.get("source") or "PDOK BAG WFS"),
            confidence_score=int(snapshot.get("confidence_score") or 0),
            raw_payload=snapshot.get("raw_payload") or {},
            success=snapshot.get("bag_official_floor_area_m2") is not None,
        )

    async def _lookup_bag_coordinates(self, property_obj: Property, address: str) -> dict[str, Any]:
        snapshot = await self.public_data_service.fetch_bag_snapshot(property_obj)
        coordinates = {
            "rd": snapshot.get("bag_coordinates_rd"),
            "ll": snapshot.get("bag_coordinates_ll"),
        }
        return self._build_success_payload(
            value=coordinates,
            source=str(snapshot.get("source") or "PDOK BAG WFS"),
            confidence_score=int(snapshot.get("confidence_score") or 0),
            raw_payload=snapshot.get("raw_payload") or {},
            success=bool(coordinates.get("rd") or coordinates.get("ll")),
        )

    async def _lookup_bag_nummeraanduiding_id(self, property_obj: Property, address: str) -> dict[str, Any]:
        snapshot = await self.public_data_service.fetch_bag_snapshot(property_obj)
        return self._build_success_payload(
            value=snapshot.get("bag_nummeraanduiding_id"),
            source=str(snapshot.get("source") or "PDOK BAG WFS"),
            confidence_score=int(snapshot.get("confidence_score") or 0),
            raw_payload=snapshot.get("raw_payload") or {},
            success=snapshot.get("bag_nummeraanduiding_id") is not None,
        )

    async def _lookup_bag_pand_id(self, property_obj: Property, address: str) -> dict[str, Any]:
        snapshot = await self.public_data_service.fetch_bag_snapshot(property_obj)
        return self._build_success_payload(
            value=snapshot.get("bag_pand_id"),
            source=str(snapshot.get("source") or "PDOK BAG WFS"),
            confidence_score=int(snapshot.get("confidence_score") or 0),
            raw_payload=snapshot.get("raw_payload") or {},
            success=snapshot.get("bag_pand_id") is not None,
        )

    async def _lookup_bag_postcode(self, property_obj: Property, address: str) -> dict[str, Any]:
        snapshot = await self.public_data_service.fetch_bag_snapshot(property_obj)
        return self._build_success_payload(
            value=snapshot.get("bag_postcode"),
            source=str(snapshot.get("source") or "PDOK BAG WFS"),
            confidence_score=int(snapshot.get("confidence_score") or 0),
            raw_payload=snapshot.get("raw_payload") or {},
            success=bool(snapshot.get("bag_postcode")),
        )

    async def _lookup_bag_municipality(self, property_obj: Property, address: str) -> dict[str, Any]:
        snapshot = await self.public_data_service.fetch_bag_snapshot(property_obj)
        return self._build_success_payload(
            value=snapshot.get("bag_municipality"),
            source=str(snapshot.get("source") or "PDOK BAG WFS"),
            confidence_score=int(snapshot.get("confidence_score") or 0),
            raw_payload=snapshot.get("raw_payload") or {},
            success=bool(snapshot.get("bag_municipality")),
        )

    async def _lookup_street_price(self, property_obj: Property, address: str) -> dict[str, Any]:
        base = self._estimate_value(property_obj, 0.0)
        return self._build_success_payload(
            value=self._estimate_value(property_obj, 0.95),
            source="mock_street_price_estimate",
            confidence_score=30,
            raw_payload={"address": address, "base_value": base},
        )

    async def _lookup_neighborhood_price(self, property_obj: Property, address: str) -> dict[str, Any]:
        return self._build_success_payload(
            value=self._estimate_value(property_obj, 0.88),
            source="mock_neighborhood_price_estimate",
            confidence_score=28,
            raw_payload={"address": address},
        )

    async def _lookup_municipality(self, property_obj: Property, address: str) -> dict[str, Any]:
        municipality = property_obj.municipality or self._extract_municipality_from_address(address)
        return self._build_success_payload(
            value=municipality,
            source="property_model_or_address",
            confidence_score=80 if municipality else 20,
            raw_payload={"address": address},
        )

    async def _lookup_postal_code(self, property_obj: Property, address: str) -> dict[str, Any]:
        postal_code = property_obj.postal_code or self._extract_postal_code(address)
        return self._build_success_payload(
            value=postal_code,
            source="property_model_or_address",
            confidence_score=85 if postal_code else 10,
            raw_payload={"address": address},
        )

    async def _distance_enrichment(self, key: str, kilometers: float, label: str) -> dict[str, Any]:
        return self._build_success_payload(
            value={"distance_km": round(kilometers, 2), "label": label},
            source="mock_geospatial_estimate",
            confidence_score=45,
            raw_payload={"unit": "km", "distance_km": kilometers},
        )

    async def _lookup_monument_status(self, property_obj: Property, address: str) -> dict[str, Any]:
        if self._is_monument_text(property_obj.raw_text, property_obj.description):
            return self._build_success_payload(
                value="monument",
                source="listing_text_indicator",
                confidence_score=55,
                raw_payload={"address": address},
            )
        return self._build_success_payload(
            value="unknown",
            source="placeholder_lookup",
            confidence_score=15,
            raw_payload={"address": address},
        )

    async def _land_registry_placeholder(self, property_obj: Property, address: str) -> dict[str, Any]:
        return self._build_success_payload(
            value={"available": False, "notes": "Placeholder until Kadaster integration exists"},
            source="placeholder_land_registry",
            confidence_score=5,
            raw_payload={"address": address},
        )

    def _placeholder(self, key: str, value: Any, source: str, confidence_score: int, success: bool = True) -> dict[str, Any]:
        return self._build_success_payload(
            value=value,
            source=source,
            confidence_score=confidence_score,
            raw_payload={"enrichment_key": key},
            success=success,
        )

    def _build_success_payload(self, *, value: Any, source: str, confidence_score: int, raw_payload: dict[str, Any], success: bool = True) -> dict[str, Any]:
        return {
            "value": value,
            "source": source,
            "retrieval_date": datetime.now(timezone.utc).isoformat(),
            "confidence_score": int(max(0, min(100, confidence_score))),
            "success": success,
            "error_message": None,
            "raw_payload": raw_payload,
        }

    def _result_to_item(self, enrichment_key: str, result: Any) -> PropertyEnrichmentItem:
        if enrichment_key == "public_data" and isinstance(result, dict):
            return PropertyEnrichmentItem(
                enrichment_key=enrichment_key,
                value=result,
                source=str(result.get("source") or "public_data"),
                retrieval_date=str(result.get("retrieval_date") or datetime.now(timezone.utc).isoformat()),
                confidence_score=int(result.get("confidence_score") or 0),
                success=True,
                error_message=None,
                raw_payload={"bag": result.get("bag"), "woz": result.get("woz")},
            )
        if isinstance(result, Exception):
            return PropertyEnrichmentItem(
                enrichment_key=enrichment_key,
                value=None,
                source="error",
                retrieval_date=datetime.now(timezone.utc).isoformat(),
                confidence_score=0,
                success=False,
                error_message=f"{type(result).__name__}: {result}",
                raw_payload={"enrichment_key": enrichment_key},
            )

        if not isinstance(result, dict):
            return PropertyEnrichmentItem(
                enrichment_key=enrichment_key,
                value=result,
                source="unknown",
                retrieval_date=datetime.now(timezone.utc).isoformat(),
                confidence_score=0,
                success=True,
                raw_payload={"enrichment_key": enrichment_key},
            )

        return PropertyEnrichmentItem(
            enrichment_key=enrichment_key,
            value=result.get("value"),
            source=str(result.get("source") or "unknown"),
            retrieval_date=str(result.get("retrieval_date") or datetime.now(timezone.utc).isoformat()),
            confidence_score=int(result.get("confidence_score") or 0),
            success=bool(result.get("success", True)),
            error_message=result.get("error_message"),
            raw_payload=result.get("raw_payload") or {},
        )

    def _expand_public_data_item(self, result: dict[str, Any]) -> list[PropertyEnrichmentItem]:
        bag = result.get("bag") or {}
        woz = result.get("woz") or {}
        metrics = result.get("metrics") or {}
        retrieval_date = str(result.get("retrieval_date") or datetime.now(timezone.utc).isoformat())
        source = str(result.get("source") or "public_data")
        confidence_score = int(result.get("confidence_score") or 0)

        def item(key: str, value: Any, raw_payload: dict[str, Any], success: bool = True) -> PropertyEnrichmentItem:
            return PropertyEnrichmentItem(
                enrichment_key=key,
                value=value,
                source=source,
                retrieval_date=retrieval_date,
                confidence_score=confidence_score,
                success=success,
                error_message=None,
                raw_payload=raw_payload,
            )

        bag_raw = bag.get("raw_payload") if isinstance(bag, dict) else {}
        woz_raw = woz.get("raw_payload") if isinstance(woz, dict) else {}

        return [
            item("bag_id", bag.get("bag_id"), bag_raw, bag.get("bag_id") is not None),
            item("bag_address_id", bag.get("bag_address_id"), bag_raw, bag.get("bag_address_id") is not None),
            item("bag_verblijfsobject_id", bag.get("bag_verblijfsobject_id"), bag_raw, bag.get("bag_verblijfsobject_id") is not None),
            item("bag_nummeraanduiding_id", bag.get("bag_nummeraanduiding_id"), bag_raw, bag.get("bag_nummeraanduiding_id") is not None),
            item("bag_pand_id", bag.get("bag_pand_id"), bag_raw, bag.get("bag_pand_id") is not None),
            item("bag_building_year", bag.get("bag_building_year"), bag_raw, bag.get("bag_building_year") is not None),
            item("bag_usage_purpose", bag.get("bag_usage_purpose"), bag_raw, bool(bag.get("bag_usage_purpose"))),
            item("bag_status", bag.get("status") or bag.get("bag_status"), bag_raw, bool(bag.get("status") or bag.get("bag_status"))),
            item("bag_official_floor_area_m2", bag.get("bag_official_floor_area_m2"), bag_raw, bag.get("bag_official_floor_area_m2") is not None),
            item("bag_coordinates", {"rd": bag.get("bag_coordinates_rd"), "ll": bag.get("bag_coordinates_ll")}, bag_raw, bool(bag.get("bag_coordinates_rd") or bag.get("bag_coordinates_ll"))),
            item("bag_postcode", bag.get("bag_postcode"), bag_raw, bool(bag.get("bag_postcode"))),
            item("bag_municipality", bag.get("bag_municipality"), bag_raw, bool(bag.get("bag_municipality"))),
            item("bag_retrieval_date", bag.get("retrieval_date"), bag_raw, bool(bag.get("retrieval_date"))),
            item("bag_source", bag.get("source"), bag_raw, bool(bag.get("source"))),
            item("bag_confidence_score", bag.get("confidence_score"), bag_raw, bag.get("confidence_score") is not None),
            item("bag_quality_flags", metrics.get("bag_quality_flags") or bag.get("quality_flags") or [], bag_raw, True),
            item("funda_living_area_m2", metrics.get("funda_living_area_m2"), bag_raw, metrics.get("funda_living_area_m2") is not None),
            item("living_area_difference_m2", metrics.get("living_area_difference_m2"), bag_raw, metrics.get("living_area_difference_m2") is not None),
            item("living_area_difference_percentage", metrics.get("living_area_difference_percentage"), bag_raw, metrics.get("living_area_difference_percentage") is not None),
            item("calculation_area_m2", metrics.get("calculation_area_m2"), bag_raw, metrics.get("calculation_area_m2") is not None),
            item("calculation_area_source", metrics.get("calculation_area_source"), bag_raw, bool(metrics.get("calculation_area_source"))),
            item("asking_price_per_m2", metrics.get("asking_price_per_m2"), bag_raw, metrics.get("asking_price_per_m2") is not None),
            item("woz_value_per_m2", metrics.get("woz_value_per_m2"), woz_raw, metrics.get("woz_value_per_m2") is not None),
            item("woz_object_number", woz.get("woz_object_number"), woz_raw, woz.get("woz_object_number") is not None),
            item("latest_woz_value", woz.get("latest_woz_value"), woz_raw, woz.get("latest_woz_value") is not None),
            item("woz_valuation_year", woz.get("woz_valuation_year"), woz_raw, woz.get("woz_valuation_year") is not None),
            item("woz_historical_values", woz.get("woz_historical_values") or [], woz_raw, bool(woz.get("woz_historical_values"))),
        ]

    def _address(self, property_obj: Property) -> str:
        parts = [property_obj.address, property_obj.city]
        return ", ".join(part for part in parts if isinstance(part, str) and part.strip())

    def _extract_postal_code(self, address: str) -> str | None:
        tokens = address.replace("-", " ").split()
        for token in tokens:
            normalized = token.upper().replace(" ", "")
            if len(normalized) == 6 and normalized[:4].isdigit() and normalized[4:].isalpha():
                return f"{normalized[:4]} {normalized[4:]}"
        return None

    def _extract_municipality_from_address(self, address: str) -> str | None:
        if not address:
            return None
        parts = [part.strip() for part in address.split(",") if part.strip()]
        return parts[-1] if parts else None

    def _estimate_value(self, property_obj: Property, multiplier: float) -> float | None:
        base = property_obj.asking_price or (property_obj.surface_m2 or 0) * 4500
        if not base:
            return None
        return round(float(base) * multiplier, 2)

    def _is_monument_text(self, raw_text: str | None, description: str | None) -> bool:
        combined = f"{raw_text or ''} {description or ''}".lower()
        return any(term in combined for term in ["monument", "rijksmonument", "gemeentelijk monument", "beschermd stads- of dorpsgezicht"])
