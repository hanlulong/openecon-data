# Semantic Runtime Boundary

**Status:** Binding runtime contract for the 99%-accuracy migration  
**Canonical production domain:** `data.openecon.ai`

---

## Why this document exists

The system is trying to reach **99% user-correctness** across a very large
indicator space. The fastest way to fail is to let semantic meaning continue to
drift across ad hoc cue maps, regex follow-up handlers, provider-specific
special cases, and permissive “returned some data” gates.

This document defines the runtime boundary that every later implementation phase
must respect.

---

## Core rule

**Deterministic runtime logic stays structural-only. Semantic meaning must be
represented through typed contracts, model-backed reasoning, or schema-backed
evidence—not through growing semantic patch tables.**

In practice:

- structural code **may** enforce:
  - parse / schema validity
  - execution legality
  - provider capability / feasibility checks
  - verification completeness checks
  - release-integrity / deployment safety checks
  - state transition invariants
- structural code **must not** become the primary semantic arbiter for:
  - what indicator the user “really” meant
  - which follow-up semantic action the user intended
  - whether a weak candidate is “close enough”
  - whether one concept-specific ambiguity deserves a one-off rule

---

## Binding product policy

The runtime must preserve these policies:

1. **Dominant verified interpretation answers directly.**
   If one interpretation is clearly dominant and can be verified, answer
   directly and optionally surface alternatives afterward.

2. **Clarify only when materially necessary.**
   Clarification is good when uncertainty is real. Over-clarification is product
   failure.

3. **Equivalent provider substitution is acceptable.**
   If the user did not request a specific provider, a semantically equivalent
   provider is valid when concept, transform, country scope, and time scope
   match.

4. **Verified truth only.**
   Provider/execution snapshots remain provisional until verification succeeds.
   Unverified semantics must not become durable conversation truth.

---

## Allowed deterministic logic

The following are allowed, and expected:

- typed schemas such as:
  - `SemanticRequest`
  - `ConversationAction`
  - `CandidateEvidence`
  - `OutcomeDecision`
  - `VerificationContract`
  - `StateCommitDecision`
- provider capability contracts
- result-shape validation
- exact-output oracles
- decomposition completeness checks
- country/group completeness checks
- value-range sanity checks
- release-integrity checks for `data.openecon.ai`

These are structural because they validate contracts, not semantic guesses.

### Mechanical metadata vs semantic authority

Provider-native constants are allowed only after semantic authority has already
been established. Examples:

- exact user/provider codes such as FRED series IDs, World Bank indicator codes,
  StatsCan table/vector IDs, IMF dataset codes, and Eurostat/OECD dataflow codes;
- provider API dimensions, coordinate IDs, frequency codes, units, and schema
  names required to execute a selected provider-native request;
- country/date/currency parsing needed to build a valid API request.

These constants must not be used as natural-language shortcut maps. On the
promoted semantic path (`USE_OUTCOME_DECISION_STAGE=true`,
`USE_POST_FETCH_SEMANTIC_JUDGE=true`, and `USE_STAGED_STATE_COMMIT=true`), a
provider-internal map may dispatch only when one of these authority markers is
present:

- exact user/provider-native target (`__exact_provider_code_match` or
  `__semantic_authority=exact_user_input`);
- LLM/adjudicator pick (`__semantic_authority=llm_adjudication`);
- a post-fetch semantic judge approval (`__semantic_authority=post_fetch_semantic_judge`).

If none of those markers is present, provider maps are treated as candidate
evidence or legacy compatibility only and must fail closed on the promoted path.

---

## Forbidden runtime patterns

The following must not be expanded as the main strategy:

- new concept-specific cue lists
- regex patches for semantic follow-up meaning
- provider-specific semantic exception tables
- “temporary” mappings that silently become permanent
- PASS conditions based on data presence alone
- provider mismatch counted as effectively correct by default
- manual correctness inferred from automated `ok` bits

Legacy heuristic modules may survive temporarily during migration only as:

- compatibility scaffolding
- telemetry for comparison
- isolated fallback paths behind feature flags

They must not remain the **primary semantic authority** on the new path.

---

## Required runtime contract chain

The flagged migration path must converge on this contract sequence:

1. **`SemanticRequest`**
   - provider-agnostic statement of intended concept, transform, scope, shape,
     and time semantics
2. **`ConversationAction`**
   - typed follow-up delta against prior semantic state
3. **`CandidateEvidence`**
   - why candidates/providers match or conflict
4. **`OutcomeDecision` / `ClarificationDecision`**
   - direct answer vs clarify vs unsupported
5. **`ExecutionPlan` / `VerificationContract`**
   - explicit proof obligations before return
6. **`StateCommitDecision`**
   - verified semantic truth may or may not be promoted

This sequence is the replacement for scattered semantic decision-making.

---

## Release / rollout implications

Because trust is part of correctness, the runtime boundary also applies to the
production surface:

- browser behavior on `https://data.openecon.ai/chat`
- route metadata
- share semantics
- machine-readable trust files
- release-integrity checks that compare local intent vs live deployment

No rollout is complete until those user-visible surfaces are verified alongside
the backend contracts.
