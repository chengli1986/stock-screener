# MVP-Trend & MVP-Dislocation — Design Spec (WIP)

**Stage**: 在 Layer 1 Weekly 之前插入两个最小可行筛器，验证用户两个核心画像
**Date**: 2026-04-30 BJT
**Status**: **WIP — brainstorming session #1 completed, decisions §1–§5 locked, §2 onwards 未 present 给用户**
**Prior art**:
- [`2026-04-14-stock-screener-design.md`](./2026-04-14-stock-screener-design.md) — MVP 整体架构 + Layer 1/2/3 funnel
- [`2026-04-18-layer1-weekly-design.md`](./2026-04-18-layer1-weekly-design.md) — Layer 1 Weekly 设计 §0–§3 frozen，§4–§5 WIP；§0 已声明 trend-side baseline + 预留 M1.5 dislocation 通道
- [`../plans/2026-04-15-phase0-data-spike.md`](../plans/2026-04-15-phase0-data-spike.md) — Phase 0 spike（已完成 2026-04-17）

---

## Session context

本次 brainstorming（2026-04-30）由用户 surface 的两个具体目标驱动：

1. **跟踪热门股票** — 高业绩增长 + 高交易量的热门股
2. **从市场中选择高业绩增长但价格位置偏低、估值被错杀的个股**

用户同时表达对"大而全设计后输出不符预期"的担忧，明确选择**砍掉非核心 scope，先做两个 MVP 验证画像**。

本次 session 钉死了 universe、共享筛子、两条腿规则、输出形式、验证标准、行业字段共 11 个关键决策（详见 §1–§5）。**§1 已 present 给用户并 implicitly accepted；§2 onwards 未 present。下次 session 从 §2 起逐节呈现 → 用户批准 → spec self-review → 用户审阅 → writing-plans。**

---

## §0 — 与 Layer 1 Weekly 的关系

本 spec **不是** Layer 1 Weekly 的替代或修订，而是**前置插入的两个 MVP**：

- Layer 1 Weekly（`2026-04-18-layer1-weekly-design.md`）的 §0 已声明：当前 4 条规则是 trend-side baseline，用户真实偏好是 dislocation-side，schema 已预留 `entry_pathway='trend'|'dislocation'` 字段。
- 本 spec 把 Layer 1 Weekly 设计文档里那条 dislocation 通道（M1.5）**提前到第一阶段**，与 trend 通道并行验证，避免一上来就把 §1–§5 全部规则集 + sector tagging + HK fallback + 错误处理打包推到 production 才发现"画像不对"。
- 验证后两条腿都画像稳定，再合并到 Layer 1 Weekly 的完整 funnel + Layer 2 多因子打分。

**对 Layer 1 Weekly spec 的影响**：暂不解冻 §1–§3，§4–§5 仍待之后处理。本 MVP 复用 §3 的 4 条规则但做了两处简化（详见 §3 below）。

---

## §1 — 范围 + 数据流（LOCKED, presented to user）

### 目标

两个最小可行筛器并行运行，**共享一份代码、一份 universe、一份数据；输出两份独立的 Top 20**：

- **MVP-Trend** — 验证"热门 + 高增长 + 高量"画像
- **MVP-Dislocation** — 验证"高增长 + 价格偏低 + 估值错杀"画像

### Universe

- A 股 CSI 300 + CSI 500（约 800 只），来自 `akshare.index_stock_cons_csindex`
- 沿用 Phase 0 §A 8 字段 schema（`market`, `symbol_raw`, `symbol_norm`, `name`, `universe_source`, `source_status`, `last_verified`, `source_note`）

### 数据源（沿用 Phase 0 验证过的栈）

| 字段类 | 来源 | 用途 |
|--------|------|------|
| 营收同比、净利润同比、PE_TTM、行业 `f127`、流通股本 | 东方财富 push2（每股 1 次调用） | 共享筛子 + 排序 + 输出表 |
| 近 252 交易日 OHLCV | Longbridge CLI（4 workers 并行） | 52w 高点回撤、MA20、5/60 日均量、20 日涨幅、60 日 sparkline |
| 换手率 | OHLCV 成交量 ÷ 流通股本，本地计算 | Trend 硬门槛 |

### As-of 时点

- OHLCV 最新交易日（运行日为交易日则 T-1，否则 T-N）
- 基本面取脚本运行时刻 push2 最新一期

### 数据缺失策略

