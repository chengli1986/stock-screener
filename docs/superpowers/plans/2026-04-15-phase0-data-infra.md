# Phase 0: Data Infrastructure Validation — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Validate that ~900 A-share + HK stock universe collection, OHLCV history, and fundamental data fetching work reliably at scale — answering "is our data pipeline real?" before any screening logic.

**Architecture:** Three-layer spike: (1) Universe collector assembles ~800 A-share + ~100 HK index constituents from akshare API + static config, (2) Data fetcher validates OHLCV via Longbridge CLI and fundamentals via East Money push2 API per stock, (3) Coverage report summarizes field availability, API errors, and timing per market.

**Tech Stack:** Python 3.12 (`~/stock-env/`), akshare 1.18.25 (A-share universe), Longbridge CLI (OHLCV), East Money push2 REST API (fundamentals), pandas, requests

**Key research findings driving design choices:**
- `ak.index_stock_cons_csindex()` works perfectly for CSI 300/500 constituents (tested: 300+500 stocks, ~2s per call)
- No free API exists for HSI/HSCEI constituents — static JSON config required
- East Money `push2his` (history) gets rate-limited after ~20 rapid calls from this EC2 → Longbridge CLI is primary OHLCV source (~3.8s/stock, reliable)
- East Money `push2` (fundamentals) works fine for A-share; HK has data gaps (revenue growth, margins return 0.0 for HK stocks)
- Longbridge kline for 900 stocks ≈ 57 min sequential, ≈ 15 min with 4 concurrent workers
- PE/PB values from East Money are ×100 (e.g., 2131 → 21.31x)

---

## File Structure

```
data/
  __init__.py              # Package init
  universe.py              # Fetch A-share universe, load HK config, merge, save
  fetch.py                 # OHLCV (Longbridge) + fundamentals (East Money push2)
  report.py                # Coverage report from fetch results
config/
  hk_constituents.json     # Static HSI + HSCEI constituent list
tests/
  __init__.py
  test_universe.py         # Universe collection + merge tests
  test_fetch.py            # Data fetcher tests (mocked APIs)
  test_report.py           # Report generation tests
scripts/
  seed_hk_constituents.py  # One-time: populate HK config from Sina Finance
  run_phase0.py            # CLI entry point: universe → fetch → report
output/                    # gitignored: CSV, JSON results
  .gitkeep
```

---

### Task 1: Project Scaffolding

**Files:**
- Create: `data/__init__.py`, `tests/__init__.py`, `output/.gitkeep`, `requirements.txt`
- Modify: `.gitignore`

- [ ] **Step 1: Create directory structure**

```bash
cd ~/stock-screener
mkdir -p data tests scripts output config
```

- [ ] **Step 2: Create package init files**

`data/__init__.py` — empty file:
```python
```

`tests/__init__.py` — empty file:
```python
```

`output/.gitkeep` — empty file.

- [ ] **Step 3: Create requirements.txt**

```
akshare>=1.18
pandas>=2.0
requests>=2.31
```

Note: akshare, pandas, requests are already in `~/stock-env/`. This file documents the dependency for clarity.

- [ ] **Step 4: Update .gitignore**

Append to existing `.gitignore` (create if missing):

```
output/
!output/.gitkeep
__pycache__/
*.pyc
.pytest_cache/
```

- [ ] **Step 5: Verify and commit**

```bash
cd ~/stock-screener
ls -la data/ tests/ scripts/ output/ config/
git add data/__init__.py tests/__init__.py output/.gitkeep requirements.txt .gitignore
git commit -m "chore: phase 0 scaffolding — directories, deps, gitignore"
```

---

### Task 2: A-share Universe Collector

**Files:**
- Create: `data/universe.py`, `tests/test_universe.py`

- [ ] **Step 1: Write failing test for A-share universe parsing**

`tests/test_universe.py`:

```python
"""Tests for universe collection and merging."""

import pandas as pd
import pytest

from data.universe import parse_ashare_constituents


class TestParseAshareConstituents:
    """Test A-share constituent parsing from akshare DataFrame."""

    def test_parses_shanghai_stock(self):
        df = pd.DataFrame([{
            "日期": "2026-04-15",
            "指数代码": "000300",
            "指数名称": "沪深300",
            "成分券代码": "600519",
            "成分券名称": "贵州茅台",
            "交易所": "上海证券交易所",
        }])
        result = parse_ashare_constituents(df, "CSI300")
        assert len(result) == 1
        assert result[0]["symbol"] == "600519"
        assert result[0]["name"] == "贵州茅台"
        assert result[0]["market"] == "SH"
        assert result[0]["source_index"] == "CSI300"
        assert result[0]["currency"] == "CNY"
        assert result[0]["exchange"] == "SSE"

    def test_parses_shenzhen_stock(self):
        df = pd.DataFrame([{
            "日期": "2026-04-15",
            "指数代码": "000905",
            "指数名称": "中证500",
            "成分券代码": "000001",
            "成分券名称": "平安银行",
            "交易所": "深圳证券交易所",
        }])
        result = parse_ashare_constituents(df, "CSI500")
        assert len(result) == 1
        assert result[0]["market"] == "SZ"
        assert result[0]["exchange"] == "SZSE"

    def test_parses_multiple_stocks(self):
        df = pd.DataFrame([
            {"日期": "2026-04-15", "指数代码": "000300", "指数名称": "沪深300",
             "成分券代码": "600519", "成分券名称": "贵州茅台", "交易所": "上海证券交易所"},
            {"日期": "2026-04-15", "指数代码": "000300", "指数名称": "沪深300",
             "成分券代码": "000001", "成分券名称": "平安银行", "交易所": "深圳证券交易所"},
        ])
        result = parse_ashare_constituents(df, "CSI300")
        assert len(result) == 2
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd ~/stock-screener && ~/stock-env/bin/python3 -m pytest tests/test_universe.py -v
```

Expected: FAIL — `ImportError: cannot import name 'parse_ashare_constituents'`

- [ ] **Step 3: Implement parse_ashare_constituents**

`data/universe.py`:

