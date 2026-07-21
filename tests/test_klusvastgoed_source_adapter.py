import unittest

from deal_finder.sources.klusvastgoed import KlusvastgoedAdapter, build_klusvastgoed_national_url, build_klusvastgoed_start_url
from services.klusvastgoed_service import KlusvastgoedService, extract_map_center_from_html, normalize_klusvastgoed_municipality_slug


LISTING_FRAGMENT = """
<div class="mb-5 sm-mb-3 h-full">
  <a href="/koop/rotterdam/nystadstraat-11">
    <div class="bg-white rounded-3xl drop-shadow-md listing-card h-full flex flex-col">
      <div class="p-2 pb-0 flex-1">
        <div class="kvg-pand-thumb-wrap">
          <img src="https://panden.klusvastgoed.nl/rotterdam/nystadstraat-11/20260701_090618_thumbnail.jpg" class="listing-thumb" loading="lazy" alt="Eengezinswoning, tussenwoning Nystadstraat 11 in Rotterdam" />
        </div>
        <div class="relative lh-20 p-2 text-base listing-info">
          <strong class="block mt-2 text-lg font-bold mobile-title">Rotterdam</strong>
          <span class="min-h-40p block text-quaternary-200 sm-text-15px">Nystadstraat 11</span>
          <div class="kvg-metrics-row">
            <span class="kvg-metric"><span class="v">3</span></span>
            <span class="kvg-metric"><span class="v">106 m&sup2;</span></span>
            <span class="kvg-metric"><span class="v">82 m&sup2;</span></span>
          </div>
          <div class="kvg-price-block"><div class="kvg-price-row"><span class="kvg-price-now">&euro;&nbsp;329.000</span></div></div>
        </div>
      </div>
    </div>
  </a>
</div>
"""

DETAIL_HTML = """
<!DOCTYPE html>
<html lang="nl">
<head>
  <title>Nystadstraat 11 Rotterdam kluswoning te koop</title>
  <meta property="og:title" content="Nystadstraat 11 Rotterdam kluswoning te koop" />
  <meta name="description" content="Kluswoning Nystadstraat 11 Rotterdam Oosterflank met tuin, balkon en uitbreidingskansen." />
  <meta property="og:description" content="Kluswoning Nystadstraat 11 Rotterdam Oosterflank met tuin, balkon en uitbreidingskansen." />
  <meta property="og:image" content="https://panden.klusvastgoed.nl/rotterdam/nystadstraat-11/20260701_090618_thumbnail.jpg" />
  <meta property="article:published_time" content="2026-07-01T09:06:18+00:00" />
</head>
<body>
  <div class="kvg-price-row"><span class="kvg-price-now">€ 329.000</span></div>
  <div class="w-full grid grid-cols-3 gap-y-2 text-base sm-flex sm-flex-col">
    <div class="mr-5"><strong>106m2</strong> leefruimte</div>
    <div class="mr-5"><strong>82m2</strong> perceel</div>
    <div class="mr-5">Gebouwd in <strong>1980</strong></div>
    <div class="mr-5"><strong>3</strong> slaapkamer(s)</div>
  </div>
  <h1>Nystadstraat 11 Rotterdam kluswoning te koop</h1>
  <p class="kz-intro">Deze eengezinswoning in Oosterflank ademt de gedateerde jaren-tachtig afwerking en vraagt om een grondige opknapbeurt.</p>
  <img src="https://panden.klusvastgoed.nl/rotterdam/nystadstraat-11/20260701_090618_thumbnail.jpg" class="listing-thumb" alt="Eengezinswoning, tussenwoning Nystadstraat 11 in Rotterdam" />
</body>
</html>
"""

