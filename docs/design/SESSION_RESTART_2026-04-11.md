# Session Restart Handoff

**Date:** 2026-04-11  
**Purpose:** Resume the outcome-guarantee architecture work using the approved
consensus plan and the new phase-gated execution contract.

## First Read On Restart

Read these before making changes:

1. [CLAUDE.md](/home/hanlulong/OpenEcon/CLAUDE.md)
2. [TESTING_PROMPT.md](/home/hanlulong/OpenEcon/TESTING_PROMPT.md)
3. [/home/hanlulong/.claude/projects/-home-hanlulong-OpenEcon/memory/MEMORY.md](/home/hanlulong/.claude/projects/-home-hanlulong-OpenEcon/memory/MEMORY.md)
4. [docs/design/SEMANTIC_RUNTIME_BOUNDARY.md](/home/hanlulong/OpenEcon/docs/design/SEMANTIC_RUNTIME_BOUNDARY.md)
5. [docs/design/QUERY_OUTCOME_GUARANTEE_SYSTEM.md](/home/hanlulong/OpenEcon/docs/design/QUERY_OUTCOME_GUARANTEE_SYSTEM.md)
6. [docs/design/QUERY_OUTCOME_IMPLEMENTATION_ROADMAP.md](/home/hanlulong/OpenEcon/docs/design/QUERY_OUTCOME_IMPLEMENTATION_ROADMAP.md)
7. [/home/hanlulong/OpenEcon.omx-team-launch-20260412T022105Z/.omx/plans/plan-session-restart-2026-04-11-outcome-guarantee-consensus.md](/home/hanlulong/OpenEcon.omx-team-launch-20260412T022105Z/.omx/plans/plan-session-restart-2026-04-11-outcome-guarantee-consensus.md)
8. [/home/hanlulong/OpenEcon.omx-team-launch-20260412T022105Z/.omx/plans/prd-session-restart-2026-04-11-outcome-guarantee.md](/home/hanlulong/OpenEcon.omx-team-launch-20260412T022105Z/.omx/plans/prd-session-restart-2026-04-11-outcome-guarantee.md)
9. [/home/hanlulong/OpenEcon.omx-team-launch-20260412T022105Z/.omx/plans/test-spec-session-restart-2026-04-11-outcome-guarantee.md](/home/hanlulong/OpenEcon.omx-team-launch-20260412T022105Z/.omx/plans/test-spec-session-restart-2026-04-11-outcome-guarantee.md)
10. [docs/KEY_PROBLEMS.md](/home/hanlulong/OpenEcon/docs/KEY_PROBLEMS.md)

## Non-Negotiable Rules

- Do not add hard-coded semantic filters, cue maps, variant keyword lists, or regex-style semantic follow-up rules in the runtime path.
- Use LLM/schema/session-state logic for semantic understanding. Deterministic code is only for structural constraints, execution safety, and exact-output checks.
- Do not fix individual queries. Only make framework-level changes that generalize across the broad provider and indicator space.
- Do not add static indicator mappings as the primary fix. Improve discovery, retrieval, candidate evidence, semantic decisioning, planning, verification, or state handling instead.
- Every change should improve the architecture, not patch symptoms.
- Verify returned data is correct, not just non-empty.
- Before any commit that changes runtime semantics, run the required manual multi-round testing. The user’s standing rule is 10 manual multi-round chains minimum.
- Follow the roadmap phase order exactly: Phase 0 plan hardening first, then Phase 1 semantic decision backbone, then Phase 2 minimal typed plan + verification + staged commit.
- Keep runtime changes behind feature flags and compare old/new behavior on the same evaluation slice before promotion.
- Every implementation cycle must include 2–3+ parallel review agents and at least one concrete improvement from that cycle.
- The correct Python environment is `backend/.venv/`. Use `backend/.venv/bin/python`, not the system interpreter.

## What Phase 0 Must Lock In

Phase 0 is complete only when the checked-in roadmap explicitly defines:

