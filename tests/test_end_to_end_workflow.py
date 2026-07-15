import io
import unittest
from contextlib import redirect_stdout

from app import _run_end_to_end_cli
from services.end_to_end_workflow import EndToEndRow, EndToEndRunResult, PropertyHunterEndToEndWorkflow


class _FakeInvestmentResult:
    def __init__(self, overall_score: int):
        self.overall_score = overall_score


class _FakeOpportunityFinding:
    def __init__(self, opportunity_type: str):
        self.opportunity_type = opportunity_type


class _FakeOpportunityResult:
    def __init__(self, opportunity_score: int, types: list[str]):
        self.opportunity_score = opportunity_score
        self.detected_opportunities = [_FakeOpportunityFinding(item) for item in types]


class _FakeInvestmentEngine:
    def evaluate(self, property_obj, portfolio=None):
        asking_price = float(property_obj.asking_price or 0)
        return _FakeInvestmentResult(overall_score=int(min(100, max(0, asking_price / 10000))))


class _FakeOpportunityEngine:
    def evaluate(self, property_obj, portfolio=None, investment_result=None):
        investment = int(getattr(investment_result, "overall_score", 0))
        return _FakeOpportunityResult(opportunity_score=min(100, investment + 7), types=["Flip Opportunity"])


class _FakeOrchestrator:
    def __init__(self, payload):
        self.payload = payload

    def import_from_source(self, source_name, configuration=None):
        return dict(self.payload)


class _FakeDatabase:
    def __init__(self, listing_details):
        self._listing_details = dict(listing_details)
        self.candidate_updates = []

    def list_raw_listings(self, limit=5000):
        rows = []
        for detail in self._listing_details.values():
            listing = detail.get("listing") if isinstance(detail, dict) else {}
            if isinstance(listing, dict):
                rows.append(dict(listing))
        return rows[:limit]

    def get_listing_detail(self, listing_id):
        return dict(self._listing_details.get(str(listing_id), {}))

    def create_or_update_deal_candidate(self, **kwargs):
        self.candidate_updates.append(dict(kwargs))
        return dict(kwargs)


class PropertyHunterEndToEndWorkflowTests(unittest.TestCase):
    def test_processes_only_new_listings_and_sorts_top_rows(self):
        listing_details = {
            "L1": {
                "listing": {
                    "id": "L1",
                    "source_url": "https://www.funda.nl/detail/koop/a/1/",
                    "address": "Straat 1",
                    "city": "Amsterdam",
                    "asking_price": 700000,
                    "surface_m2": 100,
                    "listing_status": "active",
                    "days_on_market": 12,
                },
                "source": {"name": "funda.nl"},
            },
            "L2": {
                "listing": {
                    "id": "L2",
                    "source_url": "https://www.funda.nl/detail/koop/b/2/",
                    "address": "Straat 2",
                    "city": "Rotterdam",
                    "asking_price": 500000,
                    "surface_m2": 95,
                    "listing_status": "active",
                    "total_price_reduction_percentage": 8.0,
                },
                "source": {"name": "funda.nl"},
            },
            "L3": {
                "listing": {
                    "id": "L3",
                    "source_url": "https://www.funda.nl/detail/koop/c/3/",
                    "address": "Straat 3",
                    "city": "Utrecht",
                    "asking_price": 300000,
                    "surface_m2": 80,
                    "listing_status": "active",
                },
                "source": {"name": "funda.nl"},
            },
        }
        db = _FakeDatabase(listing_details)
        orchestrator = _FakeOrchestrator(
            {
                "ok": True,
                "listings_found": 3,
                "new_listing_ids": ["L1", "L2"],
                "listing_ids": ["L1", "L2", "L3"],
                "warnings": [],
            }
        )
        workflow = PropertyHunterEndToEndWorkflow(
            orchestrator=orchestrator,
            database_service=db,
            investment_engine=_FakeInvestmentEngine(),
            opportunity_engine=_FakeOpportunityEngine(),
        )

        result = workflow.run_funda(start_url="https://www.funda.nl/zoeken/koop", max_pages=1, timeout_seconds=1.0, top_n=10)

        self.assertTrue(result.ok)
        self.assertEqual(result.listings_found, 3)
        self.assertEqual(result.new_listings, 2)
        self.assertEqual(result.processed_listings, 2)
        self.assertEqual(len(db.candidate_updates), 2)
        self.assertEqual([item["listing_id"] for item in db.candidate_updates], ["L1", "L2"])
        self.assertEqual(result.top_rows[0].listing_id, "L1")
        self.assertGreaterEqual(result.top_rows[0].deal_score, result.top_rows[1].deal_score)

    def test_returns_empty_top_rows_when_no_new_listings(self):
        db = _FakeDatabase({})
        orchestrator = _FakeOrchestrator(
            {
                "ok": True,
                "listings_found": 5,
                "new_listing_ids": [],
                "listing_ids": ["A", "B"],
                "warnings": [],
            }
        )
        workflow = PropertyHunterEndToEndWorkflow(
            orchestrator=orchestrator,
            database_service=db,
            investment_engine=_FakeInvestmentEngine(),
            opportunity_engine=_FakeOpportunityEngine(),
        )

        result = workflow.run_funda(start_url="https://www.funda.nl/zoeken/koop", max_pages=1, timeout_seconds=1.0)

        self.assertTrue(result.ok)
        self.assertEqual(result.new_listings, 0)
        self.assertEqual(result.processed_listings, 0)
        self.assertEqual(result.top_rows, [])
        self.assertEqual(db.candidate_updates, [])


class EndToEndCliTests(unittest.TestCase):
    def test_run_cli_prints_summary_and_top_rows(self):
        class FakeWorkflow:
            def run_funda(self, *, start_url, max_pages, timeout_seconds, top_n):
                self.called_with = {
                    "start_url": start_url,
                    "max_pages": max_pages,
                    "timeout_seconds": timeout_seconds,
                    "top_n": top_n,
                }
                return EndToEndRunResult(
                    ok=True,
                    listings_found=12,
                    new_listings=4,
                    processed_listings=4,
                    top_rows=[
                        EndToEndRow(
                            listing_id="L1",
                            address="Straat 1, Amsterdam",
                            asking_price=550000,
                            deal_score=89,
                            investment_score=81,
                            listing_history="status=active, 5 dagen",
                            source="funda.nl",
                        )
                    ],
                    warnings=[],
                    error=None,
                )

        fake_workflow = FakeWorkflow()
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            exit_code = _run_end_to_end_cli([], workflow=fake_workflow)

        output = buffer.getvalue()
        self.assertEqual(exit_code, 0)
        self.assertIn("aantal gevonden woningen: 12", output)
        self.assertIn("aantal nieuwe woningen: 4", output)
        self.assertIn("deal score=89", output)
        self.assertIn("investment score=81", output)
        self.assertIn("listing history=status=active, 5 dagen", output)


if __name__ == "__main__":
    unittest.main()
