"""The decision engine. Rule-based, deterministic, and NOT an LLM call.

This is the single place a BUY / SELL / HOLD / WATCH comes from. It reads only
categories that code produced: a valuation bucket, a sentiment category, the
dcf_applicable flag, the news_available flag, and whether the position is held.

Every branch records the rule name that fired, so any signal in the report can
be traced back to a row of the matrix.

Matrix (from the spec):

    | Valuation gap        | News sentiment       | Signal            |
    |----------------------|----------------------|-------------------|
    | Undervalued (large)  | Neutral / Positive   | BUY               |
    | Undervalued (large)  | Negative (strong)    | WATCH             |
    | Overvalued + held    | Neutral / Negative   | SELL              |
    | Overvalued + held    | Positive (momentum)  | HOLD (flag risk)  |
    | Near fair value      | any                  | HOLD / no action  |
    | dcf_applicable=false | any                  | WATCH only        |

Three cases the spec's table leaves open are filled in conservatively below and
marked with `SPEC-FILL`. They are called out in README.md so the choice stays
visible rather than becoming folklore.
"""

from __future__ import annotations

from ..config import Config
from ..logging_setup import get_logger
from ..models import (
    DCFResult,
    Decision,
    NewsResult,
    Sentiment,
    Signal,
    Strength,
    ValuationBucket,
)

log = get_logger("decision.engine")


def bucket_valuation(gap: float | None, cfg: Config) -> ValuationBucket:
    """Turn the continuous valuation gap into one of the matrix's categories."""
    if gap is None:
        return ValuationBucket.NOT_APPLICABLE

    large = float(cfg.get("decision.large_undervalued", 0.25))
    mild = float(cfg.get("decision.mild_undervalued", 0.10))
    over = float(cfg.get("decision.overvalued", -0.10))

    if gap >= large:
        return ValuationBucket.UNDERVALUED_LARGE
    if gap >= mild:
        return ValuationBucket.UNDERVALUED_MILD
    if gap <= over:
        return ValuationBucket.OVERVALUED
    return ValuationBucket.NEAR_FAIR


def decide(ticker: str, dcf: DCFResult, news: NewsResult, held: bool, cfg: Config) -> Decision:
    flags: list[str] = []

    sentiment = news.aggregate_label
    strength = news.aggregate_strength

    if not news.news_available:
        # Explicit, flagged path. The matrix still needs a sentiment category to
        # read, so neutral is used — but it is recorded as an assumption, and the
        # confidence layer applies its own baseline rather than scoring this as
        # if news had actually been read.
        sentiment = Sentiment.NEUTRAL
        strength = Strength.WEAK
        flags.append(f"news_unavailable: {news.reason}")

    # --- Rule 0: the DCF gate wins over everything ------------------------
    # A ticker the DCF cannot describe is never BUY and never SELL, no matter
    # how attractive anything else looks.
    if not dcf.applicable:
        flags.append(f"dcf_not_applicable: {dcf.gate.reason}")
        return Decision(
            ticker=ticker,
            signal=Signal.WATCH,
            rule="dcf_gate_failed",
            valuation_bucket=ValuationBucket.NOT_APPLICABLE,
            sentiment_label=sentiment,
            sentiment_strength=strength,
            held=held,
            flags=flags,
        )

    bucket = bucket_valuation(dcf.valuation_gap_pct, cfg)

    if dcf.wacc_estimated:
        flags.append("wacc_estimated: a default input was substituted in the discount rate")

    # --- Undervalued (large) ---------------------------------------------
    if bucket is ValuationBucket.UNDERVALUED_LARGE:
        if sentiment is Sentiment.NEGATIVE:
            if strength is Strength.STRONG:
                rule = "large_undervalued_strong_negative_news"
            else:
                # SPEC-FILL: the table names only "Negative (strong)". Any
                # negative reading defers the buy — the cheap price will still
                # be there once the news resolves.
                rule = "large_undervalued_mild_negative_news"
                flags.append("spec_fill: mild negative news also defers BUY to WATCH")
            return Decision(
                ticker, Signal.WATCH, rule, bucket, sentiment, strength, held, flags
            )

        return Decision(
            ticker, Signal.BUY, "large_undervalued_neutral_or_positive_news",
            bucket, sentiment, strength, held, flags,
        )

    # --- Overvalued -------------------------------------------------------
    if bucket is ValuationBucket.OVERVALUED:
        if not held:
            # SPEC-FILL: "Overvalued + held" is the only overvalued row given.
            # With no position there is nothing to sell, and an expensive stock
            # is not a buy.
            flags.append("spec_fill: overvalued but not held — no position to sell")
            return Decision(
                ticker, Signal.WATCH, "overvalued_not_held",
                bucket, sentiment, strength, held, flags,
            )

        if sentiment is Sentiment.POSITIVE:
            flags.append("risk: overvalued but carried by positive momentum")
            return Decision(
                ticker, Signal.HOLD, "overvalued_held_positive_momentum",
                bucket, sentiment, strength, held, flags,
            )

        return Decision(
            ticker, Signal.SELL, "overvalued_held_neutral_or_negative_news",
            bucket, sentiment, strength, held, flags,
        )

    # --- Undervalued (mild) ----------------------------------------------
    if bucket is ValuationBucket.UNDERVALUED_MILD:
        # SPEC-FILL: sits between "large" and "near fair value". Not a large
        # enough margin of safety to open on, so it behaves like near-fair.
        flags.append("spec_fill: mild discount treated as near fair value")
        signal = Signal.HOLD if held else Signal.WATCH
        return Decision(
            ticker, signal, "mild_undervalued", bucket, sentiment, strength, held, flags
        )

    # --- Near fair value --------------------------------------------------
    signal = Signal.HOLD if held else Signal.WATCH
    return Decision(
        ticker, signal, "near_fair_value", bucket, sentiment, strength, held, flags
    )
