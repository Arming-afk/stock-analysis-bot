# Project: Daily Portfolio & Stock Analysis Bot

Personal-use, single-user system. Pulls portfolio data from Webull, analyzes
holdings + a watchlist daily, and produces BUY / SELL / HOLD / WATCH signals
with a confidence score. Delivered as a PWA (Add to Home Screen on iPhone)
plus a push notification.

Not investment advice. Personal decision-support tool only.

## Core design principle (non-negotiable)

**LLMs never decide or compute numbers. They only explain decisions that
deterministic code has already made.**

This applies everywhere in the system:
- DCF calculations — code only.
- The BUY/SELL/HOLD/WATCH decision — rule-based code, deterministic.
- The confidence score — code only, fixed weighted formula.
- LLMs are used for exactly two things: (1) classifying news into a
  sentiment category, (2) writing a human-readable explanation of a signal
  that code has already produced. An LLM must never be asked to output a
  final signal or a confidence number directly — that number becomes
  unfounded pseudo-precision (not reproducible run to run) and defeats the
  purpose of having it.

## Architecture (linear pipeline)

```
Daily trigger
  → Data ingestion (Webull: holdings, cash, watchlist)
  → DCF valuation (code)          + News fetch & sentiment (LLM)
  → Decision engine (rule-based code, NOT an LLM call)
  → Risk check (code)
  → Explanation layer (LLM, narrates the decision only)
  → Output (PWA dashboard + push notification)
```

## Components

### 1. Data ingestion
- Source: Webull OpenAPI (official, OAuth2 — never store raw password).
- Pulls: holdings (ticker, qty, cost basis, holding period/date), cash
  balance.
- Watchlist: static list, phase 0. User-managed manually.

### 2. DCF valuation — code, deterministic
- Standard DCF: project free cash flow, discount by WACC, terminal value.
- **DCF-applicability gate (required, runs first):** check trailing 3–5yr
  FCF. If negative or highly volatile beyond a set threshold → flag
  `dcf_applicable = false` and skip DCF confidence + BUY eligibility for
  that ticker entirely. Do not let a numerically "stable" sensitivity
  result on a fundamentally DCF-unsuited stock look confident.
- Output: `fair_value`, `valuation_gap_pct = (fair_value - price) / price`.

### 3. News sentiment — LLM (Fireworks: Llama 3.3 70B or Qwen 2.5 72B)
- Fetch news, 24–48h lookback window.
- If 0 relevant sources found → flag `news_available = false`. Do not call
  the LLM; do not silently default a score here (see confidence section).
- If sources found → LLM classifies: positive / neutral / negative.

### 4. Decision engine — code, rule-based matrix (not an LLM call)
Inputs: `valuation_gap` category × `sentiment` category × `dcf_applicable`
flag × `news_available` flag.

| Valuation gap | News sentiment | Signal |
|---|---|---|
| Undervalued (large) | Neutral / Positive | BUY |
| Undervalued (large) | Negative (strong) | WATCH |
| Overvalued + held | Neutral / Negative | SELL |
| Overvalued + held | Positive (momentum) | HOLD (flag risk) |
| Near fair value | any | HOLD / no action |
| `dcf_applicable = false` | any | WATCH only, never BUY/SELL |

### 5. Confidence score — phase 1, code only
```
confidence = 0.5 * dcf_confidence + 0.3 * news_confidence + 0.2 * agreement_score
```
- `dcf_confidence`: from sensitivity analysis (±1–2% growth/discount rate
  vs base case). Only computed when `dcf_applicable = true`.
- `news_confidence`: source count + cross-source sentiment agreement.
  **If `news_available = false` → baseline 50, explicit rule, not
  undefined/divide-by-zero.**
- `agreement_score`: rule-based check — does the sign of `valuation_gap`
  match the direction of `sentiment`?
- Bands: 80–100 High · 50–79 Medium · <50 Low. Low always forces the
  signal down to WATCH regardless of how attractive the valuation gap
  looks.

### 6. Risk check — code
- BUY signals: check portfolio concentration limits (max % per ticker,
  max % per sector). Breach → downgrade BUY to WATCH.
- SELL signals: attach cost basis + holding period (short/long-term) to
  the report. No automated tax logic — just surface the info so the tax
  impact can be weighed before acting.

### 7. Explanation layer — LLM (Fireworks: DeepSeek V3 or Kimi K2.7)
- Input: the final signal + confidence + all computed numbers (already
  decided by code above).
- Output: natural-language rationale only.
- Must never override, adjust, or re-derive the signal or confidence
  number it was given.

### 8. Output
- Daily report compiled and stored (DB / history, for later backtesting).
- Delivered via PWA dashboard (installable through Safari → Add to Home
  Screen, iOS 16.4+ supports Web Push) and a push notification summary.

## Tech stack

- **AI inference:** Fireworks AI, OpenAI-compatible endpoint
  (`https://api.fireworks.ai/inference/v1`).
  - High-volume / cheap: Llama 3.3 70B or Qwen 2.5 72B — news sentiment.
  - Reasoning / explanation: DeepSeek V3 or Kimi K2.7 — explanation layer.
- **Brokerage data:** Webull OpenAPI (OAuth2).
- **Frontend:** PWA, installable on iPhone via Add to Home Screen. No
  Apple Developer account or Xcode required.
- **Backend:** scheduled job (cron / cloud function) does the daily run.
  iOS cannot reliably run this on-device — the phone is a display/
  notification client only, never the scheduler.

## Hard constraints — do not violate

- Never let an LLM call produce the final BUY/SELL/HOLD/WATCH decision or
  the confidence number directly.
- Never compute `dcf_confidence` for a ticker that failed the
  DCF-applicability gate.
- Never silently default `news_confidence` — the no-news case must be an
  explicit flagged path with its own baseline.
- Never trust a naive backtest of the news-sentiment leg as ground truth:
  fetching "news from date X" today is subject to hindsight bias (articles
  get updated/retracted, search indices don't preserve what was known that
  day). Backtest the DCF leg normally (financials are point-in-time).
  Validate the news leg by manual spot-check instead, at least initially.
- This is a personal tool, not investment advice, and not for
  distribution to other users.

## Phased rollout

- **Phase 0:** DCF (with applicability gate) + rule-based decision matrix
  + risk check + explanation layer. No confidence score yet. Ship and
  observe real output for 1–2 weeks before adding more.
- **Phase 1:** Add the confidence score (all three sub-scores, computed in
  code as specified above).
- **Phase 2:** Expand the buy-side universe beyond the static watchlist
  (e.g. lightweight pre-screen from S&P 500 on valuation ratios), and
  enrich the PWA dashboard (historical charts, per-ticker drill-down).