| 情形 | 策略 |
|------|------|
| 基本面字段缺失（营收/净利润/PE/f127 任一） | 该股从两个候选池都剔除（保守，避免分母 NaN） |
| OHLCV 不足 252 交易日（次新股） | 仅从 Dislocation 池剔除（Trend 不受影响，只需 60 日历史） |
| 最新季报未披露（披露窗口边界期，比如运行日是 4/30 但 Q1 还没出） | 该股本周剔除（保守） |
| 行业 `f127` 为空 | 标 "未分类"，不剔除 |

### 范围外（明确不做）

- HK 板块（fundamental 数据残缺，详见 Phase 0 §I）
- Layer 2 多因子打分
- Layer 3 LLM 研报
- 自动 cron（手动触发，画像验证 4 周后再上）
- ST/退市风险过滤（MVP 先不做，画像验证期间靠人工标注捕捉）
- 回测

### 预估单次运行耗时

universe (~10s) + push2 fundamentals (800 × ~0.5s sequential = ~7 min) + Longbridge OHLCV (800 × ~3.8s ÷ 4 workers = ~13 min) + 渲染 ≈ **20-22 分钟**

---

## §2 — 共享筛子：高增长 gate（LOCKED, NOT yet presented to user）

四条硬门槛，**两个 MVP 都先过这个 gate 再各自走自己的规则**：

1. **最新单季营收同比 ≥ 30%**
2. **最新单季净利润同比 ≥ 30%**
3. **兜底**：最新单季净利润同比 ≤ 200%（过滤基数效应失真，比如去年同期亏损导致今年增速 N 倍的股）
4. **季报状态**：必须有最新一期已披露财报；运行日处于披露窗口边界期（如 Q1 截止 4/30 当天）若该股 Q1 未披露 → 本周剔除

**预计候选池密度**：800 只过 gate 后 60-100 只（基于一般市场状态估算，实际密度需第一次跑出来观察）。

---

## §3 — MVP-Trend 通道（LOCKED, NOT yet presented to user）

### 在共享筛子之后，硬门槛

| 规则 | 阈值 | 来源 |
|------|------|------|
| MA20 当日值 > MA20 五日前值 | — | §3 of Layer 1 Weekly（`ma20_slope_positive`） |
| 收盘价 > MA20 | — | §3 of Layer 1 Weekly（`price_above_ma20`） |
| 60 日年化波动率 ≤ 上限 | **60% 年化**（日 3.8%，2026-05-02 确认） | §3 of Layer 1 Weekly（`volatility_cap`） |
| 5 日均成交量 / 60 日均成交量 ≥ **1.5** | 1.5x | **新增**（§3 原是 ≥ 0.7 "量能不塌"，本 MVP 升级到 ≥ 1.5 "量能放大"） |
| 最新交易日换手率 ≥ **5%** | 5% | **新增** |

> **简化点**：原 §3 的 `volume_not_collapsing`（5日/60日 ≥ 0.7）被升级到 ≥ 1.5 后自动覆盖，MVP 里只保留 ≥ 1.5。波动率上限具体值待 spec finalize 时讨论。

### 排序

- **近 20 个交易日累计涨幅，降序**

### 取 Top 20

如候选池过完上述硬门槛后不足 20 只，输出全部并标注实际数量（见 §6 metadata）。

---

## §4 — MVP-Dislocation 通道（LOCKED, NOT yet presented to user）

### 在共享筛子之后，硬门槛

| 规则 | 阈值 |
|------|------|
| 距 52 周（**252 交易日**）最高收盘价的回撤幅度 ≥ 20% | 20% |
| PE_TTM > 0 | — |
| 持有 ≥ 252 交易日 OHLCV 历史 | 排除次新股 |

### 排序

- **PEG = PE_TTM / 单季净利润同比增速（百分数转小数），升序**
  - PEG ≤ 1 是经典低估区
  - 共享筛子的 "增速 ≤ 200%" 兜底保证 PEG 分母不会失真极小

### 取 Top 20

同 §3，不足 20 只时输出全部。

---

## §5 — 输出 + 验证（LOCKED, NOT yet presented to user）

### 触发方式

**命令行手动触发**，不上 cron。
- 推荐运行时机：每周日晚或周一早，看周五收盘后的快照
- 4 周观察期画像稳定后再考虑上 cron（仍倾向周日晚 22:00 BJT，让用户周末有时间消化）

### 邮件配置

- **收件人**：`ch_w10@outlook.com`（与 stock-monitor 的 chengli1986@gmail.com 区分开）
- 复用 `~/.stock-monitor.env` SMTP 配置或新建独立配置（spec finalize 时讨论）

### HTML 邮件结构

