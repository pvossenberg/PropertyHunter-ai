import unittest

from models.permit import PermitRecord
from services.permit_service import PermitService


class PermitServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = PermitService()

    def test_valid_address(self):
        result = self.service.get_permits_last_10_years("Keizersgracht 123 Amsterdam")
        self.assertTrue(result["is_mock_data"])
        self.assertGreaterEqual(result["data"]["count"], 1)

    def test_empty_address(self):
        result = self.service.get_permits_last_10_years("   ")
        self.assertEqual(result["data"]["count"], 0)
        self.assertIn("Invalid or empty address input.", result["warnings"])

    def test_no_permits_found(self):
        result = self.service.get_permits_last_10_years("Coolsingel 50 Rotterdam")
        self.assertEqual(result["data"]["count"], 0)
        self.assertIn("No permit records found in the last 10 years for this address.", result["warnings"])

    def test_pending_permit(self):
        result = self.service.get_active_permits("Keizersgracht 123 Amsterdam")
        self.assertGreaterEqual(result["data"]["count"], 1)
        statuses = [record.status for record in result["data"]["permits"]]
        self.assertIn("pending", statuses)

    def test_granted_permit(self):
        result = self.service.get_granted_permits("Keizersgracht 123 Amsterdam")
        statuses = [record.status for record in result["data"]["permits"]]
        self.assertIn("granted", statuses)

    def test_rejected_permit(self):
        result = self.service.get_rejected_permits("Keizersgracht 123 Amsterdam")
        statuses = [record.status for record in result["data"]["permits"]]
        self.assertIn("rejected", statuses)

    def test_withdrawn_permit(self):
        result = self.service.get_withdrawn_permits("Keizersgracht 123 Amsterdam")
        statuses = [record.status for record in result["data"]["permits"]]
        self.assertIn("withdrawn", statuses)

    def test_mock_data_marking(self):
        summary = self.service.get_permit_summary("Keizersgracht 123 Amsterdam")
        self.assertTrue(summary["is_mock_data"])
        self.assertIn("Mock data only. Do not treat these outputs as verified municipal facts.", summary["warnings"])
        self.assertIn("status_counts", summary["data"])

    def test_returns_permitrecord_objects(self):
        result = self.service.get_granted_permits("Keizersgracht 123 Amsterdam")
        self.assertTrue(all(isinstance(item, PermitRecord) for item in result["data"]["permits"]))


if __name__ == "__main__":
    unittest.main()
