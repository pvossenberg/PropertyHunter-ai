import unittest
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

from ai.analyzer import PROPERTY_ANALYSIS_SCHEMA, REQUIRED_KEYS, _infer_asking_price_fields, analyze_property, _validate_analysis_payload
from app import (
    _build_investment_intelligence,
    _format_currency,
    _format_number,
    _investment_intelligence_rating,
    _label_score,
    _run_source_scan,
    _render_analysis_result,
)
from deal_finder.models import NormalizedListing
from deal_finder.sources.base import SourceRecordResult
from models.investment_profile import InvestmentProfile
from models.permit import PermitRecord
from models.property import Property
from models.transaction import PropertyTransaction
from scrapers.generic import fetch_page_text
from services.data_provenance import DataProvenance
from services.calculations import calculate_days_on_market, calculate_discount_percentage, calculate_gross_yield, calculate_price_change_since_last_transaction, calculate_price_per_m2, calculate_price_reduction


class PropertyHunterTests(unittest.TestCase):
    def test_run_source_scan_writes_json_and_counts_results(self):
        class FakeDatabaseService:
            def __init__(self):
                self.is_enabled = True
                self.saved_payloads = []

            def upsert_property(self, property_payload):
                self.saved_payloads.append(property_payload)
                return {"id": f"property-{len(self.saved_payloads)}", **property_payload}

        class FakeAdapter:
            source_name = "funda.nl"
            default_start_url = "https://www.funda.nl/zoeken/koop"

            def validate_configuration(self, configuration):
                return True, []

            def load_and_normalize_listings(self, configuration):
                listing = NormalizedListing(
                    source_name=self.source_name,
                    external_listing_id="f-1",
                    source_url="https://www.funda.nl/detail/koop/amsterdam/object-1/11111111/",
                    title="Test listing",
                    city="Amsterdam",
                    asking_price=450000.0,
                    surface_m2=90.0,
                    listing_status="active",
                    raw_payload={"source_url": "https://www.funda.nl/detail/koop/amsterdam/object-1/11111111/"},
                )
                return [
                    SourceRecordResult(record_index=1, success=True, listing=listing, payload=dict(listing.raw_payload)),
                    SourceRecordResult(record_index=2, success=False, listing=None, error="ValueError: malformed listing", payload={"source_url": "https://www.funda.nl/detail/koop/amsterdam/object-2/22222222/"}),
                ]

            def get_last_fetch_stats(self):
                return {"listings_found": 2, "listings_imported": 1, "duplicates_skipped": 0, "failed_listings": 1}

            def to_property_model(self, payload):
                return Property(
                    source_url=payload["source_url"],
                    title="Test listing",
                    city="Amsterdam",
                    asking_price=450000.0,
                    surface_m2=90.0,
                    listing_status="active",
                )

        class FakeRegistry:
            def __init__(self, adapter):
                self.adapter = adapter

            def resolve(self, source_name):
                return self.adapter if source_name == "funda" else None

        class FakeOrchestrator:
            def __init__(self, adapter):
                self.source_registry = FakeRegistry(adapter)

        database_service = FakeDatabaseService()
        orchestrator = FakeOrchestrator(FakeAdapter())

        with TemporaryDirectory() as temp_dir:
            result = _run_source_scan(
                "funda",
                orchestrator=orchestrator,
                database_service=database_service,
                output_dir=Path(temp_dir),
                max_pages=1,
                timeout_seconds=1.0,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["listings_found"], 2)
        self.assertEqual(result["listings_imported"], 1)
        self.assertEqual(result["fully_imported"], 0)
        self.assertEqual(result["partially_imported"], 1)
        self.assertEqual(result["listings_failed"], 1)
        self.assertGreaterEqual(result["average_import_time_seconds"], 0.0)
        self.assertEqual(len(database_service.saved_payloads), 1)
        self.assertTrue(result["output_path"].endswith(".json"))

    def test_fetch_page_text_rejects_non_http_urls(self):
        with self.assertRaises(ValueError):
            fetch_page_text("ftp://example.com")

    def test_analyze_property_requires_text(self):
        with self.assertRaises(ValueError):
            analyze_property("   ")

    def test_calculations_handle_missing_values(self):
        self.assertIsNone(calculate_price_per_m2(100000, 0))
        self.assertIsNone(calculate_gross_yield(5000, 0))
        self.assertIsNone(calculate_discount_percentage(0, 200000))
        self.assertIsNone(calculate_price_per_m2(100000, 100, "on_request"))
        self.assertIsNone(calculate_gross_yield(5000, 100000, "on_request"))

    def test_schema_validation_accepts_expected_keys(self):
        payload = {
            "property_summary": "ok",
            "extracted_data": {},
            "investment_score": 70,
            "score_breakdown": {"location": 80, "price": 70, "yield": 60, "transformation": 50, "risk": 40, "marketability": 70, "negotiation_position": 60, "permit_risk": 50},
            "analysis_confidence_score": 72,
            "data_quality_warnings": ["Prijsgegevens zijn beperkt"],
            "strengths": ["goed"],
            "risks": ["risico"],
            "missing_information": ["prijs"],
            "assumptions": ["aannames"],
            "recommendation": "nader onderzoeken",
            "next_actions": ["check"],
        }
        validated = _validate_analysis_payload(payload)
        self.assertEqual(validated["investment_score"], 70)
        self.assertEqual(set(REQUIRED_KEYS), set(payload.keys()))

    def test_models_import(self):
        property_obj = Property(source_url="https://example.com")
        profile = InvestmentProfile()
        self.assertEqual(property_obj.source_url, "https://example.com")
        self.assertEqual(profile.name, "Standaard profiel")

    def test_calculations_for_listing_history(self):
        self.assertEqual(calculate_days_on_market("2024-01-01", "2024-01-10"), 9)
        self.assertIsNone(calculate_days_on_market(None))
        self.assertEqual(calculate_price_reduction(500000, 450000), {"amount": 50000.0, "percentage": 10.0})
        self.assertEqual(calculate_price_change_since_last_transaction(475000, 500000), {"amount": -25000.0, "percentage": -5.0})

    def test_calculate_days_on_market_returns_none_for_invalid_order(self):
        self.assertIsNone(calculate_days_on_market("2025-01-10", "2025-01-01"))

    def test_calculate_price_reduction_multiple_steps_final_totals(self):
        self.assertEqual(calculate_price_reduction(525000, 450000), {"amount": 75000.0, "percentage": 14.29})

    def test_on_request_price_never_uses_fictive_zero(self):
        price, status, _ = _infer_asking_price_fields("Prijs op aanvraag")
        self.assertIsNone(price)
        self.assertEqual(status, "on_request")

    def test_infer_asking_price_fields_handles_common_price_variants(self):
        price, status, text = _infer_asking_price_fields("Prijs op aanvraag")
        self.assertIsNone(price)
        self.assertEqual(status, "on_request")
        self.assertEqual(text, "Prijs op aanvraag")

        price, status, text = _infer_asking_price_fields("POA")
        self.assertIsNone(price)
        self.assertEqual(status, "on_request")

        price, status, text = _infer_asking_price_fields("Vraagprijs € 450.000")
        self.assertEqual(price, 450000.0)
        self.assertEqual(status, "known")

        price, status, text = _infer_asking_price_fields("Vanaf € 350.000")
        self.assertEqual(price, 350000.0)
        self.assertEqual(status, "from_price")

        price, status, text = _infer_asking_price_fields("Geen prijs vermeld")
        self.assertIsNone(price)
        self.assertEqual(status, "unknown")

    def test_schema_requires_exact_property_keys_for_each_object(self):
        def assert_schema(node):
            if not isinstance(node, dict):
                return
            if node.get("type") == "object":
                properties = node.get("properties")
                required = node.get("required")
                self.assertIsInstance(properties, dict)
                self.assertIsInstance(required, list)
                self.assertEqual(set(required), set(properties.keys()))
                self.assertIs(node.get("additionalProperties"), False)
                for child in properties.values():
                    if isinstance(child, dict):
                        assert_schema(child)

        assert_schema(PROPERTY_ANALYSIS_SCHEMA)

    def test_format_currency_handles_missing_and_valid_values(self):
        self.assertEqual(_format_currency(None), "Onbekend")
        self.assertEqual(_format_currency(1450000), "€ 1.450.000")
        self.assertEqual(_format_currency("1450000"), "€ 1.450.000")
        self.assertEqual(_format_currency("abc"), "Onbekend")
        self.assertEqual(_format_currency(1775000), "€ 1.775.000")
        self.assertNotIn("\n", _format_currency(1775000))

    def test_format_number_handles_missing_and_valid_values(self):
        self.assertEqual(_format_number(4250), "4.250")
        self.assertEqual(_format_number(None), "Onbekend")
        self.assertEqual(_format_number("abc"), "Onbekend")

    def test_label_score_returns_expected_dutch_labels(self):
        self.assertEqual(_label_score("location"), "Locatie")
        self.assertEqual(_label_score("yield"), "Rendement")
        self.assertEqual(_label_score("unknown"), "Unknown")

    def test_investment_intelligence_rating_scale(self):
        self.assertEqual(_investment_intelligence_rating(90), "A+")
        self.assertEqual(_investment_intelligence_rating(80), "A")
        self.assertEqual(_investment_intelligence_rating(70), "B")
        self.assertEqual(_investment_intelligence_rating(55), "C")
        self.assertEqual(_investment_intelligence_rating(30), "D")

    def test_investment_intelligence_builds_six_categories_and_score_range(self):
        intelligence = _build_investment_intelligence(
            city="Amsterdam",
            surface_m2=95,
            gross_yield_value=7.2,
            discount_vs_market_value=8.5,
            difference_percentage=2.0,
            recommendation="Orange",
        )
        self.assertEqual(len(intelligence["categories"]), 6)
        self.assertIn("overall_score", intelligence)
        self.assertIn("rating", intelligence)
        self.assertGreaterEqual(intelligence["overall_score"], 0)
        self.assertLessEqual(intelligence["overall_score"], 100)
        self.assertIn(intelligence["rating"], {"A+", "A", "B", "C", "D"})

        category_names = [item["name"] for item in intelligence["categories"]]
        self.assertEqual(
            category_names,
            [
                "Location",
                "Valuation",
                "Rental potential",
                "Transformation potential",
                "Market momentum",
                "Risk",
            ],
        )

        for item in intelligence["categories"]:
            self.assertGreaterEqual(item["score"], 0)
            self.assertLessEqual(item["score"], 20)
            self.assertTrue(isinstance(item["explanation"], str))
            self.assertTrue(item["explanation"])

    def test_render_analysis_result_handles_empty_analysis_without_exception(self):
        try:
            _render_analysis_result("", {})
        except Exception as exc:  # pragma no cover - defensive test
            self.fail(f"_render_analysis_result raised unexpectedly: {exc}")

    def test_transaction_and_permit_serialization(self):
        transaction = PropertyTransaction(
            transaction_date=date(2021, 6, 14),
            transaction_type="sale",
            transaction_price=500000,
            source="Kadaster",
            source_url="https://example.com/transaction",
            confidence="high",
        )
        permit = PermitRecord(
            application_date=date(2023, 2, 1),
            decision_date=date(2023, 5, 15),
            permit_type="omgevingsvergunning",
            status="granted",
            authority="Gemeente",
            source="Gemeente",
            source_url="https://example.com/permit",
            confidence="medium",
        )
        self.assertEqual(transaction.to_dict()["transaction_type"], "sale")
        self.assertEqual(transaction.to_dict()["transaction_date"], "2021-06-14")
        self.assertEqual(permit.to_dict()["status"], "granted")
        self.assertEqual(permit.to_dict()["application_date"], "2023-02-01")

    def test_permit_statuses_and_empty_lists(self):
        permit_pending = PermitRecord(status="pending")
        permit_rejected = PermitRecord(status="rejected")
        permit_withdrawn = PermitRecord(status="withdrawn")
        self.assertEqual(permit_pending.status, "pending")
        self.assertEqual(permit_rejected.status, "rejected")
        self.assertEqual(permit_withdrawn.status, "withdrawn")
        self.assertEqual(Property().permits_last_10_years, [])

    def test_previous_transaction_unknown_returns_none_change(self):
        self.assertIsNone(calculate_price_change_since_last_transaction(500000, None))

    def test_property_coerces_history_dicts_to_models(self):
        property_obj = Property(
            previous_transactions=[{"transaction_type": "sale", "transaction_price": 400000, "source": "Kadaster"}],
            permits_last_10_years=[{"status": "pending", "source": "Gemeente"}],
            active_permits=[{"status": "pending", "source": "Gemeente"}],
        )
        self.assertIsInstance(property_obj.previous_transactions[0], PropertyTransaction)
        self.assertIsInstance(property_obj.permits_last_10_years[0], PermitRecord)

    def test_data_provenance_supports_missing_source_and_low_confidence(self):
        provenance = DataProvenance.from_value(
            raw_value="onbekend",
            normalized_value=None,
            source_name=None,
            source_url=None,
            confidence="low",
        )
        as_dict = provenance.to_dict()
        self.assertIsNone(as_dict["source_name"])
        self.assertIsNone(as_dict["source_url"])
        self.assertEqual(as_dict["confidence"], "low")

    def test_schema_supports_confidence_and_quality_fields(self):
        payload = {
            "property_summary": "test",
            "extracted_data": {
                "source_url": None,
                "title": None,
                "address": None,
                "city": None,
                "country": None,
                "asking_price": None,
                "asking_price_status": "unknown",
                "asking_price_text": None,
                "listed_since": None,
                "days_on_market": None,
                "listing_status": "unknown",
                "original_asking_price": None,
                "current_asking_price": None,
                "price_reduction_count": 0,
                "last_price_reduction_date": None,
                "total_price_reduction_amount": None,
                "total_price_reduction_percentage": None,
                "listing_history_source": None,
                "listing_history_confidence": "low",
                "previous_transactions": [],
                "permits_last_10_years": [],
                "active_permits": [],
                "surface_m2": None,
                "price_per_m2": None,
                "annual_rent": None,
                "property_type": None,
                "current_use": None,
                "zoning": None,
                "energy_label": None,
                "description": None,
            },
            "investment_score": 50,
            "score_breakdown": {
                "location": 50,
                "price": 50,
                "yield": 50,
                "transformation": 50,
                "risk": 50,
                "marketability": 50,
                "negotiation_position": 50,
                "permit_risk": 50,
            },
            "analysis_confidence_score": 35,
            "data_quality_warnings": ["Bronnen ontbreken", "Lage betrouwbaarheid"],
            "strengths": [],
            "risks": [],
            "missing_information": [],
            "assumptions": [],
            "recommendation": None,
            "next_actions": [],
        }
        validated = _validate_analysis_payload(payload)
        self.assertEqual(validated["analysis_confidence_score"], 35)
        self.assertEqual(validated["data_quality_warnings"][0], "Bronnen ontbreken")


if __name__ == "__main__":
    unittest.main()
