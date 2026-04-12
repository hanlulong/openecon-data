# Query Outcome Implementation Roadmap

**Status:** PHASE-GATED EXECUTION CONTRACT  
**Date:** 2026-04-11  
**Derived from:** `.omx/plans/plan-session-restart-2026-04-11-outcome-guarantee-consensus.md`  
**Depends on:** [QUERY_OUTCOME_GUARANTEE_SYSTEM.md](./QUERY_OUTCOME_GUARANTEE_SYSTEM.md), [SEMANTIC_RUNTIME_BOUNDARY.md](./SEMANTIC_RUNTIME_BOUNDARY.md), [SESSION_RESTART_2026-04-11.md](./SESSION_RESTART_2026-04-11.md)

---

## Why This Roadmap Exists

Turn the outcome-guarantee design into a binding, phase-gated implementation contract for the current codebase.

The target behavior remains:

- answer directly when the request is clear and provable
- ask compact high-value clarifications by default, but allow up to 10 concrete options when true ambiguity breadth requires it
- never silently return a semantically wrong indicator, wrong transform, wrong comparison shape, or incomplete ranking
- preserve correct multi-round state without drift
- surface ranked alternative explorations after successful answers on the new path

The target engineering behavior remains:

- no individual query fixes
- no expanding static mappings as the primary strategy
- no hard-coded semantic rule patches in the runtime path
- no rollout without exact-output verification and provider-regression evidence
- improvements must raise system-level accuracy on broad query classes
- every implementation cycle must include parallel review plus one concrete system improvement

---

## Non-Negotiable Program Rules

These rules govern every implementation phase:

1. The control path must end in exactly one of:
   - `DIRECT_ANSWER`
   - `CLARIFY`
   - `UNSUPPORTED`
2. `DIRECT_ANSWER` remains blocked unless execution feasibility is known and verification succeeds.
3. Deterministic runtime logic stays structural-only; semantic meaning remains model-backed or schema-backed.
4. Conversation truth must be verified truth; state cannot be committed before verification success.
5. Every implementation cycle must include:
   - 2–3+ parallel review agents
   - at least one concrete improvement over the prior cycle
   - feature-flag-safe old/new comparison on the same evaluation slice
6. Clarification policy should default to compact options, but it may surface up to **10** concrete options when real ambiguity breadth requires it.
7. Before any commit on runtime semantics, run 10 manual multi-round chains.

---

## Current Codebase Map

1. **One semantic decision-maker, not scattered heuristics.**
   The runtime must converge toward one shared outcome-decision contract instead of
   allowing `query.py`, clarification code, resolver selection, and fetch-time fallback
   logic to make independent semantic judgments.
2. **Direct answers must be provable.**
   The control path must end in explicit `DIRECT_ANSWER`, `CLARIFY`, or `UNSUPPORTED`.
   `DIRECT_ANSWER` is allowed only after execution and verification succeed on the new path.
3. **Conversation truth must be verified truth.**
   Conversation state must represent verified semantics, not pre-verification fetch intent.
4. **Semantic understanding must stay model-backed or schema-backed.**
   Deterministic logic may enforce structural constraints, execution legality, budgets,
   and shape checks, but not semantic rule patches.
5. **Every cycle must actively improve the broad system.**
   Each implementation cycle must include 2–3+ parallel review agents and at least one
   concrete improvement rather than only measurement or commentary.

---

## Target Runtime Architecture

The latest reports show the highest-value structural failures:

1. strict correctness is still much lower than effective pass rate
2. direct answers sometimes return semantically wrong indicators
3. clarification is present but not yet the primary control gate
4. multi-round decomposition still drifts
5. rankings and transforms are not verified strongly enough
6. evaluation and rollout gates are still too implicit to safely govern architectural migration

This means the first work should not be another routing tweak.

The first work should be:

1. mandatory Phase 0 roadmap hardening plus evaluation contract
2. semantic decision backbone
3. minimal typed `ExecutionPlan` skeleton plus post-fetch verification and staged state commit

---

## Target Runtime Architecture In The Current Repo

The shortest viable migration path is:

```text
query -> parse -> session-aware candidate retrieval -> semantic decision stage
     -> if clarify: clarification response
     -> if direct: build execution plan -> execute -> verify
     -> if verified: persist state -> respond
     -> else: clarify or unsupported
```

The important change is that fallback, recovery, clarification, and verification become explicit runtime stages instead of loosely connected post-hoc repairs.

