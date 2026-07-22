"""Report rendering: terminal, markdown, JSON, and the push summary line."""

from __future__ import annotations

import json
from pathlib import Path

from ..models import DailyReport, Signal, TickerReport, to_json

_ORDER = [Signal.BUY, Signal.SELL, Signal.WATCH, Signal.HOLD]
_EMOJI = {Signal.BUY: "🟢", Signal.SELL: "🔴", Signal.WATCH: "🟡", Signal.HOLD: "⚪"}


def _gap(t: TickerReport) -> str:
    return "n/a" if t.dcf.valuation_gap_pct is None else f"{t.dcf.valuation_gap_pct * 100:+.1f}%"


def _fair(t: TickerReport) -> str:
    return "n/a" if t.dcf.fair_value is None else f"${t.dcf.fair_value:,.2f}"


def _conf(t: TickerReport) -> str:
    return "—" if t.confidence is None else f"{t.confidence.value:.0f} ({t.confidence.band.value})"


def render_terminal(report: DailyReport) -> str:
    lines = [
        "",
        "=" * 78,
        f"  DAILY SIGNALS — {report.run_date.isoformat()}   (phase {report.phase})",
        f"  Portfolio ${report.portfolio_value:,.2f}  ·  cash ${report.cash:,.2f}"
        f"  ·  source: {report.portfolio_source}",
        "=" * 78,
    ]

    for signal in _ORDER:
        group = report.by_signal(signal)
        if not group:
            continue
        lines.append("")
        lines.append(f"{_EMOJI[signal]}  {signal.value}  ({len(group)})")
        lines.append("-" * 78)
        for t in group:
            conf = "" if t.confidence is None else f"  conf {_conf(t)}"
            lines.append(
                f"  {t.ticker:<6} ${t.price:>9,.2f}   fair {_fair(t):>12}   gap {_gap(t):>8}{conf}"
            )
            lines.append(f"         rule: {t.decision.rule}")
            if not t.dcf.applicable:
                lines.append(f"         gate: {t.dcf.gate.reason}")
            if not t.news.news_available:
                lines.append(f"         news: {t.news.reason}")
            if t.risk.downgraded:
                lines.append(f"         risk: {'; '.join(t.risk.breaches)}")
            if t.risk.position:
                p = t.risk.position
                term = f", {p.term}-term" if p.term else ""
                lines.append(
                    f"         position: {p.quantity:g} sh, cost ${p.cost_basis_per_share:,.2f}, "
                    f"P/L ${p.unrealized_pnl:,.2f} ({p.unrealized_pnl_pct * 100:+.1f}%{term}) "
                    f"— tax impact not calculated"
                )
            if t.rationale:
                lines.append(f"         {t.rationale}")
            lines.append("")

    if report.errors:
        lines += ["", "Run errors:"] + [f"  - {e}" for e in report.errors]

    lines += ["", "Not investment advice. Personal decision-support only.", ""]
    return "\n".join(lines)


def render_markdown(report: DailyReport) -> str:
    lines = [
        f"# Daily signals — {report.run_date.isoformat()}",
        "",
        f"Phase {report.phase} · portfolio ${report.portfolio_value:,.2f} · "
        f"cash ${report.cash:,.2f} · source `{report.portfolio_source}`",
        "",
        "| Signal | Ticker | Price | Fair value | Gap | Confidence | Rule |",
        "|---|---|---:|---:|---:|---|---|",
    ]
    for signal in _ORDER:
        for t in report.by_signal(signal):
            lines.append(
                f"| {_EMOJI[signal]} {signal.value} | {t.ticker} | ${t.price:,.2f} | "
                f"{_fair(t)} | {_gap(t)} | {_conf(t)} | `{t.decision.rule}` |"
            )

    for signal in _ORDER:
        group = report.by_signal(signal)
        if not group:
            continue
        lines += ["", f"## {signal.value}"]
        for t in group:
            lines += ["", f"### {t.ticker}", "", t.rationale or "_no rationale generated_"]
            details = []
            if not t.dcf.applicable:
                details.append(f"- DCF gate: `{t.dcf.gate.reason}`")
            if not t.news.news_available:
                details.append(f"- News: `{t.news.reason}`")
            if t.risk.downgraded:
                details.append(f"- Risk downgrade: {'; '.join(t.risk.breaches)}")
            if t.risk.position:
                p = t.risk.position
                details.append(
                    f"- Position: {p.quantity:g} shares · cost basis ${p.cost_basis_per_share:,.2f} · "
                    f"unrealized ${p.unrealized_pnl:,.2f} ({p.unrealized_pnl_pct * 100:+.1f}%) · "
                    f"held {p.holding_period_days if p.holding_period_days is not None else '?'} days"
                    + (f" ({p.term}-term)" if p.term else "")
                    + " · **tax impact not calculated**"
                )
            for flag in t.decision.flags:
                details.append(f"- Flag: `{flag}`")
            if details:
                lines += [""] + details

    lines += ["", "---", "", "_Not investment advice. Personal decision-support tool only._"]
    return "\n".join(lines)


def push_summary(report: DailyReport) -> tuple[str, str]:
    """(title, body) for the notification. Short enough for a lock screen."""
    buys = report.by_signal(Signal.BUY)
    sells = report.by_signal(Signal.SELL)
    watches = report.by_signal(Signal.WATCH)

    counts = f"{len(buys)} buy · {len(sells)} sell · {len(watches)} watch"
    title = f"Signals {report.run_date.strftime('%b %d')} — {counts}"

    bits: list[str] = []
    if buys:
        bits.append("BUY " + ", ".join(t.ticker for t in buys[:4]))
    if sells:
        bits.append("SELL " + ", ".join(t.ticker for t in sells[:4]))
    if not bits:
        bits.append("No action today.")
    return title, "  ·  ".join(bits)


def write_report_files(report: DailyReport, report_dir: Path) -> dict[str, Path]:
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = report.run_date.isoformat()

    json_path = report_dir / f"{stamp}.json"
    md_path = report_dir / f"{stamp}.md"
    latest_path = report_dir / "latest.json"

    payload = to_json(report)
    json_path.write_text(payload, encoding="utf-8")
    latest_path.write_text(payload, encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")

    return {"json": json_path, "markdown": md_path, "latest": latest_path}


def load_report_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)
