# Layer 1 Weekly Screener — Design Spec

**Stage**: M0 收口 + M1 Layer 1（Weekly only）
**Date**: 2026-04-18 BJT
**Status**: Sections 1–3 frozen; Sections 4–5 WIP (next brainstorming session)
**Prior art**: builds on [`2026-04-14-stock-screener-design.md`](./2026-04-14-stock-screener-design.md) (MVP spec §2 Layer 1 + §5 M0/M1) and [`../plans/2026-04-15-phase0-data-spike.md`](../plans/2026-04-15-phase0-data-spike.md) (Phase 0 spike, completed 2026-04-17)

---

## Session context

This spec is the output of a brainstorming session that took place on 2026-04-18, after Phase 0 spike was completed and reviewed. The original MVP design spec (§5) used Milestones M0–M4; "Phase 1" is loose terminology we used in conversation. This document scopes the concrete next implementation step as **M0 productionization + M1 Layer 1 Weekly**.

User went through three rounds of rigorous review on Sections 2 and 3 before freezing. Sections 4 and 5 are explicitly parked pending another session.

---

## Section 1 — Scope & Delivery

### 交付画面

敲一个命令（`python scripts/run_screener.py --mode weekly`）→ 10-60 分钟后产出：

1. `artifacts/layer1/candidates_YYYY-MM-DD.csv` — 通过所有硬门槛的候选股清单（预估 A 股 ~200 + HK ~30-50）
2. `artifacts/layer1/report_YYYY-MM-DD.html` — 浏览器打开，逐股显示命中/失败的每条规则 + 实测值 + 阈值，用于人工判断规则合理性

### 明确不做

- 不给通过的股票打分排序（Layer 2 的事）
- 不写 LLM 研报（Layer 3 的事）
- 不接 cron（下阶段和 Layer 2 一起接）
- 不做 event-driven 门控（Weekly 验证过再说）
- 不做智能 caching / 增量更新（开发用 `--limit N` 小样本调试即可）

### 要做的 5 件事

1. 数据层从 `phase0_spike.py` 单脚本拆成 `data/` 模块包 + 两个 Phase 0 已知 bug 修复（ROE=0 歧义 + HK 恒生银行 fallback）
2. Sector tagging（f127 → 申万一级映射，是修 ROE=0 bug 的前置）
3. Layer 1 四条规则 + TDD 单元测试
4. HTML 调试报告生成器
5. `run_screener.py` 入口脚本串联上面所有步骤

### 预估

2-3 周实施；~500-800 行代码；~56 个测试（含 API probe fixtures 回归）。

### 开发约定

- **触发**：纯手动 CLI，不接 cron（下一阶段）
- **测试策略**（混合）：
  - Layer 1 规则函数 + Sector mapping + classify_fundamentals → TDD（纯函数，阈值明确）
  - 数据抓取模块（fetch_*.py）→ spike 风格 + API probe fixture 回归（mock HTTP 价值低）

---

## Section 2 — 组件架构 & 数据契约（v5 冻结版）

### 目录结构