```python
"""Stock universe collection for A-share and HK markets."""

from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"
OUTPUT_DIR = PROJECT_ROOT / "output"


def parse_ashare_constituents(df: pd.DataFrame, index_name: str) -> list[dict]:
    """Parse akshare constituent DataFrame into unified stock format."""
    stocks = []
    for _, row in df.iterrows():
        is_shanghai = "上海" in row["交易所"]
        stocks.append({
            "symbol": row["成分券代码"],
            "name": row["成分券名称"],
            "market": "SH" if is_shanghai else "SZ",
            "source_index": index_name,
            "currency": "CNY",
            "exchange": "SSE" if is_shanghai else "SZSE",
        })
    return stocks


def fetch_ashare_universe() -> list[dict]:
    """Fetch CSI 300 + CSI 500 constituents from akshare."""
    import akshare as ak

    stocks = []
    for code, name in [("000300", "CSI300"), ("000905", "CSI500")]:
        logger.info("Fetching %s (%s) constituents...", name, code)
        df = ak.index_stock_cons_csindex(symbol=code)
        batch = parse_ashare_constituents(df, name)
        logger.info("  %s: %d stocks", name, len(batch))
        stocks.extend(batch)

    logger.info("A-share universe total: %d stocks", len(stocks))
    return stocks
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd ~/stock-screener && ~/stock-env/bin/python3 -m pytest tests/test_universe.py -v
```

Expected: 3 passed

- [ ] **Step 5: Write integration test for live API**

Add to `tests/test_universe.py`:

```python
@pytest.mark.integration
class TestFetchAshareUniverse:
    """Integration test — hits live akshare API."""

    def test_fetches_real_constituents(self):
        from data.universe import fetch_ashare_universe

        stocks = fetch_ashare_universe()
        # CSI300 (300) + CSI500 (500) = 800, no overlap by design
        assert len(stocks) == 800
        # Check required fields exist
        for s in stocks[:5]:
            assert s["symbol"]
            assert s["name"]
            assert s["market"] in ("SH", "SZ")
```

- [ ] **Step 6: Run integration test**

```bash
cd ~/stock-screener && ~/stock-env/bin/python3 -m pytest tests/test_universe.py -v -m integration
```

Expected: 1 passed (fetches 800 stocks)

- [ ] **Step 7: Commit**

```bash
cd ~/stock-screener
git add data/universe.py tests/test_universe.py
git commit -m "feat: A-share universe collector via akshare CSI index API"
```

---

### Task 3: HK Universe from Static Config

**Files:**
- Create: `config/hk_constituents.json`, `scripts/seed_hk_constituents.py`
- Modify: `data/universe.py`, `tests/test_universe.py`

- [ ] **Step 1: Write failing test for HK config loader**

Add to `tests/test_universe.py`:

```python
import json
import tempfile
from pathlib import Path

from data.universe import load_hk_universe


class TestLoadHkUniverse:
    """Test HK constituent loading from JSON config."""

    def test_loads_single_index(self, tmp_path):
        config = {
            "last_updated": "2026-04-15",
            "indices": [{
                "code": "HSI",
                "name": "HSI",
                "members": [
                    {"code": "00700", "name_zh": "腾讯控股", "name_en": "Tencent"},
                    {"code": "00005", "name_zh": "汇丰控股", "name_en": "HSBC"},
                ],
            }],
        }
        config_path = tmp_path / "hk_constituents.json"
        config_path.write_text(json.dumps(config, ensure_ascii=False))

        result = load_hk_universe(config_path)
        assert len(result) == 2
        assert result[0]["symbol"] == "00700"
        assert result[0]["name"] == "腾讯控股"
        assert result[0]["market"] == "HK"
        assert result[0]["source_index"] == "HSI"
        assert result[0]["currency"] == "HKD"
        assert result[0]["exchange"] == "HKEX"

    def test_deduplicates_across_indices(self, tmp_path):
        config = {
            "last_updated": "2026-04-15",
            "indices": [
                {"code": "HSI", "name": "HSI", "members": [
                    {"code": "00700", "name_zh": "腾讯控股", "name_en": "Tencent"},
                    {"code": "00005", "name_zh": "汇丰控股", "name_en": "HSBC"},
                ]},
                {"code": "HSCEI", "name": "HSCEI", "members": [
                    {"code": "00700", "name_zh": "腾讯控股", "name_en": "Tencent"},
                    {"code": "00939", "name_zh": "建设银行", "name_en": "CCB"},
                ]},
            ],
        }
        config_path = tmp_path / "hk_constituents.json"
        config_path.write_text(json.dumps(config, ensure_ascii=False))

        result = load_hk_universe(config_path)
        # 00700 appears in both, should be deduped; first occurrence (HSI) wins
        assert len(result) == 3
        symbols = [s["symbol"] for s in result]
        assert symbols.count("00700") == 1
        assert result[0]["source_index"] == "HSI"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd ~/stock-screener && ~/stock-env/bin/python3 -m pytest tests/test_universe.py::TestLoadHkUniverse -v
```

Expected: FAIL — `ImportError: cannot import name 'load_hk_universe'`

- [ ] **Step 3: Implement load_hk_universe**

Add to `data/universe.py`:

```python
def load_hk_universe(config_path: Path | None = None) -> list[dict]:
    """Load HK index constituents from static JSON config, deduplicating across indices."""
    path = config_path or CONFIG_DIR / "hk_constituents.json"
    with open(path) as f:
        config = json.load(f)

    seen: set[str] = set()
    stocks: list[dict] = []

    for index_entry in config["indices"]:
        index_name = index_entry["name"]
        for member in index_entry["members"]:
            code = member["code"]
            if code in seen:
                continue
            seen.add(code)
            stocks.append({
                "symbol": code,
                "name": member["name_zh"],
                "market": "HK",
                "source_index": index_name,
                "currency": "HKD",
                "exchange": "HKEX",
            })

    logger.info("HK universe: %d stocks (config updated %s)", len(stocks), config["last_updated"])
    return stocks
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd ~/stock-screener && ~/stock-env/bin/python3 -m pytest tests/test_universe.py -v
```

