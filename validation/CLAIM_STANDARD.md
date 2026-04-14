# Claim Standard

This document defines when OpenEcon may make a public accuracy claim such as
"99% across the catalog".

## Allowed claim form

An allowed claim must be framed as a **snapshot-bound certification claim**.

Example:

> On the frozen catalog snapshot dated 2026-04-14 containing 330,050 indicators,
> the system achieved at least 99% weighted end-to-end session success on a
> locked stratified certification set, with the 95% lower confidence bound above
> 99%.

## Disallowed claim form

The following is **not** allowed without exhaustive proof:

> We are 99% accurate for all 330K indicators.

## Preconditions for a valid claim

A claim is valid only if all of the following are true:

1. **Frozen catalog snapshot** exists and is referenced by hash / count
2. **Git commit SHA** is fixed
3. **Model / prompt / routing configuration** is fixed
4. **Certification datasets** are locked and versioned
5. **Manual adjudication** is complete for:
   - all failures
   - all uncertain automated judgments
   - a random pass sample
6. **Production replay** has been executed against the deployed target
7. Statistical gates and stratum gates have passed

## Required statistical gates

- weighted session success **>= 99.2%** (or a stricter threshold such as 99.25% when the effective sample size is smaller than nominal due to clustering)
- **95% lower confidence bound > 99.0%**
- wrong-confident-answer rate **<= 0.5%**
- unnecessary clarification rate **<= 5%**
- ambiguity-resolution success **>= 99%**
- if provider floors or risk-weighted oversampling are used, the estimator must be design-aware; naive unweighted binomial scoring is not sufficient for the top-line catalog claim

## Required stratum gates

- no critical provider/query stratum below **97%**
- no high-traffic provider/query stratum below **98%**
- no unresolved failure cluster that shows framework-level breakage

## Required production gate

The production holdout replay must pass on the deployed target, and local vs
production results must not materially diverge.

## Claim invalidation rules

A claim becomes invalid and must be re-certified if any of the following change:

- indicator catalog snapshot
- routing logic
- model or provider configuration
- prompts / parsing policy
- production deployment diverges from the certified commit

## Evidence package required for any claim

A claim must be backed by an evidence package containing:

- catalog snapshot manifest
- provider distribution summary
- strata definition
- certification summary report
- adjudication summary
- production replay summary
- exact git SHA and deployment timestamp