```
~/stock-screener/
├── config/
│   ├── hk_constituents.json            # Phase 0 已有，本阶段不动
│   ├── sector_mapping.json             # 新增（结构待 Section 4 probe 确认后冻结）
│   └── screener.json                   # 新增（Layer 1 阈值等运行参数，见 §3）
├── data/                               # 新增
│   ├── schema.py                       # 统一列名常量、状态/source 枚举、join key
│   ├── fetch_universe.py
│   ├── fetch_ohlcv.py                  # 产出 2 个 DataFrame: metadata_df + bars_df
│   ├── fetch_fundamentals.py           # 保留 raw presence + raw value，不再 `or None` 吞 0
│   ├── classify_fundamentals.py        # 纯函数：raw API data → tri-state + source + review_needed
│   └── sector_tagger.py                # f127 → sector_shenwan_l1
├── screener/
│   ├── layer1_rules.py                 # 每条规则返回 RuleResult (dataclass)，非裸 bool
│   ├── layer1_filter.py                # 编排：对每只股票应用 4 条规则
│   └── config_resolver.py              # 把 market-specific threshold 解析成单值
├── output/
│   └── html_report.py                  # 仅渲染，不做业务判定
├── scripts/
│   ├── phase0_spike.py                 # 保留作历史回放
│   ├── probe_eastmoney.py              # 新增（Section 4 probe，一次性跑）
│   └── run_screener.py                 # 新增入口脚本
├── tests/
│   ├── fixtures/eastmoney_probe/       # 新增，probe 结果固化为 6 个 JSON
│   ├── test_layer1_rules.py            # ~21 个（含 insufficient_data / invalid 分支）
│   ├── test_layer1_filter.py           # 集成测试，~5 个
│   ├── test_classify_fundamentals.py   # 8 个（基于 fixture 真实 payload）
│   ├── test_sector_tagger.py           # ~12 个（见 §4 后续）
│   ├── test_config_resolver.py         # ~4 个
│   └── test_hk_fallback.py             # ~5 个（见 §4 后续）
└── artifacts/
    ├── phase0/                         # Phase 0 已有
    └── layer1/                         # 新增
        ├── candidates_YYYY-MM-DD.csv
        └── report_YYYY-MM-DD.html
```

### 三层数据契约

#### `universe_df` (885 × 8, static)

```
symbol_norm, market, symbol_raw, name,
universe_source, source_status, last_verified, source_note
```

来自 `fetch_universe.py`，和 Phase 0 §A schema 完全一致。

#### `ohlcv_metadata_df` (885 × 6, per-stock fetch status)

```
symbol_norm, rows, time_s, fetch_status, error_type, error_msg
```

#### `ohlcv_bars_df` (~53100 × 8, long table — Phase 0 没有，本阶段新增持久化)

```
symbol_norm, bar_date, open, high, low, close, volume, turnover
```

**Phase 0 只存了 metadata，没持久化 bars**。Layer 1 规则需要 bars 才能算 MA20 / 波动率，所以 fetch_ohlcv 必须新增输出 bars_df。

#### `fundamentals_df` (885 × **40**)

粒度：**每个财务字段都有 4 个配套列**（value / status / source / review_needed）。

```
# identity + row-level fetch meta (6 cols)
symbol_norm, market, fetch_status, fetch_time_s, error_type, error_msg

# 8 financial fields × 4 cols each = 32
# for each field in {roe_ttm, revenue_growth, net_profit_growth, net_margin_ttm,
#                    gross_margin, pe_ttm, pb, market_cap}:
#   <field>_value          # float | None
#   <field>_status         # 'available' | 'missing_expected' | 'fetch_error'
#   <field>_source         # 'east_money' | 'longbridge_fallback' | 'not_fetched'
#   <field>_review_needed  # bool

# sector (2 cols)
sector_f127, sector_shenwan_l1
```

**设计决策**：

- 删除 v4 的 row-level `source_fundamentals` 和 `fallback_used`（冗余）。per-field source 即可派生（`df.filter(regex='_source$').nunique(axis=1)` 产 mixed 视图）。
- `*_source = 'not_fetched'` 专门用于"整行 API 调用失败，字段根本没尝试过"的场景，和 `'east_money' + status='fetch_error'`（"尝试过但字段为 null/0"）严格区分。调试时两种情况要分开处理。

#### `classify_field()` 纯函数

```python
def classify_field(
    raw_presence: bool,      # API JSON 中是否包含该 key
    raw_value: float | None, # 原始值，未经 `or None` 处理（保留 0.0）
    market: str,             # 'a' | 'hk'
    sector: str | None,      # 申万一级中文名（sector_tagger 先跑完）
    field_name: str,
    source: str,             # 'east_money' | 'longbridge_fallback'
) -> dict:
    """
    Returns: {value, status, source, review_needed}
    """
```

分类决策树：

```
raw_presence=False OR raw_value=None:
    if (market, field_name) ∈ DEFAULT_MISSING_EXPECTED_SET:
        status='missing_expected', review_needed=False
    elif market='a' AND field_name='roe_ttm' AND sector ∈ {电力, 新能源, pre-profit 生物医药}:
        status='fetch_error', review_needed=True  # 标记待查，不隐式改 available
    else:
        status='fetch_error', review_needed=False

raw_presence=True AND raw_value is a number (including 0):
    status='available', value=raw_value, review_needed=False
```