Expected: 5 passed (3 A-share + 2 HK)

- [ ] **Step 5: Create HK constituent seed script**

`scripts/seed_hk_constituents.py`:

```python
#!/usr/bin/env python3
"""Seed HK index constituent list from Sina Finance + known member codes.

Usage: ~/stock-env/bin/python3 scripts/seed_hk_constituents.py
Output: config/hk_constituents.json

HSI/HSCEI compositions change quarterly. Re-run after each rebalance
(March/June/September/December) or when Hang Seng announces changes.
"""

import json
import logging
import time
from pathlib import Path

import requests

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger(__name__)

CONFIG_DIR = Path(__file__).parent.parent / "config"

# HSI members as of 2026-Q1 (82 stocks)
# Source: Hang Seng Indexes Company quarterly review
HSI_CODES = [
    "00001", "00002", "00003", "00005", "00006", "00011", "00012", "00016",
    "00017", "00027", "00066", "00175", "00241", "00267", "00288", "00291",
    "00316", "00322", "00386", "00388", "00669", "00688", "00700", "00762",
    "00823", "00857", "00868", "00881", "00883", "00939", "00941", "00960",
    "00968", "00981", "01038", "01044", "01093", "01109", "01113", "01177",
    "01211", "01299", "01378", "01398", "01810", "01876", "01928", "01997",
    "02007", "02018", "02020", "02269", "02313", "02318", "02319", "02331",
    "02382", "02388", "02628", "02688", "02899", "03328", "03690", "03968",
    "03988", "06098", "06862", "09618", "09626", "09633", "09888", "09961",
    "09988", "09999",
]

# HSCEI members as of 2026-Q1 (50 stocks)
# Source: Hang Seng Indexes Company quarterly review
HSCEI_CODES = [
    "00175", "00241", "00267", "00285", "00386", "00688", "00700", "00762",
    "00857", "00883", "00914", "00939", "00941", "00981", "01024", "01093",
    "01109", "01177", "01211", "01268", "01299", "01378", "01398", "01658",
    "01772", "01810", "01833", "01876", "02007", "02015", "02018", "02020",
    "02269", "02313", "02318", "02319", "02328", "02331", "02382", "02628",
    "02688", "02899", "03328", "03690", "03968", "03988", "06098", "06862",
    "09618", "09888", "09988", "09999",
]


def fetch_hk_names(codes: list[str]) -> dict[str, str]:
    """Fetch Chinese names for HK stocks from Sina Finance API."""
    names: dict[str, str] = {}
    headers = {"User-Agent": "Mozilla/5.0"}
    url = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHKStockData"

    page = 1
    while True:
        params = {"page": page, "num": 40, "sort": "symbol", "asc": 1, "node": "qbgg_hk"}
        try:
            r = requests.get(url, params=params, headers=headers, timeout=15)
            if r.status_code != 200 or not r.text or r.text.strip() == "null":
                break
            data = r.json()
            if not data:
                break
            for item in data:
                code = item.get("symbol", "").zfill(5)
                name = item.get("name", "")
                if code in codes:
                    names[code] = name
            log.info("  Page %d: fetched %d stocks, matched %d so far", page, len(data), len(names))
            if len(names) >= len(codes):
                break
            page += 1
            time.sleep(0.5)
        except Exception as e:
            log.warning("  Page %d failed: %s", page, e)
            break

    return names


def main():
    all_codes = set(HSI_CODES) | set(HSCEI_CODES)
    log.info("Fetching names for %d unique HK stocks from Sina Finance...", len(all_codes))
    names = fetch_hk_names(all_codes)
    log.info("Resolved %d / %d names", len(names), len(all_codes))

    # For any codes without names, use placeholder
    for code in all_codes:
        if code not in names:
            names[code] = f"HK{code}"
            log.warning("  No name found for %s, using placeholder", code)

    config = {
        "last_updated": "2026-04-15",
        "source": "Hang Seng Indexes Company quarterly review, names from Sina Finance",
        "note": "Rebalance quarterly (Mar/Jun/Sep/Dec). Re-run this script after each change.",
        "indices": [
            {
                "code": "HSI",
                "name": "HSI",
                "description": "恒生指数",
                "members": [{"code": c, "name_zh": names[c], "name_en": ""} for c in sorted(HSI_CODES)],
            },
            {
                "code": "HSCEI",
                "name": "HSCEI",
                "description": "恒生中国企业指数",
                "members": [{"code": c, "name_zh": names[c], "name_en": ""} for c in sorted(HSCEI_CODES)],
            },
        ],
    }

    out_path = CONFIG_DIR / "hk_constituents.json"
    CONFIG_DIR.mkdir(exist_ok=True)
    out_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n")
    log.info("Saved to %s", out_path)


if __name__ == "__main__":
    main()
```

- [ ] **Step 6: Run seed script to populate config**

```bash
cd ~/stock-screener && ~/stock-env/bin/python3 scripts/seed_hk_constituents.py
```

Verify: `cat config/hk_constituents.json | python3 -c "import json,sys; d=json.load(sys.stdin); print(f'HSI: {len(d[\"indices\"][0][\"members\"])}, HSCEI: {len(d[\"indices\"][1][\"members\"])}')"` should show HSI: 74, HSCEI: 52.

If Sina API is unavailable, manually create a minimal `config/hk_constituents.json` with the codes from the script and placeholder names — data availability testing does not depend on Chinese names.

- [ ] **Step 7: Commit**

```bash
cd ~/stock-screener
git add data/universe.py tests/test_universe.py scripts/seed_hk_constituents.py config/hk_constituents.json
git commit -m "feat: HK universe from static config with Sina Finance seed script"
```

---

### Task 4: Universe Merge & Save

**Files:**
- Modify: `data/universe.py`, `tests/test_universe.py`

- [ ] **Step 1: Write failing tests for merge and save**

Add to `tests/test_universe.py`:

```python
from data.universe import merge_universe, save_universe


class TestMergeUniverse:
    """Test universe merging and deduplication."""

    def test_merges_two_lists(self):
        ashare = [
            {"symbol": "600519", "name": "茅台", "market": "SH", "source_index": "CSI300",
             "currency": "CNY", "exchange": "SSE"},
        ]
        hk = [
            {"symbol": "00700", "name": "腾讯", "market": "HK", "source_index": "HSI",
             "currency": "HKD", "exchange": "HKEX"},
        ]
        result = merge_universe(ashare, hk)
        assert len(result) == 2
        assert result[0]["market"] == "SH"
        assert result[1]["market"] == "HK"

    def test_no_cross_market_dedup(self):
        """Same code in different markets should both be kept."""
        ashare = [
            {"symbol": "00001", "name": "平安银行", "market": "SZ", "source_index": "CSI300",
             "currency": "CNY", "exchange": "SZSE"},
        ]
        hk = [
            {"symbol": "00001", "name": "长和", "market": "HK", "source_index": "HSI",
             "currency": "HKD", "exchange": "HKEX"},
        ]
        result = merge_universe(ashare, hk)
        assert len(result) == 2


class TestSaveUniverse:
    """Test universe CSV persistence."""

    def test_saves_csv_with_all_fields(self, tmp_path):
        stocks = [
            {"symbol": "600519", "name": "茅台", "market": "SH", "source_index": "CSI300",
             "currency": "CNY", "exchange": "SSE"},
            {"symbol": "00700", "name": "腾讯", "market": "HK", "source_index": "HSI",
             "currency": "HKD", "exchange": "HKEX"},
        ]
        path = save_universe(stocks, tmp_path)
        assert path.exists()

        df = pd.read_csv(path)
        assert len(df) == 2
        assert list(df.columns) == ["symbol", "name", "market", "source_index", "currency", "exchange"]

    def test_creates_latest_symlink(self, tmp_path):
        stocks = [{"symbol": "600519", "name": "茅台", "market": "SH",
                    "source_index": "CSI300", "currency": "CNY", "exchange": "SSE"}]
        save_universe(stocks, tmp_path)
        latest = tmp_path / "latest.csv"
        assert latest.exists()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd ~/stock-screener && ~/stock-env/bin/python3 -m pytest tests/test_universe.py::TestMergeUniverse tests/test_universe.py::TestSaveUniverse -v
```

Expected: FAIL — `ImportError`

- [ ] **Step 3: Implement merge_universe and save_universe**

Add to `data/universe.py`:

```python
def merge_universe(ashare: list[dict], hk: list[dict]) -> list[dict]:
    """Merge A-share and HK stocks, deduplicating by market:symbol key."""
    seen: set[str] = set()
    merged: list[dict] = []

    for stock in ashare + hk:
        key = f"{stock['market']}:{stock['symbol']}"
        if key not in seen:
            seen.add(key)
            merged.append(stock)

    dupes = len(ashare) + len(hk) - len(merged)
    logger.info("Merged universe: %d stocks (%d A-share + %d HK, %d dupes removed)",
                len(merged), len(ashare), len(hk), dupes)
    return merged


def save_universe(stocks: list[dict], output_dir: Path | None = None) -> Path:
    """Save universe to dated CSV + latest.csv."""
    out = output_dir or OUTPUT_DIR / "universe"
    out.mkdir(parents=True, exist_ok=True)

    df = pd.DataFrame(stocks)
    dated_path = out / f"{date.today().isoformat()}.csv"
    latest_path = out / "latest.csv"

    df.to_csv(dated_path, index=False)
    df.to_csv(latest_path, index=False)

    logger.info("Universe saved: %s (%d stocks)", dated_path, len(stocks))
    return dated_path
```

- [ ] **Step 4: Run all tests**

```bash
cd ~/stock-screener && ~/stock-env/bin/python3 -m pytest tests/test_universe.py -v
```

Expected: 9 passed (3 parse + 2 HK load + 2 merge + 2 save)

- [ ] **Step 5: Commit**

```bash
cd ~/stock-screener
git add data/universe.py tests/test_universe.py
git commit -m "feat: universe merge and CSV persistence"
```

---

### Task 5: Data Fetcher — OHLCV + Fundamentals

**Files:**
- Create: `data/fetch.py`, `tests/test_fetch.py`

- [ ] **Step 1: Write failing test for East Money secid conversion**

`tests/test_fetch.py`:

```python
"""Tests for data fetching — OHLCV and fundamentals."""

import pytest

from data.fetch import to_em_secid, parse_em_fundamentals


class TestToEmSecid:
    def test_shanghai(self):
        assert to_em_secid("600519", "SH") == "1.600519"

    def test_shenzhen(self):
        assert to_em_secid("000001", "SZ") == "0.000001"

    def test_hk(self):
        assert to_em_secid("00700", "HK") == "116.00700"

    def test_unknown_market_raises(self):
        with pytest.raises(ValueError, match="Unknown market"):
            to_em_secid("AAPL", "US")


class TestParseEmFundamentals:
    def test_parses_normal_ashare(self):
        raw = {
            "f162": 510, "f163": 510, "f167": 48,
            "f173": 9.15, "f184": -10.4, "f185": -4.21,
            "f186": 0.0, "f187": 32.43, "f116": 215000000000,
        }
        result = parse_em_fundamentals(raw)
        assert result["pe_static"] == pytest.approx(5.10)
        assert result["pe_dynamic"] == pytest.approx(5.10)
        assert result["pb"] == pytest.approx(0.48)
        assert result["roe"] == pytest.approx(9.15)
        assert result["revenue_growth"] == pytest.approx(-10.4)
        assert result["net_profit_growth"] == pytest.approx(-4.21)
        assert result["gross_margin"] == pytest.approx(0.0)
        assert result["net_margin"] == pytest.approx(32.43)

    def test_handles_missing_fields(self):
        raw = {"f162": "-", "f163": None, "f167": 48}
        result = parse_em_fundamentals(raw)
        assert result["pe_static"] is None
        assert result["pe_dynamic"] is None
        assert result["pb"] == pytest.approx(0.48)
        assert result["roe"] is None

    def test_handles_negative_pe(self):
        """Companies with losses have negative PE."""
        raw = {"f162": -53, "f163": -53, "f167": 80, "f173": -55.42}
        result = parse_em_fundamentals(raw)
        assert result["pe_static"] == pytest.approx(-0.53)
        assert result["roe"] == pytest.approx(-55.42)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd ~/stock-screener && ~/stock-env/bin/python3 -m pytest tests/test_fetch.py -v
```