- answer directly when the request is clear and provable
- ask compact high-value clarifying questions by default, but allow up to 10 concrete options when true ambiguity breadth requires it
- avoid silent wrong indicator selection
- preserve correct multi-round state
- surface meaningful alternative explorations after successful answers

Runtime work should proceed only after those items are locked in.

## Current Architecture Goal

The active objective is still to make the system reliably:

- answer directly when the request is clear, executable, and provable
- clarify when materially different interpretations remain
- fail closed when execution or verification cannot support a correct answer
- preserve verified multi-round state
- support post-answer alternative exploration after successful answers

The design direction is still not “more rules.” It is:

- broad candidate retrieval
- shared semantic decisioning
- typed execution boundaries
- post-fetch verification
- staged state commit
- post-answer alternative exploration after successful answers once the new path reaches Phase 6

## Current Workflow Structure

The live flow today is still approximately:

```text
query
  -> parse + route
  -> merge conversation state
  -> provider override / availability checks
  -> indicator resolution
  -> prefetch clarification decision
  -> provider fetch
  -> rerank / fallback / post-fetch clarification
  -> persist state
```

The main structural weakness remains the same: semantic decisions are still spread across
multiple places instead of one evidence-driven decision stage.

## Important Runtime State Already Reached

The cleanup already completed these important moves:

- pending indicator-choice follow-up classification now reuses the delta extractor instead of phrase-based follow-up heuristics
- provider fallback now splits structural validity from semantic acceptance
- prefetch resolved-indicator acceptance now uses the shared model-backed semantic judge
- fetch-time resolved-indicator acceptance now uses the shared model-backed semantic judge
- direct translation bypass now uses the model-backed semantic judge before it can skip clarification

These changes are useful, but they are not the full architecture.
The next implementation work must fit the approved phase order rather than continuing as isolated cleanup.

## Standing Rules Bound To Later Phases

- [backend/services/semantic_match_judge.py](/home/hanlulong/OpenEcon/backend/services/semantic_match_judge.py)
  New model-backed semantic judgment module.

- [backend/services/provider_fallback.py](/home/hanlulong/OpenEcon/backend/services/provider_fallback.py)
  Fallback relevance is now structural-only. Semantic acceptance moved out of this module.

- [backend/services/query.py](/home/hanlulong/OpenEcon/backend/services/query.py)
  Added runtime wrappers for model-backed semantic judging, including async direct translation verification.

- [backend/services/indicator_clarification.py](/home/hanlulong/OpenEcon/backend/services/indicator_clarification.py)
  Removed the earlier hard-coded follow-up heuristic. Added:
  - model-backed pending reply classification via delta extractor
  - structural-only direct translation helper
  - async verified direct translation helper
  - prefetch path now uses verified direct translation

- [backend/services/indicator_resolution.py](/home/hanlulong/OpenEcon/backend/services/indicator_resolution.py)
  Fetch-time resolution now uses the shared model-backed judge in the main async acceptance path, including qualifier recovery through the async verified direct translation helper.

- [backend/tests/test_query_service.py](/home/hanlulong/OpenEcon/backend/tests/test_query_service.py)
  Updated and added regression coverage for fallback judging, prefetch clarification, fetch-time resolution, and async direct-translation verification.

### Design/docs

- [docs/design/SEMANTIC_RUNTIME_BOUNDARY.md](/home/hanlulong/OpenEcon/docs/design/SEMANTIC_RUNTIME_BOUNDARY.md)
  Active runtime rule: no semantic rule patches in runtime logic.

- [docs/design/QUERY_OUTCOME_GUARANTEE_SYSTEM.md](/home/hanlulong/OpenEcon/docs/design/QUERY_OUTCOME_GUARANTEE_SYSTEM.md)
  Explains the current workflow structure, where drift happens, and the intended outcome-guarantee architecture.

- [docs/design/QUERY_OUTCOME_IMPLEMENTATION_ROADMAP.md](/home/hanlulong/OpenEcon/docs/design/QUERY_OUTCOME_IMPLEMENTATION_ROADMAP.md)
  Updated to replace the old “add ambiguity_gate.py/query_verifier.py” idea with a shared semantic decision layer plus candidate evidence builder.