---

## Required Evaluation Contract

### Provider coverage matrix

Every later implementation phase must be evaluated against, at minimum:

- FRED
- World Bank
- IMF
- BIS
- Eurostat
- StatsCan
- Comtrade
- CoinGecko
- ExchangeRate

### Exact-output checks

Every later implementation phase must preserve or improve these checks:

- exact indicator correctness
- exact group membership / country completeness
- per-series data sufficiency and requested time-range coverage
- value-range sanity
- ambiguity behavior that clarifies instead of silently guessing
- multi-round state retention
- provider-regression safety

### Process gates per cycle

Every implementation cycle must document:

- which 2–3+ parallel review agents were used
- one concrete improvement made in that cycle
- the feature flags used to compare old/new behavior
- the exact-output evidence bundle collected for the cycle

### Standing rule to phase/checklist mapping

| Standing rule                                                        | Bound phase/checklist                                                          |
| -------------------------------------------------------------------- | ------------------------------------------------------------------------------ |
| No individual query fixes as the main strategy                       | Phase 1 through Phase 5 non-goals and code-review checklist                    |
| No hard-coded semantic rule patches in runtime logic                 | Phase 1 through Phase 5 non-goals; semantic-decision and verification reviews  |
| `DIRECT_ANSWER` requires provable execution + verification           | Phase 1 outcome gating, Phase 2 typed-plan + verifier exit criteria            |
| Verified truth only / no pre-verification state commit               | Phase 2 exit criteria, Phase 4 state-model hardening                           |
| Clarification policy defaults compact but can widen up to 10 options | Phase 1 deliverables, Phase 6 evaluation evidence                              |
| 2–3+ parallel review agents every cycle                              | Required process gate for every implementation cycle                           |
| One concrete improvement every cycle                                 | Required process gate for every implementation cycle                           |
| Exact-output verification, not just non-empty results                | Required evaluation contract; Phase 2 and Phase 6 evidence bundles             |
| 10 manual multi-round chains before runtime-semantic commit          | Runtime-semantic commit gate in Phases 1–6; rollout evidence in Phase 6        |
| Post-answer alternative exploration after successful answers         | Phase 6 deliverable and rollout evidence                                       |
| Broad cross-provider accuracy and regression safety                  | Required provider matrix; Phase 5 migration evidence; Phase 6 rollout evidence |

---

## Evidence Bundles Used Across Phases

The implementation direction should be:

### Evidence Bundle A — Strict Output Correctness

Must include exact-output checks for:

- exact indicator identity
- transform correctness
- ranking completeness
- comparison cardinality
- breakdown/group completeness
- time coverage sufficiency
- value sanity / impossible-range checks

### Evidence Bundle B — Provider Matrix

Must report results across at least the core providers in the matrix below and include
provider-specific regressions, if any.

### Evidence Bundle C — Ambiguity / Clarification Quality

Must show:

- unresolved semantics produce `CLARIFY` rather than a blind fetch
- clarification options stay compact by default
- up to **10** concrete options are allowed only when ambiguity breadth truly requires it
- clarification quality is measured, not assumed

### Evidence Bundle D — Multi-Round Safety

Must show:

- verified-only state advancement on the new path
- decomposition survives follow-ups
- additive/removal/focus shifts do not poison later turns

### Evidence Bundle E — Process Discipline

Must show:

Suggested API:

```python
class OutcomeDecision(BaseModel):
    outcome: Literal["direct_answer", "clarify", "unsupported"]
    reason: str
    options: list[ClarificationOption] = []
    confidence: float
    selected_candidate_id: str | None = None
```

Rule:

- semantic decisions must be model-backed or schema-backed
- deterministic code here should only enforce structural constraints such as option count, provider legality, and question budget

### 2. Candidate Evidence Builder

Suggested new module:

- `backend/services/candidate_evidence_builder.py`

Purpose:

- collect the evidence bundle used by the semantic decision layer
- expose comparable candidate summaries rather than raw resolver internals
- make semantic decisions explainable and testable without adding cue rules

Suggested API:

```python
class CandidateEvidence(BaseModel):
    candidate_id: str
    provider: str
    code: str
    title: str
    description: str
    transform: str | None = None
    unit: str | None = None
    frequency: str | None = None
    geography_scope: str | None = None
    coverage_summary: dict[str, Any] = {}
```

