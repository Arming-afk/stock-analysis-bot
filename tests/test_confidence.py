"""Confidence score (phase 1). The two constraints under test: no dcf_confidence
for a gated-out ticker, and no silent default for the no-news case."""

from __future__ import annotations

import pytest
from conftest import make_fundamentals, make_news

from stockbot.decision.confidence import (
    agreement_score,
    apply_confidence_gate,
    compute_confidence,
    dcf_confidence,
    news_confidence,
)
from stockbot.decision.engine import decide
from stockbot.models import DCFResult, GateResult, Sentiment, Signal, Strength, ValuationBucket
from stockbot.valuation.dcf import value_ticker


def _dcf(cfg, gap: float | None = None):
    result = value_ticker(make_fundamentals(), cfg)
    if gap is not None:
        result.valuation_gap_pct = gap
    return result


def _failed_dcf():
    return DCFResult(ticker="TEST", gate=GateResult(applicable=False, reason="negative_fcf: 2 of 5"))


# --- phase gating ---------------------------------------------------------


def test_phase_0_has_no_confidence_score(cfg):
    dcf = _dcf(cfg, 0.40)
    news = make_news()
    d = decide("T", dcf, news, False, cfg)
    assert cfg.phase == 0
    assert compute_confidence(dcf, news, d, cfg) is None


def test_phase_1_produces_a_score(cfg_phase1):
    dcf = _dcf(cfg_phase1, 0.40)
    news = make_news()
    d = decide("T", dcf, news, False, cfg_phase1)
    c = compute_confidence(dcf, news, d, cfg_phase1)
    assert c is not None
    assert 0.0 <= c.value <= 100.0


# --- hard constraint: nothing scored for a gated-out ticker --------------


def test_no_confidence_at_all_when_the_dcf_gate_failed(cfg_phase1):
    dcf = _failed_dcf()
    news = make_news()
    d = decide("T", dcf, news, False, cfg_phase1)
    assert compute_confidence(dcf, news, d, cfg_phase1) is None


def test_dcf_confidence_refuses_to_run_without_a_sensitivity_grid(cfg_phase1):
    with pytest.raises(ValueError):
        dcf_confidence(_failed_dcf(), cfg_phase1)


# --- hard constraint: no-news baseline is explicit -----------------------


def test_news_confidence_uses_the_configured_baseline_when_unavailable(cfg_phase1):
    score, baseline_applied = news_confidence(make_news(available=False), cfg_phase1)
    assert baseline_applied is True
    assert score == pytest.approx(
        float(cfg_phase1.get("confidence.news_baseline_when_unavailable"))
    )


def test_no_news_baseline_is_recorded_in_the_result(cfg_phase1):
    dcf = _dcf(cfg_phase1, 0.40)
    news = make_news(available=False)
    d = decide("T", dcf, news, False, cfg_phase1)
    c = compute_confidence(dcf, news, d, cfg_phase1)
    assert c.news_baseline_applied is True
    assert any("baseline" in n for n in c.notes)


def test_news_confidence_rewards_breadth_and_consensus(cfg_phase1):
    broad, _ = news_confidence(
        make_news(n_articles=8, distinct_sources=6, agreement=1.0), cfg_phase1
    )
    narrow, _ = news_confidence(
        make_news(n_articles=2, distinct_sources=1, agreement=0.5), cfg_phase1
    )
    assert broad > narrow


# --- agreement leg --------------------------------------------------------


def test_agreement_high_when_undervalued_and_positive(cfg_phase1):
    dcf = _dcf(cfg_phase1, 0.40)
    news = make_news(label=Sentiment.POSITIVE, score=0.8)
    assert agreement_score(dcf, news, ValuationBucket.UNDERVALUED_LARGE) == 100.0


def test_agreement_low_when_undervalued_but_negative(cfg_phase1):
    dcf = _dcf(cfg_phase1, 0.40)
    news = make_news(label=Sentiment.NEGATIVE, score=-0.8)
    assert agreement_score(dcf, news, ValuationBucket.UNDERVALUED_LARGE) == 20.0


def test_agreement_high_when_overvalued_and_negative(cfg_phase1):
    dcf = _dcf(cfg_phase1, -0.40)
    news = make_news(label=Sentiment.NEGATIVE, score=-0.8)
    assert agreement_score(dcf, news, ValuationBucket.OVERVALUED) == 100.0


# --- weighted formula -----------------------------------------------------


def test_confidence_matches_the_documented_formula(cfg_phase1):
    dcf = _dcf(cfg_phase1, 0.40)
    news = make_news(label=Sentiment.POSITIVE, score=0.8)
    d = decide("T", dcf, news, False, cfg_phase1)
    c = compute_confidence(dcf, news, d, cfg_phase1)

    expected = 0.5 * c.dcf_confidence + 0.3 * c.news_confidence + 0.2 * c.agreement_score
    assert c.value == pytest.approx(expected)


def test_bands(cfg_phase1):
    from stockbot.decision.confidence import _band
    from stockbot.models import ConfidenceBand

    assert _band(90, cfg_phase1) is ConfidenceBand.HIGH
    assert _band(80, cfg_phase1) is ConfidenceBand.HIGH
    assert _band(65, cfg_phase1) is ConfidenceBand.MEDIUM
    assert _band(50, cfg_phase1) is ConfidenceBand.MEDIUM
    assert _band(49, cfg_phase1) is ConfidenceBand.LOW


# --- low confidence forces WATCH -----------------------------------------


def test_low_confidence_downgrades_a_buy_to_watch(cfg_phase1):
    dcf = _dcf(cfg_phase1, 0.40)
    news = make_news(label=Sentiment.NEUTRAL)
    d = decide("T", dcf, news, False, cfg_phase1)
    assert d.signal is Signal.BUY

    c = compute_confidence(dcf, news, d, cfg_phase1)
    c.value = 30.0  # force the low band regardless of the real inputs
    d = apply_confidence_gate(d, c, cfg_phase1)

    assert d.signal is Signal.WATCH
    assert any(f.startswith("confidence_gate:") for f in d.flags)


def test_high_confidence_leaves_the_signal_alone(cfg_phase1):
    dcf = _dcf(cfg_phase1, 0.40)
    news = make_news(label=Sentiment.POSITIVE, strength=Strength.STRONG, score=0.8)
    d = decide("T", dcf, news, False, cfg_phase1)
    c = compute_confidence(dcf, news, d, cfg_phase1)
    c.value = 88.0
    assert apply_confidence_gate(d, c, cfg_phase1).signal is Signal.BUY
