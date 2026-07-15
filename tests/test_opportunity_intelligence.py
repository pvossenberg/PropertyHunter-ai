import unittest

from models.property import Property
from services.opportunity_intelligence import OpportunityIntelligenceConfig, OpportunityIntelligenceEngine


class OpportunityIntelligenceEngineTests(unittest.TestCase):
    def setUp(self):
        self.engine = OpportunityIntelligenceEngine()

    def test_detects_multiple_opportunities_from_existing_funda_data(self):
        portfolio = [
            Property(source_url="https://example.com/1", asking_price=500000, surface_m2=100, plot_size_m2=120, bedrooms=3, construction_year=2000, property_type="Huis", energy_label="B"),
            Property(source_url="https://example.com/2", asking_price=520000, surface_m2=105, plot_size_m2=130, bedrooms=3, construction_year=2005, property_type="Appartement", energy_label="A"),
            Property(source_url="https://example.com/3", asking_price=480000, surface_m2=95, plot_size_m2=110, bedrooms=2, construction_year=1998, property_type="Huis", energy_label="C"),
        ]
        property_obj = Property(
            source_url="https://www.funda.nl/detail/koop/amsterdam/object-123/11111111/",
            asking_price=425000,
            surface_m2=165,
            plot_size_m2=520,
            bedrooms=5,
            construction_year=1968,
            property_type="Vrijstaande woning",
            energy_label="F",
            description="Vrijstaande woning met plat dak, twee ingangen en veel renovatiepotentieel. KluSwoning met eigen entree.",
            raw_text="Vrijstaande woning met plat dak, twee ingangen en veel renovatiepotentieel. KluSwoning met eigen entree.",
        )

        result = self.engine.evaluate(property_obj, portfolio=portfolio)

        detected_types = [item.opportunity_type for item in result.detected_opportunities]
        self.assertGreaterEqual(result.overall_investment_score, 0)
        self.assertLessEqual(result.overall_investment_score, 100)
        self.assertGreater(result.opportunity_score, 0)
        self.assertIn("Possible Split Opportunity", detected_types)
        self.assertIn("Possible Extension Opportunity", detected_types)
        self.assertIn("Possible Rooftop Extension", detected_types)
        self.assertIn("Renovation Opportunity", detected_types)
        self.assertIn("Rental Opportunity", detected_types)
        self.assertIn("Flip Opportunity", detected_types)
        self.assertTrue(result.explanation)

        split = next(item for item in result.detected_opportunities if item.opportunity_type == "Possible Split Opportunity")
        self.assertGreaterEqual(split.confidence, 45)
        self.assertIn("multiple entrances", split.explanation.lower())
        self.assertIn("multiple entrances", split.required_data)

    def test_reports_missing_data_when_signals_are_sparse(self):
        result = self.engine.evaluate(Property(source_url="https://example.com/missing", asking_price=300000), portfolio=[])

        self.assertEqual(result.detected_opportunities, [])
        self.assertEqual(result.opportunity_score, 0)
        self.assertIn("No clear opportunity pattern", result.explanation)

    def test_evaluate_many_returns_per_property_results(self):
        properties = [
            Property(source_url="https://example.com/a", asking_price=320000, surface_m2=85, bedrooms=2, construction_year=1995, property_type="Appartement", energy_label="B"),
            Property(source_url="https://example.com/b", asking_price=620000, surface_m2=175, plot_size_m2=700, bedrooms=5, construction_year=1975, property_type="Vrijstaande woning", energy_label="D", raw_text="plat dak en apart entree"),
        ]

        results = self.engine.evaluate_many(properties)

        self.assertEqual(len(results), 2)
        self.assertTrue(all(result.overall_investment_score >= 0 for result in results))
        self.assertTrue(any(result.detected_opportunities for result in results))

    def test_config_is_modular_for_future_enrichment(self):
        config = OpportunityIntelligenceConfig(min_confidence=20)
        engine = OpportunityIntelligenceEngine(config=config)
        result = engine.evaluate(
            Property(
                source_url="https://www.funda.nl/detail/koop/amsterdam/object-456/22222222/",
                asking_price=390000,
                surface_m2=150,
                plot_size_m2=450,
                bedrooms=4,
                construction_year=1983,
                property_type="2 onder 1 kap woning",
                energy_label="E",
                raw_text="dakopbouw mogelijk en meerdere kamers",
            ),
            portfolio=[
                Property(source_url="https://example.com/x", asking_price=500000, surface_m2=100, bedrooms=3, construction_year=2000, property_type="Huis", energy_label="B"),
                Property(source_url="https://example.com/y", asking_price=510000, surface_m2=100, bedrooms=3, construction_year=2001, property_type="Huis", energy_label="B"),
                Property(source_url="https://example.com/z", asking_price=490000, surface_m2=100, bedrooms=3, construction_year=2002, property_type="Huis", energy_label="B"),
            ],
            context={
                "enrichment": {
                    "kadaster": {},
                    "woz": {},
                    "permits": {},
                    "zoning": {},
                    "municipality": {},
                }
            },
        )

        self.assertGreaterEqual(result.opportunity_score, 20)
        self.assertTrue(result.detected_opportunities)


if __name__ == "__main__":
    unittest.main()