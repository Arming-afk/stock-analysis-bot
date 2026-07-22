"""The redaction that stands between a public repo and the whole portfolio.

These tests walk the published payload recursively looking for anything that
could disclose a position, so a field added to the report later cannot leak by
being forgotten here.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from conftest import ROOT, make_fundamentals, make_portfolio

sys.path.insert(0, str(ROOT / "tools"))

from publish import public_rationale, redact_report  # noqa: E402

from stockbot.models import to_dict  # noqa: E402

FORBIDDEN_KEYS = {
    "quantity",
    "cost_basis_per_share",
    "total_cost",
    "market_value",
    "unrealized_pnl",
    "unrealized_pnl_pct",
    "holding_period_days",
    "portfolio_value",
    "cash",
    "ticker_weight",
    "sector_weight",
}


def _walk(node, path="$"):
    """Yield (path, key, value) for every mapping entry in the tree."""
    if isinstance(node, dict):
        for k, v in node.items():
            yield f"{path}.{k}", k, v
            yield from _walk(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _walk(v, f"{path}[{i}]")


@pytest.fixture
def full_report(cfg, tmp_path, monkeypatch):
    """A real run containing a held, profitable, sellable position."""
    import copy

    import stockbot.pipeline as pipeline
    from stockbot.config import Config, Secrets

    data = copy.deepcopy(cfg.as_dict())
    data["watchlist"] = ["RICHCO"]
    data["output"]["db_path"] = str(tmp_path / "t.db")
    data["output"]["report_dir"] = str(tmp_path / "r")
    data["output"]["push_enabled"] = False
    test_cfg = Config(data, Secrets())

    class _Market:
        def fundamentals(self, ticker):
            # Priced far above fair value so the signal comes out SELL and the
            # position block is populated.
            return make_fundamentals(ticker, price=900.0, sector="Technology")

    portfolio = make_portfolio([("RICHCO", 250, 12.34, "Technology")], cash=98_765.43)
    monkeypatch.setattr(pipeline, "load_portfolio", lambda *a, **k: portfolio)

    report = pipeline.run_daily(test_cfg, offline=True, skip_news=True, market_data=_Market())
    return to_dict(report)


def test_the_fixture_actually_contains_a_position(full_report):
    """Guard the guard — if this stops holding, the leak tests prove nothing."""
    t = full_report["tickers"][0]
    assert t["signal"] == "SELL"
    assert t["risk"]["position"] is not None
    assert t["risk"]["position"]["quantity"] == 250
    assert full_report["portfolio_value"] > 0


def test_no_position_bearing_key_survives_redaction(full_report):
    published = redact_report(full_report)
    offenders = [
        (path, key)
        for path, key, value in _walk(published)
        if key in FORBIDDEN_KEYS and value is not None
    ]
    assert offenders == [], f"position data leaked: {offenders}"


def test_position_block_is_emptied(full_report):
    published = redact_report(full_report)
    assert published["tickers"][0]["risk"]["position"] is None


def test_no_raw_position_numbers_anywhere_in_the_payload(full_report):
    """Scan the serialized text, so free-form strings are covered too."""
    published = json.dumps(redact_report(full_report))
    for needle in ("98765", "98,765", "12.34", "250 sh", "250 shares"):
        assert needle not in published, f"{needle!r} leaked into the published payload"


def test_original_llm_rationale_is_replaced(full_report):
    published = redact_report(full_report)
    t = published["tickers"][0]
    assert t["rationale_source"] == "redacted"
    assert t["rationale"] != full_report["tickers"][0]["rationale"]
    assert "unrealized" not in t["rationale"].lower()


def test_allocation_percentages_are_scrubbed_from_risk_text(full_report):
    report = full_report
    report["tickers"][0]["risk"]["downgraded"] = True
    report["tickers"][0]["risk"]["breaches"] = [
        "sector_concentration: Technology is 54.4% of the portfolio, limit 35%"
    ]
    published = redact_report(report)
    breach = published["tickers"][0]["risk"]["breaches"][0]
    assert "54.4%" not in breach
    assert "[redacted]" in breach


def test_signals_and_valuation_survive(full_report):
    """Redaction must not gut the thing — the decision content is the point."""
    published = redact_report(full_report)
    t = published["tickers"][0]
    assert t["signal"] == "SELL"
    assert t["price"] == 900.0
    assert t["dcf"]["fair_value"] is not None
    assert t["dcf"]["valuation_gap_pct"] is not None
    assert t["decision"]["rule"]
    assert t["decision"]["held"] is True
    assert t["rationale"]
    assert published["redacted"] is True


def test_portfolio_totals_are_absent_not_zeroed(full_report):
    published = redact_report(full_report)
    # Absent, so the dashboard can hide the row rather than render "$0.00".
    assert "portfolio_value" not in published
    assert "cash" not in published


def test_public_rationale_mentions_the_gate_reason(cfg, full_report):
    t = full_report["tickers"][0]
    t["dcf"]["gate"]["applicable"] = False
    t["dcf"]["gate"]["reason"] = "fcf_too_volatile: coefficient of variation 1.04"
    text = public_rationale(t)
    assert "fcf_too_volatile" in text