### 3. Minimal Typed `ExecutionPlan` Skeleton, Then Full Planner Boundary

Early purpose:

- define provider choice, expected result shape, and verification expectations explicitly enough to enforce verified truth only

Later purpose:

- become the durable boundary between user semantics and provider execution across the new runtime path

Suggested module:

- yield spread answered by a single yield series
- M1 answered by M2
- ranking answered with insufficient data

### 4. `backend/services/execution_planner.py`

Purpose:

- first land a **minimal typed `ExecutionPlan` skeleton** that carries provider choice, expected result shape, and verification expectations
- then expand that skeleton into the full provider-ready `ExecutionPlan` boundary
- separate semantic intent from provider params

Suggested API:

```python
class ExecutionPlanner:
    def build_plan(
        self,
        session_state: SessionState,
        candidate: CandidateIndicator,
    ) -> ExecutionPlan:
        ...
```

### 4. Post-Fetch Verification Layer

Primary home:

- extend [backend/services/semantic_match_judge.py](/home/hanlulong/OpenEcon/backend/services/semantic_match_judge.py:1)
- pair it with structural validators in existing execution code

Purpose:

- verify semantic correctness after execution
- reject near-miss indicator matches
- verify output shape and completeness
- enforce transform-specific checks without hard-coded semantic cue maps
- block promotion of unverified truth

This is the main system-level defense against false passes such as:

- yield spread answered by a single yield series
- M1 answered by M2
- ranking answered with insufficient data

### 5. Decomposition And Verified/Tentative State Model

Primary home:

- [backend/services/conversation_state_v2.py](/home/hanlulong/OpenEcon/backend/services/conversation_state_v2.py:49)
- [backend/services/delta_extractor.py](/home/hanlulong/OpenEcon/backend/services/delta_extractor.py:1)

Purpose:

- make decomposition first-class instead of a side effect of generic dimensions
- preserve semantic state rather than fetch-time internals
- distinguish verified state from tentative state
- keep country/time/indicator/filter changes stable across turns

### 6. Clarification Policy

Suggested module:

- `backend/services/clarification_policy.py`

Purpose:

- enforce clarification budget and option width
- default to compact choices
- allow up to **10** options when true ambiguity breadth requires it
- support post-answer alternative exploration from ranked candidate evidence

---

## Core Provider Regression Matrix

The broad-accuracy target must be measured across this minimum proving-ground matrix.
Phase 0 defines the matrix; later phases populate it with results.

| Provider     | Why it is in the matrix                                | Minimum regression focus                            |
| ------------ | ------------------------------------------------------ | --------------------------------------------------- |
| FRED         | High-volume direct retrieval, transform confusion risk | levels vs growth, spreads, macro aggregates         |
| World Bank   | Broad catalog, metadata variability                    | country coverage, indicator identity                |
| IMF          | Macro aggregates with variant drift risk               | level vs rate, methodology mismatch                 |
| BIS          | Financial metrics and rate/spread ambiguity            | spreads, rates, maturity confusion                  |
| Eurostat     | Highest-drift structural provider                      | breakdown completeness, dataset dimension legality  |
| StatsCan     | Multi-round decomposition and cube dimensions          | province/member follow-ups, dimension carry-forward |
| Comtrade     | Directionality and product/country scoping             | imports vs exports, ranking completeness            |
| CoinGecko    | Market metrics with easy false matches                 | market cap vs price vs volume                       |
| ExchangeRate | FX direct retrieval baseline                           | base/quote correctness, date coverage               |

1. route all post-parse execution through the semantic decision stage
2. stop persisting delta-derived state before verification passes
3. separate direct-answer path from clarification and unsupported paths
4. move current uncertainty recovery calls behind semantic verification failure handling rather than using them as general routing logic
5. keep old paths behind feature flags during migration

---

## Feature-Flag Migration Strategy

All architectural changes must ship behind explicit flags so the old and new paths can
be compared on the same evaluation suite.

| Flag                            | Purpose                                          | Phase introduced |
| ------------------------------- | ------------------------------------------------ | ---------------- |
| `USE_OUTCOME_DECISION_STAGE`    | Enable the new shared prefetch decision stage    | Phase 1          |
| `USE_POST_FETCH_SEMANTIC_JUDGE` | Enable the new verification path after execution | Phase 2          |
| `USE_STAGED_STATE_COMMIT`       | Prevent unverified state promotion               | Phase 2          |
| `USE_EXECUTION_PLANNER`         | Enable the full planner boundary                 | Phase 3          |
| `USE_DECOMPOSITION_STATE_V3`    | Enable the new decomposition/state model         | Phase 4          |