Expected: FAIL — `ImportError`

- [ ] **Step 3: Implement to_em_secid and parse_em_fundamentals**

`data/fetch.py`:

```python
"""Data fetching: OHLCV via Longbridge CLI, fundamentals via East Money push2."""

from __future__ import annotations

import json
import logging
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import requests

logger = logging.getLogger(__name__)

EM_URL = "https://push2.eastmoney.com/api/qt/stock/get"
EM_FIELDS = "f57,f58,f116,f162,f163,f167,f173,f183,f184,f185,f186,f187"
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"}


def to_em_secid(symbol: str, market: str) -> str:
    """Convert stock symbol to East Money secid format."""
    if market == "SH":
        return f"1.{symbol}"
    if market == "SZ":
        return f"0.{symbol}"
    if market == "HK":
        return f"116.{symbol}"
    raise ValueError(f"Unknown market: {market}")


def parse_em_fundamentals(raw: dict[str, Any]) -> dict[str, float | None]:
    """Parse raw East Money API response into normalized fundamental data."""
    return {
        "pe_static": _scale(raw.get("f162"), 100),
        "pe_dynamic": _scale(raw.get("f163"), 100),
        "pb": _scale(raw.get("f167"), 100),
        "roe": _to_float(raw.get("f173")),
        "revenue_growth": _to_float(raw.get("f184")),
        "net_profit_growth": _to_float(raw.get("f185")),
        "gross_margin": _to_float(raw.get("f186")),
        "net_margin": _to_float(raw.get("f187")),
        "market_cap": _to_float(raw.get("f116")),
    }


def _scale(value: Any, divisor: float) -> float | None:
    """Convert scaled integer to float, handling missing values."""
    if value is None or value == "-" or value == "":
        return None
    try:
        return float(value) / divisor
    except (ValueError, TypeError):
        return None


def _to_float(value: Any) -> float | None:
    """Convert to float, handling missing values."""
    if value is None or value == "-" or value == "":
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd ~/stock-screener && ~/stock-env/bin/python3 -m pytest tests/test_fetch.py -v
```

Expected: 7 passed

- [ ] **Step 5: Write failing test for OHLCV fetcher**

Add to `tests/test_fetch.py`:

```python
from unittest.mock import patch, MagicMock

from data.fetch import fetch_ohlcv_single


class TestFetchOhlcvSingle:
    def test_success_returns_row_count(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps([
            {"close": "100", "high": "101", "low": "99", "open": "100",
             "time": "2026-04-15", "turnover": "1000000", "volume": "10000"},
        ] * 60)

        with patch("subprocess.run", return_value=mock_result):
            result = fetch_ohlcv_single("600519", "SH")
            assert result["rows"] == 60
            assert result["error"] is None

    def test_cli_failure_returns_error(self):
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "connection timeout"
        mock_result.stdout = ""

        with patch("subprocess.run", return_value=mock_result):
            result = fetch_ohlcv_single("600519", "SH")
            assert result["rows"] == 0
            assert result["error"] is not None

    def test_timeout_returns_error(self):
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("cmd", 15)):
            result = fetch_ohlcv_single("600519", "SH")
            assert result["rows"] == 0
            assert "timeout" in result["error"].lower()


import subprocess as _subprocess_module  # for TimeoutExpired in test
```

- [ ] **Step 6: Implement fetch_ohlcv_single**

Add to `data/fetch.py`:

```python
def _to_lb_symbol(symbol: str, market: str) -> str:
    """Convert to Longbridge symbol format."""
    if market == "SH":
        return f"{symbol}.SH"
    if market == "SZ":
        return f"{symbol}.SZ"
    if market == "HK":
        # Longbridge uses no leading zeros for HK: 700.HK not 00700.HK
        return f"{symbol.lstrip('0')}.HK"
    raise ValueError(f"Unknown market: {market}")


def fetch_ohlcv_single(symbol: str, market: str, count: int = 60) -> dict:
    """Fetch OHLCV via Longbridge CLI for a single stock.

    Returns: {"rows": int, "error": str | None}
    """
    lb_symbol = _to_lb_symbol(symbol, market)
    cmd = ["longbridge", "kline", lb_symbol, "--period", "day",
           "--count", str(count), "--format", "json"]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if result.returncode != 0:
            return {"rows": 0, "error": result.stderr.strip()[:200]}
        data = json.loads(result.stdout)
        return {"rows": len(data), "error": None}
    except subprocess.TimeoutExpired:
        return {"rows": 0, "error": "Timeout (15s)"}
    except json.JSONDecodeError as e:
        return {"rows": 0, "error": f"JSON parse error: {e}"}
    except Exception as e:
        return {"rows": 0, "error": str(e)[:200]}
```

- [ ] **Step 7: Run tests**

```bash
cd ~/stock-screener && ~/stock-env/bin/python3 -m pytest tests/test_fetch.py -v
```

Expected: 10 passed

- [ ] **Step 8: Implement fetch_fundamentals_single and batch orchestrator**

Add to `data/fetch.py`:

