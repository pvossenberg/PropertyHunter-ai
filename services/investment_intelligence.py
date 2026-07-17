from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlparse
from statistics import median
from typing import Any, Callable, Iterable

from models.property import Property
from services.calculations import calculate_price_per_m2


def _safe_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _clamp_score(value: float) -> int:
    return int(max(0, min(100, round(value))))


def _normalize_label(value: str | None) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.lower().split())


def _normalize_type(value: str | None) -> str:
    text = _normalize_label(value)
    return text.replace("_", " ").replace("-", " ")


def _source_key_from_value(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        return ""

    text = value.strip().lower()
    if "://" in text:
        parsed = urlparse(text)
        host = parsed.netloc or parsed.path
    else:
        host = text

    host = host.split("@")[-1].split(":")[0]
    if host.startswith("www."):
        host = host[4:]

    parts = [part for part in host.split(".") if part]
    if len(parts) >= 2:
        return ".".join(parts[-2:])
    return host


@dataclass(frozen=True)
class PortfolioBenchmarks:
    average_price_per_m2: float | None = None
    median_price_per_m2: float | None = None
    sample_size: int = 0


@dataclass(frozen=True)
class SourceScoringProfile:
    factor_weights: dict[str, float] = field(default_factory=dict)
    attractiveness_thresholds: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class FactorResult:
    key: str
    label: str
    score: int
    weight: float
    contribution: float
    value: Any = None
    benchmark: Any = None
    explanation: str = ""


@dataclass(frozen=True)
class InvestmentIntelligenceResult:
    overall_score: int
    price_per_m2: float | None
    estimated_attractiveness: str
    top_positive_factors: list[str] = field(default_factory=list)
    top_negative_factors: list[str] = field(default_factory=list)
    explanation: str = ""
    factors: list[FactorResult] = field(default_factory=list)
    benchmarks: PortfolioBenchmarks = field(default_factory=PortfolioBenchmarks)
    portfolio_size: int = 0


@dataclass
class InvestmentIntelligenceConfig:
    factor_weights: dict[str, float] = field(
        default_factory=lambda: {
            "asking_price_vs_portfolio_average": 15.0,
            "asking_price_vs_woz": 15.0,
            "living_area": 10.0,
            "plot_size": 10.0,
            "energy_label": 15.0,
            "building_age": 15.0,
            "bedrooms": 10.0,
            "property_type": 10.0,
            "price_per_m2": 15.0,
        }
    )
    source_factor_weights: dict[str, dict[str, float]] = field(default_factory=dict)
    source_profiles: dict[str, SourceScoringProfile] = field(default_factory=dict)
    attractiveness_thresholds: dict[str, int] = field(default_factory=lambda: {"low": 45, "medium": 70})
    min_benchmark_sample_size: int = 3
    neutral_score: int = 50


ScoreFn = Callable[[Property, PortfolioBenchmarks, dict[str, Any], InvestmentIntelligenceConfig], FactorResult]


@dataclass(frozen=True)
class FactorDefinition:
    key: str
    label: str
    scorer: ScoreFn


class InvestmentIntelligenceEngine:
    def __init__(self, config: InvestmentIntelligenceConfig | None = None, factor_definitions: Iterable[FactorDefinition] | None = None):
        self.config = config or InvestmentIntelligenceConfig()
        self.factor_definitions = list(factor_definitions or self._build_default_factor_definitions())

    def evaluate(self, property_obj: Property, portfolio: Iterable[Property] | None = None, context: dict[str, Any] | None = None) -> InvestmentIntelligenceResult:
        portfolio_items = list(portfolio or [])
        active_context = dict(context or {})
        portfolio_benchmarks = self._build_portfolio_benchmarks(property_obj, portfolio_items, active_context)

        source_key = self._source_key(property_obj, active_context)
        weights = self._effective_weights(source_key)
        attractiveness_thresholds = self._effective_thresholds(source_key)

        factor_results: list[FactorResult] = []
        for factor_definition in self.factor_definitions:
            factor_result = factor_definition.scorer(property_obj, portfolio_benchmarks, active_context, self.config)
            effective_weight = float(weights.get(factor_definition.key, factor_result.weight or 0.0))
            adjusted_result = FactorResult(
                key=factor_result.key,
                label=factor_result.label,
                score=_clamp_score(factor_result.score),
                weight=effective_weight,
                contribution=self._calculate_contribution(factor_result.score, effective_weight),
                value=factor_result.value,
                benchmark=factor_result.benchmark,
                explanation=factor_result.explanation,
            )
            factor_results.append(adjusted_result)

        overall_score = self._calculate_overall_score(factor_results)
        attractiveness = self._attractiveness_label(overall_score, attractiveness_thresholds)
        top_positive_factors, top_negative_factors = self._select_top_factors(factor_results)
        explanation = self._build_explanation(property_obj, portfolio_benchmarks, factor_results, overall_score, attractiveness)

        price_per_m2 = self._property_price_per_m2(property_obj)

        return InvestmentIntelligenceResult(
            overall_score=overall_score,
            price_per_m2=price_per_m2,
            estimated_attractiveness=attractiveness,
            top_positive_factors=top_positive_factors,
            top_negative_factors=top_negative_factors,
            explanation=explanation,
            factors=factor_results,
            benchmarks=portfolio_benchmarks,
            portfolio_size=portfolio_benchmarks.sample_size,
        )

    def evaluate_many(self, properties: Iterable[Property], context: dict[str, Any] | None = None) -> list[InvestmentIntelligenceResult]:
        property_list = list(properties)
        results: list[InvestmentIntelligenceResult] = []
        for index, property_obj in enumerate(property_list):
            leave_one_out_portfolio = [item for idx, item in enumerate(property_list) if idx != index]
            results.append(self.evaluate(property_obj, portfolio=leave_one_out_portfolio, context=context))
        return results

    def _build_default_factor_definitions(self) -> list[FactorDefinition]:
        return [
            FactorDefinition("asking_price_vs_portfolio_average", "Asking price vs portfolio average", self._score_asking_price_vs_portfolio_average),
            FactorDefinition("asking_price_vs_woz", "Asking price vs WOZ", self._score_asking_price_vs_woz),
            FactorDefinition("living_area", "Living area", self._score_living_area),
            FactorDefinition("plot_size", "Plot size", self._score_plot_size),
            FactorDefinition("energy_label", "Energy label", self._score_energy_label),
            FactorDefinition("building_age", "Building age", self._score_building_age),
            FactorDefinition("bedrooms", "Bedrooms", self._score_bedrooms),
            FactorDefinition("property_type", "Property type", self._score_property_type),
            FactorDefinition("price_per_m2", "Price per m²", self._score_price_per_m2),
        ]

    def _effective_weights(self, source_name: str) -> dict[str, float]:
        weights = dict(self.config.factor_weights)
        source_weights = self.config.source_factor_weights.get(source_name, {}) if source_name else {}
        for key, value in source_weights.items():
            weights[key] = float(value)

        source_profile = self.config.source_profiles.get(source_name) if source_name else None
        if source_profile is not None:
            for key, value in source_profile.factor_weights.items():
                weights[key] = float(value)
        return weights

    def _effective_thresholds(self, source_name: str) -> dict[str, int]:
        thresholds = dict(self.config.attractiveness_thresholds)
        source_profile = self.config.source_profiles.get(source_name) if source_name else None
        if source_profile is not None:
            for key, value in source_profile.attractiveness_thresholds.items():
                thresholds[key] = int(value)
        return thresholds

    def _source_key(self, property_obj: Property, context: dict[str, Any]) -> str:
        source_key = _source_key_from_value(context.get("source_name"))
        if source_key:
            return source_key
        source_key = _source_key_from_value(context.get("source_url"))
        if source_key:
            return source_key
        return _source_key_from_value(property_obj.source_url)

    def _build_portfolio_benchmarks(self, property_obj: Property, portfolio: list[Property], context: dict[str, Any]) -> PortfolioBenchmarks:
        price_per_m2_values: list[float] = []
        for item in portfolio:
            value = self._property_price_per_m2(item)
            if value is not None:
                price_per_m2_values.append(value)

        if not price_per_m2_values and context.get("benchmark_price_per_m2") not in (None, ""):
            value = _safe_float(context.get("benchmark_price_per_m2"))
            if value is not None:
                price_per_m2_values.append(value)

        if not price_per_m2_values:
            return PortfolioBenchmarks(sample_size=len(portfolio))

        return PortfolioBenchmarks(
            average_price_per_m2=round(sum(price_per_m2_values) / len(price_per_m2_values), 2),
            median_price_per_m2=round(float(median(price_per_m2_values)), 2),
            sample_size=len(portfolio),
        )

    def _calculate_overall_score(self, factors: list[FactorResult]) -> int:
        total_weight = sum(max(0.0, factor.weight) for factor in factors)
        if total_weight <= 0:
            return 0
        weighted_total = sum(factor.score * max(0.0, factor.weight) for factor in factors)
        return _clamp_score(weighted_total / total_weight)

    def _calculate_contribution(self, score: int, weight: float) -> float:
        neutral = float(self.config.neutral_score)
        return round(((float(score) - neutral) / max(neutral, 1.0)) * float(weight), 2)

    def _attractiveness_label(self, overall_score: int, thresholds: dict[str, int] | None = None) -> str:
        active_thresholds = thresholds or self.config.attractiveness_thresholds
        if overall_score >= active_thresholds.get("medium", 70):
            return "High"
        if overall_score >= active_thresholds.get("low", 45):
            return "Medium"
        return "Low"

    def _select_top_factors(self, factors: list[FactorResult]) -> tuple[list[str], list[str]]:
        positive = sorted(factors, key=lambda item: item.contribution, reverse=True)
        negative = sorted(factors, key=lambda item: item.contribution)

        return [self._format_factor_line(factor) for factor in positive[:5]], [self._format_factor_line(factor) for factor in negative[:5]]

    def _format_factor_line(self, factor: FactorResult) -> str:
        value_text = _format_value(factor.value)
        benchmark_text = _format_value(factor.benchmark)
        extra = f" (benchmark {benchmark_text})" if benchmark_text else ""
        if value_text:
            return f"{factor.label}: {value_text}{extra} [{factor.score}/100]"
        return f"{factor.label}: {factor.explanation or 'No data'}{extra} [{factor.score}/100]"

    def _build_explanation(self, property_obj: Property, portfolio: PortfolioBenchmarks, factors: list[FactorResult], overall_score: int, attractiveness: str) -> str:
        price_per_m2 = self._property_price_per_m2(property_obj)
        benchmark = portfolio.average_price_per_m2
        if price_per_m2 is None:
            price_part = "Price per m² could not be calculated because asking price or living area is missing."
        elif benchmark is None:
            price_part = f"Price per m² is {price_per_m2:.2f}; no portfolio benchmark is available yet."
        else:
            delta_pct = round(((price_per_m2 - benchmark) / benchmark) * 100.0, 2) if benchmark > 0 else None
            if delta_pct is None:
                price_part = f"Price per m² is {price_per_m2:.2f}; benchmark is {benchmark:.2f}."
            else:
                price_part = f"Price per m² is {price_per_m2:.2f}, which is {delta_pct:.2f}% versus the portfolio average of {benchmark:.2f}."

        strongest_positive = next((factor for factor in sorted(factors, key=lambda item: item.contribution, reverse=True) if factor.contribution > 0), None)
        strongest_negative = next((factor for factor in sorted(factors, key=lambda item: item.contribution) if factor.contribution < 0), None)

        pieces = [
            f"Overall investment score: {overall_score}/100 ({attractiveness}).",
            price_part,
        ]
        if strongest_positive:
            pieces.append(f"Best driver: {strongest_positive.label}.")
        if strongest_negative:
            pieces.append(f"Main drag: {strongest_negative.label}.")
        return " ".join(pieces)

    def _property_price_per_m2(self, property_obj: Property) -> float | None:
        asking_price_per_m2 = _safe_float(property_obj.asking_price_per_m2)
        if asking_price_per_m2 is not None:
            return round(asking_price_per_m2, 2)
        price_per_m2 = _safe_float(property_obj.price_per_m2)
        if price_per_m2 is not None:
            return round(price_per_m2, 2)
        calculation_area_m2 = _safe_float(property_obj.calculation_area_m2)
        if _safe_float(property_obj.asking_price) is not None and calculation_area_m2 not in (None, 0):
            return round(float(property_obj.asking_price) / float(calculation_area_m2), 2)
        calculated = calculate_price_per_m2(property_obj.asking_price, property_obj.surface_m2, property_obj.asking_price_status)
        if calculated is not None:
            return calculated
        asking_price = _safe_float(property_obj.asking_price)
        surface_m2 = _safe_float(property_obj.surface_m2)
        if asking_price is None or surface_m2 in (None, 0):
            return None
        return round(asking_price / surface_m2, 2)

    def _property_woz_value(self, property_obj: Property) -> float | None:
        latest_woz_value = _safe_float(property_obj.latest_woz_value)
        if latest_woz_value is not None:
            return round(latest_woz_value, 2)
        return None

    def _property_building_year(self, property_obj: Property) -> int | None:
        return getattr(property_obj, "construction_year_bag", None) or property_obj.bag_building_year or property_obj.construction_year

    def _property_age(self, property_obj: Property) -> int | None:
        building_year = self._property_building_year(property_obj)
        if building_year in (None, 0):
            return None
        current_year = datetime.now(timezone.utc).year
        return max(0, current_year - int(building_year))

    def _benchmark_neighborhood_price_per_m2(self, property_obj: Property, portfolio: PortfolioBenchmarks, context: dict[str, Any]) -> float | None:
        benchmark = _safe_float(property_obj.neighborhood_m2_price_average)
        if benchmark is not None:
            return round(benchmark, 2)
        benchmark = _safe_float(context.get("neighborhood_m2_price_average"))
        if benchmark is not None:
            return round(benchmark, 2)
        return portfolio.average_price_per_m2

    def _score_asking_price_vs_portfolio_average(self, property_obj: Property, portfolio: PortfolioBenchmarks, context: dict[str, Any], config: InvestmentIntelligenceConfig) -> FactorResult:
        price_per_m2 = self._property_price_per_m2(property_obj)
        benchmark = self._benchmark_price_per_m2(portfolio, context)
        if price_per_m2 is None or benchmark in (None, 0):
            return self._neutral_result("asking_price_vs_portfolio_average", "Asking price vs portfolio average", "Insufficient benchmark data or missing price/surface information.", benchmark)

        if property_obj.surface_m2 in (None, 0) or property_obj.asking_price in (None, 0):
            return self._neutral_result("asking_price_vs_portfolio_average", "Asking price vs portfolio average", "Missing asking price or surface area.", benchmark)

        ratio = price_per_m2 / benchmark if benchmark else None
        if ratio is None:
            score = config.neutral_score
        elif ratio <= 0.8:
            score = 95
        elif ratio <= 0.9:
            score = 85
        elif ratio <= 1.0:
            score = 75
        elif ratio <= 1.1:
            score = 55
        elif ratio <= 1.2:
            score = 35
        else:
            score = 20

        delta_pct = round(((price_per_m2 - benchmark) / benchmark) * 100.0, 2) if benchmark else None
        return FactorResult(
            key="asking_price_vs_portfolio_average",
            label="Asking price vs portfolio average",
            score=score,
            weight=self.config.factor_weights.get("asking_price_vs_portfolio_average", 0.0),
            contribution=0.0,
            value=f"{price_per_m2:.2f}",
            benchmark=f"{benchmark:.2f}",
            explanation=f"Price per m² is {delta_pct:.2f}% versus the portfolio average." if delta_pct is not None else "Price comparison available.",
        )

    def _score_asking_price_vs_woz(self, property_obj: Property, portfolio: PortfolioBenchmarks, context: dict[str, Any], config: InvestmentIntelligenceConfig) -> FactorResult:
        asking_price = _safe_float(property_obj.asking_price)
        woz_value = self._property_woz_value(property_obj)
        if asking_price is None or woz_value in (None, 0):
            return self._neutral_result("asking_price_vs_woz", "Asking price vs WOZ", "Missing asking price or WOZ value.")

        ratio = asking_price / woz_value if woz_value else None
        if ratio is None:
            score = config.neutral_score
        elif ratio <= 0.75:
            score = 95
        elif ratio <= 0.85:
            score = 85
        elif ratio <= 1.0:
            score = 75
        elif ratio <= 1.1:
            score = 55
        elif ratio <= 1.25:
            score = 35
        else:
            score = 20

        delta_pct = round(((asking_price - woz_value) / woz_value) * 100.0, 2) if woz_value else None
        explanation = f"Asking price is {delta_pct:.2f}% versus WOZ." if delta_pct is not None else "Asking price compared to WOZ."
        return FactorResult(
            key="asking_price_vs_woz",
            label="Asking price vs WOZ",
            score=score,
            weight=self.config.factor_weights.get("asking_price_vs_woz", 0.0),
            contribution=0.0,
            value=f"{asking_price:.2f}",
            benchmark=f"{woz_value:.2f}",
            explanation=explanation,
        )

    def _score_living_area(self, property_obj: Property, portfolio: PortfolioBenchmarks, context: dict[str, Any], config: InvestmentIntelligenceConfig) -> FactorResult:
        area = _safe_float(property_obj.surface_m2)
        if area is None:
            return self._neutral_result("living_area", "Living area", "Missing living area.")

        if area < 35:
            score = 30
        elif area < 50:
            score = 45
        elif area < 70:
            score = 60
        elif area < 90:
            score = 75
        elif area < 120:
            score = 85
        elif area < 160:
            score = 90
        elif area < 220:
            score = 84
        else:
            score = 78

        return FactorResult(
            key="living_area",
            label="Living area",
            score=score,
            weight=self.config.factor_weights.get("living_area", 0.0),
            contribution=0.0,
            value=round(area, 1),
            benchmark="Market-size preference",
            explanation="Mid-sized homes tend to score best for liquidity and rental flexibility.",
        )

    def _score_plot_size(self, property_obj: Property, portfolio: PortfolioBenchmarks, context: dict[str, Any], config: InvestmentIntelligenceConfig) -> FactorResult:
        plot_size = _safe_float(property_obj.plot_size_m2)
        living_area = _safe_float(property_obj.surface_m2)
        if plot_size is None:
            return self._neutral_result("plot_size", "Plot size", "Missing plot size.")

        if living_area in (None, 0):
            ratio = None
        else:
            ratio = plot_size / living_area

        if ratio is None:
            score = 60 if plot_size >= 100 else 50
        elif ratio < 0.5:
            score = 35
        elif ratio < 1.0:
            score = 55
        elif ratio < 1.5:
            score = 70
        elif ratio < 3.0:
            score = 85
        else:
            score = 92

        if plot_size >= 500:
            score = min(100, score + 5)
        elif plot_size < 50:
            score = max(0, score - 5)

        ratio_text = f"ratio {ratio:.2f}" if ratio is not None else "no area ratio"
        return FactorResult(
            key="plot_size",
            label="Plot size",
            score=score,
            weight=self.config.factor_weights.get("plot_size", 0.0),
            contribution=0.0,
            value=f"{plot_size:.1f}",
            benchmark=ratio_text,
            explanation="Larger plots and stronger land-to-floor ratios support upside and flexibility.",
        )

    def _score_energy_label(self, property_obj: Property, portfolio: PortfolioBenchmarks, context: dict[str, Any], config: InvestmentIntelligenceConfig) -> FactorResult:
        label = _normalize_label(property_obj.energy_label)
        mapping = {
            "a+++": 100,
            "a++": 98,
            "a+": 96,
            "a": 92,
            "b": 82,
            "c": 70,
            "d": 55,
            "e": 40,
            "f": 25,
            "g": 10,
        }
        score = mapping.get(label)
        if score is None:
            return self._neutral_result("energy_label", "Energy label", "Missing or unknown energy label.")
        return FactorResult(
            key="energy_label",
            label="Energy label",
            score=score,
            weight=self.config.factor_weights.get("energy_label", 0.0),
            contribution=0.0,
            value=property_obj.energy_label,
            benchmark="Higher is better",
            explanation="Better energy labels usually reduce capex and improve marketability.",
        )

    def _score_building_age(self, property_obj: Property, portfolio: PortfolioBenchmarks, context: dict[str, Any], config: InvestmentIntelligenceConfig) -> FactorResult:
        year = self._property_building_year(property_obj)
        age = self._property_age(property_obj)
        if year in (None, 0) or age is None:
            return self._neutral_result("building_age", "Building age", "Missing building year.")

        if age <= 5:
            score = 95
        elif age <= 15:
            score = 88
        elif age <= 30:
            score = 80
        elif age <= 50:
            score = 65
        elif age <= 75:
            score = 50
        else:
            score = 35

        return FactorResult(
            key="building_age",
            label="Building age",
            score=score,
            weight=self.config.factor_weights.get("building_age", 0.0),
            contribution=0.0,
            value=age,
            benchmark=f"Built in {year}",
            explanation="Building age affects maintenance risk, financing ease, and retrofit appetite.",
        )

    def _score_construction_year(self, property_obj: Property, portfolio: PortfolioBenchmarks, context: dict[str, Any], config: InvestmentIntelligenceConfig) -> FactorResult:
        year = self._property_building_year(property_obj)
        if year in (None, 0):
            return self._neutral_result("construction_year", "Construction year", "Missing construction year.")

        if year >= 2020:
            score = 95
        elif year >= 2010:
            score = 90
        elif year >= 2000:
            score = 82
        elif year >= 1990:
            score = 75
        elif year >= 1980:
            score = 68
        elif year >= 1970:
            score = 60
        elif year >= 1950:
            score = 50
        elif year >= 1930:
            score = 42
        else:
            score = 35

        return FactorResult(
            key="construction_year",
            label="Construction year",
            score=score,
            weight=self.config.factor_weights.get("construction_year", 0.0),
            contribution=0.0,
            value=year,
            benchmark="Newer is generally easier to finance and maintain",
            explanation="Newer homes usually have lower near-term maintenance risk and stronger financing appeal.",
        )

    def _score_bedrooms(self, property_obj: Property, portfolio: PortfolioBenchmarks, context: dict[str, Any], config: InvestmentIntelligenceConfig) -> FactorResult:
        bedrooms = property_obj.bedrooms
        if bedrooms in (None, 0):
            return self._neutral_result("bedrooms", "Bedrooms", "Missing bedroom count.")

        if bedrooms == 1:
            score = 45
        elif bedrooms == 2:
            score = 70
        elif bedrooms == 3:
            score = 82
        elif bedrooms == 4:
            score = 90
        elif bedrooms == 5:
            score = 88
        elif bedrooms == 6:
            score = 84
        else:
            score = 78

        return FactorResult(
            key="bedrooms",
            label="Bedrooms",
            score=score,
            weight=self.config.factor_weights.get("bedrooms", 0.0),
            contribution=0.0,
            value=bedrooms,
            benchmark="3-5 bedrooms typically balance liquidity and rental demand",
            explanation="Bedroom count influences target buyer depth and rental flexibility.",
        )

    def _score_property_type(self, property_obj: Property, portfolio: PortfolioBenchmarks, context: dict[str, Any], config: InvestmentIntelligenceConfig) -> FactorResult:
        property_type = _normalize_type(property_obj.property_type)
        if not property_type:
            return self._neutral_result("property_type", "Property type", "Missing property type.")

        patterns = [
            ("vrijstaand", 92),
            ("detached", 92),
            ("villa", 88),
            ("woonboerderij", 84),
            ("farmhouse", 84),
            ("2 onder 1 kap", 86),
            ("halfvrijstaand", 86),
            ("semidetached", 86),
            ("hoekwoning", 80),
            ("end of terrace", 80),
            ("tussenwoning", 78),
            ("rijtjeshuis", 78),
            ("terraced", 78),
            ("penthouse", 80),
            ("maisonette", 74),
            ("appartement", 72),
            ("apartment", 72),
            ("studio", 60),
        ]
        score = 65
        for pattern, candidate_score in patterns:
            if pattern in property_type:
                score = candidate_score
                break

        return FactorResult(
            key="property_type",
            label="Property type",
            score=score,
            weight=self.config.factor_weights.get("property_type", 0.0),
            contribution=0.0,
            value=property_obj.property_type,
            benchmark="Type-specific liquidity and demand assumptions",
            explanation="Some property types are easier to finance, rent, or resell than others.",
        )

    def _score_price_per_m2(self, property_obj: Property, portfolio: PortfolioBenchmarks, context: dict[str, Any], config: InvestmentIntelligenceConfig) -> FactorResult:
        price_per_m2 = self._property_price_per_m2(property_obj)
        benchmark = self._benchmark_neighborhood_price_per_m2(property_obj, portfolio, context)
        if price_per_m2 is None or benchmark in (None, 0):
            return self._neutral_result("price_per_m2", "Price per m²", "Missing price per m² or portfolio benchmark.", benchmark)

        ratio = price_per_m2 / benchmark if benchmark else None
        if ratio is None:
            score = config.neutral_score
        elif ratio <= 0.8:
            score = 95
        elif ratio <= 0.9:
            score = 85
        elif ratio <= 1.0:
            score = 75
        elif ratio <= 1.1:
            score = 55
        elif ratio <= 1.2:
            score = 35
        else:
            score = 20

        delta_pct = round(((price_per_m2 - benchmark) / benchmark) * 100.0, 2) if benchmark else None
        explanation = f"Price per m² is {delta_pct:.2f}% versus the neighborhood average." if delta_pct is not None else "Price per m² compared to neighborhood benchmark."
        return FactorResult(
            key="price_per_m2",
            label="Price per m² vs neighborhood",
            score=score,
            weight=self.config.factor_weights.get("price_per_m2", 0.0),
            contribution=0.0,
            value=f"{price_per_m2:.2f}",
            benchmark=f"{benchmark:.2f}",
            explanation=explanation,
        )

    def _benchmark_price_per_m2(self, portfolio: PortfolioBenchmarks, context: dict[str, Any]) -> float | None:
        benchmark = _safe_float(context.get("benchmark_price_per_m2"))
        if benchmark is not None:
            return round(benchmark, 2)
        return portfolio.average_price_per_m2

    def _neutral_result(self, key: str, label: str, explanation: str, benchmark: Any = None) -> FactorResult:
        return FactorResult(
            key=key,
            label=label,
            score=self.config.neutral_score,
            weight=self.config.factor_weights.get(key, 0.0),
            contribution=0.0,
            value=None,
            benchmark=benchmark,
            explanation=explanation,
        )


def _format_value(value: Any) -> str:
    if value in (None, "", [], {}):
        return ""
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)
