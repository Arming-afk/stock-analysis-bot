"""DCF gate and valuation. The gate tests are the important ones — they are the
guard that keeps unsuited tickers out of the BUY/SELL path entirely."""

from __future__ import annotations

import pytest
from conftest import make_fundamentals

from stockbot.valuation.dcf import estimate_growth, run_gate, value_ticker


# --- gate -----------------------------------------------------------------


def test_gate_passes_on_stable_positive_fcf(cfg):
    gate = run_gate(make_fundamentals(fcf=[10e9, 9.5e9, 9.0e9, 8.5e9, 8.0e9]), cfg)
    assert gate.applicable
    assert gate.years_used == 5
    assert gate.negative_years == 0
    assert gate.fcf_cv < 0.60


def test_gate_rejects_insufficient_history(cfg):
    gate = run_gate(make_fundamentals(fcf=[10e9, 9e9]), cfg)
    assert not gate.applicable
    assert gate.reason.startswith("insufficient_fcf_history")


def test_gate_rejects_any_negative_year(cfg):
    gate = run_gate(make_fundamentals(fcf=[10e9, 9e9, -1e9, 8e9, 7e9]), cfg)
    assert not gate.applicable
    assert gate.reason.startswith("negative_fcf")


def test_gate_rejects_volatile_fcf(cfg):
    # NVDA-shaped: technically all positive, but a DCF cannot describe it.
    gate = run_gate(make_fundamentals(fcf=[60e9, 27e9, 3.8e9, 8.1e9, 4.3e9]), cfg)
    assert not gate.applicable
    assert gate.reason.startswith("fcf_too_volatile")
    assert gate.fcf_cv > 0.60


def test_gate_rejects_missing_market_data(cfg):
    gate = run_gate(make_fundamentals(price=0.0), cfg)
    assert not gate.applicable
    assert gate.reason.startswith("missing_market_data")


def test_gate_only_inspects_the_lookback_window(cfg):
    # Six years supplied, oldest one negative — but lookback is 5, so it is
    # outside the window and must not trip the gate.
    gate = run_gate(make_fundamentals(fcf=[10e9, 9.5e9, 9e9, 8.5e9, 8e9, -50e9]), cfg)
    assert gate.applicable
    assert gate.years_used == 5


# --- no DCF math runs when the gate fails ---------------------------------


def test_failed_gate_produces_no_valuation(cfg):
    result = value_ticker(make_fundamentals(fcf=[10e9, 9e9, -1e9, 8e9, 7e9]), cfg)
    assert not result.applicable
    assert result.fair_value is None
    assert result.valuation_gap_pct is None
    assert result.sensitivity is None       # no dcf_confidence input can exist
    assert result.discount_rate is None
    assert result.projected_fcf == []


# --- valuation ------------------------------------------------------------


def test_valuation_is_deterministic(cfg):
    f = make_fundamentals()
    a = value_ticker(f, cfg)
    b = value_ticker(f, cfg)
    assert a.fair_value == b.fair_value
    assert a.valuation_gap_pct == b.valuation_gap_pct
    assert a.sensitivity.fair_values == b.sensitivity.fair_values


def test_valuation_gap_definition(cfg):
    result = value_ticker(make_fundamentals(price=50.0), cfg)
    expected = (result.fair_value - 50.0) / 50.0
    assert result.valuation_gap_pct == pytest.approx(expected)


def test_equity_value_nets_out_debt(cfg):
    result = value_ticker(make_fundamentals(total_debt=10e9, cash=5e9), cfg)
    assert result.net_debt == pytest.approx(5e9)
    assert result.equity_value == pytest.approx(result.enterprise_value - 5e9)


def test_discount_rate_stays_above_terminal_growth(cfg):
    # A near-zero beta would otherwise drag WACC toward the terminal growth rate
    # and blow up the Gordon denominator.
    result = value_ticker(make_fundamentals(beta=0.01, total_debt=0.0), cfg)
    assert result.discount_rate > result.terminal_growth


def test_growth_is_clamped(cfg):
    lo, hi = cfg.get("dcf.growth_clamp")
    explosive = estimate_growth([100e9, 10e9, 5e9, 2e9, 1e9], cfg)
    collapsing = estimate_growth([1e9, 2e9, 5e9, 10e9, 100e9], cfg)
    assert explosive == pytest.approx(hi)
    assert collapsing == pytest.approx(lo)


def test_sensitivity_grid_shape(cfg):
    result = value_ticker(make_fundamentals(), cfg)
    g = len(cfg.get("dcf.sensitivity.growth_deltas"))
    d = len(cfg.get("dcf.sensitivity.discount_deltas"))
    assert len(result.sensitivity.fair_values) == g * d
    assert 0.0 <= result.sensitivity.sign_agreement <= 1.0
    assert result.sensitivity.min_fair_value <= result.sensitivity.max_fair_value


def test_higher_discount_rate_lowers_fair_value(cfg):
    low_beta = value_ticker(make_fundamentals(beta=0.5), cfg)
    high_beta = value_ticker(make_fundamentals(beta=2.0), cfg)
    assert high_beta.discount_rate > low_beta.discount_rate
    assert high_beta.fair_value < low_beta.fair_value
