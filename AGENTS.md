# OpenEcon Agent Guide

This file contains repository-specific guidance only. Codex model, permission,
plugin, and personal defaults belong in the user-level Codex configuration.

## Operating Mode

- Execute clear, reversible work to completion without asking for confirmation.
- Ask only when a decision is destructive, irreversible, materially branching,
  or impossible to infer safely from repository evidence.
- Read the relevant code and tests before changing behavior.
- Prefer evidence over assumptions and verify before claiming completion.
- Use native Codex subagents for independent, bounded work when they materially
  improve speed, correctness, or review quality.
- Keep progress updates short and concrete. The lead agent owns integration and
  final verification.
- Preserve unrelated user changes in a dirty worktree.

## Engineering Agreements

- Keep changes small, reviewable, and reversible.
- Reuse existing utilities and patterns before adding abstractions.
- Prefer deletion and simplification over new layers.
- Do not add dependencies unless the user explicitly requests them.
- Use structured parsers and APIs for structured data.
- Add comments only where the code is not self-explanatory.
- For cleanup, refactor, or deslop work, write a cleanup plan first. Protect
  existing behavior with regression tests before editing when coverage is absent.
- Run the applicable formatter, lint, typecheck, tests, and static analysis after
  changes. Scale verification to the change's risk and blast radius.
- Final reports must name changed files, simplifications, verification evidence,
  and remaining risks.

## Critical Decision Gate

Before implementing, committing, deploying, or changing a claim/status gate for
a critical decision, obtain written reviews from at least three independent
agents. The lead/orchestrator does not count as a reviewer.

Critical decisions include:

- Architecture or data-model changes.
- Provider/runtime, semantic matching, provider selection, or indicator
  resolution changes.
- Validation surfaces, certification, sampling, adjudication, thresholds, or
  claim wording.
- Production deployment or rollback.
- Broadly user-visible multi-file changes.

The decision record must include each reviewer's recommendation, risks or
dissent, and the lead's resolution. Low-risk read-only inspection, mechanical
formatting, test-only reruns, and documentation/status-only updates are exempt.

## Semantic Resolution Rules

- Do not fix semantic matching, provider selection, or indicator resolution with
  shortcut guards, keyword maps, query-ID cases, or forced overrides.
- When a match is wrong, remove or demote the rule-based authority, or improve
  retrieval and model adjudication with regression tests.
- Mechanical normalization and plumbing rules are allowed when they do not make
  semantic decisions.

## Correctness Beats Provider Preference (user-mandated 2026-07-19)

- The test of every result is whether the RETURNED DATA answers what the user
  asked (concept, country, frequency or honest substitution disclosure,
  cross-checked values) — never which provider served it or which series id
  was predicted. Multiple different series can each be a correct answer.
- Provider-preference mechanisms (routing rules, coverage predicates,
  auto-routability gates, catalog "best provider" opinions, arbitration
  candidate filters) must NEVER reject, discard, down-rank, or fail data that
  correctly answers the question merely because it came from a non-preferred
  provider. Preferences may steer the FIRST attempt; they may not veto a
  correct result.
- The single provider-identity failure mode: ignoring a provider the user
  EXPLICITLY named ("from FRED", "using OECD") — that request is part of the
  intent and wins.
- Clarification follows the same principle: ask ONLY when genuinely needed to
  identify the intended series (two or more materially distinct, executable
  options); never guess-and-serve a weak match; never show a one-option menu;
  never re-ask what the conversation already answered.

## Verification

- Identify the evidence that proves the requested behavior before declaring the
  work complete.
- Run dependent checks sequentially and read their output.
- If a check fails, diagnose and continue rather than reporting success.
- Before concluding, confirm there is no pending required work, the changed
  behavior works, relevant tests pass, and known gaps are disclosed.

## Lore Commit Protocol

Commit messages are decision records. The first line states why the change was
made, not a summary of the diff. Add a concise body when context or tradeoffs are
not obvious. Use native Git trailers where they add durable value:

```text
<intent line>

<context and approach>

Constraint: <external constraint>
Rejected: <alternative> | <reason>
Confidence: <low|medium|high>
Scope-risk: <narrow|moderate|broad>
Reversibility: <clean|messy|irreversible>
Directive: <warning for future modifiers>
Tested: <verification performed>
Not-tested: <known verification gap>
Related: <issue, commit, or decision>
```

Trailers are optional, but `Tested` and `Not-tested` must be honest. Use
`Rejected` to prevent repeated exploration and `Directive` for constraints that
future changes must preserve.
