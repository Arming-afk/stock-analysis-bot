"""Data structures passed between pipeline stages.

Every field that carries a number is produced by deterministic code. The only
LLM-authored fields in this module are `ArticleSentiment.label` (a constrained
enum, validated on the way in) and `TickerReport.rationale` (prose, which no
downstream stage reads).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any


class Signal(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"
    WATCH = "WATCH"


class Sentiment(str, Enum):
    POSITIVE = "positive"
    NEUTRAL = "neutral"
    NEGATIVE = "negative"


class Strength(str, Enum):
    WEAK = "weak"
    MODERATE = "moderate"
    STRONG = "strong"


class ValuationBucket(str, Enum):
    UNDERVALUED_LARGE = "undervalued_large"
    UNDERVALUED_MILD = "undervalued_mild"
    NEAR_FAIR = "near_fair"
    OVERVALUED = "overvalued"
    NOT_APPLICABLE = "not_applicable"


class ConfidenceBand(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


# --------------------------------------------------------------------------
# Ingestion
# --------------------------------------------------------------------------


@dataclass
class Holding:
    ticker: str
    quantity: float
    cost_basis_per_share: float
    acquired_date: date | None = None
    sector: str | None = None

    @property
    def total_cost(self) -> float:
        return self.quantity * self.cost_basis_per_share

    def holding_period_days(self, as_of: date) -> int | None:
        if self.acquired_date is None:
            return None
        return (as_of - self.acquired_date).days


@dataclass
class Portfolio:
    holdings: list[Holding] = field(default_factory=list)
    cash: float = 0.0
    as_of: date = field(default_factory=date.today)
    source: str = "unknown"

    def get(self, ticker: str) -> Holding | None:
        for h in self.holdings:
            if h.ticker.upper() == ticker.upper():
                return h
        return None

    def holds(self, ticker: str) -> bool:
        h = self.get(ticker)
        return h is not None and h.quantity > 0


@dataclass
class FcfYear:
    """One fiscal year of free cash flow. FCF = operating cash flow - capex."""

    fiscal_year: int
    operating_cash_flow: float
    capital_expenditure: float

    @property
    def free_cash_flow(self) -> float:
        return self.operating_cash_flow - self.capital_expenditure


@dataclass
class Fundamentals:
    ticker: str
    price: float
    shares_outstanding: float
    fcf_history: list[FcfYear] = field(default_factory=list)
    total_debt: float = 0.0
    cash_and_equivalents: float = 0.0
    beta: float | None = None
    market_cap: float | None = None
    sector: str | None = None
    currency: str = "USD"
    fetch_errors: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# Valuation
# --------------------------------------------------------------------------


@dataclass
class GateResult:
    """Result of the DCF-applicability gate. Runs before any DCF math."""

    applicable: bool
    reason: str
    years_used: int = 0
    negative_years: int = 0
    fcf_cv: float | None = None
    fcf_series: list[float] = field(default_factory=list)


@dataclass
class SensitivityResult:
    """Grid of fair values under +/-1% and +/-2% growth and discount shifts."""

    fair_values: list[float]
    gaps: list[float]
    mean_fair_value: float
    cv: float
    sign_agreement: float  # fraction of the grid whose gap sign matches the base case
    min_fair_value: float
    max_fair_value: float


@dataclass
class DCFResult:
    ticker: str
    gate: GateResult
    # Everything below is None when gate.applicable is False. No DCF math is
    # run at all for a ticker that failed the gate.
    fair_value: float | None = None
    valuation_gap_pct: float | None = None
    base_fcf: float | None = None
    growth_rate: float | None = None
    discount_rate: float | None = None
    terminal_growth: float | None = None
    projected_fcf: list[float] = field(default_factory=list)
    pv_projected_fcf: list[float] = field(default_factory=list)
    terminal_value: float | None = None
    pv_terminal_value: float | None = None
    enterprise_value: float | None = None
    net_debt: float | None = None
    equity_value: float | None = None
    wacc_estimated: bool = False
    sensitivity: SensitivityResult | None = None

    @property
    def applicable(self) -> bool:
        return self.gate.applicable


# --------------------------------------------------------------------------
# News
# --------------------------------------------------------------------------


@dataclass
class Article:
    title: str
    source: str
    url: str = ""
    published_at: datetime | None = None
    summary: str = ""


@dataclass
class ArticleSentiment:
    """The single place an LLM's output enters the numeric pipeline.

    `label` is validated against the Sentiment enum before construction; an
    unparseable LLM response is dropped, never coerced to neutral silently.
    """

    article: Article
    label: Sentiment


@dataclass
class NewsResult:
    ticker: str
    news_available: bool
    reason: str = ""
    articles: list[ArticleSentiment] = field(default_factory=list)
    source_count: int = 0
    distinct_sources: int = 0
    # Aggregate is computed in code from the per-article labels, not asked of the LLM.
    aggregate_score: float = 0.0  # [-1, 1]
    aggregate_label: Sentiment = Sentiment.NEUTRAL
    aggregate_strength: Strength = Strength.WEAK
    agreement_ratio: float = 0.0  # fraction of articles agreeing with the majority
    assumed_neutral: bool = False  # True when news_available is False


# --------------------------------------------------------------------------
# Decision / confidence / risk
# --------------------------------------------------------------------------


@dataclass
class Decision:
    ticker: str
    signal: Signal
    rule: str  # which matrix row fired — makes every decision auditable
    valuation_bucket: ValuationBucket
    sentiment_label: Sentiment
    sentiment_strength: Strength
    held: bool
    flags: list[str] = field(default_factory=list)


@dataclass
class ConfidenceResult:
    value: float
    band: ConfidenceBand
    dcf_confidence: float | None
    news_confidence: float
    agreement_score: float
    news_baseline_applied: bool = False
    notes: list[str] = field(default_factory=list)


@dataclass
class PositionInfo:
    """Attached to SELL signals so tax impact can be weighed manually.

    Deliberately contains no tax logic — it surfaces facts only.
    """

    quantity: float
    cost_basis_per_share: float
    total_cost: float
    market_value: float
    unrealized_pnl: float
    unrealized_pnl_pct: float
    holding_period_days: int | None
    term: str | None  # "short" | "long" | None when the acquisition date is unknown


@dataclass
class RiskResult:
    downgraded: bool = False
    original_signal: Signal | None = None
    breaches: list[str] = field(default_factory=list)
    ticker_weight: float | None = None
    sector_weight: float | None = None
    position: PositionInfo | None = None


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------


@dataclass
class TickerReport:
    ticker: str
    price: float
    signal: Signal
    decision: Decision
    dcf: DCFResult
    news: NewsResult
    risk: RiskResult
    confidence: ConfidenceResult | None = None  # None in phase 0
    rationale: str = ""  # LLM prose. Nothing downstream reads this.
    rationale_source: str = "none"  # "llm" | "fallback" | "none"
    errors: list[str] = field(default_factory=list)


@dataclass
class DailyReport:
    run_date: date
    generated_at: datetime
    phase: int
    tickers: list[TickerReport] = field(default_factory=list)
    portfolio_value: float = 0.0
    cash: float = 0.0
    portfolio_source: str = "unknown"
    errors: list[str] = field(default_factory=list)

    def by_signal(self, signal: Signal) -> list[TickerReport]:
        return [t for t in self.tickers if t.signal == signal]


# --------------------------------------------------------------------------
# Serialization
# --------------------------------------------------------------------------


def _json_default(obj: Any) -> Any:
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, (date, datetime)):
        return obj.isoformat()
    raise TypeError(f"not JSON serializable: {type(obj)!r}")


def to_dict(obj: Any) -> Any:
    """dataclass -> plain dict with enums/dates flattened, ready for JSON."""
    return json.loads(json.dumps(asdict(obj), default=_json_default))


def to_json(obj: Any, indent: int | None = 2) -> str:
    return json.dumps(asdict(obj), default=_json_default, indent=indent)
