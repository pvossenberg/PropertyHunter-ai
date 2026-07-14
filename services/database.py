from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from typing import Any

from config import SUPABASE_SERVICE_ROLE_KEY, SUPABASE_URL

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
            "raw_payload": raw_payload or {},
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
            "features": snapshot.get("features") or {},
            "content_hash": current_hash,
            "raw_payload": snapshot.get("raw_payload") or {},
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
