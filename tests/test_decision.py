"""Every row of the decision matrix, plus the two hard constraints:
a gated-out ticker is never BUY/SELL, and no-news is an explicit flagged path."""

from __future__ import annotations

from conftest import make_fundamentals, make_news

from stockbot.decision.engine import bucket_valuation, decide
from stockbot.models import DCFResult, GateResult, Sentiment, Signal, Strength, ValuationBucket
from stockbot.valuation.dcf import value_ticker


def _dcf(cfg, gap: float) -> DCFResult:
    """A passing DCF whose valuation gap is exactly `gap`."""
    real = value_ticker(make_fundamentals(), cfg)
    assert real.applicable
    real.valuation_gap_pct = gap
    return real


def _failed_dcf() -> DCFResult:
    return DCFResult(
        ticker="TEST",
        gate=GateResult(applicable=False, reason="fcf_too_volatile: coefficient of variation 1.04"),
    )


# --- bucketing ------------------------------------------------------------


def test_bucket_boundaries(cfg):
    assert bucket_valuation(0.30, cfg) is ValuationBucket.UNDERVALUED_LARGE
    assert bucket_valuation(0.25, cfg) is ValuationBucket.UNDERVALUED_LARGE
    assert bucket_valuation(0.15, cfg) is ValuationBucket.UNDERVALUED_MILD
    assert bucket_valuation(0.00, cfg) is ValuationBucket.NEAR_FAIR
    assert bucket_valuation(-0.09, cfg) is ValuationBucket.NEAR_FAIR
    assert bucket_valuation(-0.10, cfg) is ValuationBucket.OVERVALUED
    assert bucket_valuation(None, cfg) is ValuationBucket.NOT_APPLICABLE


# --- matrix rows ----------------------------------------------------------


def test_large_undervalued_positive_news_is_buy(cfg):
    d = decide("T", _dcf(cfg, 0.40), make_news(label=Sentiment.POSITIVE, score=0.7), False, cfg)
    assert d.signal is Signal.BUY


def test_large_undervalued_neutral_news_is_buy(cfg):
    d = decide("T", _dcf(cfg, 0.40), make_news(label=Sentiment.NEUTRAL), False, cfg)
    assert d.signal is Signal.BUY


def test_large_undervalued_strong_negative_news_is_watch(cfg):
    news = make_news(label=Sentiment.NEGATIVE, strength=Strength.STRONG, score=-0.8)
    d = decide("T", _dcf(cfg, 0.40), news, False, cfg)
    assert d.signal is Signal.WATCH
    assert d.rule == "large_undervalued_strong_negative_news"


def test_overvalued_held_neutral_news_is_sell(cfg):
    d = decide("T", _dcf(cfg, -0.30), make_news(label=Sentiment.NEUTRAL), True, cfg)
    assert d.signal is Signal.SELL


def test_overvalued_held_negative_news_is_sell(cfg):
    news = make_news(label=Sentiment.NEGATIVE, strength=Strength.STRONG, score=-0.7)
    d = decide("T", _dcf(cfg, -0.30), news, True, cfg)
    assert d.signal is Signal.SELL


def test_overvalued_held_positive_news_is_hold_with_risk_flag(cfg):
    news = make_news(label=Sentiment.POSITIVE, strength=Strength.STRONG, score=0.8)
    d = decide("T", _dcf(cfg, -0.30), news, True, cfg)
    assert d.signal is Signal.HOLD
    assert any(f.startswith("risk:") for f in d.flags)


def test_near_fair_value_held_is_hold(cfg):
    d = decide("T", _dcf(cfg, 0.02), make_news(label=Sentiment.POSITIVE, score=0.9), True, cfg)
    assert d.signal is Signal.HOLD
    assert d.rule == "near_fair_value"


def test_near_fair_value_not_held_is_watch(cfg):
    d = decide("T", _dcf(cfg, 0.02), make_news(), False, cfg)
    assert d.signal is Signal.WATCH


# --- hard constraint: the gate wins --------------------------------------


def test_gate_failure_forces_watch_even_with_glowing_news(cfg):
    news = make_news(label=Sentiment.POSITIVE, strength=Strength.STRONG, score=1.0)
    d = decide("T", _failed_dcf(), news, held=False, cfg=cfg)
    assert d.signal is Signal.WATCH
    assert d.rule == "dcf_gate_failed"
    assert d.valuation_bucket is ValuationBucket.NOT_APPLICABLE


def test_gate_failure_never_sells_a_held_position(cfg):
    news = make_news(label=Sentiment.NEGATIVE, strength=Strength.STRONG, score=-1.0)
    d = decide("T", _failed_dcf(), news, held=True, cfg=cfg)
    assert d.signal is Signal.WATCH


def test_gate_failure_is_flagged_with_the_reason(cfg):
    d = decide("T", _failed_dcf(), make_news(), False, cfg)
    assert any(f.startswith("dcf_not_applicable:") for f in d.flags)


# --- hard constraint: no-news is explicit --------------------------------


def test_missing_news_is_flagged_not_silent(cfg):
    d = decide("T", _dcf(cfg, 0.40), make_news(available=False), False, cfg)
    assert any(f.startswith("news_unavailable:") for f in d.flags)
    assert d.sentiment_label is Sentiment.NEUTRAL


def test_missing_news_still_allows_a_buy_on_valuation(cfg):
    # Absent news must not block the valuation leg — it is flagged, not fatal.
    d = decide("T", _dcf(cfg, 0.40), make_news(available=False), False, cfg)
    assert d.signal is Signal.BUY