`DEFAULT_MISSING_EXPECTED_SET`:

- HK × {`revenue_growth`, `net_profit_growth`, `net_margin_ttm`, `gross_margin`}
- A 股金融（银行/保险/证券） × {`gross_margin`}

**不做启发式转 `available`**：对 "A 股 ROE=0 且 sector 符合电力/新能源/pre-profit 生物医药" 的情况，classify 输出 `review_needed=True`，coverage report 列出让人工确认，不隐式把 `fetch_error` 改写为 `available`。

### Phase 0 已知 bug 修（必须，在 fetcher 层）

`phase0_spike.py:452-464` 用 `raw = data.get("f173") or None` 会把 API 返的 `0` 吞成 `None`，导致 classify 层无法区分"API 返 0"和"API 返 null"。

修正：

```python
# WRONG (Phase 0)
roe_ttm = data.get("f173") or None

# CORRECT (Layer 1 实现)
raw_presence = "f173" in data
raw_value = data.get("f173") if raw_presence else None
# then pass (raw_presence, raw_value) to classify_field
```

### Layer 1 规则结果持久化（拆两张 long table）

内存中用 `@dataclass(frozen=True) RuleResult` 传递，**不要把 dataclass/object 塞进 DataFrame 单元格**。持久化拆为：

**`screening_summary_df` (885 × 4)**

```
symbol_norm, overall_passed, passed_rule_count, failed_rule_count
```

**`rule_results_df` (885 × 4 = 3540 rows × 7 cols)**

```
symbol_norm, rule_name, passed, actual_value, threshold, operator, reason_code
```

HTML 报告按 `symbol_norm` join 两表；CSV 直接 dump。

### Schema 中央化 (`data/schema.py`)

统一定义：

- 列名常量（避免各模块字符串散写）
- `FIELD_STATUS ∈ {available, missing_expected, fetch_error}`
- `FIELD_SOURCE ∈ {east_money, longbridge_fallback, not_fetched}`
- `LAYER1_REASON_CODES_COMMON = {pass, insufficient_data, invalid_input, below_threshold, above_threshold}`
- Join key: `SYMBOL_NORM_COL = 'symbol_norm'`

### 数据流

```
run_screener.py
   │
   ├─ fetch_universe()                         → universe_df
   ├─ fetch_ohlcv()                            → (ohlcv_metadata_df, ohlcv_bars_df)
   ├─ fetch_fundamentals()                     → raw_records list
   │    ├─► sector_tagger.attach(raw_records)  → + sector_f127, sector_shenwan_l1
   │    └─► classify_fundamentals(...)         → fundamentals_df (40 cols)
   │       ├─ per-field call to classify_field
   │       ├─ HK fallback trigger check (§4)
   │       └─► optional Longbridge call for triggered rows; re-classify
   │
   ▼
layer1_filter(universe_df, ohlcv_bars_df, fundamentals_df, config)
   │  per symbol:
   │    bars = ohlcv_bars_df.query('symbol_norm == @s')
   │    for rule_fn in [ma20_slope_positive, price_above_ma20,
   │                    volatility_cap, volume_not_collapsing]:
   │       rule_cfg = resolve_market_config(rule_fn.__name__, market, global_cfg)
   │       result = rule_fn(bars, rule_cfg)   # returns RuleResult
   │    overall = all(r.passed for r in results)
   │
   ▼
(screening_summary_df, rule_results_df)
   │
   ▼
html_report.render(...)                       → report.html
CSV writer                                     → candidates.csv
```

### 测试总数

| 文件 | 测试数 |
|------|-------|
| `test_layer1_rules.py` | ~21 |
| `test_layer1_filter.py` | ~5 |
| `test_classify_fundamentals.py` | ~8 |
| `test_sector_tagger.py` | ~12 (见 §4) |
| `test_config_resolver.py` | ~4 |
| `test_hk_fallback.py` | ~5 (见 §4) |
| **合计** | **~55-56** |

---

## Section 3 — Layer 1 四条规则（v2 冻结版）

