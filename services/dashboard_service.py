from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from services.database import DatabaseService


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _normalize_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _first_text(*values: Any) -> str | None:
    for value in values:
        text = _normalize_text(value)
        if text:
            return text
    return None


def _iso_from_timestamp_string(timestamp: str | None) -> str | None:
    if not isinstance(timestamp, str):
        return None
    text = timestamp.strip()
    if len(text) != 15 or text[8] != "_":
        return None
    try:
        parsed = datetime.strptime(text, "%Y%m%d_%H%M%S")
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc).isoformat()


def _json_safe_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _json_safe_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe_value(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe_value(item) for item in value]
    return value


def _source_names_from_json(payload: dict[str, Any]) -> list[str]:
    names: list[str] = []
    source_name = _first_text(payload.get("source"), payload.get("source_name"))
    if source_name:
        names.append(source_name)
    return names


@dataclass(frozen=True)
class DashboardPropertyRow:
    listing_id: str
    address: str | None = None
    city: str | None = None
    asking_price: float | None = None
    living_area: float | None = None
    plot_size: float | None = None
    bedrooms: int | None = None
    energy_label: str | None = None
    construction_year: int | None = None
    days_on_market: int | None = None
    investment_score: int | None = None
    opportunity_score: int | None = None
    source_name: str | None = None
    source_url: str | None = None
    bag_id: str | None = None
    bag_address_id: str | None = None
    bag_verblijfsobject_id: str | None = None
    bag_pand_id: str | None = None
    bag_usage_purpose: str | None = None
    bag_building_year: int | None = None
    bag_status: str | None = None
    bag_official_floor_area_m2: float | None = None
    funda_living_area_m2: float | None = None
    calculation_area_m2: float | None = None
    calculation_area_source: str | None = None
    living_area_difference_m2: float | None = None
    living_area_difference_percentage: float | None = None
    asking_price_per_m2: float | None = None
    neighborhood_price_per_m2: float | None = None
    woz_value_per_m2: float | None = None
    permit_history_count: int | None = None
    bag_confidence_score: int | None = None
    bag_quality_flags: list[str] = field(default_factory=list)
    woz_value: float | None = None
    woz_valuation_year: int | None = None
    asking_price_minus_woz_value: float | None = None
    asking_price_vs_woz_percentage: float | None = None
    woz_source: str | None = None
    woz_retrieval_date: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "listing_id": self.listing_id,
            "address": self.address,
            "city": self.city,
            "asking_price": self.asking_price,
            "living_area": self.living_area,
            "plot_size": self.plot_size,
            "bedrooms": self.bedrooms,
            "energy_label": self.energy_label,
            "construction_year": self.construction_year,
            "days_on_market": self.days_on_market,
            "investment_score": self.investment_score,
            "opportunity_score": self.opportunity_score,
            "source_name": self.source_name,
            "source_url": self.source_url,
            "bag_id": self.bag_id,
            "bag_address_id": self.bag_address_id,
            "bag_verblijfsobject_id": self.bag_verblijfsobject_id,
            "bag_pand_id": self.bag_pand_id,
            "bag_usage_purpose": self.bag_usage_purpose,
            "bag_building_year": self.bag_building_year,
            "bag_status": self.bag_status,
            "bag_official_floor_area_m2": self.bag_official_floor_area_m2,
            "funda_living_area_m2": self.funda_living_area_m2,
            "calculation_area_m2": self.calculation_area_m2,
            "calculation_area_source": self.calculation_area_source,
            "living_area_difference_m2": self.living_area_difference_m2,
            "living_area_difference_percentage": self.living_area_difference_percentage,
            "asking_price_per_m2": self.asking_price_per_m2,
            "neighborhood_price_per_m2": self.neighborhood_price_per_m2,
            "woz_value_per_m2": self.woz_value_per_m2,
            "permit_history_count": self.permit_history_count,
            "bag_confidence_score": self.bag_confidence_score,
            "bag_quality_flags": list(self.bag_quality_flags),
            "woz_value": self.woz_value,
            "woz_valuation_year": self.woz_valuation_year,
            "asking_price_minus_woz_value": self.asking_price_minus_woz_value,
            "asking_price_vs_woz_percentage": self.asking_price_vs_woz_percentage,
            "woz_source": self.woz_source,
            "woz_retrieval_date": self.woz_retrieval_date,
        }


