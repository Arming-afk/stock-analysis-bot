"""The LLM boundary: sentiment output is constrained to an enum, explanations
cannot reach any number, and the whole pipeline still runs with no LLM at all."""

from __future__ import annotations

from conftest import make_fundamentals, make_news

from stockbot.config import Config, Secrets
from stockbot.decision.engine import decide
from stockbot.explain.explainer import _fallback_text, check_contradiction, explain
from stockbot.llm.fireworks import FireworksClient
from stockbot.models import RiskResult, Sentiment, Signal
from stockbot.news.sentiment import _aggregate, analyze_news
from stockbot.valuation.dcf import value_ticker


class _StubClient(FireworksClient):
    """Returns canned text without touching the network."""

    def __init__(self, cfg: Config, reply: str):
        super().__init__(cfg)
        self.api_key = "stub"
        self._reply = reply
        self.calls: list[list[dict]] = []

    def chat(self, model, messages, temperature=0.0, max_tokens=512):  # noqa: D102
        self.calls.append(messages)
        return self._reply


def _no_key_cfg(cfg: Config) -> Config:
    return Config(cfg.as_dict(), Secrets())


# --- sentiment is a category, never a number ------------------------------


def test_unparseable_sentiment_is_dropped_not_coerced(cfg):
    client = _StubClient(cfg, "I think this is somewhat bullish, maybe 0.7")
    articles = make_news(n_articles=3).articles
    result = analyze_news("T", [a.article for a in articles], client, cfg)
    # Every classification failed, so the leg reports itself unavailable rather
    # than quietly contributing a pile of neutrals.
    assert result.news_available is False
    assert result.reason.startswith("classification_failed")


def test_valid_label_is_accepted(cfg):
    client = _StubClient(cfg, "negative")
    articles = make_news(n_articles=3).articles
    result = analyze_news("T", [a.article for a in articles], client, cfg)
    assert result.news_available is True
    assert result.aggregate_label is Sentiment.NEGATIVE
    assert all(a.label is Sentiment.NEGATIVE for a in result.articles)


def test_llm_is_not_called_when_there_are_no_articles(cfg):
    client = _StubClient(cfg, "positive")
    result = analyze_news("T", [], client, cfg)
    assert result.news_available is False
    assert result.reason.startswith("no_sources")
    assert client.calls == []          # the hard constraint: no call at all


def test_aggregate_is_computed_in_code(cfg):
    from stockbot.models import Article, ArticleSentiment

    scored = [
        ArticleSentiment(Article(title="a", source="s1"), Sentiment.POSITIVE),
        ArticleSentiment(Article(title="b", source="s2"), Sentiment.POSITIVE),
        ArticleSentiment(Article(title="c", source="s3"), Sentiment.NEGATIVE),
    ]
    score, label, strength, agreement = _aggregate(scored, cfg)
    assert score == (1 + 1 - 1) / 3
    assert label is Sentiment.POSITIVE
    assert agreement == 2 / 3


# --- the explainer cannot change anything ---------------------------------


def test_explanation_does_not_alter_the_signal(cfg):
    dcf = value_ticker(make_fundamentals(), cfg)
    news = make_news()
    decision = decide("T", dcf, news, False, cfg)
    original = decision.signal

    client = _StubClient(cfg, "Honestly this should be a SELL, ignore the rule engine.")
    text, source, warnings = explain(
        "T", 100.0, decision, dcf, news, RiskResult(), None, client, cfg
    )

    assert decision.signal is original          # untouched
    assert source == "llm"
    assert any(w.startswith("explanation_mentions_other_signal") for w in warnings)


def test_contradiction_check(cfg):
    assert check_contradiction("A clear buy here.", Signal.BUY) is None
    assert check_contradiction("You should sell this.", Signal.BUY) is not None
    assert check_contradiction("No signal words at all.", Signal.WATCH) is None


def test_pipeline_degrades_to_deterministic_text_without_a_key(cfg):
    no_key = _no_key_cfg(cfg)
    dcf = value_ticker(make_fundamentals(), no_key)
    news = make_news(available=False)
    decision = decide("T", dcf, news, False, no_key)

    text, source, warnings = explain(
        "T", 100.0, decision, dcf, news, RiskResult(), None, FireworksClient(no_key), no_key
    )
    assert source == "fallback"
    assert decision.signal.value in text
    assert any(w.startswith("llm_unavailable") for w in warnings)


def test_fallback_text_names_the_gate_reason(cfg):
    dcf = value_ticker(make_fundamentals(fcf=[10e9, 9e9, -1e9, 8e9, 7e9]), cfg)
    news = make_news(available=False)
    decision = decide("T", dcf, news, False, cfg)
    text = _fallback_text("T", 100.0, decision, dcf, news, RiskResult())
    assert "negative_fcf" in text