### 统一签名

```python
def rule_fn(bars: pd.DataFrame, cfg: dict) -> RuleResult
```

- `bars`: `ohlcv_bars_df` 按 `symbol_norm` 过滤出的子集，`bar_date` 升序
- `cfg`: 该规则的参数，已由 `config_resolver.resolve_market_config()` 解析好 market suffix（规则函数本身 market-agnostic）
- 规则自检 `len(bars) < cfg['min_bars_required']` → 返回 `reason_code='insufficient_data'`

### `RuleResult` dataclass

```python
@dataclass(frozen=True)
class RuleResult:
    rule_name: str             # 'ma20_slope_positive' | ...
    passed: bool
    actual_value: float | None
    threshold: float | None
    operator: str              # '>', '>=', '<', '<='
    reason_code: str           # LAYER1_REASON_CODES_COMMON ∪ rule-specific ext
```

`reason_code` 类型为 `str`（不用 Enum）；新增规则私有码（如 `invalid_volume_baseline`）必须在 schema.py 文档更新同一 PR 里。

### 4 条规则精确定义

#### 规则 1: `ma20_slope_positive`

- **含义**: MA20 在过去 5 个交易日净斜率 > 0 (5-day net positive slope)
- **formalization**: `MA20[t] > MA20[t-5]` （不做 monotonic 检查）
- **`actual_value`**: `(MA20[t] - MA20[t-5]) / MA20[t-5]` (相对斜率)
- **`threshold`**: 0
- **`operator`**: `>`
- **`min_bars_required`**: 25 (MA20 需 20 根 + 5 天回看)
- **额外保护** (plan 时补代码)：若 `MA20[t-5] <= 1e-6`，返回 `reason_code='ma20_baseline_invalid'`, `passed=False`

#### 规则 2: `price_above_ma20`

- **含义**: 最新 `close > MA20[t]`
- **`actual_value`**: `(close - MA20) / MA20`（相对偏离度）
- **`threshold`**: 0
- **`operator`**: `>`
- **`min_bars_required`**: 20

#### 规则 3: `volatility_cap`

- **含义**: 60 日对数收益率年化标准差上限
- **formalization**: `std(log_returns[-60:]) * sqrt(252)`
- **`actual_value`**: 年化 vol（小数形式，如 0.34 = 34%）
- **`threshold`**: A 股 0.80 / HK 1.00
- **`operator`**: `<`
- **`min_bars_required`**: 60

#### 规则 4: `volume_not_collapsing`

- **含义**: 短期成交量相对长期未过度萎缩
- **formalization**: `mean(volume[-5:]) / mean(volume[-20:])`
- **`actual_value`**: 比值
- **`threshold`**: A/HK 同为 0.6
- **`operator`**: `>=`
- **`min_bars_required`**: 20
- **额外保护**: 若 `mean(volume[-20:]) <= 1e-6`（停牌 / 异常数据 / 新上市首日），返回 `reason_code='invalid_volume_baseline'`, `passed=False`

### `config/screener.json` 结构（冻结）

```json
{
  "preferred_bars": 60,
  "layer1_weekly": {
    "ma20_slope_positive": {
      "min_bars_required": 25,
      "lookback_days": 5,
      "threshold": 0
    },
    "price_above_ma20": {
      "min_bars_required": 20,
      "threshold": 0
    },
    "volatility_cap": {
      "min_bars_required": 60,
      "window_days": 60,
      "trading_days_per_year": 252,
      "threshold_a": 0.80,
      "threshold_hk": 1.00
    },
    "volume_not_collapsing": {
      "min_bars_required": 20,
      "short_window": 5,
      "long_window": 20,
      "zero_baseline_epsilon": 1e-6,
      "threshold_a": 0.60,
      "threshold_hk": 0.60
    }
  }
}
```

### `resolve_market_config()` 契约

