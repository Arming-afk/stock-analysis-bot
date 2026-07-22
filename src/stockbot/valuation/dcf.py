"""Discounted cash flow valuation. Pure deterministic code — no LLM involvement.

Same inputs always produce the same fair_value, to the last cent.

Order of operations is load-bearing: the applicability gate runs FIRST, and when
it fails no DCF math is executed at all. A ticker that fails the gate carries
`fair_value=None` all the way through, which is what makes it impossible for the
decision engine to hand it a BUY or for the confidence layer to score it.
"""

from __future__ import annotations

import statistics

from ..config import Config
from ..logging_setup import get_logger
from ..models import DCFResult, Fundamentals, GateResult, SensitivityResult

log = get_logger("valuation.dcf")


# --------------------------------------------------------------------------
# Step 1 — applicability gate
# --------------------------------------------------------------------------


def run_gate(fundamentals: Fundamentals, cfg: Config) -> GateResult:
    """Decide whether a DCF is meaningful for this ticker at all.

    A "stable" sensitivity result on a company whose cash flows a DCF cannot
    describe is false precision. Catching that here is cheaper than explaining
    it later.
    """
    g = cfg.get("dcf.gate", {})
    lookback = int(g.get("lookback_years", 5))
    min_years = int(g.get("min_years", 3))
    max_negative = int(g.get("max_negative_years", 0))
    max_cv = float(g.get("max_fcf_cv", 0.60))
    require_positive_latest = bool(g.get("require_positive_latest", True))

    if fundamentals.price <= 0 or fundamentals.shares_outstanding <= 0:
        return GateResult(
            applicable=False,
            reason="missing_market_data: price or shares outstanding unavailable",
        )

    # Most recent `lookback` fiscal years, newest first.
    years = sorted(fundamentals.fcf_history, key=lambda y: y.fiscal_year, reverse=True)[:lookback]
    series = [y.free_cash_flow for y in years]
    n = len(series)

    if n < min_years:
        return GateResult(
            applicable=False,
            reason=f"insufficient_fcf_history: {n} year(s) available, {min_years} required",
            years_used=n,
            fcf_series=series,
        )

    negative_years = sum(1 for f in series if f < 0)
    if negative_years > max_negative:
        return GateResult(
            applicable=False,
            reason=(
                f"negative_fcf: {negative_years} of {n} trailing years have negative free "
                f"cash flow (limit {max_negative})"
            ),
            years_used=n,
            negative_years=negative_years,
            fcf_series=series,
        )

    latest = series[0]
    if require_positive_latest and latest <= 0:
        return GateResult(
            applicable=False,
            reason="negative_latest_fcf: most recent fiscal year free cash flow is not positive",
            years_used=n,
            negative_years=negative_years,
            fcf_series=series,
        )

    mean = statistics.fmean(series)
    if mean <= 0:
        return GateResult(
            applicable=False,
            reason="non_positive_mean_fcf: trailing average free cash flow is not positive",
            years_used=n,
            negative_years=negative_years,
            fcf_series=series,
        )

    cv = statistics.pstdev(series) / abs(mean)
    if cv > max_cv:
        return GateResult(
            applicable=False,
            reason=(
                f"fcf_too_volatile: coefficient of variation {cv:.2f} exceeds limit {max_cv:.2f}"
            ),
            years_used=n,
            negative_years=negative_years,
            fcf_cv=cv,
            fcf_series=series,
        )

    return GateResult(
        applicable=True,
        reason="ok",
        years_used=n,
        negative_years=negative_years,
        fcf_cv=cv,
        fcf_series=series,
    )


# --------------------------------------------------------------------------
# Step 2 — inputs
# --------------------------------------------------------------------------


