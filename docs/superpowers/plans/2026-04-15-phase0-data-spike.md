# Phase 0: Data Spike

**Goal:** Run ~900 stocks through the full data pipeline once. Answer: what data can we get, what can't we get, why, and how long does it take.

**Success criteria:**
- Universe generates (A-share 800 + HK ~100)
- Small batch (15) runs clean
- Full run completes or resumes to completion
- Coverage report shows per-market per-field availability
- Error report classifies failures by type
- Timing report shows real per-stock and total duration

## Review update (2026-04-16): tightened constraints

After external review, these constraints are locked before any code is written. They supersede anything contradicting below.

### §A Unified security schema (most important)

Every stock carries this 8-field record across all phases — this is the join key between OHLCV, fundamentals, and the final report. **All 8 fields are present on every row**, A-share and HK alike; no market-specific columns.

| Field | Type | A-share example | HK example |
|-------|------|-----------------|------------|
| `market` | enum: `a` / `hk` | `a` | `hk` |
| `symbol_raw` | string (as returned by source) | `600519.SH` / `sh600519` | `00700` / `0700.HK` |
| `symbol_norm` | canonical form (join key) | `600519.SH` | `0700.HK` |
| `name` | Chinese name | `贵州茅台` | `腾讯控股` |
| `universe_source` | which list brought it in | `csi300` / `csi500` | `hk_seed_hsi` / `hk_seed_hscei` |
| `source_status` | provenance state (NOT market-specific) | `live` | `provisional` |
| `last_verified` | when this record's source was last confirmed | runtime timestamp of akshare call | date from HK seed file |
| `source_note` | short provenance note | `akshare index_stock_cons_csindex runtime snapshot` | `manual HSI/HSCEI provisional seed` |

Normalization rules:
- A-share: `{6-digit code}.{SH|SZ}` — SSE codes (6xx) → `.SH`; SZSE codes (0xx / 3xx) → `.SZ`
- HK: `{4-digit zero-padded}.HK` (e.g. `0700.HK`, never `700.HK`, never `00700`)

Provenance fields (`source_status` / `last_verified` / `source_note`) are **universe provenance, not market-specific**: every row has them, populated with live metadata for A-share (runtime snapshot from akshare) and seed metadata for HK (from `hk_constituents.json`).

**Warning — `last_verified` ≠ "manually audited".** For A-share rows it is simply the wall-clock time the upstream API was called during this run. For HK rows it is the date someone last reconciled the seed file against official HSI/HSCEI announcements. Do not conflate the two in downstream reports.

**Field specification — `last_verified` allowed values (locked per external review, 2026-04-16):**
- **Type:** ISO 8601 string, or `null` (HK seed before first audit only).
- **Never a status enum.** Values like `"manually audited"`, `"pending"`, or any non-ISO-8601 sentinel MUST NOT appear here. If a later phase needs an audit-state signal, add a separate `audit_status` field rather than overloading this one.
- **A-share rows:** full datetime with timezone — populated at runtime from the akshare call (e.g. `"2026-04-16T01:45:00+08:00"`).
- **HK rows:** date only — the day the seed was last reconciled against official HSI/HSCEI announcements (e.g. `"2026-04-15"`); `null` until first reconciliation. Coverage report MUST tally `null` separately from real dates.
- **Comparisons allowed:** age / freshness thresholds (e.g. `now() - last_verified > 90 days`). **Not allowed:** equality against literal strings like `"manually audited"` — those belong to a future `audit_status` field if/when added.

`universe.csv` is the authoritative source of `symbol_norm`. OHLCV + fundamentals fetchers accept `symbol_norm` and internally adapt to whatever their upstream API expects.

### §B Fundamentals as single-axis tri-state

Each fundamentals field carries exactly one of three states:

| `field_status` | Meaning |
|----------------|---------|
| `available` | Value retrieved successfully |
| `missing_expected` | Field is known to be unavailable for this market (e.g. HK `revenue_growth` / `gross_margin` / `net_margin` per Phase 0 API testing) — NOT counted as an error |
| `fetch_error` | API call failed, OR field returned null/empty when it should have had a value |

When the entire API call fails for a record, all fields on that record are marked `fetch_error` uniformly.

Rationale: fundamentals coverage is inherently uneven, especially HK. Treating "HK `revenue_growth` missing" as a failure would drown real problems. Phase 0 deliberately does NOT split `fetch_error` into "API 500" vs "unexpected null" sub-categories — both are `fetch_error`, triaged later via `error_msg` if the counts warrant it.

**Implementation note (Phase 0/1 compromise, NOT semantic equivalence).** The classifier in `scripts/phase0_spike.py` that maps "unexpected null" into `fetch_error` MUST carry an explicit code comment at its definition site stating: this is a Phase 0/1 diagnostic-precision tradeoff — a null return and a failed API call are NOT the same thing, they are merged here only because the counts don't yet justify a second state. A future phase that splits them (e.g. `fetch_error_null` vs `fetch_error_api`) should find this comment and treat it as the branching point. Amend this comment when the split happens; do not delete it.

### §C Dry-run exit criteria (not "looks pretty")

The 15-stock dry run is **not** judged by coverage percentages. It passes iff:

1. **Every failure has an `error_type`** — no "unknown" bucket larger than 1-2 cases.
2. **Every failure is reproducible** — re-running the same 15 symbols produces the same classification (modulo transient network flakes, which themselves must be classified).
3. **Every failure is recoverable** — re-running `--limit 15` resumes cleanly from `artifacts/phase0/`; no manual cleanup required.

Only when all three hold do we move to `--limit 70`.

### §D Fixed report metrics (no scope creep)

`artifacts/phase0/report.json` (machine-readable) + `coverage_report.md` (human-readable) contain ONLY these:

1. Universe total (by market, by `universe_source`)
2. OHLCV success rate (by market)
3. Fundamentals coverage rate (by market, by field) — using §B tri-state
4. Error counts (by `error_type`, by phase)
5. Sample detail — the 15 dry-run rows verbatim, for audit

No charts, no recommendations, no derived metrics.

### §E Resume is row-level, not file-level

On restart:
- Load existing `ohlcv.csv` / `fundamentals.jsonl`
- Skip rows where `fetch_status == ok`
- Retry everything else (including `fetch_error` rows — may have been transient)
- `--force` ignores resume entirely

Do NOT skip a whole phase just because the file is non-empty — a mid-phase crash would then permanently skip that phase.

### §F Output directory rename

All Phase 0 outputs go under `artifacts/phase0/`, not `data/phase0/`. `data/` is reserved for production-grade outputs from Phase 1+.

File formats:
- `universe.csv`, `ohlcv.csv`, `timing.csv` — flat uniform-schema rows → CSV
- `fundamentals.jsonl` — nested per-field `field_status` + value → JSONL
- `report.json` + `coverage_report.md` — both kept; json is machine-read, md is human-read

### §G ChiNext scope clarification

**Invariant (use this line verbatim in spec, code comments, and docs):**

> Phase 0 universe uses CSI 300 + CSI 500; GEM/ChiNext names may appear only insofar as they are constituents of those indices, not as a separately sourced universe.

- **MVP universe source = CSI 300 + CSI 500 only.**
- CSI 500's construction rule already includes ChiNext stocks (e.g. `300750.SZ` 宁德时代 is a CSI 500 constituent). ChiNext stocks enter the universe *through CSI 500 membership*, not as a separate source.
- "MVP does not include ChiNext" means: we do NOT add `chinext50` or `chinext_all` as a separate `universe_source`. We do NOT widen the pool.
- The dry-run fixed sample may include a ChiNext stock **only if it is a current CSI 500 constituent**.
- Post-MVP `chinext50` / `csi1000` inclusion is a scope decision, not a data fix.