```python
# screener/config_resolver.py
def resolve_market_config(
    rule_name: str,
    market: str,             # 'a' | 'hk'
    global_cfg: dict,
) -> dict:
    """
    Collapse market-specific suffixes (_a / _hk) into single keys before
    passing to a market-agnostic rule function. Raises:
      - KeyError if rule_name not in global_cfg['layer1_weekly']
      - ValueError if market not in {'a', 'hk'}

    Example:
      Input:  cfg = {'threshold_a': 0.80, 'threshold_hk': 1.00, 'window_days': 60}
              market = 'a'
      Output: {'threshold': 0.80, 'window_days': 60}
    """
```

必须显式对未知 market / 缺失 rule_name 报错（不要静默返回半残 config）。

### `reason_code` 枚举约定

```python
# data/schema.py
LAYER1_REASON_CODES_COMMON = {
    'pass',                # 规则通过
    'insufficient_data',   # bars 数少于 min_bars_required
    'invalid_input',       # bars 含 NaN / 结构问题
    'below_threshold',     # actual_value < threshold（应 >）
    'above_threshold',     # actual_value > threshold（应 <）
}
# 规则私有扩展命名: <rule_short>_<condition>
# 示例: 'invalid_volume_baseline', 'ma20_baseline_invalid'
```

### 测试文件对应

- `test_layer1_rules.py` 每条规则 ~4-5 个用例 = ~21 个:
  - pass
  - below / above threshold
  - insufficient_data (给 `min_bars_required - 1` 根 bars 触发)
  - invalid（如 rule 4 的 `invalid_volume_baseline`，rule 1 的 `ma20_baseline_invalid`）
- `test_config_resolver.py` ~4 个: A 股解析 / HK 解析 / market-neutral key 透传 / 未知 market / 缺失 rule 报错

### 微调备注（plan 阶段实现时带上）

1. `ma20_slope_positive` 的 `actual_value` 分母 `MA20[t-5]` 若 ≤ 1e-6，防除零炸裂（上面已在"额外保护"里写入）
2. `resolve_market_config()` 对未知 market / 缺失 rule_name 显式抛 `ValueError` / `KeyError`，不要静默返回半残 config

---

## Section 4 — Sector Tagging + HK Fallback + classify 集成 (WIP)

[STATUS: DRAFT — brainstorming in progress, not yet approved]

Key open questions parked for next session:

1. **`f127` 实际返回形态** — Phase 0 的 `EM_FIELDS` 不包含 `f127`。plan 第 1 步需要用 `scripts/probe_eastmoney.py` 实测返回值是中文名还是代码、是申万还是中信、层级。6 个 fixture 将 dump 全量字段（`fields=ALL`）一次性完成 sector + fundamentals probe。
2. **HK sector 来源** — 东方财富对 HK 是否返 `f127`? 若返，是否恒生行业? 若不返，Longbridge `security_info` 是否有行业字段? 这决定 HK 的 `sector_shenwan_l1` 是否强行映射 or 填 `None`。
3. **`config/sector_mapping.json` 结构** — 两种情景（直接透传 vs code-to-L1 lookup），取决于 probe 结果。
4. **HK fallback 触发阈值** — 建议"核心 4 字段（roe/pe/pb/market_cap）中 ≥ 2 个缺失"触发 Longbridge fallback；合并策略 per-field（不覆盖 east_money 成功字段）。
5. **执行顺序** — sector_tagger 必须在 classify 之前跑，解决 ROE=0 review_needed 判断依赖 sector 的鸡生蛋问题。

Next session will close out §4 and move to §5 (error handling / resume / run_screener entrypoint / final artifacts schema).

---

## Section 5 — Error Handling / Resume / Entrypoint (WIP)

[STATUS: not yet discussed]

Parked for next session.

---

## Review log

| Date | Section | Rounds | Outcome |
|------|---------|--------|---------|
| 2026-04-18 | §1 scope | 1 | frozen |
| 2026-04-18 | §2 architecture | 5 (v1 → v5) | frozen |
| 2026-04-18 | §3 rules | 2 (v1 → v2) | frozen |
| TBD | §4 sector + fallback | in progress | — |
| TBD | §5 ops + entrypoint | not started | — |

---

## Session end state (2026-04-18 BJT)

Sections 1–3 frozen; §4 drafted but reviewing; §5 not yet opened. Next session resumes at §4 with probe design and HK fallback threshold.
