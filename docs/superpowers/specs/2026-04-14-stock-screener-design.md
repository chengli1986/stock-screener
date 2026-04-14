# Stock Screener Design Spec

> A-share + HK stock screening tool with multi-layer funnel, multi-factor scoring, LLM-assisted reports, and built-in validation framework.

**Date**: 2026-04-14
**Repo**: `chengli1986/stock-screener` (new, independent)
**Status**: Design approved, pending implementation

---

## Section 1: System Architecture

### Core Concept

Layered funnel + dual trigger mode. Each layer reduces the candidate set while increasing analysis depth and cost.

```
[Universe]  ~800 stocks (A-share + HK, index constituents)
     |
     v  Layer 1: Rule filter (pure quantitative, zero LLM)
[Candidate pool]  ~200-400
     |
     v  Layer 2: Multi-factor scoring (fundamentals + technicals + news sentiment)
[Shortlist]  ~20 (A-share 15 + HK 5)
     |
     v  Layer 3: LLM research report (expensive model, small batch)
[Recommendations]  ~20 with reports
     |
     v  Output: email report + JSON archive + SQLite tracking
```

### Trigger Modes

| Mode | Trigger | Frequency | Weight Profile |
|------|---------|-----------|----------------|
| Weekly | cron (weekend) | 1x/week | `default_weekly` |
| Event-driven | Machine-determined conditions (see Section 4) | Variable | `event_driven` |

Both modes share Layer 2-3 pipeline. Layer 1 uses mode-specific gate sets (see Section 2).

### Project Structure

```
~/stock-screener/
├── config/
│   ├── universe.json          # Stock pool definitions (index constituents)
│   ├── factors.json           # Factor weights, thresholds, scoring curves
│   └── screener.json          # Screening params, email config, LLM config
├── data/
│   ├── fetch.py               # Lean data layer (quotes, financials, klines)
│   ├── news.py                # News fetching + LLM sentiment scoring
│   └── cache.py               # Data cache (avoid redundant API calls)
├── screener/
│   ├── layer1_filter.py       # Rule-based coarse filter
│   ├── layer2_scorer.py       # Multi-factor scoring engine
│   ├── layer3_report.py       # LLM report generation
│   └── pipeline.py            # Funnel orchestration
├── backtest/
│   ├── engine.py              # Historical backtest engine
│   └── tracker.py             # Forward tracking + outcome recording
├── output/
│   ├── email.py               # HTML email generation
│   └── archive.py             # Result archival (JSON + SQLite)
├── tests/
├── scripts/
│   ├── weekly-screen.sh       # Weekly cron entry point
│   └── event-screen.sh        # Event trigger entry point
└── README.md
```

### Tech Stack

- Python 3.12, `~/stock-env/` venv
- Data: East Money push2 API + akshare + Longbridge CLI (HK) + Tencent (fallback)
- Technical analysis: pandas-ta (pure Python, no C dependency) or TA-Lib if already installed
- LLM: GPT-4.1-mini (news sentiment, batch, cheap) + Gemini 2.5 Pro / Claude (reports, small batch)
- Storage: JSONL archive + SQLite (backtest/tracking)
- Email: SMTP via `~/.stock-monitor.env`
- Scheduling: cron-wrapper.sh

### Data Layer Strategy

Independent from stock-monitor. Copy and slim down the relevant data-fetching functions rather than symlink or pip install -e. The data layer in this repo only contains what screening needs:

- Quote fetching (Longbridge primary, Tencent fallback)
- Kline fetching (60-day OHLC)
- Fundamental data (East Money push2)
- News fetching (RSS + keyword matching)

If both repos mature, consider extracting a shared data package later.

---

## Section 2: Universe & Quantitative Definitions

### Universe Definition

**A-shares:**
- Include: CSI 300 + CSI 500 constituents (~800 stocks, covering main board + ChiNext core liquidity)
- Source: akshare `index_stock_cons()`, refreshed on the 1st of each month via cron
- Exclusions:
  - ST / *ST / delisting transition stocks (name contains ST marker)
  - Suspended > 5 trading days (last trade date > 5 days ago)
  - 20-day average daily turnover < CNY 50M
  - Listed < 60 trading days (new IPO data instability)
- Dedup: stocks appearing in both CSI 300 and CSI 500 are kept once

**HK stocks:**
- Include: Hang Seng Index + Hang Seng China Enterprises Index constituents (~100 stocks)
- Source: Longbridge API or akshare HK index constituents
- Exclusions:
  - 20-day average daily turnover < HKD 20M
  - Suspended > 5 trading days
