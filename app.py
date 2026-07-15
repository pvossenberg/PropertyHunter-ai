from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import sys
import time
from typing import Any

import streamlit as st
import requests

from ai.analyzer import analyze_property
from deal_finder.orchestrator import DealFinderOrchestrator
from models.permit import PermitRecord
from models.property import Property
from models.transaction import PropertyTransaction
from scrapers.base import ScrapeResult
from scrapers.router import scrape_url
from services.calculations import calculate_days_on_market, calculate_price_change_since_last_transaction, calculate_price_per_m2, calculate_price_reduction
from services.comparable_sales import ComparableSalesService
from services.database import DatabaseService
from services.end_to_end_workflow import PropertyHunterEndToEndWorkflow
from services.property_enrichment import PropertyEnrichmentEngine


DATABASE_SERVICE = DatabaseService.from_env()
DEAL_FINDER_ORCHESTRATOR = DealFinderOrchestrator(DATABASE_SERVICE)
COMPARABLE_SALES_SERVICE = ComparableSalesService()
PROPERTY_ENRICHMENT_ENGINE = PropertyEnrichmentEngine()
PROPERTY_HUNTER_WORKFLOW = PropertyHunterEndToEndWorkflow(
    orchestrator=DEAL_FINDER_ORCHESTRATOR,
    database_service=DATABASE_SERVICE,
)
LOGGER = logging.getLogger(__name__)


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
        "bag_nummeraanduiding_id": property_data.get("bag_nummeraanduiding_id"),
        "bag_pand_id": property_data.get("bag_pand_id"),
        "bag_building_year": property_data.get("bag_building_year"),
        "bag_usage_purpose": property_data.get("bag_usage_purpose"),
        "bag_official_floor_area_m2": property_data.get("bag_official_floor_area_m2"),
        "bag_coordinates_rd": property_data.get("bag_coordinates_rd"),
        "bag_coordinates_ll": property_data.get("bag_coordinates_ll"),
        "bag_postcode": property_data.get("bag_postcode"),
        "bag_municipality": property_data.get("bag_municipality"),
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
    max_pages: int = 1000,
    timeout_seconds: float = 12.0,
) -> dict[str, Any]:
    active_orchestrator = orchestrator or DEAL_FINDER_ORCHESTRATOR
    active_database = database_service or DATABASE_SERVICE
    active_output_dir = output_dir or Path("output") / "scan-runs"

    adapter = active_orchestrator.source_registry.resolve(source_name)
    if adapter is None:
        error = f"Unknown source adapter: {source_name}"
        print(error)
        return {"ok": False, "error": error}

    configuration = {
        "start_url": getattr(adapter, "default_start_url", "") or "",
        "max_pages": max_pages,
        "timeout_seconds": timeout_seconds,
    }
    is_valid, warnings = adapter.validate_configuration(configuration)
    if not is_valid:
        error = "; ".join(warnings) or "Invalid source configuration."
        print(error)
        return {"ok": False, "error": error, "warnings": warnings}

    scan_started_at = time.perf_counter()
    print(f"Start scan voor source: {source_name}")
    print(f"Config: max_pages={max_pages}, timeout_seconds={timeout_seconds}")

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
        "summary": {
            "listings_found": listings_found,
            "listings_imported": imported_count,
            "fully_imported": fully_imported_count,
            "partially_imported": partially_imported_count,
            "listings_failed": failed_count,
            "average_import_time_seconds": average_import_time,
            "total_elapsed_seconds": total_elapsed_seconds,
            "warnings": warnings,
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
        "warnings": warnings,
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
    parser.add_argument("--max-pages", type=int, default=1000, help="Maximum number of paginated result pages to crawl")
    parser.add_argument("--timeout-seconds", type=float, default=12.0, help="HTTP timeout per request in seconds")
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
        result = _run_source_scan(
            source_name,
            output_dir=Path(args.output_dir),
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


def _to_display_text(value) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, list):
        return ", ".join(str(item) for item in value)
    if isinstance(value, dict):
        return str(value)
    return str(value)


def _render_rows_with_columns(rows: list[dict], columns: list[tuple[str, str]], empty_message: str):
    if not rows:
        st.info(empty_message)
        return

    header_columns = st.columns(len(columns))
    for index, (label, _) in enumerate(columns):
        header_columns[index].markdown(f"**{label}**")

    for row in rows:
        value_columns = st.columns(len(columns))
        for index, (_, key) in enumerate(columns):
            value_columns[index].write(_to_display_text(row.get(key)))


def _render_deal_candidate_cards(candidates: list[dict]):
    if not candidates:
        st.info("Geen deal candidates gevonden voor de gekozen filters.")
        return

    for item in candidates:
        listing = item.get("listing") or {}
        source = item.get("source") or {}
        title = listing.get("title") or "Onbekend"
        address = listing.get("address") or "Onbekend"
        city = listing.get("city") or "Onbekend"
        asking_price = _format_currency(listing.get("asking_price"))
        surface_value = listing.get("surface_m2")
        surface_text = f"{_format_number(surface_value)} m²" if surface_value not in (None, "") else "Onbekend"
        score_text = item.get("score") if item.get("score") is not None else "n.v.t."
        priority = item.get("priority") or "n.v.t."
        source_name = source.get("name") or "Onbekend"
        review_status = item.get("review_status") or "new"

        with st.container(border=True):
            st.markdown(f"**{title}**")
            st.write(f"{address} · {city}")
            st.write(f"Vraagprijs: {asking_price}")
            st.write(f"Oppervlakte: {surface_text}")
            st.write(f"Score: {score_text} | Priority: {priority} | Bron: {source_name} | Review status: {review_status}")


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
                "woonoppervlak": _safe_number(_listing_value(listing, "surface_m2", "living_area")),
                "perceel": _safe_number(_listing_value(listing, "plot_size_m2", "plot_size")),
                "slaapkamers": _safe_score(_listing_value(listing, "bedrooms")),
                "energielabel": str(_listing_value(listing, "energy_label") or "Onbekend"),
                "bouwjaar": _safe_score(_listing_value(listing, "construction_year", "bag_building_year")),
                "days_on_market": _safe_score(_listing_value(listing, "days_on_market")),
                "listing_history": _propertyhunter_listing_history_text(listing),
                "investment_score": _safe_score(candidate.get("investment_score")),
                "opportunity_score": _safe_score(candidate.get("hidden_value_score") if candidate.get("hidden_value_score") is not None else candidate.get("score")),
                "bron": str(source.get("name") or _listing_value(listing, "source_url") or "Onbekend"),
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

    st.markdown("#### Candidate")
    st.json(candidate)
    st.markdown("#### Source")
    st.json(source)
    st.markdown("#### Listing")
    st.json(listing)
    st.markdown("#### Latest snapshot")
    st.json(latest_snapshot)
    st.markdown(f"#### Snapshots ({len(snapshots)})")
    st.json(snapshots)


def _render_propertyhunter_interface_page():
    st.subheader("PropertyHunter Interface")

    if not DATABASE_SERVICE.is_enabled:
        st.info("Supabase is niet geconfigureerd. Stel SUPABASE_URL en SUPABASE_SERVICE_ROLE_KEY in om deze interface te gebruiken.")
        return

    selected_listing_id = str(st.query_params.get("listing_id") or st.session_state.get("ph_selected_listing_id") or "").strip()
    if selected_listing_id:
        st.session_state["ph_selected_listing_id"] = selected_listing_id

    active_view = str(st.session_state.get("ph_view") or ("detail" if selected_listing_id else "list"))
    st.session_state["ph_view"] = active_view

    if active_view == "detail" and selected_listing_id:
        _render_propertyhunter_detail_page(selected_listing_id)
        return

    health = DATABASE_SERVICE.get_source_health()
    metrics = _latest_scan_metrics(health)
    candidates = DATABASE_SERVICE.list_deal_candidates(limit=2000, sort_by="detected_at_desc")
    rows = _build_propertyhunter_rows(candidates)

    scored_rows = [row for row in rows if _safe_score(row.get("opportunity_score")) is not None]
    avg_opportunity = 0.0
    if scored_rows:
        avg_opportunity = round(sum(int(_safe_score(row.get("opportunity_score")) or 0) for row in scored_rows) / len(scored_rows), 2)

    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.metric("Aantal gevonden woningen", metrics.get("found", 0))
    with col2:
        st.metric("Aantal nieuwe woningen", metrics.get("new", 0))
    with col3:
        st.metric("Aantal gewijzigde woningen", metrics.get("changed", 0))
    with col4:
        st.metric("Gemiddelde Opportunity Score", avg_opportunity)

    top_20 = sorted(rows, key=lambda item: int(_safe_score(item.get("opportunity_score")) or -1), reverse=True)[:20]
    st.markdown("### Top 20 kansen")
    if top_20:
        _render_rows_with_columns(
            rows=[
                {
                    "adres": row.get("adres"),
                    "plaats": row.get("plaats"),
                    "vraagprijs": _format_currency(row.get("vraagprijs")),
                    "opportunity_score": row.get("opportunity_score") if row.get("opportunity_score") is not None else "Onbekend",
                    "investment_score": row.get("investment_score") if row.get("investment_score") is not None else "Onbekend",
                    "bron": row.get("bron"),
                }
                for row in top_20
            ],
            columns=[
                ("Adres", "adres"),
                ("Plaats", "plaats"),
                ("Vraagprijs", "vraagprijs"),
                ("Opportunity score", "opportunity_score"),
                ("Investment score", "investment_score"),
                ("Bron", "bron"),
            ],
            empty_message="Geen kansen beschikbaar.",
        )
    else:
        st.info("Nog geen kansen beschikbaar.")

    st.markdown("### Filters")
    places = sorted({str(row.get("plaats") or "").strip() for row in rows if str(row.get("plaats") or "").strip() and str(row.get("plaats") or "").strip() != "Onbekend"})
    energy_labels = sorted({str(row.get("energielabel") or "").strip().upper() for row in rows if str(row.get("energielabel") or "").strip() and str(row.get("energielabel") or "").strip().lower() != "onbekend"})
    prices = [_safe_number(row.get("vraagprijs")) for row in rows if _safe_number(row.get("vraagprijs")) is not None]

    min_price_default = int(min(prices)) if prices else 0
    max_price_default = int(max(prices)) if prices else 0

    filter_col_1, filter_col_2, filter_col_3 = st.columns(3)
    with filter_col_1:
        selected_place = st.selectbox("Plaats", ["Alle plaatsen", *places], key="ph_filter_place")
        selected_energy_label = st.selectbox("Energielabel", ["Alle labels", *energy_labels], key="ph_filter_energy")
    with filter_col_2:
        min_price = st.number_input("Minimale vraagprijs", min_value=0, value=min_price_default, step=1000, key="ph_filter_min_price")
        max_price = st.number_input("Maximale vraagprijs", min_value=0, value=max_price_default if max_price_default >= min_price_default else min_price_default, step=1000, key="ph_filter_max_price")
    with filter_col_3:
        min_surface = st.number_input("Minimaal woonoppervlak (m²)", min_value=0, value=0, step=1, key="ph_filter_min_surface")
        min_opportunity_score = st.slider("Minimale opportunity score", min_value=0, max_value=100, value=0, key="ph_filter_min_opp")

    filtered_rows = _filter_propertyhunter_rows(
        rows,
        place=None if selected_place == "Alle plaatsen" else selected_place,
        min_price=float(min_price) if min_price > 0 else None,
        max_price=float(max_price) if max_price > 0 else None,
        min_surface=float(min_surface) if min_surface > 0 else None,
        energy_label=None if selected_energy_label == "Alle labels" else selected_energy_label,
        min_opportunity_score=int(min_opportunity_score) if min_opportunity_score > 0 else None,
    )

    st.markdown("### Woningtabel")
    if not filtered_rows:
        st.info("Geen woningen gevonden met de huidige filters.")
        return

    filtered_rows.sort(key=lambda item: int(_safe_score(item.get("opportunity_score")) or -1), reverse=True)

    table_rows = []
    for row in filtered_rows:
        table_rows.append(
            {
                "listing_id": row.get("listing_id"),
                "adres": row.get("adres"),
                "plaats": row.get("plaats"),
                "vraagprijs": _format_currency(row.get("vraagprijs")),
                "woonoppervlak": f"{_format_number(row.get('woonoppervlak'))} m²" if row.get("woonoppervlak") not in (None, "") else "Onbekend",
                "perceel": f"{_format_number(row.get('perceel'))} m²" if row.get("perceel") not in (None, "") else "Onbekend",
                "slaapkamers": row.get("slaapkamers") if row.get("slaapkamers") is not None else "Onbekend",
                "energielabel": row.get("energielabel") or "Onbekend",
                "bouwjaar": row.get("bouwjaar") if row.get("bouwjaar") is not None else "Onbekend",
                "days_on_market": row.get("days_on_market") if row.get("days_on_market") is not None else "Onbekend",
                "listing_history": row.get("listing_history"),
                "investment_score": row.get("investment_score") if row.get("investment_score") is not None else "Onbekend",
                "opportunity_score": row.get("opportunity_score") if row.get("opportunity_score") is not None else "Onbekend",
                "bron": row.get("bron") or "Onbekend",
            }
        )

    selection = st.dataframe(
        table_rows,
        hide_index=True,
        use_container_width=True,
        on_select="rerun",
        selection_mode="single-row",
        column_order=[
            "adres",
            "plaats",
            "vraagprijs",
            "woonoppervlak",
            "perceel",
            "slaapkamers",
            "energielabel",
            "bouwjaar",
            "days_on_market",
            "listing_history",
            "investment_score",
            "opportunity_score",
            "bron",
        ],
    )

    selected_indices = []
    if isinstance(selection, dict):
        selected_indices = (((selection.get("selection") or {}).get("rows")) or []) if isinstance(selection.get("selection"), dict) else []
    else:
        selected_indices = (((getattr(selection, "selection", {}) or {}).get("rows")) or []) if isinstance(getattr(selection, "selection", {}), dict) else []

    if selected_indices:
        selected_index = int(selected_indices[0])
        if 0 <= selected_index < len(table_rows):
            selected_listing = str(table_rows[selected_index].get("listing_id") or "").strip()
            if selected_listing:
                st.session_state["ph_selected_listing_id"] = selected_listing
                st.session_state["ph_view"] = "detail"
                st.query_params["listing_id"] = selected_listing
                st.rerun()


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
        st.dataframe(
            [{"Stad": city, "Aantal": count} for city, count in sorted(city_counts.items(), key=lambda item: item[1], reverse=True)],
            use_container_width=True,
        )
    else:
        st.info("Nog geen stadsgegevens beschikbaar.")

    st.markdown("### Top 5 hoogste scores")
    top_properties = stats.get("top_properties") or []
    if top_properties:
        st.dataframe(
            [
                {
                    "Score": _safe_score(item.get("investment_score")),
                    "Titel": item.get("title") or "Onbekend",
                    "Adres": item.get("address") or "Onbekend",
                    "Stad": item.get("city") or "Onbekend",
                    "Vraagprijs": _format_currency(item.get("asking_price")),
                }
                for item in top_properties
            ],
            use_container_width=True,
        )
    else:
        st.info("Nog geen scoregegevens beschikbaar.")

    st.markdown("### 5 meest recent geanalyseerde properties")
    recent_properties = stats.get("recent_properties") or []
    if recent_properties:
        st.dataframe(
            [
                {
                    "Datum": item.get("created_at") or "Onbekend",
                    "Score": _safe_score(item.get("investment_score")),
                    "Titel": item.get("title") or "Onbekend",
                    "Adres": item.get("address") or "Onbekend",
                    "Stad": item.get("city") or "Onbekend",
                }
                for item in recent_properties
            ],
            use_container_width=True,
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

    sort_choice = st.selectbox("Sortering", ["Nieuwste", "Hoogste score", "Laagste vraagprijs", "Hoogste vraagprijs"], key="deal_sort")
    sort_map = {
        "Nieuwste": "detected_at_desc",
        "Hoogste score": "score_desc",
        "Laagste vraagprijs": "asking_price_asc",
        "Hoogste vraagprijs": "asking_price_desc",
    }

    candidates = DATABASE_SERVICE.list_deal_candidates(
        limit=500,
        city=None if city_filter == "Alle steden" else city_filter,
        source_id=source_options.get(source_label),
        minimum_score=min_score,
        priority=None if priority_filter == "alle" else priority_filter,
        sort_by=sort_map.get(sort_choice, "detected_at_desc"),
    )

    if not candidates:
        st.info("Geen deal candidates gevonden voor de gekozen filters.")
        _deal_finder_marker("deal_candidates_section_done_empty")
        return

    _render_deal_candidate_cards(candidates)

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
    if len(sys.argv) > 1 and sys.argv[1] == "run":
        raise SystemExit(_run_end_to_end_cli(sys.argv[2:]))
    main()
