from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from typing import Any

from config import SUPABASE_SERVICE_ROLE_KEY, SUPABASE_URL
from models.property import Property

try:
    from supabase import Client, create_client
except ImportError:  # pragma: no cover - optional dependency in runtime
    Client = Any  # type: ignore[assignment]
    create_client = None


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


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


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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


def _normalize_source_url(url: str | None) -> str:
    if not isinstance(url, str):
        return ""
    trimmed = url.strip().lower()
    while trimmed.endswith("/"):
        trimmed = trimmed[:-1]
    return trimmed


def _hash_snapshot_content(snapshot: dict[str, Any]) -> str:
    payload = {
        "asking_price": snapshot.get("asking_price"),
        "listing_status": snapshot.get("listing_status"),
        "title": snapshot.get("title"),
        "description": snapshot.get("description"),
        "surface_m2": snapshot.get("surface_m2"),
        "features": snapshot.get("features") or {},
    }
    normalized = json.dumps(payload, sort_keys=True, ensure_ascii=True, default=str)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


@dataclass
class DatabaseService:
    url: str = ""
    key: str = ""

    def __post_init__(self) -> None:
        self.url = (self.url or "").strip()
        self.key = (self.key or "").strip()
        self._enabled = bool(self.url and self.key and callable(create_client))
        self._client: Client | None = create_client(self.url, self.key) if self._enabled else None

    @classmethod
    def from_env(cls) -> "DatabaseService":
        return cls(url=SUPABASE_URL, key=SUPABASE_SERVICE_ROLE_KEY)

    @property
    def is_enabled(self) -> bool:
        return bool(self._enabled and self._client is not None)

    def _fetch_rows(
        self,
        table_name: str,
        *,
        limit: int = 100,
        order_column: str = "created_at",
        ascending: bool = False,
        filters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        if not self.is_enabled:
            return []

        client = self._client
        if client is None:
            return []

        try:
            query = client.table(table_name).select("*")
            for key, value in (filters or {}).items():
                query = query.eq(key, value)
            query = query.order(order_column, desc=not ascending).limit(max(1, int(limit)))
            response = query.execute()
            if not response or not response.data:
                return []
            return [item for item in response.data if isinstance(item, dict)]
        except Exception:
            return []

    def _insert_row(self, table_name: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.is_enabled:
            return {}
        client = self._client
        if client is None:
            return {}
        try:
            response = client.table(table_name).insert(payload).execute()
            if response and response.data:
                first = response.data[0]
                return first if isinstance(first, dict) else {}
        except Exception:
            return {}
        return {}

    def _update_row(self, table_name: str, row_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.is_enabled:
            return {}
        client = self._client
        if client is None:
            return {}
        try:
            response = client.table(table_name).update(payload).eq("id", row_id).execute()
            if response and response.data:
                first = response.data[0]
                return first if isinstance(first, dict) else {}
        except Exception:
            return {}
        return {}

    def _upsert_rows(self, table_name: str, payload: dict[str, Any], on_conflict: str) -> dict[str, Any]:
        if not self.is_enabled:
            return {}
        client = self._client
        if client is None:
            return {}
        try:
            response = client.table(table_name).upsert(payload, on_conflict=on_conflict).execute()
            if response and response.data:
                first = response.data[0]
                return first if isinstance(first, dict) else {}
        except Exception:
            return {}
        return {}

    def list_raw_listings(self, limit: int = 5000) -> list[dict[str, Any]]:
        return self._fetch_rows("listings", limit=max(1, int(limit or 5000)))

    def get_listing_snapshots(self, listing_id: str, limit: int = 100) -> list[dict[str, Any]]:
        if not isinstance(listing_id, str) or not listing_id.strip():
            return []
        return self._fetch_rows(
            "listing_snapshots",
            filters={"listing_id": listing_id.strip()},
            limit=max(1, int(limit or 100)),
            order_column="observed_at",
            ascending=True,
        )

    def list_properties(self, limit: int = 100, city: str | None = None) -> list[dict[str, Any]]:
        normalized_limit = max(1, int(limit or 100))
        filters: dict[str, Any] = {}
        if isinstance(city, str) and city.strip():
            filters["city"] = city.strip()

        properties = self._fetch_rows("properties", limit=normalized_limit, filters=filters)
        if not properties:
            return []

        property_ids = [str(item.get("id")) for item in properties if item.get("id")]
        if not property_ids:
            return []

        analyses = self._fetch_rows("analyses", limit=max(normalized_limit * 5, 100))
        latest_by_property: dict[str, dict[str, Any]] = {}
        for analysis in analyses:
            property_id = analysis.get("property_id")
            if not property_id:
                continue
            property_id = str(property_id)
            if property_id in latest_by_property:
                continue
            if property_id in property_ids:
                latest_by_property[property_id] = analysis

        rows: list[dict[str, Any]] = []
        for prop in properties:
            prop_id = str(prop.get("id") or "")
            latest = latest_by_property.get(prop_id, {})
            rows.append(
                {
                    "id": prop_id,
                    "title": prop.get("title"),
                    "address": prop.get("address"),
                    "city": prop.get("city"),
                    "asking_price": _as_float(prop.get("asking_price")),
                    "price_per_m2": _as_float(prop.get("price_per_m2")),
                    "source_url": prop.get("source_url"),
                    "created_at": latest.get("created_at") or prop.get("created_at"),
                    "investment_score": _as_int(latest.get("investment_score")),
                    "analysis_id": latest.get("id"),
                }
            )

        return rows[:normalized_limit]

    def list_analyses(self, limit: int = 100) -> list[dict[str, Any]]:
        normalized_limit = max(1, int(limit or 100))
        return self._fetch_rows("analyses", limit=normalized_limit)

    def get_property_with_latest_analysis(self, property_id: str) -> dict[str, dict[str, Any]]:
        if not isinstance(property_id, str) or not property_id.strip():
            return {
                "property": {},
                "analysis": {},
                "transactions": [],
                "permits": [],
                "energy_labels": [],
            }

        rows = self._fetch_rows("properties", filters={"id": property_id.strip()}, limit=1)
        analyses = self._fetch_rows("analyses", filters={"property_id": property_id.strip()}, limit=1)
        transactions = self._fetch_rows("transactions", filters={"property_id": property_id.strip()}, limit=200)
        permits = self._fetch_rows("permits", filters={"property_id": property_id.strip()}, limit=200)
        energy_labels = self._fetch_rows("energy_labels", filters={"property_id": property_id.strip()}, limit=50)
        return {
            "property": rows[0] if rows else {},
            "analysis": analyses[0] if analyses else {},
            "transactions": transactions,
            "permits": permits,
            "energy_labels": energy_labels,
        }

    def get_dashboard_statistics(self) -> dict[str, Any]:
        properties = self.list_properties(limit=1000)
        analyses = self.list_analyses(limit=1000)

        scores: list[int] = []
        for item in analyses:
            score = _as_int(item.get("investment_score"))
            if score is not None:
                scores.append(score)

        city_counts: dict[str, int] = {}
        for item in properties:
            city = item.get("city")
            city_key = city.strip() if isinstance(city, str) and city.strip() else "Onbekend"
            city_counts[city_key] = city_counts.get(city_key, 0) + 1

        top_properties = sorted(
            properties,
            key=lambda item: (_as_int(item.get("investment_score")) is not None, _as_int(item.get("investment_score")) or -1),
            reverse=True,
        )[:5]

        recent_properties = sorted(
            properties,
            key=lambda item: str(item.get("created_at") or ""),
            reverse=True,
        )[:5]

        transactions = self._fetch_rows("transactions", limit=2000)
        permits = self._fetch_rows("permits", limit=2000)
        energy_labels = self._fetch_rows("energy_labels", limit=2000)

        return {
            "total_properties": len(properties),
            "total_analyses": len(analyses),
            "total_transactions": len(transactions),
            "total_permits": len(permits),
            "total_energy_labels": len(energy_labels),
            "average_investment_score": round(sum(scores) / len(scores), 2) if scores else 0.0,
            "highest_investment_score": max(scores) if scores else 0,
            "properties_by_city": city_counts,
            "top_properties": top_properties,
            "recent_properties": recent_properties,
        }

    def upsert_listing_source(
        self,
        *,
        name: str,
        source_type: str,
        base_url: str | None,
        is_enabled: bool = False,
        scan_frequency_minutes: int | None = None,
        configuration: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not isinstance(name, str) or not name.strip():
            return {}
        payload = {
            "name": name.strip(),
            "source_type": source_type or "unknown",
            "base_url": base_url,
            "is_enabled": bool(is_enabled),
            "scan_frequency_minutes": scan_frequency_minutes,
            "configuration": configuration or {},
            "updated_at": _utc_now_iso(),
        }
        row = self._upsert_rows("listing_sources", payload, on_conflict="name")
        if row:
            return row
        rows = self._fetch_rows("listing_sources", filters={"name": name.strip()}, limit=1)
        return rows[0] if rows else {}

    def create_scan_run(self, source_id: str | None, status: str = "running", metadata: dict[str, Any] | None = None) -> str | None:
        payload = {
            "source_id": source_id,
            "started_at": _utc_now_iso(),
            "status": status,
            "metadata": metadata or {},
        }
        row = self._insert_row("scan_runs", payload)
        scan_id = row.get("id") if row else None
        return str(scan_id) if scan_id else None

    def upsert_property(self, property_payload: dict[str, Any]) -> dict[str, Any]:
        if not self.is_enabled:
            return {}

        if not isinstance(property_payload, dict):
            return {}

        source_url = str(property_payload.get("source_url") or "").strip()
        if not source_url:
            return {}

        payload = {
            "source_url": source_url,
            "title": property_payload.get("title"),
            "address": property_payload.get("address"),
            "city": property_payload.get("city"),
            "country": property_payload.get("country"),
            "asking_price": property_payload.get("asking_price"),
            "asking_price_status": property_payload.get("asking_price_status") or "unknown",
            "asking_price_text": property_payload.get("asking_price_text"),
            "listed_since": property_payload.get("listed_since"),
            "days_on_market": property_payload.get("days_on_market"),
            "listing_status": property_payload.get("listing_status") or "unknown",
            "original_asking_price": property_payload.get("original_asking_price"),
            "current_asking_price": property_payload.get("current_asking_price"),
            "price_reduction_count": property_payload.get("price_reduction_count") or 0,
            "last_price_reduction_date": property_payload.get("last_price_reduction_date"),
            "total_price_reduction_amount": property_payload.get("total_price_reduction_amount"),
            "total_price_reduction_percentage": property_payload.get("total_price_reduction_percentage"),
            "listing_history_source": property_payload.get("listing_history_source"),
            "listing_history_confidence": property_payload.get("listing_history_confidence") or "unknown",
            "surface_m2": property_payload.get("surface_m2"),
            "price_per_m2": property_payload.get("price_per_m2"),
            "annual_rent": property_payload.get("annual_rent"),
            "property_type": property_payload.get("property_type"),
            "current_use": property_payload.get("current_use"),
            "zoning": property_payload.get("zoning"),
            "description": property_payload.get("description"),
            "raw_extracted_data": _json_safe_value(property_payload.get("raw_extracted_data") or property_payload),
        }

        existing_rows = self._fetch_rows("properties", limit=5000)
        normalized_source_url = _normalize_source_url(source_url)
        for row in existing_rows:
            if normalized_source_url and normalized_source_url == _normalize_source_url(row.get("source_url")):
                row_id = row.get("id")
                if row_id:
                    updated = self._update_row("properties", str(row_id), payload)
                    return updated or row

        return self._insert_row("properties", payload)

    def complete_scan_run(
        self,
        *,
        scan_run_id: str | None,
        status: str,
        items_found: int,
        items_new: int,
        items_changed: int,
        error_message: str | None,
        metadata: dict[str, Any] | None,
    ) -> dict[str, Any]:
        if not scan_run_id:
            return {}
        payload = {
            "completed_at": _utc_now_iso(),
            "status": status,
            "items_found": max(0, int(items_found or 0)),
            "items_new": max(0, int(items_new or 0)),
            "items_changed": max(0, int(items_changed or 0)),
            "error_message": error_message,
            "metadata": metadata or {},
        }
        return self._update_row("scan_runs", scan_run_id, payload)

    def upsert_listing(
        self,
        *,
        listing_id: str | None = None,
        source_id: str | None,
        external_listing_id: str | None,
        source_url: str,
        title: str | None,
        address: str | None,
        city: str | None,
        asking_price: float | None,
        surface_m2: float | None,
        property_type: str | None,
        listing_status: str | None,
        raw_payload: dict[str, Any] | None,
        dedupe_match: Any = None,
    ) -> dict[str, Any]:
        if not isinstance(source_url, str) or not source_url.strip():
            return {}

        now_iso = _utc_now_iso()
        normalized_url = _normalize_source_url(source_url)
        payload = {
            "property_id": getattr(dedupe_match, "matched_property_id", None),
            "source_id": source_id,
            "external_listing_id": external_listing_id,
            "source_url": source_url.strip(),
            "title": title,
            "address": address,
            "city": city,
            "asking_price": asking_price,
            "surface_m2": surface_m2,
            "property_type": property_type,
            "listing_status": listing_status or "active",
            "last_seen_at": now_iso,
            "is_active": (listing_status or "active") == "active",
            "raw_payload": _json_safe_value(raw_payload or {}),
            "updated_at": now_iso,
        }

        if isinstance(listing_id, str) and listing_id.strip():
            updated = self._update_row("listings", listing_id.strip(), payload)
            if updated:
                return updated

        if source_id and external_listing_id:
            row = self._upsert_rows("listings", payload, on_conflict="source_id,external_listing_id")
            if row:
                return row

        existing_rows = self._fetch_rows("listings", limit=5000)
        for row in existing_rows:
            row_url = _normalize_source_url(row.get("source_url"))
            if normalized_url and normalized_url == row_url:
                row_id = row.get("id")
                if row_id:
                    updated = self._update_row("listings", str(row_id), payload)
                    return updated or row

        payload["first_seen_at"] = now_iso
        return self._insert_row("listings", payload)

    def update_listing_history(self, listing_id: str, history_payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(listing_id, str) or not listing_id.strip() or not isinstance(history_payload, dict):
            return {}

        payload = {
            "first_seen_date": history_payload.get("first_seen_date"),
            "latest_seen_date": history_payload.get("latest_seen_date"),
            "days_on_market": history_payload.get("days_on_market"),
            "original_asking_price": history_payload.get("original_asking_price"),
            "current_asking_price": history_payload.get("current_asking_price"),
            "total_price_reduction_amount": history_payload.get("total_price_reduction"),
            "total_price_reduction_percentage": history_payload.get("total_price_reduction_percentage"),
            "price_reduction_count": history_payload.get("number_of_price_changes") or 0,
            "reduction_frequency": history_payload.get("reduction_frequency"),
            "listing_status": history_payload.get("listing_status") or "active",
            "price_history": history_payload.get("price_history") or [],
            "recently_relisted": bool(history_payload.get("recently_relisted", False)),
            "relisted_date": history_payload.get("relisted_date"),
            "listing_history_source": history_payload.get("source") or "listing_snapshots",
            "listing_history_confidence": history_payload.get("confidence") or "high",
            "updated_at": _utc_now_iso(),
        }
        return self._update_row("listings", listing_id.strip(), payload)

    def upsert_property_enrichment_group(self, *, property_id: str, status: str, started_at: str | None = None, completed_at: str | None = None, source: str | None = None, warning_count: int = 0, error_count: int = 0, summary: dict[str, Any] | None = None) -> dict[str, Any]:
        if not isinstance(property_id, str) or not property_id.strip():
            return {}
        payload = {
            "property_id": property_id.strip(),
            "status": status or "pending",
            "started_at": started_at,
            "completed_at": completed_at,
            "source": source,
            "warning_count": max(0, int(warning_count or 0)),
            "error_count": max(0, int(error_count or 0)),
            "summary": summary or {},
        }
        existing = self._fetch_rows("property_enrichment_groups", filters={"property_id": property_id.strip()}, limit=1)
        if existing:
            row_id = existing[0].get("id")
            if row_id:
                return self._update_row("property_enrichment_groups", str(row_id), payload)
        return self._insert_row("property_enrichment_groups", payload)

    def add_property_enrichment(self, *, property_id: str, enrichment_key: str, value: Any, source: str, retrieval_date: str, confidence_score: int, success: bool = True, error_message: str | None = None, raw_payload: dict[str, Any] | None = None) -> dict[str, Any]:
        if not isinstance(property_id, str) or not property_id.strip() or not isinstance(enrichment_key, str) or not enrichment_key.strip():
            return {}
        payload = {
            "property_id": property_id.strip(),
            "enrichment_key": enrichment_key.strip(),
            "value": value,
            "source": source or "unknown",
            "retrieval_date": retrieval_date,
            "confidence_score": max(0, min(100, int(confidence_score or 0))),
            "success": bool(success),
            "error_message": error_message,
            "raw_payload": raw_payload or {},
        }
        return self._insert_row("property_enrichments", payload)

    def batch_upsert_property_enrichments(self, *, property_id: str, enrichments: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not isinstance(property_id, str) or not property_id.strip():
            return []
        inserted: list[dict[str, Any]] = []
        for enrichment in enrichments:
            if not isinstance(enrichment, dict):
                continue
            row = self.add_property_enrichment(
                property_id=property_id.strip(),
                enrichment_key=str(enrichment.get("enrichment_key") or ""),
                value=enrichment.get("value"),
                source=str(enrichment.get("source") or "unknown"),
                retrieval_date=str(enrichment.get("retrieval_date") or _utc_now_iso()),
                confidence_score=int(enrichment.get("confidence_score") or 0),
                success=bool(enrichment.get("success", True)),
                error_message=enrichment.get("error_message"),
                raw_payload=enrichment.get("raw_payload") or {},
            )
            if row:
                inserted.append(row)
        return inserted

    def add_listing_snapshot_if_changed(self, *, listing_id: str, snapshot: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(listing_id, str) or not listing_id.strip():
            return {"changed": False, "change_type": "invalid_listing_id", "snapshot_id": None}

        current_hash = _hash_snapshot_content(snapshot)
        previous = self._fetch_rows("listing_snapshots", filters={"listing_id": listing_id}, limit=1)
        previous_row = previous[0] if previous else {}
        previous_hash = previous_row.get("content_hash") if previous_row else None

        if previous_hash and previous_hash == current_hash:
            return {"changed": False, "change_type": "unchanged", "snapshot_id": previous_row.get("id")}

        change_type = "new_listing"
        if previous_row:
            old_price = _as_float(previous_row.get("asking_price"))
            new_price = _as_float(snapshot.get("asking_price"))
            old_status = str(previous_row.get("listing_status") or "")
            new_status = str(snapshot.get("listing_status") or "")
            old_desc = str(previous_row.get("description") or "")
            new_desc = str(snapshot.get("description") or "")

            if old_status and old_status != "active" and new_status == "active":
                change_type = "relisting_signal"
            elif old_price != new_price:
                change_type = "asking_price_change"
            elif old_status != new_status:
                change_type = "listing_status_change"
            elif old_desc != new_desc:
                change_type = "description_change"
            else:
                change_type = "content_change"

        payload = {
            "listing_id": listing_id,
            "observed_at": _utc_now_iso(),
            "asking_price": snapshot.get("asking_price"),
            "listing_status": snapshot.get("listing_status") or "active",
            "title": snapshot.get("title"),
            "description": snapshot.get("description"),
            "surface_m2": snapshot.get("surface_m2"),
            "features": _json_safe_value(snapshot.get("features") or {}),
            "content_hash": current_hash,
            "raw_payload": _json_safe_value(snapshot.get("raw_payload") or {}),
        }
        inserted = self._insert_row("listing_snapshots", payload)

        self._update_row(
            "listings",
            listing_id,
            {
                "last_seen_at": _utc_now_iso(),
                "listing_status": payload["listing_status"],
                "asking_price": payload["asking_price"],
                "title": payload["title"],
                "surface_m2": payload["surface_m2"],
                "is_active": payload["listing_status"] == "active",
                "updated_at": _utc_now_iso(),
            },
        )

        return {"changed": True, "change_type": change_type, "snapshot_id": inserted.get("id") if inserted else None}

    def create_or_update_deal_candidate(
        self,
        *,
        listing_id: str,
        property_id: str | None,
        investment_score: int | None,
        hidden_value_score: int | None,
        priority: str,
        reasons: list[str] | None,
        review_status: str = "new",
    ) -> dict[str, Any]:
        existing = self._fetch_rows("deal_candidates", filters={"listing_id": listing_id}, limit=1)
        payload = {
            "listing_id": listing_id,
            "property_id": property_id,
            "investment_score": investment_score,
            "hidden_value_score": hidden_value_score,
            "priority": priority,
            "reasons": reasons or [],
            "review_status": review_status,
            "detected_at": _utc_now_iso(),
        }
        if existing:
            row_id = existing[0].get("id")
            if row_id:
                return self._update_row("deal_candidates", str(row_id), payload)
        return self._insert_row("deal_candidates", payload)

    def mark_missing_listings_inactive(
        self,
        *,
        source_id: str | None,
        current_scan_run_id: str | None,
        current_listing_ids: list[str],
    ) -> dict[str, Any]:
        if not isinstance(source_id, str) or not source_id.strip() or not isinstance(current_scan_run_id, str) or not current_scan_run_id.strip():
            return {"marked_inactive": 0, "evaluated": 0}

        current_seen_ids = {str(item).strip() for item in current_listing_ids if str(item).strip()}
        runs = self._fetch_rows("scan_runs", filters={"source_id": source_id.strip()}, limit=20, order_column="started_at")
        completed_runs = [run for run in runs if str(run.get("status") or "").lower() == "completed"]
        current_run = next((run for run in completed_runs if str(run.get("id") or "") == current_scan_run_id.strip()), {})
        previous_run = next((run for run in completed_runs if str(run.get("id") or "") != current_scan_run_id.strip()), {})

        previous_seen_ids = _extract_seen_listing_ids(previous_run.get("metadata"))
        if not previous_seen_ids:
            return {"marked_inactive": 0, "evaluated": 0}

        source_listings = self._fetch_rows("listings", limit=5000, filters={"source_id": source_id.strip()})
        marked_inactive = 0
        evaluated = 0
        for row in source_listings:
            listing_id = str(row.get("id") or "").strip()
            if not listing_id:
                continue
            if listing_id in current_seen_ids:
                continue
            evaluated += 1
            if listing_id not in previous_seen_ids:
                updated = self._update_row(
                    "listings",
                    listing_id,
                    {
                        "is_active": False,
                        "listing_status": "inactive",
                        "updated_at": _utc_now_iso(),
                    },
                )
                if updated:
                    marked_inactive += 1

        return {"marked_inactive": marked_inactive, "evaluated": evaluated}

    def list_deal_candidates(
        self,
        *,
        limit: int = 200,
        city: str | None = None,
        source_id: str | None = None,
        minimum_score: int | None = None,
        priority: str | None = None,
        sort_by: str = "detected_at_desc",
    ) -> list[dict[str, Any]]:
        candidates = self._fetch_rows("deal_candidates", limit=max(1, int(limit or 200)))
        if not candidates:
            return []

        listings = {str(item.get("id")): item for item in self._fetch_rows("listings", limit=5000) if item.get("id")}
        sources = {str(item.get("id")): item for item in self._fetch_rows("listing_sources", limit=1000) if item.get("id")}

        rows: list[dict[str, Any]] = []
        for candidate in candidates:
            listing_id = str(candidate.get("listing_id") or "")
            listing = listings.get(listing_id, {})
            source = sources.get(str(listing.get("source_id") or ""), {})
            hidden_score = _as_int(candidate.get("hidden_value_score"))
            if minimum_score is not None and hidden_score is not None and hidden_score < int(minimum_score):
                continue
            if city and (listing.get("city") or "") != city:
                continue
            if source_id and str(listing.get("source_id") or "") != source_id:
                continue
            if priority and (candidate.get("priority") or "") != priority:
                continue

            rows.append(
                {
                    **candidate,
                    "listing": listing,
                    "source": source,
                    "score": hidden_score,
                }
            )

        if sort_by == "score_desc":
            rows.sort(key=lambda item: _as_int(item.get("score")) or -1, reverse=True)
        elif sort_by == "asking_price_asc":
            rows.sort(key=lambda item: _as_float((item.get("listing") or {}).get("asking_price")) if _as_float((item.get("listing") or {}).get("asking_price")) is not None else float("inf"))
        elif sort_by == "asking_price_desc":
            rows.sort(key=lambda item: _as_float((item.get("listing") or {}).get("asking_price")) or -1, reverse=True)
        else:
            rows.sort(key=lambda item: str(item.get("detected_at") or ""), reverse=True)

        return rows[: max(1, int(limit or 200))]

    def get_listing_detail(self, listing_id: str) -> dict[str, Any]:
        if not isinstance(listing_id, str) or not listing_id.strip():
            return {"listing": {}, "source": {}, "snapshots": [], "latest_snapshot": {}, "candidate": {}}

        listing_rows = self._fetch_rows("listings", filters={"id": listing_id.strip()}, limit=1)
        listing = listing_rows[0] if listing_rows else {}
        source_id = listing.get("source_id") if listing else None
        source_rows = self._fetch_rows("listing_sources", filters={"id": source_id}, limit=1) if source_id else []
        source = source_rows[0] if source_rows else {}
        snapshots = self._fetch_rows("listing_snapshots", filters={"listing_id": listing_id.strip()}, limit=50)
        latest_snapshot = snapshots[0] if snapshots else {}
        candidates = self._fetch_rows("deal_candidates", filters={"listing_id": listing_id.strip()}, limit=1)

        return {
            "listing": listing,
            "source": source,
            "snapshots": snapshots,
            "latest_snapshot": latest_snapshot,
            "candidate": candidates[0] if candidates else {},
        }

    def mark_candidate_reviewed(self, candidate_id: str, review_status: str = "reviewed") -> dict[str, Any]:
        if not isinstance(candidate_id, str) or not candidate_id.strip():
            return {}
        return self._update_row(
            "deal_candidates",
            candidate_id.strip(),
            {
                "review_status": review_status,
                "reviewed_at": _utc_now_iso(),
            },
        )

    def get_source_health(self) -> dict[str, Any]:
        sources = self._fetch_rows("listing_sources", limit=200)
        scan_runs = self._fetch_rows("scan_runs", limit=500)

        latest_scan_by_source: dict[str, dict[str, Any]] = {}
        for run in scan_runs:
            source_id = str(run.get("source_id") or "")
            if not source_id:
                continue
            if source_id not in latest_scan_by_source:
                latest_scan_by_source[source_id] = run

        source_rows: list[dict[str, Any]] = []
        for source in sources:
            source_id = str(source.get("id") or "")
            latest_scan = latest_scan_by_source.get(source_id, {})
            source_rows.append(
                {
                    **source,
                    "latest_scan_status": latest_scan.get("status"),
                    "latest_scan_started_at": latest_scan.get("started_at"),
                    "latest_scan_completed_at": latest_scan.get("completed_at"),
                    "latest_scan_error": latest_scan.get("error_message"),
                }
            )

        return {
            "sources": source_rows,
            "latest_scan_runs": scan_runs[:20],
        }

    def store_analyzed_property(self, source_url: str, analysis: dict[str, Any]) -> str | None:
        if not self.is_enabled:
            return None

        client = self._client
        if client is None:
            return None

        extracted = _as_dict(analysis.get("extracted_data"))

        property_payload = self._build_property_payload(source_url, extracted)
        property_response = client.table("properties").insert(property_payload).execute()
        if not property_response.data:
            return None

        property_id = property_response.data[0].get("id")
        if not property_id:
            return None

        analysis_payload = self._build_analysis_payload(property_id, analysis)
        analysis_response = client.table("analyses").insert(analysis_payload).execute()
        analysis_id = analysis_response.data[0].get("id") if analysis_response.data else None

        transactions_payload = self._build_transactions_payload(property_id, analysis_id, extracted)
        if transactions_payload:
            client.table("transactions").insert(transactions_payload).execute()

        permits_payload = self._build_permits_payload(property_id, analysis_id, extracted)
        if permits_payload:
            client.table("permits").insert(permits_payload).execute()

        energy_label_payload = self._build_energy_label_payload(property_id, extracted)
        if energy_label_payload:
            client.table("energy_labels").update({"is_current": False}).eq("property_id", property_id).eq("is_current", True).execute()
            client.table("energy_labels").insert(energy_label_payload).execute()

        try:
            from services.property_enrichment import PropertyEnrichmentEngine

            enrichment_engine = PropertyEnrichmentEngine()
            enrichment_result = enrichment_engine.enrich(
                Property(
                    source_url=source_url,
                    title=property_payload.get("title"),
                    address=property_payload.get("address"),
                    city=property_payload.get("city"),
                    postal_code=property_payload.get("postal_code"),
                    municipality=property_payload.get("municipality"),
                    asking_price=property_payload.get("asking_price"),
                    surface_m2=property_payload.get("surface_m2"),
                    plot_size_m2=property_payload.get("plot_size_m2"),
                    property_type=property_payload.get("property_type"),
                    construction_year=property_payload.get("construction_year"),
                    energy_label=extracted.get("energy_label"),
                    description=extracted.get("description"),
                    raw_text=extracted.get("raw_text"),
                    listing_id=str(property_id),
                )
            )
            client.table("property_enrichment_groups").upsert(
                {
                    "property_id": property_id,
                    "status": "completed",
                    "started_at": enrichment_result.started_at,
                    "completed_at": enrichment_result.completed_at,
                    "source": source_url,
                    "warning_count": sum(1 for item in enrichment_result.items if not item.success),
                    "error_count": sum(1 for item in enrichment_result.items if not item.success),
                    "summary": {"enrichment_count": len(enrichment_result.items)},
                },
                on_conflict="property_id",
            ).execute()
            if enrichment_result.items:
                client.table("property_enrichments").insert(
                    [
                        {
                            "property_id": property_id,
                            "enrichment_key": item.enrichment_key,
                            "value": item.value,
                            "source": item.source,
                            "retrieval_date": item.retrieval_date,
                            "confidence_score": item.confidence_score,
                            "success": item.success,
                            "error_message": item.error_message,
                            "raw_payload": item.raw_payload,
                        }
                        for item in enrichment_result.items
                    ]
                ).execute()
            property_updates = getattr(enrichment_result, "to_property_updates", lambda: {})()
            if property_updates:
                client.table("properties").update(property_updates).eq("id", property_id).execute()
        except Exception:
            pass

        return str(property_id)

    def _build_property_payload(self, source_url: str, extracted: dict[str, Any]) -> dict[str, Any]:
        return {
            "source_url": source_url or extracted.get("source_url"),
            "title": extracted.get("title"),
            "address": extracted.get("address"),
            "city": extracted.get("city"),
            "country": extracted.get("country"),
            "asking_price": extracted.get("asking_price"),
            "asking_price_status": extracted.get("asking_price_status") or "unknown",
            "asking_price_text": extracted.get("asking_price_text"),
            "postal_code": extracted.get("postal_code"),
            "municipality": extracted.get("municipality"),
            "bag_id": extracted.get("bag_id"),
            "bag_address_id": extracted.get("bag_address_id"),
            "bag_verblijfsobject_id": extracted.get("bag_verblijfsobject_id"),
            "bag_nummeraanduiding_id": extracted.get("bag_nummeraanduiding_id"),
            "bag_pand_id": extracted.get("bag_pand_id"),
            "bag_building_year": extracted.get("bag_building_year"),
            "construction_year_bag": extracted.get("construction_year_bag") or extracted.get("bag_building_year"),
            "bag_usage_purpose": extracted.get("bag_usage_purpose"),
            "usage_purpose": extracted.get("usage_purpose") or extracted.get("bag_usage_purpose"),
            "bag_status": extracted.get("bag_status") or extracted.get("status"),
            "bag_official_floor_area_m2": extracted.get("bag_official_floor_area_m2"),
            "official_floor_area_m2": extracted.get("official_floor_area_m2") or extracted.get("bag_official_floor_area_m2"),
            "bag_coordinates_rd": extracted.get("bag_coordinates_rd"),
            "bag_coordinates_ll": extracted.get("bag_coordinates_ll"),
            "coordinates": extracted.get("coordinates") or {"rd": extracted.get("bag_coordinates_rd"), "ll": extracted.get("bag_coordinates_ll")},
            "bag_postcode": extracted.get("bag_postcode"),
            "bag_municipality": extracted.get("bag_municipality"),
            "bag_retrieval_date": extracted.get("bag_retrieval_date") or extracted.get("retrieval_date"),
            "retrieval_date": extracted.get("retrieval_date") or extracted.get("bag_retrieval_date"),
            "bag_source": extracted.get("bag_source") or extracted.get("source"),
            "source": extracted.get("source") or extracted.get("bag_source"),
            "bag_confidence_score": extracted.get("bag_confidence_score") or extracted.get("confidence_score"),
            "confidence_score": extracted.get("confidence_score") or extracted.get("bag_confidence_score"),
            "bag_quality_flags": extracted.get("bag_quality_flags") or extracted.get("quality_flags") or [],
            "funda_living_area_m2": extracted.get("funda_living_area_m2"),
            "living_area_difference_m2": extracted.get("living_area_difference_m2"),
            "living_area_difference_percentage": extracted.get("living_area_difference_percentage"),
            "calculation_area_m2": extracted.get("calculation_area_m2"),
            "calculation_area_source": extracted.get("calculation_area_source"),
            "asking_price_per_m2": extracted.get("asking_price_per_m2"),
            "woz_value_per_m2": extracted.get("woz_value_per_m2"),
            "woz_object_number": extracted.get("woz_object_number"),
            "latest_woz_value": extracted.get("latest_woz_value"),
            "woz_valuation_year": extracted.get("woz_valuation_year"),
            "woz_historical_values": extracted.get("woz_historical_values") or [],
            "neighborhood_m2_price_average": extracted.get("neighborhood_m2_price_average"),
            "street_m2_price_average": extracted.get("street_m2_price_average"),
            "listed_since": extracted.get("listed_since"),
            "days_on_market": extracted.get("days_on_market"),
            "listing_status": extracted.get("listing_status") or "unknown",
            "original_asking_price": extracted.get("original_asking_price"),
            "current_asking_price": extracted.get("current_asking_price"),
            "price_reduction_count": extracted.get("price_reduction_count") or 0,
            "last_price_reduction_date": extracted.get("last_price_reduction_date"),
            "total_price_reduction_amount": extracted.get("total_price_reduction_amount"),
            "total_price_reduction_percentage": extracted.get("total_price_reduction_percentage"),
            "listing_history_source": extracted.get("listing_history_source"),
            "listing_history_confidence": extracted.get("listing_history_confidence") or "unknown",
            "surface_m2": extracted.get("surface_m2"),
            "price_per_m2": extracted.get("price_per_m2"),
            "annual_rent": extracted.get("annual_rent"),
            "property_type": extracted.get("property_type"),
            "current_use": extracted.get("current_use"),
            "zoning": extracted.get("zoning"),
            "description": extracted.get("description"),
            "raw_extracted_data": extracted,
        }

    def _build_analysis_payload(self, property_id: str, analysis: dict[str, Any]) -> dict[str, Any]:
        return {
            "property_id": property_id,
            "property_summary": analysis.get("property_summary"),
            "investment_score": analysis.get("investment_score"),
            "score_breakdown": _as_dict(analysis.get("score_breakdown")),
            "analysis_confidence_score": analysis.get("analysis_confidence_score"),
            "data_quality_warnings": _as_list(analysis.get("data_quality_warnings")),
            "strengths": _as_list(analysis.get("strengths")),
            "risks": _as_list(analysis.get("risks")),
            "missing_information": _as_list(analysis.get("missing_information")),
            "assumptions": _as_list(analysis.get("assumptions")),
            "recommendation": analysis.get("recommendation"),
            "next_actions": _as_list(analysis.get("next_actions")),
            "raw_analysis": analysis,
        }

    def _build_transactions_payload(self, property_id: str, analysis_id: str | None, extracted: dict[str, Any]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for item in _as_list(extracted.get("previous_transactions")):
            row = _as_dict(item)
            rows.append(
                {
                    "property_id": property_id,
                    "analysis_id": analysis_id,
                    "transaction_date": row.get("transaction_date"),
                    "transaction_type": row.get("transaction_type") or "unknown",
                    "transaction_price": row.get("transaction_price"),
                    "price_status": row.get("price_status") or "unknown",
                    "buyer_type": row.get("buyer_type"),
                    "seller_type": row.get("seller_type"),
                    "source": row.get("source"),
                    "source_url": row.get("source_url"),
                    "confidence": row.get("confidence") or "unknown",
                    "notes": row.get("notes"),
                }
            )
        return rows

    def _build_permits_payload(self, property_id: str, analysis_id: str | None, extracted: dict[str, Any]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []

        for item in _as_list(extracted.get("permits_last_10_years")):
            rows.append(self._permit_row(item, property_id, analysis_id, is_active=False))

        active_keys = set()
        for item in _as_list(extracted.get("active_permits")):
            row = self._permit_row(item, property_id, analysis_id, is_active=True)
            dedupe_key = (
                row.get("reference_number"),
                row.get("application_date"),
                row.get("permit_type"),
                row.get("description"),
            )
            active_keys.add(dedupe_key)
            rows.append(row)

        deduped: list[dict[str, Any]] = []
        seen: set[tuple[Any, Any, Any, Any, Any]] = set()
        for row in rows:
            dedupe_key = (
                row.get("reference_number"),
                row.get("application_date"),
                row.get("permit_type"),
                row.get("description"),
                row.get("is_active"),
            )
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            if not row.get("is_active") and dedupe_key[:-1] in active_keys:
                continue
            deduped.append(row)

        return deduped

    def _permit_row(self, item: Any, property_id: str, analysis_id: str | None, is_active: bool) -> dict[str, Any]:
        row = _as_dict(item)
        return {
            "property_id": property_id,
            "analysis_id": analysis_id,
            "application_date": row.get("application_date"),
            "decision_date": row.get("decision_date"),
            "permit_type": row.get("permit_type"),
            "description": row.get("description"),
            "status": row.get("status") or "unknown",
            "reference_number": row.get("reference_number"),
            "authority": row.get("authority"),
            "source": row.get("source"),
            "source_url": row.get("source_url"),
            "confidence": row.get("confidence") or "unknown",
            "affects_investment_case": bool(row.get("affects_investment_case", False)),
            "investment_relevance": row.get("investment_relevance"),
            "notes": row.get("notes"),
            "is_active": is_active,
        }

    def _build_energy_label_payload(self, property_id: str, extracted: dict[str, Any]) -> dict[str, Any] | None:
        energy_label = extracted.get("energy_label")
        if not isinstance(energy_label, str) or not energy_label.strip():
            return None
        return {
            "property_id": property_id,
            "label": energy_label.strip(),
            "raw_value": energy_label,
            "source": extracted.get("listing_history_source"),
            "is_current": True,
        }


def _extract_seen_listing_ids(metadata: Any) -> set[str]:
    if not isinstance(metadata, dict):
        return set()
    ids: set[str] = set()
    candidates = _as_list(metadata.get("seen_listing_ids"))
    if candidates:
        for item in candidates:
            text = str(item or "").strip()
            if text:
                ids.add(text)
        return ids

    record_results = _as_list(metadata.get("record_results"))
    for item in record_results:
        if not isinstance(item, dict):
            continue
        listing_id = str(item.get("listing_id") or "").strip()
        if listing_id:
            ids.add(listing_id)
    return ids