- Dedup: stocks appearing in both indices are kept once

**Expected scale:** ~700-800 stocks after exclusions enter Layer 1.

**Constituent update:** Monthly cron on the 1st, diff logged. For backtesting, historical constituent snapshots are stored monthly to prevent survivorship bias (see Section 5).

### Layer 1: Coarse Filter (Hard Gates)

Purpose: reduce ~800 to ~200-400. Pure rules, zero LLM calls. Only tradability, liquidity, and extreme risk exclusion.

Layer 1 has two gate sets: one for weekly mode and one for event-driven mode. This is necessary because weekly gates (MA20 trend, price > MA20) would filter out oversold rebound candidates that are the entire point of event-driven screening.

**Weekly mode gates (AND logic, all must pass):**

| Condition | A-share Threshold | HK Threshold | Source |
|-----------|-------------------|--------------|--------|
| MA20 direction | MA20 slope > 0 (MA20 rising over last 5 days) | Same | Kline calc |
| Price above MA20 | Close > MA20 | Same | Kline calc |
| 60-day volatility cap | Annualized vol < 80% (exclude extreme movers) | < 100% | Kline calc |
| Volume not collapsing | 5-day avg volume > 20-day avg volume x 0.6 | Same | Kline calc |

**Event-driven mode gates (relaxed trend requirements):**

| Condition | A-share Threshold | HK Threshold | Source |
|-----------|-------------------|--------------|--------|
| 60-day volatility cap | Annualized vol < 100% (wider tolerance) | < 120% | Kline calc |
| Volume not collapsing | 5-day avg volume > 20-day avg volume x 0.4 | Same | Kline calc |
| Not in freefall | Close > MA60 (still above long-term trend) | Same | Kline calc |
| Minimum liquidity | 20-day avg daily turnover > CNY 30M / HKD 15M | Same | Quote API |

Event mode drops MA20 slope and price-above-MA20 gates, replacing them with a looser MA60 floor. This allows stocks that have pulled back sharply but retain long-term structural support.

All thresholds are configurable in `config/factors.json`, not hardcoded.

**Explicitly NOT hard gates in either mode (moved to Layer 2 scoring):**
- RSI range — strong trend stocks often have persistently high RSI
- PE(TTM) > 0 — growth stocks, cyclicals, and some HK companies distort this metric
- Earnings growth — too noisy for binary filtering

### Baseline Definition

| Market | Benchmark Index | Usage |
|--------|----------------|-------|
| A-share | CSI 300 (000300.SH) | Primary: alpha = portfolio return - CSI 300 return |
| HK | Hang Seng Index (HSI) | Primary |
| Supplement | Equal-weight universe return | Verify screening beats "random pick" |
| Supplement | Simple heuristic baselines (see Section 5) | Verify multi-factor beats single-factor |

**Alpha calculation:** `alpha = portfolio_return(N days) - benchmark_return(N days)`

Portfolio construction: equal-weight all recommended stocks. No cap-weighting in MVP.

### Evaluation Windows & Labels

**Primary window: 10 trading days (~2 weeks)**

Rationale: weekly screening naturally maps to next-two-weeks performance. 5 days too noisy, 20 days too long for signal decay.

**Auxiliary windows:** 5-day and 20-day returns also recorded for signal decay analysis, but primary scoring uses 10-day only.

**Label definitions:**

| Label | Condition |
|-------|-----------|
| WIN | 10-day excess return > +1% |
| DRAW | 10-day excess return in [-1%, +1%] |
| LOSE | 10-day excess return < -1% |

The +/-1% threshold is an initial hyperparameter (configurable in `screener.json`), not a proven constant. It exists to filter out transaction cost and slippage noise.

**MVP evaluation metrics (primary):**
- Win rate (WIN / total)
- Average excess return (mean alpha across all recommendations)
- Max drawdown (peak-to-trough decline on the cumulative equity curve, NOT worst single-period return)
- Information ratio (mean alpha / std alpha)

Statistical significance testing (t-test etc.) is deferred until 30+ periods accumulate. Before that, only descriptive metrics.

---

## Section 3: Layer 2 Multi-Factor Scoring

### Overview

~200-400 stocks from Layer 1 are scored across 3 dimensions. Each stock gets a composite score 0-100 and is ranked within its market pool (A-share and HK ranked separately, never mixed).

