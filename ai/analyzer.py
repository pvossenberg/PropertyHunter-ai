from __future__ import annotations

import json
import re
from typing import Any

from openai import OpenAI

from config import OPENAI_API_KEY

MODEL_NAME = "gpt-4.1-mini"

REQUIRED_KEYS = {
    "property_summary",
    "extracted_data",
    "investment_score",
    "score_breakdown",
    "analysis_confidence_score",
    "data_quality_warnings",
    "strengths",
    "risks",
    "missing_information",
    "assumptions",
    "recommendation",
    "next_actions",
}

ASKING_PRICE_STATUSES = {"known", "on_request", "from_price", "range", "auction", "unknown"}
LISTING_STATUSES = {"active", "under_offer", "sold_subject_to_contract", "withdrawn", "auction", "sold", "unknown"}
TRANSACTION_TYPES = {"sale", "auction_sale", "transfer", "merger_or_split", "unknown"}
PERMIT_STATUSES = {"pending", "granted", "rejected", "withdrawn", "revoked", "lapsed", "outside_scope", "unknown"}


def _coerce_optional_price(value: Any) -> float | None:
    if value in (None, "", "unknown"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _extract_price_value(text: str) -> float | None:
    if not isinstance(text, str):
        return None
    match = re.search(r"€\s*([0-9][0-9\.,\s]*)", text)
    if not match:
        return None
    cleaned = match.group(1).replace(".", "").replace(" ", "").replace(",", ".")
    try:
        return float(cleaned)
    except ValueError:
        return None


def _infer_asking_price_fields(property_text: str) -> tuple[float | None, str, str | None]:
    if not isinstance(property_text, str) or not property_text.strip():
        return None, "unknown", None

    normalized = property_text.strip()
    lowered = normalized.lower()

    on_request_patterns = [
        r"\bprijs\s+op\s+aanvraag\b",
        r"\bpoa\b",
        r"\bop\s+aanvraag\b",
        r"\bprijs\s+te\s+bespreken\b",
        r"\bn\.v\.t\.?\b",
    ]
    if any(re.search(pattern, lowered) for pattern in on_request_patterns):
        return None, "on_request", normalized

    if re.search(r"\b(vanaf|starting from)\b", lowered):
        price = _extract_price_value(normalized)
        return (price, "from_price", normalized) if price is not None else (None, "from_price", normalized)

    if re.search(r"\b(veiling|auction)\b", lowered):
        price = _extract_price_value(normalized)
        return (price, "auction", normalized) if price is not None else (None, "auction", normalized)

    if re.search(r"\b(tot|t/m|tot en met)\b", lowered) or " - " in normalized:
        return None, "range", normalized

    price = _extract_price_value(normalized)
    if price is not None:
        return price, "known", normalized

    return None, "unknown", None


PROPERTY_ANALYSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "property_summary": {"type": ["string", "null"]},
        "extracted_data": {
            "type": "object",
            "properties": {
                "source_url": {"type": ["string", "null"]},
                "title": {"type": ["string", "null"]},
                "address": {"type": ["string", "null"]},
                "city": {"type": ["string", "null"]},
                "country": {"type": ["string", "null"]},
                "asking_price": {"type": ["number", "null"]},
                "asking_price_status": {"type": "string", "enum": sorted(ASKING_PRICE_STATUSES)},
                "asking_price_text": {"type": ["string", "null"]},
                "listed_since": {"type": ["string", "null"]},
                "days_on_market": {"type": ["integer", "null"]},
                "listing_status": {"type": "string", "enum": sorted(LISTING_STATUSES)},
                "original_asking_price": {"type": ["number", "null"]},
                "current_asking_price": {"type": ["number", "null"]},
                "price_reduction_count": {"type": "integer"},
                "last_price_reduction_date": {"type": ["string", "null"]},
                "total_price_reduction_amount": {"type": ["number", "null"]},
                "total_price_reduction_percentage": {"type": ["number", "null"]},
                "listing_history_source": {"type": ["string", "null"]},
                "listing_history_confidence": {"type": "string"},
                "previous_transactions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "transaction_date": {"type": ["string", "null"]},
                            "transaction_type": {"type": "string", "enum": sorted(TRANSACTION_TYPES)},
                            "transaction_price": {"type": ["number", "null"]},
                            "price_status": {"type": "string"},
                            "buyer_type": {"type": ["string", "null"]},
                            "seller_type": {"type": ["string", "null"]},
                            "source": {"type": ["string", "null"]},
                            "source_url": {"type": ["string", "null"]},
                            "confidence": {"type": "string"},
                            "notes": {"type": ["string", "null"]},
                        },
                        "required": [
                            "transaction_date",
                            "transaction_type",
                            "transaction_price",
                            "price_status",
                            "buyer_type",
                            "seller_type",
                            "source",
                            "source_url",
                            "confidence",
                            "notes",
                        ],
                        "additionalProperties": False,
                    },
                },
                "permits_last_10_years": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "application_date": {"type": ["string", "null"]},
                            "decision_date": {"type": ["string", "null"]},
                            "permit_type": {"type": ["string", "null"]},
                            "description": {"type": ["string", "null"]},
                            "status": {"type": "string", "enum": sorted(PERMIT_STATUSES)},
                            "reference_number": {"type": ["string", "null"]},
                            "authority": {"type": ["string", "null"]},
                            "source": {"type": ["string", "null"]},
                            "source_url": {"type": ["string", "null"]},
                            "confidence": {"type": "string"},
                            "affects_investment_case": {"type": "boolean"},
                            "investment_relevance": {"type": ["string", "null"]},
                            "notes": {"type": ["string", "null"]},
                        },
                        "required": [
                            "application_date",
                            "decision_date",
                            "permit_type",
                            "description",
                            "status",
                            "reference_number",
                            "authority",
                            "source",
                            "source_url",
                            "confidence",
                            "affects_investment_case",
                            "investment_relevance",
                            "notes",
                        ],
                        "additionalProperties": False,
                    },
                },
                "active_permits": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "application_date": {"type": ["string", "null"]},
                            "decision_date": {"type": ["string", "null"]},
                            "permit_type": {"type": ["string", "null"]},
                            "description": {"type": ["string", "null"]},
                            "status": {"type": "string", "enum": sorted(PERMIT_STATUSES)},
                            "reference_number": {"type": ["string", "null"]},
                            "authority": {"type": ["string", "null"]},
                            "source": {"type": ["string", "null"]},
                            "source_url": {"type": ["string", "null"]},
                            "confidence": {"type": "string"},
                            "affects_investment_case": {"type": "boolean"},
                            "investment_relevance": {"type": ["string", "null"]},
                            "notes": {"type": ["string", "null"]},
                        },
                        "required": [
                            "application_date",
                            "decision_date",
                            "permit_type",
                            "description",
                            "status",
                            "reference_number",
                            "authority",
                            "source",
                            "source_url",
                            "confidence",
                            "affects_investment_case",
                            "investment_relevance",
                            "notes",
                        ],
                        "additionalProperties": False,
                    },
                },
                "surface_m2": {"type": ["number", "null"]},
                "price_per_m2": {"type": ["number", "null"]},
                "annual_rent": {"type": ["number", "null"]},
                "property_type": {"type": ["string", "null"]},
                "current_use": {"type": ["string", "null"]},
                "zoning": {"type": ["string", "null"]},
                "energy_label": {"type": ["string", "null"]},
                "description": {"type": ["string", "null"]},
            },
            "required": [
                "source_url",
                "title",
                "address",
                "city",
                "country",
                "asking_price",
                "asking_price_status",
                "asking_price_text",
                "listed_since",
                "days_on_market",
                "listing_status",
                "original_asking_price",
                "current_asking_price",
                "price_reduction_count",
                "last_price_reduction_date",
                "total_price_reduction_amount",
                "total_price_reduction_percentage",
                "listing_history_source",
                "listing_history_confidence",
                "previous_transactions",
                "permits_last_10_years",
                "active_permits",
                "surface_m2",
                "price_per_m2",
                "annual_rent",
                "property_type",
                "current_use",
                "zoning",
                "energy_label",
                "description",
            ],
            "additionalProperties": False,
        },
        "investment_score": {"type": "integer", "minimum": 0, "maximum": 100},
        "score_breakdown": {
            "type": "object",
            "properties": {
                "location": {"type": "integer", "minimum": 0, "maximum": 100},
                "price": {"type": "integer", "minimum": 0, "maximum": 100},
                "yield": {"type": "integer", "minimum": 0, "maximum": 100},
                "transformation": {"type": "integer", "minimum": 0, "maximum": 100},
                "risk": {"type": "integer", "minimum": 0, "maximum": 100},
                "marketability": {"type": "integer", "minimum": 0, "maximum": 100},
                "negotiation_position": {"type": "integer", "minimum": 0, "maximum": 100},
                "permit_risk": {"type": "integer", "minimum": 0, "maximum": 100},
            },
            "required": ["location", "price", "yield", "transformation", "risk", "marketability", "negotiation_position", "permit_risk"],
            "additionalProperties": False,
        },
        "analysis_confidence_score": {"type": "integer", "minimum": 0, "maximum": 100},
        "data_quality_warnings": {"type": "array", "items": {"type": "string"}},
        "strengths": {"type": "array", "items": {"type": "string"}},
        "risks": {"type": "array", "items": {"type": "string"}},
        "missing_information": {"type": "array", "items": {"type": "string"}},
        "assumptions": {"type": "array", "items": {"type": "string"}},
        "recommendation": {"type": ["string", "null"]},
        "next_actions": {"type": "array", "items": {"type": "string"}},
    },
    "required": list(REQUIRED_KEYS),
    "additionalProperties": False,
}


