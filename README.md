# Daily Portfolio & Stock Analysis Bot

Personal-use, single-user system. Pulls portfolio data from Webull, analyzes
holdings plus a watchlist daily, and produces BUY / SELL / HOLD / WATCH signals.
Delivered as an installable PWA plus a push notification.

**Not investment advice. Personal decision-support tool only.**

Built to the spec in [Daily.md](Daily.md).

---

## The design principle, and how it is enforced

> LLMs never decide or compute numbers. They only explain decisions that
> deterministic code has already made.

This is enforced structurally, not by convention:

| Stage | Who decides | Enforcement |
|---|---|---|
| DCF valuation | code | [`valuation/dcf.py`](src/stockbot/valuation/dcf.py) — pure arithmetic, no network, no model |
| News sentiment | LLM **classifies only** | return value is parsed into a 3-value enum; anything else is dropped, never coerced |
| BUY/SELL/HOLD/WATCH | code | [`decision/engine.py`](src/stockbot/decision/engine.py) — the only place a `Signal` is constructed from inputs |
| Confidence score | code | [`decision/confidence.py`](src/stockbot/decision/confidence.py) — fixed weighted formula |
| Risk check | code | [`decision/risk.py`](src/stockbot/decision/risk.py) |
| Explanation | LLM **narrates only** | runs last, returns a string that lands in `TickerReport.rationale`, which nothing downstream reads |

The explanation layer also gets a contradiction check: if the model writes
"this should be a SELL" over a BUY, that is flagged in the report. It cannot
change the signal — the signal was fixed before the model was called.

The whole pipeline runs with **no API key at all**. Without one, the sentiment
leg reports `news_available = false` and explanations fall back to deterministic
text. Signals are identical either way. That is the clearest proof that no
signal depends on a model.

---

## Quick start

```bash
pip install -r requirements.txt
cp .env.example .env                        # fill in keys (all optional to start)
cp data/portfolio.example.json data/portfolio.json
cp data/fixtures/market_data.example.json data/fixtures/market_data.json

python tools/make_icons.py                  # generate PWA icons
python run_daily.py --offline --no-news     # full run on fixture data, no network
```

Then the dashboard:

```bash
python -m uvicorn api.server:app --port 8000
# open http://localhost:8000
```

### CLI

```bash
python run_daily.py                    # full live run
python run_daily.py --offline          # fixture market data, no network
python run_daily.py --only MSFT,KO     # subset
python run_daily.py --no-news          # skip the sentiment leg entirely
python run_daily.py --dry-run          # no push notification
python run_daily.py --local-portfolio  # force data/portfolio.json over Webull
```

### Tests

```bash
python -m pytest tests -q     # 62 tests, no network required
```

---

## Pipeline

```
Daily trigger
  → Data ingestion (Webull: holdings, cash · config: watchlist)
  → DCF valuation (code)          + News fetch & sentiment (LLM classify)
  → Decision engine (rule-based code)
  → Confidence score (code, phase 1)
  → Risk check (code)
  → Explanation layer (LLM narrates)
  → Output (SQLite + JSON/Markdown + PWA + push)
```

One linear pass in [`pipeline.py`](src/stockbot/pipeline.py). Ordering is
load-bearing: the DCF applicability gate runs before any DCF math, and the
explanation runs after every number is final.

---

## The DCF applicability gate

Runs **first**. When it fails, no DCF math executes at all — `fair_value` stays
`None` all the way through, which is what makes it impossible for the decision
engine to hand that ticker a BUY, or for the confidence layer to score it.

A ticker is gated out when any of these hold (thresholds in `config.yaml`):

- fewer than 3 years of trailing FCF
- any negative FCF year in the 5-year window
- most recent FCF not positive
- trailing mean FCF not positive
- FCF coefficient of variation above 0.60 — "positive but a DCF cannot describe it"

Gated-out tickers are **WATCH only, never BUY or SELL**, regardless of sentiment.

This is the guard against false precision: a numerically tight sensitivity
result on a fundamentally DCF-unsuited stock would otherwise look confident.

