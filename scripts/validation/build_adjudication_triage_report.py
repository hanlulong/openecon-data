#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = ROOT / "validation_private" / "reports" / "adjudication_triage_report.json"

SPARSE_PROVIDER_SURFACES = {"WORLDBANK", "OECD"}
HIGH_CONFIDENCE_DIRECT_PROVIDERS = {"FRED", "STATSCAN"}
NO_DATA_ERRORS = {"data_not_available", "no_data_available"}


def iter_jsonl(path: Path):
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


def normalize_token(value: Any) -> str:
    return "".join(ch for ch in str(value or "").upper() if ch.isalnum())


def canonical_provider(value: Any) -> str:
    normalized = normalize_token(value)
    aliases = {
        "STATISTICSCANADA": "STATSCAN",
        "STATSCAN": "STATSCAN",
        "WORLD BANK": "WORLDBANK",
        "WORLDBANK": "WORLDBANK",
    }
    return aliases.get(normalized, normalized)


def load_sessions(dataset_paths: list[Path]) -> dict[str, dict[str, Any]]:
    sessions: dict[str, dict[str, Any]] = {}
    for dataset_path in dataset_paths:
        for row in iter_jsonl(dataset_path):
            session_id = str(row.get("id") or "")
            if session_id:
                sessions[session_id] = row
    return sessions


def query_quality_info(session: dict[str, Any] | None) -> tuple[str | None, list[str]]:
    provenance = (session or {}).get("provenance") or {}
    risk = provenance.get("query_quality_risk")
    reasons = provenance.get("query_quality_reasons") or []
    return (str(risk).strip().lower() or None) if risk else None, [str(item) for item in reasons if str(item).strip()]


def raw_round_query(session: dict[str, Any] | None, queue_row: dict[str, Any]) -> str | None:
    if session is None:
        return None
    if queue_row.get("dataset_type") == "multiround":
        return None
    return str(session.get("query") or "").strip() or None


def cluster_counts_for_bucket(items: list[dict[str, Any]], bucket: str) -> dict[str, int]:
    return {
        cluster: count
        for cluster, count in sorted(Counter(item["cluster"] for item in items if item["bucket"] == bucket).items())
    }