### §H Frozen interfaces (v3, implementation-ready)

Before any code is written, these interfaces are frozen. Changing them later = rework.

**`config/hk_constituents.json`** — every record has these 8 fields (per §A):
`market`, `symbol_raw`, `symbol_norm`, `name`, `universe_source`, `source_status`, `last_verified`, `source_note`

**`scripts/phase0_spike.py` outputs** (per §F):
- `artifacts/phase0/universe.csv`
- `artifacts/phase0/ohlcv.csv`
- `artifacts/phase0/fundamentals.jsonl`
- `artifacts/phase0/report.json`
- `artifacts/phase0/coverage_report.md`
- `artifacts/phase0/timing.csv`

**Fundamentals `field_status` values** (single axis, per §B):
`available` / `missing_expected` / `fetch_error`

**`last_verified` type** (per §A): ISO 8601 string, or `null` for unverified HK seed rows. Never a status enum — if audit-state is needed, add a separate `audit_status` field.

**Fundamentals field set** (per §I): 8 fields in fixed canonical order — `roe_ttm`, `revenue_growth`, `net_profit_growth`, `net_margin_ttm`, `gross_margin`, `pe_ttm`, `pb`, `market_cap`. HK default `missing_expected` set = `{revenue_growth, net_margin_ttm, gross_margin}`; updated from dry run if actual result disagrees.

**Dry-run exit criteria** (per §C): failures classifiable, reproducible, recoverable — NOT "all green".

### §I Fundamentals field set (frozen for Phase 0)

Phase 0 captures exactly these 8 fundamentals fields per stock, in this **fixed canonical order** — used verbatim by `fundamentals.jsonl`, `report.json`, `coverage_report.md`, and test assertions. **Do not reorder.**

| # | Field | Role | Source | A-share expectation | HK expectation |
|---|-------|------|--------|---------------------|----------------|
| 1 | `roe_ttm` | Core factor (spec §3.1 Dim 1) | East Money push2 | `available` | `available` |
| 2 | `revenue_growth` | Core factor (spec §3.1 Dim 1) | East Money push2 | `available` | `missing_expected` |
| 3 | `net_profit_growth` | Core factor (spec §3.1 Dim 1) | East Money push2 | `available` | `available` |
| 4 | `net_margin_ttm` | Core factor (spec §3.1 Dim 1) | East Money push2 | `available` | `missing_expected` |
| 5 | `gross_margin` | Reserve / audit | East Money push2 | `available` | `missing_expected` |
| 6 | `pe_ttm` | Reserve (excluded from core scoring per spec; still measured to support guard rules like `PE > 0`) | East Money push2 | `available` | `available` |
| 7 | `pb` | Reserve | East Money push2 | `available` | `available` |
| 8 | `market_cap` | Reserve / universe scale reporting | East Money push2 | `available` | `available` |

Field names are lowercase_snake_case canonical keys; the fetcher translates East Money raw field names into these keys.

**Default `missing_expected` set for HK is exactly `{revenue_growth, net_margin_ttm, gross_margin}`.** This is the pre-run baseline used by the classifier, coverage report, and tests.

**Update rule (NOT "whichever feels right"):**
- If the 15-stock dry run confirms this set — no change.
- If the dry run disagrees for any HK field (e.g. `gross_margin` unexpectedly returns values, or `roe_ttm` is actually empty), the classifier rules AND this §I table are updated **from the dry run result**, not from the pre-run assumption. Plan doc and code must be amended in the same commit before proceeding to `--limit 70`.
- Implementation MUST NOT silently diverge from this table: either the table matches reality, or the commit that breaks the match also updates the table.

Adding or removing a field from this list is an interface change → requires another freeze round.

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

