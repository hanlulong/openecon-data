from __future__ import annotations

import importlib.util
import sys
import json
from pathlib import Path
from typing import Any

import pytest


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "validation" / "run_certification.py"


def load_module():
    spec = importlib.util.spec_from_file_location("run_certification_under_test", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_extract_response_signals_detects_clarification_payload():
    module = load_module()

    signals = module.extract_response_signals(
        {
            "clarificationNeeded": True,
            "clarificationOptions": [{"label": "GDP growth"}, {"label": "GDP per capita"}],
            "clarificationQuestions": ["Which GDP variant?"],
            "response": "Could you clarify which GDP variant you want?",
        }
    )

    assert signals["clarification_detected"] is True
    assert signals["clarification_options_count"] == 2
    assert signals["clarification_questions_count"] == 1
    assert signals["response_text_present"] is True
    assert signals["series_count"] == 0


def test_extract_response_signals_counts_nested_datasets_with_values():
    module = load_module()

    signals = module.extract_response_signals(
        {
            "data": {
                "datasets": [
                    {"metadata": {"source": "FRED", "country": "United States", "seriesId": "GDP"}, "observations": [{"date": "2024", "value": 1}]},
                    {"metadata": {"source": "IMF", "country": "Japan", "seriesId": "NGDP"}, "data": [{"x": 1}]},
                ]
            },
            "response": "Here is the comparison.",
        }
    )

    assert signals["clarification_detected"] is False
    assert signals["series_count"] == 2
    assert signals["response_text_present"] is True
    assert signals["providers"] == ["FRED", "IMF"]
    assert signals["countries"] == ["Japan", "United States"]
    assert signals["series_ids"] == ["GDP", "NGDP"]


def test_progress_sidecar_paths_and_summary(tmp_path: Path):
    module = load_module()

    output_path = tmp_path / "results.jsonl"
    progress_path = module.progress_output_path(output_path)
    meta_path = module.progress_meta_path(output_path)

    assert progress_path.name == "results.jsonl.inprogress"
    assert meta_path.name == "results.jsonl.progress.json"

    module.write_progress_summary(
        meta_path,
        completed_sessions=2,
        total_sessions=5,
        results_written=7,
        done=False,
        last_session_id="session-2",
        last_dataset_type="multiround",
    )
    payload = json.loads(meta_path.read_text(encoding="utf-8"))
    assert payload["updated_at_utc"]
    payload.pop("updated_at_utc")
    assert payload == {
        "completed_sessions": 2,
        "total_sessions": 5,
        "results_written": 7,
        "done": False,
        "last_session_id": "session-2",
        "last_dataset_type": "multiround",
    }


def test_execute_rows_preserves_inprogress_when_resume_enabled(tmp_path: Path, monkeypatch):
    module = load_module()
    output_path = tmp_path / "results.jsonl"
    progress_path = module.progress_output_path(output_path)
    progress_path.write_text(json.dumps({"session_id": "existing"}) + "\n", encoding="utf-8")

    calls: list[dict[str, Any]] = []

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"data": [{"metadata": {"source": "FRED"}, "observations": [{"date": "2024", "value": 1}]}]}

    def fake_post(url, json, timeout):
        calls.append(json)
        return FakeResponse()

    monkeypatch.setattr(module.requests, "post", fake_post)

    rows = [{"id": "direct-1", "query": "US GDP"}]
    results = module.execute_rows(
        rows,
        "http://localhost:3001",
        progress_output=progress_path,
        progress_meta=module.progress_meta_path(output_path),
        existing_results=[{"session_id": "existing"}],
        preserve_progress_output=True,
    )

    assert len(calls) == 1
    assert results[0]["session_id"] == "existing"
    lines = progress_path.read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[0]) == {"session_id": "existing"}
    assert json.loads(lines[1])["session_id"] == "direct-1"


def test_select_rows_applies_start_index_before_max_sessions():
    module = load_module()
    rows = [
        {"id": "direct-1", "query": "first"},
        {"id": "direct-2", "query": "second"},
        {"id": "direct-3", "query": "third"},
        {"id": "direct-4", "query": "fourth"},
    ]

    selected = module.select_rows(rows, start_index=2, max_sessions=1)

    assert [row["id"] for row in selected] == ["direct-3"]


def test_execute_rows_rejects_negative_start_index(tmp_path: Path):
    module = load_module()

    try:
        module.execute_rows([], "http://localhost:3001", start_index=-1)
    except ValueError as exc:
        assert "start_index" in str(exc)
    else:
        raise AssertionError("negative start_index should fail")


