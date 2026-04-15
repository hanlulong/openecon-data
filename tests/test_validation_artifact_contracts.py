from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_claim_gate_policy_manifest_points_to_schema_and_required_sections():
    manifest = load_json(ROOT / "validation" / "manifests" / "claim_gate_policy-v1.json")

    assert manifest["$schema"].endswith("/validation/claim_gate_policy.schema.json")
    assert "claim_thresholds" in manifest
    assert "required_direct_provider_floors" in manifest
    assert "required_multiround_family_floors" in manifest
    assert "required_ambiguity_family_floors" in manifest


def test_adjudication_schema_covers_enriched_queue_fields():
    schema = load_json(ROOT / "validation" / "schemas" / "adjudication.schema.json")
    props = schema["properties"]

    assert "provider_stratum" in props
    assert "family_stratum" in props
    assert "queue_reason" in props
    assert "review_focus_tags" in props
    assert "clarification_detected" in props
    assert "expected_clarification" in props
    assert "answer_present_without_clarification" in props
