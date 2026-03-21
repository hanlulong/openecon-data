# Automated Improvement System — User Instructions

**Owner:** hanlulong
**Created:** 2026-03-21
**Last updated:** 2026-03-21
**Version:** 2.0 (post dual-review redesign)

---

## Core Philosophy

This is a **self-improving system**, not a monitoring system.
Each cycle asks: "What is the single highest-impact improvement I can make?"
NOT: "Is anything broken?"

**Key principles:**
- Infrastructure first — fix mechanisms, not symptoms
- Every fix must help at least 5 similar queries (the "5-query test")
- NEVER add hardcoded mappings
- Wrong data scores 0 even if it doesn't crash — correctness is non-negotiable
- Test real user patterns, not just clean synthetic queries

---

## Requirements

### 1. Simulate Real User Behavior
- Test multi-round conversations (query → follow-up → refinement)
- Test complicated queries (multi-country, multi-indicator, temporal comparisons)
- Test session continuity (same conversationId across messages)
- Test clarification flows (respond to clarification options)
- Vary query complexity: simple → medium → complex → edge cases
- Include synonym/alias queries ("jobless rate" = "unemployment")
- Include ambiguous queries ("Korea GDP", "Congo inflation")
- Include negative cases (graceful failure for impossible queries)

### 2. Verify Answer Correctness
- **Deterministic checks first:** Provider match, country completeness, date range coverage, data type validation
- **Value range checks:** GDP per capita 500-200K USD, inflation -10% to +100%, unemployment 0-50%
- **Completeness checks:** G7=7 countries, G20=19, BRICS=5, EU=27, Nordic=5, ASEAN=10
- **Spot-check against authoritative sources:** FRED API, World Bank Data Catalog, Eurostat browser
- **Tolerance ranges:** GDP ±2%, inflation ±0.5%, exchange rates ±0.3% (same day)
- Cross-reference 10% of returned results against official provider APIs
- Flag impossible values automatically (zero GDP, negative population, etc.)

### 3. Progressive Test Growth
- Add 2-5 NEW benchmark queries each cycle based on findings
- Tests get harder over time as easy issues are fixed
- Track coverage via provider × query_type × country_group matrix
- Target: 50 queries by cycle 5, 100 by cycle 10, 200 by cycle 20
- Include real user queries from Supabase query_history when available
- Tag queries by root cause category (country_alias, indicator_alias, provider_routing, etc.)

