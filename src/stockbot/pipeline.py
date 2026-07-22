"""The daily run. One linear pass, in the order the spec lays out:

    ingest -> DCF (code) + news sentiment (LLM classify) -> decision (code)
           -> confidence (code, phase 1) -> risk (code) -> explain (LLM prose)
           -> store + deliver

The LLM appears exactly twice, and neither position can influence a signal:
the classifier upstream only emits a category, and the narrator downstream runs
after every number is already fixed.
"""

from __future__ import annotations

from datetime import date, datetime

from .config import Config, resolve_path
from .decision.confidence import apply_confidence_gate, compute_confidence
from .decision.engine import decide
from .decision.risk import apply_risk_checks
from .explain.explainer import explain
from .ingestion.market_data import MarketDataProvider, load_market_data_provider
from .ingestion.portfolio import load_portfolio
from .llm.fireworks import FireworksClient
from .logging_setup import get_logger
from .models import (
    DailyReport,
    Fundamentals,
    NewsResult,
    Portfolio,
    Sentiment,
    Signal,
    Strength,
    TickerReport,
)
from .news.fetch import fetch_articles
from .news.sentiment import analyze_news
from .output.push import collect_subscriptions, send_push
from .output.report import push_summary, write_report_files
from .storage.db import Database
from .valuation.dcf import value_ticker

log = get_logger("pipeline")


def build_universe(portfolio: Portfolio, cfg: Config, only: list[str] | None = None) -> list[str]:
    """Holdings are always analyzed; the watchlist adds buy candidates."""
    if only:
        return [t.upper() for t in only]
    universe = {h.ticker.upper() for h in portfolio.holdings if h.quantity > 0}
    universe |= set(cfg.watchlist)
    return sorted(universe)


def _analyze_ticker(
    ticker: str,
    portfolio: Portfolio,
    fundamentals: Fundamentals,
    prices: dict[str, float],
    client: FireworksClient,
    cfg: Config,
    run_date: date,
    skip_news: bool,
) -> TickerReport:
    errors: list[str] = list(fundamentals.fetch_errors)

    # --- 2. DCF (code) ----------------------------------------------------
    dcf = value_ticker(fundamentals, cfg)

    # --- 3. News + sentiment (LLM classifies categories only) -------------
    if skip_news:
        news = NewsResult(
            ticker=ticker,
            news_available=False,
            reason="news_disabled: --no-news",
            assumed_neutral=True,
            aggregate_label=Sentiment.NEUTRAL,
            aggregate_strength=Strength.WEAK,
        )
    else:
        try:
            articles = fetch_articles(ticker, cfg)
        except Exception as exc:
            log.warning("%s: news fetch failed (%s)", ticker, exc)
            articles = []
            errors.append(f"news_fetch_failed: {exc}")
        news = analyze_news(ticker, articles, client, cfg)

    # --- 4. Decision (code, rule-based) -----------------------------------
    held = portfolio.holds(ticker)
    decision = decide(ticker, dcf, news, held, cfg)

    # --- 5. Confidence (code, phase 1 only) -------------------------------
    confidence = compute_confidence(dcf, news, decision, cfg)
    decision = apply_confidence_gate(decision, confidence, cfg)

    # --- 6. Risk check (code) ---------------------------------------------
    signal, risk = apply_risk_checks(
        ticker=ticker,
        signal=decision.signal,
        portfolio=portfolio,
        prices=prices,
        price=fundamentals.price,
        sector=fundamentals.sector,
        as_of=run_date,
        cfg=cfg,
    )
    decision.signal = signal

    # --- 7. Explanation (LLM prose, decides nothing) ----------------------
    rationale, source, warnings = explain(
        ticker=ticker,
        price=fundamentals.price,
        decision=decision,
        dcf=dcf,
        news=news,
        risk=risk,
        confidence=confidence,
        client=client,
        cfg=cfg,
    )
    errors += warnings

    return TickerReport(
        ticker=ticker,
        price=fundamentals.price,
        signal=signal,
        decision=decision,
        dcf=dcf,
        news=news,
        risk=risk,
        confidence=confidence,
        rationale=rationale,
        rationale_source=source,
        errors=errors,
    )


def run_daily(
    cfg: Config,
    offline: bool = False,
    only: list[str] | None = None,
    skip_news: bool = False,
    skip_push: bool = False,
    prefer_local_portfolio: bool = False,
    market_data: MarketDataProvider | None = None,
) -> DailyReport:
    run_date = date.today()
    errors: list[str] = []

    # --- 1. Ingestion -----------------------------------------------------
    portfolio = load_portfolio(cfg, prefer_local=prefer_local_portfolio or offline)
    provider = market_data or load_market_data_provider(cfg, offline=offline)
    universe = build_universe(portfolio, cfg, only)
    log.info("analyzing %d ticker(s): %s", len(universe), ", ".join(universe))

    client = FireworksClient(cfg)
    if not client.available:
        errors.append(
            "FIREWORKS_API_KEY not set — sentiment leg disabled, explanations use "
            "deterministic text. Signals are unaffected."
        )

    fundamentals_map: dict[str, Fundamentals] = {}
    for ticker in universe:
        try:
            fundamentals_map[ticker] = provider.fundamentals(ticker)
        except Exception as exc:
            log.error("%s: fundamentals fetch failed (%s)", ticker, exc)
            fundamentals_map[ticker] = Fundamentals(
                ticker=ticker, price=0.0, shares_outstanding=0.0,
                fetch_errors=[f"fundamentals_failed: {exc}"],
            )

    prices = {t: f.price for t, f in fundamentals_map.items() if f.price > 0}

    # Sector is needed for the concentration check; backfill from market data
    # when the brokerage feed did not supply it.
    for holding in portfolio.holdings:
        if not holding.sector:
            f = fundamentals_map.get(holding.ticker.upper())
            if f and f.sector:
                holding.sector = f.sector

    # --- 2-7 per ticker ---------------------------------------------------
    reports: list[TickerReport] = []
    for ticker in universe:
        try:
            reports.append(
                _analyze_ticker(
                    ticker, portfolio, fundamentals_map[ticker], prices,
                    client, cfg, run_date, skip_news,
                )
            )
        except Exception as exc:
            log.exception("%s: analysis failed", ticker)
            errors.append(f"{ticker}: analysis failed ({exc})")

    portfolio_value = portfolio.cash + sum(
        h.quantity * prices.get(h.ticker.upper(), 0.0) for h in portfolio.holdings
    )

    report = DailyReport(
        run_date=run_date,
        generated_at=datetime.now(),
        phase=cfg.phase,
        tickers=reports,
        portfolio_value=portfolio_value,
        cash=portfolio.cash,
        portfolio_source=portfolio.source,
        errors=errors,
    )

    # --- 8. Store + deliver ----------------------------------------------
    db = Database(resolve_path(str(cfg.get("output.db_path", "data/stockbot.db"))))
    try:
        db.save_report(report)
        write_report_files(report, resolve_path(str(cfg.get("output.report_dir", "data/reports"))))

        if cfg.get("output.push_enabled", True) and not skip_push:
            title, body = push_summary(report)
            subscriptions = collect_subscriptions(cfg, db.subscriptions())
            _, dead = send_push(cfg, subscriptions, title, body)
            for endpoint in dead:
                db.delete_subscription(endpoint)
    finally:
        db.close()

    counts = {s.value: len(report.by_signal(s)) for s in Signal}
    log.info("run complete: %s", counts)
    return report
