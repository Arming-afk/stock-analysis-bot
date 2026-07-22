"""End-to-end run against fixture data, with the DB redirected to a temp dir."""

from __future__ import annotations

import copy

from conftest import ROOT, make_fundamentals, make_portfolio

from stockbot.config import Config, Secrets
from stockbot.models import Signal
from stockbot.storage.db import Database


class _StubMarketData:
    def __init__(self, mapping):
        self._mapping = mapping

    def fundamentals(self, ticker):
        return self._mapping[ticker]


def _isolated_cfg(cfg: Config, tmp_path) -> Config:
    data = copy.deepcopy(cfg.as_dict())
    data["watchlist"] = ["GOODCO", "BADCO"]
    data["output"]["db_path"] = str(tmp_path / "test.db")
    data["output"]["report_dir"] = str(tmp_path / "reports")
    data["output"]["push_enabled"] = False
    return Config(data, Secrets())


def _run(cfg, monkeypatch, portfolio, market_data):
    import stockbot.pipeline as pipeline

    monkeypatch.setattr(pipeline, "load_portfolio", lambda *a, **k: portfolio)
    return pipeline.run_daily(cfg, offline=True, skip_news=True, market_data=market_data)


def test_full_run_produces_signals_and_persists_them(cfg, tmp_path, monkeypatch):
    test_cfg = _isolated_cfg(cfg, tmp_path)

    market = _StubMarketData(
        {
            # Stable cash flows, priced well below the model's fair value.
            "GOODCO": make_fundamentals("GOODCO", price=20.0, sector="Healthcare"),
            # Volatile cash flows — must be gated out.
            "BADCO": make_fundamentals(
                "BADCO", price=50.0, fcf=[60e9, 27e9, 3.8e9, 8.1e9, 4.3e9], sector="Technology"
            ),
        }
    )
    portfolio = make_portfolio(cash=100_000.0)

    report = _run(test_cfg, monkeypatch, portfolio, market)

    assert {t.ticker for t in report.tickers} == {"GOODCO", "BADCO"}

    good = next(t for t in report.tickers if t.ticker == "GOODCO")
    bad = next(t for t in report.tickers if t.ticker == "BADCO")

    assert good.dcf.applicable
    assert good.signal is Signal.BUY
    assert good.rationale                      # fallback prose, no key needed
    assert good.rationale_source == "fallback"

    assert not bad.dcf.applicable
    assert bad.signal is Signal.WATCH
    assert bad.dcf.fair_value is None

    # Phase 0 ships without the confidence score.
    assert report.phase == 0
    assert all(t.confidence is None for t in report.tickers)

    # Stored for later backtesting.
    db = Database(tmp_path / "test.db")
    try:
        stored = db.latest_report()
        assert stored["run_date"] == report.run_date.isoformat()
        assert len(stored["tickers"]) == 2
        history = db.ticker_history("GOODCO")
        assert history and history[0]["signal"] == "BUY"
    finally:
        db.close()

    assert (tmp_path / "reports" / "latest.json").exists()
    assert (tmp_path / "reports" / f"{report.run_date.isoformat()}.md").exists()


def test_holdings_are_analyzed_even_when_not_on_the_watchlist(cfg, tmp_path, monkeypatch):
    test_cfg = _isolated_cfg(cfg, tmp_path)
    market = _StubMarketData(
        {
            "GOODCO": make_fundamentals("GOODCO", price=20.0),
            "BADCO": make_fundamentals("BADCO", price=50.0),
            "HELDCO": make_fundamentals("HELDCO", price=20.0, sector="Energy"),
        }
    )
    portfolio = make_portfolio([("HELDCO", 100, 10.0, "Energy")], cash=100_000.0)

    report = _run(test_cfg, monkeypatch, portfolio, market)
    assert "HELDCO" in {t.ticker for t in report.tickers}


def test_offline_run_needs_no_network_and_no_api_key(cfg, tmp_path, monkeypatch):
    test_cfg = _isolated_cfg(cfg, tmp_path)
    assert not test_cfg.secrets.has_fireworks

    market = _StubMarketData(
        {
            "GOODCO": make_fundamentals("GOODCO", price=20.0),
            "BADCO": make_fundamentals("BADCO", price=50.0),
        }
    )
    report = _run(test_cfg, monkeypatch, make_portfolio(cash=50_000.0), market)

    assert report.tickers
    assert any("FIREWORKS_API_KEY not set" in e for e in report.errors)
    # The missing key degrades the wording only — signals still exist.
    assert all(t.signal in set(Signal) for t in report.tickers)


def test_shipped_fixture_file_exercises_every_branch(cfg, tmp_path, monkeypatch):
    """The offline demo data should keep covering all five outcomes."""
    from stockbot.ingestion.market_data import FixtureMarketData

    fixture = ROOT / "data" / "fixtures" / "market_data.example.json"
    provider = FixtureMarketData(fixture)

    data = copy.deepcopy(cfg.as_dict())
    data["watchlist"] = ["MSFT", "GOOGL", "NVDA", "KO", "JNJ", "ASML"]
    data["output"]["db_path"] = str(tmp_path / "t.db")
    data["output"]["report_dir"] = str(tmp_path / "r")
    data["output"]["push_enabled"] = False
    test_cfg = Config(data, Secrets())

    portfolio = make_portfolio(
        [
            ("MSFT", 40, 310.0, "Technology"),
            ("GOOGL", 100, 96.5, "Technology"),
            ("NVDA", 50, 118.0, "Technology"),
            ("KO", 200, 54.2, "Consumer Defensive"),
        ],
        cash=15_000.0,
    )

    report = _run(test_cfg, monkeypatch, portfolio, provider)
    signals = {t.ticker: t.signal for t in report.tickers}

    assert signals["JNJ"] is Signal.BUY                    # large discount, not held
    assert signals["MSFT"] is Signal.SELL                  # overvalued, held
    assert signals["GOOGL"] is Signal.HOLD                 # near fair value, held
    assert signals["NVDA"] is Signal.WATCH                 # gate failed
    assert signals["ASML"] is Signal.WATCH                 # BUY downgraded by sector limit

    asml = next(t for t in report.tickers if t.ticker == "ASML")
    assert asml.risk.downgraded
    assert asml.risk.original_signal is Signal.BUY
