"""Risk check: concentration downgrades on BUY, position facts on SELL."""

from __future__ import annotations

from datetime import date

import pytest
from conftest import make_portfolio

from stockbot.decision.risk import apply_risk_checks
from stockbot.models import Signal

AS_OF = date(2026, 7, 22)


def test_buy_downgraded_when_ticker_is_already_concentrated(cfg):
    # AAA is 20,000 of a 25,000 portfolio — well past the 15% ticker limit.
    portfolio = make_portfolio([("AAA", 200, 50.0, "Technology")], cash=5_000)
    signal, risk = apply_risk_checks(
        "AAA", Signal.BUY, portfolio, {"AAA": 100.0}, 100.0, "Technology", AS_OF, cfg
    )
    assert signal is Signal.WATCH
    assert risk.downgraded
    assert risk.original_signal is Signal.BUY
    assert any(b.startswith("ticker_concentration") for b in risk.breaches)


def test_buy_downgraded_on_sector_concentration(cfg):
    portfolio = make_portfolio(
        [("AAA", 100, 50.0, "Technology"), ("BBB", 100, 50.0, "Technology")], cash=1_000
    )
    prices = {"AAA": 50.0, "BBB": 50.0}
    # CCC is a new name — its own weight is 0, so only the sector limit can bite.
    signal, risk = apply_risk_checks(
        "CCC", Signal.BUY, portfolio, prices, 30.0, "Technology", AS_OF, cfg
    )
    assert signal is Signal.WATCH
    assert any(b.startswith("sector_concentration") for b in risk.breaches)


def test_buy_survives_when_within_limits(cfg):
    portfolio = make_portfolio([("AAA", 10, 50.0, "Technology")], cash=90_000)
    signal, risk = apply_risk_checks(
        "CCC", Signal.BUY, portfolio, {"AAA": 50.0}, 30.0, "Healthcare", AS_OF, cfg
    )
    assert signal is Signal.BUY
    assert not risk.downgraded
    assert risk.breaches == []


def test_sell_attaches_cost_basis_and_holding_period(cfg):
    portfolio = make_portfolio(
        [("AAA", 100, 40.0, "Technology")], cash=1_000, acquired=date(2024, 1, 10)
    )
    signal, risk = apply_risk_checks(
        "AAA", Signal.SELL, portfolio, {"AAA": 60.0}, 60.0, "Technology", AS_OF, cfg
    )
    assert signal is Signal.SELL
    p = risk.position
    assert p is not None
    assert p.cost_basis_per_share == 40.0
    assert p.total_cost == pytest.approx(4_000.0)
    assert p.market_value == pytest.approx(6_000.0)
    assert p.unrealized_pnl == pytest.approx(2_000.0)
    assert p.unrealized_pnl_pct == pytest.approx(0.5)
    assert p.holding_period_days == (AS_OF - date(2024, 1, 10)).days
    assert p.term == "long"


def test_short_term_holding_is_labelled(cfg):
    portfolio = make_portfolio(
        [("AAA", 10, 40.0, "Technology")], cash=1_000, acquired=date(2026, 5, 1)
    )
    _, risk = apply_risk_checks(
        "AAA", Signal.SELL, portfolio, {"AAA": 60.0}, 60.0, "Technology", AS_OF, cfg
    )
    assert risk.position.term == "short"


def test_unknown_acquisition_date_leaves_term_unset(cfg):
    portfolio = make_portfolio([("AAA", 10, 40.0, "Technology")], cash=1_000)
    portfolio.holdings[0].acquired_date = None
    _, risk = apply_risk_checks(
        "AAA", Signal.SELL, portfolio, {"AAA": 60.0}, 60.0, "Technology", AS_OF, cfg
    )
    assert risk.position.holding_period_days is None
    assert risk.position.term is None


def test_risk_check_does_not_touch_hold_or_watch(cfg):
    portfolio = make_portfolio([("AAA", 1000, 50.0, "Technology")], cash=0)
    for original in (Signal.HOLD, Signal.WATCH):
        signal, risk = apply_risk_checks(
            "AAA", original, portfolio, {"AAA": 100.0}, 100.0, "Technology", AS_OF, cfg
        )
        assert signal is original
        assert not risk.downgraded


def test_concentration_unknown_when_portfolio_value_is_zero(cfg):
    portfolio = make_portfolio([], cash=0.0)
    signal, risk = apply_risk_checks(
        "AAA", Signal.BUY, portfolio, {}, 100.0, "Technology", AS_OF, cfg
    )
    assert signal is Signal.WATCH
    assert any(b.startswith("portfolio_value_unknown") for b in risk.breaches)