DETAIL_JSONLD_HTML = """
<!DOCTYPE html>
<html lang="nl">
<head>
  <title>2e Carnissestraat 29-B, Rotterdam</title>
  <link rel="canonical" href="https://www.klusvastgoed.nl/koop/rotterdam/2e-carnissestraat-29-b" />
  <meta property="og:url" content="https://www.klusvastgoed.nl/koop/rotterdam/2e-carnissestraat-29-b" />
  <script type="application/ld+json">
  {
    "@context": "https://schema.org",
    "@type": "RealEstateListing",
    "name": "2e Carnissestraat 29-B, Rotterdam",
    "url": "https://www.klusvastgoed.nl/koop/rotterdam/2e-carnissestraat-29-b",
    "image": ["https://example.com/photo-1.jpg", "https://example.com/photo-2.jpg"],
    "address": {
      "@type": "PostalAddress",
      "streetAddress": "2e Carnissestraat 29-B",
      "addressLocality": "Rotterdam",
      "postalCode": "3082 AB",
      "addressRegion": "Zuid-Holland",
      "addressCountry": "NL"
    },
    "offers": {
      "@type": "Offer",
      "price": 215000,
      "priceCurrency": "EUR"
    }
  }
  </script>
</head>
<body>
  <h1>2e Carnissestraat 29-B Rotterdam kluswoning te koop</h1>
</body>
</html>
"""

CITY_INDEX_HTML = """
<html><body>
  <a href="/kluswoning-rotterdam">12 kluswoningen Rotterdam Zuid-Holland</a>
  <a href="/kluswoning-utrecht">6 kluswoningen Utrecht Utrecht</a>
</body></html>
"""

CITY_HTML = """
<html><head>
  <script>var centerLat = parseFloat("51.924420"); var centerLng = parseFloat("4.477733");</script>
</head><body></body></html>
"""

FRAGMENT_PAGE_1 = """
<div class="mb-5 sm-mb-3 h-full">
  <a href="/koop/rotterdam/a-1">
    <div class="bg-white rounded-3xl drop-shadow-md listing-card h-full flex flex-col">
      <div class="relative lh-20 p-2 text-base listing-info">
        <strong class="block mt-2 text-lg font-bold mobile-title">Rotterdam</strong>
        <span class="min-h-40p block text-quaternary-200 sm-text-15px">A Street 1</span>
        <div class="kvg-metrics-row">
          <span class="kvg-metric"><span class="v">3</span></span>
          <span class="kvg-metric"><span class="v">106 m&sup2;</span></span>
          <span class="kvg-metric"><span class="v">82 m&sup2;</span></span>
        </div>
        <div class="kvg-price-block"><div class="kvg-price-row"><span class="kvg-price-now">€ 329.000</span></div></div>
      </div>
    </div>
  </a>
</div>
<div class="mb-5 sm-mb-3 h-full">
  <a href="/koop/rotterdam/a-2">
    <div class="bg-white rounded-3xl drop-shadow-md listing-card h-full flex flex-col">
      <div class="relative lh-20 p-2 text-base listing-info">
        <strong class="block mt-2 text-lg font-bold mobile-title">Rotterdam</strong>
        <span class="min-h-40p block text-quaternary-200 sm-text-15px">A Street 2</span>
        <div class="kvg-metrics-row">
          <span class="kvg-metric"><span class="v">3</span></span>
          <span class="kvg-metric"><span class="v">100 m&sup2;</span></span>
          <span class="kvg-metric"><span class="v">70 m&sup2;</span></span>
        </div>
        <div class="kvg-price-block"><div class="kvg-price-row"><span class="kvg-price-now">€ 299.000</span></div></div>
      </div>
    </div>
  </a>
</div>
"""

FRAGMENT_PAGE_2 = """
<div class="mb-5 sm-mb-3 h-full">
  <a href="/koop/rotterdam/a-2">
    <div class="bg-white rounded-3xl drop-shadow-md listing-card h-full flex flex-col">
      <div class="relative lh-20 p-2 text-base listing-info">
        <strong class="block mt-2 text-lg font-bold mobile-title">Rotterdam</strong>
        <span class="min-h-40p block text-quaternary-200 sm-text-15px">A Street 2</span>
        <div class="kvg-metrics-row">
          <span class="kvg-metric"><span class="v">3</span></span>
          <span class="kvg-metric"><span class="v">100 m&sup2;</span></span>
          <span class="kvg-metric"><span class="v">70 m&sup2;</span></span>
        </div>
        <div class="kvg-price-block"><div class="kvg-price-row"><span class="kvg-price-now">€ 299.000</span></div></div>
      </div>
    </div>
  </a>
</div>
<div class="mb-5 sm-mb-3 h-full">
  <a href="/koop/rotterdam/a-3">
    <div class="bg-white rounded-3xl drop-shadow-md listing-card h-full flex flex-col">
      <div class="relative lh-20 p-2 text-base listing-info">
        <strong class="block mt-2 text-lg font-bold mobile-title">Rotterdam</strong>
        <span class="min-h-40p block text-quaternary-200 sm-text-15px">A Street 3</span>
        <div class="kvg-metrics-row">
          <span class="kvg-metric"><span class="v">3</span></span>
          <span class="kvg-metric"><span class="v">98 m&sup2;</span></span>
          <span class="kvg-metric"><span class="v">68 m&sup2;</span></span>
        </div>
        <div class="kvg-price-block"><div class="kvg-price-row"><span class="kvg-price-now">€ 289.000</span></div></div>
      </div>
    </div>
  </a>
</div>
"""

