#!/usr/bin/env python3
"""Run the intent-level accuracy probe set against the local backend.

Sequential (never parallel — the backend serializes LLM work). Prints one
compact line per probe: served source/series/frequency/points/latest, or the
clarification shape. Judgment against `expects` is the operator's job (the
correctness rule: intent satisfaction, not id matching). Multi-round probes
thread conversationId.

    python3 scripts/run_accuracy_probes.py [probe_set.json]
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

BASE = "http://localhost:3001/api/query"
DEFAULT_SET = Path(__file__).parent / "accuracy_probe_set_2026-07-19.json"


def post(query: str, conversation_id: str | None = None, timeout: float = 150.0) -> dict:
    payload: dict = {"query": query}
    if conversation_id:
        payload["conversationId"] = conversation_id
    req = urllib.request.Request(
        BASE,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def summarize(r: dict) -> str:
    data = r.get("data") or []
    if data and isinstance(data[0], dict) and data[0].get("metadata"):
        parts = []
        for series in data[:2]:
            m = series["metadata"]
            pts = series.get("data") or []
            latest = pts[-1] if pts else None
            parts.append(
                f"{m.get('source')}|{m.get('seriesId')}|{m.get('frequency')}"
                f"|{len(pts)}pts|latest={latest}"
            )
        return "DATA " + " ; ".join(parts)
    if r.get("clarificationNeeded"):
        opts = r.get("clarificationOptions") or []
        labels = [str(o.get("label") if isinstance(o, dict) else o)[:40] for o in opts[:4]]
        qs = (r.get("clarificationQuestions") or [""])[0][:60]
        return f"ASK options={len(opts)} {labels or qs!r}"
    return f"OTHER err={str(r.get('error'))[:60]} msg={str(r.get('message'))[:60]}"


def main() -> int:
    probe_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_SET
    spec = json.loads(probe_path.read_text())
    for probe in spec["probes"]:
        pid = probe["id"]
        try:
            if "conversation" in probe:
                conv_id = None
                for i, turn in enumerate(probe["conversation"]):
                    t0 = time.time()
                    r = post(turn, conv_id)
                    conv_id = r.get("conversationId") or conv_id
                    print(f"[{pid}.t{i+1}] ({time.time()-t0:.0f}s) {turn[:36]!r} -> {summarize(r)}",
                          flush=True)
            else:
                t0 = time.time()
                r = post(probe["query"])
                print(f"[{pid}] ({time.time()-t0:.0f}s) -> {summarize(r)}", flush=True)
        except Exception as exc:  # noqa: BLE001 — operator tool
            print(f"[{pid}] EXCEPTION {exc}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
