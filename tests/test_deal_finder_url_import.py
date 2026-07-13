import ast
import unittest
from pathlib import Path
from unittest.mock import patch

from app import _run_url_import
from deal_finder.orchestrator import DealFinderOrchestrator


class UrlImportDbStub:
    def __init__(self, fail_on_upsert_listing: bool = False, missing_source: bool = False):
        self.fail_on_upsert_listing = fail_on_upsert_listing
        self.missing_source = missing_source
        self.sources = []
        self.listings = []
        self.snapshots = {}

    def upsert_listing_source(self, **kwargs):
        if self.missing_source:
            return {}
        existing = next((item for item in self.sources if item["name"] == kwargs.get("name")), None)
        if existing:
            existing.update(kwargs)
            return dict(existing)
        row = {"id": f"source-{len(self.sources) + 1}", **kwargs}
        self.sources.append(row)
        return dict(row)

    def create_scan_run(self, **kwargs):
        return "scan-1"

    def complete_scan_run(self, **kwargs):
        return {}

    def list_raw_listings(self, limit=5000):
        return [dict(item) for item in self.listings[:limit]]

    def upsert_listing(self, **kwargs):
        if self.fail_on_upsert_listing:
            raise RuntimeError("database write failed")

        source_url = kwargs.get("source_url")
        existing = next((item for item in self.listings if item.get("source_url") == source_url), None)
        if existing:
            existing.update(kwargs)
            return dict(existing)

        row = {"id": f"listing-{len(self.listings) + 1}", **kwargs}
        self.listings.append(row)
        return dict(row)

    def add_listing_snapshot_if_changed(self, **kwargs):
        listing_id = kwargs["listing_id"]
        snapshot = kwargs["snapshot"]
        previous = self.snapshots.get(listing_id)
        key = (snapshot.get("asking_price"), snapshot.get("listing_status"), snapshot.get("title"), snapshot.get("description"), snapshot.get("surface_m2"))
        if previous == key:
            return {"changed": False, "change_type": "unchanged", "snapshot_id": f"snap-{listing_id}"}
        self.snapshots[listing_id] = key
        change_type = "new_listing" if previous is None else "content_change"
        return {"changed": True, "change_type": change_type, "snapshot_id": f"snap-{listing_id}"}

    def create_or_update_deal_candidate(self, **kwargs):
        return {"id": "candidate-1", **kwargs}


class FailingOrchestrator:
    def import_urls(self, urls_text: str):
        raise RuntimeError("forced failure")


class DealFinderUrlImportTests(unittest.TestCase):
    def test_valid_pasted_url(self):
        orchestrator = DealFinderOrchestrator(UrlImportDbStub())
        result = orchestrator.import_urls("https://www.gbmakelaars.nl/object/123\n")
        self.assertEqual(result["found"], 1)
        self.assertEqual(result["new"], 1)

    def test_malformed_url(self):
        orchestrator = DealFinderOrchestrator(UrlImportDbStub())
        result = orchestrator.import_urls("notaurl\n")
        self.assertEqual(result["found"], 0)
        self.assertTrue(result["warnings"])

    def test_unreachable_url_is_not_fetched(self):
        orchestrator = DealFinderOrchestrator(UrlImportDbStub())
        with patch("requests.get") as mocked_get:
            result = orchestrator.import_urls("https://this-domain-should-not-resolve.invalid/listing\n")
            mocked_get.assert_not_called()
        self.assertEqual(result["found"], 1)

    def test_duplicate_url(self):
        db = UrlImportDbStub()
        orchestrator = DealFinderOrchestrator(db)
        result = orchestrator.import_urls("https://example.com/a\nhttps://example.com/a\n")
        self.assertEqual(result["found"], 2)
        self.assertEqual(len(db.listings), 1)

    def test_missing_source(self):
        orchestrator = DealFinderOrchestrator(UrlImportDbStub(missing_source=True))
        result = orchestrator.import_urls("https://example.com/a\n")
        self.assertTrue(any("Source could not be resolved" in warning for warning in result["warnings"]))

    def test_database_error(self):
        orchestrator = DealFinderOrchestrator(UrlImportDbStub(fail_on_upsert_listing=True))
        with self.assertRaises(RuntimeError):
            orchestrator.import_urls("https://example.com/a\n")

    def test_import_must_not_crash_streamlit(self):
        outcome = _run_url_import("https://example.com/a\n", orchestrator=FailingOrchestrator())
        self.assertFalse(outcome["ok"])
        self.assertIn("RuntimeError", outcome["error"])

    def test_url_import_does_not_import_pandas_or_pyarrow(self):
        module_paths = [
            Path("/workspaces/PropertyHunter-ai/deal_finder/orchestrator.py"),
            Path("/workspaces/PropertyHunter-ai/deal_finder/sources/manual_import.py"),
            Path("/workspaces/PropertyHunter-ai/services/database.py"),
        ]
        for module_path in module_paths:
            tree = ast.parse(module_path.read_text(encoding="utf-8"))
            imported = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.extend(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.append(node.module)
            joined = "\n".join(imported).lower()
            self.assertNotIn("pandas", joined)
            self.assertNotIn("pyarrow", joined)


if __name__ == "__main__":
    unittest.main()