FRAGMENT_UTRECHT_PAGE_1 = """
<div class="mb-5 sm-mb-3 h-full">
  <a href="/koop/utrecht/u-1">
    <div class="bg-white rounded-3xl drop-shadow-md listing-card h-full flex flex-col">
      <div class="relative lh-20 p-2 text-base listing-info">
        <strong class="block mt-2 text-lg font-bold mobile-title">Utrecht</strong>
        <span class="min-h-40p block text-quaternary-200 sm-text-15px">U Street 1</span>
        <div class="kvg-metrics-row">
          <span class="kvg-metric"><span class="v">3</span></span>
          <span class="kvg-metric"><span class="v">90 m&sup2;</span></span>
          <span class="kvg-metric"><span class="v">55 m&sup2;</span></span>
        </div>
        <div class="kvg-price-block"><div class="kvg-price-row"><span class="kvg-price-now">€ 279.000</span></div></div>
      </div>
    </div>
  </a>
</div>
"""


class _FakeNationalResponse:
    def __init__(self, text: str):
        self.text = text

    def raise_for_status(self):
        return None


class _FakeNationalSession:
  def __init__(self):
    self.headers = {}
    self.current_city = None

  def request(self, method: str, url: str, timeout: float, headers=None, data=None):
    method = method.upper()
    if method == "GET" and url.endswith("/kluswoningen-per-stad"):
      return _FakeNationalResponse(CITY_INDEX_HTML)
    if method == "GET" and url.endswith("/kluswoning-rotterdam"):
      self.current_city = "rotterdam"
      return _FakeNationalResponse(CITY_HTML)
    if method == "GET" and url.endswith("/kluswoning-utrecht"):
      self.current_city = "utrecht"
      return _FakeNationalResponse(CITY_HTML.replace("51.924420", "52.090737").replace("4.477733", "5.121420"))
    if method == "POST" and url.endswith("set_bounds.php"):
      return _FakeNationalResponse("")
    if method == "POST" and url.endswith("get_panden.php"):
      offset = str((data or {}).get("offset") or "0")
      if self.current_city == "utrecht":
        return _FakeNationalResponse(FRAGMENT_UTRECHT_PAGE_1 if offset == "0" else "")
      if offset == "0":
        return _FakeNationalResponse(FRAGMENT_PAGE_1)
      if offset == "12":
        return _FakeNationalResponse(FRAGMENT_PAGE_2)
      return _FakeNationalResponse("")
    if method == "GET" and url.endswith("/koop/rotterdam/a-1"):
      return _FakeNationalResponse(DETAIL_JSONLD_HTML)
    if method == "GET" and url.endswith("/koop/rotterdam/a-2"):
      return _FakeNationalResponse(DETAIL_JSONLD_HTML.replace("2e Carnissestraat 29-B", "A Street 2").replace("3082 AB", "3000 AA"))
    if method == "GET" and url.endswith("/koop/rotterdam/a-3"):
      return _FakeNationalResponse(DETAIL_JSONLD_HTML.replace("2e Carnissestraat 29-B", "A Street 3").replace("3082 AB", "3000 AB"))
    raise AssertionError(f"Unexpected request: {method} {url} data={data}")

MAP_CENTER_HTML = """
<script>
  var centerLat = parseFloat("52.367573");
  var centerLng = parseFloat("4.904139");
</script>
"""