def test_execute_rows_rejects_invalid_concurrency():
    module = load_module()

    with pytest.raises(ValueError, match="concurrency"):
        module.execute_rows([], "http://localhost:3001", concurrency=0)


def test_execute_rows_concurrent_preserves_session_outputs(tmp_path: Path, monkeypatch):
    module = load_module()
    calls: list[str] = []

    class FakeResponse:
        status_code = 200

        def __init__(self, source: str):
            self.source = source

        def json(self):
            return {
                "data": [
                    {
                        "metadata": {"source": self.source, "seriesId": self.source.upper()},
                        "observations": [{"date": "2024", "value": 1}],
                    }
                ]
            }

    def fake_post(url, json, timeout):
        calls.append(json["query"])
        return FakeResponse(json["query"])

    monkeypatch.setattr(module.requests, "post", fake_post)
    output_path = tmp_path / "results.jsonl"
    rows = [
        {"id": "direct-1", "query": "fred"},
        {"id": "direct-2", "query": "imf"},
    ]

    results = module.execute_rows(
        rows,
        "http://localhost:3001",
        progress_output=module.progress_output_path(output_path),
        progress_meta=module.progress_meta_path(output_path),
        concurrency=2,
    )

    assert sorted(calls) == ["fred", "imf"]
    assert {record["session_id"] for record in results} == {"direct-1", "direct-2"}
    assert {record["series_count"] for record in results} == {1}
    progress = json.loads(module.progress_meta_path(output_path).read_text(encoding="utf-8"))
    assert progress["done"] is True
    assert progress["concurrency"] == 2


def test_execute_rows_concurrent_keeps_multiround_session_order(tmp_path: Path, monkeypatch):
    module = load_module()
    calls: list[dict[str, Any]] = []

    class FakeResponse:
        status_code = 200

        def __init__(self, conversation_id: str):
            self.conversation_id = conversation_id

        def json(self):
            return {
                "conversationId": self.conversation_id,
                "data": [{"metadata": {"source": "FRED"}, "observations": [{"date": "2024", "value": 1}]}],
            }

    def fake_post(url, json, timeout):
        calls.append(dict(json))
        return FakeResponse("conv-1")

    monkeypatch.setattr(module.requests, "post", fake_post)
    rows = [
        {
            "id": "multi-1",
            "rounds": [
                {"query": "first"},
                {"query": "second"},
            ],
        }
    ]

    results = module.execute_rows(
        rows,
        "http://localhost:3001",
        progress_output=tmp_path / "results.jsonl.inprogress",
        progress_meta=tmp_path / "results.jsonl.progress.json",
        concurrency=2,
    )

    assert [call["query"] for call in calls] == ["first", "second"]
    assert calls[1]["conversationId"] == "conv-1"
    assert [record["round_index"] for record in results] == [1, 2]


def test_completed_session_ids_requires_all_multiround_rounds():
    module = load_module()
    rows = [
        {"id": "multi-1", "rounds": [{"query": "first"}, {"query": "second"}]},
        {"id": "direct-1", "query": "US GDP"},
    ]
    partial_records = [
        {"session_id": "multi-1", "round_index": 1},
        {"session_id": "direct-1", "round_index": 1},
    ]

    assert module.completed_session_ids_from_results(rows, partial_records) == {"direct-1"}

    complete_records = partial_records + [{"session_id": "multi-1", "round_index": 2}]
    assert module.completed_session_ids_from_results(rows, complete_records) == {"multi-1", "direct-1"}


def test_completed_session_ids_ignores_failed_request_records():
    module = load_module()
    rows = [{"id": "direct-1", "query": "US GDP"}]
    records = [{"session_id": "direct-1", "round_index": 1, "request_failed": True}]

    assert module.completed_session_ids_from_results(rows, records) == set()
    assert module.keep_complete_session_records(rows, records) == []