**Scoring flow:**
```
Raw value → Standardize (0-100) → Dimension weighting → Composite score → Rank → Top N to Layer 3
```

### Standardization Method

Two methods depending on factor type:

| Factor Type | Method | Example |
|-------------|--------|---------|
| Monotonic (higher/lower is always better) | Percentile rank within current candidate pool | ROE, revenue growth, 10-day return |
| Optimal-range (sweet spot exists) | Rule-based mapping function | RSI, Bollinger position, news heat |

This distinction is critical for implementation: do not apply percentile rank to optimal-range factors.

### Missing Data Handling

- Missing factor value = `None`, not a default score
- Dimension score is recalculated using only available factors with re-normalized weights
- If available factors within a dimension < 50%: dimension is flagged **low confidence**, its weight is halved in composite
- Each stock carries a confidence label (high / medium / low) based on missing factor count
- Low-confidence stocks are not hard-excluded but rank lower naturally

### Dimension 1: Fundamentals (4 factors)

| Factor | Calculation | Source | Direction | A/HK |
|--------|-------------|--------|-----------|------|
| ROE (TTM) | Net income TTM / avg equity | East Money push2 | Higher = better | Shared |
| Revenue growth | (Current - YoY) / YoY | East Money push2 | Higher = better | Shared |
| Net profit growth | Same logic | East Money push2 | Higher = better | Shared |
| Net margin (TTM) | Net income / revenue | East Money push2 | Higher = better | Shared |

**Intra-dimension weighting:** Equal weight (25% each).

**Design decisions:**
- PE/PB excluded from core factors due to cross-sector incomparability. Revisit after sector neutralization is implemented.
- PEG excluded from MVP due to instability when profit growth is small/negative. Reserve factor, enable only when PE > 0 AND net profit growth > 10%.

### Dimension 2: Technicals + Momentum (8 factors)

Combines technical indicators and momentum signals into a single "market action" dimension to avoid double-counting trend signals.

**Technical sub-group (5 factors):**

| Factor | Calculation | Type | Direction/Curve |
|--------|-------------|------|-----------------|
| MA alignment | Count of MA5>MA10>MA20>MA60 layers met (0-3) | Monotonic | Higher = better |
| MACD position | DIF - DEA value | Monotonic | Percentile rank |
| RSI(14) | Standard RSI | Optimal-range | Bell curve centered at 50 |
| Bollinger position | (Close - LowerBand) / (UpperBand - LowerBand) | Optimal-range | 0.3-0.7 optimal, extremes decay |
| Volume-price sync | 5d avg vol / 20d avg vol, direction-adjusted | Monotonic | Volume up + price up = high score |

**RSI scoring curve:**
```python
def rsi_score(rsi: float) -> float:
    if 40 <= rsi <= 60: return 100
    elif 30 <= rsi < 40 or 60 < rsi <= 70: return 70
    elif 25 <= rsi < 30 or 70 < rsi <= 75: return 40
    else: return 10  # Extreme zones: low score but not excluded
```

**Bollinger scoring curve:**
```python
def bollinger_score(pct: float) -> float:
    if 0.3 <= pct <= 0.7: return 100
    elif 0.15 <= pct < 0.3 or 0.7 < pct <= 0.85: return 60
    elif 0.0 <= pct < 0.15 or 0.85 < pct <= 1.0: return 30
    else: return 10  # Outside bands
```

**Momentum sub-group (3 factors):**

| Factor | Calculation | Type | Direction |
|--------|-------------|------|-----------|
| 10-day return | (Close - Close_10d_ago) / Close_10d_ago | Monotonic | Percentile rank |
| 20-day return | Same logic, 20 days | Monotonic | Percentile rank |
| Relative strength | Stock 10d return - benchmark 10d return | Monotonic | Percentile rank |

**Intra-dimension weighting:**
- Technical sub-group 60%: MA(15%) + MACD(15%) + RSI(12%) + Bollinger(9%) + Volume-price(9%)
- Momentum sub-group 40%: 10d return(16%) + 20d return(12%) + Relative strength(12%)

**Reserve factors (not in MVP, enable after validation):**
- KDJ: Useful but overlaps significantly with RSI
- ADX: Trend strength indicator, consider adding if trend-following bias needs strengthening

### Dimension 3: News Sentiment (2 factors)

| Factor | Calculation | Source | Type |
|--------|-------------|--------|------|
| News heat | Count of mentions in last 7 days | RSS + keyword matching | Optimal-range |
| Sentiment score | LLM judges each headline (-1/0/+1), take mean | GPT-4.1-mini | Monotonic |

