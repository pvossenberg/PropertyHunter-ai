import re
import unicodedata
import unittest

from services.dutch_municipalities import DUTCH_MUNICIPALITIES, get_dutch_municipalities


def _sort_key(value: str) -> str:
    text = str(value or "").strip().lower()
    normalized = unicodedata.normalize("NFKD", text)
    ascii_value = normalized.encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", ascii_value).strip()


class DutchMunicipalitiesTests(unittest.TestCase):
    def test_municipalities_are_sorted_alphabetically(self):
        municipalities = get_dutch_municipalities()
        self.assertEqual(municipalities, sorted(municipalities, key=_sort_key))

    def test_municipalities_have_no_duplicates(self):
        municipalities = get_dutch_municipalities()
        lowered = [name.casefold() for name in municipalities]
        self.assertEqual(len(lowered), len(set(lowered)))

    def test_expected_municipalities_are_present(self):
        municipalities = set(get_dutch_municipalities())
        self.assertIn("Rotterdam", municipalities)
        self.assertIn("'s-Hertogenbosch", municipalities)
        self.assertIn("Súdwest-Fryslân", municipalities)

    def test_raw_constant_contains_expected_volume(self):
        self.assertGreaterEqual(len(DUTCH_MUNICIPALITIES), 300)


if __name__ == "__main__":
    unittest.main()
