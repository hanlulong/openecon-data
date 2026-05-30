# OpenEcon Deep Review — 2026-05-30

**Status:** Findings only. Implementation requires user approval per item.
**Date:** 2026-05-30 (Saturday)
**Supersedes (partially):** `docs/KEY_PROBLEMS.md` (2026-04-11) — re-verified, expanded, and re-prioritized below.

---

## 0. TL;DR

- **Architecture health:** Mixed. Routing tier consolidation and prompt-registry components are correctly architected and only need cleanup. **Indicator resolution, conversation-state delta extraction, and the catalog runtime surface carry hardcoded-semantic-rule debt that directly limits the 95%/330K accuracy goal.**
- **Current accuracy:** ~85% effective (when provider mismatches counted as success), **~51% strict** on the 100-query suite. **The 99% reported on the 10×10 multi-round test reflects the curated multi-round path, not the long-tail accuracy.**
- **Projected trajectory:** **+6 to +14pp toward 95%** after Phase 1+2 land. Compounding gains in Phase 3 (provider layer) and Phase 4 (cache + routing-authority schema).
- **Critical pre-work added:** **Phase 0 — security & ops-reliability hotfixes.** The current code is missing a sync-endpoint timeout, has an X-Forwarded-For spoofing hole, and a Supabase-misconfig silent-fail. These block the shadow-mode telemetry the accuracy work depends on.
- **What the audit found that the user has been right about all along:** **6 confirmed hardcoded-semantic-rule violations** in the hot path (provider matrix prompt, indicator_database country/domain penalties, CoinGecko regex guard, FRED-USA override, delta_extractor crypto/alias dicts). All must be removed via the LLM-first directions in Phase 1–2.

---

## 1. Methodology

Three independent multi-agent audits, then cross-verification:

| Audit | Agents | Output | Status |
|---|---|---|---|
| **Workflow A** — Discovery | 8 finders × 3 verifiers + synthesis | 128 raw → **101 deduped** findings with file:line citations | Synthesis failed mid-run; **finder output recovered** from agent transcripts |
| **Workflow B** — Architectural debate | 8 component maps + 24 advocates + 8 judges + synthesis | **8 component recommendations + 5-phase roadmap** + 10 cross-cutting invariants + 11 anti-patterns | Completed (41 agents, 2.3M tokens) |
| **Workflow C** — Verification + Completeness | 4 parallel agents (high-sev re-verify, A↔B cross-map, completeness critic, user-journey audit) | Cluster verification, conflict resolution, 10 missed categories, 5 user-impact gaps | Completed |

**Triangulation logic:** A finding that survived all three lenses (independently surfaced + adversarially verified + accepted by user-journey audit) is "high-confidence." Findings that only one workflow raised are "single-source" and demoted unless they reproduce on direct code read.

**Rule-1 audit:** A separate pass through every recommendation specifically checked whether the proposed fix introduces hardcoded semantic rules. **Five fixes failed this check and were rewritten** to LLM-first equivalents (noted inline below).

---

## 2. Current State (Verified)

### 2.1 Repo on disk (2026-05-30)

| Module | Lines | State | Source |
|---|---|---|---|
| `backend/services/query.py` | 7,665 | Monolithic (185 methods). Process_query is the hot path. | A#0 verified |
| `backend/services/indicator_clarification.py` | 3,394 | Oversized | A#7 |
| `backend/services/indicator_resolution.py` | 3,206 | Oversized | A#7 |
| `backend/routing/unified_router.py` | 699 | Primary router. Contains semantic keyword lists (146–155, 477–482, 579–621). | B map verified |
| `backend/routing/hybrid_router.py` | 294 | **Dead.** Disabled by config default; no production code path reaches it. | A#2, B Phase 1 |
| `backend/routing/semantic_provider_router.py` | 471 | **Dead.** Same as hybrid. | A#48 |
| `backend/services/indicator_lookup.py` | small | **Orphan compat stub.** Zero non-test importers. | A#1, A#79 |
| `backend/services/reranker.py` | 156 | **Dead.** Zero non-test callers. flashrank dep can drop. | B + A#84 |
| `backend/data/indicators.db` | 903 MB | Last modified 2026-04-26. No version file. | C-completeness |
| `backend/catalog/concepts/*.yaml` | ~30 concepts | Curated head; long tail unrepresented. | C user-journey |

### 2.2 Accuracy snapshot (most recent measurements)

