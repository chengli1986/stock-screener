# Stock Screener

A-share + HK stock screening tool with multi-layer funnel, multi-factor scoring, LLM-assisted reports, and built-in validation framework.

## Architecture

```mermaid
flowchart TD
    A[Universe\nA股: 沪深300 + 中证500\n港股: 恒指 + 国企指数] --> B[Point-in-time 快照\n历史成分 / 财报滞后 / 防前视偏差]

    B --> C[触发层]
    C --> C1[Weekly\n每周 cron]
    C --> C2[Event-driven\n每日检查]
    C2 --> C3[指数大跌 / 成交量激增 / VHSI / 披露密度]
    C3 --> C4[同日事件合并为 combined run]

    C1 --> D[Layer 1 粗筛]
    C4 --> D

    D --> D1[Weekly 门控\nMA20 向上\n收盘价站上 MA20\n波动率不过高\n量能不塌]
    D --> D2[Event 门控\n去掉 MA20 趋势要求\n改用 MA60 地板\n保留波动率和量能约束]
    D1 --> E[候选池]
    D2 --> E[候选池]

    E --> F[Layer 2 多因子评分]
    F --> F1[基本面\nROE / 营收增速 / 净利润增速 / 净利率]
    F --> F2[技术+动量\nMA / MACD / RSI / 布林 / 量价\n10日收益 / 20日收益 / 相对强弱]
    F --> F3[新闻\n热度 / 情绪]
    F --> F4[标准化\n单调因子: percentile\n区间型因子: 规则映射]
    F --> F5[缺失值与置信度处理]
    F --> F6[事件模式加分项]

    F --> G[分池排名\nA股与港股分开\n不混排]
    G --> G1[Top 15 A股]
    G --> G2[Top 5 港股]

    G1 --> H[Layer 3 LLM 研报]
    G2 --> H
    H --> H1[Gemini 2.5 Pro 主\nGPT-4.1 / Claude fallback]
    H --> H2[只解释不打分\n输出核心逻辑 / 风险 / 价位解读 / 置信度]

    H --> I[输出]
    I --> I1[HTML 邮件]
    I --> I2[JSON 归档]
    I --> I3[SQLite 跟踪]

    I2 --> J[前瞻跟踪]
    I3 --> J
    J --> J1[stock-week 去重]
    J --> J2[5天冷却期]
    J --> J3[10个交易日后回填收益]
    J --> J4[胜率 / 平均超额 / 最大回撤 / 信息比率]

    B --> K[回测]
    K --> K1[仅跑 Layer 1 + Layer 2]
    K --> K2[财务数据按发布滞后 +45天]
    K --> K3[新闻降级为关键词代理\n不用 LLM]
    K --> K4[对比基线\n随机 / 相对强弱 / ROE]

    J --> L[决策]
    K --> L
    L --> L1[继续迭代]
    L --> L2[进入诊断]
    L --> L3[项目归档]
```

## Overview

```mermaid
flowchart LR
    A[股票池\nA股核心指数 + 港股核心指数] --> B[数据准备\n行情 / 财务 / 新闻]
    B --> C[两种触发方式]
    C --> C1[每周固定跑一次]
    C --> C2[突发事件额外跑一次]

    C1 --> D[第一层: 先排除\n不能交易 / 流动性差 / 风险极端的股票]
    C2 --> D

    D --> E[第二层: 综合打分]
    E --> E1[基本面\n公司质量和增长]
    E --> E2[技术面\n位置、趋势、强弱]
    E --> E3[新闻面\n热度、情绪、事件]

    E --> F[分别选出\nA股前15 + 港股前5]
    F --> G[第三层: AI 生成简报]
    G --> G1[解释为什么值得关注]
    G --> G2[提示量化指标没覆盖到的风险]
    G --> G3[解释关键支撑/阻力位]

    G --> H[输出结果]
    H --> H1[每周邮件报告]
    H --> H2[历史结果归档]
    H --> H3[持续跟踪表现]

    H3 --> I[验证系统是否真有效]
    I --> I1[回测\n看历史上是否优于基线]
    I --> I2[前瞻跟踪\n看未来几个月真实表现]

    I --> J{结果是否持续有效}
    J -->|是| K[继续迭代和扩展]
    J -->|一般| L[诊断数据/因子/权重]
    J -->|否| M[停止投入，项目归档]
```

## Key Design Decisions

- **Independent repo** — no symlink/dependency on stock-monitor; lean data layer copied and slimmed down
- **A-share and HK ranked separately** — different liquidity, data sources, and trading accounts
- **Dual trigger mode** — weekly scheduled + event-driven (with separate Layer 1 gates per mode)
- **LLM explains, not scores** — Layer 3 generates reports but does not influence ranking
- **Point-in-time backtest** — fundamentals use +45d publication lag, news degrades to keyword proxy
- **Built-in stop conditions** — 12-period diagnosis gate, 24-period archive gate

## Tech Stack

- Python 3.12, `~/stock-env/` venv
- Data: East Money push2 + akshare + Longbridge CLI (HK) + Tencent (fallback)
- Technical analysis: pandas-ta
- LLM: GPT-4.1-mini (sentiment batch) + Gemini 2.5 Pro (reports) with fallback chain
- Storage: JSONL + SQLite
- Output: HTML email (MVP), Web UI (post-validation)

## Status

- Design spec: `docs/superpowers/specs/2026-04-14-stock-screener-design.md`
- Phase: design complete, implementation planning next
