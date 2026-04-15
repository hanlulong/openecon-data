from __future__ import annotations

import importlib.util
import sys
import json
from pathlib import Path


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
    assert payload == {
        "completed_sessions": 2,
        "total_sessions": 5,
        "results_written": 7,
        "done": False,
        "last_session_id": "session-2",
        "last_dataset_type": "multiround",
    }