- `framework_sweep_100_latest.json`: **51/100 strict pass, 85% effective.**
- Multi-round 10×10 (most recent): **99/100 (99%)** — but only covers 10 well-shaped scenarios.
- **Failing categories on 100-query suite:** HS Trade (0%), FX (0%), Trade Ratio (44%), regional aggregates (clarify-or-fail).

### 2.3 What works well

- Provider routing for unambiguous cases (FRED-US, WB-international, Eurostat-EU member) ✓
- Crypto (CoinGecko routing) — 5/5 pass ✓
- Multi-round country/indicator/time/frequency switches — delta extractor handles these deterministically ✓
- Streaming endpoint + SSE — 300s timeout exists ✓
- Pro Mode and code execution sandbox — functional ✓

---

## 3. Cross-Cutting Themes (8)

Findings clustered into 8 themes that span multiple files/components. Each is the lens through which Phase 1–4 work should be planned.

| # | Theme | Severity | Scope of impact | Where addressed |
|---|---|---|---|---|
| **T1** | **Hardcoded semantic rules in the runtime path** | High | Affects every query | Phase 1 (catalog runtime + prompt matrix) + Phase 2 (indicator_database penalties + delta_extractor noop handlers) + Phase 4 (UnifiedRouter keyword lists) |
| **T2** | **Operational reliability — no sync-endpoint timeout, X-Forwarded-For spoofing, Supabase silent-fail** | High | Production stability + security | **Phase 0** (new) |
| **T3** | **Frontend SSE robustness bugs (buffer flush, null-check, isolation)** | High (#13/16/22) → Medium | All streaming queries on frontend | Phase 1 (surgical bug-fix only, no refactor) |
| **T4** | **Indicator resolution: ranking-layer semantic shortcuts + missing telemetry** | High | ~30% of short-name queries pick wrong series | Phase 2 (RRF + telemetry + LLM-driven cue adjudication) |
| **T5** | **Conversation/decomposition gaps** — decomposition follow-ups never wired, no cross-provider join, no aggregation pipeline | Critical (decomposition) | Multi-round + cross-provider | Phase 2 (Tier-3 escape hatch + schema bump for `changed_decomposition`, `aggregation_type`, `yoy_change`) |
| **T6** | **Provider layer: silent-empty returns + duplicated normalization** | Medium-High | Defeats the accuracy verification harness (empty-vs-error indistinguishable) | Phase 3 (composition-based `_sdmx/` utilities + error-handling convention) |
| **T7** | **Observability/CI gaps — no structured logs, no CI, no token-cost tracking, no coverage gate, no MCP versioning** | Medium-High | Blocks Phase 2 shadow-mode validation | Phase 0 (basic logs + CI) + Phase 1 (cost telemetry) |
| **T8** | **Catalog as a runtime authority** — `find_concept_by_term`, `explicit_exclusions`, primary/confidence fields override LLM routing | Medium-High | Long-tail 99.97% of inventory loses to short-circuit on curated head | Phase 1 (catalog runtime surface deletion) |

---

## 4. Confirmed Hardcoded-Rule Violations (Rule 1)

Six distinct violations of the user's #1 standing rule ("no hardcoded semantic rules in the runtime path"). Each is in the hot path of every query and **must be replaced with LLM-driven adjudication or removed**. **Do not patch them; remove the surface.**

| # | Location | What it does | LLM-first replacement |
|---|---|---|---|
| H1 | `backend/services/simplified_prompt.py:404–430` | 11 if-then rules: "Canada → StatsCan", "EU country → Eurostat", etc. baked into the LLM prompt | Replace with neutral capability statements; gate the matrix on `include_provider_hints`; let LLM apply capabilities |
| H2 | `backend/routing/unified_router.py:146–155` | `_FISCAL` / `_CRYPTO` regex lists reject CoinGecko on macro queries | Delete; let the LLM provider-choice handle dual-intent queries |
| H3 | `backend/services/indicator_database.py:1049–1116` | Hardcoded `non_us_countries` list applies -15 penalty to FRED results | Replace with `CountryResolver.detect_all_countries_in_query()` (already exists in unified_router.py) |
| H4 | `backend/services/indicator_database.py:1073–1084` | M2/GDP subject-vs-reference +5/-10 penalties via string-pattern matching | Replace with `extract_indicator_cues()` LLM adjudication for ambiguous candidates |
| H5 | `backend/services/indicator_database.py:1177–1200` | `_domain_mismatches` dict applies -12 penalty for keyword pair mismatches | Replace with LLM semantic adjudication when domain conflict is suspected |
| H6 | `backend/services/parameter_validator.py:268–307` | Overrides LLM-chosen WorldBank to FRED when word-overlap ≥30% | Delete the override; pass FRED candidates as evidence to LLM |
| H7 | `backend/services/delta_extractor.py:485–660` | Four `_try_*_noop` handlers + `_CRYPTO_INDICATOR_TO_COIN_ID` + `_INDICATOR_ALIAS_EQUIVALENTS` dicts | Port handler intents into `extract_with_llm` prompt; delete the dicts |

**ADDITIONAL** rule-violating "fix" proposals from the audits that we explicitly **REJECT**:

- ❌ "Pre-seed an allowlist of 200 canonical core indicators" (suggested in user-journey audit) — relocates the hardcoded mapping; same anti-pattern.
- ❌ "Auto-generate 5K+ entry catalog with primary provider/code per concept" (Workflow B optionC for catalog) — industrializes the mapping.
- ❌ "Move SERIES_MAPPINGS / DATASET_DEFAULT_FILTERS / COUNTRY_MAPPINGS into YAML" — the rule is about existence, not language.

---

## 5. Updated 6-Phase Roadmap

**Phase 0 was added** by Workflow C's cross-map and completeness lenses; it's a precondition for the accuracy work in Phase 1+2 to be measurable.

### Phase 0 — Security & Ops-Reliability Hotfixes (NEW)
**Duration:** ~3–5 days · **PRs:** 2 · **Risk:** Low (pure bug fixes, no architectural change)

- **0.1** Add `asyncio.wait_for(timeout=QUERY_TIMEOUT_SECONDS or 120)` wrapper to `query_endpoint` and `query_pro_endpoint` at `backend/main.py:669–748` — A#1/8/22/23/24/25 (5 finders agreed).
- **0.2** Fix X-Forwarded-For spoofing at `backend/main.py:262–269` with a `TRUSTED_PROXIES` env-var allowlist — A#19/52/59.
- **0.3** Production fail-closed on missing Supabase at `backend/services/supabase_service.py:281–293` — match `auth_factory.py:34–39` pattern — A#27/57.
- **0.4** Gate `/api/debug/conversation-state` behind `Depends(get_required_user)` — A#58.
- **0.5** Wrap startup metadata loader in `asyncio.wait_for(timeout=metadata_loading_timeout+10)` at `backend/main.py:122–151` — A#61/63.
- **0.6** Real `/api/health` Redis + Supabase + LLM ping with cached result — A#51; expand `health()` at `backend/main.py:597–606`.
- **0.7** Frontend `CodeExecutionDisplay.tsx:82–94` URL allowlist for code-execution media — A#21.

**Acceptance:** All 6 test files posting to `/api/query` still pass. Health check returns 503 when Redis is dead.

---

### Phase 1 — Cleanup & Quick Wins
**Duration:** ~1–2 weeks · **PRs:** 4–5 · **Risk:** Low (pure deletion + surgical bug fixes)

- **1.1** Delete `backend/routing/hybrid_router.py` + `backend/routing/semantic_provider_router.py` + the `use_hybrid_router` / `use_semantic_provider_router` / `use_litellm_router_fallback` config flags + the `+0.10` guardrail at `query.py:1088–1107`. Collapse `_select_routed_provider` (`query.py:1043–1158`) to ~30 lines of UnifiedRouter-only. Remove `semantic-router` and `deepagents` from `requirements.txt` if unused elsewhere. **Net: ~765 LOC and ~63 MB of deps gone, zero runtime behavior change.**
- **1.2** Delete the **catalog runtime authority surface**: `explicit_exclusions` (`catalog_service.py:225–261`), `find_concept_by_term` token-overlap with import/export directional penalties (`catalog_service.py:150–222`), `validate_indicator_match`, `is_false_positive`, `expand_indicator`, and the `primary`/`confidence` provider-preference fields. Migrate 6 reverse-lookup callers to `indicators.db` SELECTs. Retain `catalog_validator.py` as **CI-only** metadata gate. Delete `backend/services/indicator_lookup.py` orphan stub.
- **1.3** **Prompt registry quick wins** (Position A from Workflow B): gate `_provider_matrix()` on `include_provider_hints` flag (~690 tokens reclaimed per intent parse); add `_sanitize_context()` helper at `simplified_prompt.py:220` to neutralize f-string interpolation of follow-up fields (**closes LLM prompt-injection vector** — C-completeness #5); add `tests/test_semantic_match_judge_prompts.py` and `tests/test_grok_system_prompt.py`.
- **1.4** **NEW: Error contract** — register `@app.exception_handler(RequestValidationError)`; define `ErrorResponse` type; log `ValidationError` at `query.py:3318–3328` and `delta_extractor.py:300–500` — A#9, A#81, A#94, A#98.
- **1.5** **NEW: Frontend SSE bug fixes (no refactor)** — confined to `packages/frontend/src/services/api.ts`:
  - Flush trailing buffer on stream end (A#13, A#16, A#11/12)
  - Inactivity timeout (A#22)
  - Null-check `conversationId` (A#17)
  - Scope-ID callback dispatch so cancelled-stream events can't reach active UI (A#18, A#24)
  - Reset `isLoggingOut` flag (A#20)
  - **DO NOT touch `ChatPage.tsx`** — preserves the load-bearing `streamingQueryRef + sessionStorage` StrictMode-defense per Workflow B invariant.
- **1.6** **NEW: Token-cost telemetry** (C-completeness #3) — log `prompt_tokens`, `completion_tokens`, `estimated_cost_usd` per query in structured form. Add `/api/cost/daily` admin endpoint.

**Acceptance:** All existing tests pass. `/api/cache/stats` shows no Redis-vs-memory key divergence. Prompt size for intent parse drops by ~690 tokens. `pip-audit` runs clean.

---

### Phase 2 — Accuracy-Critical (the 95%/330K levers)
**Duration:** ~2–3 weeks · **PRs:** 5–7, **all flag-gated with shadow-mode validation ≥7d against the 100-query suite** · **Risk:** Medium

- **2.1** **IndicatorSelector per-stage telemetry baseline.** Before any algorithmic change. Capture FTS5 candidates / embedding candidates / RRF rank / LLM-pick / final code at every query. Surface to `/api/admin/indicator_telemetry`. This is also the prerequisite for measuring everything that follows.
- **2.2** **RRF fusion** behind `INDICATOR_FUSION=rrf|legacy` flag. Replace `_effective_rank` magic constants at `indicator_selector.py:467–491` with parameterless Reciprocal Rank Fusion. Shadow-mode for ≥7d.
- **2.3** **Delete `backend/services/reranker.py`** + the `flashrank` dep once RRF parity is confirmed. Verified zero non-test callers.
- **2.4** **Replace H3 / H4 / H5** (`indicator_database.py:1049–1200` country/subject-ref/domain hardcoded penalties) with `extract_indicator_cues()` LLM adjudication for ambiguous candidates. Flag-gated; A/B against shadow logs. **Do NOT add a canonical-indicator allowlist** (rejected; see §4).
- **2.5** **Delete H6** (`parameter_validator.py:268–307`) FRED-USA override. Pass FRED candidates as evidence to LLM. Re-measure WB/FRED disambiguation.
- **2.6** **Conversation-state schema bump:** add `delta_confidence`, `needs_full_rewrite`, **`changed_decomposition`** (T5 critical fix), `yoy_change`, `rolling_window`, `aggregation_type` to `FollowUpDelta`. Initially as no-op telemetry fields. Then port `_try_*_noop` handler intents into `extract_with_llm` prompt with concrete few-shot examples.
- **2.7** **Tier-3 escape hatch:** on low `delta_confidence` or compound restructuring, escalate to a full-state LLM rewrite that re-folds through `extract_state_from_intent + merge_new_state_with_previous` to preserve `resolved_indicator_code` and `statscan_cube_metadata`. Behind `DELTA_FULL_REWRITE_ENABLED=false`. Shadow-validate against the golden conversation set.
- **2.8** **Delete H7** (`delta_extractor.py:485–660` noop handlers + `_CRYPTO_INDICATOR_TO_COIN_ID` + `_INDICATOR_ALIAS_EQUIVALENTS`) **only after** 2.6 prompt-port validation.
- **2.9** **Indicator/provider compatibility validation in `merge_state()`** — A#100.
- **2.10** **Clarification quality** — validate options against provider before offering (A#92); enrich informational queries with point counts / date range / frequency (A#93).
- **2.11** **Decomposition pipeline wiring** — populate `changed_decomposition` end-to-end (T5 critical). The `needsDecomposition=true` flag from intent parse is detected but never reaches the multi-round handler.

**Acceptance:** Strict pass rate on 100-query suite climbs from 51% to **65%+**. Decomposition follow-ups work for at least province / state / sector / category. No regression on multi-round 10×10.

---

### Phase 3 — Provider-Layer Consolidation
**Duration:** ~2–3 weeks · **PRs:** 5, per-provider `USE_SHARED_PARSER_FOR_<X>` flags · **Risk:** Medium

- **3.1** Extract `_normalize_percentage_values` (three forks across FRED/IMF/Eurostat) into `backend/providers/_sdmx/normalizers.py` as opt-in utilities. **Composition, not inheritance.** Per-provider flag-gated.
- **3.2** Extract `_parse_json_stat` into `_sdmx/json_stat.py` (Eurostat only consumer).
- **3.3** Extract `_parse_sdmx_structure_specific_xml` (`imf.py:1588`) and `_parse_sdmx_csv` (`imf.py:1615`) into `_sdmx/sdmx.py`. **Most parity scrutiny here** — touches the recently-hardened IMF certification path (commits f7c139d / 54f888e / ad97843 / 386c8e3 / 4872e11).
- **3.4** **NEW: Provider error-handling convention.** All providers raise `DataNotAvailableError` on parse failure. `return []` means "API success, zero rows." Invariant added. Fixes Eurostat `_parse_dataset` (A#4/66 — lines 816, 824, 832), IMF missing-field validation (A#67), BIS swallowed exceptions (A#70).
- **3.5** **NEW: BaseProvider plumbing fixes** — `_post_with_retry` honors `effective_timeout` (A#3/65), unified timeout convention (A#75), WorldBank circuit-breaker `asyncio.Lock` (A#73), fix `CL_LS_TYPE_OF_TRANSFORMAtION` typo + codelist alias test (A#76).
- **3.6** **NEW: Retire provider-layer hardcoded dicts** — Comtrade `COMMODITY_MAPPINGS` (A#69) and Eurostat `DATASET_DEFAULT_FILTERS` (A#74) migrate to live SDMX/metadata API discovery with caching. Percentage normalization becomes metadata-driven (`unit_is_decimal_percent` flag from API), not value-magnitude heuristic (A#72).
- **3.7** Skip OECD migration (60 req/hr makes A/B noisy). Skip FRED / CoinGecko / ExchangeRate / Comtrade / WorldBank / StatsCan (no SDMX surface to benefit).
- **3.8** **NEW: Feedback service async correction** — move SMTP/HTTP off the event loop. Either `asyncio.create_task()` or `ThreadPoolExecutor` (A#6/7/13/16).

**Acceptance:** ~180K SDMX-served indicators (IMF + Eurostat + BIS) gain consistent percentage normalization. Silent-empty bug class eliminated.

---

### Phase 4 — Cache Unification + Routing-Authority Schema
**Duration:** ~1–2 weeks · **PRs:** 3 · **Risk:** Medium

- **4.1** **Unified cache key builder** with `Namespace` enum (INTENT / INDICATOR_RESOLUTION / PROVIDER_DATA) and `Layer` enum (L1_MEMORY / L2_REDIS). Fixes the verified MD5 divergence between `redis_cache.py:165–188` and `cache.py:65–66` and the 60s-vs-3600s TTL bug for crypto/FX. Ship behind `OPENECON_CACHE_TIERED=1` with write-through.
- **4.2** Pin invariants at coding time: L1 keeps `get_stale`-equivalent (load-bearing at `query.py:5508–5529`); single-worker remains default (Position B cross-worker argument moot per `restart_dev.py:227–235`); TTLs stay content/provider-driven, carried by `Namespace`, not by `Layer`.
- **4.3** **NEW: Routing decision-authority schema** — split `confidence` (probability) from `authority` (enum: explicit / mechanical_structure / semantic_llm / default) in `RoutingDecision`. Centralize all thresholds in a `ConfidencePolicy` class. Refuse override when `hardcoded_final_authority` is set. This is the "scoped C debate" Workflow B explicitly deferred from `routing_layers`.
- **4.4** **NEW: Delete UnifiedRouter's `_FISCAL` / `_CRYPTO` / `_PROPERTY` / macro_terms keyword lists** at `unified_router.py:146–153, 477–482, 579–621` (H2). Gated, shadow-mode against 100-query suite for 14d before default-on.
- **4.5** Defer the catalog/embedding-version-in-key sub-proposal until per-namespace hit-rate metrics prove the indicator-resolution cache benefit empirically.

**Acceptance:** Cache hit-rate dashboard works. `RoutingDecision.authority` populated everywhere. No cross-layer key collisions.

---

### Phase 5 — Deferred / On-Demand
**Duration:** 0 weeks now; revisit only if a concrete trigger emerges

- **5.1** Frontend `ChatPage.tsx` decomposition — Position A wins (no refactor). YAGNI; ~600 of 1,333 LOC are export-to-Python codegen strings, not React.
- **5.2** **NEW (deferred):** `QueryService` decomposition (A#0 — 185 methods, 7,665 lines) and `indicator_clarification`/`indicator_resolution` split (A#7) — same YAGNI logic.
- **5.3** **NEW (deferred):** `CountryResolver` consolidation across the 5 forks (A#5) — defer unless Phase 3 provider work surfaces drift.
- **5.4** Two surgical follow-ups land opportunistically (not as a dedicated cycle): route export-to-Python button (`ChatPage.tsx:612`) through `api.queryStream`; add a comment block documenting `streamingQueryRef + sessionStorage` as StrictMode-defense.

---

## 6. Cross-Cutting Invariants (every PR must satisfy)

1. **No hardcoded semantic rules in the runtime path.** Structural parsing only (HS-code regex, ISO year/country, currency-pair, frequency tokens, provider-name verbs). All semantic understanding routes through the LLM via Pydantic-typed schemas (`ParsedIntent`, `FollowUpDelta`, `SemanticMatchJudgment`, `ExecutionResultJudgment`).
2. **Framework fixes only.** Every change helps 5+ query categories OR is pure cleanup. Never add to `SERIES_MAPPINGS`, `catalog.explicit_exclusions`, `_CRYPTO_INDICATOR_TO_COIN_ID`, `_INDICATOR_ALIAS_EQUIVALENTS`, or any per-query patch surface.
3. **Preserve multi-round conversation behavior.** `ConversationState.resolved_indicator_code`, `statscan_product_id`, `statscan_cube_metadata`, `provider_locked`, `active_answer_members` survive every refactor. Tier-3 full-state rewrites MUST re-fold through `extract_state_from_intent + merge_new_state_with_previous`. The INTENT cache invalidation guard `conversation_context is None` at `query.py:4858` stays verbatim. UnifiedRouter must keep honoring `__semantic_provider_locked` at `query.py:4504`.
4. **Preserve public endpoint contracts:** `/api/query` (sync), `/api/query/stream` (SSE), `/api/query/pro`, `/api/query/pro/stream`, and the MCP `/mcp` surface with `operation_id=query_data`. The 6 test files posting to `/api/query` MUST continue passing unchanged.
5. **Production fails closed.** `ALLOW_TEST_USER=true` is a dev-only switch. Supabase missing in production = auth fails, not silently downgrades. Rate-limiting bypass is localhost/dev-only.
6. **OECD stays low priority.** No refactor makes OECD primary; no migration A/Bs against OECD (60 req/hr); OECD's 30-min cache TTL preserved.
7. **Indicator discovery is the only solution for missing indicators.** `indicators.db` FTS5 (330K+ rows) is the authority; missing coverage is fixed by `scripts/fetch_all_indicators.py` or FTS keyword enrichment, **never** by static-dict additions.
8. **Every accuracy-affecting change ships behind a feature flag with shadow-mode telemetry ≥7d** against the 100-query infrastructure suite + 10 manual multi-round tests before default-on flip.
9. **Catalog stays at 0% participation in runtime indicator code resolution** (per `indicator_selector.py:8` stance and `tests/test_no_semantic_shortcut_rules.py`). `catalog_validator.py` is CI-only metadata-integrity tooling.
10. **Cache TTLs are content-driven** (per-provider freshness), not layer-driven. The `Namespace` enum carries TTL policy; `Layer` enum carries only memory-vs-Redis transport semantics.
11. **NEW: All providers raise `DataNotAvailableError` on parse failure**; `[]` means "API success with zero rows." (Phase 3)
12. **NEW: Sync `/api/query` MUST have an `asyncio.wait_for` deadline**; default 120s, configurable via `QUERY_TIMEOUT_SECONDS`. (Phase 0)
13. **NEW: X-Forwarded-For trusted only from `TRUSTED_PROXIES`**; localhost bypass requires no XFF header AND a trusted-local client IP. (Phase 0)
14. **NEW: Frontend SSE parser MUST flush the trailing buffer on stream end**; per-stream callbacks MUST be scoped to a query ID. (Phase 1)

---

## 7. Anti-Patterns to Avoid (Explicit "Don't Do This" List)

1. **Don't bundle UnifiedRouter's semantic keyword-list removal into the hybrid_router.py deletion commit.** Conflating tier consolidation with rule removal masks scope.
2. **Don't auto-generate a 5K+ entry "primary provider/code per concept" catalog.** Industrializes the hardcoded-mapping anti-pattern; duplicates `indicators.db`; drifts.
3. **Don't move `SERIES_MAPPINGS`, `DATASET_DEFAULT_FILTERS`, or `COUNTRY_MAPPINGS` into YAML.** Relocating hardcoded rules into a different file format does not eliminate them.
4. **Don't replace `ConversationState.resolved_indicator_code` with full LLM regeneration every turn.** Re-resolving "CPI" against FRED's 80+ variants each follow-up is non-deterministic, threatens 95% accuracy, and incurs O(turns²) token cost. Use the confidence-gated Tier-3 rewrite instead.
5. **Don't add a "catalog hint as advisory LLM prompt string"** to the indicator-resolution prompt. Even an advisory hint that injects concept→code text recreates the semantic shortcut surface `tests/test_no_semantic_shortcut_rules.py` guards against.
6. **Don't collapse `/api/query` into an SSE-draining adapter.** Six test files including `comprehensive_100_queries.py` and the MCP `query_data` operation depend on the sync contract.
7. **Don't extract a mandatory `SDMXBaseProvider` that all providers inherit.** Seven of ten providers are not SDMX-shaped; composition via opt-in utility module only.
8. **Don't move to multi-worker uvicorn in conjunction with the cache refactor.** Currently moot; conflating turns pure cleanup into deployment-shape change.
9. **Don't refactor `ChatPage.tsx` without a second concrete consumer of streaming logic.** YAGNI; the ~600-LOC export-to-Python codegen string section is not React logic.
10. **Don't build a Jinja-based prompt registry.** Pydantic schemas via Instructor already make schemas the source of truth on 4 of 5 prompt sites.
11. **Don't keep the four `delta_extractor _try_*_noop` handlers "just in case."** They encode hardcoded indicator-code sets and substring checks — direct Rule-1 violations.
12. **Don't re-enable `apply_concept_provider_override` or `apply_catalog_availability_override` as anything other than identity functions.** Tests intentionally guard against this.
13. **NEW: Don't pre-seed a "canonical core indicator allowlist."** Same anti-pattern as the catalog primary fields. Replace with FTS5 keyword enrichment + RRF (Phase 2).
14. **NEW: Don't add a Jinja prompt-registry layer just to centralize 5 prompts.** Schema-first via Pydantic + Instructor already covers 4 of 5.

---

## 8. Gaps Both Audits Missed (from Completeness Critic)

1. **No CI/CD pipeline.** No `.github/workflows/`. No coverage gates. → **Phase 0** adds CI with `pytest-cov --cov-fail-under=65`.
2. **No structured logging.** Python logging used but no JSON / no trace IDs / no Sentry. → **Phase 0** structured logger; trace_id per request. Required to validate Phase 2 shadow-mode.
3. **No LLM token-cost tracking.** Spend is unmeasured. → **Phase 1.6** (above).
4. **Database performance unaudited.** `indicators.db` is 903 MB; no EXPLAIN audit; index strategy undocumented. → **Phase 2 prerequisite:** run `ANALYZE; PRAGMA optimize;` and verify FTS5 vocab.
5. **LLM prompt injection vector.** User query f-string-interpolated into prompts at `simplified_prompt.py:220` and `delta_extractor.py:915–996`. → **Phase 1.3** `_sanitize_context` helper.
6. **No indicator freshness tracking.** `indicators.db` updated 2026-04-26 but no version file. → **Phase 3 add-on:** write `indicator_catalog_version.json` per provider on fetch.
7. **Health check doesn't validate dependencies.** Returns `{"status":"healthy"}` regardless of Redis/Supabase/LLM state. → **Phase 0.6**.
8. **Mobile + WCAG accessibility.** No viewport meta; sparse ARIA. → **Phase 5 deferred unless launch demand.**
9. **MCP endpoint undocumented.** No versioning; no schema docs. → **Phase 4 add-on** (low priority).
10. **Dependency hygiene.** 63 MB unused (`semantic-router`, `deepagents`). → **Phase 1** removes when dead routers go.

---

## 9. Quick-Win Punch List (low risk, high yield, can ship in Phase 0–1)

| # | Item | File:line | Effort | Why now |
|---|---|---|---|---|
| Q1 | Add `asyncio.wait_for(120s)` to sync `/api/query` | `backend/main.py:669–748` | 1h | 5 finders flagged; security + UX |
| Q2 | Fix X-Forwarded-For spoofing | `backend/main.py:262–269` | 2h | Security |
| Q3 | Production fail-closed on missing Supabase | `backend/services/supabase_service.py:281–293` | 1h | Security; matches existing `auth_factory.py` pattern |
| Q4 | Delete `hybrid_router.py` + `semantic_provider_router.py` + 7 dead flags | `backend/routing/` + `backend/config.py:58–94` | 3h | -765 LOC, -63 MB deps, 0 behavior change |
| Q5 | Delete `indicator_lookup.py` orphan stub | `backend/services/indicator_lookup.py` | 15min | Zero callers |
| Q6 | Frontend SSE buffer-flush + null-check + scope-ID | `packages/frontend/src/services/api.ts:207–266` | 3h | All streaming queries; no UI refactor |
| Q7 | Gate `_provider_matrix()` on `include_provider_hints` | `backend/services/simplified_prompt.py:404–430` | 1h | -690 tokens per intent parse |
| Q8 | `_sanitize_context()` for user query interpolation | `backend/services/simplified_prompt.py:220` | 2h | Prompt-injection mitigation |
| Q9 | `/api/health` deep ping (Redis + Supabase + LLM) | `backend/main.py:597–606` | 2h | Operability |
| Q10 | Register `RequestValidationError` handler | `backend/main.py` | 1h | User-facing error UX |

**Total Phase 0+ Quick Wins: ~16 hours of work for ~10pp of cumulative reliability/security/quick-LLM-cost lift.** All compatible with each other; can land as 2 PRs.

---

## 10. What's Excluded From This Cycle

- **OECD-specific work.** Low priority (60 req/hr rate limit).
- **Frontend redesign.** Only surgical bug fixes in `api.ts`. `ChatPage.tsx` is correctly decomposed at the right altitude already.
- **`QueryService` decomposition.** Deferred to Phase 5; no second consumer.
- **`indicator_clarification` / `indicator_resolution` split.** Same logic.
- **Mobile / i18n.** Defer unless launch-driven demand.

---

## 11. Implementation Guidance (for the next session)

Before any change in Phase 1 or later:

1. **Baseline measurement:** Run `python3 backend/tests/comprehensive_100_queries.py` and the 10×10 multi-round test. Record current pass rates.
2. **Shadow-mode protocol:** every accuracy-affecting flag stays at `false` for ≥7 days with telemetry to `/api/admin/indicator_telemetry` (Phase 2.1). Default-on flip requires:
   - ≥7d of production shadow data
   - 100-query suite ≥ baseline
   - 10×10 multi-round ≥ baseline
   - No new failures in `tests/test_no_semantic_shortcut_rules.py`
3. **Per-PR checklist:**
   - [ ] Doesn't add to any of: `SERIES_MAPPINGS`, `KEYWORD_*`, `_FISCAL`, `_CRYPTO`, catalog `explicit_exclusions`, `_INDICATOR_ALIAS_*`
   - [ ] Doesn't introduce regex/keyword semantic filters (structural parsing only)
   - [ ] Doesn't break the 6 test files posting to `/api/query`
   - [ ] Preserves multi-round invariants (item #3 in §6)
   - [ ] Includes test against `tests/test_no_semantic_shortcut_rules.py`

---

## 12. Audit Trail

- **Workflow A** transcript dir: `/home/hanlulong/.claude/projects/-home-hanlulong-OpenEcon/83938b99-0636-4c9a-a36d-6a609d5a968c/subagents/workflows/wf_3e1db3e9-c61`
- **Workflow B** transcript dir: `/home/hanlulong/.claude/projects/-home-hanlulong-OpenEcon/83938b99-0636-4c9a-a36d-6a609d5a968c/subagents/workflows/wf_f0eecac3-762`
- **Raw findings JSON:** `/tmp/wfA_findings/_deduped.json` (101 deduped findings recovered from Workflow A finders)
- **Workflow B output:** `/tmp/wfA_findings/_workflow_B_compact.json`
- **Total agent count across both workflows + Workflow C:** 333 (288 in A, 41 in B, 4 in C)
- **Total token spend:** ~8.8M output tokens

---

*Authored: 2026-05-30. Replaces and supersedes `docs/KEY_PROBLEMS.md` (2026-04-11), which can be kept for historical context but should no longer be the primary planning document.*
