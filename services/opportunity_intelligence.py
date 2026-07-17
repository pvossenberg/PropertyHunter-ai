from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Protocol

from models.property import Property
from services.investment_intelligence import InvestmentIntelligenceEngine, InvestmentIntelligenceResult


def _safe_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _normalize_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.lower().split())


def _normalize_type(value: Any) -> str:
    return _normalize_text(value).replace("_", " ").replace("-", " ")


def _contains_any(text: str, terms: list[str]) -> bool:
    return any(term in text for term in terms)


def _match_terms(text: str, terms: list[str]) -> list[str]:
    return [term for term in terms if term in text]


@dataclass(frozen=True)
class OpportunityFinding:
    opportunity_type: str
    confidence: int
    explanation: str
    required_data: list[str] = field(default_factory=list)
    missing_data: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class OpportunityIntelligenceResult:
    overall_investment_score: int
    opportunity_score: int
    detected_opportunities: list[OpportunityFinding] = field(default_factory=list)
    explanation: str = ""
    investment_result: InvestmentIntelligenceResult | None = None
    portfolio_size: int = 0


@dataclass(frozen=True)
class OpportunitySignals:
    living_area: float | None
    plot_size: float | None
    bedrooms: int | None
    asking_price: float | None
    price_per_m2: float | None
    benchmark_price_per_m2: float | None
    price_per_m2_delta_pct: float | None
    construction_year: int | None
    energy_label: str
    property_type: str
    raw_text: str
    description: str
    has_multiple_entrances: bool | None
    has_flat_roof: bool | None
    detached_indicator: bool
    semi_detached_indicator: bool
    renovation_keywords: list[str]
    remaining_outdoor_space_m2: float | None
    outdoor_space_ratio: float | None


OpportunityDetector = Callable[[Property, InvestmentIntelligenceResult, OpportunitySignals, "OpportunityIntelligenceConfig"], OpportunityFinding | None]


@dataclass(frozen=True)
class OpportunityRuleDefinition:
    opportunity_type: str
    required_data: list[str]
    detector: OpportunityDetector


@dataclass
class OpportunityIntelligenceConfig:
    min_confidence: int = 35
    confidence_thresholds: dict[str, int] = field(
        default_factory=lambda: {
            "possible_split_opportunity": 45,
            "possible_extension_opportunity": 40,
            "possible_rooftop_extension": 40,
            "renovation_opportunity": 40,
            "rental_opportunity": 40,
            "flip_opportunity": 45,
        }
    )


class OpportunityRule(Protocol):
    def detect(self, property_obj: Property, investment_result: InvestmentIntelligenceResult, signals: OpportunitySignals, config: OpportunityIntelligenceConfig) -> OpportunityFinding | None:
        ...