1. **Universe** — akshare CSI 300+500 (live) + `config/hk_constituents.json` (static) → merge → `artifacts/phase0/universe.csv` (unified 8-field schema per §A)
2. **OHLCV** — Longbridge CLI `kline` per stock, N workers → `artifacts/phase0/ohlcv.csv`
3. **Fundamentals** — East Money push2 per stock, sequential with Session → `artifacts/phase0/fundamentals.jsonl` (tri-state per §B)
4. **Report** — aggregate into `artifacts/phase0/report.json` (fixed metrics per §D) + `artifacts/phase0/coverage_report.md` + `artifacts/phase0/timing.csv`

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

Binding rule: **row-level only** — see §E.

- Before OHLCV phase: load existing `ohlcv.csv`, skip rows with `fetch_status == ok`
- Before fundamentals phase: load existing `fundamentals.jsonl`, skip rows with `fetch_status == ok`
- `--force` ignores resume state

### Output files

| File | Content |
|------|---------|
| `artifacts/phase0/universe.csv` | Unified 8-col schema (§A): `market`, `symbol_raw`, `symbol_norm`, `name`, `universe_source`, `source_status`, `last_verified`, `source_note` |
| `artifacts/phase0/ohlcv.csv` | `symbol_norm`, `market`, `rows`, `time_s`, `fetch_status`, `error_type`, `error_msg` |
| `artifacts/phase0/fundamentals.jsonl` | `symbol_norm`, `market`, per-field `field_status` + value (single-axis tri-state per §B), `error_type`, `error_msg` |
| `artifacts/phase0/report.json` | Machine-readable fixed metrics (§D): universe total / OHLCV success / fundamentals coverage / error counts / sample detail |
| `artifacts/phase0/coverage_report.md` | Human-readable version of `report.json` |
| `artifacts/phase0/timing.csv` | `phase`, `total_stocks`, `succeeded`, `failed`, `elapsed_s`, `avg_per_stock_s` |

### Tests (minimal)

- `tests/test_spike.py::test_smoke` — runs `phase0_spike.py --limit 3 --market a`, asserts exit 0 + output files exist
- `tests/test_spike.py::test_hk_config_schema` — validates `hk_constituents.json` has required fields
- `tests/test_spike.py::test_classify_error` — unit test for error classifier if extracted as function

### Graduated execution

1. `--limit 15` → fix symbol format / API issues — **graduation gate: §C three tests (classifiable / reproducible / recoverable), NOT coverage %**
2. `--limit 70` → verify rate limit handling + error classification at scale
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
- A-share 10 (frozen tickers, verified 2026-04-16 against live CSI 300 / CSI 500 membership via `ak.index_stock_cons_csindex`):
  - **SSE (5):** `600519.SH` 贵州茅台 (consumer), `600036.SH` 招商银行 (financials), `601318.SH` 中国平安 (financials), `600276.SH` 恒瑞医药 (healthcare), `600900.SH` 长江电力 (utilities)
  - **SZSE main-board (3):** `000333.SZ` 美的集团 (consumer/manufacturing), `000858.SZ` 五粮液 (consumer), `000651.SZ` 格力电器 (consumer/manufacturing)
  - **ChiNext (2)**, both verified as current CSI 500 constituents per §G: `300454.SZ` 深信服 (tech), `300450.SZ` 先导智能 (manufacturing)
- HK 5: `0700.HK` 腾讯, `0005.HK` 汇丰, `0941.HK` 中移动, `9988.HK` 阿里, `3690.HK` 美团 (normalized per §A)

**3. Phase 0 = facts only.** The script reports what it sees. It does NOT:
- Fill missing HK fields
- Derive growth rates
- Adjust weights
- Apply Layer 1 filters
- Score or recommend anything

Those belong to Phase 1.

### Config dependency

`config/hk_constituents.json` — provisional HSI + HSCEI members with codes + Chinese names. Seeded by `scripts/seed_hk_constituents.py`, maintained manually each quarter. Must be verified against official announcements before Phase 1.
