# Stock Screener

## Overview
A-share + HK stock screener: multi-layer funnel → multi-factor scoring → LLM-assisted research reports → backtest validation.

## Develop / Test
- **Run tests**: `python3 -m pytest tests/ -q`  (16 tests)
- Python 3; market/financial data fetchers (see `config/` + `requirements`).

## Architecture (funnel)
- **Universe**: A股 沪深300 + 中证500;港股 恒指 + 国企指数 (point-in-time).
- **Triggers**: Weekly (scheduled) + Event-driven (index drop / volume spike / VHSI / disclosure density); same-day events merge into one combined run.
- **Layer 1 (coarse filter)**: Weekly gate (MA20 trend up, close above MA20, volatility cap, liquidity) vs Event gate (drops the MA20-trend requirement, uses an MA60 floor).
- **Layer 2**: multi-factor scoring → candidate pool.
- **Layer 3**: LLM research reports for top candidates.

## Structure
- `scripts/` — pipeline + research-data updaters (`update_research_*.py`), `growth_gate_probe.py`, `phase0_spike.py`, `probe_eastmoney.py`.
- `config/` — universe + scoring config (**single source of truth — edit here, not in code**).
- `tests/` — pytest (16). `artifacts/`, `docs/`.

## Key Facts / Gotchas
- The `update_research_*.py` scripts write snapshots consumed by the docs site; keep their output schema stable.
- Data-source return types vary — coerce dates with `pd.to_datetime()` before use.
