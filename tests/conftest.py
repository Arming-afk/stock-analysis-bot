from __future__ import annotations

import copy
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from stockbot.config import Config, Secrets, load_config  # noqa: E402
from stockbot.models import (  # noqa: E402
    Article,
    ArticleSentiment,
    FcfYear,
    Fundamentals,
    Holding,
    NewsResult,
    Portfolio,
    Sentiment,
    Strength,
)


@pytest.fixture
def cfg() -> Config:
    """The real shipped config, so tests exercise the thresholds we actually run."""
    return load_config(ROOT / "config.yaml")


@pytest.fixture
def cfg_phase1(cfg: Config) -> Config:
    data = copy.deepcopy(cfg.as_dict())
    data["phase"] = 1
    return Config(data, Secrets())


def make_fundamentals(
    ticker: str = "TEST",
    price: float = 100.0,
    fcf: list[float] | None = None,
    shares: float = 1_000_000_000,
    total_debt: float = 10_000_000_000,
    cash: float = 5_000_000_000,
    beta: float | None = 1.0,
    market_cap: float | None = None,
    sector: str | None = "Technology",
) -> Fundamentals:
    """FCF list is newest-first, in dollars."""
    fcf = fcf if fcf is not None else [10e9, 9.5e9, 9.0e9, 8.5e9, 8.0e9]
    history = [
        FcfYear(fiscal_year=2025 - i, operating_cash_flow=v, capital_expenditure=0.0)
        for i, v in enumerate(fcf)
    ]
    return Fundamentals(
        ticker=ticker,
        price=price,
        shares_outstanding=shares,
        fcf_history=history,
        total_debt=total_debt,
        cash_and_equivalents=cash,
        beta=beta,
        market_cap=market_cap if market_cap is not None else price * shares,
        sector=sector,
    )


def make_news(
    ticker: str = "TEST",
    available: bool = True,
    label: Sentiment = Sentiment.NEUTRAL,
    strength: Strength = Strength.WEAK,
    score: float = 0.0,
    n_articles: int = 4,
    distinct_sources: int = 4,
    agreement: float = 1.0,
) -> NewsResult:
    if not available:
        return NewsResult(
            ticker=ticker,
            news_available=False,
            reason="no_sources: 0 article(s) in lookback window",
            assumed_neutral=True,
            aggregate_label=Sentiment.NEUTRAL,
            aggregate_strength=Strength.WEAK,
        )
    articles = [
        ArticleSentiment(Article(title=f"headline {i}", source=f"src{i}"), label)
        for i in range(n_articles)
    ]
    return NewsResult(
        ticker=ticker,
        news_available=True,
        reason="ok",
        articles=articles,
        source_count=n_articles,
        distinct_sources=distinct_sources,
        aggregate_score=score,
        aggregate_label=label,
        aggregate_strength=strength,
        agreement_ratio=agreement,
    )


def make_portfolio(
    holdings: list[tuple[str, float, float, str]] | None = None,
    cash: float = 10_000.0,
    acquired: date | None = None,
) -> Portfolio:
    """holdings: (ticker, qty, cost_basis, sector)"""
    holdings = holdings or []
    return Portfolio(
        holdings=[
            Holding(
                ticker=t,
                quantity=q,
                cost_basis_per_share=c,
                acquired_date=acquired or date(2020, 1, 1),
                sector=s,
            )
            for t, q, c, s in holdings
        ],
        cash=cash,
        as_of=date(2026, 7, 22),
    )
