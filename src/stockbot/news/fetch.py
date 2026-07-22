"""News retrieval for the sentiment leg. No LLM here — this only gathers text.

Providers are pluggable because news sourcing is the least stable part of the
system. The default (Google News RSS) needs no API key.
"""

from __future__ import annotations

import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import requests

from ..config import Config
from ..logging_setup import get_logger
from ..models import Article

log = get_logger("news.fetch")

_UA = "Mozilla/5.0 (compatible; stockbot/0.1; personal use)"


def _parse_rfc822(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _within_window(dt: datetime | None, cutoff: datetime) -> bool:
    # An article with no usable timestamp is kept — dropping it would quietly
    # shrink the source count that news_confidence is built from.
    return dt is None or dt >= cutoff


def _google_news_rss(ticker: str, company: str | None, cutoff: datetime, limit: int) -> list[Article]:
    query = f'"{company or ticker}" OR {ticker} stock'
    url = (
        "https://news.google.com/rss/search?q="
        + urllib.parse.quote(query)
        + "&hl=en-US&gl=US&ceid=US:en"
    )
    resp = requests.get(url, headers={"User-Agent": _UA}, timeout=20)
    resp.raise_for_status()

    root = ET.fromstring(resp.content)
    articles: list[Article] = []
    for item in root.iterfind(".//item"):
        title = (item.findtext("title") or "").strip()
        if not title:
            continue
        published = _parse_rfc822(item.findtext("pubDate"))
        if not _within_window(published, cutoff):
            continue
        source_el = item.find("source")
        source = (source_el.text or "").strip() if source_el is not None else "Google News"
        articles.append(
            Article(
                title=title,
                source=source or "Google News",
                url=(item.findtext("link") or "").strip(),
                published_at=published,
                summary="",
            )
        )
        if len(articles) >= limit:
            break
    return articles


def _yfinance_news(ticker: str, cutoff: datetime, limit: int) -> list[Article]:
    try:
        import yfinance as yf
    except ImportError:
        return []

    try:
        raw = yf.Ticker(ticker).news or []
    except Exception as exc:  # yfinance raises a wide variety of transport errors
        log.debug("%s: yfinance news unavailable (%s)", ticker, exc)
        return []

    articles: list[Article] = []
    for item in raw:
        content = item.get("content") or item
        title = (content.get("title") or "").strip()
        if not title:
            continue

        published = None
        if "providerPublishTime" in item:
            published = datetime.fromtimestamp(item["providerPublishTime"], tz=timezone.utc)
        elif content.get("pubDate"):
            try:
                published = datetime.fromisoformat(str(content["pubDate"]).replace("Z", "+00:00"))
            except ValueError:
                published = None
        if not _within_window(published, cutoff):
            continue

        provider = content.get("provider") or {}
        source = provider.get("displayName") if isinstance(provider, dict) else None
        articles.append(
            Article(
                title=title,
                source=source or item.get("publisher") or "Yahoo Finance",
                url=(content.get("canonicalUrl") or {}).get("url", "") if isinstance(content.get("canonicalUrl"), dict) else item.get("link", ""),
                published_at=published,
                summary=(content.get("summary") or "")[:500],
            )
        )
        if len(articles) >= limit:
            break
    return articles


def _newsapi(ticker: str, company: str | None, cutoff: datetime, limit: int, api_key: str) -> list[Article]:
    resp = requests.get(
        "https://newsapi.org/v2/everything",
        params={
            "q": company or ticker,
            "from": cutoff.strftime("%Y-%m-%dT%H:%M:%S"),
            "sortBy": "publishedAt",
            "language": "en",
            "pageSize": limit,
            "apiKey": api_key,
        },
        headers={"User-Agent": _UA},
        timeout=20,
    )
    resp.raise_for_status()
    articles: list[Article] = []
    for item in resp.json().get("articles", [])[:limit]:
        title = (item.get("title") or "").strip()
        if not title:
            continue
        published = None
        if item.get("publishedAt"):
            try:
                published = datetime.fromisoformat(item["publishedAt"].replace("Z", "+00:00"))
            except ValueError:
                published = None
        articles.append(
            Article(
                title=title,
                source=(item.get("source") or {}).get("name") or "NewsAPI",
                url=item.get("url", ""),
                published_at=published,
                summary=(item.get("description") or "")[:500],
            )
        )
    return articles


def _dedupe(articles: list[Article]) -> list[Article]:
    seen: set[str] = set()
    out: list[Article] = []
    for a in articles:
        key = a.title.lower().strip()
        if key in seen:
            continue
        seen.add(key)
        out.append(a)
    return out


def fetch_articles(ticker: str, cfg: Config, company: str | None = None) -> list[Article]:
    """Gather recent articles for `ticker` inside the configured lookback window."""
    lookback = int(cfg.get("news.lookback_hours", 48))
    limit = int(cfg.get("news.max_articles", 12))
    provider = str(cfg.get("news.provider", "rss")).lower()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback)

    if provider == "none":
        return []

    collected: list[Article] = []

    if provider == "newsapi" and cfg.secrets.newsapi_key:
        try:
            collected += _newsapi(ticker, company, cutoff, limit, cfg.secrets.newsapi_key)
        except Exception as exc:
            log.warning("%s: newsapi fetch failed (%s)", ticker, exc)

    if provider in ("rss", "newsapi"):
        try:
            collected += _google_news_rss(ticker, company, cutoff, limit)
        except Exception as exc:
            log.warning("%s: rss fetch failed (%s)", ticker, exc)

    try:
        collected += _yfinance_news(ticker, cutoff, limit)
    except Exception as exc:
        log.debug("%s: yfinance news leg failed (%s)", ticker, exc)

    articles = _dedupe(collected)[:limit]
    log.info("%s: %d article(s) in the last %dh", ticker, len(articles), lookback)
    return articles