```python
def fetch_fundamentals_single(symbol: str, market: str, session: requests.Session | None = None) -> dict:
    """Fetch fundamentals from East Money push2 API for a single stock.

    Returns: {"fields": {...}, "error": str | None}
    """
    try:
        secid = to_em_secid(symbol, market)
        s = session or requests
        r = s.get(EM_URL, params={"secid": secid, "fields": EM_FIELDS},
                  headers=HEADERS, timeout=10)
        data = r.json().get("data")
        if not data:
            return {"fields": {}, "error": "No data in response"}
        return {"fields": parse_em_fundamentals(data), "error": None}
    except Exception as e:
        return {"fields": {}, "error": str(e)[:200]}


def check_all_stocks(
    stocks: list[dict],
    ohlcv_workers: int = 3,
    fundamental_delay: float = 0.15,
    progress_every: int = 50,
) -> list[dict]:
    """Run full data availability check on all stocks.

    OHLCV: parallel via Longbridge CLI (ohlcv_workers threads).
    Fundamentals: sequential via East Money (single-threaded, rate-limited).

    Returns list of dicts, one per stock, with ohlcv/fundamentals/timing.
    """
    total = len(stocks)
    results: list[dict] = [None] * total  # type: ignore[list-item]

    # Phase 1: OHLCV via Longbridge (parallel)
    logger.info("Phase 1/2: Fetching OHLCV for %d stocks (%d workers)...", total, ohlcv_workers)
    t0 = time.time()

    def _ohlcv_task(idx: int, stock: dict) -> tuple[int, dict]:
        ohlcv = fetch_ohlcv_single(stock["symbol"], stock["market"])
        return idx, ohlcv

    with ThreadPoolExecutor(max_workers=ohlcv_workers) as pool:
        futures = {pool.submit(_ohlcv_task, i, s): i for i, s in enumerate(stocks)}
        done_count = 0
        for future in as_completed(futures):
            idx, ohlcv = future.result()
            results[idx] = {**stocks[idx], "ohlcv": ohlcv}
            done_count += 1
            if done_count % progress_every == 0:
                logger.info("  OHLCV progress: %d/%d", done_count, total)

    ohlcv_time = time.time() - t0
    logger.info("  OHLCV done in %.0fs", ohlcv_time)

    # Phase 2: Fundamentals via East Money (sequential, rate-limited)
    logger.info("Phase 2/2: Fetching fundamentals for %d stocks...", total)
    t0 = time.time()
    session = requests.Session()
    session.headers.update(HEADERS)

    for i, stock in enumerate(stocks):
        fund = fetch_fundamentals_single(stock["symbol"], stock["market"], session)
        results[i]["fundamentals"] = fund
        if (i + 1) % progress_every == 0:
            logger.info("  Fundamentals progress: %d/%d", i + 1, total)
        time.sleep(fundamental_delay)

    fund_time = time.time() - t0
    logger.info("  Fundamentals done in %.0fs", fund_time)
    logger.info("Total fetch time: %.0fs (OHLCV %.0fs + Fundamentals %.0fs)",
                ohlcv_time + fund_time, ohlcv_time, fund_time)

    return results
```

- [ ] **Step 9: Write integration test for single-stock fetch**

Add to `tests/test_fetch.py`:

```python
@pytest.mark.integration
class TestLiveFetch:
    def test_ohlcv_ashare(self):
        from data.fetch import fetch_ohlcv_single
        result = fetch_ohlcv_single("600519", "SH")
        assert result["rows"] >= 30, f"Expected >=30 rows, got {result}"
        assert result["error"] is None

    def test_ohlcv_hk(self):
        from data.fetch import fetch_ohlcv_single
        result = fetch_ohlcv_single("00700", "HK")
        assert result["rows"] >= 30, f"Expected >=30 rows, got {result}"
        assert result["error"] is None

    def test_fundamentals_ashare(self):
        from data.fetch import fetch_fundamentals_single
        result = fetch_fundamentals_single("600519", "SH")
        assert result["error"] is None
        fields = result["fields"]
        assert fields["pe_dynamic"] is not None and fields["pe_dynamic"] > 0

    def test_fundamentals_hk(self):
        from data.fetch import fetch_fundamentals_single
        result = fetch_fundamentals_single("00700", "HK")
        assert result["error"] is None
        fields = result["fields"]
        assert fields["pe_dynamic"] is not None and fields["pe_dynamic"] > 0
```

- [ ] **Step 10: Run integration tests**

```bash
cd ~/stock-screener && ~/stock-env/bin/python3 -m pytest tests/test_fetch.py -v -m integration
```

Expected: 4 passed

- [ ] **Step 11: Commit**

```bash
cd ~/stock-screener
git add data/fetch.py tests/test_fetch.py
git commit -m "feat: OHLCV fetcher (Longbridge) + fundamentals fetcher (East Money push2)"
```

---

### Task 6: Coverage Report Generator

**Files:**
- Create: `data/report.py`, `tests/test_report.py`

- [ ] **Step 1: Write failing test for report generation**

`tests/test_report.py`:

```python
"""Tests for coverage report generation."""

from data.report import generate_coverage_report


class TestGenerateCoverageReport:
    def _make_result(self, symbol, market, ohlcv_rows, ohlcv_error, fields, fund_error):
        return {
            "symbol": symbol, "name": f"Stock_{symbol}", "market": market,
            "source_index": "TEST", "currency": "CNY", "exchange": "TEST",
            "ohlcv": {"rows": ohlcv_rows, "error": ohlcv_error},
            "fundamentals": {"fields": fields, "error": fund_error},
        }

    def test_counts_total_by_market(self):
        results = [
            self._make_result("001", "SH", 60, None, {"pe_dynamic": 10.0}, None),
            self._make_result("002", "SZ", 60, None, {"pe_dynamic": 5.0}, None),
            self._make_result("700", "HK", 60, None, {"pe_dynamic": 20.0}, None),
        ]
        report = generate_coverage_report(results)
        assert report["total"] == 3
        assert report["by_market"]["SH"]["total"] == 1
        assert report["by_market"]["SZ"]["total"] == 1
        assert report["by_market"]["HK"]["total"] == 1

    def test_counts_ohlcv_success(self):
        results = [
            self._make_result("001", "SH", 60, None, {}, None),
            self._make_result("002", "SH", 0, "timeout", {}, None),
            self._make_result("003", "SH", 30, None, {}, None),
        ]
        report = generate_coverage_report(results)
        assert report["by_market"]["SH"]["ohlcv_success"] == 2
        assert report["by_market"]["SH"]["ohlcv_fail"] == 1

    def test_counts_fundamental_field_coverage(self):
        results = [
            self._make_result("001", "SH", 60, None,
                              {"pe_dynamic": 10.0, "roe": 15.0, "revenue_growth": None}, None),
            self._make_result("002", "SH", 60, None,
                              {"pe_dynamic": 5.0, "roe": None, "revenue_growth": 8.0}, None),
        ]
        report = generate_coverage_report(results)
        fields = report["by_market"]["SH"]["fundamental_fields"]
        assert fields["pe_dynamic"]["available"] == 2
        assert fields["roe"]["available"] == 1
        assert fields["revenue_growth"]["available"] == 1

    def test_tracks_errors(self):
        results = [
            self._make_result("001", "SH", 0, "timeout", {}, "connection error"),
        ]
        report = generate_coverage_report(results)
        assert len(report["ohlcv_errors"]) == 1
        assert len(report["fundamental_errors"]) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd ~/stock-screener && ~/stock-env/bin/python3 -m pytest tests/test_report.py -v
```

