#!/usr/bin/env python
"""Build the publishable copy of a report.

    python tools/publish.py --in data/reports/latest.json --out docs/data

The full report contains the whole financial position: share counts, cost
basis, unrealized P/L, portfolio value, cash, and an LLM rationale that was
written with all of that in context. None of it may reach a public repository.

So the public copy is built from an explicit **whitelist**, not by deleting
known-bad keys. A blacklist silently leaks whatever field gets added next; a
whitelist fails closed. The rationale is regenerated here by deterministic code
from the redacted fields, rather than trying to scrub money out of free text.

What survives: signals, prices, DCF internals, gate verdicts, news sentiment,
confidence, and whether a position is held.
What does not: every quantity and dollar amount, portfolio weights, and the
original rationale text.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Percentages in risk text reveal portfolio allocation.
_PCT = re.compile(r"\d+(?:\.\d+)?%")


def _fmt_money(v: float | None) -> str:
    return "n/a" if v is None else f"${v:,.2f}"


def _fmt_pct(v: float | None) -> str:
    return "n/a" if v is None else f"{v * 100:+.1f}%"


def _sanitize(text: str) -> str:
    """Blunt the allocation numbers out of engine-generated strings."""
    return _PCT.sub("[redacted]", text)


def public_rationale(t: dict) -> str:
    """Deterministic prose built only from fields that are safe to publish."""
    signal = t["signal"]
    dcf = t["dcf"]
    news = t["news"]
    risk = t["risk"]
    decision = t["decision"]

    parts = [f"{t['ticker']}: {signal} at {_fmt_money(t.get('price'))}."]

    if dcf["gate"]["applicable"]:
        parts.append(
            f"DCF fair value {_fmt_money(dcf.get('fair_value'))}, a gap of "
            f"{_fmt_pct(dcf.get('valuation_gap_pct'))} versus the current price "
            f"({decision['valuation_bucket'].replace('_', ' ')})."
        )
    else:
        parts.append(f"No DCF was run — {dcf['gate']['reason']}.")

    if news["news_available"]:
        parts.append(
            f"News reads {news['aggregate_label']} ({news['aggregate_strength']}) "
            f"across {news['source_count']} article(s) from "
            f"{news['distinct_sources']} source(s)."
        )
    else:
        parts.append(f"No usable news ({news['reason']}); sentiment assumed neutral.")

    if risk.get("downgraded"):
        breaches = "; ".join(_sanitize(b) for b in risk.get("breaches", []))
        parts.append(f"Downgraded by the risk check: {breaches}.")

    if decision.get("held") and signal == "SELL":
        parts.append("Position details and tax impact are not published — check them privately.")

    parts.append(f"Rule: {decision['rule']}.")
    return " ".join(parts)


def redact_ticker(t: dict) -> dict:
    """Whitelist rebuild of one ticker entry."""
    decision = t["decision"]
    risk = t["risk"]

    out = {
        "ticker": t["ticker"],
        "price": t.get("price"),
        "signal": t["signal"],
        # DCF is derived from public market data — safe in full.
        "dcf": t["dcf"],
        "news": t["news"],
        "confidence": t.get("confidence"),
        "decision": {
            "ticker": decision["ticker"],
            "signal": decision["signal"],
            "rule": decision["rule"],
            "valuation_bucket": decision["valuation_bucket"],
            "sentiment_label": decision["sentiment_label"],
            "sentiment_strength": decision["sentiment_strength"],
            "held": decision.get("held", False),
            "flags": [_sanitize(f) for f in decision.get("flags", [])],
        },
        "risk": {
            # position, ticker_weight and sector_weight are deliberately absent.
            "downgraded": risk.get("downgraded", False),
            "original_signal": risk.get("original_signal"),
            "breaches": [_sanitize(b) for b in risk.get("breaches", [])],
            "position": None,
        },
        "rationale_source": "redacted",
        "errors": [_sanitize(e) for e in t.get("errors", [])],
    }
    out["rationale"] = public_rationale(t)
    return out


def redact_report(full: dict) -> dict:
    return {
        "run_date": full["run_date"],
        "generated_at": full["generated_at"],
        "phase": full["phase"],
        # portfolio_value and cash are omitted entirely. The dashboard treats
        # them as absent rather than as zero, so nothing renders a fake total.
        "portfolio_source": "redacted",
        "redacted": True,
        "tickers": [redact_ticker(t) for t in full.get("tickers", [])],
        "errors": [_sanitize(e) for e in full.get("errors", [])],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Publish a redacted report")
    parser.add_argument("--in", dest="src", default="data/reports/latest.json")
    parser.add_argument("--out", dest="out", default="docs/data")
    parser.add_argument(
        "--no-redact",
        action="store_true",
        help="publish the full report — ONLY safe for a private repository",
    )
    args = parser.parse_args()

    src = Path(args.src)
    if not src.is_absolute():
        src = ROOT / src
    if not src.exists():
        print(f"no report at {src}", file=sys.stderr)
        return 1

    out_dir = Path(args.out)
    if not out_dir.is_absolute():
        out_dir = ROOT / out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    full = json.loads(src.read_text(encoding="utf-8"))
    report = full if args.no_redact else redact_report(full)

    payload = json.dumps(report, indent=2)
    (out_dir / "latest.json").write_text(payload, encoding="utf-8")
    (out_dir / f"{report['run_date']}.json").write_text(payload, encoding="utf-8")

    mode = "FULL (not redacted)" if args.no_redact else "redacted"
    print(f"published {mode}: {len(report['tickers'])} ticker(s) -> {out_dir}")
    if args.no_redact:
        print("WARNING: this output contains position sizes and portfolio value.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