**Migration rule:** old/new behavior must be runnable side-by-side until strict-correctness
and provider-regression evidence justify promotion.

---

## Standing Rule To Phase / Checklist Mapping

The long-lived memory rules are only useful if they are attached to concrete delivery gates.

| Standing rule                                                     | Bound phase/checklist                                                        |
| ----------------------------------------------------------------- | ---------------------------------------------------------------------------- |
| No heuristic semantic rule patches                                | Phase 1–6 code review checklist; reject provider-specific semantic shortcuts |
| Direct answers must be provable                                   | Phase 1 decision contract, Phase 2 verifier gate, Phase 6 rollout gate       |
| Verified truth only                                               | Phase 2 staged-state gate, Phase 4 state-V3 hardening                        |
| 10 manual multi-round chains before runtime-semantic commits      | Phase 2–6 Evidence Bundle E                                                  |
| 2–3+ parallel review agents each cycle                            | Phase 1–6 Evidence Bundle E                                                  |
| One concrete improvement each cycle                               | Phase 1–6 Evidence Bundle E                                                  |
| Default compact clarifications, up to 10 when breadth requires it | Phase 1 clarification policy, Phase 6 ambiguity evidence                     |
| Post-answer alternative exploration                               | Phase 6 product behavior + evaluation checks                                 |
| Broad provider accuracy, not isolated query wins                  | Provider matrix in Phase 5–6                                                 |

---

## Phase Plan

#### Phase 0 — Plan hardening and evaluation contract

**Hard pass/fail gate:** This phase is complete only when the roadmap itself names the approved phase order, non-goals, feature-flag expectations, provider matrix, exact-output checks, clarification-width rule, process gates, and post-answer alternative exploration requirement.

**Goal**

- convert the design direction into a binding implementation contract before more runtime changes land

**Must deliver**

- explicit phase order: Phase 1 semantic decision backbone; Phase 2 minimal typed `ExecutionPlan` skeleton + post-fetch verification + staged state commit; Phase 3 full `ExecutionPlan` / planner generalization; Phase 4 decomposition/state V3 hardening; Phase 5 provider migration; Phase 6 broad evaluation and rollout
- explicit non-goals, feature flags, and exit evidence for every phase
- regression matrix covering FRED, World Bank, IMF, BIS, Eurostat, StatsCan, Comtrade, CoinGecko, and ExchangeRate
- exact-output checks for indicator correctness, group membership, data sufficiency, value-range sanity, and ambiguity behavior
- clarification policy contract: compact by default, but up to 10 concrete options when true ambiguity breadth requires it
- process gates requiring 2–3+ parallel review agents per cycle and at least one concrete system improvement per cycle
- post-answer alternative exploration requirement, to be shipped and verified on the new path in Phase 6

**Non-goals**

- no runtime code changes
- no provider-specific migrations
- no heuristic hotspot sweeps disguised as planning

Feature-flag expectations:

- document the flags that will guard Phases 1-6 before implementation begins
- preserve old/new side-by-side comparisons throughout rollout

**Exit evidence required**

- roadmap text committed with the named phase gates, named non-goals, feature-flag safety rules, and named evidence bundles
- standing memory rules mapped to the owning phase/checklist
- evaluation matrix and exact-output checks written down in the roadmap itself

#### Phase 1 — Candidate evidence + semantic decision backbone

**Hard pass/fail gate:** This phase is complete only when the new prefetch path uses explicit direct/clarify/unsupported outcome decisions and blocks direct answers when candidate semantics remain unresolved.

**Goal**

- make candidate meaning and answerability explicit before fetch

**Must deliver**

- a shared `CandidateEvidence` contract and `OutcomeDecision` surface in the main query flow
- prefetch routing through explicit `DIRECT_ANSWER | CLARIFY | UNSUPPORTED` decisions
- a shared clarification-width policy that defaults to compact options but can present up to 10 concrete choices when true ambiguity breadth requires it
- enough control-contract scaffolding to hand Phase 2 a selected candidate/provider decision plus verification expectations

**Non-goals**

- no typed `ExecutionPlan` skeleton yet
- no full provider migration
- no decomposition/state V3 redesign
- no provider-specific exception systems as a shortcut for feasibility