**News matching strategy:**

Whitelist-based matching to control noise:
- **High confidence (direct scoring):** Stock code variants + official short name
- **Low confidence (candidate only, not scored):** Broad keywords, subsidiary names, sector terms

A-share and HK stocks have many name collisions (abbreviations, group vs subsidiary). Low-confidence matches are recorded for context but do not contribute to the sentiment score.

**News quality controls:**
- **Duplicate headline collapsing:** Before counting mentions or scoring sentiment, deduplicate headlines by Jaccard similarity > 0.7 (same approach as global-news). Multiple outlets running the same wire story count as 1 mention, not N.
- **Company-specific vs macro/sector:** LLM sentiment prompt includes instruction to return `"scope": "company" | "sector" | "macro"`. Only `company`-scoped headlines contribute to the stock's sentiment score. `sector` and `macro` headlines are logged for context in the LLM report but do not inflate the stock-level sentiment factor.
- **Source quality weighting:** Not in MVP (all sources equal weight). Reserve for post-MVP if large-cap coverage bias proves problematic.

**News sources:**
- A-share: CLS / Jin10 / Yicai (via rsshub.rssforever.com)
- HK: MarketWatch / CNBC / East Money HK channel

**LLM sentiment prompt (GPT-4.1-mini, batch):**

```
Given these news headlines about {stock_name}, judge each headline's impact and scope:
sentiment: -1 = clearly negative, 0 = neutral/irrelevant, 1 = clearly positive
scope: "company" = specifically about this company, "sector" = about its industry/sector, "macro" = macro/policy news

Headlines:
{titles}

Return JSON: [{"title": "...", "sentiment": -1|0|1, "scope": "company"|"sector"|"macro"}]
```

Batch 10-20 headlines per call. ~200-400 stocks x ~3-5 headlines avg = ~1000 headlines, ~50-100 API calls. Estimated cost: $0.01-0.02 per screening run.

**News heat scoring curve:**
```python
def heat_score(mentions: int) -> float:
    if mentions == 0: return 30      # Insufficient info penalty
    elif mentions <= 2: return 50
    elif mentions <= 5: return 80
    elif mentions <= 8: return 100   # Healthy attention
    elif mentions <= 12: return 80   # Starting to overheat
    else: return 50                  # Likely already priced in
```

Curve parameters configurable in `factors.json`.

**Intra-dimension weighting:** Heat 40% + Sentiment 60%.

### Composite Score

| Dimension | Weekly Weight | Event-Driven Weight |
|-----------|-------------|---------------------|
| Fundamentals | 35% | 15% |
| Technicals + Momentum | 40% | 50% |
| News Sentiment | 25% | 35% |

Event-driven emphasizes technicals (oversold bounces) and news sentiment (panic/opportunity), because fundamentals don't change in a single-day crash.

**Weight management:** MVP defines exactly 2 weight profiles (`default_weekly`, `event_driven`) in `factors.json`. No automated parameter search. Backtest compares these 2 profiles only. Adding new profiles requires explicit justification and design review.

**Output:** Composite scores ranked descending within each market. Top 15 A-share + top 5 HK enter Layer 3. Counts configurable in `screener.json`.

---

## Section 4: Layer 3, Output & Validation

### Layer 3: LLM Research Reports

**Input:** Top 20 from Layer 2 (A-share 15 + HK 5).

**Role:** LLM does NOT participate in scoring or ranking. Its job is to explain why a stock scored high, and surface risks that quantitative factors cannot cover (policy risk, insider selling, lock-up expiry, etc.).

**Model:** Gemini 2.5 Pro (primary) -> GPT-4.1 (fallback) -> Claude Sonnet (fallback). Same chain as stock-monitor's proven fallback pattern.

**Per-stock input data package:**

```
Stock: {name} ({code})
Market: A-share / HK

[Fundamentals]
ROE: {roe}%, Revenue growth: {rev_growth}%, Net profit growth: {profit_growth}%
Net margin: {net_margin}%, Score: {fundamental_score}/100

[Technicals + Momentum]
MA alignment: {ma_alignment}, MACD: {macd_status}
RSI(14): {rsi}, Bollinger position: {boll_pct}%
10d return: {ret_10d}%, Relative strength: {rel_strength}%
Score: {technical_score}/100

[News]
Mentions (7d): {mention_count}, Sentiment avg: {sentiment_avg}
Top headlines:
{top_3_news_titles}
Score: {news_score}/100

[Price Levels (rule-calculated)]
Support: {20d_low}, {ma60}
Resistance: {60d_high}, {ma20 if above}

[Composite] {total_score}/100, Rank: {rank}/{total}
```

