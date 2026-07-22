"""News sentiment: the LLM's first and only classification job.

Contract with the model:
  in  -> one headline (+ optional summary)
  out -> exactly one of: positive | neutral | negative

Anything else is discarded, not coerced. The aggregate score, the strength
label and the agreement ratio are all computed here in code from the returned
categories — the model is never asked for a number.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from ..config import Config
from ..llm.fireworks import FireworksClient, LLMError
from ..logging_setup import get_logger
from ..models import Article, ArticleSentiment, NewsResult, Sentiment, Strength

log = get_logger("news.sentiment")

_SYSTEM = (
    "You are a financial news classifier. You classify the likely effect of a "
    "news item on the share price of the company in question.\n"
    "Answer with EXACTLY ONE word, lowercase, no punctuation, no explanation: "
    "positive, neutral, or negative.\n"
    "Use 'neutral' when the item is routine coverage, an opinion piece, a "
    "listicle, or not really about the company."
)

_VALID = {s.value: s for s in Sentiment}


def _classify_one(client: FireworksClient, model: str, ticker: str, article: Article, temperature: float) -> ArticleSentiment | None:
    body = f"Company ticker: {ticker}\nHeadline: {article.title}"
    if article.summary:
        body += f"\nSummary: {article.summary[:400]}"
    body += "\n\nClassification:"

    try:
        raw = client.chat(
            model=model,
            messages=[{"role": "system", "content": _SYSTEM}, {"role": "user", "content": body}],
            temperature=temperature,
            max_tokens=5,
        )
    except LLMError as exc:
        log.warning("%s: sentiment call failed for %r (%s)", ticker, article.title[:60], exc)
        return None

    label = _VALID.get(raw.strip().lower().strip(".").split()[0] if raw.strip() else "")
    if label is None:
        # Unparseable output is dropped. Coercing it to neutral would let a
        # broken model quietly shift the aggregate toward the middle.
        log.warning("%s: unparseable sentiment %r — dropping article", ticker, raw[:40])
        return None

    return ArticleSentiment(article=article, label=label)


def _aggregate(scored: list[ArticleSentiment], cfg: Config) -> tuple[float, Sentiment, Strength, float]:
    """Code-only aggregation of per-article categories."""
    weights = {Sentiment.POSITIVE: 1.0, Sentiment.NEUTRAL: 0.0, Sentiment.NEGATIVE: -1.0}
    score = sum(weights[s.label] for s in scored) / len(scored)

    deadband = float(cfg.get("news.aggregate_deadband", 0.15))
    if score > deadband:
        label = Sentiment.POSITIVE
    elif score < -deadband:
        label = Sentiment.NEGATIVE
    else:
        label = Sentiment.NEUTRAL

    strong = float(cfg.get("decision.strong_sentiment", 0.60))
    moderate = float(cfg.get("decision.moderate_sentiment", 0.30))
    magnitude = abs(score)
    if magnitude >= strong:
        strength = Strength.STRONG
    elif magnitude >= moderate:
        strength = Strength.MODERATE
    else:
        strength = Strength.WEAK

    counts: dict[Sentiment, int] = {}
    for s in scored:
        counts[s.label] = counts.get(s.label, 0) + 1
    agreement = max(counts.values()) / len(scored)

    return score, label, strength, agreement


def analyze_news(ticker: str, articles: list[Article], client: FireworksClient, cfg: Config) -> NewsResult:
    """Classify each article, then aggregate in code.

    When no articles are found the LLM is not called at all and the result is
    flagged `news_available=False`. Downstream, that flag drives an explicit
    baseline — it is never silently treated as if neutral news had been read.
    """
    min_articles = int(cfg.get("news.min_articles_for_available", 1))

    if len(articles) < min_articles:
        log.info("%s: news_available=False (%d article(s) found)", ticker, len(articles))
        return NewsResult(
            ticker=ticker,
            news_available=False,
            reason=f"no_sources: {len(articles)} article(s) in lookback window",
            source_count=len(articles),
            assumed_neutral=True,
            aggregate_label=Sentiment.NEUTRAL,
            aggregate_strength=Strength.WEAK,
        )

    if not client.available:
        log.warning("%s: no Fireworks key — news leg disabled", ticker)
        return NewsResult(
            ticker=ticker,
            news_available=False,
            reason="llm_unavailable: FIREWORKS_API_KEY not configured",
            source_count=len(articles),
            assumed_neutral=True,
            aggregate_label=Sentiment.NEUTRAL,
            aggregate_strength=Strength.WEAK,
        )

    model = str(cfg.get("llm.sentiment_model"))
    temperature = float(cfg.get("llm.sentiment_temperature", 0.0))

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(
            pool.map(lambda a: _classify_one(client, model, ticker, a, temperature), articles)
        )
    scored = [r for r in results if r is not None]

    if not scored:
        return NewsResult(
            ticker=ticker,
            news_available=False,
            reason="classification_failed: no article could be classified",
            source_count=len(articles),
            assumed_neutral=True,
            aggregate_label=Sentiment.NEUTRAL,
            aggregate_strength=Strength.WEAK,
        )

    score, label, strength, agreement = _aggregate(scored, cfg)
    distinct = len({s.article.source.lower() for s in scored})

    log.info(
        "%s: sentiment=%s (%s, score=%+.2f) from %d article(s) / %d source(s)",
        ticker, label.value, strength.value, score, len(scored), distinct,
    )

    return NewsResult(
        ticker=ticker,
        news_available=True,
        reason="ok",
        articles=scored,
        source_count=len(scored),
        distinct_sources=distinct,
        aggregate_score=score,
        aggregate_label=label,
        aggregate_strength=strength,
        agreement_ratio=agreement,
        assumed_neutral=False,
    )