def estimate_growth(series: list[float], cfg: Config) -> float:
    """Historical FCF CAGR over the trailing window, clamped.

    `series` is newest-first. The clamp keeps one freak year from producing a
    runaway projection, and keeps the result inside a band a human would accept.
    """
    lo, hi = cfg.get("dcf.growth_clamp", [0.0, 0.12])
    if len(series) < 2:
        return max(lo, min(hi, 0.0))

    latest, oldest = series[0], series[-1]
    periods = len(series) - 1
    if oldest <= 0 or latest <= 0:
        cagr = 0.0
    else:
        cagr = (latest / oldest) ** (1.0 / periods) - 1.0
    return max(lo, min(hi, cagr))


def compute_discount_rate(fundamentals: Fundamentals, cfg: Config) -> tuple[float, bool]:
    """WACC via CAPM, clamped. Returns (rate, was_estimated).

    was_estimated is True when we had to substitute a default for a missing
    input — it is surfaced in the report rather than hidden.
    """
    rf = float(cfg.get("dcf.risk_free_rate", 0.042))
    erp = float(cfg.get("dcf.equity_risk_premium", 0.055))
    default_beta = float(cfg.get("dcf.default_beta", 1.10))
    tax = float(cfg.get("dcf.tax_rate", 0.21))
    cost_of_debt = float(cfg.get("dcf.default_cost_of_debt", 0.055))
    default_rate = float(cfg.get("dcf.default_discount_rate", 0.090))
    lo, hi = cfg.get("dcf.discount_clamp", [0.06, 0.15])
    terminal_growth = float(cfg.get("dcf.terminal_growth", 0.025))
    min_spread = float(cfg.get("dcf.min_spread_over_terminal", 0.005))

    estimated = False

    beta = fundamentals.beta
    if beta is None or beta <= 0:
        beta = default_beta
        estimated = True

    equity = fundamentals.market_cap
    if not equity or equity <= 0:
        equity = fundamentals.price * fundamentals.shares_outstanding
    debt = max(fundamentals.total_debt, 0.0)

    if equity + debt <= 0:
        rate = default_rate
        estimated = True
    else:
        we = equity / (equity + debt)
        wd = debt / (equity + debt)
        cost_equity = rf + beta * erp
        rate = we * cost_equity + wd * cost_of_debt * (1.0 - tax)

    rate = max(lo, min(hi, rate))

    # Gordon growth requires a strictly positive denominator with headroom.
    floor = terminal_growth + min_spread
    if rate <= floor:
        rate = floor
        estimated = True

    return rate, estimated


def select_base_fcf(series: list[float], cfg: Config) -> float:
    """`series` is newest-first."""
    method = str(cfg.get("dcf.base_fcf_method", "latest")).lower()
    if method == "avg3" and len(series) >= 3:
        return statistics.fmean(series[:3])
    return series[0]


# --------------------------------------------------------------------------
# Step 3 — the DCF itself
# --------------------------------------------------------------------------


def _dcf_fair_value(
    base_fcf: float,
    growth: float,
    discount: float,
    terminal_growth: float,
    years: int,
    net_debt: float,
    shares: float,
) -> dict:
    """Two-stage DCF: explicit projection window, then Gordon growth terminal value."""
    projected: list[float] = []
    pv_projected: list[float] = []

    for t in range(1, years + 1):
        fcf_t = base_fcf * (1.0 + growth) ** t
        projected.append(fcf_t)
        pv_projected.append(fcf_t / (1.0 + discount) ** t)

    terminal_value = projected[-1] * (1.0 + terminal_growth) / (discount - terminal_growth)
    pv_terminal = terminal_value / (1.0 + discount) ** years

    enterprise_value = sum(pv_projected) + pv_terminal
    equity_value = enterprise_value - net_debt
    fair_value = equity_value / shares

    return {
        "projected_fcf": projected,
        "pv_projected_fcf": pv_projected,
        "terminal_value": terminal_value,
        "pv_terminal_value": pv_terminal,
        "enterprise_value": enterprise_value,
        "equity_value": equity_value,
        "fair_value": fair_value,
    }