**Prompt:**

```
You are a professional equity analyst. Based on the data above, write a brief analysis (under 200 Chinese characters):

1. Core thesis (one sentence: why this stock deserves attention)
2. Key risks (risks NOT covered by quantitative metrics: policy, insider selling, lock-up, sector headwinds)
3. Price level interpretation (explain the rule-calculated support/resistance levels, do NOT invent new ones)
4. Confidence (high/medium/low) + one-sentence justification

Return JSON format.
```

**Cost:** 20 stocks x ~1000 tokens input + ~500 tokens output = ~30K tokens/run. Gemini 2.5 Pro: ~$0.01-0.02/run. Monthly (weekly runs): < $0.10.

### Event-Driven Triggers

**Global triggers (decide whether to run event screening):**

| Trigger | Condition | Detection | Source |
|---------|-----------|-----------|--------|
| Index crash | CSI 300 or HSI daily drop > 3% | Daily post-close | Quote API |
| Volume surge | Index turnover > 20d avg x 1.5 | Daily post-close | Quote API |
| VIX spike | VHSI > 30 | Daily post-close | Quote API |
| Earnings dense week | Number of index constituents with earnings release in past 5 trading days > 30 | Earnings calendar API or config | `screener.json` |

Any single condition triggers event screening. Note: earnings season is defined by actual disclosure density, not calendar month. A whole-month trigger (Apr/Aug/Oct) would fire too frequently and degrade event mode into a noisy alternate weekly scan.

**Stock/sector triggers (bonus signals within Layer 2 scoring):**

| Trigger | Condition | Effect |
|---------|-----------|--------|
| Individual volume spike | Stock 1d volume > 20d avg x 2.0 | +10 bonus to technicals score |
| 60-day high breakout | Close > max(Close, 60d) | +8 bonus to technicals score |
| Earnings pre-announcement | Stock has filed earnings preview in last 7 days | +5 bonus to news score |
| High news frequency | News mentions > 10 in 3 days | Flag for LLM review (no auto-bonus, may be negative) |

Stock/sector triggers only activate during event-driven runs, not weekly runs. Bonus points are capped and configurable in `factors.json`.

### Email Report

HTML email with embedded base64 PNG charts (matplotlib, same approach as stock-monitor).

**Structure:**

```
Weekly Stock Screening Report — 2026 Week XX (YYYY-MM-DD)
Trigger: Weekly / Event-driven (CSI 300 -3.2%)

--- Market Overview ---
CSI 300: 3,856 (+1.2%)  HSI: 18,234 (-0.8%)
Universe: A-share 756 / HK 89
Layer 1 passed: 312 → Layer 2 scored → Top 20

--- A-Share Recommendations (15) ---
#1 Kweichow Moutai (600519)  Score: 87/100  Confidence: High
   Fundamentals: 92  Tech+Momentum: 83  News: 85
   [60-day kline mini chart]
   Core thesis: ...
   Key risks: ...
   Support: 1680 (20d low) / 1650 (MA60)
   Resistance: 1780 (60d high)

--- HK Recommendations (5) ---
... (same format)

--- Tracking Review ---
Last week (W{XX-1}) results:
  Win rate: 11/20 (55%)  Avg alpha: +1.8%
  Best: XX (+8.2%)  Worst: XX (-3.1%)
Cumulative (N periods):
  Win rate: 58%  Avg alpha: +1.3%  Max drawdown: -4.2%
  Information ratio: 0.42
```

MIME-Version: 1.0 header. `html.escape()` on all external text.

### Result Archive Schema

**JSON archive:** `results/{run_id}.json`

**run_id format:** `{YYYY}-W{XX}-weekly` or `{YYYY-MM-DD}-event-{trigger_name}`