**Feature flags**

- `USE_OUTCOME_DECISION_STAGE`
- `USE_SHARED_CLARIFICATION_POLICY`

**Exit evidence required**

- materially ambiguous requests clarify instead of fetching blindly on the new path
- the new path refuses `DIRECT_ANSWER` whenever candidate semantics remain unresolved
- old/new side-by-side behavior comparisons are captured behind feature flags

#### Phase 2 — Minimal typed `ExecutionPlan` skeleton + post-fetch verification + staged state commit

**Hard pass/fail gate:** This phase is complete only when verified-truth-only behavior is enforced through the typed skeleton, verification catches known false-pass classes, and state no longer advances before verification succeeds.

**Goal**

- make “verified truth only” enforceable with an explicit early execution boundary

**Must deliver**

- a minimal typed `ExecutionPlan` skeleton carrying provider choice, expected output shape, and verification expectations
- post-fetch semantic + structural verification against the fetched result and that typed skeleton
- tentative-versus-verified state handling early enough to stop pre-verification truth commits
- exact-output checks for group completeness, data sufficiency, and value sanity in the verification harness

**Non-goals**

- no broad planner/provider generalization yet
- no decomposition/state V3 redesign yet
- no broad provider migration yet

**Feature flags**

- `USE_MINIMAL_EXECUTION_PLAN`
- `USE_POST_FETCH_SEMANTIC_JUDGE`
- `USE_STAGED_STATE_COMMIT`

**Exit evidence required**

- known false-pass cases fail closed or clarify, including yield spread answered by a single yield series, M1 answered by M2, and ranking answered with insufficient data
- conversation state advances only after verification succeeds on the new path
- verification checks explicit typed-plan fields rather than inferred cue heuristics
- regression slices report direct-answer precision, false direct-answer rate, verification catch rate, and multi-round carry-forward

#### Phase 3 — Full execution planner boundary and provider-boundary generalization

**Hard pass/fail gate:** This phase is complete only when the Phase 2 typed skeleton expands into the durable planner boundary used by the new path.

**Goal**

- expand the Phase 2 typed skeleton into the durable planner boundary used across the new path

**Must deliver**

- full `ExecutionPlan` objects and planner interfaces
- provider-ready execution plans generalized from the Phase 2 skeleton
- broader provider consumption of plan semantics instead of raw mutated intent params

**Non-goals**

- no broad provider migration breadth yet
- no decomposition/state V3 redesign yet
- no fresh heuristic verifier shortcuts

**Feature flags**

- `USE_EXECUTION_PLANNER`

**Exit evidence required**

- `data_fetcher` consumes plan semantics instead of primarily raw mutated intent params on the new path
- provider adapters receive explicit instructions for at least one proving-ground path without silent semantic mutation

#### Phase 4 — Decomposition and state-model hardening (V3)

**Hard pass/fail gate:** This phase is complete only when decomposition becomes a first-class state concept and verified/tentative state semantics preserve cross-turn meaning.

**Goal**

- make decomposition and cross-turn semantics first-class, verified state concepts

**Must deliver**

- decomposition in the canonical state model
- durable verified/tentative state semantics evolved from the earlier safeguard
- explicit distinction between breakdown requests and member-filter changes in delta/state behavior

**Non-goals**

- no broad provider migration breadth yet
- no fallback to hidden `__...` params as the main cross-turn semantic carrier

**Feature flags**

- `USE_DECOMPOSITION_STATE_V3`

**Exit evidence required**

- “by province” and “Ontario only” remain stable, distinct transitions across follow-ups
- multi-round regression slices cover decomposition plus indicator/time/filter changes

#### Phase 5 — Provider migration under the new contracts

**Hard pass/fail gate:** This phase is complete only when high-drift providers run through the new contracts without provider-specific semantic exception systems.

**Goal**

- move the highest-drift providers onto the new contracts without reintroducing semantic exception systems

**Must deliver**

- provider adapters consuming `ExecutionPlan`
- structure-aware planning for high-risk providers such as Eurostat
- reduced hidden semantic mutation in fetch-time code

**Non-goals**

- no provider-specific symptom patches as the main rollout strategy
- no “temporary” semantic rules embedded in provider adapters

**Feature flags**

- `USE_PROVIDER_PLAN_EXECUTION`

**Exit evidence required**

- proving-ground provider migration evidence for at least Eurostat plus one non-Eurostat provider
- provider-regression gate results show no silently weakened coverage or strict-accuracy regressions

