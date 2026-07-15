import unittest

from services.listing_history import ListingHistoryEngine


class ListingHistoryEngineTests(unittest.TestCase):
    def setUp(self):
        self.engine = ListingHistoryEngine(recent_relist_window_days=180)

    def test_builds_listing_history_summary(self):
        listing = {
            "source_url": "https://example.com/listing/1",
            "listing_status": "active",
        }
        snapshots = [
            {
                "observed_at": "2025-01-01T10:00:00+00:00",
                "asking_price": 100000,
                "listing_status": "active",
            },
            {
                "observed_at": "2025-01-11T10:00:00+00:00",
                "asking_price": 95000,
                "listing_status": "active",
            },
            {
                "observed_at": "2025-01-21T10:00:00+00:00",
                "asking_price": 90000,
                "listing_status": "active",
            },
        ]

        result = self.engine.build(listing, snapshots)

        self.assertEqual(result.first_seen_date.isoformat(), "2025-01-01")
        self.assertEqual(result.latest_seen_date.isoformat(), "2025-01-21")
        self.assertEqual(result.days_on_market, 20)
        self.assertEqual(result.original_asking_price, 100000.0)
        self.assertEqual(result.current_asking_price, 90000.0)
        self.assertEqual(result.total_price_reduction, 10000.0)
        self.assertEqual(result.total_price_reduction_percentage, 10.0)
        self.assertEqual(result.number_of_price_changes, 2)
        self.assertEqual(result.listing_status, "active")
        self.assertEqual(len(result.price_history), 3)
        self.assertFalse(result.recently_relisted)

    def test_detects_recent_relisting(self):
        listing = {
            "source_url": "https://example.com/listing/2",
            "listing_status": "active",
        }
        snapshots = [
            {
                "observed_at": "2025-02-01T10:00:00+00:00",
                "asking_price": 210000,
                "listing_status": "withdrawn",
            },
            {
                "observed_at": "2025-02-18T10:00:00+00:00",
                "asking_price": 210000,
                "listing_status": "active",
            },
        ]

        result = self.engine.build(listing, snapshots)

        self.assertTrue(result.recently_relisted)
        self.assertEqual(result.relisted_date.isoformat(), "2025-02-18")
        self.assertEqual(result.listing_status, "active")

    def test_empty_snapshot_history_uses_listing_status(self):
        result = self.engine.build({"listing_status": "withdrawn"}, [])
        self.assertIsNone(result.days_on_market)
        self.assertEqual(result.listing_status, "withdrawn")
        self.assertEqual(result.number_of_price_changes, 0)


if __name__ == "__main__":
    unittest.main()