```json
{
  "meta": {
    "run_id": "2026-W16-weekly",
    "date": "2026-04-18",
    "trigger": "weekly",
    "weight_profile": "default_weekly",
    "universe": {"a_share": 756, "hk": 89},
    "layer1_passed": {"a_share": 245, "hk": 67},
    "version": "0.1.0"
  },
  "recommendations": [
    {
      "code": "600519",
      "name": "Kweichow Moutai",
      "market": "a_share",
      "rank": 1,
      "scores": {
        "total": 87,
        "fundamental": 92,
        "technical_momentum": 83,
        "news_sentiment": 85,
        "confidence": "high",
        "missing_factors": []
      },
      "factors": {
        "roe": 28.5,
        "rev_growth": 12.3,
        "profit_growth": 15.1,
        "net_margin": 52.1,
        "ma_alignment": 3,
        "macd_value": 2.3,
        "rsi": 55,
        "bollinger_pct": 0.62,
        "volume_price_sync": 1.3,
        "ret_10d": 2.3,
        "ret_20d": 4.1,
        "rel_strength": 1.8,
        "news_mentions": 4,
        "sentiment_avg": 0.6
      },
      "price_levels": {
        "support_20d_low": 1680,
        "support_ma60": 1650,
        "resistance_60d_high": 1780
      },
      "llm_report": {
        "core_thesis": "...",
        "risks": "...",
        "price_interpretation": "...",
        "confidence": "high",
        "model_used": "gemini-2.5-pro"
      },
      "entry_price": 1725.0,
      "entry_date": "2026-04-18"
    }
  ],
  "benchmark": {
    "csi300": {"level": 3856, "date": "2026-04-18"},
    "hsi": {"level": 18234, "date": "2026-04-18"}
  }
}
```

### SQLite Tracking Schema

**Database:** `data/tracking.db`

| Table | Key Fields | Purpose |
|-------|-----------|---------|
| `recommendations` | run_id, code, market, rank, total_score, entry_price, entry_date, factors_json, confidence | Record each recommendation |
| `outcomes` | run_id, code, ret_5d, ret_10d, ret_20d, benchmark_ret_5d, benchmark_ret_10d, benchmark_ret_20d, label_10d | Cron backfills 10 trading days later |
| `run_summary` | run_id, trigger, stock_count, win_rate, avg_alpha, max_drawdown, info_ratio | Per-run aggregate |

All tables keyed by `run_id` to distinguish weekly vs event-driven runs within the same week.

### Overlapping Run Deduplication

When the same stock appears in multiple runs whose 10-day evaluation windows overlap (e.g., weekly run on Monday + event-driven run on Wednesday of the same week), the following rules apply:

**For forward tracking evaluation:**
- Each run is evaluated independently (both get outcome records)
- But for aggregate statistics (win rate, avg alpha, info ratio), a stock-week pair is counted **only once**, using the earliest run's entry price as the reference
- The `run_summary` table includes a `deduped_count` field showing how many unique stock-week pairs were evaluated vs raw count

**For backtest:**
- Same dedup rule: if the same stock would be selected by both weekly and event-driven backtests in overlapping windows, count it once
- Backtest report explicitly shows raw vs deduped sample sizes

**Cooldown rule:** After an event-driven run, no new event-driven run for the same trigger type within 5 trading days. Multiple different triggers on the same day are consolidated into a single event run (combined run_id: `{YYYY-MM-DD}-event-combined`).

---

## Section 5: MVP Scope & Milestones

### MVP Includes

| Module | Scope |
|--------|-------|
| Universe | CSI 300 + CSI 500 + HSI + HSCEI, deduped, ~800 stocks |
| Layer 1 | Liquidity + trend + extreme risk hard gates |
| Layer 2 | 3-dimension scoring (fundamentals / technicals+momentum / news sentiment), percentile + rule-function hybrid |
| Layer 3 | LLM reports (top 20), rule-calculated price levels + LLM interpretation |
| Triggers | Weekly cron + event-driven (4 global + 4 stock-level conditions) |
| Output | HTML email + JSON archive + SQLite tracking |
| Validation | Backtest (pipeline verification) + forward tracking + random baseline + simple-rule baselines |
| Technical analysis | A+B level (MA/MACD/RSI/Bollinger/volume-price) |

### MVP Excludes

| Excluded | Reason | Revisit When |
|----------|--------|-------------|
| Web UI | Validate value before investing in frontend | Forward tracking 12 periods, win rate > 55% |
| US stocks | Phased, stabilize A+HK first | MVP validated |
| C-level technicals (candlestick patterns, Elliott waves, chip distribution) | Subjective, hard to validate | Clear signal value evidence |
| Automated parameter search | Overfitting risk | 30+ period samples, controlled experiment design |
| Auto-trading / order execution | Far beyond MVP | Not on roadmap |
| PE/PB as core factors | Cross-sector bias | Sector neutralization implemented |
| PEG | Unstable with low/negative growth | Validated on PE>0 AND growth>10% subset |
| KDJ | Significant overlap with RSI | Post-MVP, if RSI proves insufficient |
| ADX | Trend strength, additive but not essential | Post-MVP, if trend-following needs strengthening |