#### Phase 6 — Broad evaluation, regression hardening, rollout

**Hard pass/fail gate:** This phase is complete only when broad strict-correctness gains and provider-regression safety are evidenced and the required manual/review-cycle checks are attached to rollout.

**Goal**

- prove broad strict-correctness gains before promoting the new path

**Must deliver**

- cross-provider benchmark matrix results
- exact group-membership checks, per-series sufficiency checks, value-range checks, and ambiguity tests
- post-answer alternative exploration after successful answers using ranked alternatives from candidate evidence
- 10 manual multi-round chains before commit
- evidence that active-improvement and parallel-review rules were followed each cycle

**Non-goals**

- no rollout based only on “effective” pass rate or plausible-looking outputs
- no promotion of new flags while provider-regression evidence is unresolved

**Feature flags**

- `USE_RANKED_ALTERNATIVE_EXPLORATION`

**Exit evidence required**

- direct-answer precision, false direct-answer rate, verification catch rate, clarification resolution rate, and multi-round retention reported for the new path
- manual-chain pass/fail report and provider-regression report attached to the rollout decision

## Standing Memory Rules -> Phase / Checklist Mapping

The following standing rules remain binding throughout implementation and are mapped to explicit phase gates so they do not degrade into vague principles:

| Standing rule                                                                                                  | Phase/checklist binding                                                                                         |
| -------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------- |
| No individual query fixes or provider-specific symptom patches as the main strategy                            | Phase 0 non-goals; Phase 1/5 non-goals; Phase 6 rollout gate                                                    |
| No hard-coded semantic rules, cue maps, or static indicator mappings as the main strategy                      | Phase 0 non-goals; Phase 1/2/3 non-goals; verification review checklist each cycle                              |
| Direct answers must be provable and verified                                                                   | Phase 1 hard gate; Phase 2 hard gate; Phase 6 metrics                                                           |
| Only verified results become conversation truth                                                                | Phase 2 hard gate and exit evidence; Phase 4 state-model gate                                                   |
| Run 10 manual multi-round chains before commit                                                                 | Phase 6 must-deliver and rollout evidence                                                                       |
| Use 2–3+ parallel review agents every improvement cycle                                                        | Phase 0 process gate; Phase 6 evidence bundle                                                                   |
| Make at least one concrete improvement each cycle                                                              | Phase 0 process gate; Phase 6 evidence bundle                                                                   |
| Clarification should stay compact by default but may widen to 10 real options when ambiguity truly requires it | Phase 0 must-define clarification policy; Phase 1 shared clarification-width policy; Phase 6 ambiguity evidence |
| Post-answer alternative exploration is a product requirement                                                   | Phase 0 must-define; Phase 6 must-deliver and verify                                                            |
| Broad provider coverage and 95%+ directionally improving strict accuracy matter more than local wins           | Phase 0 provider matrix; Phase 5 migration evidence; Phase 6 benchmark matrix and rollout gate                  |

---

## Evaluation Contract And Provider Matrix

### Required provider matrix

Every phase that claims runtime progress must report results for at least these providers when they are in scope:

- FRED
- World Bank
- IMF
- BIS
- Eurostat
- StatsCan
- Comtrade
- CoinGecko
- ExchangeRate

### Exact-output checks

The evaluation harness must score more than non-empty responses. It must verify:

- indicator correctness
- group membership completeness
- data sufficiency for rankings, comparisons, and decompositions
- value-range sanity
- ambiguity behavior: clarify when unresolved, answer directly only when provable
- multi-round carry-forward correctness

### Known false-pass classes that must stay in the suite

- yield spread answered by a single yield series
- M1 answered by M2
- ranking answered with insufficient coverage
- group request answered with missing members
- methodology mismatch across providers

---

## Standing Rule Mapping

The roadmap must keep the standing user-memory rules attached to explicit phases/checklists:

- **No individual query fixes / no static mapping strategy** → applies to all phases; enforce in Phase 0 review checklists and Phase 5 migration reviews
- **No heuristic semantic runtime patches** → enforce in Phase 1 decision backbone, Phase 2 verifier, and Phase 5 provider migration reviews
- **Verified truth only** → Phase 2 exit gate and Phase 4 state-model hardening
- **10 manual multi-round chains before commit** → Phase 6 rollout gate and any runtime-semantics commit checklist
- **2–3+ parallel review agents each cycle** → every implementation cycle, captured in Phase 0/6 process evidence
- **At least one concrete improvement per cycle** → every implementation cycle, captured in Phase 0/6 process evidence
- **Compact clarification by default, but up to 10 when truly needed** → Phase 1 implementation and Phase 6 ambiguity verification
- **Post-answer alternative exploration** → Phase 6 product requirement and verification bundle
- **95%+ broad accuracy push across providers / indicator space** → program-level benchmark target, tracked in Phase 6 reporting