def _run_sensitivity(
    base_fcf: float,
    growth: float,
    discount: float,
    terminal_growth: float,
    years: int,
    net_debt: float,
    shares: float,
    price: float,
    base_gap: float,
    cfg: Config,
) -> SensitivityResult:
    """Re-run the DCF across the +/-1% and +/-2% grid from the spec."""
    growth_deltas = cfg.get("dcf.sensitivity.growth_deltas", [-0.02, -0.01, 0.0, 0.01, 0.02])
    discount_deltas = cfg.get("dcf.sensitivity.discount_deltas", [-0.02, -0.01, 0.0, 0.01, 0.02])
    min_spread = float(cfg.get("dcf.min_spread_over_terminal", 0.005))
    floor = terminal_growth + min_spread

    fair_values: list[float] = []
    gaps: list[float] = []

    for dg in growth_deltas:
        for dd in discount_deltas:
            g = growth + dg
            r = max(floor, discount + dd)
            out = _dcf_fair_value(base_fcf, g, r, terminal_growth, years, net_debt, shares)
            fv = out["fair_value"]
            fair_values.append(fv)
            gaps.append((fv - price) / price)

    mean_fv = statistics.fmean(fair_values)
    cv = statistics.pstdev(fair_values) / abs(mean_fv) if mean_fv else float("inf")
    base_positive = base_gap > 0
    sign_agreement = sum(1 for gp in gaps if (gp > 0) == base_positive) / len(gaps)

    return SensitivityResult(
        fair_values=fair_values,
        gaps=gaps,
        mean_fair_value=mean_fv,
        cv=cv,
        sign_agreement=sign_agreement,
        min_fair_value=min(fair_values),
        max_fair_value=max(fair_values),
    )


def value_ticker(fundamentals: Fundamentals, cfg: Config) -> DCFResult:
    """Gate, then value. Returns a gate-only result when the gate fails."""
    gate = run_gate(fundamentals, cfg)
    if not gate.applicable:
        log.info("%s: DCF not applicable — %s", fundamentals.ticker, gate.reason)
        return DCFResult(ticker=fundamentals.ticker, gate=gate)

    years = int(cfg.get("dcf.projection_years", 5))
    terminal_growth = float(cfg.get("dcf.terminal_growth", 0.025))

    base_fcf = select_base_fcf(gate.fcf_series, cfg)
    growth = estimate_growth(gate.fcf_series, cfg)
    discount, wacc_estimated = compute_discount_rate(fundamentals, cfg)
    net_debt = fundamentals.total_debt - fundamentals.cash_and_equivalents

    core = _dcf_fair_value(
        base_fcf=base_fcf,
        growth=growth,
        discount=discount,
        terminal_growth=terminal_growth,
        years=years,
        net_debt=net_debt,
        shares=fundamentals.shares_outstanding,
    )

    fair_value = core["fair_value"]
    gap = (fair_value - fundamentals.price) / fundamentals.price

    sensitivity = _run_sensitivity(
        base_fcf=base_fcf,
        growth=growth,
        discount=discount,
        terminal_growth=terminal_growth,
        years=years,
        net_debt=net_debt,
        shares=fundamentals.shares_outstanding,
        price=fundamentals.price,
        base_gap=gap,
        cfg=cfg,
    )

    log.info(
        "%s: fair_value=%.2f price=%.2f gap=%+.1f%% (r=%.2f%%, g=%.2f%%)",
        fundamentals.ticker, fair_value, fundamentals.price, gap * 100,
        discount * 100, growth * 100,
    )

    return DCFResult(
        ticker=fundamentals.ticker,
        gate=gate,
        fair_value=fair_value,
        valuation_gap_pct=gap,
        base_fcf=base_fcf,
        growth_rate=growth,
        discount_rate=discount,
        terminal_growth=terminal_growth,
        projected_fcf=core["projected_fcf"],
        pv_projected_fcf=core["pv_projected_fcf"],
        terminal_value=core["terminal_value"],
        pv_terminal_value=core["pv_terminal_value"],
        enterprise_value=core["enterprise_value"],
        net_debt=net_debt,
        equity_value=core["equity_value"],
        wacc_estimated=wacc_estimated,
        sensitivity=sensitivity,
    )
