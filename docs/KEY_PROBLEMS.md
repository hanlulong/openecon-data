# Key Problems & Architecture Issues

Last updated: 2026-04-11

---

## 1. Indicator Resolution Accuracy (~47% on edge cases)

**The #1 accuracy bottleneck.** We have 330K+ indicators in the database but the retrieval pipeline often picks the wrong one.

### Root Causes

| Cause | Example | Impact |
|-------|---------|--------|
| FTS5 returns regional noise | "CPI" → Honolulu CPI (DISCONTINUED) instead of national CPIAUCSL | ~30% of FRED queries |
| Short names don't embed well | "M2" (2 chars) doesn't match "M2 money supply" semantically | ~20% of FRED queries |
| Embedding swamped by verbose titles | "Sticky Price CPI Less Food and Energy" scores higher than "Core CPI" | ~15% of queries |
| LLM picks wrong from candidates | Correct code present in top 5 but LLM chooses a variant | ~10% of queries |

### Status

- **Partial fix (Cycle 42):** Catalog codes now injected as top candidates (score 0.95)
- **Remaining:** FTS5 regional noise filtering, embedding index enrichment for short-named series

---

## 2. Eurostat/Provider Data Fetch Failures

Routing is now correct (Italy → Eurostat) but Eurostat often returns **0 data points**.

### Root Causes

- Eurostat requires exact dataset codes (e.g., `NAMA_10_GDP`, `UNE_RT_A`) — generic indicator names fail
- The Eurostat provider's `fetch_indicator()` doesn't always map concept codes to the right SDMX query parameters
- Some datasets require specific dimension filters (frequency, unit, geo) that aren't being set

### Status

- Routing fixed (Cycle 42)
- Data fetching needs work — Eurostat provider logic must translate catalog codes into valid API calls

---

## 3. Multi-Round Decomposition Not Wired

Follow-ups like "break it down by category" don't work.

### Root Causes

- The delta extractor prompt has no guidance for decomposition follow-ups
- `changed_decomposition` field exists in `FollowUpDelta` but is never populated
- FRED decomposition requires fetching multiple sub-series (food CPI, energy CPI, etc.) — no pipeline for this

### Status

- 4/5 multi-round scenarios work (country switch, indicator switch, time range, provider switch)
- Decomposition is the only failing case — architectural gap, not a bug

---

## 4. Long Query Latency (No Timeout on Sync Endpoint)

Complex queries (10+ countries) can take 85+ seconds with no progress indication.

### Root Causes

- `/api/query` (sync endpoint) has no request-level timeout
- Multi-country queries fetch sequentially, not in parallel
- No early warning to user about expected latency

### Status

- Streaming endpoint (`/api/query/stream`) has 300s timeout with progress events
- Sync endpoint needs `asyncio.wait_for()` wrapper or upfront complexity warning

---

## 5. Validation Errors Return Raw Pydantic

Empty queries, missing fields, and invalid JSON return raw Pydantic `ValidationError` objects instead of user-friendly messages.

### Root Cause

- No global `@app.exception_handler(RequestValidationError)` registered
- Pydantic errors bypass the custom `format_error_message` pipeline

### Status

- Low priority but affects developer experience and frontend error handling

---

## 6. Dead Code & Technical Debt

| Item | Size | Status |
|------|------|--------|
| `hybrid_router.py` | 11,851 bytes | Dead — disabled by config, never instantiated |
| `semantic_provider_router.py` | ~400 lines | Disabled but code remains (config toggle) |
| `indicator_lookup.py` | Compat stub | Orphan — nothing imports it |
| `process_query()` | 874 lines | Needs decomposition into phases |
| `_score_search_match()` | 510 lines | Should be split into scoring sub-methods |
| 22+ unused imports | Scattered | Partially cleaned (10 removed Cycle 42) |
| 16 unused exception classes | `exceptions.py` | Never raised anywhere |

---

## 7. Provider Matrix in Prompt (~690 tokens wasted)

The LLM prompt includes a full provider capability matrix + 11 routing rules (~690 tokens). The UnifiedRouter handles routing anyway, so most of this prompt space is wasted.

### Options

- Remove provider matrix entirely (save 690 tokens) — risk: LLM's provider hint at step 9 becomes less informed
- Keep a minimal 3-line hint per provider (save ~400 tokens) — balanced approach
- Current state: full matrix remains (deferred to future cycle)

---

## Priority Order

1. **Indicator resolution** — highest user-facing impact
2. **Eurostat data fetch** — routing is correct but returns empty
3. **Dead code cleanup** — reduce maintenance burden
4. **Decomposition follow-ups** — feature gap
5. **Latency/timeout** — UX improvement
6. **Prompt optimization** — cost/speed improvement
