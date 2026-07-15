import unittest
from unittest.mock import patch

from deal_finder.deduplication import match_listing, normalize_text, normalize_url, parse_address_components
from deal_finder.models import NormalizedListing
from deal_finder.orchestrator import DealFinderOrchestrator
from deal_finder.ranking import rank_listing
from deal_finder.sources.base import SourceRecordResult
from deal_finder.sources.manual_import import ManualImportAdapter
from services.database import DatabaseService


class InMemoryDatabaseService(DatabaseService):
    def __init__(self):
        super().__init__(url="", key="")
        self._enabled = True
        self._client = object()
        self._tables = {
            "listing_sources": [],
            "listings": [],
            "listing_snapshots": [],
            "scan_runs": [],
            "deal_candidates": [],
        }
        self._counter = 1

    def _next_id(self) -> str:
        value = f"id-{self._counter}"
        self._counter += 1
        return value

    def _fetch_rows(self, table_name, *, limit=100, order_column="created_at", ascending=False, filters=None):
        rows = [dict(item) for item in self._tables.get(table_name, [])]
        filtered = []
        for row in rows:
            ok = True
            for key, value in (filters or {}).items():
                if row.get(key) != value:
                    ok = False
                    break
            if ok:
                filtered.append(row)
        filtered.sort(key=lambda item: str(item.get(order_column) or ""), reverse=not ascending)
        return filtered[:limit]

    def _insert_row(self, table_name, payload):
        row = dict(payload)
        row.setdefault("id", self._next_id())
        self._tables.setdefault(table_name, []).append(row)
        return dict(row)

    def _update_row(self, table_name, row_id, payload):
        rows = self._tables.get(table_name, [])
        for idx, row in enumerate(rows):
            if str(row.get("id")) == str(row_id):
                updated = dict(row)
                updated.update(payload)
                rows[idx] = updated
                return dict(updated)
        return {}

    def _upsert_rows(self, table_name, payload, on_conflict):
        rows = self._tables.setdefault(table_name, [])
        keys = [item.strip() for item in on_conflict.split(",")]
        for idx, row in enumerate(rows):
            if all(row.get(key) == payload.get(key) for key in keys):
                updated = dict(row)
                updated.update(payload)
                updated.setdefault("id", row.get("id") or self._next_id())
                rows[idx] = updated
                return dict(updated)
        return self._insert_row(table_name, payload)