**Header metadata**（一行小字）：
- universe 总数 (CSI 300+500)
- 过共享筛子的实际候选池数量
- 数据 as-of 日期（OHLCV 最新交易日）
- 运行日

**两份 Top 20 表格**（trend / dislocation 各一）：

字段 | Trend | Dislocation
---|---|---
股票代码 | ✓ | ✓
股票名称 | ✓ | ✓
行业（f127） | ✓ | ✓
营收同比 | ✓ | ✓
净利润同比 | ✓ | ✓
PE_TTM | ✓ | ✓
距 52w 高点回撤 | ✓ | ✓
近 20 日涨幅 | ✓ | ✓
PEG | — | ✓
5 日量比 + 换手率 | ✓ | —
60 日 mini sparkline | ✓ | ✓

### 验证标准（hit rate 标注法）

每周收到邮件后，对每条 Top 20 标 yes/no（"这是不是我心里的画像股"）。**4 周累计 hit rate ≥ 14/20（70%）视为画像稳定**，可推进到 Layer 1 Weekly 全量集成。

#### 画像 rubric（spec finalize 时与用户共同审阅这两段文字）

> **Trend 画像**：当前市场关注度高（量价齐升）、业绩处于明显加速期、所属板块或主题正在被市场定价。**反例**（标 no）：纯粹炒作 / 业绩造假风险 / 量起来但价不动的滞涨股。
>
> **Dislocation 画像**：基本面持续高增长，但近期股价回撤显著，估值（PEG）在合理偏低区间，看起来是"市场杀估值但基本面没坏"的状态。**反例**：业绩拐点向下 / 行业系统性问题 / 价值陷阱（看起来便宜但永远便宜）。

---

## §6 — WIP / 待下次 session

下次 brainstorming（earliest 2026-05-01 BJT）从此处接着做：

1. **Present §2 → §5 给用户逐节确认**（§1 今天已 present，其余只在对话中口头确认了规则细节但未做完整章节呈现）
2. **§7 错误处理 + resume**（沿用 Phase 0 §B 三态分类 + §E row-level resume，但需要为 MVP 明确：universe 取数失败、push2 失败、Longbridge 失败分别怎么降级）
3. **§8 测试策略**（复用 Phase 0 的 15 只 fixture stocks 已 verified 2026-04-16；为新增的"高增长 gate"+"PEG 排序"+"换手率门槛" 写单元测试）
4. **§9 文件结构 + 依赖**（沿用 `scripts/` 还是新增 `mvp/` 子目录；与 `phase0_spike.py` 的代码复用关系）
5. **波动率上限具体数值拍板**（建议 60% 年化）
6. **画像 rubric 文字与用户共同审阅并冻结**（§5 提议的两段话需用户明确批准）
7. **Spec self-review**（placeholder / 内部一致性 / scope / 歧义）
8. **用户审阅 spec**
9. **Invoke `superpowers:writing-plans` skill 写实现计划**

---

## 决策记录（12 项全部 LOCKED）

| # | 决策 | 选项 | 用户选择 |
|---|------|------|----------|
| 1 | 策略两条腿先做哪条 | A 先 Trend / B 先 Dislocation / C 并行 | **C** |
| 2 | "高增长"定义 | A 单季 / B TTM / C 双门槛 | **A** |
| 3 | "高增长"阈值 | A 双≥30% / B 双≥50% / C 营收20%+利润30% / D 营收30%+利润≥0 / E 任一≥30% | **A** |
| 4 | Universe 范围 | A 股 only vs 含 HK | **A 股 only** |
| 5 | MVP-Trend 规则 | A 复用§3+热门度打分 / B §3+量+换手硬门槛 / C 砍波动率上限 | **B** |
| 12 | Trend 波动率上限具体值 | 50% / 60% / 70% / 80% | **60%（日均 ±3.8%）** |
| 6 | MVP-Dislocation 规则 | A 简单PE升序 / B PEG升序 / C 行业相对 | **B** |
| 7 | 52w 高点窗口 | 252 交易日 vs 365 自然日 | **252 交易日** |
| 8 | 输出 + 频率 | A 命令行手跑 / B 周一 cron / C 周日晚 cron | **A** |
| 9 | 验证方式 | A 主观 / B hit rate 标注 / C 持仓回测 | **B** |
| 10 | 行业字段 | A 不加 / B 东方财富 f127 / C Shenwan | **B** |
| 11 | 邮件收件人 | chengli1986@gmail.com / ch_w10@outlook.com | **ch_w10@outlook.com** |

---

**End of WIP spec. Resume from §6 next session.**
