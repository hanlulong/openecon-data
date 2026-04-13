# Query Outcome Guarantee System

**Status:** System design reference for the 99%-accuracy migration  
**Canonical production domain:** `data.openecon.ai`

---

## Goal

The system should satisfy the user’s request in one of three correct ways:

1. a **direct answer** with the correct dominant interpretation,
2. a **clarification** whose options include the correct answer,
3. a **multi-round clarification path** that ends with the correct answer.

The system must improve direct-answer capability over time while keeping
clarification **necessary, not excessive**.

---

## Current failure classes

The codebase currently has three system-level weaknesses:

### 1. Semantic runtime drift

Semantic decisions are still distributed across multiple places:

- `QueryService` orchestration paths
- clarification generation
- indicator resolution heuristics
- cue-based reranking
- follow-up regex/token logic

That makes it too easy for one path to behave differently from another.

### 2. Thin proof contracts

The existing minimal execution planning and structural verification logic can
prove:

- some shape expectations,
- some scope expectations,
- some decomposition cardinality expectations,

but not enough of the user’s intended semantics. That leaves too much of the
correctness story to permissive heuristics and shallow summary judging.

### 3. False-green evaluation

Some current tests still prove “a result came back” better than “the user got
the right answer they wanted.”

That creates a gap between green metrics and real product trust.

---

## Target architecture

The target path is:

```text
query
  -> SemanticRequest
  -> ConversationAction (for follow-ups)
  -> CandidateEvidence
  -> OutcomeDecision / ClarificationDecision
  -> ExecutionPlan / VerificationContract
  -> provider execution
  -> verification
  -> StateCommitDecision
  -> response (+ alternatives when appropriate)
```

This is intentionally more explicit than the current system. The goal is not a
larger orchestrator, but a system with fewer hidden semantic jumps.

---

## Authoritative contracts

### `SemanticRequest`

Provider-agnostic representation of:

- concept family
- transform / variant
- country / group scope
- decomposition axis
- answer shape
- time range / frequency intent

### `ConversationAction`

Typed follow-up intent such as:

- replace metric
- add metric
- replace scope
- add scope
- remove scope
- switch provider
- change time
- set decomposition
- resolve clarification

### `CandidateEvidence`

For each candidate/provider combination:

- matching evidence
- conflicting evidence
- ambiguity class
- provider-feasibility evidence
- verification obligations

### `OutcomeDecision`

Explicitly decides:

- `DIRECT_ANSWER`
- `CLARIFY`
- `UNSUPPORTED`

with a reason grounded in evidence, not just ad hoc heuristics.

### `ExecutionPlan` / `VerificationContract`

Defines what must be true before returning the answer:

- concept / semantic family
- acceptable provider family or equivalence set
- scope completeness
- transform / variant correctness
- decomposition behavior
- ranking / comparison completeness
- value sanity when applicable

### `StateCommitDecision`

Determines whether the result is verified enough to become durable semantic
conversation truth.

---

## Verified truth model

Durable conversation truth must be **semantic** and **verified**.

That means the state system should separate:

1. **Semantic conversation state**
   - provider-agnostic user intent
2. **Resolution snapshot**
   - provider/candidate choices under consideration
3. **Provider execution cache**
   - provider-specific execution details

Only verified outcomes may promote semantic truth.

---

## Clarification policy

Clarification is not the default for every non-zero ambiguity.

The product policy is:

- answer directly when one interpretation is dominant and verifiable
- clarify when ambiguity is materially unresolved
- allow semantically equivalent provider substitution when the user did not ask
  for a specific provider
- show alternative providers/series **after** the answer when useful

This policy must be implemented through typed ambiguity classes and explicit
decision rules, not ad hoc phrase lists.

---

## Evaluation policy

A passing evaluation must mean the user got the correct semantic outcome.

So the target evaluation stack must reject these false-green behaviors:

- PASS because some data exists
- WARN/provider mismatch counted as effectively green by default
- clarification counted as success without validating the final resolved answer
- manual correctness derived from automated `ok` flags

The required evaluation stack includes:

- oracle-bearing multiround suites
- exact-output suites
- ambiguity-to-resolution suites
- browser/manual validation on `https://data.openecon.ai/chat`
- release-integrity checks for production assets and trust surfaces

---

## Rollout model

Migration may proceed behind feature flags and stronger gates.

That is acceptable only if:

- old-path vs new-path comparisons are run on the same oracle-bearing slices
- the false-green gap shrinks
- production trust and browser behavior are validated
- GitHub/main, deployment, and live production converge before completion

The system is not done until the final user-visible behavior on
`data.openecon.ai` is verified.

