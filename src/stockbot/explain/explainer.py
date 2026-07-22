"""Explanation layer — the LLM's second and last job.

It receives a decision that code already made, together with every number that
produced it, and writes prose. It cannot change anything: the signal and the
confidence are already fixed in the `Decision`/`ConfidenceResult` objects, the
return value of `explain()` is a string, and the only field it lands in is
`TickerReport.rationale`, which nothing downstream reads.

A contradiction check runs on the output anyway. It cannot alter the signal —
it just flags the case for the reader, because a model arguing against the
signal it was handed is worth seeing.
"""

from __future__ import annotations

import re

from ..config import Config
from ..llm.fireworks import FireworksClient, LLMError
from ..logging_setup import get_logger
from ..models import (
    ConfidenceResult,
    DCFResult,
    Decision,
    NewsResult,
    RiskResult,
    Signal,
)

log = get_logger("explain")

_SYSTEM = (
    "You explain investment signals that have ALREADY been decided by a "
    "deterministic rule engine. You are a narrator, not an analyst.\n\n"
    "Rules you must follow:\n"
    "1. The signal you are given is final. Never suggest a different one, never "
    "hedge it into a different one, never say the signal 'should' be something else.\n"
    "2. Never invent, recompute, or adjust any number. Use only the figures given.\n"
    "3. Never state a confidence level that was not given to you.\n"
    "4. Write 3-5 plain sentences for one reader who owns this portfolio. No "
    "headings, no bullet points, no preamble, no disclaimer.\n"
    "5. Say plainly what drove the decision, and name the single biggest caveat "
    "in the data you were given."
)


def _fmt_pct(x: float | None, digits: int = 1) -> str:
    return "n/a" if x is None else f"{x * 100:+.{digits}f}%"


def _fmt_money(x: float | None) -> str:
    return "n/a" if x is None else f"${x:,.2f}"


def build_context(
    ticker: str,
    price: float,
    decision: Decision,
    dcf: DCFResult,
    news: NewsResult,
    risk: RiskResult,
    confidence: ConfidenceResult | None,
) -> str:
    """Everything the narrator is allowed to know, as flat facts."""
    lines = [
        f"Ticker: {ticker}",
        f"FINAL SIGNAL (already decided, not yours to change): {decision.signal.value}",
        f"Rule that fired: {decision.rule}",
        f"Currently held: {'yes' if decision.held else 'no'}",
        f"Current price: {_fmt_money(price)}",
    ]

    if dcf.applicable:
        lines += [
            f"DCF fair value: {_fmt_money(dcf.fair_value)}",
            f"Valuation gap vs price: {_fmt_pct(dcf.valuation_gap_pct)}",
            f"Valuation category: {decision.valuation_bucket.value}",
            f"Discount rate used: {_fmt_pct(dcf.discount_rate, 2)}",
            f"FCF growth assumption: {_fmt_pct(dcf.growth_rate, 2)}",
            f"Terminal growth: {_fmt_pct(dcf.terminal_growth, 2)}",
            f"Trailing FCF years used: {dcf.gate.years_used}",
        ]
        if dcf.sensitivity:
            lines.append(
                f"Fair value across the sensitivity grid: "
                f"{_fmt_money(dcf.sensitivity.min_fair_value)} to "
                f"{_fmt_money(dcf.sensitivity.max_fair_value)}"
            )
    else:
        lines += [
            "DCF: NOT APPLICABLE for this ticker.",
            f"Reason the DCF was skipped: {dcf.gate.reason}",
            "No fair value exists for this ticker. Do not estimate one.",
        ]

    if news.news_available:
        lines += [
            f"News sentiment: {news.aggregate_label.value} ({news.aggregate_strength.value})",
            f"Articles classified: {news.source_count} from {news.distinct_sources} distinct source(s)",
            f"Cross-article agreement: {news.agreement_ratio:.0%}",
        ]
        headlines = [s.article.title for s in news.articles[:5]]
        if headlines:
            lines.append("Recent headlines: " + " | ".join(headlines))
    else:
        lines += [
            "News: NOT AVAILABLE for this ticker in the lookback window.",
            f"Reason: {news.reason}",
            "Sentiment was treated as neutral by assumption. Say so if it matters.",
        ]

    if confidence is not None:
        lines += [
            f"Confidence score: {confidence.value:.0f}/100 ({confidence.band.value})",
            f"  from dcf={confidence.dcf_confidence:.0f}, "
            f"news={confidence.news_confidence:.0f}, agreement={confidence.agreement_score:.0f}",
        ]

    if risk.downgraded:
        lines.append(
            f"RISK DOWNGRADE: this was a {risk.original_signal.value if risk.original_signal else '?'} "
            f"before the risk check. Reasons: {'; '.join(risk.breaches)}"
        )
    if risk.ticker_weight is not None:
        lines.append(f"Current portfolio weight: {risk.ticker_weight:.1%}")

    if risk.position is not None:
        p = risk.position
        lines += [
            f"Position: {p.quantity:g} shares at {_fmt_money(p.cost_basis_per_share)} cost basis",
            f"Unrealized P/L: {_fmt_money(p.unrealized_pnl)} ({_fmt_pct(p.unrealized_pnl_pct)})",
            f"Holding period: {p.holding_period_days if p.holding_period_days is not None else 'unknown'} days"
            + (f" ({p.term}-term)" if p.term else ""),
            "Mention that the tax impact of this sale has not been calculated and should be checked.",
        ]

    if decision.flags:
        lines.append("Flags raised by the engine: " + "; ".join(decision.flags))

    return "\n".join(lines)


