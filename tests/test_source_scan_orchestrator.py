import unittest

from deal_finder.models import NormalizedListing
from deal_finder.orchestrator import DealFinderOrchestrator
from deal_finder.sources.base import ListingSourceAdapter, SourceRecordResult
from deal_finder.sources.beleggingspanden import BeleggingspandenAdapter
from deal_finder.sources.jaap import JaapAdapter
from deal_finder.sources.marktplaats import MarktplaatsAdapter
from deal_finder.sources.registry import SourceAdapterRegistry


class SourceScanDbStub:
    def __init__(self, fail_url: str | None = None):
        self.fail_url = fail_url
        self.sources = []
        self.listings = []
        self.scan_runs = []
        self.snapshots = {}

    def upsert_listing_source(self, **kwargs):
        existing = next((item for item in self.sources if item.get("name") == kwargs.get("name")), None)
        if existing:
            existing.update(kwargs)
            return dict(existing)
        row = {"id": f"source-{len(self.sources) + 1}", **kwargs}
        self.sources.append(row)
        return dict(row)

    def create_scan_run(self, **kwargs):
        row = {"id": f"scan-{len(self.scan_runs) + 1}", **kwargs}
        self.scan_runs.append(row)
        return row["id"]

    def complete_scan_run(self, **kwargs):
        self.scan_runs.append({"completed": True, **kwargs})
        return kwargs

    def list_raw_listings(self, limit=5000):
        return [dict(item) for item in self.listings[:limit]]

    def upsert_listing(self, **kwargs):
        source_url = kwargs.get("source_url")
        if self.fail_url and source_url == self.fail_url:
            raise RuntimeError("forced per-listing storage failure")

        source_id = kwargs.get("source_id")
        external_listing_id = kwargs.get("external_listing_id")

        if source_id and external_listing_id:
            existing = next(
                (
                    item
                    for item in self.listings
                    if item.get("source_id") == source_id and item.get("external_listing_id") == external_listing_id
                ),
                None,
            )
            if existing:
                existing.update(kwargs)
                return dict(existing)

        existing_by_url = next((item for item in self.listings if item.get("source_url") == source_url), None)
        if existing_by_url:
            existing_by_url.update(kwargs)
            return dict(existing_by_url)

        row = {"id": f"listing-{len(self.listings) + 1}", **kwargs}
        self.listings.append(row)
        return dict(row)

    def add_listing_snapshot_if_changed(self, **kwargs):
        listing_id = kwargs["listing_id"]
        snapshot = kwargs["snapshot"]
        key = (
            snapshot.get("asking_price"),
            snapshot.get("listing_status"),
            snapshot.get("title"),
            snapshot.get("description"),
            snapshot.get("surface_m2"),
        )
        previous = self.snapshots.get(listing_id)
        if previous == key:
            return {"changed": False, "change_type": "unchanged", "snapshot_id": f"snap-{listing_id}"}
        self.snapshots[listing_id] = key
        return {
            "changed": True,
            "change_type": "new_listing" if previous is None else "content_change",
            "snapshot_id": f"snap-{listing_id}",
        }

    def get_listing_snapshots(self, listing_id, limit=100):
        if listing_id not in self.snapshots:
            return []
        asking_price, listing_status, title, description, surface_m2 = self.snapshots[listing_id]
        return [
            {
                "observed_at": "2026-07-15T10:00:00+00:00",
                "asking_price": asking_price,
                "listing_status": listing_status,
                "title": title,
                "description": description,
                "surface_m2": surface_m2,
            }
        ]

    def update_listing_history(self, listing_id, history_payload):
        for idx, row in enumerate(self.listings):
            if str(row.get("id")) == str(listing_id):
                updated = dict(row)
                updated.update(history_payload or {})
                self.listings[idx] = updated
                return updated
        return {}

    def create_or_update_deal_candidate(self, **kwargs):
        return {"id": f"candidate-{kwargs.get('listing_id')}", **kwargs}