def test_skip_completed_does_not_skip_partial_multiround_session(tmp_path: Path, monkeypatch):
    module = load_module()
    calls: list[dict[str, Any]] = []

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"conversationId": "conv", "data": [{"metadata": {"source": "FRED"}, "observations": [{"date": "2024", "value": 1}]}]}

    def fake_post(url, json, timeout):
        calls.append(json)
        return FakeResponse()

    monkeypatch.setattr(module.requests, "post", fake_post)

    rows = [{"id": "multi-1", "rounds": [{"query": "first"}, {"query": "second"}]}]
    partial_records = [{"session_id": "multi-1", "round_index": 1}]
    completed_ids = module.completed_session_ids_from_results(rows, partial_records)
    complete_existing = module.keep_complete_session_records(rows, partial_records)

    results = module.execute_rows(
        rows,
        "http://localhost:3001",
        progress_output=tmp_path / "results.jsonl.inprogress",
        progress_meta=tmp_path / "results.jsonl.progress.json",
        skip_completed_session_ids=completed_ids,
        existing_results=complete_existing,
        preserve_progress_output=True,
    )

    assert completed_ids == set()
    assert complete_existing == []
    assert [call["query"] for call in calls] == ["first", "second"]
    assert [record["round_index"] for record in results] == [1, 2]


def test_resume_loads_existing_complete_results_before_appending(tmp_path: Path, monkeypatch):
    module = load_module()
    output_path = tmp_path / "results.jsonl"
    progress_path = module.progress_output_path(output_path)
    progress_path.write_text(json.dumps({"session_id": "direct-1", "round_index": 1}) + "\n", encoding="utf-8")
    existing = module.load_existing_results(progress_path)
    rows = [
        {"id": "direct-1", "query": "first"},
        {"id": "direct-2", "query": "second"},
    ]
    completed_ids = module.completed_session_ids_from_results(rows, existing)
    calls: list[dict[str, Any]] = []

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"data": [{"metadata": {"source": "FRED"}, "observations": [{"date": "2024", "value": 1}]}]}

    def fake_post(url, json, timeout):
        calls.append(json)
        return FakeResponse()

    monkeypatch.setattr(module.requests, "post", fake_post)

    results = module.execute_rows(
        rows,
        "http://localhost:3001",
        progress_output=progress_path,
        progress_meta=module.progress_meta_path(output_path),
        skip_completed_session_ids=completed_ids,
        existing_results=existing,
        preserve_progress_output=True,
    )

    assert completed_ids == {"direct-1"}
    assert [call["query"] for call in calls] == ["second"]
    assert [record["session_id"] for record in results] == ["direct-1", "direct-2"]


def test_execute_rows_concurrency_runs_sessions_and_preserves_multiround_order(tmp_path: Path, monkeypatch):
    module = load_module()
    calls: list[dict[str, Any]] = []

    class FakeResponse:
        status_code = 200

        def __init__(self, query: str):
            self.query = query

        def json(self):
            return {
                "conversationId": f"conv-{self.query}",
                "data": [{"metadata": {"source": "FRED"}, "observations": [{"date": "2024", "value": 1}]}],
            }

    def fake_post(url, json, timeout):
        calls.append(dict(json))
        return FakeResponse(json["query"])

    monkeypatch.setattr(module.requests, "post", fake_post)
    rows = [
        {"id": "multi-1", "rounds": [{"query": "first"}, {"query": "second"}]},
        {"id": "direct-1", "query": "direct"},
    ]

    results = module.execute_rows(
        rows,
        "http://localhost:3001",
        progress_output=tmp_path / "results.jsonl.inprogress",
        progress_meta=tmp_path / "results.jsonl.progress.json",
        concurrency=2,
    )

    assert sorted(record["session_id"] for record in results) == ["direct-1", "multi-1", "multi-1"]
    first_payload = next(call for call in calls if call["query"] == "first")
    second_payload = next(call for call in calls if call["query"] == "second")
    assert "conversationId" not in first_payload
    assert second_payload["conversationId"] == "conv-first"
    progress = json.loads((tmp_path / "results.jsonl.progress.json").read_text(encoding="utf-8"))
    assert progress["done"] is True
    assert progress["concurrency"] == 2


def test_execute_rows_concurrency_writes_failure_checkpoint_before_reraising(tmp_path: Path, monkeypatch):
    module = load_module()

    def fake_post(url, json, timeout):
        raise module.requests.Timeout("timed out")

    monkeypatch.setattr(module.requests, "post", fake_post)
    progress_path = tmp_path / "results.jsonl.inprogress"
    meta_path = tmp_path / "results.jsonl.progress.json"

    with pytest.raises(module.requests.Timeout):
        module.execute_rows(
            [{"id": "direct-1", "query": "US GDP"}],
            "http://localhost:3001",
            progress_output=progress_path,
            progress_meta=meta_path,
            concurrency=2,
        )

    record = json.loads(progress_path.read_text(encoding="utf-8").splitlines()[0])
    assert record["session_id"] == "direct-1"
    assert record["request_failed"] is True
    payload = json.loads(meta_path.read_text(encoding="utf-8"))
    assert payload["done"] is False
    assert payload["concurrency"] == 2
    assert "timed out" in payload["last_error"]


