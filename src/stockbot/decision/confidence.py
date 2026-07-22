"""Confidence score — Phase 1. Code only, fixed weighted formula.

    confidence = 0.5*dcf_confidence + 0.3*news_confidence + 0.2*agreement_score

No LLM contributes to any term. Two rules from the spec are enforced structurally
rather than by convention:

  * dcf_confidence is never computed for a ticker that failed the applicability
    gate — `compute_confidence` returns None outright in that case.
  * news_confidence is never silently defaulted — the no-news path returns an
    explicit configured baseline and sets `news_baseline_applied`.
"""

from __future__ import annotations

from ..config import Config
from ..logging_setup import get_logger
from ..models import (
    ConfidenceBand,
    ConfidenceResult,
    DCFResult,
    Decision,
    NewsResult,
    Sentiment,
    Signal,
    ValuationBucket,
)

log = get_logger("decision.confidence")


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def dcf_confidence(dcf: DCFResult, cfg: Config) -> float:
    """How stable is the fair value under +/-1% and +/-2% input shifts?

    Two components:
      sign agreement — does the buy/sell direction survive the whole grid?
      dispersion     — how tightly do the fair values cluster?

    Caller must have verified dcf.applicable first.
    """
    if dcf.sensitivity is None:
        raise ValueError("dcf_confidence requires a sensitivity grid")

    max_cv = float(cfg.get("dcf.sensitivity.max_cv", 0.40))
    dispersion = 1.0 - _clamp01(dcf.sensitivity.cv / max_cv) if max_cv > 0 else 0.0
    return 100.0 * (0.6 * dcf.sensitivity.sign_agreement + 0.4 * dispersion)


def news_confidence(news: NewsResult, cfg: Config) -> tuple[float, bool]:
    """Source count + cross-source agreement. Returns (score, baseline_applied)."""
    if not news.news_available:
        baseline = float(cfg.get("confidence.news_baseline_when_unavailable", 50.0))
        return baseline, True

    target = int(cfg.get("confidence.target_source_count", 5))
    breadth = _clamp01(news.distinct_sources / target) if target > 0 else 0.0

    # agreement_ratio bottoms out near 1/3 when three labels are evenly split;
    # rescale so an even split scores 0 rather than 33.
    consensus = _clamp01((news.agreement_ratio - 1 / 3) / (1 - 1 / 3))

    return 100.0 * (0.4 * breadth + 0.6 * consensus), False


def agreement_score(dcf: DCFResult, news: NewsResult, bucket: ValuationBucket) -> float:
    """Does the sign of the valuation gap point the same way as sentiment?

    Undervalued + positive news, or overvalued + negative news, is agreement.
    The opposite is a contradiction the score should punish.
    """
    if bucket is ValuationBucket.NEAR_FAIR or dcf.valuation_gap_pct is None:
        return 60.0  # nothing to agree or disagree with

    undervalued = dcf.valuation_gap_pct > 0
    sentiment = news.aggregate_label

    if sentiment is Sentiment.NEUTRAL or not news.news_available:
        return 60.0
    aligned = (undervalued and sentiment is Sentiment.POSITIVE) or (
        not undervalued and sentiment is Sentiment.NEGATIVE
    )
    return 100.0 if aligned else 20.0


def _band(value: float, cfg: Config) -> ConfidenceBand:
    high = float(cfg.get("confidence.bands.high", 80))
    medium = float(cfg.get("confidence.bands.medium", 50))
    if value >= high:
        return ConfidenceBand.HIGH
    if value >= medium:
        return ConfidenceBand.MEDIUM
    return ConfidenceBand.LOW


def compute_confidence(
    dcf: DCFResult, news: NewsResult, decision: Decision, cfg: Config
) -> ConfidenceResult | None:
    """Returns None in phase 0, and None for any ticker that failed the DCF gate."""
    if not cfg.confidence_enabled:
        return None

    if not dcf.applicable:
        # Hard constraint: no dcf_confidence for a gated-out ticker, and no
        # composite that would imply one exists.
        log.debug("%s: no confidence score — DCF gate failed", decision.ticker)
        return None

    weights = cfg.get("confidence.weights", {}) or {}
    w_dcf = float(weights.get("dcf", 0.5))
    w_news = float(weights.get("news", 0.3))
    w_agree = float(weights.get("agreement", 0.2))

    c_dcf = dcf_confidence(dcf, cfg)
    c_news, baseline_applied = news_confidence(news, cfg)
    c_agree = agreement_score(dcf, news, decision.valuation_bucket)

    value = w_dcf * c_dcf + w_news * c_news + w_agree * c_agree

    notes: list[str] = []
    if baseline_applied:
        notes.append(
            f"news_confidence used the no-news baseline ({c_news:.0f}) — {news.reason}"
        )
    if dcf.wacc_estimated:
        notes.append("discount rate used a default input")

    return ConfidenceResult(
        value=value,
        band=_band(value, cfg),
        dcf_confidence=c_dcf,
        news_confidence=c_news,
        agreement_score=c_agree,
        news_baseline_applied=baseline_applied,
        notes=notes,
    )


def apply_confidence_gate(
    decision: Decision, confidence: ConfidenceResult | None, cfg: Config
) -> Decision:
    """Low confidence forces the signal down to WATCH, however good the gap looks."""
    if confidence is None:
        return decision

    threshold = float(cfg.get("confidence.force_watch_below", 50))
    if confidence.value >= threshold or decision.signal is Signal.WATCH:
        return decision

    log.info(
        "%s: %s -> WATCH (confidence %.0f below %.0f)",
        decision.ticker, decision.signal.value, confidence.value, threshold,
    )
    decision.flags.append(
        f"confidence_gate: {decision.signal.value} downgraded to WATCH "
        f"(confidence {confidence.value:.0f} < {threshold:.0f})"
    )
    decision.signal = Signal.WATCH
    decision.rule = f"{decision.rule}+confidence_forced_watch"
    return decision