class FakeSourceAdapter(ListingSourceAdapter):
    source_name = "funda.nl"
    source_type = "portal"

    def __init__(self, *, with_failure_payload: bool = True):
        self.with_failure_payload = with_failure_payload
        self._last_fetch_stats = {
            "listings_found": 4,
            "listings_imported": 2,
            "duplicates_skipped": 1,
            "failed_listings": 1,
        }

    def validate_configuration(self, configuration):
        start_url = str(configuration.get("start_url") or "").strip()
        if not start_url:
            return False, ["start_url is required"]
        return True, []

    def discover_listings(self, configuration):
        return []

    def fetch_listing_details(self, listing_ref, configuration):
        return dict(listing_ref or {})

    def normalize_listing(self, payload):
        raise NotImplementedError

    def load_and_normalize_listings(self, configuration):
        listing_1 = NormalizedListing(
            source_name=self.source_name,
            external_listing_id="f-1",
            source_url="https://www.funda.nl/detail/koop/test/f-1/1111/",
            title="Listing 1",
            city="Amsterdam",
            asking_price=450000.0,
            surface_m2=90.0,
            listing_status="active",
            raw_payload={"listing_id": "1111"},
        )
        listing_2 = NormalizedListing(
            source_name=self.source_name,
            external_listing_id="f-2",
            source_url="https://www.funda.nl/detail/koop/test/f-2/2222/",
            title="Listing 2",
            city="Utrecht",
            asking_price=520000.0,
            surface_m2=110.0,
            listing_status="active",
            raw_payload={"listing_id": "2222"},
        )

        results = [
            SourceRecordResult(record_index=1, success=True, listing=listing_1, payload={"source_url": listing_1.source_url}),
            SourceRecordResult(record_index=2, success=True, listing=listing_2, payload={"source_url": listing_2.source_url}),
        ]
        if self.with_failure_payload:
            results.append(
                SourceRecordResult(
                    record_index=3,
                    success=False,
                    listing=None,
                    error="ValueError: malformed listing",
                    payload={"source_url": "https://www.funda.nl/detail/koop/test/f-3/3333/"},
                )
            )
        return results


class SourceScanOrchestratorTests(unittest.TestCase):
    def test_registry_resolves_funda_alias(self):
        registry = SourceAdapterRegistry(adapters=[FakeSourceAdapter()])
        self.assertIsNotNone(registry.resolve("funda"))
        self.assertIsNotNone(registry.resolve("Funda.nl"))

    def test_registry_resolves_new_source_aliases(self):
        registry = SourceAdapterRegistry(adapters=[JaapAdapter(), BeleggingspandenAdapter(), MarktplaatsAdapter()])
        self.assertIsNotNone(registry.resolve("jaap"))
        self.assertIsNotNone(registry.resolve("beleggingspanden"))
        self.assertIsNotNone(registry.resolve("marktplaats"))

    def test_import_from_source_returns_requested_stats(self):
        adapter = FakeSourceAdapter()
        registry = SourceAdapterRegistry(adapters=[adapter])
        orchestrator = DealFinderOrchestrator(SourceScanDbStub(), source_registry=registry)

        result = orchestrator.import_from_source("funda", {"start_url": "https://www.funda.nl/zoeken/koop"})

        self.assertTrue(result["ok"])
        self.assertEqual(result["source"], "funda.nl")
        self.assertEqual(result["listings_found"], 4)
        self.assertEqual(result["listings_imported"], 2)
        self.assertEqual(result["duplicates_skipped"], 1)
        self.assertEqual(result["failed_listings"], 2)

    def test_import_from_source_continues_when_one_listing_write_fails(self):
        adapter = FakeSourceAdapter(with_failure_payload=False)
        registry = SourceAdapterRegistry(adapters=[adapter])
        db = SourceScanDbStub(fail_url="https://www.funda.nl/detail/koop/test/f-2/2222/")
        orchestrator = DealFinderOrchestrator(db, source_registry=registry)

        result = orchestrator.import_from_source("funda", {"start_url": "https://www.funda.nl/zoeken/koop"})

        self.assertTrue(result["ok"])
        self.assertEqual(result["listings_imported"], 1)
        self.assertGreaterEqual(result["failed_listings"], 1)
        self.assertEqual(len(result["listing_ids"]), 1)


if __name__ == "__main__":
    unittest.main()
