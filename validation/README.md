# Validation Framework

This directory defines the **claim-grade validation framework** for OpenEcon.

The goal is to support a defensible claim such as:

> On a frozen catalog snapshot, OpenEcon achieved at least 99% weighted
> end-to-end session success on a locked, stratified certification set, with
> the 95% lower confidence bound above 99%.

## Principles

- **Snapshot-based claims only**: every claim is tied to a frozen catalog snapshot,
  code revision, model configuration, and routing/prompt version.
- **Holdout discipline**: development benchmarks, shadow validation, and
  certification holdouts are separate.
- **Session-level evaluation**: success includes direct answers, necessary
  clarifications, and multiround clarification chains that converge to the right
  result.
- **Semantic correctness over brittle string matching**: provider-equivalent
  answers can be accepted when the concept, transform, country scope, and time
  scope still match user intent.
- **Production replay required**: a local certification pass is not enough for a
  claim about the deployed system.
- **Weighted inference for top-line claims**: if provider floors or risk-weighted
  oversampling are used, the top-line catalog claim must rely on design-aware
  weights rather than naive unweighted accuracy.

## Directory layout

- `schemas/` — JSON schemas for datasets, adjudication artifacts, and reports
- `manifests/` — tracked public manifests and generated distribution/strata files
- `reports/` — tracked claim/report stubs or public summaries only
- `../validation_private/` — ignored private datasets, adjudication queues,
  frozen holdouts, and internal reports

## Recommended dataset tiers

1. **Development benchmark**
   - visible during iteration
   - used for fast regression detection
2. **Shadow validation**
   - broader than the dev benchmark
   - still available during development
3. **Certification holdout**
   - frozen and hidden during development
   - used for the actual claim
4. **Production holdout replay**
   - replay against `https://data.openecon.ai`
   - verifies deployment parity

## Recommended claim thresholds

A 99% catalog-wide claim should require all of the following:

- weighted end-to-end session success **>= 99.2%**
- **95% lower confidence bound > 99.0%**
- wrong-confident-answer rate **<= 0.5%**
- unnecessary clarification rate **<= 5%**
- ambiguity-resolution success **>= 99%**
- no critical provider/query stratum below **97%**
- production holdout replay **>= 99%**

## Fresh current context (snapshot guidance)

The current catalog snapshot in `backend/data/indicators.db` contains roughly
330K indicators and is highly provider-skewed, with FRED and IMF dominating the
count. This means direct-session sampling must be weighted by provider count,
while multiround sampling should be risk-weighted toward stateful/high-risk
families (provider switches, Comtrade bilateral chains, StatsCan decomposition,
and ambiguity-heavy prompts).

## Current first-phase tooling

The initial scripts under `scripts/validation/` currently support:

- exporting a frozen catalog snapshot manifest
- building provider-distribution summaries from the catalog DB
- generating a tracked strata definition with a direct-session provider
  allocation baseline
- generating direct / multiround / ambiguity candidate datasets with provenance
  fields
- freezing a split manifest and writing deterministic non-overlapping split
  files
- dry-running a certification runner skeleton against those split datasets

Further phases should add semantic scoring, adjudication workflows, weighted
claim estimation, production replay, and final claim gating.