class OpportunityIntelligenceEngine:
    def __init__(self, investment_engine: InvestmentIntelligenceEngine | None = None, config: OpportunityIntelligenceConfig | None = None, rule_definitions: Iterable[OpportunityRuleDefinition] | None = None):
        self.investment_engine = investment_engine or InvestmentIntelligenceEngine()
        self.config = config or OpportunityIntelligenceConfig()
        self.rule_definitions = list(rule_definitions or self._build_default_rule_definitions())

    def evaluate(self, property_obj: Property, portfolio: Iterable[Property] | None = None, context: dict[str, Any] | None = None, investment_result: InvestmentIntelligenceResult | None = None) -> OpportunityIntelligenceResult:
        active_context = dict(context or {})
        investment_result = investment_result or self.investment_engine.evaluate(property_obj, portfolio=portfolio, context=active_context)
        signals = self._build_signals(property_obj, investment_result, active_context)

        detected_opportunities: list[OpportunityFinding] = []
        for rule_definition in self.rule_definitions:
            opportunity = rule_definition.detector(property_obj, investment_result, signals, self.config)
            if opportunity is not None:
                detected_opportunities.append(opportunity)

        detected_opportunities.sort(key=lambda item: item.confidence, reverse=True)
        opportunity_score = self._calculate_opportunity_score(detected_opportunities)
        explanation = self._build_summary(investment_result, detected_opportunities)

        return OpportunityIntelligenceResult(
            overall_investment_score=investment_result.overall_score,
            opportunity_score=opportunity_score,
            detected_opportunities=detected_opportunities,
            explanation=explanation,
            investment_result=investment_result,
            portfolio_size=investment_result.portfolio_size,
        )

    def evaluate_many(self, properties: Iterable[Property], context: dict[str, Any] | None = None) -> list[OpportunityIntelligenceResult]:
        property_list = list(properties)
        investment_results = self.investment_engine.evaluate_many(property_list, context=context)
        return [self.evaluate(property_obj, context=context, investment_result=investment_result) for property_obj, investment_result in zip(property_list, investment_results, strict=True)]

    def _build_default_rule_definitions(self) -> list[OpportunityRuleDefinition]:
        return [
            OpportunityRuleDefinition(
                opportunity_type="Possible Split Opportunity",
                required_data=["living area", "bedrooms", "multiple entrances", "property type"],
                detector=self._detect_split_opportunity,
            ),
            OpportunityRuleDefinition(
                opportunity_type="Possible Extension Opportunity",
                required_data=["plot size", "living area", "property type", "remaining outdoor space"],
                detector=self._detect_extension_opportunity,
            ),
            OpportunityRuleDefinition(
                opportunity_type="Possible Rooftop Extension",
                required_data=["flat roof", "construction year", "property type"],
                detector=self._detect_rooftop_extension,
            ),
            OpportunityRuleDefinition(
                opportunity_type="Renovation Opportunity",
                required_data=["construction year", "energy label", "price per m²"],
                detector=self._detect_renovation_opportunity,
            ),
            OpportunityRuleDefinition(
                opportunity_type="Rental Opportunity",
                required_data=["bedrooms", "price per m²", "portfolio benchmark"],
                detector=self._detect_rental_opportunity,
            ),
            OpportunityRuleDefinition(
                opportunity_type="Flip Opportunity",
                required_data=["price per m²", "construction year", "energy label", "renovation indicators"],
                detector=self._detect_flip_opportunity,
            ),
        ]

    def _build_signals(self, property_obj: Property, investment_result: InvestmentIntelligenceResult, context: dict[str, Any]) -> OpportunitySignals:
        raw_text = _normalize_text(property_obj.raw_text)
        description = _normalize_text(property_obj.description)
        combined_text = f"{raw_text} {description}".strip()

        plot_size = _safe_float(property_obj.plot_size_m2)
        living_area = _safe_float(property_obj.calculation_area_m2) or _safe_float(property_obj.surface_m2)
        remaining_outdoor_space_m2 = None
        outdoor_space_ratio = None
        if plot_size is not None and plot_size > 0 and living_area is not None and living_area > 0:
            remaining_outdoor_space_m2 = round(max(0.0, plot_size - living_area), 2)
            outdoor_space_ratio = round(plot_size / living_area, 2)

        price_per_m2 = _safe_float(investment_result.price_per_m2)
        benchmark_price_per_m2 = _safe_float(investment_result.benchmarks.average_price_per_m2)
        price_per_m2_delta_pct = None
        if price_per_m2 is not None and benchmark_price_per_m2 not in (None, 0):
            price_per_m2_delta_pct = round(((price_per_m2 - benchmark_price_per_m2) / benchmark_price_per_m2) * 100.0, 2)

        multiple_entrances_terms = [
            "multiple entrances",
            "multiple entrance",
            "two entrances",
            "2 entrances",
            "twee ingangen",
            "twee entrees",
            "eigen entree",
            "separate entrance",
            "separate entrances",
            "private entrance",
            "aparte entree",
        ]
        flat_roof_terms = [
            "plat dak",
            "flat roof",
            "dakopbouw",
            "dakterras",
            "roof terrace",
        ]
        renovation_terms = [
            "kluswoning",
            "opknapper",
            "opknap",
            "renovatie",
            "te moderniseren",
            "te renoveren",
            "sloop",
            "shell",
        ]

        property_type = _normalize_type(property_obj.property_type)
        detached_indicator = _contains_any(property_type, ["vrijstaand", "detached", "villa", "woonboerderij"])
        semi_detached_indicator = _contains_any(property_type, ["2 onder 1 kap", "2 onder een kap", "halfvrijstaand", "semi", "semidetached"])

        has_multiple_entrances = None
        if combined_text:
            has_multiple_entrances = _contains_any(combined_text, multiple_entrances_terms)

        has_flat_roof = None
        if combined_text:
            has_flat_roof = _contains_any(combined_text, flat_roof_terms)

        renovation_keywords = _match_terms(combined_text, renovation_terms)

        return OpportunitySignals(
            living_area=living_area,
            plot_size=plot_size,
            bedrooms=_safe_int(property_obj.bedrooms),
            asking_price=_safe_float(property_obj.asking_price),
            price_per_m2=price_per_m2,
            benchmark_price_per_m2=benchmark_price_per_m2,
            price_per_m2_delta_pct=price_per_m2_delta_pct,
            construction_year=_safe_int(getattr(property_obj, "construction_year_bag", None) or property_obj.bag_building_year or property_obj.construction_year),
            energy_label=_normalize_text(property_obj.energy_label),
            property_type=property_type,
            raw_text=raw_text,
            description=description,
            has_multiple_entrances=has_multiple_entrances,
            has_flat_roof=has_flat_roof,
            detached_indicator=detached_indicator,
            semi_detached_indicator=semi_detached_indicator,
            renovation_keywords=renovation_keywords,
            remaining_outdoor_space_m2=remaining_outdoor_space_m2,
            outdoor_space_ratio=outdoor_space_ratio,
        )

    def _calculate_opportunity_score(self, opportunities: list[OpportunityFinding]) -> int:
        if not opportunities:
            return 0
        strongest = max(opportunity.confidence for opportunity in opportunities)
        breadth_bonus = max(0, len(opportunities) - 1) * 5
        return min(100, strongest + breadth_bonus)

    def _build_summary(self, investment_result: InvestmentIntelligenceResult, opportunities: list[OpportunityFinding]) -> str:
        if not opportunities:
            return f"Investment score {investment_result.overall_score}/100. No clear opportunity pattern was detected with the current data."

        top = opportunities[0]
        labels = ", ".join(opportunity.opportunity_type for opportunity in opportunities[:3])
        return f"Investment score {investment_result.overall_score}/100. Top signal: {top.opportunity_type} ({top.confidence}/100). Detected: {labels}."

    def _emit_if_confident(self, opportunity_type: str, confidence: int, explanation: str, required_data: list[str], missing_data: list[str]) -> OpportunityFinding | None:
        if confidence < self.config.min_confidence:
            return None
        return OpportunityFinding(
            opportunity_type=opportunity_type,
            confidence=max(0, min(100, confidence)),
            explanation=explanation,
            required_data=required_data,
            missing_data=missing_data,
        )

    def _detect_split_opportunity(self, property_obj: Property, investment_result: InvestmentIntelligenceResult, signals: OpportunitySignals, config: OpportunityIntelligenceConfig) -> OpportunityFinding | None:
        confidence = 0
        missing_data: list[str] = []
        notes: list[str] = []

        if signals.living_area is None:
            missing_data.append("living area")
        elif signals.living_area >= 180:
            confidence += 35
            notes.append(f"large living area ({signals.living_area:.0f} m²)")
        elif signals.living_area >= 140:
            confidence += 28
            notes.append(f"large living area ({signals.living_area:.0f} m²)")
        elif signals.living_area >= 110:
            confidence += 18
            notes.append(f"moderate living area ({signals.living_area:.0f} m²)")

        if signals.bedrooms is None:
            missing_data.append("many bedrooms")
        elif signals.bedrooms >= 5:
            confidence += 30
            notes.append(f"{signals.bedrooms} bedrooms")
        elif signals.bedrooms >= 4:
            confidence += 24
            notes.append(f"{signals.bedrooms} bedrooms")
        elif signals.bedrooms >= 3:
            confidence += 12
            notes.append(f"{signals.bedrooms} bedrooms")

        if signals.has_multiple_entrances is None:
            missing_data.append("multiple entrances")
        elif signals.has_multiple_entrances:
            confidence += 30
            notes.append("multiple entrances referenced in listing text")

        if _contains_any(signals.property_type, ["huis", "woning", "villa", "semi", "halfvrijstaand", "detached", "maisonette"]):
            confidence += 8

        explanation = "Split potential suggested by " + ", ".join(notes) if notes else "Split potential is only weakly supported by the current data."
        return self._emit_if_confident("Possible Split Opportunity", confidence, explanation, ["living area", "bedrooms", "multiple entrances"], missing_data)

    def _detect_extension_opportunity(self, property_obj: Property, investment_result: InvestmentIntelligenceResult, signals: OpportunitySignals, config: OpportunityIntelligenceConfig) -> OpportunityFinding | None:
        confidence = 0
        missing_data: list[str] = []
        notes: list[str] = []

        if signals.plot_size is None:
            missing_data.append("plot size")
        elif signals.plot_size >= 600:
            confidence += 35
            notes.append(f"large plot ({signals.plot_size:.0f} m²)")
        elif signals.plot_size >= 350:
            confidence += 26
            notes.append(f"large plot ({signals.plot_size:.0f} m²)")
        elif signals.plot_size >= 200:
            confidence += 16
            notes.append(f"usable plot ({signals.plot_size:.0f} m²)")

        if signals.detached_indicator:
            confidence += 18
            notes.append("detached property type")
        elif signals.semi_detached_indicator:
            confidence += 14
            notes.append("semi-detached property type")
        else:
            missing_data.append("detached or semi-detached type")

        if signals.remaining_outdoor_space_m2 is None:
            missing_data.append("remaining outdoor space")
        elif signals.remaining_outdoor_space_m2 >= 200:
            confidence += 26
            notes.append(f"substantial remaining outdoor space ({signals.remaining_outdoor_space_m2:.0f} m²)")
        elif signals.remaining_outdoor_space_m2 >= 100:
            confidence += 16
            notes.append(f"remaining outdoor space ({signals.remaining_outdoor_space_m2:.0f} m²)")
        elif signals.remaining_outdoor_space_m2 >= 50:
            confidence += 8

        if signals.outdoor_space_ratio is not None and signals.outdoor_space_ratio >= 2.0:
            confidence += 10

        explanation = "Extension potential suggested by " + ", ".join(notes) if notes else "Extension potential is only weakly supported by the current data."
        return self._emit_if_confident("Possible Extension Opportunity", confidence, explanation, ["plot size", "detached or semi-detached type", "remaining outdoor space"], missing_data)

    def _detect_rooftop_extension(self, property_obj: Property, investment_result: InvestmentIntelligenceResult, signals: OpportunitySignals, config: OpportunityIntelligenceConfig) -> OpportunityFinding | None:
        confidence = 0
        missing_data: list[str] = []
        notes: list[str] = []

        if signals.has_flat_roof is None:
            missing_data.append("flat roof")
        elif signals.has_flat_roof:
            confidence += 40
            notes.append("flat roof referenced in listing text")

        if signals.construction_year is None:
            missing_data.append("construction year")
        elif 1950 <= signals.construction_year <= 2005:
            confidence += 18
            notes.append(f"construction year {signals.construction_year}")
        elif signals.construction_year < 1950:
            confidence += 8

        if _contains_any(signals.property_type, ["huis", "woning", "appartement", "portiekflat", "galerijflat", "penthouse"]):
            confidence += 8

        if _contains_any(signals.raw_text, ["dakopbouw", "dakterras", "plat dak", "roof terrace"]):
            confidence += 12
            notes.append("roof-related clue in listing text")

        explanation = "Rooftop extension potential suggested by " + ", ".join(notes) if notes else "Rooftop extension potential is only weakly supported by the current data."
        return self._emit_if_confident("Possible Rooftop Extension", confidence, explanation, ["flat roof", "construction year", "property type"], missing_data)

    def _detect_renovation_opportunity(self, property_obj: Property, investment_result: InvestmentIntelligenceResult, signals: OpportunitySignals, config: OpportunityIntelligenceConfig) -> OpportunityFinding | None:
        confidence = 0
        missing_data: list[str] = []
        notes: list[str] = []

        if signals.construction_year is None:
            missing_data.append("construction year")
        elif signals.construction_year <= 1940:
            confidence += 32
            notes.append(f"old construction year {signals.construction_year}")
        elif signals.construction_year <= 1970:
            confidence += 24
            notes.append(f"older construction year {signals.construction_year}")
        elif signals.construction_year <= 1985:
            confidence += 14

        if not signals.energy_label:
            missing_data.append("energy label")
        elif signals.energy_label in {"g", "f", "e"}:
            confidence += 30
            notes.append(f"poor energy label {signals.energy_label.upper()}")
        elif signals.energy_label == "d":
            confidence += 18
            notes.append(f"moderate energy label {signals.energy_label.upper()}")

        if signals.price_per_m2 is None:
            missing_data.append("price per m²")
        elif signals.price_per_m2_delta_pct is not None and signals.price_per_m2_delta_pct <= -15:
            confidence += 28
            notes.append(f"price per m² {abs(signals.price_per_m2_delta_pct):.0f}% below benchmark")
        elif signals.price_per_m2_delta_pct is not None and signals.price_per_m2_delta_pct <= -5:
            confidence += 14

        if signals.renovation_keywords:
            confidence += 18
            notes.append(f"renovation-related wording ({', '.join(signals.renovation_keywords[:2])})")

        explanation = "Renovation potential suggested by " + ", ".join(notes) if notes else "Renovation potential is only weakly supported by the current data."
        return self._emit_if_confident("Renovation Opportunity", confidence, explanation, ["construction year", "energy label", "price per m²"], missing_data)

    def _detect_rental_opportunity(self, property_obj: Property, investment_result: InvestmentIntelligenceResult, signals: OpportunitySignals, config: OpportunityIntelligenceConfig) -> OpportunityFinding | None:
        confidence = 0
        missing_data: list[str] = []
        notes: list[str] = []

        if signals.bedrooms is None:
            missing_data.append("bedrooms")
        elif signals.bedrooms >= 5:
            confidence += 30
            notes.append(f"{signals.bedrooms} bedrooms")
        elif signals.bedrooms == 4:
            confidence += 22
            notes.append(f"{signals.bedrooms} bedrooms")
        elif signals.bedrooms == 3:
            confidence += 12
            notes.append(f"{signals.bedrooms} bedrooms")

        if signals.price_per_m2 is None:
            missing_data.append("price per m²")
        elif signals.price_per_m2_delta_pct is not None and signals.price_per_m2_delta_pct <= -10:
            confidence += 34
            notes.append("attractive price per m²")
        elif signals.price_per_m2_delta_pct is not None and signals.price_per_m2_delta_pct <= -5:
            confidence += 18

        if _contains_any(signals.property_type, ["appartement", "huis", "woning", "maisonette", "portiekflat", "galerijflat"]):
            confidence += 8

        explanation = "Rental potential suggested by " + ", ".join(notes) if notes else "Rental potential is only weakly supported by the current data."
        return self._emit_if_confident("Rental Opportunity", confidence, explanation, ["bedrooms", "price per m²", "portfolio benchmark"], missing_data)

    def _detect_flip_opportunity(self, property_obj: Property, investment_result: InvestmentIntelligenceResult, signals: OpportunitySignals, config: OpportunityIntelligenceConfig) -> OpportunityFinding | None:
        confidence = 0
        missing_data: list[str] = []
        notes: list[str] = []

        if signals.price_per_m2 is None:
            missing_data.append("price per m²")
        elif signals.price_per_m2_delta_pct is not None and signals.price_per_m2_delta_pct <= -15:
            confidence += 36
            notes.append("below-average price per m²")
        elif signals.price_per_m2_delta_pct is not None and signals.price_per_m2_delta_pct <= -8:
            confidence += 24
            notes.append("slightly below-average price per m²")

        renovation_score = 0
        if signals.construction_year is None:
            missing_data.append("construction year")
        elif signals.construction_year <= 1965:
            renovation_score += 22
        elif signals.construction_year <= 1980:
            renovation_score += 14

        if not signals.energy_label:
            missing_data.append("energy label")
        elif signals.energy_label in {"g", "f", "e"}:
            renovation_score += 22
        elif signals.energy_label == "d":
            renovation_score += 12

        if signals.renovation_keywords:
            renovation_score += 20
            notes.append("renovation cues in the listing text")

        if renovation_score >= 20:
            confidence += renovation_score

        if _contains_any(signals.property_type, ["appartement", "huis", "woning", "maisonette", "portiekflat", "galerijflat"]):
            confidence += 8

        explanation = "Flip potential suggested by " + ", ".join(notes) if notes else "Flip potential is only weakly supported by the current data."
        return self._emit_if_confident("Flip Opportunity", confidence, explanation, ["price per m²", "construction year", "energy label", "renovation indicators"], missing_data)

