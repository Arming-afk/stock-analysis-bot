"""Portfolio ingestion.

Two providers:

  WebullPortfolio   — Webull OpenAPI, authenticated with an app key/secret pair
                      generated in the Webull web console. The account password
                      is never requested, transmitted, or stored by this system.

  LocalFilePortfolio — reads data/portfolio.json. Used when no Webull
                      credentials are configured, and for offline runs.

`load_portfolio` picks Webull when credentials exist and falls back to the local
file otherwise, so a first run works before any brokerage setup is done.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any, Protocol

from ..config import Config, resolve_path
from ..logging_setup import get_logger
from ..models import Holding, Portfolio

log = get_logger("ingestion.portfolio")

# Webull OpenAPI regional endpoints.
_ENDPOINTS = {
    "us": ("us-east-1", "api.webull.com"),
    "hk": ("hk", "api.webull.hk"),
}


class PortfolioProvider(Protocol):
    def fetch(self) -> Portfolio: ...


def _parse_date(value: Any) -> date | None:
    if not value:
        return None
    if isinstance(value, date):
        return value
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text[: len(fmt) + 2], fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        log.debug("could not parse acquisition date %r", value)
        return None


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class LocalFilePortfolio:
    """Reads a hand-maintained JSON file. See data/portfolio.example.json."""

    def __init__(self, path: Path):
        self.path = path

    def fetch(self) -> Portfolio:
        if not self.path.exists():
            log.warning("portfolio file not found at %s — treating as empty", self.path)
            return Portfolio(holdings=[], cash=0.0, source="local_file(missing)")

        with self.path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)

        holdings = [
            Holding(
                ticker=str(h["ticker"]).upper(),
                quantity=_to_float(h.get("quantity")),
                cost_basis_per_share=_to_float(h.get("cost_basis_per_share")),
                acquired_date=_parse_date(h.get("acquired_date")),
                sector=h.get("sector"),
            )
            for h in data.get("holdings", [])
        ]
        portfolio = Portfolio(
            holdings=holdings,
            cash=_to_float(data.get("cash")),
            as_of=_parse_date(data.get("as_of")) or date.today(),
            source="local_file",
        )
        log.info("loaded %d holding(s) and %.2f cash from %s", len(holdings), portfolio.cash, self.path)
        return portfolio


class WebullPortfolio:
    """Webull OpenAPI provider (official SDK).

    Requires:  pip install webull-python-sdk-core webull-python-sdk-trade

    The SDK's account methods have shifted names across releases, so the calls
    below probe a few known spellings rather than hard-coding one. If none
    resolve, the error names the SDK object so the correct method can be wired
    up against whichever version is installed.
    """

    _BALANCE_METHODS = ("get_account_balance", "get_account_balance_v2", "get_balance")
    _POSITION_METHODS = ("get_account_position", "get_account_positions", "get_positions")

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.account_id = cfg.secrets.webull_account_id
        self.region = cfg.secrets.webull_region or "us"

    def _api(self):
        try:
            from webullsdkcore.client import ApiClient  # type: ignore
            from webullsdktrade.api import API  # type: ignore
        except ImportError as exc:  # pragma: no cover - depends on optional dep
            raise RuntimeError(
                "Webull SDK not installed. Run:\n"
                "  pip install webull-python-sdk-core webull-python-sdk-trade"
            ) from exc

        region_id, host = _ENDPOINTS.get(self.region, _ENDPOINTS["us"])
        client = ApiClient(self.cfg.secrets.webull_app_key, self.cfg.secrets.webull_app_secret)
        client.add_endpoint(region_id, host)
        return API(client)

    @staticmethod
    def _call(obj: Any, names: tuple[str, ...], *args):
        for name in names:
            fn = getattr(obj, name, None)
            if callable(fn):
                return fn(*args)
        available = [n for n in dir(obj) if not n.startswith("_")]
        raise RuntimeError(
            f"none of {names} exist on {type(obj).__name__}. Available: {available}"
        )

    @staticmethod
    def _payload(response: Any) -> dict:
        """Unwrap whichever envelope the SDK version returns."""
        if hasattr(response, "json"):
            try:
                response = response.json()
            except Exception:  # pragma: no cover
                pass
        if isinstance(response, dict):
            for key in ("data", "result"):
                if isinstance(response.get(key), (dict, list)):
                    return response[key]
            return response
        return {}

    def fetch(self) -> Portfolio:
        api = self._api()
        if not self.account_id:
            raise RuntimeError("WEBULL_ACCOUNT_ID is not set")

        balance = self._payload(self._call(api.account, self._BALANCE_METHODS, self.account_id))
        positions = self._payload(self._call(api.account, self._POSITION_METHODS, self.account_id))

        cash = 0.0
        if isinstance(balance, dict):
            buckets = balance.get("accountBalances") or balance.get("account_balances") or [balance]
            for b in buckets if isinstance(buckets, list) else [buckets]:
                if not isinstance(b, dict):
                    continue
                for key in ("cashBalance", "cash_balance", "settledFunds", "totalCashValue"):
                    if b.get(key) is not None:
                        cash = _to_float(b[key])
                        break
                if cash:
                    break

        rows = positions if isinstance(positions, list) else positions.get("positions", [])
        holdings: list[Holding] = []
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            ticker = (
                row.get("symbol")
                or (row.get("ticker") or {}).get("symbol")
                or row.get("instrumentSymbol")
            )
            qty = _to_float(row.get("quantity") or row.get("position") or row.get("totalQuantity"))
            if not ticker or qty <= 0:
                continue
            holdings.append(
                Holding(
                    ticker=str(ticker).upper(),
                    quantity=qty,
                    cost_basis_per_share=_to_float(
                        row.get("costPrice") or row.get("avgCost") or row.get("averageCost")
                    ),
                    acquired_date=_parse_date(
                        row.get("openDate") or row.get("createTime") or row.get("firstTradeDate")
                    ),
                    sector=row.get("sector"),
                )
            )

        log.info("Webull: %d position(s), cash %.2f", len(holdings), cash)
        return Portfolio(holdings=holdings, cash=cash, as_of=date.today(), source="webull")


def load_portfolio(cfg: Config, prefer_local: bool = False) -> Portfolio:
    local_path = resolve_path("data/portfolio.json")

    if cfg.secrets.has_webull and not prefer_local:
        try:
            return WebullPortfolio(cfg).fetch()
        except Exception as exc:
            log.error("Webull ingestion failed (%s) — falling back to %s", exc, local_path)

    return LocalFilePortfolio(local_path).fetch()
