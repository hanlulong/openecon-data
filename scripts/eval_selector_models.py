#!/usr/bin/env python3
"""Evaluate candidate LLMs on the indicator-selector adjudication task.

Runs the REAL selector pipeline up to the LLM step (embedding+FTS retrieval,
country constraint, metadata enrichment, the production selection prompt),
then sends the identical prompt to each candidate model and scores the parsed
decision against labeled acceptable answers.

Usage:
    source backend/.venv/bin/activate
    python scripts/eval_selector_models.py            # full matrix
    python scripts/eval_selector_models.py --models local,openai/gpt-4o-mini
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from backend.config import Settings  # noqa: E402
from backend.services.indicator_selector import (  # noqa: E402
    LLM_SELECTION_PROMPT,
    IndicatorSelector,
)

# (query, provider, country, accepted_codes) — ASK counts as "asked" (safe),
# REJECT/None as "none", any other pick as "wrong".
CASES = [
    ("CPI inflation rate", "FRED", "US",
     {"FPCPITOTLZGUSA", "CPALTT01USM659N", "CPALTT01USA659N", "CPALTT01USQ659N"}),
    ("unemployment rate", "FRED", "US", {"UNRATE"}),
    ("GDP growth", "WorldBank", "IN", {"NY.GDP.MKTP.KD.ZG"}),
    ("inflation rate", "WorldBank", "DE", {"FP.CPI.TOTL.ZG"}),
    ("10 year treasury yield", "FRED", "US", {"DGS10", "GS10"}),
    ("government debt to GDP", "IMF", "FR", {"GG_DEBT_GDP", "GGXWDG_NGDP"}),
    ("population", "WorldBank", "CN", {"SP.POP.TOTL"}),
    ("exports as share of GDP", "WorldBank", "KR", {"NE.EXP.GNFS.ZS"}),
    ("real GDP", "FRED", "US", {"GDPC1", "GDPCA"}),
    ("house price index", "FRED", "US", {"CSUSHPINSA", "CSUSHPISA", "USSTHPI"}),
    ("M2 money supply", "FRED", "US", {"M2SL", "WM2NS", "M2NS"}),
    ("youth unemployment rate", "WorldBank", "ES",
     {"SL.UEM.1524.ZS", "SL.UEM.1524.NE.ZS"}),
]

# (label, openrouter_model_or_None_for_local, $/M prompt, $/M completion)
MODELS = [
    ("local/gpt-oss-120b", None, 0.0, 0.0),
    ("openai/gpt-4o-mini", "openai/gpt-4o-mini", 0.15, 0.60),
    ("openai/gpt-oss-120b", "openai/gpt-oss-120b", 0.039, 0.18),
    ("openai/gpt-5-nano", "openai/gpt-5-nano", 0.05, 0.40),
    ("openai/gpt-5-mini", "openai/gpt-5-mini", 0.25, 2.00),
    ("google/gemini-2.5-flash-lite", "google/gemini-2.5-flash-lite", 0.10, 0.40),
    ("google/gemini-2.5-flash", "google/gemini-2.5-flash", 0.30, 2.50),
    ("deepseek/deepseek-v3.2", "deepseek/deepseek-v3.2", 0.229, 0.343),
    ("qwen/qwen3-32b", "qwen/qwen3-32b", 0.08, 0.28),
]

LOCAL_URL = "http://localhost:8000/v1/chat/completions"
LOCAL_MODEL = "gpt-oss-120b"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


def build_prompt(sel: IndicatorSelector, query: str, provider: str, country: str):
    """Replicates select()'s pre-LLM pipeline to produce the production prompt."""
    candidates, scores = sel._get_candidates_with_scores(query, provider)
    if candidates and country:
        candidates, scores, conflict = sel._apply_country_constraint(country, candidates, scores)
        if conflict:
            return None, []
    candidates = candidates[:20]
    enriched = sel._enrich_candidates(candidates, provider)
    option_lines = []
    for i, item in enumerate(enriched):
        parts = [f"{i + 1}. [{item['code']}] {item['name']}"]
        meta_parts = []
        if item["frequency"]:
            meta_parts.append(item["frequency"])
        if item["unit"]:
            meta_parts.append(item["unit"])
        if item.get("category"):
            meta_parts.append(f"category: {item['category']}")
        evidence_text = " ".join(
            str(item.get(key) or "").strip() for key in ("keywords", "description")
        ).strip()
        if evidence_text:
            evidence_text = re.sub(r"\s+", " ", evidence_text)[:180]
            meta_parts.append(f"evidence: {evidence_text}")
        if item["end_date"]:
            meta_parts.append(f"last data: {item['end_date'][:10]}")
        if item["discontinued"]:
            meta_parts.append("DISCONTINUED")
        if meta_parts:
            parts.append(f"  ({', '.join(meta_parts)})")
        option_lines.append("".join(parts))
    prompt = LLM_SELECTION_PROMPT.format(
        query=query, provider=provider, options="\n".join(option_lines)
    )
    return prompt, candidates