### 4. Production Testing
- Test against production site (https://openecon.ai) not just localhost
- Use chrome-devtools MCP to verify:
  - Chart renders (not blank), axes labeled, data points visible
  - Table displays correctly for multi-series data
  - Streaming progress steps appear in real-time
  - Clarification buttons render and are clickable
  - Error messages display clearly (not stack traces)
  - Mobile responsive at 375px width
  - No JavaScript console errors
  - Export (CSV/JSON) works correctly
- Take screenshots for regression detection

### 5. Risk-Based Review Before Changes
Review tier determined by fix complexity:
- **Trivial** (test additions, typos, comments): skip review
- **Configuration** (thresholds, timeouts, marker lists): single review agent
- **Infrastructure** (routing logic, resolution mechanisms, new capabilities): dual review agents
- **Architectural** (new services, data flow changes, API changes): dual review + ask user

Review agent checklist:
- Follows infrastructure-first principle (no hardcoding)?
- Would fix at least 5 similar queries?
- Doesn't degrade existing passing tests?
- Improves benchmark dimension scores?
- No new static dict entries (grep check)?

### 6. Documentation Self-Maintenance
- Update CLAUDE.md, TESTING_PROMPT.md, README.md when code changes affect them
- Keep this file (IMPROVEMENT_INSTRUCTIONS.md) current
- Update improvement_tracker.json with metrics each cycle
- Maintain monitoring scripts (heartbeat.sh)

### 7. Commit Rules (Ottawa Work Hours)
- **No git commits** during Ottawa work hours: Mon-Fri 8am-6pm EDT/EST
- **Can deploy/restart production** during work hours
- **Auto-commit** small improvements without asking user
- **Ask user via Discord** for big architectural decisions only

### 9. Informational / Metadata Queries
- Users should be able to ask informational questions, not just data-fetching queries
- Examples: "What employment series does World Bank have?", "Which providers cover trade data?", "What indicators are available for Japan?"
- These should return **text answers** (indicator lists, explanations) rather than charts/data
- The system should detect query intent: informational vs data-fetching
- Use existing indicator database (330K+ indicators) and provider metadata to answer

### 10. Dual Review for Key Changes
- Use dual review agents (two independent agents) for any infrastructure or architectural changes
- Review agents should independently analyze the proposed fix and consider better alternatives
- Only proceed when both agree — reconcile differences or escalate to user
- This applies to both automated cycle improvements AND manual development work

### 8. Reporting
- Report to Discord channel `1484703567519547424` every cycle
- Include: dimension scores, trends, focus area, root cause, fix, impact
- Include: production site status, UI verification results
- Include: queries added this cycle, total query count
- Predict next cycle's focus area

---

## Cycle Structure

```
PHASE 1: HEALTH CHECK (3 min)
├── Local backend + frontend health (heartbeat.sh)
├── Production site health (openecon.ai/api/health)
├── Check for process crashes, port conflicts
└── Service status summary

PHASE 2: BENCHMARK — Raw Scoring (12 min)
├── Run 15-25 benchmark queries against LOCAL backend
├── Run 3-5 critical queries against PRODUCTION
├── Run 2-3 multi-round conversation tests
├── Score each query: 0=error/timeout, 1=partial/incomplete, 2=correct
├── Apply deterministic checks (completeness, provider match, data ranges)
├── Track response times (flag if >5s for simple, >15s for complex)
└── Calculate dimension scores

PHASE 3: DIAGNOSE — Weighted Impact (5 min)
├── Calculate weighted impact scores (not just "weakest dimension")
│   accuracy(30%) × coverage(15%) × multi_country(20%) ×
│   reliability(15%) × session(10%) × ui(10%)
├── Root cause analysis (mechanism, not symptom)
├── "5-query test": will the fix help at least 5 similar queries?
├── Check if failure is flaky (rate limits, network) vs logic bug
└── Propose fix with expected impact estimate

PHASE 4: REVIEW — Risk-Based (0-10 min)
├── Determine fix tier: trivial / configuration / infrastructure / architectural
├── Trivial: proceed immediately
├── Configuration: spawn single review agent
├── Infrastructure: spawn dual review agents
├── Architectural: dual review + Discord message to user
├── Review checklist: no hardcoding, 5-query test, no regressions
└── If disagreement: take conservative approach or ask user

PHASE 5: IMPROVE (15 min)
├── Implement the fix
├── Add 2-5 new benchmark queries based on cycle findings
├── Fix infrastructure/mechanism, never hardcode
├── Run full test suite (pytest backend/tests/ -x -q)
├── If tests fail: revert changes, log failure, skip to Phase 7
└── Update documentation if affected

PHASE 6: VERIFY — Correctness + Production (8 min)
├── Re-run failed benchmark queries (local)
├── Spot-check 2-3 results against authoritative sources (web search)
├── Deploy to production if outside Ottawa work hours
│   (npm run build:frontend if frontend changed, restart backend)
├── Test 2-3 queries on production
├── UI smoke test via chrome-devtools (chart renders, no console errors)
└── Confirm improvement with before/after comparison

PHASE 7: RECORD & REPORT (3 min)
├── Update improvement_tracker.json with cycle data
│   (scores, focus, fix description, queries improved, score deltas, stddev)
├── Commit & push (if outside Ottawa work hours)
├── Discord report with full metrics
├── Predict next cycle focus
└── Flag if score variance >10 points (possible flakiness)
```

---

## Benchmark Dimensions (scored 0-100, weighted)

| Dimension | Weight | What it measures |
|-----------|--------|-----------------|
| **accuracy** | 30% | Single-country queries return correct data from right provider |
| **multi_country** | 20% | Group queries (G7, G20, BRICS, EU) return ALL member countries |
| **coverage** | 15% | All providers work (FRED, WorldBank, IMF, Eurostat, BIS, Comtrade, etc.) |
| **reliability** | 15% | Vague/minimal/edge-case queries handled gracefully, no crashes |
| **session** | 10% | Multi-turn conversations, follow-ups, clarification responses work |
| **ui** | 10% | Production browser test via chrome-devtools: charts render, follow-ups work, no JS errors, export buttons visible |

**Weighted score** = sum(dimension_score × weight). Track trend across cycles.

---

## Multi-Round Conversation Test Scenarios

### Scenario 1: Clarification Response
```
Turn 1: "Show me employment data for US"
Expected: Clarification (employment = unemployment rate? number employed? labor force?)
Turn 2: Select option "employment rate"
Expected: Returns US employment rate data, no further clarification
Pass: Turn 2 returns data, same conversationId
```

### Scenario 2: Follow-up Country Addition
```
Turn 1: "US GDP last 5 years"
Expected: Returns US GDP data
Turn 2: "Add Germany and France"
Expected: Returns 3-country comparison (US, Germany, France)
Pass: Turn 2 has 3 series, same conversationId
```

### Scenario 3: Temporal Refinement
```
Turn 1: "Inflation rate in Japan"
Expected: Returns recent inflation data
Turn 2: "What about 2015?"
Expected: Returns 2015 data without re-asking for country
Pass: Turn 2 has 2015 data point, same conversationId
```

### Scenario 4: Provider Switch
```
Turn 1: "GDP for Brazil 2020-2024"
Expected: Returns data (any provider)
Turn 2: "Get that from World Bank instead"
Expected: Re-routes to World Bank, returns data
Pass: Turn 2 indicates World Bank as source
```

### Scenario 5: Complex Chaining
```
Turn 1: "G7 GDP comparison 2020-2023"
Expected: 7 countries, multi-year data
Turn 2: "Show only US and Japan"
Expected: Filters to 2 countries from same indicator
Pass: Turn 2 has exactly 2 series (US, Japan)
```

---

## Verification Rules

### Completeness (exact counts)
| Group | Expected Count |
|-------|---------------|
| G7 | 7 |
| G20 | 19 |
| BRICS | 5 |
| BRICS+ | 9 |
| EU | 27 |
| Eurozone | 20 |
| ASEAN | 10 |
| Nordic | 5 |
| OECD | 38 |

### Value Ranges (flag if outside)
| Indicator | Valid Range |
|-----------|------------|
| GDP (nominal, USD) | > 0 |
| GDP per capita | 500 — 200,000 |
| GDP growth rate | -30% — +30% |
| Unemployment rate | 0% — 50% |
| Inflation rate | -10% — +100% |
| Population | > 0 |
| Exchange rate | > 0 |
| Interest rate | -5% — +50% |
| Debt-to-GDP | 0% — 300% |

### Response Time Thresholds
| Query Type | Target | Warn | Fail |
|-----------|--------|------|------|
| Single-country simple | <5s | >8s | >15s |
| Multi-country (≤5) | <10s | >15s | >30s |
| Multi-country (>5) | <15s | >25s | >60s |
| Complex/Pro Mode | <20s | >30s | >90s |

---

## Failure Recovery Protocol

If a fix causes test regressions:
1. Revert all changes immediately
2. Re-run benchmarks to confirm revert restored scores
3. Log failure in improvement_tracker.json (with "reverted: true")
4. Send Discord alert: "Improvement attempt failed, reverted"
5. Skip to Phase 7 (record & report)
6. Do NOT attempt the same fix approach in the next cycle

---

## Flakiness Detection

Track standard deviation of dimension scores across last 5 cycles.
- If stddev > 10 for any dimension: flag as "possibly flaky"
- If a query fails inconsistently (passes 60%, fails 40%): tag as "flaky"
- Separate rate-limit failures from logic failures
- Require 3 consecutive passes before declaring a fix successful

---

## Cron Limitations

- CronCreate has a hard **7-day auto-expire**
- Cron dies when the Claude session ends
- **Restart procedure:** In a new session, read this file and run `/loop 2h <cycle-prompt>`
- All state persists in files (benchmark_queries.json, improvement_tracker.json)
- Nothing is lost between sessions

---

## File Locations

| File | Purpose |
|------|---------|
| `scripts/IMPROVEMENT_INSTRUCTIONS.md` | This file — user requirements and system design |
| `scripts/benchmark_queries.json` | Growing benchmark query suite (target: 100+) |
| `scripts/improvement_tracker.json` | Metrics history across cycles |
| `scripts/heartbeat.sh` | Quick health check script |
| `CLAUDE.md` | Project instructions for Claude |
| `TESTING_PROMPT.md` | Testing philosophy and standards |

---

## Coverage Matrix (track gaps)

```
                FRED  WorldBank  Eurostat  IMF  StatsCan  BIS  Comtrade  ExRate  Crypto
GDP              ✓     ✓          ✓        ✓    ✓
Unemployment     ✓     ✓          ✓                ✓
Inflation        ✓     ✓          ✓        ✓    ✓
Interest Rate    ✓                                       ✓
Trade                  ✓                                       ✓
Exchange Rate                                                         ✓
Crypto                                                                         ✓
Debt/Credit            ✓                   ✓             ✓
Population             ✓
Employment       ✓     ✓          ✓              ✓
```

Empty cells = gap in test coverage → priority for new benchmark queries.