## Exact New Runtime Boundary

These are the current intended semantics:

- Structural helper:
  `get_direct_provider_indicator_translation()`
  may discover a provider-native code, but it must not make semantic acceptance decisions.

- Runtime acceptance helper:
  `get_verified_direct_provider_indicator_translation()`
  is allowed to approve a direct translation only after the model-backed semantic judge accepts it.

- The query service wrapper:
  `_get_direct_provider_indicator_translation_async()`
  is the path fetch-time logic should use whenever a translation result could change execution behavior.

## Active Phase Gate

- **Current active phase:** Phase 0 — Plan Hardening And Evaluation Contract
- **Blocking rule:** do not proceed to Phase 1 runtime work until the roadmap docs explicitly encode the gated phase order, non-goals, provider matrix, exact-output checks, review-agent/process rules, clarification-width rule, post-answer alternative exploration requirement, and feature-flag comparison safety.
- **Migration rule:** keep old/new behavior comparable behind feature flags and collect exact-output evidence before promoting the new path.

## Remaining Legacy Hotspots

These are the next architectural weak points. Do not expand them with more rules.

1. [backend/services/indicator_resolution.py](/home/hanlulong/OpenEcon/backend/services/indicator_resolution.py)
   Remaining issues:
   - `is_resolved_indicator_plausible()`
   - `has_implausible_top_series()`
   - variant/frequency keyword heuristics
   - cue-based precision thresholds

2. [backend/services/indicator_clarification.py](/home/hanlulong/OpenEcon/backend/services/indicator_clarification.py)
   Remaining issues:
   - `collect_indicator_choice_options()` still filters with cue compatibility and legacy plausibility logic
   - some clarification option generation is still too heuristic-heavy

3. [backend/services/relevance_scorer.py](/home/hanlulong/OpenEcon/backend/services/relevance_scorer.py)
   Remaining issues:
   - semantic cue extraction
   - hand-tuned semantic penalties/boosts

4. State management
   Remaining issue:
   - state can still be committed too early relative to semantic verification certainty

## Recommended Next Step

The next required slice is Phase 0 from `docs/design/QUERY_OUTCOME_IMPLEMENTATION_ROADMAP.md`:

1. Rewrite the roadmap into a binding implementation contract with the consensus phase order and exit evidence.
2. Lock the evaluation contract: provider matrix, exact-output checks, clarification-width rule, review-agent requirement, active-improvement requirement, and post-answer alternative exploration requirement.
3. Only after Phase 0 evidence exists should runtime work continue with Phase 1 candidate evidence + semantic decision backbone.

After Phase 0 is complete, the first runtime hotspots to address remain the same:

- reduce heuristic acceptance logic in `collect_indicator_choice_options()` via the shared decision contract
- replace remaining plausibility-matrix acceptance logic with the shared model-backed semantic judgment path
- keep semantic acceptance logic converging into `semantic_match_judge.py` rather than creating new guardrail modules

## Verification Discipline On Restart

For the current execution cycle, keep the following discipline:

- run focused verification for the files you change
- run full type/build checks when applicable
- if runtime semantics change, run the manual multi-round requirement before commit
- document PASS/FAIL evidence for each verification step
- if a phase gate is not evidenced yet, stop cleanly instead of skipping ahead

## Restart Commands

Use these first:

```bash
cd /home/hanlulong/OpenEcon
source backend/.venv/bin/activate
python3 scripts/restart_dev.py --status
```

If resuming code work after Phase 0, re-run the focused test slice most related to the next change.

## Session Resume Summary

Short version:

- the project direction is outcome-guarantee architecture, not more heuristic guardrails
- the roadmap is now phase-gated and must be followed in order
- Phase 0 must lock the execution contract before runtime work continues
- Phase 1 starts with candidate evidence and shared outcome decisioning
- feature flags, provider-matrix evaluation, exact-output checks, and strict evidence bundles are mandatory
