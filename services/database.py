from __future__ import annotations

from dataclasses import dataclass
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
