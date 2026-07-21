from __future__ import annotations

import argparse
from dataclasses import asdict
from contextlib import contextmanager
from datetime import datetime, timezone
from html import escape
import json
import logging
from pathlib import Path
import re
import os
import sys
import time
from typing import Any
from urllib.parse import urlencode

import streamlit as st
import requests

from ai.analyzer import analyze_property
from config import FUNDA_DEFAULT_SCAN_CITIES
from deal_finder.orchestrator import DealFinderOrchestrator
from deal_finder.sources.funda import normalize_funda_area_slug
from deal_finder.sources.klusvastgoed import KlusvastgoedAdapter, build_klusvastgoed_start_url
from models.permit import PermitRecord
from models.property import Property
from models.transaction import PropertyTransaction
from scrapers.base import ScrapeResult
from scrapers.router import scrape_url
from services.calculations import calculate_days_on_market, calculate_price_change_since_last_transaction, calculate_price_per_m2, calculate_price_reduction
from services.dashboard_service import DashboardService
from services.comparable_sales import ComparableSalesService
from services.database import DatabaseService
from services.dutch_municipalities import get_dutch_municipalities
from services.end_to_end_workflow import PropertyHunterEndToEndWorkflow
from services.property_enrichment import PropertyEnrichmentEngine


DATABASE_SERVICE = DatabaseService.from_env()
DEAL_FINDER_ORCHESTRATOR = DealFinderOrchestrator(DATABASE_SERVICE)
COMPARABLE_SALES_SERVICE = ComparableSalesService()
DASHBOARD_SERVICE = DashboardService(DATABASE_SERVICE)
PROPERTY_ENRICHMENT_ENGINE = PropertyEnrichmentEngine()
PROPERTY_HUNTER_WORKFLOW = PropertyHunterEndToEndWorkflow(
    orchestrator=DEAL_FINDER_ORCHESTRATOR,
    database_service=DATABASE_SERVICE,
)
LOGGER = logging.getLogger(__name__)

FUNDA_PLACE_ALIASES: tuple[str, ...] = (
    "Den Haag",
)


class _ReadOnlyScanDatabaseService:
    is_enabled = False