class _StubService:
    def fetch_listing_cards(self, start_url: str, *, timeout_seconds: float, max_pages: int):
        return [
            {
                "source_url": "https://www.klusvastgoed.nl/pand/rotterdam/nystadstraat-11",
                "title": "Nystadstraat 11 Rotterdam",
                "address": "Nystadstraat 11",
                "city": "Rotterdam",
                "asking_price": 329000.0,
                "living_area": 106.0,
                "plot_area": 82.0,
                "image_url": "https://panden.klusvastgoed.nl/rotterdam/nystadstraat-11/20260701_090618_thumbnail.jpg",
                "property_type": "eengezinswoning",
            }
        ]

    def fetch_listing_detail_html(self, source_url: str, *, timeout_seconds: float, referer: str | None = None):
        return DETAIL_HTML

    def build_start_url(self, municipality: str) -> str:
        return build_klusvastgoed_start_url(municipality)


class KlusvastgoedAdapterTests(unittest.TestCase):
  def test_service_discovers_national_listings_without_duplicate_urls(self):
    service = KlusvastgoedService(session=_FakeNationalSession())
    refs = service.fetch_national_listing_cards(timeout_seconds=1.0, max_pages=2)

    self.assertEqual(len(refs), 4)
    self.assertEqual({row["municipality"] for row in refs}, {"Rotterdam", "Utrecht"})
    self.assertEqual({row["province"] for row in refs}, {"Zuid-Holland", "Utrecht"})

  def test_service_extracts_city_index_metadata(self):
    service = KlusvastgoedService(session=_FakeNationalSession())
    city_refs = service.discover_city_refs(timeout_seconds=1.0)

    self.assertEqual(len(city_refs), 2)
    self.assertEqual(city_refs[0]["municipality"], "Rotterdam")
    self.assertEqual(city_refs[0]["province"], "Zuid-Holland")

  def test_service_extracts_listing_refs_from_ajax_fragment(self):
    service = KlusvastgoedService()
    refs = service.extract_listing_refs_from_fragment(
      start_url="https://www.klusvastgoed.nl/kluswoning-amsterdam",
      html_fragment=LISTING_FRAGMENT,
    )

    self.assertEqual(len(refs), 1)
    record = refs[0]
    self.assertEqual(record.source_url, "https://www.klusvastgoed.nl/koop/rotterdam/nystadstraat-11")
    self.assertEqual(record.address, "Nystadstraat 11")
    self.assertEqual(record.city, "Rotterdam")
    self.assertEqual(record.asking_price, 329000.0)
    self.assertEqual(record.living_area, 106.0)
    self.assertEqual(record.plot_area, 82.0)
    self.assertEqual(record.property_type, "Eengezinswoning")

  def test_normalize_listing_maps_required_fields_and_fallbacks(self):
    adapter = KlusvastgoedAdapter(service=_StubService())
    payload = adapter._extract_listing_record(
      source_url="https://www.klusvastgoed.nl/pand/rotterdam/nystadstraat-11",
      html=DETAIL_HTML,
      listing_ref={
        "address": "Nystadstraat 11",
        "city": "Rotterdam",
        "asking_price": 329000.0,
        "living_area": 106.0,
        "plot_area": 82.0,
        "image_url": "https://panden.klusvastgoed.nl/rotterdam/nystadstraat-11/20260701_090618_thumbnail.jpg",
      },
    )

    self.assertEqual(payload["title"], "Nystadstraat 11 Rotterdam kluswoning te koop")
    self.assertEqual(payload["address"], "Nystadstraat 11")
    self.assertEqual(payload["city"], "Rotterdam")
    self.assertEqual(payload["asking_price"], 329000.0)
    self.assertEqual(payload["living_area"], 106.0)
    self.assertEqual(payload["plot_size"], 82.0)
    self.assertEqual(payload["property_type"], "eengezinswoning")
    self.assertEqual(payload["publication_date"].isoformat(), "2026-07-01")
    self.assertEqual(payload["listed_since"].isoformat(), "2026-07-01")
    self.assertTrue(payload["photos"])

    listing = adapter.normalize_listing(payload)
    self.assertEqual(listing.source_name, "klusvastgoed.nl")
    self.assertEqual(listing.external_listing_id, "pand::rotterdam::nystadstraat-11")
    self.assertEqual(listing.asking_price, 329000.0)
    self.assertEqual(listing.surface_m2, 106.0)
    self.assertEqual(listing.property_type, "eengezinswoning")

    property_model = adapter.to_property_model(payload)
    self.assertEqual(property_model.city, "Rotterdam")
    self.assertEqual(property_model.asking_price, 329000.0)
    self.assertEqual(property_model.surface_m2, 106.0)
    self.assertEqual(property_model.plot_size_m2, 82.0)
    self.assertEqual(property_model.listed_since.isoformat(), "2026-07-01")
    self.assertTrue(property_model.photos)

  def test_extract_listing_record_gracefully_marks_missing_fields(self):
    adapter = KlusvastgoedAdapter(service=_StubService())
    payload = adapter._extract_listing_record(
      source_url="https://www.klusvastgoed.nl/pand/rotterdam/minimal",
      html="""
      <html><head><title>Minimal listing</title></head><body>
        <h1>Minimal listing</h1>
      </body></html>
      """,
      listing_ref={"source_url": "https://www.klusvastgoed.nl/pand/rotterdam/minimal"},
    )

    self.assertEqual(payload["source_name"], "klusvastgoed.nl")
    self.assertTrue(payload["missing_fields"])
    self.assertIn("asking_price", payload["missing_fields"])
    self.assertTrue(payload["parser_warnings"])

    listing = adapter.normalize_listing(payload)
    self.assertEqual(listing.source_name, "klusvastgoed.nl")
    self.assertEqual(listing.title, "Minimal listing")
    self.assertIsNone(listing.asking_price)

  def test_extract_listing_record_uses_jsonld_geo_fields_and_canonical_url(self):
    adapter = KlusvastgoedAdapter(service=_StubService())
    payload = adapter._extract_listing_record(
      source_url="https://www.klusvastgoed.nl/koop/rotterdam/2e-carnissestraat-29-b",
      html=DETAIL_JSONLD_HTML,
      listing_ref={"province": "Zuid-Holland"},
    )

    self.assertEqual(payload["source_url"], "https://www.klusvastgoed.nl/koop/rotterdam/2e-carnissestraat-29-b")
    self.assertEqual(payload["canonical_url"], "https://www.klusvastgoed.nl/koop/rotterdam/2e-carnissestraat-29-b")
    self.assertEqual(payload["city"], "Rotterdam")
    self.assertEqual(payload["municipality"], "Rotterdam")
    self.assertEqual(payload["province"], "Zuid-Holland")
    self.assertEqual(payload["postal_code"], "3082 AB")
    self.assertEqual(payload["asking_price"], 215000.0)
    self.assertEqual(payload["photos"][0], "https://example.com/photo-1.jpg")

  def test_load_and_normalize_listings_continues_when_one_detail_fails(self):
    class FailingService(_StubService):
      def fetch_listing_cards(self, start_url: str, *, timeout_seconds: float, max_pages: int):
        rows = super().fetch_listing_cards(start_url, timeout_seconds=timeout_seconds, max_pages=max_pages)
        rows.append({"source_url": "https://www.klusvastgoed.nl/pand/rotterdam/missing"})
        return rows

      def fetch_listing_detail_html(self, source_url: str, *, timeout_seconds: float, referer: str | None = None):
        if source_url.endswith("/missing"):
          raise RuntimeError("boom")
        return super().fetch_listing_detail_html(source_url, timeout_seconds=timeout_seconds, referer=referer)

    adapter = KlusvastgoedAdapter(service=FailingService())
    results = adapter.load_and_normalize_listings(
      {"start_url": "https://www.klusvastgoed.nl/kluswoning-rotterdam", "max_pages": 1, "timeout_seconds": 1.0}
    )

    self.assertEqual(len(results), 2)
    self.assertEqual(sum(1 for item in results if item.success), 1)
    self.assertEqual(adapter.get_last_fetch_stats()["failed_listings"], 1)

  def test_build_start_url_normalizes_selected_municipality(self):
    self.assertEqual(normalize_klusvastgoed_municipality_slug("'s-Hertogenbosch"), "s-hertogenbosch")
    self.assertEqual(build_klusvastgoed_start_url("Den Haag"), "https://www.klusvastgoed.nl/kluswoning-den-haag")
    self.assertEqual(build_klusvastgoed_national_url(), "https://www.klusvastgoed.nl/kluswoningen-per-stad")

  def test_extract_map_center_from_html(self):
    self.assertEqual(extract_map_center_from_html(MAP_CENTER_HTML), (52.367573, 4.904139))


if __name__ == "__main__":
    unittest.main()