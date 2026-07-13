from __future__ import annotations

from datetime import datetime

import streamlit as st
import requests

from ai.analyzer import analyze_property
from models.permit import PermitRecord
from models.property import Property
from models.transaction import PropertyTransaction
from scrapers.base import ScrapeResult
from scrapers.router import scrape_url
from services.calculations import calculate_days_on_market, calculate_price_change_since_last_transaction, calculate_price_per_m2, calculate_price_reduction
from services.database import DatabaseService


DATABASE_SERVICE = DatabaseService.from_env()


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
            st.dataframe(
                [
                    {
                        "Datum": transaction.transaction_date,
                        "Type": transaction.transaction_type,
                        "Prijs": _format_currency(transaction.transaction_price) if transaction.transaction_price is not None else "Onbekend",
                        "Bron": transaction.source or "Onbekend",
                        "Betrouwbaarheid": transaction.confidence,
                    }
                    for transaction in property_data.previous_transactions
                ]
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
            st.dataframe(
                [
                    {
                        "Aanvraagdatum": permit.application_date,
                        "Type": permit.permit_type or "Onbekend",
                        "Omschrijving": permit.description or "Onbekend",
                        "Status": _permit_status_label(permit.status),
                        "Besluitdatum": permit.decision_date,
                        "Instantie": permit.authority or "Onbekend",
                        "Relevantie": permit.investment_relevance or "Onbekend",
                        "Bron": permit.source or "Onbekend",
                        "Bronlink": permit.source_url or "",
                    }
                    for permit in property_data.permits_last_10_years
                ]
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

    st.dataframe(
        [
            {
                "Investment score": _safe_score(item.get("investment_score")),
                "Titel": item.get("title") or "Onbekend",
                "Adres": item.get("address") or "Onbekend",
                "Stad": item.get("city") or "Onbekend",
                "Vraagprijs": _format_currency(item.get("asking_price")),
                "Prijs per m²": _format_number(item.get("price_per_m2")),
                "Aangemaakt": item.get("created_at") or "Onbekend",
                "Bron": item.get("source_url") or "",
            }
            for item in rows
        ],
        use_container_width=True,
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


def main():
    st.set_page_config(page_title="PropertyHunter AI", page_icon="🏠", layout="centered")
    st.title("PropertyHunter AI")
    with st.sidebar:
        st.markdown("## Navigatie")
        page = st.radio("Kies een onderdeel", ["Nieuwe analyse", "Mijn analyses", "Dashboard"], index=0)

    if page == "Nieuwe analyse":
        _render_new_analysis_page()
    elif page == "Mijn analyses":
        _render_my_analyses_page()
    else:
        _render_dashboard_page()


if __name__ == "__main__":
    main()