---

## Evaluation Contract And Provider Matrix

### 1. Add adversarial near-miss tests

New mandatory test sets should include:

- direct retrieval
- transform / derived metrics
- ranking
- breakdown / decomposition
- comparison
- multi-round state transitions
- ambiguous clarification cases

### Required adversarial near-miss cases

- growth vs level
- spread vs single series
- real vs nominal
- core vs headline
- ranking with incomplete coverage
- group query with missing members
- methodology mismatch across providers
- exact indicator correctness
- group membership correctness
- per-series sufficiency / completeness
- value-range sanity
- ambiguity behavior

### 2. Split metrics

Track at least:

- direct-answer precision
- session outcome accuracy
- clarification rate
- false direct-answer rate
- verification catch rate
- multi-round state retention
- provider-regression rate
- alternative-exploration coverage after successful answers

### 3. Preserve existing strong areas

Do not regress areas that already perform well in recent reports:

- crypto
- FX direct retrieval
- trade flows
- regional expansion
- basic multi-round additive country changes

### 4. Manual multi-round verification

Before any commit that changes runtime semantics:

- run 10 manual multi-round chains
- include decomposition, transform, provider switch, additive/removal flows, and ambiguity follow-ups
- verify returned data, not just response shape

### 5. Process evidence per cycle

Every implementation cycle must attach:

- evidence from 2–3+ parallel review agents
- one concrete improvement that landed because of that cycle
- old/new comparisons on the same flagged evaluation slice

---

## Recommended Feature-Flag Strategy

Introduce new flags rather than replacing the current system in one step.

Suggested flags:

- `USE_OUTCOME_DECISION_STAGE`
- `USE_SHARED_CLARIFICATION_POLICY`
- `USE_MINIMAL_EXECUTION_PLAN`
- `USE_POST_FETCH_SEMANTIC_JUDGE`
- `USE_STAGED_STATE_COMMIT`
- `USE_EXECUTION_PLANNER`
- `USE_DECOMPOSITION_STATE_V3`
- `USE_PROVIDER_PLAN_EXECUTION`
- `USE_RANKED_ALTERNATIVE_EXPLORATION`

Migration rule:

- keep old behavior available during rollout
- compare old and new behavior on the same evaluation suite
- only promote a new path when strict correctness improves and provider-regression evidence is green

---

## First Runtime Work Packages After Phase 0

Phase 0 is mandatory before runtime implementation resumes. Once it is complete, the first two runtime work packages should be:

### Work Package A: Candidate Evidence + Outcome Decision

Deliverables:

- semantic decision flow using `semantic_match_judge.py`
- candidate evidence builder
- integration into `process_query()`
- compact clarification generation with the bounded-width rule
- explicit refusal to direct-answer unresolved semantics

Reason:

- this directly reduces false direct answers
- it establishes the control contract Phase 2 depends on

### Work Package B: Minimal Typed Plan + Verification + Staged Commit

Deliverables:

- minimal typed `ExecutionPlan` skeleton
- model-backed semantic verification
- transform-aware structural verification
- output-shape and completeness verification
- fail-closed direct-answer policy plus verified-only state commits

Reason:

- this is the shortest path to “verified truth only”
- it blocks the known strict-vs-effective accuracy failures before the full planner lands

These two packages should land before full planner generalization, decomposition V3 hardening, or broad provider migration.

---

## Bottom Line

The path to broad outcome accuracy in this repo is not:

- more special cases
- more fallback chains
- more prompt text
- more provider-specific exceptions

It is:

1. Phase 0 plan hardening plus a binding evaluation contract
2. better ambiguity control before fetch
3. a minimal typed execution boundary that enforces verification and verified-only state commits
4. a full planner boundary between semantics and provider execution
5. decomposition/state hardening, provider migration, and broad regression evidence before rollout

This roadmap is designed to get there incrementally without hiding architectural work inside local heuristics or unevidenced rollouts.