def _validate_analysis_payload(payload: Any, source_text: str | None = None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("De AI-analyse retourneerde geen geldig JSON-object.")

    missing = sorted(REQUIRED_KEYS.difference(payload.keys()))
    if missing:
        raise ValueError(f"De AI-analyse mist verplichte velden: {', '.join(missing)}")

    if not isinstance(payload.get("investment_score"), int):
        raise ValueError("investment_score moet een geheel getal zijn.")

    score_breakdown = payload.get("score_breakdown")
    if not isinstance(score_breakdown, dict):
        raise ValueError("score_breakdown moet een object zijn.")

    for key in ("location", "price", "yield", "transformation", "risk", "marketability", "negotiation_position", "permit_risk"):
        if not isinstance(score_breakdown.get(key), int):
            raise ValueError(f"score_breakdown.{key} moet een geheel getal zijn.")

    if not isinstance(payload.get("analysis_confidence_score"), int):
        raise ValueError("analysis_confidence_score moet een geheel getal zijn.")

    if not isinstance(payload.get("data_quality_warnings"), list):
        raise ValueError("data_quality_warnings moet een lijst zijn.")

    extracted_data = payload.get("extracted_data")
    if not isinstance(extracted_data, dict):
        extracted_data = {}
        payload["extracted_data"] = extracted_data

    inferred_price, inferred_status, inferred_text = _infer_asking_price_fields(source_text or "")
    extracted_data.setdefault("asking_price", inferred_price)
    extracted_data.setdefault("asking_price_status", inferred_status)
    extracted_data.setdefault("asking_price_text", inferred_text)

    if "asking_price" in extracted_data:
        extracted_data["asking_price"] = _coerce_optional_price(extracted_data.get("asking_price"))
    if "asking_price_status" in extracted_data:
        status = extracted_data.get("asking_price_status")
        extracted_data["asking_price_status"] = status if isinstance(status, str) and status in ASKING_PRICE_STATUSES else "unknown"
    if "asking_price_text" not in extracted_data or extracted_data.get("asking_price_text") in (None, ""):
        extracted_data["asking_price_text"] = inferred_text

    return payload


def analyze_property(property_text: str) -> dict[str, Any]:
    if not property_text or not property_text.strip():
        raise ValueError("Geef tekst van een vastgoedobject op voor analyse.")

    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY is niet ingesteld. Voeg deze toe aan .env.")

    client = OpenAI(api_key=OPENAI_API_KEY)
    prompt = f"""
Je bent PropertyHunter AI, een kritische en zakelijke vastgoedanalist.

Analyseer de volgende vastgoedadvertentie in het Nederlands.

Geef een JSON-object met exact deze sleutels:
- property_summary
- extracted_data
- investment_score
- score_breakdown
- analysis_confidence_score
- data_quality_warnings
- strengths
- risks
- missing_information
- assumptions
- recommendation
- next_actions

Gebruik alleen betrouwbare informatie uit de advertentie. Verzin geen ontbrekende vastgoedfeiten. Gebruik null of "onbekend" voor ontbrekende waarden.

Beoordeel expliciet in property_summary, risks, assumptions en next_actions:
- hoe lang het object te koop staat;
- of een lange verkooptijd op onderhandelingsruimte kan wijzen;
- hoeveel en wanneer de vraagprijs is verlaagd;
- totale prijsdaling in euro en procenten;
- vorige transactiedatum en vorige koopsom;
- verschil tussen vorige koopsom en huidige vraagprijs;
- recente vergunningaanvragen;
- geweigerde of ingetrokken vergunningen;
- lopende vergunningen;
- vergunningen die transformatie, splitsing, optoppen, gebruik of waarde kunnen beinvloeden.

Regels voor historiedata:
- Verzin nooit verkoop- of vergunningenhistorie; gebruik lege lijsten als gegevens ontbreken.
- Vermeld bij listing_history_source, transacties en vergunningen altijd bron en confidence; als onbekend: null bron met confidence "unknown".
- Maak onderscheid tussen feiten (hard uit bron) en aannames (in assumptions).
- Een geweigerde vergunning is niet automatisch fataal, maar wel een belangrijk risicosignaal.
- Een verleende vergunning kan waarde toevoegen, maar benoem controle op geldigheid en overdraagbaarheid.
- Een lopende aanvraag is onzekerheid en hoort onder risico's en vervolgstappen.

Voor score_breakdown gebruik een object met deze sleutels: location, price, yield, transformation, risk, marketability, negotiation_position, permit_risk.
Alle scores moeten gehele getallen van 0 tot 100 zijn.

Belangrijk voor scoring:
- Een lange verkooptijd of prijsverlaging is niet automatisch negatief.
- Deze signalen kunnen zowel risico als onderhandelingsruimte betekenen.
- Verwerk die nuance expliciet in marketability en negotiation_position.

Behandel "prijs op aanvraag" niet als ontbrekende fout. Gebruik expliciet een status voor de prijs: known, on_request, from_price, range, auction of unknown.
Bij on_request of onbekende prijs mag je rendement, prijs per m² en maximale biedprijs niet betrouwbaar berekenen. Benoem dat expliciet in de analyse en in de vervolgstappen.
Als de status on_request is, voeg in next_actions expliciet toe: "Vraagprijs opvragen bij makelaar".

Vastgoedadvertentie:
{property_text}
"""

    try:
        response = client.responses.create(
            model=MODEL_NAME,
            input=prompt,
            text={
                "format": {
                    "type": "json_schema",
                    "name": "property_analysis",
                    "strict": True,
                    "schema": PROPERTY_ANALYSIS_SCHEMA,
                }
            },
        )
    except Exception as exc:
        raise RuntimeError(f"OpenAI-analyse kon niet worden voltooid: {exc}") from exc

    raw_text = getattr(response, "output_text", None)
    if not raw_text:
        raise RuntimeError("OpenAI heeft geen analyse teruggegeven.")

    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise RuntimeError("OpenAI heeft geen geldig JSON teruggegeven.") from exc

    return _validate_analysis_payload(payload, property_text)