@contextmanager
def _exclusive_scan_lock(lock_path: Path):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+")
    try:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise
    except Exception:
        handle.close()
        raise

    try:
        yield handle
    finally:
        try:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _run_klusvastgoed_national_cli(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Run the national Klusvastgoed scan.")
    parser.add_argument("--max-pages", type=int, default=1000, help="Maximum paginated result pages per city")
    parser.add_argument("--timeout-seconds", type=float, default=12.0, help="HTTP timeout per request in seconds")
    parser.add_argument("--lock-file", default="/tmp/propertyhunter-klusvastgoed.lock", help="Lock file path to prevent concurrent scans")
    args = parser.parse_args(argv)

    if not DATABASE_SERVICE.is_enabled:
        print("Database is not enabled. Set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY first.")
        return 1

    lock_path = Path(str(args.lock_file or "/tmp/propertyhunter-klusvastgoed.lock"))
    try:
        with _exclusive_scan_lock(lock_path):
            result = DEAL_FINDER_ORCHESTRATOR.import_from_source(
                "klusvastgoed",
                {
                    "mode": "national",
                    "max_pages": max(1, int(args.max_pages or 1)),
                    "timeout_seconds": float(args.timeout_seconds or 12.0),
                    "scan_frequency_minutes": 30,
                    "include_in_combined_ranking": False,
                },
            )
    except BlockingIOError:
        print(f"Klusvastgoed national scan is already running: {lock_path}")
        return 0

    if not result.get("ok"):
        print(f"Klusvastgoed national scan failed: {result.get('error')}")
        if result.get("warnings"):
            for warning in result["warnings"][:20]:
                print(f"- {warning}")
        return 1

    print("Klusvastgoed nationale import")
    print(f"Listings gevonden: {result.get('listings_found')}")
    print(f"Nieuwe listings: {result.get('new')}")
    print(f"Gewijzigde listings: {result.get('changed')}")
    print(f"Cities gevonden: {result.get('cities_found')}")
    print(f"Gemeenten gevonden: {result.get('municipalities_found')}")
    print(f"Provincies gevonden: {result.get('provinces_found')}")
    inactive_result = result.get("inactive_result") or {}
    if isinstance(inactive_result, dict):
        print(f"Inactief gemarkeerd: {inactive_result.get('marked_inactive', 0)}")
    if result.get("record_results"):
        examples = [item for item in result["record_results"] if isinstance(item, dict) and item.get("success")][:5]
        if examples:
            print("Voorbeelden")
            for item in examples:
                print(f"- {item.get('source_url')}")
    return 0


def _run_url_import(urls_text: str, orchestrator: DealFinderOrchestrator | None = None) -> dict:
    active_orchestrator = orchestrator or DEAL_FINDER_ORCHESTRATOR
    try:
        result = active_orchestrator.import_urls(urls_text)
    except Exception as error:
        LOGGER.exception("Deal Finder URL import failed: %s", type(error).__name__)
        return {"ok": False, "error": f"{type(error).__name__}: {error}", "result": None}
    return {"ok": True, "error": None, "result": result}


def _json_safe_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _json_safe_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe_value(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe_value(item) for item in value]
    return value


def _property_to_scan_payload(property_obj: Property) -> dict[str, Any]:
    property_data = _json_safe_value(asdict(property_obj))
    if not isinstance(property_data, dict):
        property_data = {}

    payload = {
        "source_url": property_data.get("source_url"),
        "title": property_data.get("title"),
        "address": property_data.get("address"),
        "city": property_data.get("city"),
        "country": property_data.get("country"),
        "asking_price": property_data.get("asking_price"),
        "asking_price_status": property_data.get("asking_price_status") or "unknown",
        "asking_price_text": property_data.get("asking_price_text"),
        "postal_code": property_data.get("postal_code"),
        "municipality": property_data.get("municipality"),
        "bag_id": property_data.get("bag_id"),
        "bag_address_id": property_data.get("bag_address_id"),
        "bag_verblijfsobject_id": property_data.get("bag_verblijfsobject_id"),
        "bag_nummeraanduiding_id": property_data.get("bag_nummeraanduiding_id"),
        "bag_pand_id": property_data.get("bag_pand_id"),
        "bag_building_year": property_data.get("bag_building_year"),
        "construction_year_bag": property_data.get("construction_year_bag") or property_data.get("bag_building_year"),
        "bag_usage_purpose": property_data.get("bag_usage_purpose"),
        "usage_purpose": property_data.get("usage_purpose") or property_data.get("bag_usage_purpose"),
        "bag_status": property_data.get("bag_status"),
        "bag_official_floor_area_m2": property_data.get("bag_official_floor_area_m2"),
        "official_floor_area_m2": property_data.get("official_floor_area_m2") or property_data.get("bag_official_floor_area_m2"),
        "bag_coordinates_rd": property_data.get("bag_coordinates_rd"),
        "bag_coordinates_ll": property_data.get("bag_coordinates_ll"),
        "coordinates": property_data.get("coordinates") or {"rd": property_data.get("bag_coordinates_rd"), "ll": property_data.get("bag_coordinates_ll")},
        "bag_postcode": property_data.get("bag_postcode"),
        "bag_municipality": property_data.get("bag_municipality"),
        "bag_retrieval_date": property_data.get("bag_retrieval_date"),
        "retrieval_date": property_data.get("retrieval_date") or property_data.get("bag_retrieval_date"),
        "bag_source": property_data.get("bag_source"),
        "source": property_data.get("source") or property_data.get("bag_source"),
        "bag_confidence_score": property_data.get("bag_confidence_score"),
        "confidence_score": property_data.get("confidence_score") or property_data.get("bag_confidence_score"),
        "bag_quality_flags": property_data.get("bag_quality_flags") or [],
        "funda_living_area_m2": property_data.get("funda_living_area_m2"),
        "living_area_difference_m2": property_data.get("living_area_difference_m2"),
        "living_area_difference_percentage": property_data.get("living_area_difference_percentage"),
        "calculation_area_m2": property_data.get("calculation_area_m2"),
        "calculation_area_source": property_data.get("calculation_area_source"),
        "asking_price_per_m2": property_data.get("asking_price_per_m2"),
        "woz_value_per_m2": property_data.get("woz_value_per_m2"),
        "woz_object_number": property_data.get("woz_object_number"),
        "latest_woz_value": property_data.get("latest_woz_value"),
        "woz_valuation_year": property_data.get("woz_valuation_year"),
        "woz_historical_values": property_data.get("woz_historical_values") or [],
        "neighborhood_m2_price_average": property_data.get("neighborhood_m2_price_average"),
        "street_m2_price_average": property_data.get("street_m2_price_average"),
        "listed_since": property_data.get("listed_since"),
        "days_on_market": property_data.get("days_on_market"),
        "listing_status": property_data.get("listing_status") or "unknown",
        "original_asking_price": property_data.get("original_asking_price"),
        "current_asking_price": property_data.get("current_asking_price"),
        "price_reduction_count": property_data.get("price_reduction_count") or 0,
        "last_price_reduction_date": property_data.get("last_price_reduction_date"),
        "total_price_reduction_amount": property_data.get("total_price_reduction_amount"),
        "total_price_reduction_percentage": property_data.get("total_price_reduction_percentage"),
        "listing_history_source": property_data.get("listing_history_source"),
        "listing_history_confidence": property_data.get("listing_history_confidence") or "unknown",
        "surface_m2": property_data.get("surface_m2"),
        "price_per_m2": property_data.get("price_per_m2"),
        "annual_rent": property_data.get("annual_rent"),
        "property_type": property_data.get("property_type"),
        "current_use": property_data.get("current_use"),
        "zoning": property_data.get("zoning"),
        "description": property_data.get("description"),
        "raw_extracted_data": property_data,
    }
    return payload


_SCAN_COMPLETENESS_FIELDS = [
    "asking_price",
    "surface_m2",
    "plot_size_m2",
    "bedrooms",
    "energy_label",
    "construction_year",
    "listed_since",
    "price_reduction_count",
    "broker",
]


def _scan_missing_fields(property_data: dict[str, Any]) -> list[str]:
    missing_fields: list[str] = []
    for field_name in _SCAN_COMPLETENESS_FIELDS:
        value = property_data.get(field_name)
        if field_name == "asking_price":
            asking_price_status = str(property_data.get("asking_price_status") or "unknown").strip().lower()
            if value is None and asking_price_status in {"unknown", ""}:
                missing_fields.append(field_name)
            continue
        if value in (None, "", [], {}):
            missing_fields.append(field_name)
    return missing_fields


def _scan_import_status(property_data: dict[str, Any]) -> tuple[str, list[str]]:
    missing_fields = _scan_missing_fields(property_data)
    if missing_fields:
        return "partially_imported", missing_fields
    return "fully_imported", missing_fields


def _format_seconds(seconds: float | None) -> str:
    if seconds is None:
        return "0.000"
    return f"{max(0.0, float(seconds)):.3f}"


def _write_scan_output(output_dir: Path, scan_payload: dict[str, Any]) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    output_path = output_dir / f"{scan_payload.get('source', 'scan')}_scan_{timestamp}.json"
    with output_path.open("w", encoding="utf-8") as handle:
        json.dump(_json_safe_value(scan_payload), handle, ensure_ascii=False, indent=2)
    return output_path


def _run_source_scan(
    source_name: str,
    *,
    orchestrator: DealFinderOrchestrator | None = None,
    database_service: DatabaseService | None = None,
    output_dir: Path | None = None,
    start_url: str | None = None,
    max_pages: int = 1000,
    timeout_seconds: float = 12.0,
    force_refresh: bool = False,
) -> dict[str, Any]:
    active_orchestrator = orchestrator or DEAL_FINDER_ORCHESTRATOR
    active_database = database_service or DATABASE_SERVICE
    active_output_dir = output_dir or Path("output") / "scan-runs"
    scan_warnings: list[str] = []

    adapter = active_orchestrator.source_registry.resolve(source_name)
    if adapter is None:
        error = f"Unknown source adapter: {source_name}"
        print(error)
        return {"ok": False, "error": error}

    configuration = {
        "start_url": str(start_url or getattr(adapter, "default_start_url", "") or "").strip(),
        "max_pages": max_pages,
        "timeout_seconds": timeout_seconds,
        "force_refresh": bool(force_refresh),
    }
    is_valid, warnings = adapter.validate_configuration(configuration)
    if not is_valid:
        error = "; ".join(warnings) or "Invalid source configuration."
        print(error)
        return {"ok": False, "error": error, "warnings": warnings}
    scan_warnings.extend(warnings)

    scan_started_at = time.perf_counter()
    print(f"Start scan voor source: {source_name}")
    print(f"Config: max_pages={max_pages}, timeout_seconds={timeout_seconds}, force_refresh={bool(force_refresh)}")

    try:
        results = adapter.load_and_normalize_listings(configuration)
    except Exception as error:
        error_message = f"{type(error).__name__}: {error}"
        print(f"Scan mislukt: {error_message}")
        return {"ok": False, "error": error_message, "warnings": warnings}

    fetch_stats = adapter.get_last_fetch_stats()
    listings_found = max(int(fetch_stats.get("listings_found") or 0), len(results))
    print(f"Listings gevonden: {listings_found}")

    imported_properties: list[dict[str, Any]] = []
    failed_records: list[dict[str, Any]] = []
    import_durations: list[float] = []

    fully_imported_count = 0
    partially_imported_count = 0
    failed_count = 0

    for result in results:
        item_started_at = time.perf_counter()
        payload = result.payload if isinstance(result.payload, dict) else {}
        if not payload and result.listing is not None:
            payload = dict(result.listing.raw_payload or {})

        if not result.success or result.listing is None:
            failed_count += 1
            failed_records.append(
                {
                    "record_index": result.record_index,
                    "source_url": payload.get("source_url") if isinstance(payload, dict) else None,
                    "error": result.error,
                }
            )
            print(f"[{result.record_index}] mislukt | imported={fully_imported_count + partially_imported_count} | failed={failed_count}")
            continue

        try:
            property_obj = adapter.to_property_model(payload)
        except Exception as error:
            failed_count += 1
            error_message = f"{type(error).__name__}: {error}"
            failed_records.append(
                {
                    "record_index": result.record_index,
                    "source_url": payload.get("source_url") if isinstance(payload, dict) else None,
                    "error": error_message,
                }
            )
            print(f"[{result.record_index}] mislukt | imported={fully_imported_count + partially_imported_count} | failed={failed_count}")
            continue

        property_payload = _property_to_scan_payload(property_obj)
        report_payload = _json_safe_value(asdict(property_obj))
        if not isinstance(report_payload, dict):
            report_payload = {}

        if "funda" in str(source_name).lower() and not active_database.is_enabled:
            try:
                enriched_listing_row, enrichment_warnings = active_orchestrator._enrich_listing_with_public_data(
                    listing_row={
                        "id": None,
                        "source_id": None,
                        "external_listing_id": property_obj.external_listing_id,
                        "source_url": property_obj.source_url,
                        "title": property_obj.title,
                        "address": property_obj.address,
                        "city": property_obj.city,
                        "asking_price": property_obj.asking_price,
                        "surface_m2": property_obj.surface_m2,
                        "property_type": property_obj.property_type,
                        "listing_status": property_obj.listing_status,
                        "raw_payload": property_payload,
                    },
                    source_name=source_name,
                    persist=False,
                )
                scan_warnings.extend(enrichment_warnings)
                enriched_raw_payload = enriched_listing_row.get("raw_payload") if isinstance(enriched_listing_row.get("raw_payload"), dict) else {}
                if enriched_raw_payload:
                    report_payload.update(enriched_raw_payload)
                    property_payload = {
                        **property_payload,
                        **enriched_raw_payload,
                        "raw_extracted_data": {
                            **(property_payload.get("raw_extracted_data") or {}),
                            **enriched_raw_payload,
                        },
                    }
            except Exception as error:
                scan_warnings.append(f"{property_obj.source_url}: dry-run BAG/WOZ enrichment failed: {type(error).__name__}: {error}")
                LOGGER.warning("Dry-run property enrichment failed for %s: %s", property_obj.source_url, error)

        import_status, missing_fields = _scan_import_status(report_payload)
        storage_error: str | None = None
        stored_row: dict[str, Any] = {}

        if active_database.is_enabled:
            try:
                stored_row = active_database.upsert_property(property_payload)
            except Exception as error:
                storage_error = f"{type(error).__name__}: {error}"
            if not stored_row and storage_error is None:
                storage_error = "Property could not be stored."

        if stored_row and active_database.is_enabled:
            try:
                enrichment_result = PROPERTY_ENRICHMENT_ENGINE.enrich(property_obj)
                active_database.upsert_property_enrichment_group(
                    property_id=str(stored_row.get("id") or ""),
                    status="completed",
                    started_at=enrichment_result.started_at,
                    completed_at=enrichment_result.completed_at,
                    source=property_obj.source_url,
                    warning_count=sum(1 for item in enrichment_result.items if not item.success),
                    error_count=sum(1 for item in enrichment_result.items if not item.success),
                    summary={"enrichment_count": len(enrichment_result.items)},
                )
                active_database.batch_upsert_property_enrichments(
                    property_id=str(stored_row.get("id") or ""),
                    enrichments=[item.to_dict() for item in enrichment_result.items],
                )
                property_updates = enrichment_result.to_property_updates()
                if property_updates:
                    active_database._update_row("properties", str(stored_row.get("id") or ""), property_updates)
            except Exception as error:
                LOGGER.warning("Property enrichment failed for %s: %s", property_obj.source_url, error)

        elapsed_seconds = time.perf_counter() - item_started_at
        if storage_error:
            failed_count += 1
            failed_records.append(
                {
                    "record_index": result.record_index,
                    "source_url": property_obj.source_url,
                    "error": storage_error,
                }
            )
            print(f"[{result.record_index}] mislukt | imported={fully_imported_count + partially_imported_count} | failed={failed_count}")
            continue

        if import_status == "fully_imported":
            fully_imported_count += 1
        else:
            partially_imported_count += 1
        import_durations.append(elapsed_seconds)
        imported_properties.append(
            {
                "record_index": result.record_index,
                "source_url": property_obj.source_url,
                "import_status": import_status,
                "missing_fields": missing_fields,
                "property": report_payload,
                "property_for_database": property_payload,
                "stored_row": _json_safe_value(stored_row),
                "import_time_seconds": round(elapsed_seconds, 3),
            }
        )
        print(f"[{result.record_index}] geïmporteerd | imported={fully_imported_count + partially_imported_count} | failed={failed_count}")

    imported_count = fully_imported_count + partially_imported_count
    average_import_time = round(sum(import_durations) / len(import_durations), 3) if import_durations else 0.0
    total_elapsed_seconds = round(time.perf_counter() - scan_started_at, 3)

    scan_payload = {
        "source": source_name,
        "configuration": configuration,
        "data_origin": "live_scraper",
        "summary": {
            "listings_found": listings_found,
            "listings_imported": imported_count,
            "fully_imported": fully_imported_count,
            "partially_imported": partially_imported_count,
            "listings_failed": failed_count,
            "average_import_time_seconds": average_import_time,
            "total_elapsed_seconds": total_elapsed_seconds,
                "warnings": scan_warnings,
        },
        "properties": imported_properties,
        "failed_records": failed_records,
    }

    output_path = _write_scan_output(active_output_dir, scan_payload)
    print(f"JSON-output: {output_path}")
    print("Samenvatting")
    print(f"Listings gevonden: {listings_found}")
    print(f"Volledig geïmporteerd: {fully_imported_count}")
    print(f"Gedeeltelijk geïmporteerd: {partially_imported_count}")
    print(f"Gemiddelde importtijd: {_format_seconds(average_import_time)} s")
    print(f"Mislukt: {failed_count}")
    if imported_properties:
        print("Eerste 10 geïmporteerde panden")
        for item in imported_properties[:10]:
            print(f"- [{item.get('record_index')}] {item.get('source_url')} ({item.get('import_status')})")
            print(json.dumps(item.get("property") or {}, ensure_ascii=False, indent=2))
    if failed_records:
        for item in failed_records[:10]:
            print(f"- [{item.get('record_index')}] {item.get('error')}")

    return {
        "ok": True,
        "source": source_name,
        "output_path": str(output_path),
        "listings_found": listings_found,
        "listings_imported": imported_count,
        "fully_imported": fully_imported_count,
        "partially_imported": partially_imported_count,
        "listings_failed": failed_count,
        "average_import_time_seconds": average_import_time,
            "warnings": scan_warnings,
        "failed_records": failed_records,
    }


def _resolve_scan_sources(*, source_names: list[str] | None, scan_all: bool, orchestrator: DealFinderOrchestrator | None = None) -> list[str]:
    active_orchestrator = orchestrator or DEAL_FINDER_ORCHESTRATOR
    if scan_all:
        names: list[str] = []
        seen: set[str] = set()
        for entry in active_orchestrator.source_registry.list_entries():
            adapter = entry.adapter
            source_info = adapter.get_source_info()
            if source_info.source_type not in {"portal", "broker"}:
                continue
            if not source_info.is_enabled:
                continue
            source_name = str(source_info.source_name or "").strip()
            normalized = source_name.lower()
            if not source_name or normalized in seen:
                continue
            seen.add(normalized)
            names.append(source_name)
        return names

    names = [str(item).strip() for item in (source_names or []) if str(item).strip()]
    deduplicated: list[str] = []
    seen: set[str] = set()
    for item in names:
        normalized = item.lower()
        if normalized in seen:
            continue
        seen.add(normalized)
        deduplicated.append(item)
    return deduplicated


def _run_scan_cli(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Run a deal finder source scan.")
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--source", action="append", help="Source name to scan, for example funda")
    selection.add_argument("--all", action="store_true", help="Scan all enabled portal and broker sources")
    parser.add_argument("--output-dir", default="output/scan-runs", help="Directory for temporary JSON results")
    parser.add_argument("--municipality", help="Optional municipality for municipality-specific sources such as Klusvastgoed")
    parser.add_argument("--max-pages", type=int, default=1000, help="Maximum number of paginated result pages to crawl")
    parser.add_argument("--timeout-seconds", type=float, default=12.0, help="HTTP timeout per request in seconds")
    parser.add_argument("--dry-run", action="store_true", help="Skip database writes and only write JSON output")
    args = parser.parse_args(argv)

    source_names = _resolve_scan_sources(source_names=args.source, scan_all=bool(args.all))
    if not source_names:
        print("Geen scanbare bronnen gevonden.")
        return 1

    aggregate_found = 0
    aggregate_imported = 0
    aggregate_fully_imported = 0
    aggregate_partially_imported = 0
    aggregate_failed = 0
    aggregate_duration = 0.0
    executed_runs: list[dict[str, Any]] = []

    for source_name in source_names:
        start_url: str | None = None
        if args.municipality and source_name.strip().lower() in {"klusvastgoed", "klusvastgoed.nl"}:
            start_url = build_klusvastgoed_start_url(args.municipality)

        result = _run_source_scan(
            source_name,
            database_service=_ReadOnlyScanDatabaseService() if args.dry_run else None,
            output_dir=Path(args.output_dir),
            start_url=start_url,
            max_pages=max(1, int(args.max_pages or 1)),
            timeout_seconds=float(args.timeout_seconds or 12.0),
        )
        executed_runs.append(result)
        if result.get("ok"):
            aggregate_found += int(result.get("listings_found") or 0)
            aggregate_imported += int(result.get("listings_imported") or 0)
            aggregate_fully_imported += int(result.get("fully_imported") or 0)
            aggregate_partially_imported += int(result.get("partially_imported") or 0)
            aggregate_failed += int(result.get("listings_failed") or 0)
            aggregate_duration += float(result.get("average_import_time_seconds") or 0.0) * int(result.get("listings_imported") or 0)

    successful_runs = [item for item in executed_runs if item.get("ok")]
    if successful_runs:
        total_imported = sum(int(item.get("listings_imported") or 0) for item in successful_runs)
        weighted_average = aggregate_duration / total_imported if total_imported else 0.0
        print("Totaaloverzicht")
        print(f"Listings gevonden: {aggregate_found}")
        print(f"Volledig geïmporteerd: {aggregate_fully_imported}")
        print(f"Gedeeltelijk geïmporteerd: {aggregate_partially_imported}")
        print(f"Gemiddelde importtijd: {_format_seconds(weighted_average)} s")
        print(f"Mislukt: {aggregate_failed}")

    return 0 if successful_runs else 1


def _run_klusvastgoed_scan_from_ui(
    *,
    municipality: str,
    max_pages: int = 1,
    dry_run: bool = True,
) -> dict[str, Any]:
    selected_municipality = str(municipality or "").strip()
    if not selected_municipality:
        raise ValueError("Selecteer een gemeente.")

    start_url = build_klusvastgoed_start_url(selected_municipality)
    adapter = DEAL_FINDER_ORCHESTRATOR.source_registry.resolve("klusvastgoed")
    if isinstance(adapter, KlusvastgoedAdapter):
        start_url = adapter.build_start_url_for_municipality(selected_municipality)

    database_service = _ReadOnlyScanDatabaseService() if dry_run else DatabaseService()
    result = _run_source_scan(
        "klusvastgoed",
        orchestrator=DEAL_FINDER_ORCHESTRATOR,
        database_service=database_service,
        output_dir=Path("output") / "scan-runs",
        start_url=start_url,
        max_pages=max(1, int(max_pages or 1)),
        timeout_seconds=12.0,
        force_refresh=True,
    )
    if not result.get("ok"):
        return result

    rows: list[dict[str, Any]] = []
    output_path = str(result.get("output_path") or "").strip()
    if output_path:
        try:
            payload = json.loads(Path(output_path).read_text(encoding="utf-8"))
            properties = payload.get("properties") if isinstance(payload, dict) else []
            if isinstance(properties, list):
                rows = _build_rows_from_scan_properties(properties)
        except Exception as error:
            LOGGER.warning("Klusvastgoed dry-run output could not be parsed: %s", error)

    scored_rows = _score_rows_with_opportunity_intelligence(rows)
    sorted_rows = sorted(scored_rows, key=lambda item: _safe_score(item.get("opportunity_score")) or -1, reverse=True)

    return {
        **result,
        "source": "klusvastgoed.nl",
        "mode": "dry-run" if dry_run else "live",
        "municipality": selected_municipality,
        "start_url": start_url,
        "top_rows": sorted_rows[:20],
        "rows_found": len(rows),
    }


def _run_end_to_end_cli(argv: list[str], workflow: PropertyHunterEndToEndWorkflow | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the full end-to-end PropertyHunter workflow.")
    parser.add_argument("--start-url", default="https://www.funda.nl/zoeken/koop", help="Funda search start URL")
    parser.add_argument("--max-pages", type=int, default=1, help="Maximum paginated pages for source scanning")
    parser.add_argument("--timeout-seconds", type=float, default=12.0, help="HTTP timeout per request in seconds")
    parser.add_argument("--top", type=int, default=10, help="Number of top opportunity rows to show")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s - %(message)s")
    active_workflow = workflow or PROPERTY_HUNTER_WORKFLOW

    if not DATABASE_SERVICE.is_enabled and workflow is None:
        print("Database is not enabled. Set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY first.")
        return 1

    result = active_workflow.run_funda(
        start_url=str(args.start_url),
        max_pages=max(1, int(args.max_pages or 1)),
        timeout_seconds=float(args.timeout_seconds or 12.0),
        top_n=max(1, int(args.top or 10)),
    )

    print("E2E workflow resultaat")
    print(f"- aantal gevonden woningen: {result.listings_found}")
    print(f"- aantal nieuwe woningen: {result.new_listings}")
    print(f"- aantal verwerkte woningen: {result.processed_listings}")

    if result.warnings:
        print("Waarschuwingen")
        for warning in result.warnings[:20]:
            print(f"- {warning}")

    if not result.ok:
        print(f"Workflow mislukt: {result.error or 'Onbekende fout'}")
        return 1

    print("Top opportunity scores")
    if not result.top_rows:
        print("- Geen nieuwe listings om te tonen")
    else:
        for idx, row in enumerate(result.top_rows, start=1):
            print(
                f"{idx:>2}. {row.address} | vraagprijs={_format_currency(row.asking_price)} | "
                f"deal score={row.deal_score} | investment score={row.investment_score} | "
                f"listing history={row.listing_history} | bron={row.source}"
            )

    return 0


def _format_currency(value):
    if value in (None, ""):
        return "Onbekend"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "Onbekend"
    integer_value = int(round(number))
    formatted = f"{integer_value:,}".replace(",", ".")
    return f"€ {formatted}"


def _format_number(value):
    if value in (None, ""):
        return "Onbekend"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "Onbekend"
    integer_value = int(round(number))
    return f"{integer_value:,}".replace(",", ".")


def _format_dashboard_timestamp(value: str | None) -> str:
    if not isinstance(value, str) or not value.strip():
        return "Onbekend"
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _to_display_text(value) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, list):
        return ", ".join(str(item) for item in value)
    if isinstance(value, dict):
        return str(value)
    return str(value)


def _sanitize_table_cell(value: Any) -> Any:
    if isinstance(value, str):
        # Remove potential inline HTML fragments to keep table layout stable.
        stripped = re.sub(r"<[^>]+>", "", value)
        return " ".join(stripped.split())
    return value


def _table_column_width(key: str, label: str) -> str:
    token = f"{key} {label}".lower()
    if any(part in token for part in ("url", "adres", "address", "omschrijving", "description", "error", "waarschuwing", "warning")):
        return "large"
    if any(part in token for part in ("plaats", "city", "status", "bron", "source", "label", "recommendation")):
        return "medium"
    return "small"


def _build_table_column_config(rows: list[dict], columns: list[tuple[str, str]]) -> dict[str, Any]:
    config: dict[str, Any] = {}
    for label, key in columns:
        width = _table_column_width(key, label)
        config[key] = st.column_config.TextColumn(label=label, width=width)
    return config


def _render_interactive_table(rows: list[dict[str, Any]], *, columns: list[tuple[str, str]]):
    ordered_keys = [key for _, key in columns]
    header_cells = "".join(
        f"<th style='text-align:left;padding:0.55rem 0.75rem;border-bottom:1px solid #d9d9d9;white-space:nowrap;background:#f6f8fb;'>{escape(str(label))}</th>"
        for label, _ in columns
    )

    body_rows: list[str] = []
    for row in rows:
        cells = "".join(
            f"<td style='padding:0.5rem 0.75rem;border-bottom:1px solid #ececec;vertical-align:top;'>{escape(str(row.get(key, '')))}</td>"
            for key in ordered_keys
        )
        body_rows.append(f"<tr>{cells}</tr>")

    table_html = (
        "<div style='overflow-x:auto;border:1px solid #e6e6e6;border-radius:0.75rem;'>"
        "<table style='width:100%;border-collapse:collapse;font-size:0.92rem;'>"
        f"<thead><tr>{header_cells}</tr></thead>"
        f"<tbody>{''.join(body_rows)}</tbody>"
        "</table>"
        "</div>"
    )
    st.markdown(table_html, unsafe_allow_html=True)


def _render_compact_json_section(title: str, payload: Any, *, max_chars: int = 6000):
    st.markdown(title)
    serialized = json.dumps(_json_safe_value(payload), ensure_ascii=False, indent=2)
    if len(serialized) > max_chars:
        st.caption(f"Weergegeven als afgekort JSON-overzicht ({len(serialized)} tekens)")
        serialized = serialized[:max_chars] + "\n..."
    st.code(serialized, language="json")


def _render_rows_with_columns(rows: list[dict], columns: list[tuple[str, str]], empty_message: str):
    if not rows:
        st.info(empty_message)
        return

    ordered_keys = [key for _, key in columns]
    table_rows = []
    for row in rows:
        table_rows.append(
            {
                key: _to_display_text(_sanitize_table_cell(row.get(key)))
                for key in ordered_keys
            }
        )

    _render_interactive_table(table_rows, columns=columns)


def _render_deal_candidate_cards(candidates: list[dict]):
    if not candidates:
        st.info("Geen deal candidates gevonden voor de gekozen filters.")
        return


def _clamp_score_0_100(value: Any) -> int | None:
    numeric = _safe_score(value)
    if numeric is None:
        return None
    return max(0, min(100, int(numeric)))


def _parse_iso_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _extract_previous_asking_prices(snapshots: list[dict[str, Any]], current_price: float | None) -> list[float]:
    prices: list[float] = []
    for snapshot in snapshots:
        if not isinstance(snapshot, dict):
            continue
        value = _safe_number(snapshot.get("asking_price"))
        if value is None or value <= 0:
            continue
        prices.append(round(value, 2))

    if not prices:
        return []

    previous = prices[:-1] if len(prices) > 1 else []
    if current_price is not None:
        previous = [value for value in previous if abs(float(value) - float(current_price)) > 0.009]

    deduped: list[float] = []
    for value in previous:
        if deduped and abs(deduped[-1] - value) <= 0.009:
            continue
        deduped.append(value)
    return deduped


def _format_price_history(values: list[float]) -> str:
    if not values:
        return "Onbekend"
    return " -> ".join(_format_currency(value) for value in values)


def _woz_metrics(asking_price: float | None, woz_value: float | None) -> tuple[float | None, float | None]:
    if asking_price is None or woz_value in (None, 0):
        return None, None
    difference_eur = round(float(asking_price) - float(woz_value), 2)
    difference_pct = round(((float(asking_price) - float(woz_value)) / float(woz_value)) * 100.0, 2)
    return difference_eur, difference_pct


def _woz_pct_badge(value: float | None) -> str:
    if value is None:
        return "<span style='background:#757575;color:#FFFFFF;padding:2px 8px;border-radius:12px;font-weight:700;'>Niet beschikbaar</span>"
    if value < 0:
        color = "#2E7D32"
    elif value <= 10:
        color = "#EF6C00"
    else:
        color = "#C62828"
    return f"<span style='background:{color};color:#FFFFFF;padding:2px 8px;border-radius:12px;font-weight:700;'>{_format_percentage(value, decimals=2)}</span>"


def _derive_price_reduction_count(row: dict[str, Any], snapshots: list[dict[str, Any]]) -> int:
    explicit = _safe_score(row.get("price_reduction_count"))
    if explicit is not None and explicit >= 0:
        return int(explicit)

    price_points = [
        _safe_number(snapshot.get("asking_price"))
        for snapshot in snapshots
        if isinstance(snapshot, dict) and _safe_number(snapshot.get("asking_price")) is not None
    ]
    reductions = 0
    for previous, current in zip(price_points, price_points[1:]):
        if previous is not None and current is not None and current < previous:
            reductions += 1
    return reductions


def _derive_days_on_market(row: dict[str, Any], listing: dict[str, Any], snapshots: list[dict[str, Any]]) -> int | None:
    explicit = _safe_score(row.get("days_on_market"))
    if explicit is not None:
        return explicit

    listed_since = _listing_value(listing, "listed_since")
    calculated = calculate_days_on_market(listed_since) if listed_since not in (None, "") else None
    if calculated is not None:
        return calculated

    observed_dates = [
        _parse_iso_datetime(snapshot.get("observed_at"))
        for snapshot in snapshots
        if isinstance(snapshot, dict)
    ]
    observed_dates = [value for value in observed_dates if value is not None]
    if not observed_dates:
        return None
    oldest = min(observed_dates)
    return calculate_days_on_market(oldest.date().isoformat())


def _deal_recommendation_from_score(score: int | None) -> str:
    if score is None:
        return "★ Avoid"
    if score >= 85:
        return "★★★★★ Exceptional"
    if score >= 70:
        return "★★★★ Strong Buy"
    if score >= 55:
        return "★★★ Consider"
    if score >= 40:
        return "★★ Weak"
    return "★ Avoid"


def _sort_deal_intelligence_rows(rows: list[dict[str, Any]], sort_choice: str) -> list[dict[str, Any]]:
    result = list(rows)
    numeric_desc_keys = {
        "Hoogste deal score": "deal_score",
        "Hoogste investment score": "investment_score",
        "Hoogste opportunity score": "opportunity_score",
        "Hoogste split potential": "split_potential",
        "Hoogste vertical extension potential": "vertical_extension_potential",
        "Hoogste rental potential": "rental_potential",
        "Hoogste renovation potential": "renovation_potential",
    }

    if sort_choice in numeric_desc_keys:
        key_name = numeric_desc_keys[sort_choice]
        result.sort(key=lambda item: _safe_number(item.get(key_name)) if _safe_number(item.get(key_name)) is not None else -1, reverse=True)
    elif sort_choice == "Laagste vraagprijs":
        result.sort(key=lambda item: _safe_number(item.get("vraagprijs")) if _safe_number(item.get("vraagprijs")) is not None else float("inf"))
    elif sort_choice == "Hoogste vraagprijs":
        result.sort(key=lambda item: _safe_number(item.get("vraagprijs")) if _safe_number(item.get("vraagprijs")) is not None else -1, reverse=True)
    elif sort_choice == "Nieuwste":
        result.sort(key=lambda item: str(item.get("detected_at") or ""), reverse=True)

    return result


def _build_deal_intelligence_rows(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = _build_propertyhunter_rows(candidates)
    if not rows:
        return []

    candidate_by_listing_id = {
        str(((candidate.get("listing") or {}).get("id") or "")).strip(): candidate
        for candidate in candidates
        if isinstance(candidate, dict)
    }

    for row in rows:
        listing_id = str(row.get("listing_id") or "").strip()
        candidate = candidate_by_listing_id.get(listing_id, {})
        listing = candidate.get("listing") if isinstance(candidate.get("listing"), dict) else {}
        snapshots = DATABASE_SERVICE.get_listing_snapshots(listing_id, limit=25) if listing_id else []

        current_price = _safe_number(row.get("vraagprijs"))
        previous_prices = _extract_previous_asking_prices(snapshots, current_price)
        row["vorige_vraagprijzen"] = _format_price_history(previous_prices)
        row["price_reduction_count"] = _derive_price_reduction_count(row, snapshots)
        row["days_on_market"] = _derive_days_on_market(row, listing, snapshots)
        row["deal_score"] = _clamp_score_0_100(candidate.get("score"))
        row["detected_at"] = candidate.get("detected_at")

        raw_payload = listing.get("raw_payload") if isinstance(listing.get("raw_payload"), dict) else {}
        woz_value = _safe_number(raw_payload.get("latest_woz_value") or listing.get("latest_woz_value"))
        woz_valuation_year = _safe_score(raw_payload.get("woz_valuation_year") or listing.get("woz_valuation_year"))
        bag_id = str(raw_payload.get("bag_id") or "").strip() or None
        bag_address_id = str(raw_payload.get("bag_address_id") or "").strip() or None
        bag_verblijfsobject_id = str(raw_payload.get("bag_verblijfsobject_id") or "").strip() or None
        bag_pand_id = str(raw_payload.get("bag_pand_id") or "").strip() or None
        bag_usage_purpose = str(raw_payload.get("bag_usage_purpose") or "").strip() or None
        bag_building_year = _safe_score(raw_payload.get("bag_building_year"))
        bag_confidence_score = _safe_score(raw_payload.get("bag_confidence_score"))
        bag_quality_flags = raw_payload.get("bag_quality_flags") if isinstance(raw_payload.get("bag_quality_flags"), list) else []
        woz_source = str(raw_payload.get("woz_source") or "").strip() or None
        woz_retrieval_date = str(raw_payload.get("woz_retrieval_date") or "").strip() or None

        row["woz_value"] = woz_value
        row["woz_valuation_year"] = woz_valuation_year
        row["bag_id"] = bag_id
        row["bag_address_id"] = bag_address_id
        row["bag_verblijfsobject_id"] = bag_verblijfsobject_id
        row["bag_pand_id"] = bag_pand_id
        row["bag_usage_purpose"] = bag_usage_purpose
        row["bag_bouwjaar"] = bag_building_year
        row["bag_confidence_score"] = bag_confidence_score
        row["bag_quality_flags"] = [str(item) for item in bag_quality_flags if str(item).strip()]
        row["woz_source"] = woz_source
        row["woz_retrieval_date"] = woz_retrieval_date
        row["funda_woonoppervlak"] = _safe_number(raw_payload.get("funda_living_area_m2") or row.get("funda_woonoppervlak"))
        row["bag_oppervlak"] = _safe_number(raw_payload.get("bag_official_floor_area_m2") or row.get("bag_oppervlak"))
        row["gebruikt_rekenoppervlak"] = _safe_number(raw_payload.get("calculation_area_m2") or row.get("gebruikt_rekenoppervlak") or row.get("woonoppervlak"))
        row["bron_rekenoppervlak"] = str(raw_payload.get("calculation_area_source") or row.get("bron_rekenoppervlak") or "Funda")
        row["funda_bag_verschil_m2"] = _safe_number(raw_payload.get("living_area_difference_m2") or row.get("funda_bag_verschil_m2"))
        row["funda_bag_verschil_pct"] = _safe_number(raw_payload.get("living_area_difference_percentage") or row.get("funda_bag_verschil_pct"))
        row["asking_price_per_m2"] = _safe_number(raw_payload.get("asking_price_per_m2") or row.get("asking_price_per_m2") or row.get("price_per_m2"))
        row["woz_per_m2"] = _safe_number(raw_payload.get("woz_value_per_m2") or row.get("woz_per_m2"))

        diff_eur, diff_pct = _woz_metrics(current_price, woz_value)
        row["asking_price_minus_woz_value"] = diff_eur
        row["asking_price_vs_woz_percentage"] = diff_pct

    return _score_rows_with_opportunity_intelligence(rows)


def _latest_scan_metrics(health_payload: dict[str, Any]) -> dict[str, int]:
    runs = health_payload.get("latest_scan_runs") if isinstance(health_payload, dict) else []
    if not isinstance(runs, list) or not runs:
        return {"found": 0, "new": 0, "changed": 0}

    latest = max(
        [item for item in runs if isinstance(item, dict)],
        key=lambda item: str(item.get("started_at") or item.get("created_at") or ""),
        default={},
    )
    return {
        "found": int(_safe_score(latest.get("items_found")) or 0),
        "new": int(_safe_score(latest.get("items_new")) or 0),
        "changed": int(_safe_score(latest.get("items_changed")) or 0),
    }


def _clear_deal_finder_refresh_state():
    st.session_state.pop("deal_candidate_select", None)


def _deal_candidate_listing_id(candidate: dict) -> str:
    listing = candidate.get("listing") or {}
    return str(listing.get("id") or "")


def _deal_candidate_label(candidate: dict) -> str:
    listing = candidate.get("listing") or {}
    return (
        f"{listing.get('title') or 'Onbekend'} | score {candidate.get('score') if candidate.get('score') is not None else 'n.v.t.'} | "
        f"{candidate.get('priority') or 'n.v.t.'}"
    )


def _resolve_selected_deal_candidate(candidates: list[dict]) -> dict:
    candidate_map = {_deal_candidate_listing_id(item): item for item in candidates if _deal_candidate_listing_id(item)}
    if not candidate_map:
        return {}

    candidate_ids = list(candidate_map.keys())
    preferred_listing_id = str(st.session_state.get("deal_selected_listing_id") or "")
    default_index = candidate_ids.index(preferred_listing_id) if preferred_listing_id in candidate_map else 0
    selected_listing_id = st.selectbox(
        "Selecteer listing",
        candidate_ids,
        index=default_index,
        key="deal_candidate_select",
        format_func=lambda listing_id: _deal_candidate_label(candidate_map.get(str(listing_id), {})),
    )
    st.session_state["deal_selected_listing_id"] = str(selected_listing_id or candidate_ids[0])
    return candidate_map.get(str(selected_listing_id), candidate_map[candidate_ids[0]])


def _listing_value(listing: dict[str, Any], *keys: str) -> Any:
    if not isinstance(listing, dict):
        return None
    raw_payload = listing.get("raw_payload") if isinstance(listing.get("raw_payload"), dict) else {}
    for key in keys:
        if key in listing and listing.get(key) not in (None, ""):
            return listing.get(key)
        if key in raw_payload and raw_payload.get(key) not in (None, ""):
            return raw_payload.get(key)
    return None


def _propertyhunter_listing_history_text(listing: dict[str, Any]) -> str:
    status = str(_listing_value(listing, "listing_status") or "unknown")
    reduction_pct = _safe_number(_listing_value(listing, "total_price_reduction_percentage"))
    if reduction_pct is not None:
        return f"status={status}, {int(round(reduction_pct))}% prijsdaling"
    days_on_market = _safe_score(_listing_value(listing, "days_on_market"))
    if days_on_market is not None:
        return f"status={status}, {days_on_market} dagen"
    return f"status={status}"


def _source_display_name(source_name: Any, source_url: Any = None) -> str:
    raw_name = str(source_name or "").strip()
    normalized_name = raw_name.lower()
    if normalized_name in {"klusvastgoed", "klusvastgoed.nl"}:
        return "Klusvastgoed"

    raw_url = str(source_url or "").strip().lower()
    if "klusvastgoed.nl" in raw_url:
        return "Klusvastgoed"

    return raw_name or "Onbekend"


def _build_propertyhunter_rows(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for candidate in candidates:
        listing = candidate.get("listing") if isinstance(candidate.get("listing"), dict) else {}
        source = candidate.get("source") if isinstance(candidate.get("source"), dict) else {}
        listing_id = str(listing.get("id") or "").strip()
        if not listing_id:
            continue

        rows.append(
            {
                "listing_id": listing_id,
                "adres": _listing_value(listing, "address") or "Onbekend",
                "plaats": _listing_value(listing, "city") or "Onbekend",
                "vraagprijs": _safe_number(_listing_value(listing, "asking_price")),
                "asking_price_status": str(_listing_value(listing, "asking_price_status") or "unknown"),
                "asking_price_text": str(_listing_value(listing, "asking_price_text") or ""),
                "woonoppervlak": _safe_number(_listing_value(listing, "surface_m2", "living_area")),
                "funda_woonoppervlak": _safe_number(_listing_value(listing, "funda_living_area_m2", "surface_m2", "living_area")),
                "bag_oppervlak": _safe_number(_listing_value(listing, "bag_official_floor_area_m2")),
                "gebruikt_rekenoppervlak": _safe_number(_listing_value(listing, "calculation_area_m2", "surface_m2", "living_area")),
                "bron_rekenoppervlak": str(_listing_value(listing, "calculation_area_source") or "Funda"),
                "funda_bag_verschil_m2": _safe_number(_listing_value(listing, "living_area_difference_m2")),
                "funda_bag_verschil_pct": _safe_number(_listing_value(listing, "living_area_difference_percentage")),
                "perceel": _safe_number(_listing_value(listing, "plot_size_m2", "plot_size")),
                "slaapkamers": _safe_score(_listing_value(listing, "bedrooms")),
                "energielabel": str(_listing_value(listing, "energy_label") or "Onbekend"),
                "bouwjaar": _safe_score(_listing_value(listing, "construction_year", "bag_building_year")),
                "price_per_m2": _safe_number(_listing_value(listing, "asking_price_per_m2", "price_per_m2")),
                "asking_price_per_m2": _safe_number(_listing_value(listing, "asking_price_per_m2", "price_per_m2")),
                "neighborhood_price_per_m2": _safe_number(_listing_value(listing, "neighborhood_m2_price_average", "neighbourhood_m2_price_average", "neighborhood_price_per_m2")),
                "woz_per_m2": _safe_number(_listing_value(listing, "woz_value_per_m2")),
                "bag_usage_purpose": str(_listing_value(listing, "bag_usage_purpose") or ""),
                "bag_bouwjaar": _safe_score(_listing_value(listing, "bag_building_year")),
                "bag_confidence_score": _safe_score(_listing_value(listing, "bag_confidence_score", "confidence_score")),
                "bag_quality_flags": _listing_value(listing, "bag_quality_flags") or [],
                "price_reduction_count": _safe_score(_listing_value(listing, "price_reduction_count")),
                "days_on_market": _safe_score(_listing_value(listing, "days_on_market")),
                "listing_history": _propertyhunter_listing_history_text(listing),
                "listed_since": str(_listing_value(listing, "listed_since") or ""),
                "source_timestamp": str(_listing_value(listing, "source_timestamp", "timestamp") or ""),
                "investment_score": _safe_score(candidate.get("investment_score")),
                "opportunity_score": _safe_score(candidate.get("hidden_value_score") if candidate.get("hidden_value_score") is not None else candidate.get("score")),
                "bron": _source_display_name(source.get("name"), _listing_value(listing, "source_url")),
                "source_url": str(_listing_value(listing, "source_url") or ""),
            }
        )
    return rows


def _filter_propertyhunter_rows(
    rows: list[dict[str, Any]],
    *,
    place: str | None,
    min_price: float | None,
    max_price: float | None,
    min_surface: float | None,
    energy_label: str | None,
    min_opportunity_score: int | None,
) -> list[dict[str, Any]]:
    filtered: list[dict[str, Any]] = []
    for row in rows:
        row_place = str(row.get("plaats") or "").strip()
        row_price = _safe_number(row.get("vraagprijs"))
        row_surface = _safe_number(row.get("woonoppervlak"))
        row_energy = str(row.get("energielabel") or "").strip()
        row_opp = _safe_score(row.get("opportunity_score"))

        if isinstance(place, str) and place.strip() and row_place != place.strip():
            continue
        if min_price is not None and (row_price is None or row_price < float(min_price)):
            continue
        if max_price is not None and (row_price is None or row_price > float(max_price)):
            continue
        if min_surface is not None and (row_surface is None or row_surface < float(min_surface)):
            continue
        if isinstance(energy_label, str) and energy_label.strip() and row_energy.upper() != energy_label.strip().upper():
            continue
        if min_opportunity_score is not None and (row_opp is None or row_opp < int(min_opportunity_score)):
            continue

        filtered.append(row)
    return filtered


def _build_funda_start_url(
    city: str,
    *,
    min_price: int | None,
    max_price: int | None,
    min_living_area: int | None,
) -> str:
    city_slug = normalize_funda_area_slug(city)
    if not city_slug:
        city_slug = "nederland"

    base_url = "https://www.funda.nl/zoeken/koop/"

    params: dict[str, str] = {
        "selected_area": json.dumps([city_slug], ensure_ascii=True),
    }
    if min_price is not None or max_price is not None:
        lower = str(int(min_price)) if min_price is not None and int(min_price) > 0 else ""
        upper = str(int(max_price)) if max_price is not None and int(max_price) > 0 else ""
        params["price"] = f'"{lower}-{upper}"'
    if min_living_area is not None and int(min_living_area) > 0:
        params["floor_area"] = f'"{int(min_living_area)}-"'

    return f"{base_url}?{urlencode(params)}"


def _probe_http_status(url: str, *, timeout_seconds: float = 12.0) -> tuple[int | None, str | None]:
    try:
        response = requests.get(
            str(url).strip(),
            headers={"User-Agent": "Mozilla/5.0 (compatible; PropertyHunterAI-DealFinder/1.0)"},
            timeout=max(1.0, float(timeout_seconds or 12.0)),
        )
        return int(response.status_code), None
    except Exception as error:
        return None, f"{type(error).__name__}: {error}"


def _normalize_city_input(city: str) -> str:
    return " ".join(str(city or "").split()).strip()


def _normalize_address_input(address: str) -> str:
    return " ".join(str(address or "").strip().casefold().split())


@st.cache_data(ttl=24 * 60 * 60, show_spinner=False)
def _load_funda_place_options() -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()

    def add_places(values: list[str]):
        for value in values:
            normalized = _normalize_city_input(value)
            if not normalized:
                continue
            key = normalized.casefold()
            if key in seen:
                continue
            seen.add(key)
            merged.append(normalized)

    add_places(get_dutch_municipalities())
    add_places(list(FUNDA_PLACE_ALIASES))

    # Keep defaults selectable even when municipality metadata is tijdelijk niet beschikbaar.
    add_places(list(FUNDA_DEFAULT_SCAN_CITIES))

    return sorted(merged, key=lambda value: value.casefold())


def _selected_municipality_summary(selected: list[str]) -> str:
    total = len(selected)
    if total == 0:
        return "0 gemeenten geselecteerd"

    shown = selected[:5]
    base = f"{total} gemeenten geselecteerd: {', '.join(shown)}"
    remaining = total - len(shown)
    if remaining > 0:
        return f"{base} en nog {remaining} gemeenten"
    return base


def _merge_scan_cities(selected_cities: list[str], custom_city: str) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()

    for city in [*selected_cities, custom_city]:
        normalized = _normalize_city_input(city)
        if not normalized:
            continue
        key = normalized.casefold()
        if key in seen:
            continue
        seen.add(key)
        merged.append(normalized)

    return merged


def _scan_data_origin_label(*, latest_scan_result: dict[str, Any] | None, database_enabled: bool) -> str:
    if isinstance(latest_scan_result, dict):
        mode = str(latest_scan_result.get("mode") or "").strip().lower()
        if mode == "live":
            return "Dataherkomst: live scraperresultaten van de laatste handmatige scan."
        if mode == "dry-run":
            return "Dataherkomst: live scraperresultaten (dry-run, niet blijvend opgeslagen)."
    if database_enabled:
        return "Dataherkomst: opgeslagen listings/dealcandidates uit Supabase."
    return "Dataherkomst: geen live databron beschikbaar (mock/offline toestand)."


def _build_rows_from_scan_properties(properties: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in properties:
        if not isinstance(item, dict):
            continue
        property_data = item.get("property") if isinstance(item.get("property"), dict) else {}
        if not property_data:
            continue

        raw_extracted = property_data.get("raw_extracted_data") if isinstance(property_data.get("raw_extracted_data"), dict) else {}
        raw_payload = property_data.get("raw_payload") if isinstance(property_data.get("raw_payload"), dict) else {}

        def value(*keys: str) -> Any:
            for key in keys:
                if property_data.get(key) not in (None, ""):
                    return property_data.get(key)
                if raw_extracted.get(key) not in (None, ""):
                    return raw_extracted.get(key)
                if raw_payload.get(key) not in (None, ""):
                    return raw_payload.get(key)
            return None

        rows.append(
            {
                "listing_id": str(value("listing_id") or "").strip(),
                "adres": value("address") or "Onbekend",
                "plaats": value("city") or "Onbekend",
                "vraagprijs": _safe_number(value("asking_price")),
                "asking_price_status": str(value("asking_price_status") or "unknown"),
                "asking_price_text": str(value("asking_price_text") or ""),
                "woonoppervlak": _safe_number(value("surface_m2", "living_area", "bag_official_floor_area_m2")),
                "funda_woonoppervlak": _safe_number(value("funda_living_area_m2", "surface_m2", "living_area")),
                "bag_oppervlak": _safe_number(value("bag_official_floor_area_m2")),
                "gebruikt_rekenoppervlak": _safe_number(value("calculation_area_m2", "surface_m2", "living_area")),
                "bron_rekenoppervlak": str(value("calculation_area_source") or "Funda"),
                "funda_bag_verschil_m2": _safe_number(value("living_area_difference_m2")),
                "funda_bag_verschil_pct": _safe_number(value("living_area_difference_percentage")),
                "perceel": _safe_number(value("plot_size_m2", "plot_size")),
                "slaapkamers": _safe_score(value("bedrooms")),
                "energielabel": str(value("energy_label") or "Onbekend"),
                "bouwjaar": _safe_score(value("construction_year", "bag_building_year")),
                "price_reduction_count": _safe_score(value("price_reduction_count")),
                "listed_since": str(value("listed_since") or ""),
                "source_timestamp": str(value("source_timestamp", "timestamp") or ""),
                "price_per_m2": _safe_number(value("asking_price_per_m2", "price_per_m2")),
                "asking_price_per_m2": _safe_number(value("asking_price_per_m2", "price_per_m2")),
                "neighborhood_price_per_m2": _safe_number(value("neighborhood_m2_price_average", "neighbourhood_m2_price_average", "neighborhood_price_per_m2")),
                "woz_per_m2": _safe_number(value("woz_value_per_m2")),
                "bag_usage_purpose": str(value("bag_usage_purpose") or ""),
                "bag_bouwjaar": _safe_score(value("bag_building_year")),
                "bag_confidence_score": _safe_score(value("bag_confidence_score", "confidence_score")),
                "bag_quality_flags": value("bag_quality_flags") or [],
                "investment_score": None,
                "opportunity_score": None,
                "bron": _source_display_name(value("source_name", "source"), value("source_url")),
                "source_url": str(value("source_url") or ""),
            }
        )
    return rows


def _build_rows_from_listing_ids(listing_ids: list[str]) -> tuple[list[dict[str, Any]], int]:
    rows: list[dict[str, Any]] = []
    price_reductions = 0
    seen: set[str] = set()

    for listing_id in listing_ids:
        normalized_id = str(listing_id or "").strip()
        if not normalized_id or normalized_id in seen:
            continue
        seen.add(normalized_id)

        detail = DATABASE_SERVICE.get_listing_detail(normalized_id)
        listing = detail.get("listing") if isinstance(detail.get("listing"), dict) else {}
        if not listing:
            continue

        source = detail.get("source") if isinstance(detail.get("source"), dict) else {}
        candidate = detail.get("candidate") if isinstance(detail.get("candidate"), dict) else {}

        if int(_safe_score(listing.get("price_reduction_count")) or 0) > 0:
            price_reductions += 1

        rows.extend(
            _build_propertyhunter_rows(
                [
                    {
                        "listing": listing,
                        "source": source,
                        "investment_score": _safe_score(candidate.get("investment_score")),
                        "hidden_value_score": _safe_score(candidate.get("hidden_value_score")),
                        "score": _safe_score(candidate.get("hidden_value_score")),
                    }
                ]
            )
        )

    return rows, price_reductions


def _compute_row_price_per_m2(row: dict[str, Any]) -> float | None:
    explicit = _safe_number(row.get("asking_price_per_m2"))
    if explicit is None:
        explicit = _safe_number(row.get("price_per_m2"))
    if explicit is not None:
        return explicit
    price = _safe_number(row.get("vraagprijs"))
    living_area = _safe_number(row.get("gebruikt_rekenoppervlak"))
    if living_area is None:
        living_area = _safe_number(row.get("woonoppervlak"))
    if price is None or living_area in (None, 0):
        return None
    return round(price / living_area, 2)


def _compute_market_discount_ratio(*, asking_price_per_m2: float | None, neighborhood_price_per_m2: float | None) -> float | None:
    if asking_price_per_m2 is None:
        return None
    if neighborhood_price_per_m2 in (None, 0):
        return None
    return (float(neighborhood_price_per_m2) - float(asking_price_per_m2)) / float(neighborhood_price_per_m2)


def _market_discount_score(discount_ratio: float | None) -> int | None:
    if discount_ratio is None:
        return None
    # 0% discount -> 0, 100% discount -> 100; negative discounts are clamped to 0.
    return max(0, min(100, int(round(float(discount_ratio) * 100.0))))


def _enrich_market_discount_metrics(row: dict[str, Any]) -> dict[str, Any]:
    asking_ppm2 = _compute_row_price_per_m2(row)
    neighborhood_ppm2 = _safe_number(
        row.get("neighborhood_price_per_m2")
        or row.get("neighborhood_m2_price_average")
        or row.get("neighbourhood_m2_price_average")
    )
    discount_ratio = _compute_market_discount_ratio(
        asking_price_per_m2=asking_ppm2,
        neighborhood_price_per_m2=neighborhood_ppm2,
    )
    return {
        **row,
        "asking_price_per_m2": asking_ppm2,
        "price_per_m2": asking_ppm2,
        "neighborhood_price_per_m2": neighborhood_ppm2,
        "discount_percentage": round(discount_ratio * 100.0, 2) if discount_ratio is not None else None,
        "market_discount_score": _market_discount_score(discount_ratio),
        "is_market_discount_highlight": bool(discount_ratio is not None and discount_ratio >= 0.25),
    }


def _score_rows_with_opportunity_intelligence(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not rows:
        return []

    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        city_key = str(row.get("plaats") or "Onbekend").strip().lower()
        grouped.setdefault(city_key, []).append(row)

    for city_rows in grouped.values():
        city_prices = [_safe_number(item.get("vraagprijs")) for item in city_rows if _safe_number(item.get("vraagprijs")) is not None]
        city_ppm2 = [_compute_row_price_per_m2(item) for item in city_rows if _compute_row_price_per_m2(item) is not None]
        sorted_ppm2 = sorted(float(value) for value in city_ppm2)

        avg_ppm2 = (sum(city_ppm2) / len(city_ppm2)) if city_ppm2 else None
        sorted_prices = sorted(float(item) for item in city_prices)
        median_price = None
        if sorted_prices:
            middle = len(sorted_prices) // 2
            if len(sorted_prices) % 2 == 1:
                median_price = sorted_prices[middle]
            else:
                median_price = (sorted_prices[middle - 1] + sorted_prices[middle]) / 2.0

        for row in city_rows:
            row_price = _safe_number(row.get("vraagprijs"))
            row_area = _safe_number(row.get("woonoppervlak"))
            row_plot = _safe_number(row.get("perceel"))
            row_energy = str(row.get("energielabel") or "").strip().upper()
            row_year = _safe_score(row.get("bouwjaar"))
            row_bedrooms = _safe_score(row.get("slaapkamers"))
            row_days = _safe_score(row.get("days_on_market"))
            row_reductions = _safe_score(row.get("price_reduction_count")) or 0
            row_ppm2 = _compute_row_price_per_m2(row)
            row["price_per_m2"] = row_ppm2
            row["asking_price_per_m2"] = row_ppm2
            neighborhood_ppm2 = _safe_number(
                row.get("neighborhood_price_per_m2")
                or row.get("neighborhood_m2_price_average")
                or row.get("neighbourhood_m2_price_average")
            )
            row["neighborhood_price_per_m2"] = neighborhood_ppm2
            discount_ratio = _compute_market_discount_ratio(
                asking_price_per_m2=row_ppm2,
                neighborhood_price_per_m2=neighborhood_ppm2,
            )
            row["discount_percentage"] = round(discount_ratio * 100.0, 2) if discount_ratio is not None else None
            row["market_discount_score"] = _market_discount_score(discount_ratio)
            row["is_market_discount_highlight"] = bool(discount_ratio is not None and discount_ratio >= 0.25)
            row["city_avg_price_per_m2"] = round(avg_ppm2, 2) if avg_ppm2 is not None else None
            row["difference_vs_city_avg_pct"] = None
            if row_ppm2 is not None and avg_ppm2 is not None and avg_ppm2 > 0:
                row["difference_vs_city_avg_pct"] = round(((row_ppm2 - avg_ppm2) / avg_ppm2) * 100.0, 2)

            investment_score = _safe_score(row.get("investment_score"))
            if investment_score is None:
                score = 0
                if row_ppm2 is not None and avg_ppm2 is not None and row_ppm2 < avg_ppm2:
                    score += 25
                if row_area is not None and row_area > 100:
                    score += 20
                if row_energy in {"A", "A+", "A++", "A+++", "A++++", "B"}:
                    score += 15
                if row_year is not None and row_year > 1995:
                    score += 15
                if row_price is not None and median_price is not None and row_price < median_price:
                    score += 15
                if row_plot is not None and row_plot > 150:
                    score += 10
                investment_score = max(0, min(100, score))
            row["investment_score"] = max(0, min(100, int(investment_score))) if investment_score is not None else None
            investment_score = row.get("investment_score")

            price_per_m2_percentile = None
            if row_ppm2 is not None and sorted_ppm2:
                lower_count = sum(1 for value in sorted_ppm2 if value <= row_ppm2)
                percentile = (lower_count / len(sorted_ppm2)) * 100.0
                # Lower price per m² is better, so invert percentile for opportunity component.
                price_per_m2_percentile = max(0.0, min(100.0, 100.0 - percentile))
            row["price_per_m2_percentile"] = price_per_m2_percentile

            opportunity_score = _safe_score(row.get("opportunity_score"))
            if opportunity_score is None:
                percentile_component = 0.0
                if price_per_m2_percentile is not None:
                    percentile_component = (price_per_m2_percentile / 100.0) * 20.0

                completeness_fields = [row_area, row_energy if row_energy and row_energy != "ONBEKEND" else None, row_year, row_plot, row_bedrooms, row_ppm2]
                completeness_count = sum(1 for value in completeness_fields if value not in (None, "", 0))
                completeness_component = (completeness_count / len(completeness_fields)) * 15.0

                upside_component = 0.0
                if int(_safe_score(row.get("price_reduction_count")) or 0) > 0:
                    upside_component += 5.0
                if row_year is not None and row_year < 1990:
                    upside_component += 3.0
                if row_energy in {"D", "E", "F", "G"}:
                    upside_component += 2.0
                upside_component = max(0.0, min(10.0, upside_component))

                calculated = (float(investment_score or 0) * 0.55) + percentile_component + completeness_component + upside_component
                opportunity_score = max(0, min(100, int(round(calculated))))
            row["opportunity_score"] = max(0, min(100, int(opportunity_score))) if opportunity_score is not None else None

            diff_pct = _safe_number(row.get("difference_vs_city_avg_pct"))

            split_score = 20
            if row_area is not None:
                if row_area >= 140:
                    split_score += 30
                elif row_area >= 110:
                    split_score += 20
                elif row_area >= 90:
                    split_score += 10
            if row_bedrooms is not None:
                if row_bedrooms >= 4:
                    split_score += 20
                elif row_bedrooms >= 3:
                    split_score += 10
            if row_plot is not None and row_plot > 180:
                split_score += 10
            if row_energy in {"A", "A+", "A++", "A+++", "A++++", "B"}:
                split_score += 5
            if row_days is not None and row_days > 60:
                split_score += 10
            if diff_pct is not None:
                if diff_pct <= -10:
                    split_score += 15
                elif diff_pct < 0:
                    split_score += 8
            split_score = max(0, min(100, split_score))

            vertical_score = 15
            if row_plot is not None and row_area is not None and row_area > 0:
                ratio = row_plot / row_area
                if ratio >= 2.0:
                    vertical_score += 35
                elif ratio >= 1.5:
                    vertical_score += 25
                elif ratio >= 1.2:
                    vertical_score += 15
            if row_year is not None:
                if row_year < 1980:
                    vertical_score += 15
                elif row_year < 2000:
                    vertical_score += 8
            if row_plot is not None:
                if row_plot > 200:
                    vertical_score += 20
                elif row_plot > 150:
                    vertical_score += 12
            if diff_pct is not None and diff_pct < 0:
                vertical_score += 10
            vertical_score = max(0, min(100, vertical_score))

            rental_score = 20
            if diff_pct is not None:
                if diff_pct <= -15:
                    rental_score += 25
                elif diff_pct < 0:
                    rental_score += 15
            if row_area is not None:
                if 50 <= row_area <= 120:
                    rental_score += 15
                elif row_area <= 150:
                    rental_score += 8
            if row_bedrooms is not None:
                if row_bedrooms >= 2:
                    rental_score += 15
                elif row_bedrooms == 1:
                    rental_score += 8
            if row_energy in {"A", "A+", "A++", "A+++", "A++++", "B", "C"}:
                rental_score += 10
            if row_days is not None and row_days > 45:
                rental_score += 10
            rental_score = max(0, min(100, rental_score))

            renovation_score = 10
            if row_year is not None:
                if row_year < 1980:
                    renovation_score += 30
                elif row_year < 1995:
                    renovation_score += 20
            if row_energy in {"D", "E", "F", "G"}:
                renovation_score += 25
            elif row_energy == "C":
                renovation_score += 10
            if row_reductions > 0:
                renovation_score += min(10, row_reductions * 5)
            if diff_pct is not None and diff_pct <= -8:
                renovation_score += 15
            if row_days is not None and row_days > 60:
                renovation_score += 10
            renovation_score = max(0, min(100, renovation_score))

            row["split_potential"] = split_score
            row["vertical_extension_potential"] = vertical_score
            row["rental_potential"] = rental_score
            row["renovation_potential"] = renovation_score

            overall_score = int(
                round(
                    (0.35 * float(row.get("investment_score") or 0))
                    + (0.25 * float(row.get("opportunity_score") or 0))
                    + (0.15 * float(split_score))
                    + (0.15 * float(rental_score))
                    + (0.10 * float(renovation_score))
                )
            )
            overall_score = max(0, min(100, overall_score))
            row["overall_investment_score"] = overall_score
            row["investment_recommendation"] = _deal_recommendation_from_score(overall_score)
    return rows


def _score_badge(value: Any) -> str:
    score = _safe_score(value)
    if score is None:
        return "<span style='background:#9E9E9E;color:#FFFFFF;padding:2px 8px;border-radius:12px;font-weight:600;'>Onbekend</span>"
    if score >= 90:
        color = "#1B5E20"
    elif score >= 80:
        color = "#2E7D32"
    elif score >= 70:
        color = "#EF6C00"
    else:
        color = "#757575"
    return f"<span style='background:{color};color:#FFFFFF;padding:2px 8px;border-radius:12px;font-weight:700;'>{score}</span>"


def _render_opportunity_top_rows(rows: list[dict[str, Any]]):
    _render_rows_with_columns(
        rows=rows,
        columns=[
            ("Address", "address"),
            ("City", "city"),
            ("Price", "asking_price"),
            ("Living area", "living_area"),
            ("Price per m²", "price_per_m2"),
            ("Investment score", "investment_score"),
            ("Opportunity score", "opportunity_score"),
        ],
        empty_message="Geen resultaten beschikbaar voor de geselecteerde scaninstellingen.",
    )


def _run_funda_scan_from_ui(
    *,
    cities: list[str],
    min_price: int,
    max_price: int,
    min_living_area: int,
    max_pages_per_city: int,
    dry_run: bool,
) -> dict[str, Any]:
    started_at = time.perf_counter()
    per_city_results: list[dict[str, Any]] = []
    per_city_rows: dict[str, list[dict[str, Any]]] = {}
    all_rows: list[dict[str, Any]] = []
    seen_row_keys: set[str] = set()

    listings_found = 0
    listings_imported = 0
    new_listings = 0
    changed_listings = 0
    unchanged_listings = 0
    failed_listings = 0
    price_reductions = 0

    selected_cities = [str(city).strip() for city in cities if str(city).strip()]
    if not selected_cities:
        raise ValueError("Selecteer minimaal een stad.")

    for city in selected_cities:
        start_url = _build_funda_start_url(
            city,
            min_price=min_price if min_price > 0 else None,
            max_price=max_price if max_price > 0 else None,
            min_living_area=min_living_area if min_living_area > 0 else None,
        )
        http_status, http_error = _probe_http_status(start_url, timeout_seconds=12.0)

        try:
            if dry_run:
                city_result = _run_source_scan(
                    "funda",
                    orchestrator=DEAL_FINDER_ORCHESTRATOR,
                    database_service=DatabaseService(),
                    output_dir=Path("output") / "scan-runs",
                    start_url=start_url,
                    max_pages=max(1, int(max_pages_per_city or 1)),
                    timeout_seconds=12.0,
                    force_refresh=True,
                )
                if not city_result.get("ok"):
                    raise RuntimeError(city_result.get("error") or "onbekende fout")

                listings_found += int(city_result.get("listings_found") or 0)
                listings_imported += int(city_result.get("listings_imported") or 0)
                failed_listings += int(city_result.get("listings_failed") or 0)
                unchanged_listings += int(city_result.get("listings_imported") or 0)

                city_output_path = str(city_result.get("output_path") or "").strip()
                city_rows: list[dict[str, Any]] = []
                city_price_reductions = 0
                if city_output_path:
                    try:
                        payload = json.loads(Path(city_output_path).read_text(encoding="utf-8"))
                        properties = payload.get("properties") if isinstance(payload, dict) else []
                        if isinstance(properties, list):
                            city_rows = _build_rows_from_scan_properties(properties)
                            city_price_reductions = sum(
                                1
                                for row in city_rows
                                if int(_safe_score((row or {}).get("price_reduction_count")) or 0) > 0
                            )
                    except Exception:
                        city_rows = []
                        city_price_reductions = 0

                for row in city_rows:
                    row_key = str(row.get("listing_id") or row.get("source_url") or f"{row.get('adres')}::{row.get('plaats')}").strip().casefold()
                    if row_key and row_key in seen_row_keys:
                        continue
                    if row_key:
                        seen_row_keys.add(row_key)
                    all_rows.append(row)
                per_city_rows[city] = city_rows
                price_reductions += city_price_reductions
                per_city_results.append(
                    {
                        "city": city,
                        "mode": "dry-run",
                        "start_url": start_url,
                        "http_status": http_status,
                        "http_status_error": http_error,
                        "result": city_result,
                        "rows_found": len(city_rows),
                        "ok": True,
                        "error": None,
                    }
                )
                continue

            import_result = DEAL_FINDER_ORCHESTRATOR.import_from_source(
                "funda",
                {
                    "start_url": start_url,
                    "max_pages": max(1, int(max_pages_per_city or 1)),
                    "timeout_seconds": 12.0,
                    "force_refresh": True,
                },
            )
            if not import_result.get("ok"):
                raise RuntimeError(import_result.get("error") or "onbekende fout")

            city_found = int(import_result.get("listings_found") or 0)
            city_new = int(import_result.get("new") or 0)
            city_changed = int(import_result.get("changed") or 0)
            city_imported = int(import_result.get("listings_imported") or 0)
            city_failed = int(import_result.get("failed_listings") or 0)
            city_unchanged = max(0, city_imported - city_new - city_changed)

            listings_found += city_found
            listings_imported += city_imported
            new_listings += city_new
            changed_listings += city_changed
            failed_listings += city_failed
            unchanged_listings += city_unchanged

            city_listing_ids = [str(item).strip() for item in (import_result.get("listing_ids") or []) if str(item).strip()]
            city_rows, city_price_reductions = _build_rows_from_listing_ids(city_listing_ids)
            for row in city_rows:
                row_key = str(row.get("listing_id") or row.get("source_url") or f"{row.get('adres')}::{row.get('plaats')}").strip().casefold()
                if row_key and row_key in seen_row_keys:
                    continue
                if row_key:
                    seen_row_keys.add(row_key)
                all_rows.append(row)
            per_city_rows[city] = city_rows
            price_reductions += city_price_reductions

            per_city_results.append(
                {
                    "city": city,
                    "mode": "live",
                    "start_url": start_url,
                    "http_status": http_status,
                    "http_status_error": http_error,
                    "result": import_result,
                    "rows_found": len(city_rows),
                    "ok": True,
                    "error": None,
                }
            )
        except Exception as error:
            per_city_results.append(
                {
                    "city": city,
                    "mode": "dry-run" if dry_run else "live",
                    "start_url": start_url,
                    "http_status": http_status,
                    "http_status_error": http_error,
                    "result": {},
                    "rows_found": 0,
                    "ok": False,
                    "error": f"{type(error).__name__}: {error}",
                }
            )
            continue

    scored_rows = _score_rows_with_opportunity_intelligence(all_rows)
    sorted_rows = sorted(scored_rows, key=lambda item: _safe_score(item.get("opportunity_score")) or -1, reverse=True)
    top_rows = sorted_rows[:20]
    rows_by_city: dict[str, list[dict[str, Any]]] = {}
    for row in sorted_rows:
        city_name = str(row.get("plaats") or "Onbekend").strip() or "Onbekend"
        city_rows = rows_by_city.setdefault(city_name, [])
        if len(city_rows) < 20:
            city_rows.append(row)
    duration_seconds = round(time.perf_counter() - started_at, 3)

    output_payload = {
        "source": "funda.nl",
        "mode": "dry-run" if dry_run else "live",
        "cities": selected_cities,
        "filters": {
            "min_price": min_price,
            "max_price": max_price,
            "min_living_area": min_living_area,
            "max_pages_per_city": max_pages_per_city,
        },
        "summary": {
            "listings_found": listings_found,
            "listings_imported": listings_imported,
            "new_listings": new_listings,
            "changed_listings": changed_listings,
            "unchanged_listings": unchanged_listings,
            "price_reductions": price_reductions,
            "failed_listings": failed_listings,
            "duration_seconds": duration_seconds,
            "rows_returned": len(top_rows),
            "failed_cities": sum(1 for item in per_city_results if not bool(item.get("ok"))),
        },
        "per_city_results": per_city_results,
        "rows_by_city": rows_by_city,
        "top_rows": top_rows,
    }
    output_path = _write_scan_output(Path("output") / "scan-runs", output_payload)

    return {
        "ok": True,
        "mode": "dry-run" if dry_run else "live",
        "listings_found": listings_found,
        "listings_imported": listings_imported,
        "new_listings": new_listings,
        "changed_listings": changed_listings,
        "unchanged_listings": unchanged_listings,
        "price_reductions": price_reductions,
        "failed_listings": failed_listings,
        "duration_seconds": duration_seconds,
        "output_path": str(output_path),
        "top_rows": top_rows,
        "per_city_results": per_city_results,
        "rows_by_city": rows_by_city,
        "failed_cities": sum(1 for item in per_city_results if not bool(item.get("ok"))),
    }


def _render_propertyhunter_detail_page(listing_id: str):
    detail = DATABASE_SERVICE.get_listing_detail(listing_id)
    listing = detail.get("listing") if isinstance(detail.get("listing"), dict) else {}
    source = detail.get("source") if isinstance(detail.get("source"), dict) else {}
    candidate = detail.get("candidate") if isinstance(detail.get("candidate"), dict) else {}
    latest_snapshot = detail.get("latest_snapshot") if isinstance(detail.get("latest_snapshot"), dict) else {}
    snapshots = detail.get("snapshots") if isinstance(detail.get("snapshots"), list) else []

    if not listing:
        st.warning("De geselecteerde woning is niet meer beschikbaar.")
        if st.button("Terug naar overzicht", key="ph_back_missing"):
            st.session_state["ph_view"] = "list"
            st.session_state.pop("ph_selected_listing_id", None)
            st.query_params.clear()
            st.rerun()
        return

    if st.button("Terug naar overzicht", key="ph_back_button", type="secondary"):
        st.session_state["ph_view"] = "list"
        st.query_params.clear()
        st.rerun()

    st.subheader("Woningdetails")
    st.markdown(f"### {listing.get('title') or listing.get('address') or 'Onbekend object'}")
    st.write(f"Adres: {listing.get('address') or 'Onbekend'}")
    st.write(f"Plaats: {listing.get('city') or 'Onbekend'}")
    st.write(f"Vraagprijs: {_format_currency(_listing_value(listing, 'asking_price'))}")
    st.write(f"Woonoppervlak: {_format_number(_listing_value(listing, 'surface_m2', 'living_area'))} m²")
    perceel = _listing_value(listing, "plot_size_m2", "plot_size")
    st.write(f"Perceel: {_format_number(perceel) if perceel not in (None, '') else 'Onbekend'} m²")
    st.write(f"Slaapkamers: {_safe_score(_listing_value(listing, 'bedrooms')) if _safe_score(_listing_value(listing, 'bedrooms')) is not None else 'Onbekend'}")
    st.write(f"Energielabel: {_listing_value(listing, 'energy_label') or 'Onbekend'}")
    st.write(f"Bouwjaar: {_safe_score(_listing_value(listing, 'construction_year', 'bag_building_year')) if _safe_score(_listing_value(listing, 'construction_year', 'bag_building_year')) is not None else 'Onbekend'}")
    st.write(f"Days on market: {_safe_score(_listing_value(listing, 'days_on_market')) if _safe_score(_listing_value(listing, 'days_on_market')) is not None else 'Onbekend'}")
    st.write(f"Listing history: {_propertyhunter_listing_history_text(listing)}")
    st.write(f"Investment score: {_safe_score(candidate.get('investment_score')) if _safe_score(candidate.get('investment_score')) is not None else 'Onbekend'}")
    st.write(f"Opportunity score: {_safe_score(candidate.get('hidden_value_score')) if _safe_score(candidate.get('hidden_value_score')) is not None else 'Onbekend'}")
    st.write(f"Bron: {source.get('name') or _listing_value(listing, 'source_url') or 'Onbekend'}")

    if isinstance(_listing_value(listing, "source_url"), str) and _listing_value(listing, "source_url").strip():
        st.link_button("Open bron", str(_listing_value(listing, "source_url")))

    _render_compact_json_section("#### Candidate", candidate)
    _render_compact_json_section("#### Source", source)
    _render_compact_json_section("#### Listing", listing)
    _render_compact_json_section("#### Latest snapshot", latest_snapshot)
    _render_compact_json_section(f"#### Snapshots ({len(snapshots)})", snapshots[:3] if snapshots else [])


def _render_propertyhunter_interface_page():
    st.subheader("PropertyHunter Interface")

    dashboard_result = DASHBOARD_SERVICE.load_latest_completed_scan()
    rows = [row.to_dict() for row in dashboard_result.properties]

    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.metric("Laatste scan datum", _format_dashboard_timestamp(dashboard_result.scan_timestamp))
    with col2:
        st.metric("Bronnen", len(dashboard_result.source_names))
        if dashboard_result.source_names:
            st.caption(", ".join(dashboard_result.source_names))
    with col3:
        st.metric("Listings gevonden", dashboard_result.listings_found)
    with col4:
        st.metric("Nieuwe listings", dashboard_result.new_listings)

    col5, col6, col7 = st.columns(3)
    with col5:
        st.metric("Gewijzigde listings", dashboard_result.changed_listings)
    with col6:
        st.metric("Gemiddelde Investment Score", f"{dashboard_result.average_investment_score:.2f}")
    with col7:
        st.metric("Gemiddelde Opportunity Score", f"{dashboard_result.average_opportunity_score:.2f}")

    if not rows:
        st.info("Geen scanresultaten beschikbaar.")
        return

    municipality_options = _load_funda_place_options()
    places = [city for city in municipality_options if any(str(row.get("city") or "").strip() == city for row in rows)]
    asking_prices = [_safe_number(row.get("asking_price")) for row in rows if _safe_number(row.get("asking_price")) is not None]
    living_areas = [_safe_number(row.get("living_area")) for row in rows if _safe_number(row.get("living_area")) is not None]

    min_price_default = int(min(asking_prices)) if asking_prices else 0
    max_price_default = int(max(asking_prices)) if asking_prices else 0
    min_area_default = int(min(living_areas)) if living_areas else 0

    st.markdown("### Filters")
    filter_col_1, filter_col_2, filter_col_3 = st.columns(3)
    with filter_col_1:
        selected_place = st.selectbox("Plaats", ["Alle plaatsen", *places], key="dashboard_filter_place")
        min_price = st.number_input("Minimale vraagprijs", min_value=0, value=min_price_default, step=1000, key="dashboard_filter_min_price")
    with filter_col_2:
        max_price = st.number_input("Maximale vraagprijs", min_value=0, value=max_price_default if max_price_default >= min_price_default else min_price_default, step=1000, key="dashboard_filter_max_price")
        min_surface = st.number_input("Minimaal woonoppervlak", min_value=0, value=min_area_default, step=1, key="dashboard_filter_min_surface")
    with filter_col_3:
        min_opportunity_score = st.slider("Minimale opportunity score", min_value=0, max_value=100, value=0, key="dashboard_filter_min_opportunity")

    enriched_rows = [_enrich_market_discount_metrics(dict(row)) for row in rows]

    filtered_rows = [
        row
        for row in enriched_rows
        if (selected_place == "Alle plaatsen" or str(row.get("city") or "").strip() == selected_place)
        and (_safe_number(row.get("asking_price")) is not None and _safe_number(row.get("asking_price")) >= float(min_price))
        and (_safe_number(row.get("asking_price")) is not None and _safe_number(row.get("asking_price")) <= float(max_price))
        and (_safe_number(row.get("living_area")) is not None and _safe_number(row.get("living_area")) >= float(min_surface))
        and (_safe_score(row.get("opportunity_score")) is not None and _safe_score(row.get("opportunity_score")) >= int(min_opportunity_score))
    ]

    filtered_rows.sort(
        key=lambda item: (
            _safe_number(item.get("discount_percentage")) if _safe_number(item.get("discount_percentage")) is not None else -9999.0,
            int(_safe_score(item.get("opportunity_score")) or -1),
        ),
        reverse=True,
    )

    table_rows = []
    for row in filtered_rows:
        table_rows.append(
            {
                "listing_id": row.get("listing_id"),
                "adres": row.get("address") or "Onbekend",
                "plaats": row.get("city") or "Onbekend",
                "vraagprijs": _format_currency(row.get("asking_price")),
                "funda_woonoppervlak": _format_number(row.get("funda_living_area_m2")) if row.get("funda_living_area_m2") is not None else "Niet beschikbaar",
                "bag_oppervlak": _format_number(row.get("bag_official_floor_area_m2")) if row.get("bag_official_floor_area_m2") is not None else "Niet beschikbaar",
                "gebruikt_rekenoppervlak": _format_number(row.get("calculation_area_m2")) if row.get("calculation_area_m2") is not None else "Niet beschikbaar",
                "bron_rekenoppervlak": row.get("calculation_area_source") or "Niet beschikbaar",
                "asking_eur_m2": _format_currency(row.get("asking_price_per_m2")) if row.get("asking_price_per_m2") is not None else "Niet beschikbaar",
                "neighborhood_eur_m2": _format_currency(row.get("neighborhood_price_per_m2")) if row.get("neighborhood_price_per_m2") is not None else "Niet beschikbaar",
                "discount_pct": _format_percentage(_safe_number(row.get("discount_percentage")), decimals=2) if _safe_number(row.get("discount_percentage")) is not None else "Niet beschikbaar",
                "market_discount_score": row.get("market_discount_score") if row.get("market_discount_score") is not None else "Onbekend",
                "discount_highlight": "JA (>=25%)" if bool(row.get("is_market_discount_highlight")) else "Nee",
                "woz_per_m2": _format_currency(row.get("woz_value_per_m2")) if row.get("woz_value_per_m2") is not None else "Niet beschikbaar",
                "funda_bag_verschil_m2": _format_number(row.get("living_area_difference_m2")) if row.get("living_area_difference_m2") is not None else "Niet beschikbaar",
                "funda_bag_verschil_pct": _format_percentage(_safe_number(row.get("living_area_difference_percentage")), decimals=2) if _safe_number(row.get("living_area_difference_percentage")) is not None else "Niet beschikbaar",
                "perceel": _format_number(row.get("plot_size")),
                "slaapkamers": row.get("bedrooms") if row.get("bedrooms") is not None else "Onbekend",
                "energielabel": row.get("energy_label") or "Onbekend",
                "bouwjaar": row.get("construction_year") if row.get("construction_year") is not None else "Onbekend",
                "bag_gebruiksdoel": row.get("bag_usage_purpose") or "Niet beschikbaar",
                "bag_bouwjaar": row.get("bag_building_year") if row.get("bag_building_year") is not None else "Niet beschikbaar",
                "days_on_market": row.get("days_on_market") if row.get("days_on_market") is not None else "Onbekend",
                "investment_score": row.get("investment_score") if row.get("investment_score") is not None else "Onbekend",
                "opportunity_score": row.get("opportunity_score") if row.get("opportunity_score") is not None else "Onbekend",
                "source_name": _source_display_name(row.get("source_name"), row.get("source_url")),
                "source_url": row.get("source_url") or "",
            }
        )

    st.markdown("### Woningtabel")
    if not table_rows:
        st.info("Geen woningen gevonden met de huidige filters.")
        return

    st.caption("Overzicht (eerste 25 resultaten)")
    preview_rows = [
        {
            "adres": ("🔥 " if str(row.get("discount_highlight") or "").startswith("JA") else "") + (row.get("adres") or "Onbekend"),
            "plaats": row.get("plaats") or "Onbekend",
            "vraagprijs": row.get("vraagprijs") or "Onbekend",
            "asking_eur_m2": row.get("asking_eur_m2") or "Onbekend",
            "neighborhood_eur_m2": row.get("neighborhood_eur_m2") or "Onbekend",
            "discount_pct": row.get("discount_pct") or "Onbekend",
            "market_discount_score": row.get("market_discount_score") or "Onbekend",
            "bron": row.get("source_name") or "Onbekend",
            "highlight": row.get("discount_highlight") or "Nee",
        }
        for row in table_rows[:25]
    ]
    header_cols = st.columns(9)
    header_cols[0].markdown("**Adres**")
    header_cols[1].markdown("**Plaats**")
    header_cols[2].markdown("**Vraagprijs**")
    header_cols[3].markdown("**Asking €/m²**")
    header_cols[4].markdown("**Neighbourhood €/m²**")
    header_cols[5].markdown("**Discount %**")
    header_cols[6].markdown("**Market Discount Score**")
    header_cols[7].markdown("**Bron**")
    header_cols[8].markdown("**Highlight**")
    for preview_row in preview_rows:
        row_cols = st.columns(9)
        row_cols[0].write(preview_row.get("adres") or "Onbekend")
        row_cols[1].write(preview_row.get("plaats") or "Onbekend")
        row_cols[2].write(preview_row.get("vraagprijs") or "Onbekend")
        row_cols[3].write(preview_row.get("asking_eur_m2") or "Onbekend")
        row_cols[4].write(preview_row.get("neighborhood_eur_m2") or "Onbekend")
        row_cols[5].write(preview_row.get("discount_pct") or "Onbekend")
        row_cols[6].write(preview_row.get("market_discount_score") or "Onbekend")
        row_cols[7].write(preview_row.get("bron") or "Onbekend")
        row_cols[8].write(preview_row.get("highlight") or "Nee")

    row_options = [
        (
            f"{'🔥 ' if str(row.get('discount_highlight') or '').startswith('JA') else ''}{row.get('adres') or 'Onbekend'} | {row.get('plaats') or 'Onbekend'} | {row.get('vraagprijs') or 'Onbekend'} | {index + 1}",
            row,
        )
        for index, row in enumerate(table_rows)
    ]
    selected_row_label = st.selectbox(
        "Selecteer woning",
        options=[label for label, _ in row_options],
        key="ph_interface_selected_row",
    )
    selected_row = next((row for label, row in row_options if label == selected_row_label), None)

    if isinstance(selected_row, dict):
        with st.container(border=True):
            st.markdown("### Geselecteerde woning")
            st.write(f"{selected_row.get('adres')} · {selected_row.get('plaats')}")
            st.write(f"Vraagprijs: {selected_row.get('vraagprijs')}")
            st.write(f"Funda woonoppervlak: {selected_row.get('funda_woonoppervlak')} m²")
            st.write(f"BAG-oppervlak: {selected_row.get('bag_oppervlak')} m²")
            st.write(f"Gebruikt rekenoppervlak: {selected_row.get('gebruikt_rekenoppervlak')} m²")
            st.write(f"Bron rekenoppervlak: {selected_row.get('bron_rekenoppervlak')}")
            st.write(f"Asking €/m²: {selected_row.get('asking_eur_m2')}")
            st.write(f"Neighbourhood €/m²: {selected_row.get('neighborhood_eur_m2')}")
            st.write(f"Discount %: {selected_row.get('discount_pct')}")
            st.write(f"Market Discount Score: {selected_row.get('market_discount_score')}")
            st.write(f"Discount highlight: {selected_row.get('discount_highlight')}")
            st.write(f"WOZ per m²: {selected_row.get('woz_per_m2')}")
            st.write(f"Verschil Funda/BAG m²: {selected_row.get('funda_bag_verschil_m2')}")
            st.write(f"Verschil Funda/BAG %: {selected_row.get('funda_bag_verschil_pct')}")
            st.write(f"Perceel: {selected_row.get('perceel')} m²")
            st.write(f"Slaapkamers: {selected_row.get('slaapkamers')}")
            st.write(f"Energielabel: {selected_row.get('energielabel')}")
            st.write(f"Bouwjaar: {selected_row.get('bouwjaar')}")
            st.write(f"BAG gebruiksdoel: {selected_row.get('bag_gebruiksdoel')}")
            st.write(f"BAG bouwjaar: {selected_row.get('bag_bouwjaar')}")
            st.write(f"Days on market: {selected_row.get('days_on_market')}")
            st.write(f"Investment score: {selected_row.get('investment_score')}")
            st.write(f"Opportunity score: {selected_row.get('opportunity_score')}")
            st.write(f"Bron: {selected_row.get('source_name') or 'Onbekend'}")
            if str(selected_row.get("source_url") or "").strip():
                st.link_button("Open bron", str(selected_row.get("source_url")))



def _set_funda_scan_all_municipalities(available_places: list[str]):
    st.session_state["deal_funda_scan_cities"] = list(available_places)


def _clear_funda_scan_municipalities():
    st.session_state["deal_funda_scan_cities"] = []


def _deal_finder_marker(message: str):
    LOGGER.warning("[DEAL_FINDER_MARKER] %s", message)


def _format_asking_price(status: str, price, text: str | None = None) -> str:
    if status == "known":
        return _format_currency(price)
    if status == "on_request":
        return "Prijs op aanvraag"
    if status == "from_price":
        if price is not None:
            return f"Vanaf {_format_currency(price)}"
        return "Vanaf …"
    if status == "range":
        return text or "Prijsrange onbekend"
    if status == "auction":
        return f"Veiling{': ' + text if text else ''}"
    return "Onbekend"


def _label_score(key: str) -> str:
    labels = {
        "location": "Locatie",
        "price": "Prijs",
        "yield": "Rendement",
        "transformation": "Transformatie",
        "risk": "Risico",
    }
    if isinstance(key, str) and key in labels:
        return labels[key]
    if isinstance(key, str):
        return key.replace("_", " ").title()
    return "Onbekend"


def _to_transaction_list(items) -> list[PropertyTransaction]:
    if not isinstance(items, list):
        return []
    return [PropertyTransaction.from_dict(item) for item in items]


def _to_permit_list(items) -> list[PermitRecord]:
    if not isinstance(items, list):
        return []
    return [PermitRecord.from_dict(item) for item in items]


def _permit_status_label(status: str) -> str:
    labels = {
        "pending": "LOPEND",
        "rejected": "GEWEIGERD",
        "withdrawn": "INGETROKKEN",
        "granted": "VERLEEND",
    }
    return labels.get(status or "", (status or "unknown").upper())


def _render_list(items):
    if not items:
        st.write("- Geen gegevens")
        return
    if isinstance(items, list):
        for item in items:
            if isinstance(item, str):
                st.write(f"- {item}")
            else:
                st.write(f"- {item}")
    else:
        st.write(f"- {items}")


def _render_analysis_result(source_url: str, analysis: dict):
    if not isinstance(analysis, dict):
        analysis = {}

    extracted = analysis.get("extracted_data") or {}
    if not isinstance(extracted, dict):
        extracted = {}

    property_data = Property(
        source_url=source_url or extracted.get("source_url") or "",
        title=extracted.get("title") or "Onbekend object",
        address=extracted.get("address") or "Onbekend",
        city=extracted.get("city"),
        country=extracted.get("country"),
        asking_price=extracted.get("asking_price"),
        asking_price_status=extracted.get("asking_price_status") or "unknown",
        asking_price_text=extracted.get("asking_price_text"),
        listed_since=extracted.get("listed_since"),
        days_on_market=extracted.get("days_on_market"),
        listing_status=extracted.get("listing_status") or "unknown",
        original_asking_price=extracted.get("original_asking_price"),
        current_asking_price=extracted.get("current_asking_price"),
        price_reduction_count=extracted.get("price_reduction_count") or 0,
        last_price_reduction_date=extracted.get("last_price_reduction_date"),
        total_price_reduction_amount=extracted.get("total_price_reduction_amount"),
        total_price_reduction_percentage=extracted.get("total_price_reduction_percentage"),
        listing_history_source=extracted.get("listing_history_source"),
        listing_history_confidence=extracted.get("listing_history_confidence") or "unknown",
        surface_m2=extracted.get("surface_m2"),
        price_per_m2=extracted.get("price_per_m2"),
        annual_rent=extracted.get("annual_rent"),
        property_type=extracted.get("property_type"),
        current_use=extracted.get("current_use"),
        zoning=extracted.get("zoning"),
        energy_label=extracted.get("energy_label"),
        description=extracted.get("description"),
        raw_text=analysis.get("property_summary"),
        previous_transactions=_to_transaction_list(extracted.get("previous_transactions") or []),
        permits_last_10_years=_to_permit_list(extracted.get("permits_last_10_years") or []),
        active_permits=_to_permit_list(extracted.get("active_permits") or []),
    )

    title = property_data.title or "Onbekend object"

    st.markdown("---")
    st.header(title)

    if property_data.address:
        st.write(f"Adres: {property_data.address}")
    if property_data.source_url:
        st.link_button("Bronlink", property_data.source_url)

    col1, col2, col3 = st.columns(3)
    with col1:
        investment_score = analysis.get("investment_score", 0)
        if not isinstance(investment_score, (int, float)):
            investment_score = 0
        score_status = property_data.asking_price_status or "unknown"
        display_score = int(investment_score)
        if score_status != "known":
            display_score = max(0, display_score - 5)
        st.metric("Investment Score", f"{display_score}/100")
        if score_status != "known":
            st.warning("Score voorlopig: prijsanalyse is niet volledig beoordeelbaar.")
    with col2:
        st.metric("Vraagprijs", _format_asking_price(property_data.asking_price_status, property_data.asking_price, property_data.asking_price_text))
    with col3:
        price_per_m2 = property_data.price_per_m2
        if price_per_m2 is None:
            price_per_m2 = calculate_price_per_m2(property_data.asking_price, property_data.surface_m2, property_data.asking_price_status)
        st.metric("Prijs per m²", _format_number(price_per_m2) if price_per_m2 is not None else "Niet berekenbaar")

    with st.expander("Scoreverdeling"):
        score_breakdown = analysis.get("score_breakdown") or {}
        if not isinstance(score_breakdown, dict):
            score_breakdown = {}
        for key in ("location", "price", "yield", "transformation", "risk", "marketability", "negotiation_position", "permit_risk"):
            value = score_breakdown.get(key, 0)
            if not isinstance(value, (int, float)):
                value = 0
            st.progress(max(0, min(1, float(value) / 100)), text=f"{_label_score(key)}: {int(value)}/100")

    with st.expander("Verkoopgeschiedenis"):
        st.write(f"Te koop sinds: {property_data.listed_since or 'Onbekend'}")
        if property_data.days_on_market is None:
            property_data.days_on_market = calculate_days_on_market(property_data.listed_since)
        st.write(f"Aantal dagen te koop: {property_data.days_on_market if property_data.days_on_market is not None else 'Onbekend'}")
        st.write(f"Oorspronkelijke vraagprijs: { _format_currency(property_data.original_asking_price) if property_data.original_asking_price is not None else 'Onbekend' }")
        st.write(f"Huidige vraagprijs: { _format_currency(property_data.current_asking_price) if property_data.current_asking_price is not None else 'Onbekend' }")
        st.write(f"Aantal prijsverlagingen: {property_data.price_reduction_count}")
        st.write(f"Totale daling: { _format_currency(property_data.total_price_reduction_amount) if property_data.total_price_reduction_amount is not None else 'Onbekend' } / {property_data.total_price_reduction_percentage if property_data.total_price_reduction_percentage is not None else 'Onbekend'}%")
        st.write(f"Datum laatste prijsverlaging: {property_data.last_price_reduction_date or 'Onbekend'}")
        st.write(f"Bron listing history: {property_data.listing_history_source or 'Onbekend'}")
        st.write(f"Betrouwbaarheid listing history: {property_data.listing_history_confidence}")

        derived_reduction = calculate_price_reduction(property_data.original_asking_price, property_data.current_asking_price)
        if derived_reduction is not None:
            st.write(
                "Afgeleide daling (berekend): "
                f"{_format_currency(derived_reduction['amount'])} / {derived_reduction['percentage']}%"
            )

    with st.expander("Vorige transacties"):
        if property_data.previous_transactions:
            transaction_rows = [
                {
                    "date": transaction.transaction_date,
                    "type": transaction.transaction_type,
                    "price": _format_currency(transaction.transaction_price) if transaction.transaction_price is not None else "Onbekend",
                    "source": transaction.source or "Onbekend",
                    "confidence": transaction.confidence,
                }
                for transaction in property_data.previous_transactions
            ]
            _render_rows_with_columns(
                rows=transaction_rows,
                columns=[
                    ("Datum", "date"),
                    ("Type", "type"),
                    ("Prijs", "price"),
                    ("Bron", "source"),
                    ("Betrouwbaarheid", "confidence"),
                ],
                empty_message="Geen vorige transacties bekend.",
            )

            last_known_transaction = None
            for transaction in property_data.previous_transactions:
                if transaction.transaction_price not in (None, ""):
                    last_known_transaction = transaction
                    break

            if last_known_transaction is not None:
                current_price = property_data.current_asking_price if property_data.current_asking_price is not None else property_data.asking_price
                delta = calculate_price_change_since_last_transaction(current_price, last_known_transaction.transaction_price)
                if delta is not None:
                    st.write(
                        "Verschil t.o.v. vorige bekende transactie: "
                        f"{_format_currency(delta['amount'])} / {delta['percentage']}%"
                    )
        else:
            st.write("Geen vorige transacties bekend.")

    with st.expander("Vergunningen afgelopen tien jaar"):
        if property_data.permits_last_10_years:
            permit_rows = [
                {
                    "application_date": permit.application_date,
                    "type": permit.permit_type or "Onbekend",
                    "description": permit.description or "Onbekend",
                    "status": _permit_status_label(permit.status),
                    "decision_date": permit.decision_date,
                    "authority": permit.authority or "Onbekend",
                    "relevance": permit.investment_relevance or "Onbekend",
                    "source": permit.source or "Onbekend",
                    "source_url": permit.source_url or "",
                }
                for permit in property_data.permits_last_10_years
            ]
            _render_rows_with_columns(
                rows=permit_rows,
                columns=[
                    ("Aanvraagdatum", "application_date"),
                    ("Type", "type"),
                    ("Omschrijving", "description"),
                    ("Status", "status"),
                    ("Besluitdatum", "decision_date"),
                    ("Instantie", "authority"),
                    ("Relevantie", "relevance"),
                    ("Bron", "source"),
                    ("Bronlink", "source_url"),
                ],
                empty_message="Geen vergunningen bekend.",
            )
        else:
            st.write("Geen vergunningen bekend.")

        if property_data.active_permits:
            st.write(f"Actieve vergunningen: {len(property_data.active_permits)}")

    with st.expander("Samenvatting"):
        st.write(analysis.get("property_summary") or "Geen samenvatting beschikbaar.")

    with st.expander("Sterke punten"):
        _render_list(analysis.get("strengths"))

    with st.expander("Risico's"):
        _render_list(analysis.get("risks"))

    with st.expander("Ontbrekende informatie"):
        _render_list(analysis.get("missing_information"))

    with st.expander("Aannames"):
        _render_list(analysis.get("assumptions"))

    with st.expander("Advies"):
        st.write(analysis.get("recommendation") or "Geen advies beschikbaar.")

    with st.expander("Aanbevolen vervolgstappen"):
        _render_list(analysis.get("next_actions"))


def _compose_source_text(result: ScrapeResult) -> str:
    chunks: list[str] = []
    for value in (result.title, result.address, result.description, result.raw_text):
        if isinstance(value, str) and value.strip():
            chunks.append(value.strip())
    if result.features:
        chunks.extend([item.strip() for item in result.features if isinstance(item, str) and item.strip()])
    merged = "\n".join(chunks)
    return " ".join(merged.split())


def _has_sufficient_source_text(text: str, min_words: int = 40) -> bool:
    return isinstance(text, str) and len(text.split()) >= min_words


def _render_funda_failure_ui(result: ScrapeResult):
    st.error("Deze Funda-pagina kon niet volledig worden uitgelezen.")
    if result.warnings:
        for warning in result.warnings:
            st.warning(warning)

    fallback = result.fallback_recommendation or {}
    suggestion = fallback.get("broker_search_query")
    if suggestion:
        st.info(f"Voorgestelde zoekopdracht voor makelaarssite: {suggestion}")

    st.text_area(
        "Plak de advertentietekst",
        height=220,
        placeholder="Plak hier de volledige advertentietekst van de listing.",
        key="funda_fallback_text",
    )
    st.text_input(
        "Voer het adres handmatig in",
        placeholder="Bijv. Voorbeeldstraat 12, Amsterdam",
        key="funda_manual_address",
    )
    st.text_input(
        "Plak de URL van de verkopende makelaar",
        placeholder="https://www.makelaar.nl/object/...",
        key="funda_broker_url",
    )
    if st.button("Opnieuw proberen", key="retry_funda", type="secondary"):
        st.rerun()


def _persist_analysis_result(source_url: str, analysis: dict):
    if not isinstance(analysis, dict) or not DATABASE_SERVICE.is_enabled:
        return

    try:
        DATABASE_SERVICE.store_analyzed_property(source_url=source_url, analysis=analysis)
    except Exception as error:
        st.warning(f"Analyse kon niet naar Supabase worden opgeslagen: {error}")


def _safe_score(value) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _safe_number(value) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _deal_rating_from_score(score: int | None) -> str:
    if score is None:
        return "Onbekend"
    if score >= 85:
        return "A+"
    if score >= 70:
        return "A"
    if score >= 55:
        return "B"
    if score >= 40:
        return "C"
    return "D"


def _deal_summary_text_from_rating(rating: str) -> str:
    if rating in {"A+", "A"}:
        return "Sterke deal"
    if rating == "B":
        return "Nader onderzoeken"
    if rating in {"C", "D"}:
        return "Zwakke deal"
    return "Nader onderzoeken"


def _extract_gross_yield_value(*payloads: dict) -> float | None:
    keys = ("gross_yield", "yield", "cap_rate", "roi")
    candidates: list[dict] = []

    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        candidates.append(payload)

        metadata = payload.get("metadata")
        if isinstance(metadata, dict):
            candidates.append(metadata)

        raw_payload = payload.get("raw_payload")
        if isinstance(raw_payload, dict):
            candidates.append(raw_payload)
            raw_metadata = raw_payload.get("metadata")
            if isinstance(raw_metadata, dict):
                candidates.append(raw_metadata)

    for candidate in candidates:
        for key in keys:
            value = candidate.get(key)
            if isinstance(value, (int, float)):
                percentage = float(value)
                if 0 < percentage <= 1:
                    percentage *= 100.0
                return percentage
    return None


def _format_percentage(value: float | None, decimals: int = 1) -> str:
    if value is None:
        return "Onbekend"
    return f"{value:.{decimals}f}".replace(".", ",") + "%"


def _format_price_per_m2_currency(asking_price, surface_m2) -> str:
    price_value = _safe_number(asking_price)
    surface_value = _safe_number(surface_m2)
    if price_value is None or surface_value is None or surface_value <= 0:
        return "Onbekend"
    return f"{_format_currency(price_value / surface_value)} per m²"


def _estimated_market_value(asking_price) -> float | None:
    price_value = _safe_number(asking_price)
    if price_value is None or price_value <= 0:
        return None
    return price_value * 1.10


def _discount_vs_market_value_percentage(asking_price) -> float | None:
    price_value = _safe_number(asking_price)
    market_value = _estimated_market_value(asking_price)
    if price_value is None or market_value is None or market_value <= 0:
        return None
    return ((market_value - price_value) / market_value) * 100.0


def _maximum_purchase_price_placeholder(asking_price) -> float | None:
    price_value = _safe_number(asking_price)
    if price_value is None or price_value <= 0:
        return None
    return price_value * 0.92


def _difference_with_asking_price_placeholder(asking_price) -> float | None:
    price_value = _safe_number(asking_price)
    max_purchase = _maximum_purchase_price_placeholder(asking_price)
    if price_value is None or max_purchase is None:
        return None
    return price_value - max_purchase


def _difference_percentage_vs_asking(asking_price) -> float | None:
    price_value = _safe_number(asking_price)
    difference = _difference_with_asking_price_placeholder(asking_price)
    if price_value is None or price_value <= 0 or difference is None:
        return None
    return (difference / price_value) * 100.0


def _recommendation_from_difference_percentage(difference_pct: float | None) -> str:
    if difference_pct is None:
        return "Onbekend"
    if difference_pct <= 0:
        return "Green"
    if difference_pct <= 5:
        return "Orange"
    return "Red"


def _ai_investment_summary_from_rating(rating: str) -> str:
    if rating in {"A+", "A"}:
        return "Excellent investment opportunity. Attractive valuation with strong upside potential."
    if rating == "B":
        return "Interesting investment. Further due diligence is recommended."
    if rating == "C":
        return "Average investment opportunity. Returns depend on execution."
    if rating == "D":
        return "Weak investment opportunity. Current asking price appears unattractive compared to expected return."
    if rating == "F":
        return "Avoid. The investment fundamentals are currently too weak."
    return "Interesting investment. Further due diligence is recommended."


def _ai_strengths_text(score: int | None, gross_yield_value: float | None, recommendation: str) -> str:
    parts: list[str] = []
    if score is not None:
        parts.append(f"Investment score {score}/100")
    if gross_yield_value is not None:
        parts.append(f"gross yield {_format_percentage(gross_yield_value)}")
    if recommendation == "Green":
        parts.append("asking price ligt op of onder de placeholder aankoopgrens")
    return ", ".join(parts) if parts else "Beperkte sterke signalen beschikbaar in de huidige listingdata."


def _ai_weaknesses_text(difference_percentage: float | None, price_per_m2_text: str, discount_vs_market_value: float | None) -> str:
    parts: list[str] = []
    if difference_percentage is not None and difference_percentage > 0:
        parts.append(f"vraagprijs ligt {_format_percentage(difference_percentage)} boven de placeholder aankoopgrens")
    if price_per_m2_text != "Onbekend":
        parts.append(f"prijsniveau is {price_per_m2_text}")
    if discount_vs_market_value is not None:
        parts.append(f"placeholder discount is -{_format_percentage(discount_vs_market_value)}")
    return ", ".join(parts) if parts else "Onvoldoende waardedata beschikbaar om duidelijke zwaktes te onderbouwen."


def _ai_next_step_text(recommendation: str, gross_yield_value: float | None) -> str:
    if recommendation == "Green":
        return "Valideer direct markthuur, onderhoud en transactiereferenties om een biedingsstrategie te bepalen."
    if recommendation == "Orange":
        return "Controleer huurpotentie, renovatiekosten en vergelijkbare verkopen voordat je een bod overweegt."
    if recommendation == "Red":
        return "Heronderhandel de prijs of wacht op betere fundamentals voordat je verdergaat."
    if gross_yield_value is None:
        return "Verzamel eerst huurgegevens en marktvergelijkingen voor een betrouwbare onderbouwing."
    return "Werk de deal verder uit met aanvullende huur- en marktdata."


def _cap_score_0_20(value: float | int | None) -> int:
    if value is None:
        return 0
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0
    return int(max(0, min(20, round(number))))


def _investment_intelligence_rating(score: int) -> str:
    if score >= 85:
        return "A+"
    if score >= 75:
        return "A"
    if score >= 65:
        return "B"
    if score >= 50:
        return "C"
    return "D"


def _build_investment_intelligence(
    *,
    city: str | None,
    surface_m2,
    gross_yield_value: float | None,
    discount_vs_market_value: float | None,
    difference_percentage: float | None,
    recommendation: str,
) -> dict:
    city_known = isinstance(city, str) and bool(city.strip()) and city.strip().lower() != "onbekend"
    surface_known = _safe_number(surface_m2)

    location_score = 14
    if city_known:
        location_score += 3
    if surface_known is not None and surface_known >= 60:
        location_score += 2

    valuation_score = 8
    if discount_vs_market_value is not None:
        if discount_vs_market_value >= 12:
            valuation_score = 19
        elif discount_vs_market_value >= 9:
            valuation_score = 17
        elif discount_vs_market_value >= 6:
            valuation_score = 15
        elif discount_vs_market_value >= 3:
            valuation_score = 12
        elif discount_vs_market_value >= 0:
            valuation_score = 10
        else:
            valuation_score = 7

    rental_potential_score = _return_score_from_gross_yield(gross_yield_value)

    transformation_score = 9
    if surface_known is not None and surface_known >= 120:
        transformation_score = 16
    elif surface_known is not None and surface_known >= 90:
        transformation_score = 14
    elif surface_known is not None and surface_known >= 70:
        transformation_score = 12
    elif surface_known is None:
        transformation_score = 8

    market_momentum_score = 10
    if difference_percentage is not None:
        if difference_percentage <= 0:
            market_momentum_score = 18
        elif difference_percentage <= 2:
            market_momentum_score = 15
        elif difference_percentage <= 5:
            market_momentum_score = 12
        elif difference_percentage <= 8:
            market_momentum_score = 9
        else:
            market_momentum_score = 6

    risk_score = 11
    if recommendation == "Green":
        risk_score = 17
    elif recommendation == "Orange":
        risk_score = 12
    elif recommendation == "Red":
        risk_score = 7

    categories = [
        {
            "name": "Location",
            "score": _cap_score_0_20(location_score),
            "explanation": (
                "Locatie scoort op basis van beschikbare basisdata zoals stad en bruikbare oppervlakte. "
                f"Stad: {city.strip() if isinstance(city, str) and city.strip() else 'Onbekend'}, "
                f"oppervlakte: {_format_number(surface_m2)} m²."
            ),
        },
        {
            "name": "Valuation",
            "score": _cap_score_0_20(valuation_score),
            "explanation": (
                "Waardering is gebaseerd op het verschil met de placeholder marktwaarde. "
                f"Discount vs marktwaarde: {_format_percentage(discount_vs_market_value)}."
            ),
        },
        {
            "name": "Rental potential",
            "score": _cap_score_0_20(rental_potential_score),
            "explanation": (
                "Huurpotentie volgt uit de geschatte bruto aanvangsrendementen. "
                f"Gross yield: {_format_percentage(gross_yield_value)}."
            ),
        },
        {
            "name": "Transformation potential",
            "score": _cap_score_0_20(transformation_score),
            "explanation": (
                "Transformatiepotentieel gebruikt oppervlakte als praktische proxy voor flexibiliteit in indeling en herpositionering. "
                f"Beschikbare oppervlakte: {_format_number(surface_m2)} m²."
            ),
        },
        {
            "name": "Market momentum",
            "score": _cap_score_0_20(market_momentum_score),
            "explanation": (
                "Marktmomentum volgt uit de afstand tussen vraagprijs en placeholder koopgrens. "
                f"Verschil vs vraagprijs: {_format_percentage(difference_percentage)}."
            ),
        },
        {
            "name": "Risk",
            "score": _cap_score_0_20(risk_score),
            "explanation": (
                "Risicoscore combineert prijspositie en onderhandelingsadvies. "
                f"Huidig advies: {recommendation}."
            ),
        },
    ]

    total = sum(item["score"] for item in categories)
    overall_score = int(round((total / 120) * 100))
    overall_score = max(0, min(100, overall_score))
    rating = _investment_intelligence_rating(overall_score)

    return {
        "categories": categories,
        "overall_score": overall_score,
        "rating": rating,
    }


def _overall_investment_rating(score: int) -> str:
    if score >= 90:
        return "A+"
    if score >= 80:
        return "A"
    if score >= 70:
        return "B"
    if score >= 60:
        return "C"
    if score >= 50:
        return "D"
    return "F"


def _return_score_from_gross_yield(gross_yield_value: float | None) -> int:
    if gross_yield_value is None or gross_yield_value <= 0:
        return 0
    if gross_yield_value >= 10:
        return 20
    if gross_yield_value >= 8:
        return 17
    if gross_yield_value >= 6:
        return 14
    if gross_yield_value >= 4.5:
        return 10
    return 5


def _parse_datetime(value) -> datetime:
    if not isinstance(value, str) or not value.strip():
        return datetime.min
    normalized = value.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return datetime.min


def _filter_and_sort_properties(
    properties: list[dict],
    *,
    city_filter: str,
    min_investment_score: int,
    max_asking_price: float | None,
    search_query: str,
    sort_option: str,
) -> list[dict]:
    filtered: list[dict] = []
    needle = (search_query or "").strip().lower()

    for item in properties:
        city_value = (item.get("city") or "").strip()
        score = _safe_score(item.get("investment_score"))
        asking_price = _safe_number(item.get("asking_price"))
        title = str(item.get("title") or "")
        address = str(item.get("address") or "")

        if city_filter != "Alle steden" and city_value != city_filter:
            continue
        if score is None or score < min_investment_score:
            continue
        if max_asking_price is not None and asking_price is not None and asking_price > max_asking_price:
            continue
        if needle and needle not in title.lower() and needle not in address.lower():
            continue
        filtered.append(item)

    if sort_option == "Hoogste score":
        filtered.sort(key=lambda item: _safe_score(item.get("investment_score")) or -1, reverse=True)
    elif sort_option == "Nieuwste eerst":
        filtered.sort(key=lambda item: _parse_datetime(item.get("created_at")), reverse=True)
    elif sort_option == "Laagste vraagprijs":
        filtered.sort(key=lambda item: _safe_number(item.get("asking_price")) if _safe_number(item.get("asking_price")) is not None else float("inf"))
    elif sort_option == "Hoogste vraagprijs":
        filtered.sort(key=lambda item: _safe_number(item.get("asking_price")) or -1, reverse=True)

    return filtered


def _build_selected_analysis(property_data: dict, analysis_data: dict) -> dict:
    extracted = {
        "source_url": property_data.get("source_url"),
        "title": property_data.get("title"),
        "address": property_data.get("address"),
        "city": property_data.get("city"),
        "country": property_data.get("country"),
        "asking_price": property_data.get("asking_price"),
        "asking_price_status": property_data.get("asking_price_status") or "unknown",
        "asking_price_text": property_data.get("asking_price_text"),
        "listed_since": property_data.get("listed_since"),
        "days_on_market": property_data.get("days_on_market"),
        "listing_status": property_data.get("listing_status") or "unknown",
        "original_asking_price": property_data.get("original_asking_price"),
        "current_asking_price": property_data.get("current_asking_price"),
        "price_reduction_count": property_data.get("price_reduction_count") or 0,
        "last_price_reduction_date": property_data.get("last_price_reduction_date"),
        "total_price_reduction_amount": property_data.get("total_price_reduction_amount"),
        "total_price_reduction_percentage": property_data.get("total_price_reduction_percentage"),
        "listing_history_source": property_data.get("listing_history_source"),
        "listing_history_confidence": property_data.get("listing_history_confidence") or "unknown",
        "surface_m2": property_data.get("surface_m2"),
        "price_per_m2": property_data.get("price_per_m2"),
        "annual_rent": property_data.get("annual_rent"),
        "property_type": property_data.get("property_type"),
        "current_use": property_data.get("current_use"),
        "zoning": property_data.get("zoning"),
        "energy_label": property_data.get("energy_label"),
        "description": property_data.get("description"),
        "previous_transactions": [],
        "permits_last_10_years": [],
        "active_permits": [],
    }

    return {
        "property_summary": analysis_data.get("property_summary"),
        "extracted_data": extracted,
        "investment_score": analysis_data.get("investment_score") or 0,
        "score_breakdown": analysis_data.get("score_breakdown") or {},
        "analysis_confidence_score": analysis_data.get("analysis_confidence_score") or 0,
        "data_quality_warnings": analysis_data.get("data_quality_warnings") or [],
        "strengths": analysis_data.get("strengths") or [],
        "risks": analysis_data.get("risks") or [],
        "missing_information": analysis_data.get("missing_information") or [],
        "assumptions": analysis_data.get("assumptions") or [],
        "recommendation": analysis_data.get("recommendation"),
        "next_actions": analysis_data.get("next_actions") or [],
    }


def _render_new_analysis_page():
    st.caption("Analyseer vastgoedobjecten met een URL of handmatig geplakte advertentietekst.")

    tab_url, tab_text = st.tabs(["Analyse via URL", "Tekst handmatig invoeren"])

    with tab_url:
        url = st.text_input("Vastgoed-URL", placeholder="https://www.example.com/tekoop/object", key="url_input")
        if st.button("Object ophalen en analyseren", type="primary", key="analyze_url"):
            if not url.strip():
                st.error("Voer een geldige URL in.")
            else:
                with st.spinner("De pagina wordt opgehaald en geanalyseerd..."):
                    try:
                        scrape_result = scrape_url(url.strip())
                    except ValueError as error:
                        st.error(str(error))
                    except requests.RequestException as error:
                        st.error(f"De website kon niet worden opgehaald: {error}")
                    except RuntimeError as error:
                        st.error(str(error))
                    except Exception as error:
                        st.error(f"Er ging iets mis tijdens de analyse: {error}")
                    else:
                        if not scrape_result.success and scrape_result.source_name in {"funda", "funda_business"}:
                            _render_funda_failure_ui(scrape_result)
                        else:
                            property_text = _compose_source_text(scrape_result)
                            if not _has_sufficient_source_text(property_text):
                                st.warning(
                                    "Er is onvoldoende brontekst beschikbaar voor een betrouwbare AI-analyse. "
                                    "Plak de advertentietekst handmatig in het teksttabblad."
                                )
                            else:
                                analysis = analyze_property(property_text)
                                _persist_analysis_result(url.strip(), analysis)
                                _render_analysis_result(url.strip(), analysis)

    with tab_text:
        manual_text = st.text_area("Vastgoedadvertentie", height=280, placeholder="Plak hier de volledige advertentietekst van het object.", key="manual_input")
        if st.button("Tekst analyseren", type="primary", key="analyze_text"):
            if not manual_text.strip():
                st.error("Plak eerst een advertentietekst.")
            else:
                with st.spinner("De tekst wordt geanalyseerd..."):
                    try:
                        analysis = analyze_property(manual_text)
                    except ValueError as error:
                        st.error(str(error))
                    except RuntimeError as error:
                        st.error(str(error))
                    except Exception as error:
                        st.error(f"Er ging iets mis tijdens de analyse: {error}")
                    else:
                        _persist_analysis_result("", analysis)
                        _render_analysis_result("", analysis)


def _render_my_analyses_page():
    st.subheader("Mijn analyses")

    if not DATABASE_SERVICE.is_enabled:
        st.info("Supabase is nog niet geconfigureerd. Vul SUPABASE_URL en SUPABASE_SERVICE_ROLE_KEY in .env in.")
        return

    all_properties = DATABASE_SERVICE.list_properties(limit=500)
    if not all_properties:
        st.info("Er zijn nog geen opgeslagen analyses.")
        return

    city_options = sorted({(item.get("city") or "").strip() for item in all_properties if isinstance(item.get("city"), str) and item.get("city").strip()})
    city_filter = st.selectbox("Stad", ["Alle steden", *city_options], index=0)

    min_score = st.slider("Minimum investment score", min_value=0, max_value=100, value=0, step=1)
    max_price_input = st.number_input("Maximum vraagprijs (0 = geen limiet)", min_value=0.0, value=0.0, step=10000.0)
    max_price = max_price_input if max_price_input > 0 else None
    search_query = st.text_input("Zoek op titel of adres")
    sort_option = st.selectbox("Sortering", ["Hoogste score", "Nieuwste eerst", "Laagste vraagprijs", "Hoogste vraagprijs"], index=0)

    rows = _filter_and_sort_properties(
        all_properties,
        city_filter=city_filter,
        min_investment_score=min_score,
        max_asking_price=max_price,
        search_query=search_query,
        sort_option=sort_option,
    )

    if not rows:
        st.info("Geen analyses gevonden voor de gekozen filters.")
        return

    analysis_rows = [
        {
            "investment_score": _safe_score(item.get("investment_score")),
            "title": item.get("title") or "Onbekend",
            "address": item.get("address") or "Onbekend",
            "city": item.get("city") or "Onbekend",
            "asking_price": _format_currency(item.get("asking_price")),
            "price_per_m2": _format_number(item.get("price_per_m2")),
            "created_at": item.get("created_at") or "Onbekend",
            "source_url": item.get("source_url") or "",
        }
        for item in rows
    ]
    _render_rows_with_columns(
        rows=analysis_rows,
        columns=[
            ("Investment score", "investment_score"),
            ("Titel", "title"),
            ("Adres", "address"),
            ("Stad", "city"),
            ("Vraagprijs", "asking_price"),
            ("Prijs per m²", "price_per_m2"),
            ("Aangemaakt", "created_at"),
            ("Bron", "source_url"),
        ],
        empty_message="Geen analyses gevonden voor de gekozen filters.",
    )

    selected = st.selectbox(
        "Selecteer een property",
        rows,
        format_func=lambda item: f"{item.get('title') or 'Onbekend'} | {item.get('address') or 'Onbekend'} | score {item.get('investment_score') if item.get('investment_score') is not None else 'n.v.t.'}",
    )
    selected_id = selected.get("id") if isinstance(selected, dict) else None
    if not selected_id:
        return

    detail = DATABASE_SERVICE.get_property_with_latest_analysis(str(selected_id))
    property_data = detail.get("property") or {}
    analysis_data = detail.get("analysis") or {}

    if not property_data:
        st.warning("Geen details gevonden voor dit object.")
        return

    if analysis_data:
        combined_analysis = _build_selected_analysis(property_data, analysis_data)
        _render_analysis_result(property_data.get("source_url") or "", combined_analysis)
    else:
        st.subheader(property_data.get("title") or "Onbekend object")
        st.write(f"Adres: {property_data.get('address') or 'Onbekend'}")
        st.write(f"Stad: {property_data.get('city') or 'Onbekend'}")
        st.write(f"Vraagprijs: {_format_currency(property_data.get('asking_price'))}")
        if property_data.get("source_url"):
            st.link_button("Bronlink", property_data.get("source_url"))
        st.info("Nog geen analysegegevens beschikbaar voor dit object.")


def _render_dashboard_page():
    st.subheader("Dashboard")

    stats = DATABASE_SERVICE.get_dashboard_statistics()
    total_properties = stats.get("total_properties", 0)
    total_analyses = stats.get("total_analyses", 0)
    average_score = stats.get("average_investment_score", 0)
    highest_score = stats.get("highest_investment_score", 0)

    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.metric("Totaal opgeslagen properties", total_properties)
    with col2:
        st.metric("Totaal analyses", total_analyses)
    with col3:
        st.metric("Gemiddelde investment score", average_score)
    with col4:
        st.metric("Hoogste investment score", highest_score)

    city_counts = stats.get("properties_by_city") or {}
    st.markdown("### Properties per stad")
    if city_counts:
        _render_rows_with_columns(
            rows=[{"city": city, "count": count} for city, count in sorted(city_counts.items(), key=lambda item: item[1], reverse=True)],
            columns=[("Stad", "city"), ("Aantal", "count")],
            empty_message="Nog geen stadsgegevens beschikbaar.",
        )
    else:
        st.info("Nog geen stadsgegevens beschikbaar.")

    st.markdown("### Top 5 hoogste scores")
    top_properties = stats.get("top_properties") or []
    if top_properties:
        _render_rows_with_columns(
            rows=[
                {
                    "score": _safe_score(item.get("investment_score")) if _safe_score(item.get("investment_score")) is not None else "Onbekend",
                    "title": item.get("title") or "Onbekend",
                    "address": item.get("address") or "Onbekend",
                    "city": item.get("city") or "Onbekend",
                    "asking_price": _format_currency(item.get("asking_price")),
                }
                for item in top_properties
            ],
            columns=[
                ("Score", "score"),
                ("Titel", "title"),
                ("Adres", "address"),
                ("Stad", "city"),
                ("Vraagprijs", "asking_price"),
            ],
            empty_message="Nog geen scoregegevens beschikbaar.",
        )
    else:
        st.info("Nog geen scoregegevens beschikbaar.")

    st.markdown("### 5 meest recent geanalyseerde properties")
    recent_properties = stats.get("recent_properties") or []
    if recent_properties:
        _render_rows_with_columns(
            rows=[
                {
                    "created_at": item.get("created_at") or "Onbekend",
                    "score": _safe_score(item.get("investment_score")) if _safe_score(item.get("investment_score")) is not None else "Onbekend",
                    "title": item.get("title") or "Onbekend",
                    "address": item.get("address") or "Onbekend",
                    "city": item.get("city") or "Onbekend",
                }
                for item in recent_properties
            ],
            columns=[
                ("Datum", "created_at"),
                ("Score", "score"),
                ("Titel", "title"),
                ("Adres", "address"),
                ("Stad", "city"),
            ],
            empty_message="Nog geen recente analyses beschikbaar.",
        )
    else:
        st.info("Nog geen recente analyses beschikbaar.")


def _render_deal_finder_page():
    _deal_finder_marker("page_start")
    st.subheader("Deal Finder")

    if not DATABASE_SERVICE.is_enabled:
        st.info("Supabase is niet geconfigureerd. Stel SUPABASE_URL en SUPABASE_SERVICE_ROLE_KEY in om Deal Finder te gebruiken.")
        return

    st.caption("Foundationversie: ingestie, deduplicatie, snapshots en rules-based ranking zonder live scraping bypasses.")

    col_seed, col_refresh = st.columns([1, 1])
    with col_seed:
        if st.button("Seed brondefinities", key="seed_sources", type="secondary"):
            _deal_finder_marker("seed_sources_start")
            seeded = DEAL_FINDER_ORCHESTRATOR.seed_default_sources()
            st.success(f"{len(seeded)} brondefinities upserted.")
            _deal_finder_marker("seed_sources_done")
    with col_refresh:
        if st.button("Vernieuwen", key="refresh_deal_finder", type="secondary"):
            st.rerun()

    health = DATABASE_SERVICE.get_source_health()
    _deal_finder_marker("source_health_loaded")
    source_rows = health.get("sources") or []
    latest_runs = health.get("latest_scan_runs") or []

    st.markdown("### Source status")
    _deal_finder_marker("source_status_render_start")
    if source_rows:
        source_table_rows = [
            {
                "name": row.get("name") or "Onbekend",
                "type": row.get("source_type") or "unknown",
                "enabled": bool(row.get("is_enabled")),
                "latest_status": row.get("latest_scan_status") or "n.v.t.",
                "last_success": row.get("last_successful_scan_at") or "n.v.t.",
                "latest_error": row.get("latest_scan_error") or row.get("last_error") or "",
            }
            for row in source_rows
        ]
        _render_rows_with_columns(
            rows=source_table_rows,
            columns=[
                ("Bron", "name"),
                ("Type", "type"),
                ("Enabled", "enabled"),
                ("Laatste status", "latest_status"),
                ("Laatste succesvolle scan", "last_success"),
                ("Laatste fout", "latest_error"),
            ],
            empty_message="Nog geen bronnen beschikbaar. Gebruik 'Seed brondefinities'.",
        )
    else:
        st.info("Nog geen bronnen beschikbaar. Gebruik 'Seed brondefinities'.")
    _deal_finder_marker("source_status_render_done")

    st.markdown("### Latest scan runs")
    _deal_finder_marker("latest_scan_runs_render_start")
    if latest_runs:
        latest_run_rows = [
            {
                "source_id": run.get("source_id") or "",
                "status": run.get("status") or "",
                "started_at": run.get("started_at") or "",
                "completed_at": run.get("completed_at") or "",
                "found": run.get("items_found") or 0,
                "new": run.get("items_new") or 0,
                "changed": run.get("items_changed") or 0,
                "error": run.get("error_message") or "",
            }
            for run in latest_runs[:20]
        ]
        _render_rows_with_columns(
            rows=latest_run_rows,
            columns=[
                ("Bron ID", "source_id"),
                ("Status", "status"),
                ("Gestart", "started_at"),
                ("Voltooid", "completed_at"),
                ("Found", "found"),
                ("Nieuw", "new"),
                ("Gewijzigd", "changed"),
                ("Fout", "error"),
            ],
            empty_message="Nog geen scan runs.",
        )
    else:
        st.info("Nog geen scan runs.")
    _deal_finder_marker("latest_scan_runs_render_done")

    st.markdown("### Handmatige import")
    _deal_finder_marker("manual_import_section_start")
    csv_file = st.file_uploader("Upload CSV", type=["csv"], key="deal_csv_upload")
    if csv_file is not None and st.button("Importeer CSV", key="import_csv"):
        csv_text = csv_file.getvalue().decode("utf-8", errors="ignore")
        result = DEAL_FINDER_ORCHESTRATOR.import_csv(csv_text)
        st.success(f"CSV verwerkt: found={result['found']} new={result['new']} changed={result['changed']}")
        if result.get("warnings"):
            for warning in result["warnings"]:
                st.warning(warning)

    json_file = st.file_uploader("Upload JSON", type=["json"], key="deal_json_upload")
    if json_file is not None and st.button("Importeer JSON", key="import_json"):
        json_text = json_file.getvalue().decode("utf-8", errors="ignore")
        result = DEAL_FINDER_ORCHESTRATOR.import_json(json_text)
        st.success(f"JSON verwerkt: found={result['found']} new={result['new']} changed={result['changed']}")
        if result.get("warnings"):
            for warning in result["warnings"]:
                st.warning(warning)

    urls_text = st.text_area("Plak listing-URL's (1 per regel)", key="deal_urls_input", height=120)
    if st.button("Importeer URL's", key="import_urls"):
        _deal_finder_marker("url_import_start")
        outcome = _run_url_import(urls_text)
        if not outcome["ok"]:
            st.error(f"URL import mislukt: {outcome['error']}")
            st.session_state["deal_last_url_import"] = {
                "status": "error",
                "error": outcome["error"],
            }
        else:
            result = outcome["result"] or {}
            st.success(f"URL import verwerkt: found={result['found']} new={result['new']} changed={result['changed']}")
            if result.get("warnings"):
                for warning in result["warnings"]:
                    st.warning(warning)
            enrichment_items = result.get("enrichment") or []
            failed_enrichment = [item for item in enrichment_items if not item.get("success")]
            if failed_enrichment:
                st.warning(
                    "URL-records zijn opgeslagen, maar metadata enrichment mislukte voor "
                    f"{len(failed_enrichment)} listing(s)."
                )
            st.session_state["deal_last_url_import"] = {
                "status": "ok",
                "found": result.get("found") or 0,
                "new": result.get("new") or 0,
                "changed": result.get("changed") or 0,
                "warnings": result.get("warnings") or [],
                "listing_ids": result.get("listing_ids") or [],
                "enrichment": enrichment_items,
            }
        _deal_finder_marker("url_import_done")

    last_url_import = st.session_state.get("deal_last_url_import")
    if isinstance(last_url_import, dict):
        st.markdown("#### Laatste URL import resultaat")
        if last_url_import.get("status") == "error":
            st.error(f"Status: error | {last_url_import.get('error') or 'Onbekende fout'}")
        else:
            url_result_rows = [
                {
                    "found": last_url_import.get("found") or 0,
                    "new": last_url_import.get("new") or 0,
                    "changed": last_url_import.get("changed") or 0,
                    "listing_ids": ", ".join(last_url_import.get("listing_ids") or []),
                }
            ]
            _render_rows_with_columns(
                rows=url_result_rows,
                columns=[
                    ("Found", "found"),
                    ("New", "new"),
                    ("Changed", "changed"),
                    ("Listing IDs", "listing_ids"),
                ],
                empty_message="Geen URL importresultaat beschikbaar.",
            )
            warnings = last_url_import.get("warnings") or []
            for warning in warnings:
                st.warning(warning)
            enrichment_items = last_url_import.get("enrichment") or []
            if enrichment_items:
                enrichment_rows = [
                    {
                        "source_url": item.get("source_url") or "",
                        "success": bool(item.get("success")),
                        "method": item.get("extraction_method") or "none",
                        "confidence": item.get("confidence") if item.get("confidence") is not None else 0,
                        "warnings": "; ".join(item.get("warnings") or []),
                    }
                    for item in enrichment_items
                ]
                _render_rows_with_columns(
                    rows=enrichment_rows,
                    columns=[
                        ("Source URL", "source_url"),
                        ("Success", "success"),
                        ("Methode", "method"),
                        ("Confidence", "confidence"),
                        ("Warnings", "warnings"),
                    ],
                    empty_message="Geen enrichmentresultaten.",
                )
    _deal_finder_marker("manual_import_section_done")

    st.markdown("### Funda scan")
    st.caption("Start een gemeentegerichte Funda-scan via de bestaande orchestratieflow.")

    if "deal_funda_scan_running" not in st.session_state:
        st.session_state["deal_funda_scan_running"] = False

    available_places = _load_funda_place_options()
    if "deal_funda_scan_cities" not in st.session_state:
        st.session_state["deal_funda_scan_cities"] = [city for city in FUNDA_DEFAULT_SCAN_CITIES if city in available_places]
    if "deal_funda_scan_custom_city" not in st.session_state:
        st.session_state["deal_funda_scan_custom_city"] = ""

    scan_col_1, scan_col_2 = st.columns(2)
    with scan_col_1:
        selected_cities = st.multiselect(
            "Gemeenten (zoekbaar)",
            options=available_places,
            key="deal_funda_scan_cities",
            help="Typ om een Nederlandse gemeente te zoeken en selecteer er meerdere.",
        )
        custom_city = st.text_input(
            "Custom plaats",
            key="deal_funda_scan_custom_city",
            help="Voeg handmatig een plaatsnaam toe als die niet in de lijst staat.",
            placeholder="Bijvoorbeeld: Den Haag",
        )

        selection_col_1, selection_col_2 = st.columns(2)
        with selection_col_1:
            st.button(
                "Selecteer alle gemeenten",
                key="deal_funda_scan_select_all",
                on_click=_set_funda_scan_all_municipalities,
                args=(available_places,),
            )
        with selection_col_2:
            st.button(
                "Wis selectie",
                key="deal_funda_scan_clear_selection",
                on_click=_clear_funda_scan_municipalities,
            )

        scan_cities = _merge_scan_cities([str(city) for city in selected_cities], str(custom_city or ""))
        st.caption(_selected_municipality_summary(scan_cities))
        st.caption(f"Beschikbare plaatsen: {len(available_places)}")
        if len(scan_cities) > 25:
            st.warning("Een landelijke of zeer brede scan kan lang duren en veel scraperverbruik veroorzaken.")

        min_price_scan = st.number_input("Minimale vraagprijs", min_value=0, value=0, step=1000, key="deal_funda_scan_min_price")
        max_price_scan = st.number_input("Maximale vraagprijs", min_value=0, value=0, step=1000, key="deal_funda_scan_max_price")
    with scan_col_2:
        min_living_area_scan = st.number_input("Minimaal woonoppervlak (m²)", min_value=0, value=0, step=1, key="deal_funda_scan_min_living_area")
        max_pages_per_city = st.number_input("Max pagina's per stad", min_value=1, value=1, step=1, key="deal_funda_scan_max_pages")
        dry_run = st.checkbox("Dry-run", value=True, key="deal_funda_scan_dry_run")

    if min_price_scan > 0 and max_price_scan > 0 and min_price_scan > max_price_scan:
        st.error("Minimale vraagprijs mag niet hoger zijn dan maximale vraagprijs.")
    else:
        run_scan_clicked = st.button(
            "Start Funda-scan",
            key="deal_funda_scan_start",
            type="primary",
            disabled=bool(st.session_state.get("deal_funda_scan_running")),
        )

        if run_scan_clicked and st.session_state.get("deal_funda_scan_running"):
            st.info("Er draait al een scan. Wacht tot deze klaar is.")

        if run_scan_clicked and not st.session_state.get("deal_funda_scan_running"):
            st.session_state["deal_funda_scan_running"] = True
            try:
                with st.spinner("Funda-scan wordt uitgevoerd..."):
                    scan_result = _run_funda_scan_from_ui(
                        cities=scan_cities,
                        min_price=int(min_price_scan),
                        max_price=int(max_price_scan),
                        min_living_area=int(min_living_area_scan),
                        max_pages_per_city=int(max_pages_per_city),
                        dry_run=bool(dry_run),
                    )
                st.session_state["deal_funda_scan_result"] = scan_result
            except Exception as error:
                st.session_state.pop("deal_funda_scan_result", None)
                st.error(f"Funda-scan mislukt: {type(error).__name__}: {error}")
            finally:
                st.session_state["deal_funda_scan_running"] = False

    latest_scan_result = st.session_state.get("deal_funda_scan_result")
    st.info(_scan_data_origin_label(latest_scan_result=latest_scan_result if isinstance(latest_scan_result, dict) else None, database_enabled=DATABASE_SERVICE.is_enabled))
    if isinstance(latest_scan_result, dict):
        st.markdown("#### Laatste Funda-scanresultaat")
        st.write(f"Listings found: {int(latest_scan_result.get('listings_found') or 0)}")
        st.write(f"Listings imported: {int(latest_scan_result.get('listings_imported') or 0)}")
        st.write(f"New listings: {int(latest_scan_result.get('new_listings') or 0)}")
        st.write(f"Changed listings: {int(latest_scan_result.get('changed_listings') or 0)}")
        st.write(f"Unchanged listings: {int(latest_scan_result.get('unchanged_listings') or 0)}")
        st.write(f"Price reductions: {int(latest_scan_result.get('price_reductions') or 0)}")
        st.write(f"Failed listings: {int(latest_scan_result.get('failed_listings') or 0)}")
        st.write(f"Failed cities: {int(latest_scan_result.get('failed_cities') or 0)}")
        st.write(f"Duration: {_format_seconds(latest_scan_result.get('duration_seconds'))} s")
        st.write(f"JSON output: {latest_scan_result.get('output_path') or 'Onbekend'}")

        per_city_results = latest_scan_result.get("per_city_results") if isinstance(latest_scan_result.get("per_city_results"), list) else []
        if per_city_results:
            st.markdown("#### Resultaat per stad")
            city_rows = []
            for item in per_city_results:
                result_payload = item.get("result") if isinstance(item.get("result"), dict) else {}
                city_ok = bool(item.get("ok"))
                city_rows.append(
                    {
                        "city": item.get("city") or "Onbekend",
                        "status": "ok" if city_ok else "failed",
                        "http_status": item.get("http_status") if item.get("http_status") is not None else "",
                        "http_status_error": item.get("http_status_error") or "",
                        "listings_found": int(result_payload.get("listings_found") or 0),
                        "rows_found": int(item.get("rows_found") or 0),
                        "start_url": item.get("start_url") or "",
                        "error": item.get("error") or "",
                    }
                )
                if not city_ok and item.get("error"):
                    st.error(f"{item.get('city')}: {item.get('error')}")

            _render_rows_with_columns(
                rows=city_rows,
                columns=[
                    ("City", "city"),
                    ("Status", "status"),
                    ("HTTP status", "http_status"),
                    ("HTTP status error", "http_status_error"),
                    ("Listings found", "listings_found"),
                    ("Rows found", "rows_found"),
                    ("Start URL", "start_url"),
                    ("Error", "error"),
                ],
                empty_message="Geen per-stad resultaten beschikbaar.",
            )

        rows_by_city = latest_scan_result.get("rows_by_city") if isinstance(latest_scan_result.get("rows_by_city"), dict) else {}
        if rows_by_city:
            st.markdown("#### Listing preview per stad")
            for city_name in sorted(rows_by_city.keys(), key=lambda value: value.casefold()):
                city_rows = rows_by_city.get(city_name) or []
                if not city_rows:
                    continue
                st.markdown(f"##### {city_name}")
                _render_rows_with_columns(
                    rows=[
                        {
                            "address": row.get("adres") or "Onbekend",
                            "asking_price": _format_currency(row.get("vraagprijs")),
                            "living_area": _format_number(row.get("woonoppervlak")),
                            "opportunity_score": _safe_score(row.get("opportunity_score")) if _safe_score(row.get("opportunity_score")) is not None else "Onbekend",
                        }
                        for row in city_rows[:10]
                    ],
                    columns=[
                        ("Adres", "address"),
                        ("Vraagprijs", "asking_price"),
                        ("Woonoppervlak", "living_area"),
                        ("Opportunity score", "opportunity_score"),
                    ],
                    empty_message="Geen listings beschikbaar voor deze stad.",
                )

        top_rows = latest_scan_result.get("top_rows") if isinstance(latest_scan_result.get("top_rows"), list) else []
        st.markdown("#### Gecombineerde ranking over alle geselecteerde steden")
        if not top_rows:
            st.info("Geen resultaten beschikbaar voor de geselecteerde scaninstellingen.")
        else:
            presentation_rows = [
                {
                    "address": row.get("adres") or "Onbekend",
                    "city": row.get("plaats") or "Onbekend",
                    "asking_price": _format_currency(row.get("vraagprijs")),
                    "living_area": _format_number(row.get("woonoppervlak")),
                    "price_per_m2": _format_currency(row.get("price_per_m2")) if row.get("price_per_m2") is not None else "Onbekend",
                    "investment_score": _safe_score(row.get("investment_score")) if _safe_score(row.get("investment_score")) is not None else "Onbekend",
                    "opportunity_score": _safe_score(row.get("opportunity_score")) if _safe_score(row.get("opportunity_score")) is not None else "Onbekend",
                }
                for row in top_rows
            ]
            _render_opportunity_top_rows(presentation_rows)

    st.markdown("### New deal candidates")
    _deal_finder_marker("deal_candidates_section_start")
    source_options = {"Alle bronnen": None}
    for source in source_rows:
        if source.get("id"):
            source_options[str(source.get("name") or source.get("id"))] = str(source.get("id"))

    city_values = sorted({(item.get("city") or "").strip() for item in DATABASE_SERVICE.list_raw_listings(limit=5000) if isinstance(item.get("city"), str) and item.get("city").strip()})

    col_a, col_b, col_c, col_d = st.columns(4)
    with col_a:
        city_filter = st.selectbox("Stad", ["Alle steden", *city_values], key="deal_city_filter")
    with col_b:
        source_label = st.selectbox("Bron", list(source_options.keys()), key="deal_source_filter")
    with col_c:
        min_score = st.slider("Minimum score", min_value=0, max_value=100, value=0, key="deal_min_score")
    with col_d:
        priority_filter = st.selectbox("Priority", ["alle", "low", "medium", "high", "urgent"], key="deal_priority_filter")

    sort_choice = st.selectbox(
        "Sortering",
        [
            "Nieuwste",
            "Hoogste deal score",
            "Hoogste investment score",
            "Hoogste opportunity score",
            "Hoogste split potential",
            "Hoogste vertical extension potential",
            "Hoogste rental potential",
            "Hoogste renovation potential",
            "Laagste vraagprijs",
            "Hoogste vraagprijs",
        ],
        key="deal_sort",
    )
    exact_address_query = st.text_input(
        "Zoek exact adres",
        value="",
        key="deal_exact_address_query",
        help="Exacte match op adres (hoofdletterongevoelig), bijv. Mathenesserlaan 369 A/B.",
    )

    candidates = DATABASE_SERVICE.list_deal_candidates(
        limit=500,
        city=None if city_filter == "Alle steden" else city_filter,
        source_id=source_options.get(source_label),
        minimum_score=min_score,
        priority=None if priority_filter == "alle" else priority_filter,
        sort_by="detected_at_desc",
    )

    if not candidates:
        st.info("Geen deal candidates gevonden voor de gekozen filters.")
        _deal_finder_marker("deal_candidates_section_done_empty")
        return

    deal_intelligence_rows = _build_deal_intelligence_rows(candidates)
    if isinstance(exact_address_query, str) and exact_address_query.strip():
        normalized_query = _normalize_address_input(exact_address_query)
        deal_intelligence_rows = [
            row for row in deal_intelligence_rows
            if _normalize_address_input(row.get("adres") or "") == normalized_query
        ]
    sorted_deal_intelligence_rows = _sort_deal_intelligence_rows(deal_intelligence_rows, sort_choice)

    if isinstance(exact_address_query, str) and exact_address_query.strip():
        st.caption(f"Exacte adresfilter actief: {len(sorted_deal_intelligence_rows)} resultaat/resultaten.")

    st.markdown("#### Deal Intelligence Pro tabel")
    _render_rows_with_columns(
        rows=[
            {
                "address": row.get("adres") or "Onbekend",
                "city": row.get("plaats") or "Onbekend",
                "price": _format_currency(row.get("vraagprijs")),
                "living_area": _format_number(row.get("woonoppervlak")),
                "funda_living_area": _format_number(row.get("funda_woonoppervlak")) if row.get("funda_woonoppervlak") is not None else "Niet beschikbaar",
                "bag_living_area": _format_number(row.get("bag_oppervlak")) if row.get("bag_oppervlak") is not None else "Niet beschikbaar",
                "calculation_area": _format_number(row.get("gebruikt_rekenoppervlak")) if row.get("gebruikt_rekenoppervlak") is not None else "Niet beschikbaar",
                "calculation_source": row.get("bron_rekenoppervlak") or "Niet beschikbaar",
                "plot_size": _format_number(row.get("perceel")),
                "energy_label": row.get("energielabel") or "Onbekend",
                "construction_year": row.get("bouwjaar") if row.get("bouwjaar") is not None else "Onbekend",
                "price_per_m2": _format_currency(row.get("asking_price_per_m2") or row.get("price_per_m2")) if (row.get("asking_price_per_m2") is not None or row.get("price_per_m2") is not None) else "Niet beschikbaar",
                "woz_per_m2": _format_currency(row.get("woz_per_m2")) if row.get("woz_per_m2") is not None else "Niet beschikbaar",
                "funda_bag_difference_m2": _format_number(row.get("funda_bag_verschil_m2")) if row.get("funda_bag_verschil_m2") is not None else "Niet beschikbaar",
                "funda_bag_difference_pct": _format_percentage(_safe_number(row.get("funda_bag_verschil_pct")), decimals=2) if _safe_number(row.get("funda_bag_verschil_pct")) is not None else "Niet beschikbaar",
                "city_avg_price_per_m2": _format_currency(row.get("city_avg_price_per_m2")) if row.get("city_avg_price_per_m2") is not None else "Onbekend",
                "difference_vs_city_avg_pct": _format_percentage(_safe_number(row.get("difference_vs_city_avg_pct")), decimals=2),
                "bag_usage_purpose": row.get("bag_usage_purpose") or "Niet beschikbaar",
                "bag_building_year": row.get("bag_bouwjaar") if row.get("bag_bouwjaar") is not None else "Niet beschikbaar",
                "bag_confidence": row.get("bag_confidence_score") if row.get("bag_confidence_score") is not None else "Niet beschikbaar",
                "bag_quality_flags": ", ".join(str(item) for item in (row.get("bag_quality_flags") or [])) or "Geen",
                "woz_value": _format_currency(row.get("woz_value")) if row.get("woz_value") is not None else "Niet beschikbaar",
                "woz_valuation_year": row.get("woz_valuation_year") if row.get("woz_valuation_year") is not None else "Niet beschikbaar",
                "woz_difference_eur": _format_currency(row.get("asking_price_minus_woz_value")) if row.get("asking_price_minus_woz_value") is not None else "Niet beschikbaar",
                "woz_difference_pct": _woz_pct_badge(_safe_number(row.get("asking_price_vs_woz_percentage"))),
                "days_on_market": row.get("days_on_market") if row.get("days_on_market") is not None else "Onbekend",
                "listed_since": row.get("listed_since") or "Onbekend",
                "asking_price_status": row.get("asking_price_status") or "unknown",
                "asking_price_text": row.get("asking_price_text") or "",
                "price_reductions": row.get("price_reduction_count") if row.get("price_reduction_count") is not None else 0,
                "previous_asking_prices": row.get("vorige_vraagprijzen") or "Onbekend",
                "split_potential": row.get("split_potential") if row.get("split_potential") is not None else "Onbekend",
                "vertical_extension_potential": row.get("vertical_extension_potential") if row.get("vertical_extension_potential") is not None else "Onbekend",
                "rental_potential": row.get("rental_potential") if row.get("rental_potential") is not None else "Onbekend",
                "renovation_potential": row.get("renovation_potential") if row.get("renovation_potential") is not None else "Onbekend",
                "investment_score": row.get("investment_score") if row.get("investment_score") is not None else "Onbekend",
                "opportunity_score": row.get("opportunity_score") if row.get("opportunity_score") is not None else "Onbekend",
                "recommendation": row.get("investment_recommendation") or "★ Avoid",
                "source": row.get("bron") or _source_display_name(None, row.get("source_url")),
                "source_timestamp": row.get("source_timestamp") or "Onbekend",
                "source_url": row.get("source_url") or "",
            }
            for row in sorted_deal_intelligence_rows
        ],
        columns=[
            ("Adres", "address"),
            ("Plaats", "city"),
            ("Vraagprijs", "price"),
            ("Vraagprijs status", "asking_price_status"),
            ("Vraagprijs tekst", "asking_price_text"),
            ("Woonoppervlak", "living_area"),
            ("Funda woonoppervlak", "funda_living_area"),
            ("BAG-oppervlak", "bag_living_area"),
            ("Gebruikt rekenoppervlak", "calculation_area"),
            ("Bron rekenoppervlak", "calculation_source"),
            ("Perceel", "plot_size"),
            ("Energielabel", "energy_label"),
            ("Bouwjaar", "construction_year"),
            ("Vraagprijs per m²", "price_per_m2"),
            ("WOZ per m²", "woz_per_m2"),
            ("Verschil Funda/BAG m²", "funda_bag_difference_m2"),
            ("Verschil Funda/BAG %", "funda_bag_difference_pct"),
            ("Gem. prijs per m² stad", "city_avg_price_per_m2"),
            ("Verschil t.o.v. stad", "difference_vs_city_avg_pct"),
            ("BAG gebruiksdoel", "bag_usage_purpose"),
            ("BAG bouwjaar", "bag_building_year"),
            ("BAG confidence", "bag_confidence"),
            ("BAG waarschuwingen", "bag_quality_flags"),
            ("WOZ-waarde", "woz_value"),
            ("WOZ-jaar", "woz_valuation_year"),
            ("Verschil €", "woz_difference_eur"),
            ("Verschil %", "woz_difference_pct"),
            ("Online sinds", "listed_since"),
            ("Dagen op markt", "days_on_market"),
            ("Prijsverlagingen", "price_reductions"),
            ("Vorige vraagprijzen", "previous_asking_prices"),
            ("Splitsingspotentieel", "split_potential"),
            ("Optoppotentieel", "vertical_extension_potential"),
            ("Verhuurpotentieel", "rental_potential"),
            ("Renovatiepotentieel", "renovation_potential"),
            ("Investment score", "investment_score"),
            ("Opportunity score", "opportunity_score"),
            ("Investeringsadvies", "recommendation"),
            ("Bron", "source"),
            ("Bron timestamp", "source_timestamp"),
            ("Bron URL", "source_url"),
        ],
        empty_message="Geen Deal Intelligence data beschikbaar.",
    )

    selected_candidate = _resolve_selected_deal_candidate(candidates)

    listing = (selected_candidate or {}).get("listing") or {}
    listing_id = listing.get("id")
    if not listing_id:
        return

    detail = DATABASE_SERVICE.get_listing_detail(str(listing_id))
    listing_detail = detail.get("listing") or {}
    latest_snapshot = detail.get("latest_snapshot") or {}
    source_detail = detail.get("source") or {}
    candidate_detail = detail.get("candidate") or {}

    st.markdown("### Geselecteerde listing details")
    _deal_finder_marker("selected_listing_details_start")
    st.write(f"Titel: {listing_detail.get('title') or 'Onbekend'}")
    st.write(f"Adres: {listing_detail.get('address') or 'Onbekend'}")
    st.write(f"Stad: {listing_detail.get('city') or 'Onbekend'}")
    st.write(f"Vraagprijs: {_format_currency(listing_detail.get('asking_price'))}")
    st.write(f"Oppervlakte: {_format_number(listing_detail.get('surface_m2'))} m²")
    st.write(f"Status: {listing_detail.get('listing_status') or 'Onbekend'}")
    st.write(f"Bron: {source_detail.get('name') or 'Onbekend'}")

    listing_raw_payload = listing_detail.get("raw_payload") if isinstance(listing_detail.get("raw_payload"), dict) else {}
    detail_funda_area = _safe_number(listing_raw_payload.get("funda_living_area_m2") or listing_detail.get("surface_m2"))
    detail_bag_area = _safe_number(listing_raw_payload.get("bag_official_floor_area_m2"))
    detail_calc_area = _safe_number(listing_raw_payload.get("calculation_area_m2") or detail_funda_area)
    detail_calc_source = str(listing_raw_payload.get("calculation_area_source") or "Funda")
    detail_area_diff_m2 = _safe_number(listing_raw_payload.get("living_area_difference_m2"))
    detail_area_diff_pct = _safe_number(listing_raw_payload.get("living_area_difference_percentage"))
    detail_asking_ppm2 = _safe_number(listing_raw_payload.get("asking_price_per_m2"))
    detail_woz_ppm2 = _safe_number(listing_raw_payload.get("woz_value_per_m2"))
    detail_bag_usage = str(listing_raw_payload.get("bag_usage_purpose") or "")
    detail_bag_year = _safe_score(listing_raw_payload.get("bag_building_year"))
    detail_bag_confidence = _safe_score(listing_raw_payload.get("bag_confidence_score"))
    detail_bag_flags = listing_raw_payload.get("bag_quality_flags") if isinstance(listing_raw_payload.get("bag_quality_flags"), list) else []
    detail_bag_address_id = str(listing_raw_payload.get("bag_address_id") or "")
    detail_bag_vbo_id = str(listing_raw_payload.get("bag_verblijfsobject_id") or listing_raw_payload.get("bag_id") or "")
    detail_bag_pand_id = str(listing_raw_payload.get("bag_pand_id") or "")
    detail_woz_value = _safe_number(listing_raw_payload.get("latest_woz_value") or listing_detail.get("latest_woz_value"))
    detail_woz_year = _safe_score(listing_raw_payload.get("woz_valuation_year") or listing_detail.get("woz_valuation_year"))
    detail_woz_source = str(listing_raw_payload.get("woz_source") or "Kadaster WOZ-waardeloket")
    detail_woz_retrieval_date = str(listing_raw_payload.get("woz_retrieval_date") or "Niet beschikbaar")
    detail_diff_eur, detail_diff_pct = _woz_metrics(_safe_number(listing_detail.get("asking_price")), detail_woz_value)

    st.markdown("#### WOZ details")
    st.write(f"Asking price: {_format_currency(listing_detail.get('asking_price'))}")
    st.write(f"WOZ value: {_format_currency(detail_woz_value) if detail_woz_value is not None else 'Niet beschikbaar'}")
    st.write(f"Valuation year: {detail_woz_year if detail_woz_year is not None else 'Niet beschikbaar'}")
    st.write(f"Difference in euros: {_format_currency(detail_diff_eur) if detail_diff_eur is not None else 'Niet beschikbaar'}")
    st.write(f"Difference in percentage: {_format_percentage(detail_diff_pct, decimals=2) if detail_diff_pct is not None else 'Niet beschikbaar'}")
    st.write(f"WOZ source: {detail_woz_source}")
    st.write(f"Retrieval date: {detail_woz_retrieval_date}")
    st.caption("WOZ is een historische belastingwaardering en geen actuele marktwaardering.")

    st.markdown("#### BAG en oppervlakte details")
    st.write(f"BAG nummeraanduiding ID: {detail_bag_address_id or 'Niet beschikbaar'}")
    st.write(f"BAG verblijfsobject ID: {detail_bag_vbo_id or 'Niet beschikbaar'}")
    st.write(f"BAG pand ID: {detail_bag_pand_id or 'Niet beschikbaar'}")
    st.write(f"Funda woonoppervlak: {_format_number(detail_funda_area) if detail_funda_area is not None else 'Niet beschikbaar'} m²")
    st.write(f"BAG-oppervlak: {_format_number(detail_bag_area) if detail_bag_area is not None else 'Niet beschikbaar'} m²")
    st.write(f"Gebruikt rekenoppervlak: {_format_number(detail_calc_area) if detail_calc_area is not None else 'Niet beschikbaar'} m²")
    st.write(f"Bron rekenoppervlak: {detail_calc_source or 'Niet beschikbaar'}")
    st.write(f"Verschil Funda/BAG m²: {_format_number(detail_area_diff_m2) if detail_area_diff_m2 is not None else 'Niet beschikbaar'}")
    st.write(f"Verschil Funda/BAG %: {_format_percentage(detail_area_diff_pct, decimals=2) if detail_area_diff_pct is not None else 'Niet beschikbaar'}")
    st.write(f"Vraagprijs per m²: {_format_currency(detail_asking_ppm2) if detail_asking_ppm2 is not None else 'Niet beschikbaar'}")
    st.write(f"WOZ per m²: {_format_currency(detail_woz_ppm2) if detail_woz_ppm2 is not None else 'Niet beschikbaar'}")
    st.write(f"BAG gebruiksdoel: {detail_bag_usage or 'Niet beschikbaar'}")
    st.write(f"BAG bouwjaar: {detail_bag_year if detail_bag_year is not None else 'Niet beschikbaar'}")
    st.write(f"BAG matching confidence: {detail_bag_confidence if detail_bag_confidence is not None else 'Niet beschikbaar'}")
    st.write(f"Data-kwaliteit waarschuwingen: {', '.join(str(item) for item in detail_bag_flags) if detail_bag_flags else 'Geen'}")

    deal_score = _safe_score((selected_candidate or {}).get("score"))
    if deal_score is None:
        deal_score = _safe_score(candidate_detail.get("hidden_value_score"))

    gross_yield_value = _extract_gross_yield_value(selected_candidate, listing_detail, latest_snapshot, candidate_detail)
    price_per_m2_text = _format_price_per_m2_currency(listing_detail.get("asking_price"), listing_detail.get("surface_m2"))
    estimated_market_value = _estimated_market_value(listing_detail.get("asking_price"))
    discount_vs_market_value = _discount_vs_market_value_percentage(listing_detail.get("asking_price"))
    maximum_purchase_price = _maximum_purchase_price_placeholder(listing_detail.get("asking_price"))
    difference_with_asking = _difference_with_asking_price_placeholder(listing_detail.get("asking_price"))
    difference_percentage = _difference_percentage_vs_asking(listing_detail.get("asking_price"))
    recommendation = _recommendation_from_difference_percentage(difference_percentage)
    return_score = _return_score_from_gross_yield(gross_yield_value)
    investment_intelligence = _build_investment_intelligence(
        city=listing_detail.get("city"),
        surface_m2=listing_detail.get("surface_m2"),
        gross_yield_value=gross_yield_value,
        discount_vs_market_value=discount_vs_market_value,
        difference_percentage=difference_percentage,
        recommendation=recommendation,
    )
    score_cards = [
        ("💰 Return", return_score),
        ("📍 Location", 17),
        ("🏗 Development potential", 12),
        ("💎 Valuation", 18),
        ("⚠ Risk", 10),
    ]
    overall_investment_score = sum(value for _, value in score_cards)
    overall_rating = _overall_investment_rating(overall_investment_score)
    ai_summary = _ai_investment_summary_from_rating(overall_rating)
    ai_strengths = _ai_strengths_text(overall_investment_score, gross_yield_value, recommendation)
    ai_weaknesses = _ai_weaknesses_text(difference_percentage, price_per_m2_text, discount_vs_market_value)
    ai_next_step = _ai_next_step_text(recommendation, gross_yield_value)
    comparable_subject = {
        "address": listing_detail.get("address"),
        "city": listing_detail.get("city"),
        "asking_price": listing_detail.get("asking_price"),
        "surface_m2": listing_detail.get("surface_m2"),
    }
    comparables = COMPARABLE_SALES_SERVICE.get_comparables(comparable_subject)
    sort_options = {
        "Address": "address",
        "Distance (meters)": "distance_meters",
        "Living area (m²)": "living_area_m2",
        "Asking price": "asking_price",
        "Sold price": "sold_price",
        "Sold date": "sold_date",
        "Price per m²": "price_per_m2",
        "Difference with subject (%)": "difference_with_subject_pct",
    }

    with st.container(border=True):
        st.markdown("#### Deal scorekaart")
        for label, value in score_cards:
            with st.container(border=True):
                st.markdown(f"**{label}**")
                st.write(f"{value}/20")
                st.progress(value / 20)
                if label == "💰 Return":
                    st.write(f"Gross yield: {_format_percentage(gross_yield_value)}")

        st.markdown("#### Investment Intelligence")
        for category in investment_intelligence.get("categories", []):
            category_name = category.get("name") or "Onbekend"
            category_score = _cap_score_0_20(category.get("score"))
            st.markdown(f"**{category_name}**")
            st.write(f"{category_score}/20")
            st.progress(category_score / 20)

        st.markdown(f"**Investment Intelligence Score:** {investment_intelligence.get('overall_score', 0)}/100")
        st.markdown(f"**Investment Intelligence Rating:** {investment_intelligence.get('rating', 'D')}")
        st.markdown("**Category explanations**")
        for category in investment_intelligence.get("categories", []):
            category_name = category.get("name") or "Onbekend"
            category_explanation = category.get("explanation") or "Geen toelichting beschikbaar."
            st.markdown(f"- {category_name}: {category_explanation}")

        st.markdown(f"**Overall Investment Score:** {overall_investment_score}/100")
        st.markdown(f"**Rating:** {overall_rating}")
        st.markdown(f"**Prijs per m²:** {price_per_m2_text}")
        st.markdown(
            f"**Estimated market value:** {_format_currency(estimated_market_value)}"
            if estimated_market_value is not None
            else "**Estimated market value:** Onbekend"
        )
        st.markdown(
            f"**Discount:** -{_format_percentage(discount_vs_market_value)}"
            if discount_vs_market_value is not None
            else "**Discount:** Onbekend"
        )
        st.markdown(
            f"**Maximum purchase price:** {_format_currency(maximum_purchase_price)}"
            if maximum_purchase_price is not None
            else "**Maximum purchase price:** Onbekend"
        )
        st.markdown(
            f"**Difference with asking price:** {_format_currency(difference_with_asking)} ({_format_percentage(difference_percentage)})"
            if difference_with_asking is not None and difference_percentage is not None
            else "**Difference with asking price:** Onbekend"
        )
        st.markdown(f"**Recommendation:** {recommendation}")
        st.markdown(f"**Gross yield:** {_format_percentage(gross_yield_value)}")
        st.markdown("#### AI Investment Summary")
        st.write(ai_summary)
        st.markdown(f"- Strengths: {ai_strengths}")
        st.markdown(f"- Weaknesses: {ai_weaknesses}")
        st.markdown(f"- Suggested next step: {ai_next_step}")

    with st.container(border=True):
        st.markdown("#### Comparable Sales")
        col_sort_key, col_sort_direction = st.columns([3, 2])
        with col_sort_key:
            selected_sort_label = st.selectbox(
                "Sort by",
                list(sort_options.keys()),
                index=list(sort_options.keys()).index("Distance (meters)"),
                key="comps_sort_key",
            )
        with col_sort_direction:
            sort_desc = st.checkbox("Descending", value=False, key="comps_sort_desc")

        sorted_comps = COMPARABLE_SALES_SERVICE.sort_comparables(
            comparables,
            sort_key=sort_options.get(selected_sort_label, "distance_meters"),
            descending=sort_desc,
        )
        comps_rows = COMPARABLE_SALES_SERVICE.build_table_rows(sorted_comps)
        _render_rows_with_columns(
            rows=comps_rows,
            columns=[
                ("Address", "Address"),
                ("Distance (meters)", "Distance (meters)"),
                ("Living area (m²)", "Living area (m²)"),
                ("Asking price", "Asking price"),
                ("Sold price", "Sold price"),
                ("Sold date", "Sold date"),
                ("Price per m²", "Price per m²"),
                ("Difference with subject (%)", "Difference with subject (%)"),
            ],
            empty_message="Geen vergelijkbare verkopen beschikbaar.",
        )

        comps_summary = COMPARABLE_SALES_SERVICE.calculate_summary(sorted_comps)
        comps_valuation = COMPARABLE_SALES_SERVICE.calculate_valuation(
            subject_asking_price=listing_detail.get("asking_price"),
            subject_surface_m2=listing_detail.get("surface_m2"),
            comparables=sorted_comps,
        )

        st.markdown(
            f"**Average price per m²:** {_format_currency(comps_summary.get('average_price_per_m2'))}"
            if comps_summary.get("average_price_per_m2") is not None
            else "**Average price per m²:** Onbekend"
        )
        st.markdown(
            f"**Median price per m²:** {_format_currency(comps_summary.get('median_price_per_m2'))}"
            if comps_summary.get("median_price_per_m2") is not None
            else "**Median price per m²:** Onbekend"
        )

        lowest_comp = comps_summary.get("lowest_comparable")
        highest_comp = comps_summary.get("highest_comparable")
        st.markdown(
            f"**Lowest comparable:** {(lowest_comp.address if lowest_comp else 'Onbekend')} ({_format_currency(lowest_comp.price_per_m2) if lowest_comp else 'Onbekend'} per m²)"
        )
        st.markdown(
            f"**Highest comparable:** {(highest_comp.address if highest_comp else 'Onbekend')} ({_format_currency(highest_comp.price_per_m2) if highest_comp else 'Onbekend'} per m²)"
        )

        st.markdown(
            f"**Estimated market value (comps):** {_format_currency(comps_valuation.get('estimated_market_value'))}"
            if comps_valuation.get("estimated_market_value") is not None
            else "**Estimated market value (comps):** Onbekend"
        )
        st.markdown(
            f"**Recommended maximum bid (comps):** {_format_currency(comps_valuation.get('recommended_max_bid'))}"
            if comps_valuation.get("recommended_max_bid") is not None
            else "**Recommended maximum bid (comps):** Onbekend"
        )
        st.markdown(
            f"**Negotiation margin (%):** {_format_percentage(comps_valuation.get('negotiation_margin_pct'))}"
            if comps_valuation.get("negotiation_margin_pct") is not None
            else "**Negotiation margin (%):** Onbekend"
        )

    st.write(f"Priority: {candidate_detail.get('priority') or 'Onbekend'}")
    st.write(f"Reason codes: {', '.join(candidate_detail.get('reasons') or []) or 'Geen'}")
    if listing_detail.get("source_url"):
        st.link_button("Open listing", listing_detail.get("source_url"))

    metadata_payload = listing_detail.get("raw_payload") if isinstance(listing_detail.get("raw_payload"), dict) else {}
    metadata_info = metadata_payload.get("metadata") if isinstance(metadata_payload.get("metadata"), dict) else {}
    extraction_meta = metadata_payload.get("metadata_extraction") if isinstance(metadata_payload.get("metadata_extraction"), dict) else {}
    extraction_status = "success" if extraction_meta.get("success") else "failed"
    st.markdown("#### Metadata extractie")
    st.write(f"Extraction status: {extraction_status}")
    st.write(f"Extraction methode: {extraction_meta.get('extraction_method') or 'none'}")
    st.write(f"Confidence: {extraction_meta.get('confidence') if extraction_meta.get('confidence') is not None else 0}")
    if metadata_info.get("title"):
        st.write(f"Metadata titel: {metadata_info.get('title')}")
    if metadata_info.get("address"):
        st.write(f"Metadata adres: {metadata_info.get('address')}")
    if metadata_info.get("city"):
        st.write(f"Metadata stad: {metadata_info.get('city')}")
    if metadata_info.get("asking_price") is not None:
        st.write(f"Metadata vraagprijs: {_format_currency(metadata_info.get('asking_price'))}")
    if metadata_info.get("surface_m2") is not None:
        st.write(f"Metadata oppervlakte: {_format_number(metadata_info.get('surface_m2'))} m²")
    extraction_warnings = extraction_meta.get("warnings") if isinstance(extraction_meta.get("warnings"), list) else []
    if extraction_warnings:
        for warning in extraction_warnings:
            st.warning(str(warning))

    col_review, col_refresh, col_analyze = st.columns(3)
    with col_review:
        if st.button("Markeer reviewed", key="mark_candidate_reviewed"):
            candidate_id = selected_candidate.get("id") if isinstance(selected_candidate, dict) else None
            if candidate_id:
                DATABASE_SERVICE.mark_candidate_reviewed(str(candidate_id), review_status="reviewed")
                st.success("Candidate gemarkeerd als reviewed.")

    with col_refresh:
        if st.button("Metadata opnieuw ophalen", key="refresh_listing_metadata"):
            refresh_result = DEAL_FINDER_ORCHESTRATOR.refresh_listing_metadata(str(listing_id))
            if refresh_result.get("ok"):
                extraction = refresh_result.get("extraction") or {}
                status_label = "succesvol" if extraction.get("success") else "mislukt"
                st.success(
                    "Metadata opnieuw opgehaald: "
                    f"{status_label}, snapshot_changed={bool(refresh_result.get('snapshot_changed'))}"
                )
                if extraction.get("warnings"):
                    for warning in extraction.get("warnings"):
                        st.warning(str(warning))
                st.session_state["deal_selected_listing_id"] = str(refresh_result.get("listing_id") or listing_id)
                _clear_deal_finder_refresh_state()
                st.rerun()
            else:
                st.error(f"Metadata ophalen mislukt: {refresh_result.get('error') or 'Onbekende fout'}")

    with col_analyze:
        if st.button("Analyseer geselecteerde listing", key="analyze_selected_listing"):
            description = latest_snapshot.get("description") or listing_detail.get("title") or ""
            source_text = " ".join([str(listing_detail.get("title") or ""), str(listing_detail.get("address") or ""), str(description or "")]).strip()
            if len(source_text.split()) < 5:
                st.warning("Onvoldoende tekst beschikbaar om deze listing te analyseren.")
            else:
                try:
                    analysis = analyze_property(source_text)
                except Exception as error:
                    st.error(f"Analyse mislukt: {error}")
                else:
                    _persist_analysis_result(str(listing_detail.get("source_url") or ""), analysis)
                    _render_analysis_result(str(listing_detail.get("source_url") or ""), analysis)
    _deal_finder_marker("selected_listing_details_done")
    _deal_finder_marker("deal_candidates_section_done")
    _deal_finder_marker("page_done")


def main():
    st.set_page_config(page_title="PropertyHunter AI", page_icon="🏠", layout="centered")
    st.title("PropertyHunter AI")
    with st.sidebar:
        st.markdown("## Navigatie")
        page = st.radio("Kies een onderdeel", ["Nieuwe analyse", "Mijn analyses", "Dashboard", "Deal Finder", "PropertyHunter Interface"], index=0)

    if page == "Nieuwe analyse":
        _render_new_analysis_page()
    elif page == "Mijn analyses":
        _render_my_analyses_page()
    elif page == "Dashboard":
        _render_dashboard_page()
    elif page == "Deal Finder":
        _render_deal_finder_page()
    else:
        _render_propertyhunter_interface_page()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "scan":
        raise SystemExit(_run_scan_cli(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] == "klusvastgoed-national":
        raise SystemExit(_run_klusvastgoed_national_cli(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] == "run":
        raise SystemExit(_run_end_to_end_cli(sys.argv[2:]))
    main()