### Milestones

#### M0: Data Layer

**Goal:** Lean data fetching module for A-share + HK quotes, financials, klines, and news.

**Pass criteria (split by data type):**
- Quotes / Klines: coverage > 95% of universe
- Fundamentals (ROE, growth, margin): coverage > 85%
- News: coverage > 60% (news is naturally sparse; acceptable if explainable)

#### M1: Funnel Pipeline

**Goal:** Layer 1 + Layer 2 end-to-end, producing ranked top 20.

**Pass criteria (checklist):**
- Top 20 contains no suspended / ST / obviously illiquid stocks
- Both A-share and HK pools produce results with non-degenerate score distributions
- Key factors and ranking direction are consistent (spot-check: highest ROE stocks should score high on fundamentals dimension)
- Pipeline completes within 10 minutes wall clock

#### M2: Backtest Verification

**Goal:** Run pipeline against historical data, verify it's not absurd.

**Point-in-time data integrity (critical):**

Backtesting is only valid if each simulated screening run uses ONLY data that was available at the simulated date. Using future-known data (look-ahead bias) invalidates all results. The following rules are mandatory:

| Data Type | Point-in-Time Rule | Implementation |
|-----------|-------------------|----------------|
| **Fundamentals (ROE, growth, margin)** | Use only the most recent **published** financial report as of the simulated date. A-share quarterly reports have mandatory disclosure deadlines: Q1 by Apr 30, H1 by Aug 31, Q3 by Oct 31, Annual by Apr 30. Apply a **publication lag buffer of +45 days** from period end to be conservative (e.g., a screen on Jan 15 can only use data from Q3 ending Sep 30, not Q4). | Store historical fundamentals snapshots keyed by `(stock, report_period, disclosure_date)`. Backtest engine queries `WHERE disclosure_date <= simulated_date`. |
| **Klines / Quotes** | Use only data up to the simulated date. No peeking at future prices. | Standard: fetch klines with `end_date = simulated_date`. |
| **News headlines** | Use only headlines published before the simulated date. | Archive headlines with publication timestamps. Backtest queries `WHERE pub_date <= simulated_date`. LLM sentiment is NOT re-run in backtest — instead, use a simplified keyword-based proxy (positive/negative word lists) to avoid anachronistic LLM behavior. |
| **Universe constituents** | Use the constituent list that was in effect at the simulated date, not the current list. | Monthly constituent snapshots (see Universe Definition). |

**News in backtest — deliberate degradation:** LLM sentiment scoring is skipped in backtest mode because (a) LLM behavior is non-deterministic and may reflect training data from after the simulated date, and (b) historical headline archives may be incomplete. Instead, backtest uses a simple keyword-based sentiment proxy (+1/-1/0 from word lists). This means backtest results will understate the news dimension's contribution — which is acceptable as a conservative lower bound.

**Fundamentals snapshot collection:** The backtest engine requires a historical fundamentals store. For MVP, this is populated by:
1. Fetching current and last-4-quarters financials from East Money API (which provides historical report data)
2. Storing as `data/fundamentals_history/{stock_code}.json` with `report_period` and `disclosure_date` fields
3. For data before the tool existed: akshare `stock_financial_report_sina()` or similar as a one-time backfill

**Phase 1 (3 months):** Verify pipeline runs end-to-end on historical data with point-in-time constraints, produces outputs, no crashes. Verify that the fundamentals used in each simulated period are from the correct reporting quarter.

**Phase 2 (6-12 months):** Initial judgment on signal quality. Compare against:
- Random baseline: equal-weight random 20 from universe
- Simple-rule baseline 1: top 20 by 10-day relative strength only
- Simple-rule baseline 2: top 20 by ROE only

**Pass criteria:** Pipeline functional with correct point-in-time data (Phase 1). Multi-factor model not worse than simple baselines on 6+ month data (Phase 2).

#### M3: Layer 3 + Email

**Goal:** LLM reports + complete email output.

**Pass criteria:** First real weekly report sent, readable, data accurate, LLM reports coherent.

#### M4: Forward Tracking

**Goal:** Automated outcome recording + monthly review.

**Flow:**
1. Weekly screening → results written to `recommendations` table
2. 10 trading days later → cron fetches closing prices → computes returns → writes `outcomes` table + labels
3. Monthly → review email (win rate trend, factor contribution, best/worst cases)

