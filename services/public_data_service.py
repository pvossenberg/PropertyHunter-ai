from __future__ import annotations

import asyncio
import copy
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

import requests

from models.property import Property

PDOK_LOCATIESERVER_FREE_URL = "https://api.pdok.nl/bzk/locatieserver/search/v3_1/free"
WOZ_SERVICE_BASE_URL = "https://api.kadaster.nl/lvwoz/wozwaardeloket-api/v1"
BAG_WFS_BASE_URL = "https://service.pdok.nl/lv/bag/wfs/v2_0?service=WFS&version=2.0.0"


_CACHE: dict[str, dict[str, Any]] = {}
_LOCKS: dict[str, threading.Lock] = {}
_CACHE_LOCK = threading.Lock()


def _safe_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _normalize_postcode(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    match = re.search(r"(\d{4})\s*([A-Za-z]{2})", value)
    if not match:
        return None
    return f"{match.group(1)}{match.group(2).upper()}"


def _normalize_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.strip().lower().split())


def _normalize_identifier(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    digits = "".join(character for character in text if character.isdigit())
    if digits:
        return digits.lstrip("0") or "0"
    return text.lower()


def _parse_rd_point(value: str | None) -> dict[str, float] | None:
    if not isinstance(value, str) or "POINT(" not in value:
        return None
    try:
        payload = value.strip().removeprefix("POINT(").removesuffix(")")
        x_str, y_str = payload.split()
        return {"x": round(float(x_str), 3), "y": round(float(y_str), 3)}
    except Exception:
        return None


def _parse_ll_point(value: str | None) -> dict[str, float] | None:
    if not isinstance(value, str) or "POINT(" not in value:
        return None
    try:
        payload = value.strip().removeprefix("POINT(").removesuffix(")")
        lon_str, lat_str = payload.split()
        return {"longitude": round(float(lon_str), 6), "latitude": round(float(lat_str), 6)}
    except Exception:
        return None


@dataclass(frozen=True)
class PublicDatasetResult:
    value: Any
    source: str
    retrieval_date: str
    confidence_score: int
    raw_payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "source": self.source,
            "retrieval_date": self.retrieval_date,
            "confidence_score": self.confidence_score,
            "raw_payload": self.raw_payload,
        }


