# Infrastructure Fix Plan — Superseded

This December 2025 plan is retained only as a historical note. Do **not** execute
its proposed shortcut-map fixes.

## Current policy

OpenEcon no longer fixes semantic matching failures by adding keyword maps,
provider-code shortcuts, fuzzy concept rules, or guard/overwrite paths. The
current framework-level path is:

1. provider-local candidate retrieval;
2. LLM adjudication with PICK / ASK / REJECT / SEARCH retry;
3. exact provider-native code/title passthrough only when the user supplied that
   mechanical identity;
4. provider metadata discovery or fail-closed behavior when no reliable semantic
   authority exists.

## Why this plan was superseded

The original plan recommended concept aliases, fuzzy-threshold tweaks, and
provider-specific fallback rules. Those approaches can make one query pass while
creating hidden wrong-answer risk for similar user questions. They are now
classified as rule-based semantic shortcuts and must not be reintroduced.

## Replacement guidance

When a query such as a monetary aggregate, household count, children count,
policy-rate, or provider coverage question fails:

- improve candidate retrieval quality or provider metadata discovery;
- improve deterministic LLM adjudication fixtures and reject/retry handling;
- add regression tests that prove no candidate-generation path can become final
  answer authority by itself;
- preserve mechanical API constants and exact provider-native passthrough.