def _fallback_text(
    ticker: str, price: float, decision: Decision, dcf: DCFResult, news: NewsResult, risk: RiskResult
) -> str:
    """Deterministic prose for when no LLM is reachable.

    Phase 0 must be able to run start to finish without an API key — no signal
    depends on the model, so a missing key degrades the wording, nothing else.
    """
    parts = [f"{ticker}: {decision.signal.value} at {_fmt_money(price)}."]

    if dcf.applicable:
        parts.append(
            f"DCF fair value {_fmt_money(dcf.fair_value)}, a gap of "
            f"{_fmt_pct(dcf.valuation_gap_pct)} versus the current price "
            f"({decision.valuation_bucket.value.replace('_', ' ')})."
        )
    else:
        parts.append(f"No DCF was run — {dcf.gate.reason}.")

    if news.news_available:
        parts.append(
            f"News over the lookback window reads {news.aggregate_label.value} "
            f"({news.aggregate_strength.value}) across {news.source_count} article(s)."
        )
    else:
        parts.append(f"No usable news was found ({news.reason}); sentiment assumed neutral.")

    if risk.downgraded:
        parts.append("Downgraded by the risk check: " + "; ".join(risk.breaches) + ".")
    if risk.position is not None:
        p = risk.position
        term = f", {p.term}-term" if p.term else ""
        parts.append(
            f"Position: {p.quantity:g} shares, unrealized {_fmt_money(p.unrealized_pnl)} "
            f"({_fmt_pct(p.unrealized_pnl_pct)}{term}). Tax impact not calculated."
        )

    parts.append(f"Rule: {decision.rule}.")
    return " ".join(parts)


_SIGNAL_WORDS = {s.value for s in Signal}


def check_contradiction(text: str, signal: Signal) -> str | None:
    """Flag prose that names a signal other than the one it was given.

    Advisory only — it cannot and does not change the signal.
    """
    found = {
        w.upper()
        for w in re.findall(r"\b(buy|sell|hold|watch)\b", text, flags=re.IGNORECASE)
    }
    others = found & _SIGNAL_WORDS - {signal.value}
    if others:
        return (
            f"explanation_mentions_other_signal: text names {sorted(others)} while the "
            f"signal is {signal.value} (signal unchanged)"
        )
    return None


def explain(
    ticker: str,
    price: float,
    decision: Decision,
    dcf: DCFResult,
    news: NewsResult,
    risk: RiskResult,
    confidence: ConfidenceResult | None,
    client: FireworksClient,
    cfg: Config,
) -> tuple[str, str, list[str]]:
    """Returns (rationale, source, warnings). `source` is "llm" or "fallback"."""
    warnings: list[str] = []

    if not client.available:
        return (
            _fallback_text(ticker, price, decision, dcf, news, risk),
            "fallback",
            ["llm_unavailable: FIREWORKS_API_KEY not configured"],
        )

    context = build_context(ticker, price, decision, dcf, news, risk, confidence)
    try:
        text = client.chat(
            model=str(cfg.get("llm.explanation_model")),
            messages=[
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": context},
            ],
            temperature=float(cfg.get("llm.explanation_temperature", 0.3)),
            max_tokens=int(cfg.get("llm.explanation_max_tokens", 400)),
        )
    except LLMError as exc:
        log.warning("%s: explanation failed (%s) — using deterministic text", ticker, exc)
        return (
            _fallback_text(ticker, price, decision, dcf, news, risk),
            "fallback",
            [f"llm_error: {exc}"],
        )

    contradiction = check_contradiction(text, decision.signal)
    if contradiction:
        log.warning("%s: %s", ticker, contradiction)
        warnings.append(contradiction)

    return text.strip(), "llm", warnings
