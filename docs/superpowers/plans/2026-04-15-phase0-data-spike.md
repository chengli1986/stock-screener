# Phase 0: Data Spike

**Goal:** Run ~900 stocks through the full data pipeline once. Answer: what data can we get, what can't we get, why, and how long does it take.

**Success criteria:**
- Universe generates (A-share 800 + HK ~100)
- Small batch (15) runs clean
- Full run completes or resumes to completion
- Coverage report shows per-market per-field availability
- Error report classifies failures by type
- Timing report shows real per-stock and total duration

## One script: `scripts/phase0_spike.py`

```
phase0_spike.py --limit 15              # dry run
phase0_spike.py --limit 70              # small batch
phase0_spike.py                         # full run (~900)
phase0_spike.py --market a --limit 50   # A-share only
phase0_spike.py --workers 4             # parallel OHLCV
phase0_spike.py --force                 # ignore resume, refetch all
phase0_spike.py --skip-ohlcv            # fundamentals only
phase0_spike.py --skip-fundamentals     # OHLCV only
```

### Pipeline

1. **Universe** — akshare CSI 300+500 (live) + `config/hk_constituents.json` (static) → merge → `data/phase0/universe.csv`
2. **OHLCV** — Longbridge CLI `kline` per stock, N workers → `data/phase0/ohlcv_results.csv`
3. **Fundamentals** — East Money push2 per stock, sequential with Session → `data/phase0/fundamental_results.csv`
4. **Report** — aggregate into `data/phase0/coverage_report.md` + `data/phase0/timing.csv`

### Error classification

Every fetch result gets one of:
- `ok` — data returned successfully
- `timeout` — API or subprocess timed out
- `rate_limited` — HTTP 429 or connection reset after rapid calls
- `empty_response` — API returned 200 but no data
- `symbol_not_found` — API explicitly says symbol unknown
- `connection_error` — network-level failure
- `parse_error` — response not valid JSON / unexpected schema
- `subprocess_error` — Longbridge CLI non-zero exit
- `unknown` — uncategorized

### Resume

- Before OHLCV phase: load existing `ohlcv_results.csv`, skip symbols with `status=ok`
- Before fundamentals phase: load existing `fundamental_results.csv`, skip `status=ok`
- `--force` ignores resume state

### Output files

| File | Content |
|------|---------|
| `data/phase0/universe.csv` | symbol, name, market, source_index |
| `data/phase0/ohlcv_results.csv` | symbol, market, rows, time_s, status, error_type, error_msg |
| `data/phase0/fundamental_results.csv` | symbol, market, pe, pb, roe, rev_growth, profit_growth, gross_margin, net_margin, mkt_cap, status, error_type, error_msg |
| `data/phase0/coverage_report.md` | Human-readable: per-market field coverage, error breakdown, timing |
| `data/phase0/timing.csv` | phase, total_stocks, succeeded, failed, elapsed_s, avg_per_stock_s |

### Tests (minimal)

- `tests/test_spike.py::test_smoke` — runs `phase0_spike.py --limit 3 --market a`, asserts exit 0 + output files exist
- `tests/test_spike.py::test_hk_config_schema` — validates `hk_constituents.json` has required fields
- `tests/test_spike.py::test_classify_error` — unit test for error classifier if extracted as function

### Graduated execution

1. `--limit 15` → fix symbol format / API issues
2. `--limit 70` → verify rate limit handling + error classification
3. Full run → real coverage numbers + timing baseline

No mock tests. The whole point is testing real API behavior.

### Guardrails

**1. HK JSON is provisional.** Constituent codes are initial seeds, not verified against official HSI/HSCEI quarterly announcements. Config must include:
```json
{
  "source_status": "provisional",
  "last_verified": null,
  "source_note": "Initial seed list; verify against official HSI/HSCEI constituent announcements before production use."
}
```
Coverage report must state this clearly.

**2. Dry run uses fixed samples, not random.** The 15-stock dry run must be deterministic and reproducible:
- A-share 10: cover SSE + SZSE + ChiNext, sectors = financials, consumer, tech, manufacturing
- HK 5: 00700 腾讯, 00005 汇丰, 00941 中移动, 09988 阿里, 03690 美团

**3. Phase 0 = facts only.** The script reports what it sees. It does NOT:
- Fill missing HK fields
- Derive growth rates
- Adjust weights
- Apply Layer 1 filters
- Score or recommend anything

Those belong to Phase 1.

### Config dependency

`config/hk_constituents.json` — provisional HSI + HSCEI members with codes + Chinese names. Seeded by `scripts/seed_hk_constituents.py`, maintained manually each quarter. Must be verified against official announcements before Phase 1.