class DealFinderFoundationTests(unittest.TestCase):
    def setUp(self):
        self.adapter = ManualImportAdapter()

    def test_csv_import(self):
        csv_text = "source_name,external_listing_id,source_url,title,address,city,asking_price\nmanual,a1,https://example.com/a1,Object A,Straat 1,Amsterdam,450000\n"
        listings, warnings = self.adapter.import_csv(csv_text)
        self.assertEqual(len(warnings), 0)
        self.assertEqual(len(listings), 1)
        self.assertEqual(listings[0].external_listing_id, "a1")

    def test_json_import(self):
        json_text = '[{"source_name":"manual","external_listing_id":"x1","source_url":"https://example.com/x1","title":"X"}]'
        listings, warnings = self.adapter.import_json(json_text)
        self.assertEqual(len(warnings), 0)
        self.assertEqual(len(listings), 1)
        self.assertEqual(listings[0].source_url, "https://example.com/x1")

    def test_malformed_imports(self):
        listings, warnings = self.adapter.import_json("{not-json")
        self.assertEqual(listings, [])
        self.assertTrue(warnings)

        listings2, warnings2 = self.adapter.import_csv("")
        self.assertEqual(listings2, [])
        self.assertTrue(warnings2)

    def test_duplicate_external_listing_id(self):
        db = InMemoryDatabaseService()
        source = db.upsert_listing_source(name="manual", source_type="manual_import", base_url=None, is_enabled=True, configuration={})
        source_id = source.get("id")

        first = db.upsert_listing(
            source_id=source_id,
            external_listing_id="dup-1",
            source_url="https://example.com/listing/1",
            title="A",
            address="Straat 1",
            city="Amsterdam",
            asking_price=300000,
            surface_m2=70,
            property_type="woning",
            listing_status="active",
            raw_payload={},
        )
        second = db.upsert_listing(
            source_id=source_id,
            external_listing_id="dup-1",
            source_url="https://example.com/listing/1-updated",
            title="A2",
            address="Straat 1",
            city="Amsterdam",
            asking_price=320000,
            surface_m2=71,
            property_type="woning",
            listing_status="active",
            raw_payload={},
        )
        self.assertEqual(first.get("id"), second.get("id"))

    def test_duplicate_normalized_url(self):
        db = InMemoryDatabaseService()
        source = db.upsert_listing_source(name="manual", source_type="manual_import", base_url=None, is_enabled=True, configuration={})
        source_id = source.get("id")

        first = db.upsert_listing(
            source_id=source_id,
            external_listing_id=None,
            source_url="https://example.com/listing/2/",
            title="B",
            address="Straat 2",
            city="Rotterdam",
            asking_price=200000,
            surface_m2=55,
            property_type="woning",
            listing_status="active",
            raw_payload={},
        )
        second = db.upsert_listing(
            source_id=source_id,
            external_listing_id=None,
            source_url="https://example.com/listing/2",
            title="B2",
            address="Straat 2",
            city="Rotterdam",
            asking_price=205000,
            surface_m2=55,
            property_type="woning",
            listing_status="active",
            raw_payload={},
        )
        self.assertEqual(first.get("id"), second.get("id"))

    def test_address_normalization(self):
        normalized = normalize_text("  Keizersgracht 123-A, Amsterdam ")
        self.assertIn("keizersgracht", normalized)

        components = parse_address_components("Keizersgracht 123-A 1015CJ Amsterdam")
        self.assertEqual(components["postcode"], "1015CJ")
        self.assertEqual(components["house_number"], "123")

        url = normalize_url("HTTPS://EXAMPLE.COM/Listing/3/")
        self.assertEqual(url, "https://example.com/Listing/3")

    def test_unchanged_snapshot(self):
        db = InMemoryDatabaseService()
        listing = db._insert_row("listings", {"source_url": "https://example.com/a"})
        listing_id = listing["id"]
        first = db.add_listing_snapshot_if_changed(
            listing_id=listing_id,
            snapshot={"asking_price": 100000, "listing_status": "active", "title": "A", "description": "Desc", "surface_m2": 50, "features": {}, "raw_payload": {}},
        )
        second = db.add_listing_snapshot_if_changed(
            listing_id=listing_id,
            snapshot={"asking_price": 100000, "listing_status": "active", "title": "A", "description": "Desc", "surface_m2": 50, "features": {}, "raw_payload": {}},
        )
        self.assertTrue(first["changed"])
        self.assertFalse(second["changed"])
        self.assertEqual(second["change_type"], "unchanged")

    def test_asking_price_change_snapshot(self):
        db = InMemoryDatabaseService()
        listing = db._insert_row("listings", {"source_url": "https://example.com/b"})
        listing_id = listing["id"]
        db.add_listing_snapshot_if_changed(
            listing_id=listing_id,
            snapshot={"asking_price": 200000, "listing_status": "active", "title": "B", "description": "Desc", "surface_m2": 60, "features": {}, "raw_payload": {}},
        )
        changed = db.add_listing_snapshot_if_changed(
            listing_id=listing_id,
            snapshot={"asking_price": 180000, "listing_status": "active", "title": "B", "description": "Desc", "surface_m2": 60, "features": {}, "raw_payload": {}},
        )
        self.assertTrue(changed["changed"])
        self.assertEqual(changed["change_type"], "asking_price_change")

    def test_status_change_snapshot(self):
        db = InMemoryDatabaseService()
        listing = db._insert_row("listings", {"source_url": "https://example.com/c"})
        listing_id = listing["id"]
        db.add_listing_snapshot_if_changed(
            listing_id=listing_id,
            snapshot={"asking_price": 250000, "listing_status": "active", "title": "C", "description": "Desc", "surface_m2": 65, "features": {}, "raw_payload": {}},
        )
        changed = db.add_listing_snapshot_if_changed(
            listing_id=listing_id,
            snapshot={"asking_price": 250000, "listing_status": "withdrawn", "title": "C", "description": "Desc", "surface_m2": 65, "features": {}, "raw_payload": {}},
        )
        self.assertTrue(changed["changed"])
        self.assertEqual(changed["change_type"], "listing_status_change")

    def test_ranking_with_complete_data(self):
        listing = NormalizedListing(
            source_name="manual",
            source_url="https://example.com/r1",
            title="Object",
            address="Straat 1",
            city="Amsterdam",
            asking_price=320000,
            surface_m2=80,
            property_type="woning",
            description="Mooie kans",
        )
        result = rank_listing(listing, context={"price_per_m2": 3200, "days_on_market": 130, "price_reduction_count": 2, "investment_score": 82})
        self.assertGreaterEqual(result.candidate_score, 60)
        self.assertIn(result.priority, {"high", "urgent"})

    def test_ranking_with_missing_data(self):
        listing = NormalizedListing(source_name="manual", source_url="https://example.com/r2")
        result = rank_listing(listing, context={})
        self.assertGreaterEqual(len(result.missing_data_warnings), 1)
        self.assertIn(result.priority, {"low", "medium", "high", "urgent"})

    def test_empty_candidate_list(self):
        db = InMemoryDatabaseService()
        candidates = db.list_deal_candidates(limit=10)
        self.assertEqual(candidates, [])

    def test_missing_supabase_credentials(self):
        service = DatabaseService(url="", key="")
        self.assertFalse(service.is_enabled)
        self.assertEqual(service.list_deal_candidates(), [])
        self.assertEqual(service.get_source_health(), {"sources": [], "latest_scan_runs": []})

    def test_source_health_status(self):
        db = InMemoryDatabaseService()
        source = db.upsert_listing_source(name="Manual", source_type="manual_import", base_url=None, is_enabled=True, configuration={})
        source_id = source.get("id")
        scan_id = db.create_scan_run(source_id=source_id, status="running", metadata={"k": "v"})
        db.complete_scan_run(
            scan_run_id=scan_id,
            status="completed",
            items_found=4,
            items_new=2,
            items_changed=1,
            error_message=None,
            metadata={},
        )
        health = db.get_source_health()
        self.assertEqual(len(health["sources"]), 1)
        self.assertEqual(health["sources"][0]["latest_scan_status"], "completed")

    def test_no_live_network_calls(self):
        with patch("requests.get") as mocked_get:
            adapter = ManualImportAdapter()
            listings, warnings = adapter.import_urls("https://example.com/abc\n")
            self.assertEqual(len(listings), 1)
            self.assertEqual(warnings, [])
            mocked_get.assert_not_called()

    def test_ingest_refreshes_listing_history_summary(self):
        db = InMemoryDatabaseService()
        orchestrator = DealFinderOrchestrator(database_service=db)

        first_result = SourceRecordResult(
            record_index=0,
            success=True,
            listing=NormalizedListing(
                source_name="manual",
                source_url="https://example.com/history-1",
                external_listing_id="history-1",
                title="History Object",
                address="Straat 1",
                city="Amsterdam",
                asking_price=200000,
                surface_m2=50,
                property_type="woning",
                description="First snapshot",
                listing_status="active",
                raw_payload={},
            ),
        )
        second_result = SourceRecordResult(
            record_index=1,
            success=True,
            listing=NormalizedListing(
                source_name="manual",
                source_url="https://example.com/history-1",
                external_listing_id="history-1",
                title="History Object",
                address="Straat 1",
                city="Amsterdam",
                asking_price=180000,
                surface_m2=50,
                property_type="woning",
                description="Second snapshot",
                listing_status="active",
                raw_payload={},
            ),
        )

        first_ingest = orchestrator._ingest_source_results(source_id=None, source_name="manual", results=[first_result])
        second_ingest = orchestrator._ingest_source_results(source_id=None, source_name="manual", results=[second_result])

        self.assertEqual(first_ingest["imported"], 1)
        self.assertEqual(second_ingest["imported"], 1)

        listing_row = db._tables["listings"][0]
        self.assertEqual(listing_row.get("current_asking_price"), 180000)
        self.assertEqual(listing_row.get("original_asking_price"), 200000)
        self.assertEqual(listing_row.get("price_reduction_count"), 1)
        self.assertEqual(listing_row.get("days_on_market"), 0)
        self.assertEqual(len(listing_row.get("price_history") or []), 2)

    def test_deduplication_methods(self):
        incoming = NormalizedListing(
            source_name="manual",
            external_listing_id="x-1",
            source_url="https://example.com/l/1",
            title="A",
            address="Keizersgracht 123A 1015CJ Amsterdam",
        )
        existing = [{"id": "l1", "property_id": "p1", "source_name": "manual", "external_listing_id": "x-1", "source_url": "https://example.com/l/1", "address": "Keizersgracht 123A 1015CJ Amsterdam"}]
        match = match_listing(incoming, existing)
        self.assertEqual(match.match_method, "source_external_listing_id")
        self.assertEqual(match.matched_listing_id, "l1")


if __name__ == "__main__":
    unittest.main()