---

## Decision matrix

| Valuation gap | News sentiment | Signal |
|---|---|---|
| Undervalued (large, ≥ +25%) | Neutral / Positive | BUY |
| Undervalued (large) | Negative (strong) | WATCH |
| Overvalued (≤ −10%) + held | Neutral / Negative | SELL |
| Overvalued + held | Positive (momentum) | HOLD (risk flagged) |
| Near fair value (−10%…+10%) | any | HOLD if held, else WATCH |
| `dcf_applicable = false` | any | WATCH only |

### Cases the spec left open

Three combinations are not in the spec's table. They are filled in
conservatively and tagged `spec_fill` in the report so the choice stays visible:

| Case | Chosen behaviour | Why |
|---|---|---|
| Large discount + **mild** negative news | WATCH | The table only names "Negative (strong)". Any negative reading defers the buy — the cheap price will still be there once the news resolves. |
| Overvalued + **not** held | WATCH | The table's overvalued row assumes a position. With none there is nothing to sell, and an expensive stock is not a buy. |
| **Mild** discount (+10%…+25%) | HOLD if held, else WATCH | Sits between "large" and "near fair value". Not enough margin of safety to open a position on. |

Change any of these in `decide()` if you disagree — each is a single branch.

---

## Confidence score (phase 1)

```
confidence = 0.5 * dcf_confidence + 0.3 * news_confidence + 0.2 * agreement_score
```

- **dcf_confidence** — from the ±1% / ±2% sensitivity grid (25 combinations):
  60% sign agreement across the grid, 40% tightness of the fair-value cluster.
  Never computed for a gated-out ticker.
- **news_confidence** — 40% source breadth, 60% cross-source consensus.
  When `news_available = false` it is the configured baseline (50), flagged as
  `news_baseline_applied`. Never a silent default, never a divide-by-zero.
- **agreement_score** — does the sign of the valuation gap match the direction
  of sentiment? 100 aligned, 60 neutral/near-fair, 20 contradictory.

Bands: 80–100 High · 50–79 Medium · <50 Low.
**Low always forces the signal down to WATCH**, however attractive the gap.

Phase 0 ships with this off (`phase: 0` in `config.yaml`). Flip to `phase: 1`
to enable it — the code and its tests are already in place.

---

## Risk check

- **BUY** → concentration limits (default 15% per ticker, 35% per sector, of
  holdings + cash). A breach downgrades the BUY to WATCH with the reason attached.
- **SELL** → attaches quantity, cost basis, unrealized P/L, days held, and
  short/long-term classification to the report.

There is deliberately **no tax logic**. The report surfaces the facts so the tax
impact can be weighed by hand before acting.

---

## Configuration

Every threshold lives in [`config.yaml`](config.yaml); every secret in `.env`.
Nothing in the decision path is hard-coded in Python.

| Secret | Needed for | Without it |
|---|---|---|
| `FIREWORKS_API_KEY` | sentiment + explanations | signals unchanged; sentiment leg off, deterministic prose |
| `WEBULL_APP_KEY` / `_SECRET` / `_ACCOUNT_ID` | live holdings | falls back to `data/portfolio.json` |
| `VAPID_PUBLIC_KEY` / `_PRIVATE_KEY` | push notifications | dashboard still works, no push |

