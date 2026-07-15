import unittest

from models.property import Property
from services.investment_intelligence import InvestmentIntelligenceConfig, InvestmentIntelligenceEngine, SourceScoringProfile


class InvestmentIntelligenceEngineTests(unittest.TestCase):
    def setUp(self):
        self.engine = InvestmentIntelligenceEngine()

    def test_evaluate_property_returns_required_fields(self):
        portfolio = [
            Property(source_url="https://example.com/1", asking_price=400000, surface_m2=100, plot_size_m2=120, bedrooms=3, construction_year=1998, property_type="Appartement", energy_label="B", broker="Broker A"),
            Property(source_url="https://example.com/2", asking_price=500000, surface_m2=100, plot_size_m2=150, bedrooms=4, construction_year=2010, property_type="Huis", energy_label="A", broker="Broker B"),
            Property(source_url="https://example.com/3", asking_price=450000, surface_m2=90, plot_size_m2=110, bedrooms=3, construction_year=2005, property_type="Huis", energy_label="C", broker="Broker C"),
        ]
        subject = Property(
            source_url="https://example.com/subject",
            asking_price=420000,
            surface_m2=105,
            plot_size_m2=140,
            bedrooms=4,
            construction_year=2008,
            property_type="Huis",
            energy_label="A",
            broker="Broker D",
        )

        result = self.engine.evaluate(subject, portfolio=portfolio)

        self.assertGreaterEqual(result.overall_score, 0)
        self.assertLessEqual(result.overall_score, 100)
        self.assertIn(result.estimated_attractiveness, {"Low", "Medium", "High"})
        self.assertIsNotNone(result.price_per_m2)
        self.assertEqual(len(result.factors), 9)
        factor_keys = {factor.key for factor in result.factors}
        self.assertIn("asking_price_vs_woz", factor_keys)
        self.assertIn("building_age", factor_keys)
        self.assertEqual(len(result.top_positive_factors), 5)
        self.assertEqual(len(result.top_negative_factors), 5)
        self.assertTrue(result.explanation)

    def test_evaluate_many_returns_leave_one_out_results(self):
        properties = [
            Property(source_url="https://example.com/a", asking_price=300000, surface_m2=75, plot_size_m2=80, bedrooms=2, construction_year=1992, property_type="Appartement", energy_label="B"),
            Property(source_url="https://example.com/b", asking_price=600000, surface_m2=150, plot_size_m2=220, bedrooms=5, construction_year=2018, property_type="Huis", energy_label="A"),
        ]

        results = self.engine.evaluate_many(properties)

        self.assertEqual(len(results), 2)
        self.assertTrue(all(result.overall_score >= 0 for result in results))
        self.assertTrue(all(result.portfolio_size == 1 for result in results))

    def test_missing_values_do_not_crash_and_remain_neutral(self):
        result = self.engine.evaluate(Property(source_url="https://example.com/missing"), portfolio=[])

        self.assertEqual(result.price_per_m2, None)
        self.assertEqual(result.overall_score, 50)
        self.assertEqual(result.estimated_attractiveness, "Medium")
        self.assertTrue(result.top_positive_factors == [] or isinstance(result.top_positive_factors, list))
        self.assertTrue(result.top_negative_factors == [] or isinstance(result.top_negative_factors, list))

    def test_configurable_weights_change_score_behaviour(self):
        base_engine = InvestmentIntelligenceEngine()
        price_heavy_engine = InvestmentIntelligenceEngine(
            InvestmentIntelligenceConfig(
                factor_weights={
                    "asking_price_vs_portfolio_average": 60.0,
                    "asking_price_vs_woz": 5.0,
                    "living_area": 5.0,
                    "plot_size": 5.0,
                    "energy_label": 5.0,
                    "building_age": 5.0,
                    "bedrooms": 5.0,
                    "property_type": 5.0,
                    "price_per_m2": 10.0,
                }
            )
        )

        portfolio = [
            Property(source_url="https://example.com/1", asking_price=500000, surface_m2=100, plot_size_m2=120, bedrooms=3, construction_year=2000, property_type="Huis", energy_label="B"),
            Property(source_url="https://example.com/2", asking_price=500000, surface_m2=100, plot_size_m2=120, bedrooms=3, construction_year=2000, property_type="Huis", energy_label="B"),
        ]
        subject = Property(
            source_url="https://example.com/subject",
            asking_price=380000,
            surface_m2=100,
            plot_size_m2=140,
            bedrooms=4,
            construction_year=2015,
            property_type="Huis",
            energy_label="A",
        )

        base_result = base_engine.evaluate(subject, portfolio=portfolio)
        price_heavy_result = price_heavy_engine.evaluate(subject, portfolio=portfolio)

        self.assertNotEqual(base_result.overall_score, price_heavy_result.overall_score)

    def test_evaluate_many_uses_average_price_per_m2_from_imported_listings(self):
        properties = [
            Property(source_url="https://example.com/a", asking_price=400000, surface_m2=100, plot_size_m2=100, bedrooms=3, construction_year=2000, property_type="Huis", energy_label="B"),
            Property(source_url="https://example.com/b", asking_price=500000, surface_m2=100, plot_size_m2=120, bedrooms=4, construction_year=2010, property_type="Appartement", energy_label="A"),
            Property(source_url="https://example.com/c", asking_price=360000, surface_m2=100, plot_size_m2=90, bedrooms=2, construction_year=1995, property_type="Huis", energy_label="C"),
        ]

        results = self.engine.evaluate_many(properties)
        subject_result = results[2]
        price_factor = next(factor for factor in subject_result.factors if factor.key == "price_per_m2")

        self.assertEqual(price_factor.benchmark, "4500.00")
        self.assertEqual(subject_result.price_per_m2, 3600.0)

    def test_source_profile_overrides_apply_for_funda_domain(self):
        engine = InvestmentIntelligenceEngine(
            InvestmentIntelligenceConfig(
                source_profiles={
                    "funda.nl": SourceScoringProfile(
                        factor_weights={"price_per_m2": 45.0, "asking_price_vs_portfolio_average": 30.0},
                        attractiveness_thresholds={"low": 35, "medium": 65},
                    )
                }
            )
        )
        portfolio = [
            Property(source_url="https://example.com/1", asking_price=500000, surface_m2=100, plot_size_m2=120, bedrooms=3, construction_year=2000, property_type="Huis", energy_label="B"),
            Property(source_url="https://example.com/2", asking_price=500000, surface_m2=100, plot_size_m2=120, bedrooms=3, construction_year=2000, property_type="Huis", energy_label="B"),
        ]
        property_obj = Property(
            source_url="https://www.funda.nl/koop/rotterdam/appartement-123/",
            asking_price=380000,
            surface_m2=100,
            plot_size_m2=140,
            bedrooms=4,
            construction_year=2015,
            property_type="Appartement",
            energy_label="A",
        )

        default_result = self.engine.evaluate(property_obj, portfolio=portfolio)
        configured_result = engine.evaluate(property_obj, portfolio=portfolio)

        self.assertNotEqual(default_result.overall_score, configured_result.overall_score)
        self.assertIn(configured_result.estimated_attractiveness, {"Low", "Medium", "High"})

    def test_woz_and_building_age_factors_are_data_driven(self):
        subject = Property(
            source_url="https://example.com/subject",
            asking_price=450000,
            latest_woz_value=400000,
            surface_m2=100,
            bag_building_year=2000,
            property_type="Huis",
            energy_label="A",
            bedrooms=3,
        )

        result = self.engine.evaluate(subject, portfolio=[])
        woz_factor = next(factor for factor in result.factors if factor.key == "asking_price_vs_woz")
        age_factor = next(factor for factor in result.factors if factor.key == "building_age")

        self.assertEqual(woz_factor.benchmark, "400000.00")
        self.assertIsNotNone(age_factor.value)


if __name__ == "__main__":
    unittest.main()