def test_load_resume_records_prefers_nonempty_inprogress(tmp_path: Path):
    module = load_module()
    output_path = tmp_path / "results.jsonl"
    progress_path = module.progress_output_path(output_path)
    output_path.write_text(json.dumps({"session_id": "final", "round_index": 1}) + "\n", encoding="utf-8")
    progress_path.write_text(json.dumps({"session_id": "progress", "round_index": 1}) + "\n", encoding="utf-8")

    records = module.load_resume_records(output_path, progress_path)

    assert [record["session_id"] for record in records] == ["progress"]


def test_keep_complete_session_records_drops_duplicate_rounds():
    module = load_module()
    rows = [{"id": "direct-1", "query": "US GDP"}]
    records = [
        {"session_id": "direct-1", "round_index": 1, "value": "first"},
        {"session_id": "direct-1", "round_index": 1, "value": "dupe"},
    ]

    assert module.completed_session_ids_from_results(rows, records) == set()
    assert module.keep_complete_session_records(rows, records) == []


def test_execute_rows_writes_failure_checkpoint_before_reraising(tmp_path: Path, monkeypatch):
    module = load_module()

    def fake_post(url, json, timeout):
        raise module.requests.Timeout("timed out")

    monkeypatch.setattr(module.requests, "post", fake_post)
    progress_path = tmp_path / "results.jsonl.inprogress"
    meta_path = tmp_path / "results.jsonl.progress.json"

    with pytest.raises(module.requests.Timeout):
        module.execute_rows(
            [{"id": "direct-1", "query": "US GDP"}],
            "http://localhost:3001",
            progress_output=progress_path,
            progress_meta=meta_path,
        )

    record = json.loads(progress_path.read_text(encoding="utf-8").splitlines()[0])
    assert record["session_id"] == "direct-1"
    assert record["round_index"] == 1
    assert record["request_failed"] is True
    assert "timed out" in record["error"]

    payload = json.loads(meta_path.read_text(encoding="utf-8"))
    assert payload["done"] is False
    assert payload["completed_sessions"] == 0
    assert payload["last_session_id"] == "direct-1"
    assert "timed out" in payload["last_error"]


def test_resume_retries_failed_request_records(tmp_path: Path, monkeypatch):
    module = load_module()
    progress_path = tmp_path / "results.jsonl.inprogress"
    progress_path.write_text(
        json.dumps({"session_id": "direct-1", "round_index": 1, "request_failed": True}) + "\n",
        encoding="utf-8",
    )
    rows = [{"id": "direct-1", "query": "US GDP"}]
    existing = module.load_existing_results(progress_path)
    completed_ids = module.completed_session_ids_from_results(rows, existing)
    complete_existing = module.keep_complete_session_records(rows, existing)
    calls: list[dict[str, Any]] = []

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"data": [{"metadata": {"source": "FRED"}, "observations": [{"date": "2024", "value": 1}]}]}

    def fake_post(url, json, timeout):
        calls.append(json)
        return FakeResponse()

    monkeypatch.setattr(module.requests, "post", fake_post)

    results = module.execute_rows(
        rows,
        "http://localhost:3001",
        progress_output=progress_path,
        progress_meta=tmp_path / "results.jsonl.progress.json",
        skip_completed_session_ids=completed_ids,
        existing_results=complete_existing,
        preserve_progress_output=True,
    )

    assert completed_ids == set()
    assert complete_existing == []
    assert [call["query"] for call in calls] == ["US GDP"]
    assert [record["session_id"] for record in results] == ["direct-1"]


def test_write_progress_summary_is_atomic_json(tmp_path: Path):
    module = load_module()
    meta_path = tmp_path / "results.jsonl.progress.json"

    module.write_progress_summary(
        meta_path,
        completed_sessions=1,
        total_sessions=2,
        results_written=1,
        done=False,
        last_session_id="direct-1",
        last_dataset_type="direct",
        start_index=1,
        skipped_sessions=0,
        completed_session_ids_count=1,
    )

    payload = json.loads(meta_path.read_text(encoding="utf-8"))
    assert payload["completed_sessions"] == 1
    assert payload["start_index"] == 1
    assert payload["completed_session_ids_count"] == 1
    assert not meta_path.with_name(meta_path.name + ".tmp").exists()
