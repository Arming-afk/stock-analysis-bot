"""Risk check — code only.

BUY  : concentration limits. A breach downgrades the BUY to WATCH.
SELL : attaches cost basis and holding period to the report.

There is deliberately no tax logic here. The report surfaces the facts (term,
unrealized P/L, days held) so the tax impact can be weighed by hand before
acting on a SELL.
"""

from __future__ import annotations

from datetime import date

from ..config import Config
from ..logging_setup import get_logger
from ..models import Portfolio, PositionInfo, RiskResult, Signal

log = get_logger("decision.risk")


def _portfolio_value(portfolio: Portfolio, prices: dict[str, float]) -> float:
    total = portfolio.cash
    for h in portfolio.holdings:
        price = prices.get(h.ticker.upper())
        if price:
            total += h.quantity * price
    return total


def _sector_value(portfolio: Portfolio, prices: dict[str, float], sector: str | None) -> float:
    if not sector:
        return 0.0
    total = 0.0
    for h in portfolio.holdings:
        if (h.sector or "").lower() != sector.lower():
            continue
        price = prices.get(h.ticker.upper())
        if price:
            total += h.quantity * price
    return total


def _position_info(
    portfolio: Portfolio, ticker: str, price: float, as_of: date, cfg: Config
) -> PositionInfo | None:
    holding = portfolio.get(ticker)
    if holding is None or holding.quantity <= 0:
        return None

    market_value = holding.quantity * price
    total_cost = holding.total_cost
    pnl = market_value - total_cost
    pnl_pct = (pnl / total_cost) if total_cost else 0.0

    days = holding.holding_period_days(as_of)
    long_term_days = int(cfg.get("risk.long_term_holding_days", 365))
    term = None if days is None else ("long" if days > long_term_days else "short")

    return PositionInfo(
        quantity=holding.quantity,
        cost_basis_per_share=holding.cost_basis_per_share,
        total_cost=total_cost,
        market_value=market_value,
        unrealized_pnl=pnl,
        unrealized_pnl_pct=pnl_pct,
        holding_period_days=days,
        term=term,
    )


def apply_risk_checks(
    ticker: str,
    signal: Signal,
    portfolio: Portfolio,
    prices: dict[str, float],
    price: float,
    sector: str | None,
    as_of: date,
    cfg: Config,
) -> tuple[Signal, RiskResult]:
    """Returns the (possibly downgraded) signal plus the risk detail."""
    result = RiskResult()
    total = _portfolio_value(portfolio, prices)

    holding = portfolio.get(ticker)
    position_value = (holding.quantity * price) if holding else 0.0

    if total > 0:
        result.ticker_weight = position_value / total
        result.sector_weight = _sector_value(portfolio, prices, sector) / total

    if signal is Signal.BUY:
        max_ticker = float(cfg.get("risk.max_ticker_pct", 0.15))
        max_sector = float(cfg.get("risk.max_sector_pct", 0.35))

        if total <= 0:
            result.breaches.append(
                "portfolio_value_unknown: concentration limits could not be checked"
            )
        else:
            if result.ticker_weight is not None and result.ticker_weight >= max_ticker:
                result.breaches.append(
                    f"ticker_concentration: {ticker} is {result.ticker_weight:.1%} of the "
                    f"portfolio, limit {max_ticker:.0%}"
                )
            if sector and result.sector_weight is not None and result.sector_weight >= max_sector:
                result.breaches.append(
                    f"sector_concentration: {sector} is {result.sector_weight:.1%} of the "
                    f"portfolio, limit {max_sector:.0%}"
                )

        if result.breaches:
            log.info("%s: BUY -> WATCH (%s)", ticker, "; ".join(result.breaches))
            result.downgraded = True
            result.original_signal = Signal.BUY
            signal = Signal.WATCH

    if signal is Signal.SELL:
        result.position = _position_info(portfolio, ticker, price, as_of, cfg)

    return signal, result