def classify_item(
    queue_row: dict[str, Any],
    session: dict[str, Any] | None,
    raw_rounds: list[dict[str, Any]],
) -> tuple[str, str, str]:
    dataset_type = str(queue_row.get("dataset_type") or "")
    queue_reason = str(queue_row.get("queue_reason") or "")
    failure_class = str(queue_row.get("failure_class") or "")
    provider = canonical_provider(queue_row.get("provider_stratum") or (session or {}).get("provider_stratum"))
    family = str(queue_row.get("family_stratum") or (session or {}).get("family") or "")
    quality_risk, _ = query_quality_info(session)
    has_no_data_error = any(str(round_row.get("error") or "").strip().lower() in NO_DATA_ERRORS for round_row in raw_rounds)
    has_clarification = any(bool(round_row.get("clarification_detected")) for round_row in raw_rounds)

    if queue_reason in {"random_pass_audit", "minimum_pass_audit"}:
        return (
            "human_adjudication",
            "pass_audit_review",
            "Pass-audit rows still need human confirmation because structural success alone does not certify semantic correctness.",
        )

    if dataset_type == "ambiguity":
        return (
            "human_adjudication",
            "ambiguity_semantic_review",
            "Ambiguity cases require human review of whether clarification behavior and option quality were actually appropriate.",
        )

    if dataset_type == "multiround":
        if "statscan_decomposition" in family or (family.endswith("decomposition_chain") and has_clarification):
            return (
                "likely_framework_bug",
                "multiround_state_carryover",
                "This looks like a multiround state-carryover failure: an early clarification interrupted a follow-up chain that later resumed successfully.",
            )
        if has_clarification:
            return (
                "likely_framework_bug",
                "multiround_followup_clarification",
                "Unexpected clarification inside a multiround follow-up chain usually points to carryover/state handling rather than dataset quality.",
            )
        return (
            "human_adjudication",
            "multiround_semantic_review",
            "Multiround failures without a clear structural signature still need human review of the round-by-round semantics.",
        )

    if dataset_type == "direct":
        if failure_class == "unnecessary_clarification_proxy":
            if provider in HIGH_CONFIDENCE_DIRECT_PROVIDERS:
                return (
                    "likely_framework_bug",
                    "unexpected_clarification_on_direct_query",
                    "A clearly answerable direct query asked for clarification on a provider surface that should normally resolve directly.",
                )
            if provider in SPARSE_PROVIDER_SURFACES or quality_risk in {"medium", "high"}:
                return (
                    "likely_dataset_or_query_surface",
                    "provider_surface_query_shape",
                    "The direct query shape likely exposed a sparse or overly specific provider surface rather than a general routing/state bug.",
                )
        if failure_class == "error_or_no_data" or has_no_data_error:
            if provider in SPARSE_PROVIDER_SURFACES or quality_risk in {"medium", "high"}:
                return (
                    "likely_dataset_or_query_surface",
                    "provider_data_availability_surface",
                    "The provider returned no data for a long-tail row, which is more consistent with dataset/query-surface sparsity than a broad framework defect.",
                )
        if failure_class == "wrong_confident_answer_proxy":
            return (
                "likely_framework_bug",
                "wrong_confident_direct_answer",
                "The system answered directly where human review suggests semantic correctness is still in doubt.",
            )

    return (
        "human_adjudication",
        "needs_human_adjudication",
        "The automated signals are not strong enough to separate framework defects from dataset/query-surface noise without human review.",
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build a generalized adjudication triage report that separates likely framework bugs from likely dataset/query-surface issues."
    )
    parser.add_argument("--queue", type=Path, required=True)
    parser.add_argument("--raw-results", type=Path, required=True)
    parser.add_argument("--dataset", action="append", type=Path, required=True, help="JSONL dataset file; pass multiple times")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    queue_path = args.queue.resolve()
    raw_results_path = args.raw_results.resolve()
    dataset_paths = [path.resolve() for path in args.dataset]

    sessions = load_sessions(dataset_paths)
    raw_by_session: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in iter_jsonl(raw_results_path):
        raw_by_session[str(row.get("session_id") or "")].append(row)

    items: list[dict[str, Any]] = []
    bucket_counts = Counter()
    cluster_counts = Counter()
    missing_dataset_ids: list[str] = []

    for queue_row in iter_jsonl(queue_path):
        session_id = str(queue_row.get("session_id") or "")
        session = sessions.get(session_id)
        if session is None:
            missing_dataset_ids.append(session_id)
        raw_rounds = sorted(raw_by_session.get(session_id, []), key=lambda row: int(row.get("round_index") or 0))
        bucket, cluster, rationale = classify_item(queue_row, session, raw_rounds)
        bucket_counts[bucket] += 1
        cluster_counts[cluster] += 1
        quality_risk, quality_reasons = query_quality_info(session)
        items.append(
            {
                "session_id": session_id,
                "bucket": bucket,
                "cluster": cluster,
                "rationale": rationale,
                "dataset_type": queue_row.get("dataset_type"),
                "provider_stratum": queue_row.get("provider_stratum"),
                "family_stratum": queue_row.get("family_stratum"),
                "queue_reason": queue_row.get("queue_reason"),
                "automated_label": queue_row.get("automated_label"),
                "failure_class": queue_row.get("failure_class"),
                "query": raw_round_query(session, queue_row),
                "query_quality_risk": quality_risk,
                "query_quality_reasons": quality_reasons,
                "review_focus_tags": list(queue_row.get("review_focus_tags") or []),
                "raw_rounds": raw_rounds,
            }
        )

    report = {
        "queue_path": str(queue_path),
        "raw_results_path": str(raw_results_path),
        "dataset_paths": [str(path) for path in dataset_paths],
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "total_records": len(items),
            "bucket_counts": dict(bucket_counts),
            "cluster_counts": dict(cluster_counts),
            "framework_bug_cluster_counts": cluster_counts_for_bucket(items, "likely_framework_bug"),
            "dataset_or_query_surface_cluster_counts": cluster_counts_for_bucket(items, "likely_dataset_or_query_surface"),
            "human_adjudication_cluster_counts": cluster_counts_for_bucket(items, "human_adjudication"),
            "missing_dataset_rows": sorted(missing_dataset_ids),
        },
        "items": items,
    }

    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(output), "summary": report["summary"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
