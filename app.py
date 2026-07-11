from __future__ import annotations

import streamlit as st
import requests

from ai.analyzer import analyze_property
from models.permit import PermitRecord
from models.property import Property
from models.transaction import PropertyTransaction
from scrapers.generic import fetch_page_text
from services.calculations import calculate_days_on_market, calculate_price_change_since_last_transaction, calculate_price_per_m2, calculate_price_reduction


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


def main():
    st.set_page_config(page_title="PropertyHunter AI", page_icon="🏠", layout="centered")
    st.title("PropertyHunter AI")

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
                        property_text = fetch_page_text(url)
                        if len(property_text.split()) < 40:
                            st.warning("De opgehaalde tekst is erg kort. Plaats de advertentietekst handmatig als de website weinig inhoud toont.")
                        analysis = analyze_property(property_text)
                    except ValueError as error:
                        st.error(str(error))
                    except requests.RequestException as error:
                        st.error(f"De website kon niet worden opgehaald: {error}")
                    except RuntimeError as error:
                        st.error(str(error))
                    except Exception as error:
                        st.error(f"Er ging iets mis tijdens de analyse: {error}")
                    else:
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
                        _render_analysis_result("", analysis)


if __name__ == "__main__":
    main()
