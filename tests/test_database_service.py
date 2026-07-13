import unittest

from app import _filter_and_sort_properties
from services.database import DatabaseService


class StubDatabaseService(DatabaseService):
    def __init__(self, rows_by_table: dict[str, list[dict]]):
        super().__init__(url="", key="")
        self._enabled = True
        self._client = object()
        self._rows_by_table = rows_by_table

    def _fetch_rows(
        self,
        table_name: str,
        *,
        limit: int = 100,
        order_column: str = "created_at",
        ascending: bool = False,
        filters: dict | None = None,
    ) -> list[dict]:
        rows = [dict(item) for item in self._rows_by_table.get(table_name, [])]

        filtered_rows = []
        for row in rows:
            matches = True
            for key, value in (filters or {}).items():
                if row.get(key) != value:
                    matches = False
                    break
            if matches:
                filtered_rows.append(row)

        filtered_rows.sort(key=lambda item: str(item.get(order_column) or ""), reverse=not ascending)
        return filtered_rows[:limit]


class DatabaseServiceTests(unittest.TestCase):
    def test_service_disabled_without_credentials(self):
        service = DatabaseService(url="", key="")
        self.assertFalse(service.is_enabled)
        self.assertIsNone(service.store_analyzed_property("", {}))
        self.assertEqual(service.list_properties(), [])
        self.assertEqual(service.list_analyses(), [])
        self.assertEqual(
            service.get_property_with_latest_analysis("x"),
            {
                "property": {},
                "analysis": {},
                "transactions": [],
                "permits": [],
                "energy_labels": [],
            },
        )
        self.assertEqual(service.get_dashboard_statistics()["total_properties"], 0)

    def test_build_payloads_from_analysis(self):
        service = DatabaseService(url="", key="")
        extracted = {
            "title": "Test Object",
            "address": "Voorbeeldstraat 1",
            "city": "Amsterdam",
            "country": "NL",
            "asking_price": 500000,
            "asking_price_status": "known",
            "energy_label": "A",
            "previous_transactions": [
                {
                    "transaction_date": "2024-01-01",
                    "transaction_type": "sale",
                    "transaction_price": 450000,
                    "price_status": "known",
                    "confidence": "high",
                }
            ],
            "permits_last_10_years": [
                {
                    "application_date": "2024-02-01",
                    "permit_type": "verbouwing",
                    "status": "pending",
                    "confidence": "medium",
                    "affects_investment_case": True,
                }
            ],
            "active_permits": [],
        }
        analysis = {
            "property_summary": "Samenvatting",
            "investment_score": 70,
            "score_breakdown": {"location": 70},
            "analysis_confidence_score": 65,
            "data_quality_warnings": [],
            "strengths": [],
            "risks": [],
            "missing_information": [],
            "assumptions": [],
            "recommendation": "Onderzoek verder",
            "next_actions": [],
            "extracted_data": extracted,
        }

        property_payload = service._build_property_payload("https://example.com", extracted)
        self.assertEqual(property_payload["title"], "Test Object")
        self.assertEqual(property_payload["asking_price_status"], "known")

        analysis_payload = service._build_analysis_payload("property-id", analysis)
        self.assertEqual(analysis_payload["property_id"], "property-id")
        self.assertEqual(analysis_payload["investment_score"], 70)

        transactions = service._build_transactions_payload("property-id", "analysis-id", extracted)
        self.assertEqual(len(transactions), 1)
        self.assertEqual(transactions[0]["transaction_type"], "sale")

        permits = service._build_permits_payload("property-id", "analysis-id", extracted)
        self.assertEqual(len(permits), 1)
        self.assertEqual(permits[0]["status"], "pending")

        energy_label = service._build_energy_label_payload("property-id", extracted)
        self.assertIsNotNone(energy_label)
        self.assertEqual(energy_label["label"], "A")

    def test_empty_database(self):
        service = StubDatabaseService(rows_by_table={})
        self.assertEqual(service.list_properties(), [])
        self.assertEqual(service.list_analyses(), [])
        detail = service.get_property_with_latest_analysis("missing")
        self.assertEqual(detail["property"], {})
        self.assertEqual(detail["analysis"], {})
        self.assertEqual(detail["transactions"], [])
        self.assertEqual(detail["permits"], [])
        self.assertEqual(detail["energy_labels"], [])

        stats = service.get_dashboard_statistics()
        self.assertEqual(stats["total_properties"], 0)
        self.assertEqual(stats["total_analyses"], 0)
        self.assertEqual(stats["average_investment_score"], 0.0)
        self.assertEqual(stats["highest_investment_score"], 0)
        self.assertEqual(stats["properties_by_city"], {})

    def test_one_stored_property(self):
        service = StubDatabaseService(
            rows_by_table={
                "properties": [
                    {
                        "id": "p1",
                        "title": "Kanaalpand",
                        "address": "Herengracht 1",
                        "city": "Amsterdam",
                        "asking_price": 700000,
                        "price_per_m2": 5000,
                        "source_url": "https://example.com/p1",
                        "created_at": "2026-07-10T10:00:00+00:00",
                    }
                ],
                "analyses": [
                    {
                        "id": "a1",
                        "property_id": "p1",
                        "investment_score": 84,
                        "created_at": "2026-07-11T09:00:00+00:00",
                    }
                ],
            }
        )

        rows = service.list_properties()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], "p1")
        self.assertEqual(rows[0]["investment_score"], 84)

    def test_filtering_by_city(self):
        service = StubDatabaseService(
            rows_by_table={
                "properties": [
                    {"id": "p1", "title": "A", "city": "Amsterdam", "created_at": "2026-07-10T10:00:00+00:00"},
                    {"id": "p2", "title": "B", "city": "Rotterdam", "created_at": "2026-07-10T09:00:00+00:00"},
                ],
                "analyses": [],
            }
        )

        rows = service.list_properties(city="Amsterdam")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["city"], "Amsterdam")

    def test_property_detail_loading(self):
        service = StubDatabaseService(
            rows_by_table={
                "properties": [{"id": "p1", "title": "Kanaalpand"}],
                "analyses": [{"id": "a1", "property_id": "p1", "investment_score": 77}],
            }
        )

        detail = service.get_property_with_latest_analysis("p1")
        self.assertEqual(detail["property"]["id"], "p1")
        self.assertEqual(detail["analysis"]["id"], "a1")
        self.assertEqual(detail["transactions"], [])

    def test_dashboard_statistics(self):
        service = StubDatabaseService(
            rows_by_table={
                "properties": [
                    {"id": "p1", "title": "A", "city": "Amsterdam", "created_at": "2026-07-11T10:00:00+00:00", "asking_price": 600000},
                    {"id": "p2", "title": "B", "city": "Amsterdam", "created_at": "2026-07-12T10:00:00+00:00", "asking_price": 400000},
                    {"id": "p3", "title": "C", "city": "Rotterdam", "created_at": "2026-07-09T10:00:00+00:00", "asking_price": 300000},
                ],
                "analyses": [
                    {"id": "a1", "property_id": "p1", "investment_score": 80, "created_at": "2026-07-11T10:00:00+00:00"},
                    {"id": "a2", "property_id": "p2", "investment_score": 90, "created_at": "2026-07-12T10:00:00+00:00"},
                    {"id": "a3", "property_id": "p3", "investment_score": 70, "created_at": "2026-07-09T10:00:00+00:00"},
                ],
            }
        )

        stats = service.get_dashboard_statistics()
        self.assertEqual(stats["total_properties"], 3)
        self.assertEqual(stats["total_analyses"], 3)
        self.assertEqual(stats["average_investment_score"], 80.0)
        self.assertEqual(stats["highest_investment_score"], 90)
        self.assertEqual(stats["properties_by_city"]["Amsterdam"], 2)
        self.assertEqual(len(stats["top_properties"]), 3)

    def test_filtering_by_minimum_score(self):
        rows = [
            {"id": "p1", "title": "A", "address": "Straat 1", "city": "Amsterdam", "investment_score": 75, "asking_price": 300000, "created_at": "2026-07-01T10:00:00+00:00"},
            {"id": "p2", "title": "B", "address": "Straat 2", "city": "Amsterdam", "investment_score": 40, "asking_price": 250000, "created_at": "2026-07-02T10:00:00+00:00"},
        ]

        filtered = _filter_and_sort_properties(
            rows,
            city_filter="Alle steden",
            min_investment_score=60,
            max_asking_price=None,
            search_query="",
            sort_option="Hoogste score",
        )
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0]["id"], "p1")

    def test_sorting(self):
        rows = [
            {"id": "p1", "title": "A", "address": "Straat 1", "city": "Amsterdam", "investment_score": 50, "asking_price": 450000, "created_at": "2026-07-01T10:00:00+00:00"},
            {"id": "p2", "title": "B", "address": "Straat 2", "city": "Amsterdam", "investment_score": 90, "asking_price": 650000, "created_at": "2026-07-03T10:00:00+00:00"},
            {"id": "p3", "title": "C", "address": "Straat 3", "city": "Amsterdam", "investment_score": 70, "asking_price": 300000, "created_at": "2026-07-02T10:00:00+00:00"},
        ]

        sorted_by_score = _filter_and_sort_properties(
            rows,
            city_filter="Alle steden",
            min_investment_score=0,
            max_asking_price=None,
            search_query="",
            sort_option="Hoogste score",
        )
        self.assertEqual([item["id"] for item in sorted_by_score], ["p2", "p3", "p1"])

        sorted_by_price = _filter_and_sort_properties(
            rows,
            city_filter="Alle steden",
            min_investment_score=0,
            max_asking_price=None,
            search_query="",
            sort_option="Laagste vraagprijs",
        )
        self.assertEqual([item["id"] for item in sorted_by_price], ["p3", "p1", "p2"])


if __name__ == "__main__":
    unittest.main()