Webull OpenAPI authenticates with an **app key/secret pair** generated in the
web console ([US](https://www.webull.com/center#openApiManagement) ·
[HK](https://www.webull.hk/open-api)). Your account password is never
requested, transmitted, or stored.

Generate VAPID keys once:

```bash
python tools/gen_vapid.py
```

### Models

Set in `config.yaml` under `llm:` — Fireworks model IDs use the
`accounts/fireworks/models/<name>` form and do drift, so they are config, not code.

- sentiment (high volume, cheap): `llama-v3p3-70b-instruct` or `qwen2p5-72b-instruct`
- explanation (reasoning): `deepseek-v3` or `kimi-k2-instruct`

---

## Data sources

| Data | Source | Note |
|---|---|---|
| Holdings, cash | Webull OpenAPI | falls back to `data/portfolio.json` |
| Prices, fundamentals, FCF history | yfinance | Webull's account API does not expose multi-year cash flow statements |
| News | Google News RSS (default), NewsAPI, yfinance | no key needed for the default |

`MarketDataProvider` is a Protocol — swap in a paid feed without touching the
valuation code.

**Two things worth knowing before you trust a live run:**

1. **The Webull calls are unverified against a live account.** The SDK's account
   method names have shifted between releases, so
   [`WebullPortfolio`](src/stockbot/ingestion/portfolio.py) probes several known
   spellings and, if none resolve, raises an error listing what the SDK object
   actually exposes. Expect to adjust the field mapping on first connection.
   Everything downstream is unaffected — it consumes a normalized `Portfolio`.
2. **The offline fixture data is synthetic.** Cash-flow figures are rounded and
   the prices were chosen to exercise every branch of the matrix. They are not
   real quotes.

---

## PWA on iPhone

1. Serve over HTTPS (a reverse proxy with a certificate; localhost also works
   for testing).
2. Open in **Safari** → Share → **Add to Home Screen**.
3. Launch from the home screen icon → tap **Enable alerts**.

iOS 16.4+ supports Web Push, but **only** from a home-screen-installed PWA — a
Safari tab will not receive notifications. No Apple Developer account or Xcode
required.

The dashboard caches the last report in `localStorage`, so it still renders
something useful with no signal.

---

## Scheduling

The phone is a display and notification client. It never runs the analysis.

**Windows Task Scheduler**

```powershell
schtasks /create /tn "StockBot Daily" /tr "python D:\Stock_Analysis_Bot\run_daily.py" /sc daily /st 17:30
```

**cron**

```
30 17 * * 1-5 cd /srv/stockbot && /usr/bin/python run_daily.py >> data/logs/cron.log 2>&1
```

Run after the US close so the day's prices are settled.

---

## Backtesting

Every run is stored whole in SQLite — DCF inputs, the gate verdict, the rule
that fired. Replaying stored inputs reproduces the stored fair value exactly.

- **DCF leg — backtest normally.** Financials are point-in-time.
- **News leg — do not trust a naive backtest.** Fetching "news from date X"
  today is subject to hindsight bias: articles get updated and retracted, and
  search indices do not preserve what was knowable that day. Validate this leg
  by manual spot-check instead, at least initially.

---

## Layout

```
run_daily.py              daily entrypoint (cron / Task Scheduler)
config.yaml               every threshold
api/server.py             FastAPI: report JSON + PWA hosting + push subscribe
web/                      PWA (index.html, app.js, styles.css, sw.js, manifest)
tools/make_icons.py       dependency-free PNG icon generator
src/stockbot/
  pipeline.py             the linear daily run
  models.py               dataclasses passed between stages
  config.py               YAML + .env loading
  ingestion/              portfolio.py (Webull + local), market_data.py (yfinance)
  valuation/dcf.py        applicability gate, DCF, sensitivity grid
  news/                   fetch.py (RSS/NewsAPI/yfinance), sentiment.py (LLM classify)
  decision/               engine.py (matrix), confidence.py, risk.py
  explain/explainer.py    LLM narration + contradiction check
  llm/fireworks.py        OpenAI-compatible client
  storage/db.py           SQLite history
  output/                 report.py (render), push.py (Web Push)
tests/                    62 tests, no network
```

---

## Phased rollout

- **Phase 0 — shipped.** DCF + gate, decision matrix, risk check, explanation
  layer, PWA, push. No confidence score. Run it for 1–2 weeks and read the real
  output before adding more.
- **Phase 1 — code present, off.** Set `phase: 1` in `config.yaml`.
- **Phase 2 — not built.** Pre-screen a wider buy-side universe from the S&P 500
  on valuation ratios; per-ticker drill-down and historical charts in the PWA.
  The API already exposes `/api/ticker/{ticker}/history` for the charts.