class DutchPublicDataService:
    def __init__(self, *, timeout_seconds: float = 12.0, requester: Callable[..., requests.Response] | None = None) -> None:
        self.timeout_seconds = float(timeout_seconds)
        self._requester = requester or requests.request
        self._session = requests.Session()

    async def fetch_bag_snapshot(self, property_obj: Property) -> dict[str, Any]:
        return await self._to_thread(self._fetch_bag_snapshot_sync, property_obj)

    async def fetch_woz_snapshot(self, property_obj: Property) -> dict[str, Any]:
        return await self._to_thread(self._fetch_woz_snapshot_sync, property_obj)

    def _fetch_bag_snapshot_sync(self, property_obj: Property) -> dict[str, Any]:
        cache_key = self._cache_key("bag", property_obj)

        def _load() -> dict[str, Any]:
            woz_snapshot = self._fetch_woz_snapshot_sync(property_obj)
            address_docs = self._resolve_address_docs(property_obj)
            selected_match = self._select_best_bag_match(property_obj, address_docs, woz_snapshot)
            address_doc = selected_match.get("address_doc") or {}
            official = selected_match.get("official") or {}
            nummeraanduiding_id = selected_match.get("nummeraanduiding_id")
            vbo_id = selected_match.get("vbo_id")

            usage_purpose = self._normalize_usage(official.get("gebruiksdoel"))
            confidence_score = self._bag_confidence_score(
                property_obj=property_obj,
                address_doc=address_doc,
                official=official,
                all_docs=address_docs,
            )
            quality_flags = self._bag_quality_flags(
                address_doc=address_doc,
                official=official,
                all_docs=address_docs,
                confidence_score=confidence_score,
            )

            result = {
                "bag_address_id": str(nummeraanduiding_id or "") or None,
                "bag_verblijfsobject_id": str(vbo_id or "") or None,
                "bag_id": str(vbo_id) if vbo_id else None,
                "bag_nummeraanduiding_id": str(nummeraanduiding_id) if nummeraanduiding_id else None,
                "bag_pand_id": str(official.get("pandidentificatie") or woz_snapshot.get("bag_pand_id") or "") or None,
                "bag_building_year": _safe_int(official.get("bouwjaar") or woz_snapshot.get("bag_building_year")),
                "construction_year_bag": _safe_int(official.get("bouwjaar") or woz_snapshot.get("bag_building_year")),
                "bag_usage_purpose": usage_purpose,
                "usage_purpose": usage_purpose,
                "status": str(official.get("status") or official.get("pandstatus") or "").strip() or None,
                "bag_official_floor_area_m2": _safe_float(official.get("oppervlakte") or woz_snapshot.get("bag_official_floor_area_m2")),
                "official_floor_area_m2": _safe_float(official.get("oppervlakte") or woz_snapshot.get("bag_official_floor_area_m2")),
                "bag_coordinates_rd": _parse_rd_point(address_doc.get("centroide_rd")),
                "bag_coordinates_ll": _parse_ll_point(address_doc.get("centroide_ll")),
                "coordinates": {
                    "rd": _parse_rd_point(address_doc.get("centroide_rd")),
                    "ll": _parse_ll_point(address_doc.get("centroide_ll")),
                },
                "bag_postcode": address_doc.get("postcode") or official.get("postcode") or property_obj.postal_code,
                "bag_municipality": address_doc.get("gemeentenaam") or property_obj.municipality or property_obj.city,
                "source": "PDOK BAG WFS + PDOK Locatieserver",
                "retrieval_date": datetime.now(timezone.utc).isoformat(),
                "confidence_score": confidence_score,
                "quality_flags": quality_flags,
                "raw_payload": {
                    "address": address_doc,
                    "address_candidates": address_docs,
                    "selected_match": selected_match,
                    "bag_verblijfsobject": official,
                    "woz_snapshot": woz_snapshot,
                },
            }
            return result

        return self._cached(cache_key, _load)

    def _fetch_woz_snapshot_sync(self, property_obj: Property) -> dict[str, Any]:
        cache_key = self._cache_key("woz", property_obj)

        def _load() -> dict[str, Any]:
            address_doc = self._resolve_address(property_obj)
            nummeraanduiding_candidates = self._nummeraanduiding_candidates(address_doc)
            if not nummeraanduiding_candidates:
                return {
                    "woz_object_number": None,
                    "latest_woz_value": None,
                    "woz_valuation_year": None,
                    "woz_historical_values": [],
                    "bag_id": None,
                    "bag_numberaanduiding_id": None,
                    "bag_pand_id": None,
                    "source": "Kadaster WOZ-waardeloket",
                    "retrieval_date": datetime.now(timezone.utc).isoformat(),
                    "confidence_score": 10,
                    "raw_payload": {"address": address_doc, "tried_nummeraanduiding_ids": []},
                }

            response = None
            selected_nummeraanduiding_id = None
            errors: list[str] = []
            for nummeraanduiding_id in nummeraanduiding_candidates:
                try:
                    response = self._request_json(
                        "GET",
                        f"{WOZ_SERVICE_BASE_URL}/wozwaarde/nummeraanduiding/{nummeraanduiding_id}",
                    )
                    selected_nummeraanduiding_id = nummeraanduiding_id
                    break
                except Exception as error:
                    errors.append(f"{nummeraanduiding_id}: {type(error).__name__}: {error}")

            if response is None:
                return {
                    "woz_object_number": None,
                    "latest_woz_value": None,
                    "woz_valuation_year": None,
                    "woz_historical_values": [],
                    "bag_id": None,
                    "bag_numberaanduiding_id": None,
                    "bag_pand_id": None,
                    "source": "Kadaster WOZ-waardeloket",
                    "retrieval_date": datetime.now(timezone.utc).isoformat(),
                    "confidence_score": 5,
                    "raw_payload": {
                        "address": address_doc,
                        "tried_nummeraanduiding_ids": nummeraanduiding_candidates,
                        "errors": errors,
                    },
                }

            woz_object = response.get("wozObject") or {}
            values = response.get("wozWaarden") or []
            historical_values = []
            for item in values:
                year = self._safe_year(item.get("peildatum"))
                historical_values.append(
                    {
                        "valuation_year": year,
                        "peildatum": item.get("peildatum"),
                        "value": _safe_float(item.get("vastgesteldeWaarde")),
                    }
                )

            latest = values[0] if values else {}
            return {
                "woz_object_number": _safe_int(woz_object.get("wozobjectnummer")),
                "latest_woz_value": _safe_float(latest.get("vastgesteldeWaarde")),
                "woz_valuation_year": self._safe_year(latest.get("peildatum")),
                "woz_historical_values": historical_values,
                "bag_id": str(woz_object.get("adresseerbaarobjectid") or "") or None,
                "bag_numberaanduiding_id": str(woz_object.get("nummeraanduidingid") or selected_nummeraanduiding_id or "") or None,
                "bag_pand_id": None,
                "bag_ground_area_m2": _safe_float(woz_object.get("grondoppervlakte")),
                "address": {
                    "woonplaatsnaam": woz_object.get("woonplaatsnaam"),
                    "openbareruimtenaam": woz_object.get("openbareruimtenaam"),
                    "postcode": woz_object.get("postcode"),
                    "huisnummer": woz_object.get("huisnummer"),
                    "huisletter": woz_object.get("huisletter"),
                },
                "source": "Kadaster WOZ-waardeloket",
                "retrieval_date": datetime.now(timezone.utc).isoformat(),
                "confidence_score": 98 if latest else 65,
                "raw_payload": {
                    "response": response,
                    "address": address_doc,
                    "selected_nummeraanduiding_id": selected_nummeraanduiding_id,
                    "tried_nummeraanduiding_ids": nummeraanduiding_candidates,
                    "errors": errors,
                },
            }
        return self._cached(cache_key, _load)

    def _lookup_bag_verblijfsobject(self, vbo_id: str | int | None) -> dict[str, Any]:
        if vbo_id in (None, ""):
            return {}
        normalized = str(vbo_id).strip()
        if not normalized:
            return {}
        cache_key = f"bag:vbo:{normalized}"

        def _load() -> dict[str, Any]:
            body = f'''<wfs:GetFeature xmlns:wfs="http://www.opengis.net/wfs" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:fes="http://www.opengis.net/fes/2.0" service="WFS" version="2.0.0" outputFormat="application/json">
  <wfs:Query typeNames="verblijfsobject">
    <fes:Filter>
      <fes:PropertyIsEqualTo>
        <fes:ValueReference>identificatie</fes:ValueReference>
        <fes:Literal>{normalized}</fes:Literal>
      </fes:PropertyIsEqualTo>
    </fes:Filter>
  </wfs:Query>
</wfs:GetFeature>'''
            response = self._request_json("POST", BAG_WFS_BASE_URL, data=body, headers={"Content-Type": "application/xml"})
            features = response.get("features") or []
            if not features:
                return {}
            properties = (features[0] or {}).get("properties") or {}
            return {
                "identificatie": properties.get("identificatie"),
                "oppervlakte": properties.get("oppervlakte"),
                "gebruiksdoel": properties.get("gebruiksdoel"),
                "bouwjaar": properties.get("bouwjaar"),
                "pandidentificatie": properties.get("pandidentificatie"),
                "pandstatus": properties.get("pandstatus"),
                "status": properties.get("status"),
                "postcode": properties.get("postcode"),
                "openbare_ruimte": properties.get("openbare_ruimte"),
                "huisnummer": properties.get("huisnummer"),
                "huisletter": properties.get("huisletter"),
                "toevoeging": properties.get("toevoeging"),
                "woonplaats": properties.get("woonplaats"),
                "geometry": (features[0] or {}).get("geometry"),
            }

        return self._cached(cache_key, _load)

    def _resolve_address(self, property_obj: Property) -> dict[str, Any]:
        docs = self._resolve_address_docs(property_obj)
        return self._select_best_address_doc(property_obj, docs)

    def _select_best_bag_match(
        self,
        property_obj: Property,
        docs: list[dict[str, Any]],
        woz_snapshot: dict[str, Any],
    ) -> dict[str, Any]:
        expected = self._parse_address_components(property_obj)
        preferred_nummeraanduiding_id = str(woz_snapshot.get("bag_numberaanduiding_id") or "").strip()
        preferred_vbo_id = str(woz_snapshot.get("bag_id") or woz_snapshot.get("bag_verblijfsobject_id") or "").strip()

        candidates: list[dict[str, Any]] = []
        for doc in docs:
            if not isinstance(doc, dict):
                continue

            doc_nummeraanduiding_id = str(
                doc.get("nummeraanduiding_id")
                or doc.get("nummeraanduidingid")
                or doc.get("identificatie")
                or ""
            ).strip()
            doc_vbo_id = str(
                doc.get("adresseerbaarobjectid")
                or doc.get("adresseerbaarobject_id")
                or doc.get("identificatie")
                or ""
            ).strip()
            nummeraanduiding_id = str(
                doc_nummeraanduiding_id
                or preferred_nummeraanduiding_id
                or ""
            ).strip() or None
            vbo_id = str(
                doc_vbo_id
                or preferred_vbo_id
                or ""
            ).strip() or None
            official = self._lookup_bag_verblijfsobject(vbo_id) if vbo_id else {}
            candidates.append(
                {
                    "address_doc": doc,
                    "nummeraanduiding_id": nummeraanduiding_id,
                    "vbo_id": vbo_id,
                    "official": official,
                    "score": self._bag_candidate_score(
                        property_obj=property_obj,
                        expected=expected,
                        doc=doc,
                        official=official,
                        preferred_nummeraanduiding_id=preferred_nummeraanduiding_id,
                        preferred_vbo_id=preferred_vbo_id,
                    ),
                }
            )

        if candidates:
            return max(candidates, key=lambda item: item.get("score") or 0)

        fallback_doc = self._select_best_address_doc(property_obj, docs)
        nummeraanduiding_id = str(
            fallback_doc.get("nummeraanduiding_id")
            or fallback_doc.get("nummeraanduidingid")
            or preferred_nummeraanduiding_id
            or fallback_doc.get("identificatie")
            or ""
        ).strip() or None
        vbo_id = str(
            fallback_doc.get("adresseerbaarobjectid")
            or fallback_doc.get("adresseerbaarobject_id")
            or preferred_vbo_id
            or fallback_doc.get("identificatie")
            or ""
        ).strip() or None
        return {
            "address_doc": fallback_doc,
            "nummeraanduiding_id": nummeraanduiding_id,
            "vbo_id": vbo_id,
            "official": self._lookup_bag_verblijfsobject(vbo_id) if vbo_id else {},
            "score": 0,
        }

    def _resolve_address_docs(self, property_obj: Property) -> list[dict[str, Any]]:
        cache_key = f"loc:{self._cache_key_suffix(property_obj)}"

        def _load() -> dict[str, Any]:
            query_parts = [property_obj.address, property_obj.postal_code, property_obj.municipality or property_obj.city]
            query = " ".join(part for part in query_parts if isinstance(part, str) and part.strip())
            response = self._request_json("GET", PDOK_LOCATIESERVER_FREE_URL, params={"q": query})
            docs = (response.get("response") or {}).get("docs") or []
            normalized_docs = [dict(doc) for doc in docs if isinstance(doc, dict)]
            return {"docs": normalized_docs}

        payload = self._cached(cache_key, _load)
        return payload.get("docs") if isinstance(payload.get("docs"), list) else []

    def _select_best_address_doc(self, property_obj: Property, docs: list[dict[str, Any]]) -> dict[str, Any]:
        if not docs:
            return {}
        expected = self._parse_address_components(property_obj)
        expected_city = _normalize_text(property_obj.municipality or property_obj.city)
        expected_street = _normalize_text(expected.get("street_name") or "")

        def score(doc: dict[str, Any]) -> tuple[int, float]:
            points = 0
            if str(doc.get("type") or "").strip().lower() == "adres":
                points += 40

            if expected["postcode"]:
                doc_postcode = str(doc.get("postcode") or "").replace(" ", "").upper()
                if doc_postcode == expected["postcode"]:
                    points += 30

            if expected["house_number"] is not None:
                if _safe_int(doc.get("huisnummer")) == expected["house_number"]:
                    points += 15

            if expected["house_letter"]:
                if str(doc.get("huisletter") or "").strip().upper() == expected["house_letter"]:
                    points += 8

            if expected["house_number_addition"]:
                if str(doc.get("huisnummertoevoeging") or "").strip().upper() == expected["house_number_addition"]:
                    points += 8

            if expected_street:
                doc_street = _normalize_text(doc.get("straatnaam") or doc.get("openbareruimtenaam"))
                if doc_street and doc_street == expected_street:
                    points += 14

            doc_purpose = self._normalize_usage(doc.get("gebruiksdoel") or doc.get("gebruiksdoelen"))
            if doc_purpose and "woon" in doc_purpose.lower():
                points += 8

            if expected_city and _normalize_text(doc.get("woonplaatsnaam")) == expected_city:
                points += 8

            relevance = _safe_float(doc.get("score")) or 0.0
            return points, relevance

        best = max((doc for doc in docs if isinstance(doc, dict)), key=score, default={})
        return best if isinstance(best, dict) else {}

    def _parse_address_components(self, property_obj: Property) -> dict[str, Any]:
        address = str(property_obj.address or "")
        postal_code_text = str(property_obj.postal_code or "")

        postcode_candidate = postal_code_text or address
        normalized_postcode = _normalize_postcode(postcode_candidate)

        house_number = None
        house_letter = ""
        house_number_addition = ""
        house_match = re.search(r"\b(\d{1,5})(?:\s*[-/]?\s*([A-Za-z]))?(?:\s*[-/]?\s*([A-Za-z0-9]{1,6}))?\b", address)
        if house_match:
            house_number = _safe_int(house_match.group(1))
            house_letter = str(house_match.group(2) or "").strip().upper()
            house_number_addition = str(house_match.group(3) or "").strip().upper()
            if house_number_addition and house_number_addition == house_letter:
                house_number_addition = ""

        street_name = ""
        if house_match:
            street_name = address[: house_match.start()].strip(" ,")
        if not street_name and isinstance(property_obj.address, str):
            street_name = property_obj.address.strip()

        return {
            "postcode": normalized_postcode,
            "house_number": house_number,
            "house_letter": house_letter,
            "house_number_addition": house_number_addition,
            "street_name": street_name,
        }

    def _bag_candidate_score(
        self,
        *,
        property_obj: Property,
        expected: dict[str, Any],
        doc: dict[str, Any],
        official: dict[str, Any],
        preferred_nummeraanduiding_id: str,
        preferred_vbo_id: str,
    ) -> int:
        score = 0

        if str(doc.get("type") or "").strip().lower() == "adres":
            score += 35

        expected_postcode = expected.get("postcode")
        doc_postcode = _normalize_postcode(doc.get("postcode"))
        if expected_postcode and doc_postcode == expected_postcode:
            score += 20

        expected_number = expected.get("house_number")
        if expected_number is not None and _safe_int(doc.get("huisnummer")) == expected_number:
            score += 15

        expected_letter = str(expected.get("house_letter") or "").upper()
        doc_letter = str(doc.get("huisletter") or official.get("huisletter") or "").strip().upper()
        if expected_letter:
            if doc_letter == expected_letter:
                score += 14
            elif doc_letter:
                score -= 10

        expected_addition = str(expected.get("house_number_addition") or "").upper()
        doc_addition = str(doc.get("huisnummertoevoeging") or official.get("toevoeging") or "").strip().upper()
        if expected_addition:
            if doc_addition == expected_addition:
                score += 14
            elif doc_addition:
                score -= 10

        expected_street = _normalize_text(expected.get("street_name"))
        doc_street = _normalize_text(doc.get("straatnaam") or doc.get("openbareruimtenaam") or official.get("openbare_ruimte"))
        if expected_street and doc_street == expected_street:
            score += 10

        expected_city = _normalize_text(property_obj.municipality or property_obj.city)
        doc_city = _normalize_text(doc.get("woonplaatsnaam") or official.get("woonplaats"))
        if expected_city and doc_city == expected_city:
            score += 8

        usage = self._normalize_usage(official.get("gebruiksdoel") or doc.get("gebruiksdoel") or doc.get("gebruiksdoelen"))
        if usage:
            if "woon" in usage.lower():
                score += 22
            else:
                score -= 12

        area = _safe_float(official.get("oppervlakte"))
        if area not in (None, 0):
            score += 8

        nummeraanduiding_id = str(doc.get("nummeraanduiding_id") or doc.get("nummeraanduidingid") or "").strip()
        vbo_id = str(doc.get("adresseerbaarobjectid") or doc.get("adresseerbaarobject_id") or "").strip()
        if preferred_nummeraanduiding_id and _normalize_identifier(nummeraanduiding_id) == _normalize_identifier(preferred_nummeraanduiding_id):
            score += 10
        if preferred_vbo_id and _normalize_identifier(vbo_id) == _normalize_identifier(preferred_vbo_id):
            score += 12

        relevance = _safe_float(doc.get("score"))
        if relevance is not None:
            score += int(round(relevance))

        return score

    def _bag_confidence_score(
        self,
        *,
        property_obj: Property,
        address_doc: dict[str, Any],
        official: dict[str, Any],
        all_docs: list[dict[str, Any]],
    ) -> int:
        score = 0
        expected = self._parse_address_components(property_obj)

        if address_doc:
            score += 30

        if official:
            score += 30

        expected_postcode = expected.get("postcode")
        if expected_postcode:
            doc_postcode = _normalize_postcode(address_doc.get("postcode"))
            if doc_postcode == expected_postcode:
                score += 15

        expected_number = expected.get("house_number")
        if expected_number is not None and _safe_int(address_doc.get("huisnummer")) == expected_number:
            score += 10

        expected_letter = str(expected.get("house_letter") or "").upper()
        if expected_letter and str(address_doc.get("huisletter") or official.get("huisletter") or "").strip().upper() == expected_letter:
            score += 5

        expected_addition = str(expected.get("house_number_addition") or "").upper()
        if expected_addition and str(address_doc.get("huisnummertoevoeging") or official.get("toevoeging") or "").strip().upper() == expected_addition:
            score += 5

        usage = self._normalize_usage(official.get("gebruiksdoel"))
        if usage and "woon" in usage.lower():
            score += 10

        if len(all_docs) > 1:
            score -= 5

        return max(0, min(100, int(score)))

    def _bag_quality_flags(
        self,
        *,
        address_doc: dict[str, Any],
        official: dict[str, Any],
        all_docs: list[dict[str, Any]],
        confidence_score: int,
    ) -> list[str]:
        flags: list[str] = []
        if not address_doc:
            flags.append("bag_match_not_found")
        if len(all_docs) > 1:
            flags.append("multiple_possible_bag_matches")

        usage = self._normalize_usage(official.get("gebruiksdoel"))
        if usage and "woon" not in usage.lower():
            flags.append("non_residential_usage_purpose")

        area = _safe_float(official.get("oppervlakte"))
        if area in (None, 0):
            flags.append("missing_official_area")

        if confidence_score < 70:
            flags.append("low_confidence_match")
        return flags

    def _nummeraanduiding_candidates(self, address_doc: dict[str, Any]) -> list[str]:
        if not isinstance(address_doc, dict):
            return []

        candidates: list[str] = []
        for key in ("nummeraanduiding_id", "nummeraanduidingid"):
            value = str(address_doc.get(key) or "").strip()
            if value and value not in candidates:
                candidates.append(value)

        identificatie = str(address_doc.get("identificatie") or "").strip()
        if identificatie:
            split_parts = [part.strip() for part in identificatie.split("-") if part.strip()]
            for part in split_parts:
                if part not in candidates:
                    candidates.append(part)

        # Nummeraanduiding identifiers in BAG are numeric; prefer those likely to be nummeraanduiding over VBO ids.
        numeric_candidates = [item for item in candidates if item.isdigit()]
        preferred = [item for item in numeric_candidates if item.startswith("0")]
        ordered = preferred + [item for item in numeric_candidates if item not in preferred]
        return ordered or candidates

    def _request_json(self, method: str, url: str, *, params: dict[str, Any] | None = None, data: Any = None, headers: dict[str, str] | None = None) -> dict[str, Any]:
        response = self._requester(
            method.upper(),
            url,
            params=params,
            data=data,
            headers=headers,
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        return response.json()

    def _cached(self, key: str, loader: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        with _CACHE_LOCK:
            cached = _CACHE.get(key)
            if cached is not None:
                return copy.deepcopy(cached)
            lock = _LOCKS.setdefault(key, threading.Lock())

        with lock:
            with _CACHE_LOCK:
                cached = _CACHE.get(key)
                if cached is not None:
                    return copy.deepcopy(cached)
            result = loader()
            with _CACHE_LOCK:
                _CACHE[key] = copy.deepcopy(result)
            return copy.deepcopy(result)

    def _cache_key(self, prefix: str, property_obj: Property) -> str:
        return f"{prefix}:{self._cache_key_suffix(property_obj)}"

    def _cache_key_suffix(self, property_obj: Property) -> str:
        parts = [property_obj.source_url, property_obj.address, property_obj.postal_code, property_obj.municipality or property_obj.city]
        return "|".join((part or "").strip().lower() for part in parts)

    def _normalize_usage(self, value: Any) -> str | None:
        if value in (None, ""):
            return None
        if isinstance(value, list):
            return ", ".join(str(item).strip() for item in value if str(item).strip()) or None
        return str(value).strip() or None

    def _safe_year(self, value: Any) -> int | None:
        if not isinstance(value, str) or len(value) < 4:
            return None
        try:
            return int(value[:4])
        except ValueError:
            return None

    async def _to_thread(self, func: Callable[..., Any], *args: Any) -> Any:
        return await asyncio.to_thread(func, *args)
