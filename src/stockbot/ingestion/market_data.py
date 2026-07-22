"""Prices and fundamentals — the numeric inputs the DCF runs on.

Webull OpenAPI covers the account side (holdings, cash) but not multi-year cash
flow statements, so fundamentals come from a separate market data provider.
yfinance is the default; the Protocol below is the seam to swap in a paid feed
later without touching the valuation code.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Protocol

from ..config import Config, resolve_path
from ..logging_setup import get_logger
from ..models import FcfYear, Fundamentals

log = get_logger("ingestion.market_data")


class MarketDataProvider(Protocol):
    def fundamentals(self, ticker: str) -> Fundamentals: ...


# Statement row labels vary by yfinance version and by filer. Try in order.
_OCF_ROWS = (
    "Operating Cash Flow",
    "Total Cash From Operating Activities",
    "Cash Flow From Continuing Operating Activities",
)
_CAPEX_ROWS = (
    "Capital Expenditure",
    "Capital Expenditures",
    "Purchase Of PPE",
)
_FCF_ROWS = ("Free Cash Flow",)
_DEBT_ROWS = ("Total Debt", "Long Term Debt And Capital Lease Obligation")
_CASH_ROWS = (
    "Cash And Cash Equivalents",
    "Cash Cash Equivalents And Short Term Investments",
    "Cash And Cash Equivalents At Carrying Value",
)


def _is_num(x: Any) -> bool:
    return isinstance(x, (int, float)) and not (isinstance(x, float) and math.isnan(x))


def _row(df, candidates: tuple[str, ...]) -> dict[int, float]:
    """Extract {fiscal_year: value} for the first matching row label."""
    if df is None or getattr(df, "empty", True):
        return {}
    for label in candidates:
        if label not in df.index:
            continue
        series = df.loc[label]
        out: dict[int, float] = {}
        for col, val in series.items():
            year = getattr(col, "year", None)
            if year is not None and _is_num(val):
                out[int(year)] = float(val)
        if out:
            return out
    return {}


def _latest(mapping: dict[int, float], default: float = 0.0) -> float:
    if not mapping:
        return default
    return mapping[max(mapping)]


class YFinanceMarketData:
    def fundamentals(self, ticker: str) -> Fundamentals:
        import yfinance as yf

        errors: list[str] = []
        t = yf.Ticker(ticker)

        try:
            info = t.info or {}
        except Exception as exc:
            info = {}
            errors.append(f"info_unavailable: {exc}")

        price = 0.0
        for key in ("currentPrice", "regularMarketPrice", "previousClose"):
            if _is_num(info.get(key)):
                price = float(info[key])
                break
        if price <= 0:
            try:
                hist = t.history(period="5d")
                if not hist.empty:
                    price = float(hist["Close"].iloc[-1])
            except Exception as exc:
                errors.append(f"price_unavailable: {exc}")

        try:
            cashflow = t.cashflow
        except Exception as exc:
            cashflow = None
            errors.append(f"cashflow_unavailable: {exc}")

        try:
            balance = t.balance_sheet
        except Exception as exc:
            balance = None
            errors.append(f"balance_sheet_unavailable: {exc}")

        ocf = _row(cashflow, _OCF_ROWS)
        capex = _row(cashflow, _CAPEX_ROWS)
        fcf_direct = _row(cashflow, _FCF_ROWS)

        fcf_history: list[FcfYear] = []
        for year in sorted(set(ocf) | set(fcf_direct), reverse=True):
            if year in ocf and year in capex:
                # yfinance reports capex as a negative outflow; FCF = OCF - capex
                # so the sign has to be normalised to a positive spend.
                fcf_history.append(
                    FcfYear(
                        fiscal_year=year,
                        operating_cash_flow=ocf[year],
                        capital_expenditure=abs(capex[year]),
                    )
                )
            elif year in fcf_direct:
                fcf_history.append(
                    FcfYear(
                        fiscal_year=year,
                        operating_cash_flow=fcf_direct[year],
                        capital_expenditure=0.0,
                    )
                )

        if not fcf_history:
            errors.append("no_fcf_history: cash flow statement rows not found")

        shares = 0.0
        for key in ("sharesOutstanding", "impliedSharesOutstanding", "floatShares"):
            if _is_num(info.get(key)) and info[key] > 0:
                shares = float(info[key])
                break

        beta = float(info["beta"]) if _is_num(info.get("beta")) else None
        market_cap = float(info["marketCap"]) if _is_num(info.get("marketCap")) else None

        total_debt = (
            float(info["totalDebt"]) if _is_num(info.get("totalDebt")) else _latest(_row(balance, _DEBT_ROWS))
        )
        cash_eq = (
            float(info["totalCash"]) if _is_num(info.get("totalCash")) else _latest(_row(balance, _CASH_ROWS))
        )

        fundamentals = Fundamentals(
            ticker=ticker.upper(),
            price=price,
            shares_outstanding=shares,
            fcf_history=fcf_history,
            total_debt=total_debt,
            cash_and_equivalents=cash_eq,
            beta=beta,
            market_cap=market_cap,
            sector=info.get("sector"),
            currency=info.get("currency", "USD"),
            fetch_errors=errors,
        )
        log.info(
            "%s: price=%.2f shares=%.3gM fcf_years=%d%s",
            ticker, price, shares / 1e6 if shares else 0, len(fcf_history),
            f" errors={errors}" if errors else "",
        )
        return fundamentals


class FixtureMarketData:
    """Reads a JSON snapshot. Lets the pipeline run with no network at all."""

    def __init__(self, path: Path):
        self.path = path
        with path.open("r", encoding="utf-8") as fh:
            self._data: dict[str, Any] = json.load(fh)

    def fundamentals(self, ticker: str) -> Fundamentals:
        raw = self._data.get(ticker.upper())
        if raw is None:
            return Fundamentals(
                ticker=ticker.upper(), price=0.0, shares_outstanding=0.0,
                fetch_errors=[f"no_fixture_for_{ticker}"],
            )
        return Fundamentals(
            ticker=ticker.upper(),
            price=float(raw["price"]),
            shares_outstanding=float(raw["shares_outstanding"]),
            fcf_history=[
                FcfYear(
                    fiscal_year=int(y["fiscal_year"]),
                    operating_cash_flow=float(y["operating_cash_flow"]),
                    capital_expenditure=float(y["capital_expenditure"]),
                )
                for y in raw.get("fcf_history", [])
            ],
            total_debt=float(raw.get("total_debt", 0.0)),
            cash_and_equivalents=float(raw.get("cash_and_equivalents", 0.0)),
            beta=raw.get("beta"),
            market_cap=raw.get("market_cap"),
            sector=raw.get("sector"),
            currency=raw.get("currency", "USD"),
        )


def load_market_data_provider(cfg: Config, offline: bool = False) -> MarketDataProvider:
    if offline:
        path = resolve_path("data/fixtures/market_data.json")
        if not path.exists():
            raise FileNotFoundError(
                f"offline mode needs a fixture at {path} — copy market_data.example.json"
            )
        log.info("using offline fixture data from %s", path)
        return FixtureMarketData(path)
    return YFinanceMarketData()