async def call_model(client, url, headers, model, prompt):
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 2000,
        "temperature": 0,
    }
    t0 = time.time()
    resp = await client.post(url, headers=headers, json=payload, timeout=90)
    if resp.status_code == 400 and "temperature" in resp.text:
        payload.pop("temperature")
        resp = await client.post(url, headers=headers, json=payload, timeout=90)
    resp.raise_for_status()
    data = resp.json()
    latency = time.time() - t0
    msg = data["choices"][0]["message"]
    content = (msg.get("content") or "").strip()
    usage = data.get("usage") or {}
    return content, latency, usage


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default=None, help="comma-separated label filter")
    args = ap.parse_args()

    settings = Settings()
    sel = IndicatorSelector()

    print("Building production prompts (retrieval + constraint + enrichment)...")
    prompts = []
    for query, provider, country, accept in CASES:
        prompt, candidates = build_prompt(sel, query, provider, country)
        prompts.append((query, provider, country, accept, prompt, candidates))
        print(f"  {query!r:40} {provider:10} {len(candidates)} candidates")

    models = MODELS
    if args.models:
        wanted = {m.strip() for m in args.models.split(",")}
        models = [m for m in MODELS if m[0] in wanted]

    or_headers = {
        "Authorization": f"Bearer {settings.openrouter_api_key}",
        "Content-Type": "application/json",
    }
    results = {}
    async with httpx.AsyncClient() as client:
        for label, or_model, p_in, p_out in models:
            rows = []
            total_cost = 0.0
            for query, provider, country, accept, prompt, candidates in prompts:
                if prompt is None:
                    rows.append((query, "skip", None, 0.0))
                    continue
                url = LOCAL_URL if or_model is None else OPENROUTER_URL
                headers = {"Content-Type": "application/json"} if or_model is None else or_headers
                model = LOCAL_MODEL if or_model is None else or_model
                try:
                    content, latency, usage = await call_model(client, url, headers, model, prompt)
                    parsed = sel._parse_llm_response(content, candidates, provider, query)
                    cost = (
                        (usage.get("prompt_tokens", 0) * p_in)
                        + (usage.get("completion_tokens", 0) * p_out)
                    ) / 1e6
                    total_cost += cost
                    if parsed is None:
                        verdict, picked = "none", None
                    elif parsed.needs_user_choice:
                        verdict, picked = "asked", None
                    elif parsed.code is None:
                        verdict, picked = "none", None
                    elif parsed.code in accept:
                        verdict, picked = "correct", parsed.code
                    else:
                        verdict, picked = "wrong", parsed.code
                except Exception as exc:  # noqa: BLE001 - eval harness
                    verdict, picked, latency = "error", str(exc)[:60], 0.0
                rows.append((query, verdict, picked, latency))
            n = len([r for r in rows if r[1] != "skip"])
            correct = sum(1 for r in rows if r[1] == "correct")
            asked = sum(1 for r in rows if r[1] == "asked")
            wrong = sum(1 for r in rows if r[1] == "wrong")
            none_ = sum(1 for r in rows if r[1] in ("none", "error"))
            lat = [r[3] for r in rows if r[1] in ("correct", "asked", "wrong", "none")]
            mean_lat = sum(lat) / len(lat) if lat else 0.0
            results[label] = (correct, asked, wrong, none_, n, mean_lat, total_cost, rows)
            print(
                f"\n{label}: {correct}/{n} correct, {asked} asked, {wrong} WRONG, "
                f"{none_} none | mean {mean_lat:.1f}s | ${total_cost*1000:.3f}/1k-selections-est"
            )
            for query, verdict, picked, latency in rows:
                if verdict in ("wrong", "error"):
                    print(f"    ✗ {query!r}: {verdict} -> {picked}")

    print("\n===== SUMMARY (correct | asked | wrong | none | mean s | $/1k sel) =====")
    for label, (c, a, w, n0, n, lat, cost, _rows) in sorted(
        results.items(), key=lambda kv: (-kv[1][0], kv[1][2])
    ):
        print(f"{label:32} {c:2}/{n}  ask={a}  wrong={w}  none={n0}  {lat:5.1f}s  ${cost/len(CASES)*1000:.2f}")
    out = Path("/tmp/selector_model_eval.json")
    out.write_text(json.dumps(
        {label: {"rows": [(q, v, p, round(l, 2)) for q, v, p, l in rows]}
         for label, (*_x, rows) in results.items()}, indent=2))
    print(f"\nDetailed rows: {out}")


if __name__ == "__main__":
    asyncio.run(main())