Expected: FAIL — `ImportError`

- [ ] **Step 3: Implement generate_coverage_report**

`data/report.py`:

```python
"""Coverage report generation from data availability check results."""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

FUNDAMENTAL_FIELDS = [
    "pe_static", "pe_dynamic", "pb", "roe",
    "revenue_growth", "net_profit_growth", "gross_margin", "net_margin", "market_cap",
]


def generate_coverage_report(results: list[dict]) -> dict:
    """Generate coverage summary from fetch results."""
    by_market: dict[str, dict] = {}
    ohlcv_errors: list[dict] = []
    fundamental_errors: list[dict] = []

    # Group by market
    grouped: dict[str, list[dict]] = defaultdict(list)
    for r in results:
        grouped[r["market"]].append(r)

    for market, stocks in grouped.items():
        total = len(stocks)
        ohlcv_ok = sum(1 for s in stocks if s["ohlcv"]["rows"] > 0)
        ohlcv_fail = total - ohlcv_ok

        # Fundamental field coverage
        field_stats: dict[str, dict] = {}
        for field_name in FUNDAMENTAL_FIELDS:
            available = 0
            zero_count = 0
            for s in stocks:
                fields = s.get("fundamentals", {}).get("fields", {})
                val = fields.get(field_name)
                if val is not None:
                    available += 1
                    if val == 0.0:
                        zero_count += 1
            field_stats[field_name] = {
                "available": available,
                "missing": total - available,
                "zero_count": zero_count,
                "coverage_pct": round(available / total * 100, 1) if total > 0 else 0,
            }

        by_market[market] = {
            "total": total,
            "ohlcv_success": ohlcv_ok,
            "ohlcv_fail": ohlcv_fail,
            "ohlcv_coverage_pct": round(ohlcv_ok / total * 100, 1) if total > 0 else 0,
            "fundamental_fields": field_stats,
        }

    # Collect errors
    for r in results:
        if r["ohlcv"]["error"]:
            ohlcv_errors.append({
                "symbol": r["symbol"], "market": r["market"],
                "name": r["name"], "error": r["ohlcv"]["error"],
            })
        fund = r.get("fundamentals", {})
        if fund.get("error"):
            fundamental_errors.append({
                "symbol": r["symbol"], "market": r["market"],
                "name": r["name"], "error": fund["error"],
            })

    return {
        "generated_at": datetime.now().isoformat(),
        "total": len(results),
        "by_market": by_market,
        "ohlcv_errors": ohlcv_errors,
        "fundamental_errors": fundamental_errors,
    }


def format_report_text(report: dict) -> str:
    """Format coverage report as human-readable text."""
    lines = []
    lines.append("=" * 70)
    lines.append("STOCK SCREENER — Phase 0 Data Coverage Report")
    lines.append(f"Generated: {report['generated_at']}")
    lines.append(f"Total stocks checked: {report['total']}")
    lines.append("=" * 70)

    for market, data in sorted(report["by_market"].items()):
        lines.append("")
        lines.append(f"--- {market} ({data['total']} stocks) ---")
        lines.append(f"  OHLCV: {data['ohlcv_success']}/{data['total']} success "
                      f"({data['ohlcv_coverage_pct']}%), {data['ohlcv_fail']} failed")
        lines.append("  Fundamentals:")
        for field_name, stats in data["fundamental_fields"].items():
            bar = "#" * int(stats["coverage_pct"] / 5) + "." * (20 - int(stats["coverage_pct"] / 5))
            zero_note = f" ({stats['zero_count']} zeros)" if stats["zero_count"] > 0 else ""
            lines.append(f"    {field_name:20s} [{bar}] {stats['coverage_pct']:5.1f}%"
                          f" ({stats['available']}/{stats['available'] + stats['missing']}){zero_note}")

    if report["ohlcv_errors"]:
        lines.append("")
        lines.append(f"--- OHLCV Errors (first 20 of {len(report['ohlcv_errors'])}) ---")
        for err in report["ohlcv_errors"][:20]:
            lines.append(f"  {err['market']}:{err['symbol']} {err['name']}: {err['error'][:80]}")

    if report["fundamental_errors"]:
        lines.append("")
        lines.append(f"--- Fundamental Errors (first 20 of {len(report['fundamental_errors'])}) ---")
        for err in report["fundamental_errors"][:20]:
            lines.append(f"  {err['market']}:{err['symbol']} {err['name']}: {err['error'][:80]}")

    lines.append("")
    lines.append("=" * 70)
    return "\n".join(lines)


def save_report(report: dict, output_dir: Path) -> tuple[Path, Path]:
    """Save report as JSON + text."""
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "coverage-report.json"
    text_path = output_dir / "coverage-report.txt"

    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    text_path.write_text(format_report_text(report) + "\n")

    logger.info("Report saved: %s, %s", json_path, text_path)
    return json_path, text_path
```

- [ ] **Step 4: Run tests**

