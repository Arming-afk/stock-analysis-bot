"""SQLite history.

Every run is stored whole, including the DCF inputs and the gate verdict. That
is what makes the DCF leg backtestable later: financials are point-in-time, so
replaying stored inputs reproduces the stored fair value exactly.

The news leg is stored too, but see README — a naive backtest of it is not
trustworthy, because "news as of date X" cannot be reconstructed after the fact.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Any

from ..logging_setup import get_logger
from ..models import DailyReport, to_dict

log = get_logger("storage.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_date      TEXT NOT NULL,
    generated_at  TEXT NOT NULL,
    phase         INTEGER NOT NULL,
    portfolio_value REAL,
    cash          REAL,
    payload       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_date ON runs(run_date);

CREATE TABLE IF NOT EXISTS signals (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    run_date      TEXT NOT NULL,
    ticker        TEXT NOT NULL,
    signal        TEXT NOT NULL,
    rule          TEXT NOT NULL,
    price         REAL,
    fair_value    REAL,
    valuation_gap REAL,
    dcf_applicable INTEGER NOT NULL,
    gate_reason   TEXT,
    news_available INTEGER NOT NULL,
    sentiment     TEXT,
    confidence    REAL,
    payload       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_signals_ticker ON signals(ticker, run_date);

CREATE TABLE IF NOT EXISTS push_subscriptions (
    endpoint   TEXT PRIMARY KEY,
    payload    TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path))
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # -- writes ------------------------------------------------------------

    def save_report(self, report: DailyReport) -> int:
        payload = to_dict(report)
        cur = self._conn.execute(
            "INSERT INTO runs (run_date, generated_at, phase, portfolio_value, cash, payload)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                report.run_date.isoformat(),
                report.generated_at.isoformat(),
                report.phase,
                report.portfolio_value,
                report.cash,
                json.dumps(payload),
            ),
        )
        run_id = int(cur.lastrowid)

        for t, t_payload in zip(report.tickers, payload["tickers"]):
            self._conn.execute(
                "INSERT INTO signals (run_id, run_date, ticker, signal, rule, price, fair_value,"
                " valuation_gap, dcf_applicable, gate_reason, news_available, sentiment,"
                " confidence, payload) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    report.run_date.isoformat(),
                    t.ticker,
                    t.signal.value,
                    t.decision.rule,
                    t.price,
                    t.dcf.fair_value,
                    t.dcf.valuation_gap_pct,
                    int(t.dcf.applicable),
                    t.dcf.gate.reason,
                    int(t.news.news_available),
                    t.news.aggregate_label.value,
                    t.confidence.value if t.confidence else None,
                    json.dumps(t_payload),
                ),
            )

        self._conn.commit()
        log.info("saved run %d (%d ticker(s)) to %s", run_id, len(report.tickers), self.path)
        return run_id

    def save_subscription(self, subscription: dict[str, Any]) -> None:
        endpoint = subscription.get("endpoint")
        if not endpoint:
            raise ValueError("subscription has no endpoint")
        self._conn.execute(
            "INSERT OR REPLACE INTO push_subscriptions (endpoint, payload, created_at)"
            " VALUES (?, ?, ?)",
            (endpoint, json.dumps(subscription), datetime.now().isoformat()),
        )
        self._conn.commit()

    def delete_subscription(self, endpoint: str) -> None:
        self._conn.execute("DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,))
        self._conn.commit()

    # -- reads -------------------------------------------------------------

    def latest_report(self) -> dict | None:
        row = self._conn.execute(
            "SELECT payload FROM runs ORDER BY generated_at DESC LIMIT 1"
        ).fetchone()
        return json.loads(row["payload"]) if row else None

    def report_for_date(self, run_date: date | str) -> dict | None:
        key = run_date.isoformat() if isinstance(run_date, date) else str(run_date)
        row = self._conn.execute(
            "SELECT payload FROM runs WHERE run_date = ? ORDER BY generated_at DESC LIMIT 1",
            (key,),
        ).fetchone()
        return json.loads(row["payload"]) if row else None

    def run_dates(self, limit: int = 60) -> list[str]:
        rows = self._conn.execute(
            "SELECT DISTINCT run_date FROM runs ORDER BY run_date DESC LIMIT ?", (limit,)
        ).fetchall()
        return [r["run_date"] for r in rows]

    def ticker_history(self, ticker: str, limit: int = 90) -> list[dict]:
        rows = self._conn.execute(
            "SELECT run_date, signal, price, fair_value, valuation_gap, confidence"
            " FROM signals WHERE ticker = ? ORDER BY run_date DESC LIMIT ?",
            (ticker.upper(), limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def subscriptions(self) -> list[dict]:
        rows = self._conn.execute("SELECT payload FROM push_subscriptions").fetchall()
        return [json.loads(r["payload"]) for r in rows]