**Pass criteria:** Tracking system operational, auto-backfill working.

### Decision Points

**After M2:**
- If backtest win rate < 45% AND avg alpha < -1% on 6-month data: pause project, review factor selection
- Diagnosis priority (in order): data errors/mapping bugs → factor definitions → weights (last resort)

**After M4 (12 periods, ~3 months):**
- If forward tracking win rate < 50% AND avg alpha < 0: enter diagnosis phase
- Diagnosis priority: same as above — data first, factors second, weights last

**After M4 (24 periods, ~6 months):**
- If still unable to consistently beat simple-rule baselines: archive project

### Configuration File Drafts

**`config/factors.json` (excerpt):**

```json
{
  "layer1": {
    "weekly": {
      "ma20_slope_positive": true,
      "price_above_ma20": true,
      "volatility_cap_a": 0.80,
      "volatility_cap_hk": 1.00,
      "volume_ratio_min": 0.6
    },
    "event_driven": {
      "close_above_ma60": true,
      "volatility_cap_a": 1.00,
      "volatility_cap_hk": 1.20,
      "volume_ratio_min": 0.4,
      "min_turnover_a": 30000000,
      "min_turnover_hk": 15000000
    }
  },
  "layer2": {
    "standardization": {
      "monotonic_factors": ["roe", "rev_growth", "profit_growth", "net_margin", "ma_alignment", "macd_value", "volume_price_sync", "ret_10d", "ret_20d", "rel_strength", "sentiment_avg"],
      "optimal_range_factors": ["rsi", "bollinger_pct", "news_heat"]
    },
    "weights": {
      "default_weekly": {
        "fundamental": 0.35,
        "technical_momentum": 0.40,
        "news_sentiment": 0.25
      },
      "event_driven": {
        "fundamental": 0.15,
        "technical_momentum": 0.50,
        "news_sentiment": 0.35
      }
    },
    "missing_data": {
      "low_confidence_threshold": 0.5,
      "low_confidence_weight_multiplier": 0.5
    }
  },
  "layer3": {
    "top_n_a": 15,
    "top_n_hk": 5
  },
  "labels": {
    "win_threshold": 0.01,
    "lose_threshold": -0.01
  },
  "event_triggers": {
    "index_drop_pct": 0.03,
    "volume_surge_ratio": 1.5,
    "vhsi_threshold": 30,
    "earnings_dense_week_threshold": 30
  },
  "event_bonuses": {
    "individual_volume_spike": {"ratio": 2.0, "bonus": 10},
    "breakout_60d_high": {"bonus": 8},
    "earnings_preview": {"bonus": 5},
    "high_news_frequency": {"mentions": 10, "days": 3, "bonus": 0, "flag_for_review": true}
  }
}
```

**`config/screener.json` (excerpt):**

```json
{
  "email": {
    "smtp_env_file": "~/.stock-monitor.env",
    "recipients": ["ch_w10@outlook.com"],
    "subject_prefix": "Stock Screener"
  },
  "llm": {
    "sentiment_model": "gpt-4.1-mini",
    "report_model_chain": ["gemini-2.5-pro", "gpt-4.1", "claude-sonnet"],
    "sentiment_batch_size": 20,
    "report_max_tokens": 500
  },
  "schedule": {
    "weekly_cron": "0 8 * * 6",
    "event_check_cron": "0 9 * * 1-5",
    "outcome_backfill_cron": "0 10 * * 1-5"
  }
}
```

### run_id Naming Convention

| Trigger | Format | Example |
|---------|--------|---------|
| Weekly | `{YYYY}-W{XX}-weekly` | `2026-W16-weekly` |
| Event (index drop) | `{YYYY-MM-DD}-event-{index}-drop` | `2026-04-18-event-csi300-drop` |
| Event (volume surge) | `{YYYY-MM-DD}-event-volume-surge` | `2026-04-18-event-volume-surge` |
| Event (VIX spike) | `{YYYY-MM-DD}-event-vhsi-spike` | `2026-04-18-event-vhsi-spike` |
| Event (earnings dense week) | `{YYYY-MM-DD}-event-earnings-dense` | `2026-04-18-event-earnings-dense` |
| Event (combined, same day) | `{YYYY-MM-DD}-event-combined` | `2026-04-18-event-combined` |

If multiple triggers fire on the same day, they are consolidated into a single combined run (not separate runs per trigger). Cooldown: no repeat event run for the same trigger type within 5 trading days.