```bash
cd ~/stock-screener && ~/stock-env/bin/python3 -m pytest tests/test_report.py -v
```

Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
cd ~/stock-screener
git add data/report.py tests/test_report.py
git commit -m "feat: coverage report generator with text and JSON output"
```

---

### Task 7: CLI Runner + Full Spike Execution

**Files:**
- Create: `scripts/run_phase0.py`

- [ ] **Step 1: Create CLI entry point**

`scripts/run_phase0.py`:

```python
#!/usr/bin/env python3
"""Phase 0: Data Infrastructure Validation.

Fetches the full stock universe, checks OHLCV and fundamental data availability
for every stock, and generates a coverage report.

Usage:
  ~/stock-env/bin/python3 scripts/run_phase0.py              # full run (~25 min)
  ~/stock-env/bin/python3 scripts/run_phase0.py --sample 10   # quick dry run
"""

import argparse
import logging
import random
import sys
import time
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from data.universe import fetch_ashare_universe, load_hk_universe, merge_universe, save_universe
from data.fetch import check_all_stocks
from data.report import generate_coverage_report, format_report_text, save_report

OUTPUT_DIR = Path(__file__).parent.parent / "output"


def main():
    parser = argparse.ArgumentParser(description="Phase 0: Data Infrastructure Validation")
    parser.add_argument("--sample", type=int, default=0,
                        help="Only check N random stocks (0 = all)")
    parser.add_argument("--ohlcv-workers", type=int, default=3,
                        help="Parallel OHLCV fetch workers (default: 3)")
    parser.add_argument("--skip-ohlcv", action="store_true",
                        help="Skip OHLCV fetch (fundamentals only)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-5s %(message)s",
        datefmt="%H:%M:%S",
    )
    log = logging.getLogger(__name__)

    t_start = time.time()

    # Step 1: Collect universe
    log.info("=" * 60)
    log.info("Step 1: Collecting universe...")
    ashare = fetch_ashare_universe()
    hk = load_hk_universe()
    all_stocks = merge_universe(ashare, hk)
    universe_path = save_universe(all_stocks)
    log.info("Universe: %d stocks saved to %s", len(all_stocks), universe_path)

    # Step 2: Sample if requested
    if args.sample > 0 and args.sample < len(all_stocks):
        log.info("Sampling %d stocks from %d...", args.sample, len(all_stocks))
        # Ensure mix of markets
        ashare_stocks = [s for s in all_stocks if s["market"] in ("SH", "SZ")]
        hk_stocks = [s for s in all_stocks if s["market"] == "HK"]
        n_hk = max(2, args.sample // 8)  # ~12% HK
        n_ashare = args.sample - n_hk
        sample = (random.sample(ashare_stocks, min(n_ashare, len(ashare_stocks)))
                  + random.sample(hk_stocks, min(n_hk, len(hk_stocks))))
        all_stocks = sample
        log.info("Sample: %d A-share + %d HK = %d total",
                 n_ashare, n_hk, len(all_stocks))

    # Step 3: Check data availability
    log.info("=" * 60)
    log.info("Step 2: Checking data availability for %d stocks...", len(all_stocks))
    results = check_all_stocks(
        all_stocks,
        ohlcv_workers=args.ohlcv_workers,
    )

    # Step 4: Generate report
    log.info("=" * 60)
    log.info("Step 3: Generating coverage report...")
    report = generate_coverage_report(results)
    json_path, text_path = save_report(report, OUTPUT_DIR)

    # Print report to stdout
    print()
    print(format_report_text(report))

    elapsed = time.time() - t_start
    log.info("Phase 0 complete in %.0fs (%.1f min)", elapsed, elapsed / 60)
    log.info("Results: %s", OUTPUT_DIR)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Quick dry run with 5 stocks**

```bash
cd ~/stock-screener && ~/stock-env/bin/python3 scripts/run_phase0.py --sample 5
```

Verify: script runs, fetches universe, checks 5 stocks, prints report. Should take ~30-60 seconds. Check that OHLCV and fundamentals are both populated.

- [ ] **Step 3: Fix any issues from dry run, re-run if needed**

Review the output. Common issues to check:
- Longbridge symbol conversion (leading zeros for HK)
- East Money API response parsing
- Report formatting

- [ ] **Step 4: Run all unit tests**

```bash
cd ~/stock-screener && ~/stock-env/bin/python3 -m pytest tests/ -v --ignore=tests -m "not integration"
```

Expected: all unit tests pass

- [ ] **Step 5: Commit CLI runner**

```bash
cd ~/stock-screener
git add scripts/run_phase0.py
git commit -m "feat: Phase 0 CLI runner with sample mode and coverage report"
```

- [ ] **Step 6: Full pipeline run (~25 min)**

```bash
cd ~/stock-screener && ~/stock-env/bin/python3 scripts/run_phase0.py --ohlcv-workers 4 2>&1 | tee output/full-run.log
```

This is the actual spike. Let it run. Expected output:
- ~800 A-share + ~100 HK stocks in universe
- OHLCV: ~15 min with 4 workers
- Fundamentals: ~7-10 min
- Report shows per-market per-field coverage

- [ ] **Step 7: Review report and commit results**

Read `output/coverage-report.txt`. Key things to verify:

1. **A-share OHLCV coverage**: should be 95%+ (failures = suspended or very new IPOs)
2. **HK OHLCV coverage**: should be 90%+ (failures = Longbridge symbol mapping issues)
3. **A-share fundamentals**: PE/PB/ROE should be 90%+, revenue/profit growth ~85%+
4. **HK fundamentals**: PE/PB/ROE ~85%+, revenue growth/margins likely low coverage (known EM gap)
5. **Error patterns**: check if errors cluster around specific stock types or API issues

```bash
cd ~/stock-screener
git add output/coverage-report.json output/coverage-report.txt output/full-run.log
git commit -m "data: Phase 0 full pipeline run — coverage report for ~900 stocks"
```

- [ ] **Step 8: Update README with Phase 0 results**

Update `README.md` Status section:

```markdown
## Status

- Design spec: `docs/superpowers/specs/2026-04-14-stock-screener-design.md`
- Phase 0: Data infrastructure validated — [coverage report](output/coverage-report.txt)
- Phase: awaiting Layer 1/2 implementation planning
```

```bash
cd ~/stock-screener
git add README.md
git commit -m "docs: update status with Phase 0 results"
```