@dataclass(frozen=True)
class DashboardResult:
    scan_timestamp: str | None
    source_names: list[str] = field(default_factory=list)
    listings_found: int = 0
    new_listings: int = 0
    changed_listings: int = 0
    failed_listings: int = 0
    average_investment_score: float = 0.0
    average_opportunity_score: float = 0.0
    properties: list[DashboardPropertyRow] = field(default_factory=list)
    top_properties: list[DashboardPropertyRow] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "scan_timestamp": self.scan_timestamp,
            "source_names": list(self.source_names),
            "listings_found": self.listings_found,
            "new_listings": self.new_listings,
            "changed_listings": self.changed_listings,
            "failed_listings": self.failed_listings,
            "average_investment_score": self.average_investment_score,
            "average_opportunity_score": self.average_opportunity_score,
            "properties": [row.to_dict() for row in self.properties],
            "top_properties": [row.to_dict() for row in self.top_properties],
        }


class DashboardService:
    def __init__(self, database_service: DatabaseService | None = None, scan_runs_dir: Path | None = None) -> None:
        self.database_service = database_service or DatabaseService.from_env()
        self.scan_runs_dir = scan_runs_dir or (Path("output") / "scan-runs")

    @property
    def uses_database(self) -> bool:
        return bool(self.database_service and self.database_service.is_enabled)

    def load_latest_dashboard_result(self) -> DashboardResult:
        if self.uses_database:
            return self._load_from_database()
        return self._load_from_json()

    def load_latest_completed_scan(self) -> DashboardResult:
        return self.load_latest_dashboard_result()

    def _load_from_database(self) -> DashboardResult:
        scan_run = self._latest_completed_scan_run()
        if not scan_run:
            return self._load_from_json()

        source_names = self._scan_source_names_from_database(scan_run)
        start_at = _normalize_text(scan_run.get("started_at"))
        completed_at = _normalize_text(scan_run.get("completed_at"))
        source_id = _normalize_text(scan_run.get("source_id"))

        candidates = self.database_service.list_deal_candidates(limit=5000, source_id=source_id, sort_by="score_desc")
        rows = [
            row
            for row in (
                self._normalize_candidate_row(candidate, start_at=start_at, completed_at=completed_at)
                for candidate in candidates
            )
            if row is not None
        ]

        rows.sort(key=self._opportunity_sort_key, reverse=True)
        return self._build_result_from_rows(scan_run, source_names, rows)

    def _load_from_json(self) -> DashboardResult:
        scan_file = self._latest_scan_file()
        if scan_file is None:
            return DashboardResult(scan_timestamp=None)

        try:
            with scan_file.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except Exception:
            return DashboardResult(scan_timestamp=_iso_from_timestamp_string(self._timestamp_from_filename(scan_file)) or self._file_mtime_iso(scan_file))

        summary = _as_dict(payload.get("summary"))
        source_names = _source_names_from_json(payload)
        rows = [
            row
            for row in (
                self._normalize_json_property_row(item, source_name=source_names[0] if source_names else None)
                for item in _as_list(payload.get("properties"))
            )
            if row is not None
        ]
        rows.sort(key=self._opportunity_sort_key, reverse=True)

        return DashboardResult(
            scan_timestamp=_iso_from_timestamp_string(self._timestamp_from_filename(scan_file)) or self._file_mtime_iso(scan_file),
            source_names=source_names,
            listings_found=_as_int(summary.get("listings_found")) or 0,
            new_listings=_as_int(summary.get("listings_imported")) or _as_int(summary.get("fully_imported")) or 0,
            changed_listings=_as_int(summary.get("partially_imported")) or 0,
            failed_listings=_as_int(summary.get("listings_failed")) or 0,
            average_investment_score=self._average_score(rows, "investment_score"),
            average_opportunity_score=self._average_score(rows, "opportunity_score"),
            properties=rows,
            top_properties=rows[:20],
        )

    def _latest_completed_scan_run(self) -> dict[str, Any]:
        if not self.uses_database:
            return {}

        rows = self.database_service._fetch_rows("scan_runs", limit=200, order_column="started_at", ascending=False)
        for row in rows:
            status = _normalize_text(row.get("status"))
            if status in {"completed", "success", "succeeded", "ok"} or row.get("completed_at"):
                return row
        return {}

    def _scan_source_names_from_database(self, scan_run: dict[str, Any]) -> list[str]:
        names: list[str] = []
        source_id = _normalize_text(scan_run.get("source_id"))
        if source_id:
            source_rows = self.database_service._fetch_rows("listing_sources", filters={"id": source_id}, limit=1)
            source_name = _first_text(source_rows[0].get("name") if source_rows else None)
            if source_name:
                names.append(source_name)

        metadata = _as_dict(scan_run.get("metadata"))
        for candidate in (metadata.get("import_type"), metadata.get("source")):
            source_name = _first_text(candidate)
            if source_name and source_name not in names:
                names.append(source_name)
        return names

    def _normalize_candidate_row(
        self,
        candidate: dict[str, Any],
        *,
        start_at: str | None,
        completed_at: str | None,
    ) -> DashboardPropertyRow | None:
        listing = _as_dict(candidate.get("listing"))
        source = _as_dict(candidate.get("source"))
        detected_at = _normalize_text(candidate.get("detected_at"))
        if start_at and detected_at and detected_at < start_at:
            return None
        if completed_at and detected_at and detected_at > completed_at:
            return None

        raw_payload = _as_dict(listing.get("raw_payload"))
        payload = raw_payload or listing
        woz_value = _as_float(payload.get("latest_woz_value") or listing.get("latest_woz_value"))
        asking_price = _as_float(listing.get("asking_price") or payload.get("asking_price") or payload.get("current_asking_price"))
        asking_minus_woz = None
        asking_vs_woz_pct = None
        if asking_price is not None and woz_value not in (None, 0):
            asking_minus_woz = round(asking_price - float(woz_value), 2)
            asking_vs_woz_pct = round(((asking_price - float(woz_value)) / float(woz_value)) * 100.0, 2)

        listing_id = _first_text(listing.get("id"), candidate.get("listing_id"))
        if not listing_id:
            return None

        return DashboardPropertyRow(
            listing_id=listing_id,
            address=_first_text(listing.get("address"), payload.get("address")),
            city=_first_text(listing.get("city"), payload.get("city")),
            asking_price=_as_float(listing.get("asking_price") or payload.get("asking_price") or payload.get("current_asking_price")),
            living_area=_as_float(payload.get("living_area") or payload.get("surface_m2") or listing.get("surface_m2")),
            plot_size=_as_float(payload.get("plot_size") or payload.get("plot_size_m2") or listing.get("plot_size_m2")),
            bedrooms=_as_int(payload.get("bedrooms")),
            energy_label=_first_text(payload.get("energy_label")),
            construction_year=_as_int(payload.get("construction_year") or payload.get("bag_building_year")),
            days_on_market=_as_int(listing.get("days_on_market") or payload.get("days_on_market")),
            investment_score=_as_int(candidate.get("investment_score")),
            opportunity_score=_as_int(candidate.get("hidden_value_score") or candidate.get("score")),
            source_name=_first_text(source.get("name"), listing.get("source_name"), payload.get("source_name")),
            source_url=_first_text(listing.get("source_url"), payload.get("source_url")),
            bag_id=_first_text(payload.get("bag_id")),
            bag_address_id=_first_text(payload.get("bag_address_id")),
            bag_verblijfsobject_id=_first_text(payload.get("bag_verblijfsobject_id")),
            bag_pand_id=_first_text(payload.get("bag_pand_id")),
            bag_usage_purpose=_first_text(payload.get("bag_usage_purpose")),
            bag_building_year=_as_int(payload.get("bag_building_year")),
            bag_status=_first_text(payload.get("bag_status")),
            bag_official_floor_area_m2=_as_float(payload.get("bag_official_floor_area_m2")),
            funda_living_area_m2=_as_float(payload.get("funda_living_area_m2") or payload.get("living_area") or payload.get("surface_m2")),
            calculation_area_m2=_as_float(payload.get("calculation_area_m2") or payload.get("surface_m2")),
            calculation_area_source=_first_text(payload.get("calculation_area_source")),
            living_area_difference_m2=_as_float(payload.get("living_area_difference_m2")),
            living_area_difference_percentage=_as_float(payload.get("living_area_difference_percentage")),
            asking_price_per_m2=_as_float(payload.get("asking_price_per_m2") or payload.get("price_per_m2")),
            neighborhood_price_per_m2=_as_float(
                payload.get("neighborhood_m2_price_average")
                or payload.get("neighbourhood_m2_price_average")
                or payload.get("neighborhood_price_per_m2")
            ),
            woz_value_per_m2=_as_float(payload.get("woz_value_per_m2")),
            permit_history_count=(
                len(payload.get("permits_last_10_years")) if isinstance(payload.get("permits_last_10_years"), list) else 0
            )
            + (len(payload.get("active_permits")) if isinstance(payload.get("active_permits"), list) else 0),
            bag_confidence_score=_as_int(payload.get("bag_confidence_score")),
            bag_quality_flags=[str(item) for item in _as_list(payload.get("bag_quality_flags")) if str(item).strip()],
            woz_value=woz_value,
            woz_valuation_year=_as_int(payload.get("woz_valuation_year") or listing.get("woz_valuation_year")),
            asking_price_minus_woz_value=asking_minus_woz,
            asking_price_vs_woz_percentage=asking_vs_woz_pct,
            woz_source=_first_text(payload.get("woz_source")),
            woz_retrieval_date=_first_text(payload.get("woz_retrieval_date")),
        )

    def _normalize_json_property_row(self, item: dict[str, Any], *, source_name: str | None = None) -> DashboardPropertyRow | None:
        if not isinstance(item, dict):
            return None

        payload = _as_dict(item.get("property")) or _as_dict(item.get("property_for_database")) or _as_dict(item.get("stored_row"))
        if not payload:
            return None

        listing_id = _first_text(payload.get("listing_id"), item.get("listing_id"), payload.get("id"))
        if not listing_id:
            return None

        source_text = _first_text(source_name, item.get("source_name"), payload.get("source_name"))
        asking_price = _as_float(payload.get("asking_price") or payload.get("current_asking_price"))
        woz_value = _as_float(payload.get("latest_woz_value") or payload.get("woz_value"))
        asking_minus_woz = None
        asking_vs_woz_pct = None
        if asking_price is not None and woz_value not in (None, 0):
            asking_minus_woz = round(asking_price - float(woz_value), 2)
            asking_vs_woz_pct = round(((asking_price - float(woz_value)) / float(woz_value)) * 100.0, 2)

        return DashboardPropertyRow(
            listing_id=listing_id,
            address=_first_text(payload.get("address")),
            city=_first_text(payload.get("city")),
            asking_price=asking_price,
            living_area=_as_float(payload.get("living_area") or payload.get("surface_m2")),
            plot_size=_as_float(payload.get("plot_size") or payload.get("plot_size_m2")),
            bedrooms=_as_int(payload.get("bedrooms")),
            energy_label=_first_text(payload.get("energy_label")),
            construction_year=_as_int(payload.get("construction_year") or payload.get("bag_building_year")),
            days_on_market=_as_int(payload.get("days_on_market")),
            investment_score=_as_int(item.get("investment_score") or payload.get("investment_score")),
            opportunity_score=_as_int(item.get("opportunity_score") or item.get("hidden_value_score") or payload.get("hidden_value_score")),
            source_name=source_text,
            source_url=_first_text(payload.get("source_url"), item.get("source_url")),
            bag_id=_first_text(payload.get("bag_id")),
            bag_address_id=_first_text(payload.get("bag_address_id")),
            bag_verblijfsobject_id=_first_text(payload.get("bag_verblijfsobject_id")),
            bag_pand_id=_first_text(payload.get("bag_pand_id")),
            bag_usage_purpose=_first_text(payload.get("bag_usage_purpose")),
            bag_building_year=_as_int(payload.get("bag_building_year")),
            bag_status=_first_text(payload.get("bag_status")),
            bag_official_floor_area_m2=_as_float(payload.get("bag_official_floor_area_m2")),
            funda_living_area_m2=_as_float(payload.get("funda_living_area_m2") or payload.get("living_area") or payload.get("surface_m2")),
            calculation_area_m2=_as_float(payload.get("calculation_area_m2") or payload.get("surface_m2")),
            calculation_area_source=_first_text(payload.get("calculation_area_source")),
            living_area_difference_m2=_as_float(payload.get("living_area_difference_m2")),
            living_area_difference_percentage=_as_float(payload.get("living_area_difference_percentage")),
            asking_price_per_m2=_as_float(payload.get("asking_price_per_m2") or payload.get("price_per_m2")),
            neighborhood_price_per_m2=_as_float(
                payload.get("neighborhood_m2_price_average")
                or payload.get("neighbourhood_m2_price_average")
                or payload.get("neighborhood_price_per_m2")
            ),
            woz_value_per_m2=_as_float(payload.get("woz_value_per_m2")),
            permit_history_count=(
                len(payload.get("permits_last_10_years")) if isinstance(payload.get("permits_last_10_years"), list) else 0
            )
            + (len(payload.get("active_permits")) if isinstance(payload.get("active_permits"), list) else 0),
            bag_confidence_score=_as_int(payload.get("bag_confidence_score")),
            bag_quality_flags=[str(item) for item in _as_list(payload.get("bag_quality_flags")) if str(item).strip()],
            woz_value=woz_value,
            woz_valuation_year=_as_int(payload.get("woz_valuation_year")),
            asking_price_minus_woz_value=asking_minus_woz,
            asking_price_vs_woz_percentage=asking_vs_woz_pct,
            woz_source=_first_text(payload.get("woz_source")),
            woz_retrieval_date=_first_text(payload.get("woz_retrieval_date")),
        )

    def _build_result_from_rows(
        self,
        scan_run: dict[str, Any],
        source_names: list[str],
        rows: list[DashboardPropertyRow],
    ) -> DashboardResult:
        metadata = _as_dict(scan_run.get("metadata"))
        source_stats = _as_dict(metadata.get("source_stats"))
        scan_timestamp = _normalize_text(scan_run.get("completed_at")) or _normalize_text(scan_run.get("started_at"))
        return DashboardResult(
            scan_timestamp=scan_timestamp,
            source_names=source_names,
            listings_found=_as_int(scan_run.get("items_found")) or 0,
            new_listings=_as_int(scan_run.get("items_new")) or 0,
            changed_listings=_as_int(scan_run.get("items_changed")) or 0,
            failed_listings=_as_int(source_stats.get("failed_listings")) or _as_int(scan_run.get("failed_listings")) or 0,
            average_investment_score=self._average_score(rows, "investment_score"),
            average_opportunity_score=self._average_score(rows, "opportunity_score"),
            properties=rows,
            top_properties=rows[:20],
        )

    def _opportunity_sort_key(self, row: DashboardPropertyRow) -> tuple[bool, int]:
        score = row.opportunity_score
        return (score is not None, score or -1)

    def _average_score(self, rows: list[DashboardPropertyRow], field_name: str) -> float:
        scores = [
            score
            for score in (
                getattr(row, field_name)
                for row in rows
            )
            if score is not None
        ]
        if not scores:
            return 0.0
        return round(sum(scores) / len(scores), 2)

    def _latest_scan_file(self) -> Path | None:
        if not self.scan_runs_dir.exists():
            return None

        candidates = [path for path in self.scan_runs_dir.glob("*.json") if path.is_file()]
        if not candidates:
            return None
        return max(candidates, key=lambda path: (path.stat().st_mtime, path.name))

    def _timestamp_from_filename(self, scan_file: Path) -> str | None:
        stem = scan_file.stem
        if "_" not in stem:
            return None
        return stem.rsplit("_", 1)[-1]

    def _file_mtime_iso(self, scan_file: Path) -> str | None:
        try:
            return datetime.fromtimestamp(scan_file.stat().st_mtime, tz=timezone.utc).isoformat()
        except OSError:
            return None


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []