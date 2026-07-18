#!/usr/bin/env python3
"""Enrich indicators.db `synonyms` with user-vocabulary search phrases.

WHY: official series titles often lack the words users type — FRED's M2SL is
titled just "M2" (no "money supply"), PAYEMS is "All Employees, Total Nonfarm"
(no "payrolls"), CPILFESL never says "core". FTS therefore can't surface the
canonical series for the most common queries. Synonymy belongs in DATA, not
in runtime expansion rules: this script asks an LLM (the local vLLM model by
default — free) for the 2-6 short phrases a non-expert would type to find
exactly each series, and stores them in the `synonyms` column. The FTS
external-content UPDATE trigger keeps indicators_fts in sync automatically
(synonyms carry bm25 weight 2.0).

Usage:
    source backend/.venv/bin/activate
    python scripts/enrich_indicator_synonyms.py --provider FRED --min-popularity 60
    python scripts/enrich_indicator_synonyms.py --provider FRED --limit 50 --dry-run

Idempotent: rows with non-empty synonyms are skipped unless --force.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

DB_PATH = Path(__file__).resolve().parents[1] / "backend" / "data" / "indicators.db"
LOCAL_URL = "http://localhost:8000/v1/chat/completions"
LOCAL_MODEL = "gpt-oss-120b"
BATCH_SIZE = 20

PROMPT = """You generate search-alias metadata for an economic data catalog.

For EACH series below, list 2-6 SHORT search phrases (1-4 words each) that a
non-expert user would type to find exactly that series. Rules:
- Include the common/popular name when one exists (e.g. "core CPI",
  "nonfarm payrolls", "M2 money supply", "case-shiller index").
- Include the plain-language concept ("inflation rate", "home prices") ONLY
  when this series is a standard headline measure of that concept.
- NEVER include a phrase that better describes a DIFFERENT series (no
  "core CPI" for a trimmed-mean or flexible-price variant; no "unemployment
  rate" for a state-level or NSA variant — say "michigan unemployment rate"
  or "unemployment rate nsa" instead).
- Keep qualifiers that distinguish the series (state names, "NSA",
  "private", "real", tenor like "10 year").
- Lowercase. No explanations.

Series:
{series_block}

Respond with ONLY a JSON object mapping code to a list of phrases:
{{"CODE1": ["phrase a", "phrase b"], "CODE2": [...]}}"""


def fetch_rows(conn, provider: str, min_popularity: int, limit: int, force: bool,
               category: str = None):
    """Select rows to enrich.

    Providers with `popularity` (FRED) rank by it. Providers without it
    (WorldBank/IMF/Eurostat/StatsCan have NULL popularity) must scope by a
    structural signal instead — pass `category` (e.g. WorldBank's
    'World Development Indicators' core database = ~1500 commonly-queried rows)
    so enrichment stays bounded to series users actually query and does not add
    synonym noise to tens of thousands of niche rows.
    """
    where = "provider = ?"
    params = [provider]
    if category:
        where += " AND category = ?"
        params.append(category)
    else:
        # NULL popularity (every provider except FRED) must not exclude rows:
        # COALESCE lets --min-popularity 0 mean "all rows" for such providers
        # while FRED keeps its popularity ranking/floor.
        where += " AND COALESCE(popularity, 0) >= ?"
        params.append(min_popularity)
    if not force:
        where += " AND (synonyms IS NULL OR synonyms = '')"
    params.append(limit)
    cur = conn.execute(
        f"""SELECT code, name, description, unit, frequency, popularity
            FROM indicators WHERE {where}
            ORDER BY popularity DESC, code LIMIT ?""",
        params,
    )
    return cur.fetchall()


def first_sentence(text: str, max_len: int = 160) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if not text:
        return ""
    dot = text.find(". ")
    if 0 < dot < max_len:
        return text[: dot + 1]
    return text[:max_len]


def build_series_block(rows) -> str:
    lines = []
    for code, name, desc, unit, freq, _pop in rows:
        parts = [f"{code} | {name}"]
        meta = ", ".join(p for p in (unit, freq) if p)
        if meta:
            parts.append(f"({meta})")
        sentence = first_sentence(desc)
        if sentence:
            parts.append(f"— {sentence}")
        lines.append(" ".join(parts))
    return "\n".join(lines)


def call_llm(client: httpx.Client, prompt: str) -> dict:
    resp = client.post(
        LOCAL_URL,
        json={
            "model": LOCAL_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 2500,
            "temperature": 0,
            "reasoning_effort": "low",
        },
        timeout=120,
    )
    resp.raise_for_status()
    content = (resp.json()["choices"][0]["message"].get("content") or "").strip()
    match = re.search(r"\{.*\}", content, re.DOTALL)
    if not match:
        raise ValueError(f"no JSON object in response: {content[:200]}")
    return json.loads(match.group(0))


def clean_phrases(phrases, code: str, name: str) -> str:
    out = []
    for phrase in phrases if isinstance(phrases, list) else []:
        p = re.sub(r"\s+", " ", str(phrase or "")).strip().lower()
        p = p.strip('"\'')
        if not p or len(p) > 60 or len(p.split()) > 6:
            continue
        if p in out:
            continue
        out.append(p)
    # The code itself is a legitimate search term users paste.
    code_l = code.lower()
    if code_l not in out and len(code_l) >= 2:
        out.append(code_l)
    return ", ".join(out[:8])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", required=True)
    ap.add_argument("--min-popularity", type=int, default=60)
    ap.add_argument("--limit", type=int, default=5000)
    ap.add_argument("--force", action="store_true")
    ap.add_argument(
        "--append", action="store_true",
        help="Append generated phrases to EXISTING synonyms instead of "
             "replacing them (use with --force for providers whose synonyms "
             "column holds structural aliases, e.g. StatsCan legacy table "
             "numbers, that must be preserved).",
    )
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument(
        "--codes-file", default=None,
        help="Newline-separated provider codes: restrict enrichment to exactly "
             "these rows (e.g. the IMF supportability-passing subset — "
             "enriching unservable codes just polishes menu-filtered "
             "dead-ends). Combined with the other filters.",
    )
    ap.add_argument(
        "--category", default=None,
        help="Scope by indicators.category instead of popularity (for providers "
             "with no popularity signal, e.g. WorldBank 'World Development Indicators').",
    )
    args = ap.parse_args()

    conn = sqlite3.connect(str(DB_PATH), timeout=60)
    # The production backend reads this DB concurrently; WAL + busy timeout
    # let the enrichment writer commit without "database is locked".
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=60000")
    rows = fetch_rows(conn, args.provider, args.min_popularity, args.limit, args.force,
                      category=args.category)
    if args.codes_file:
        wanted = {
            line.strip() for line in Path(args.codes_file).read_text().splitlines()
            if line.strip()
        }
        before = len(rows)
        rows = [r for r in rows if str(r[0]).strip() in wanted]  # SELECT col 0 = code
        print(f"--codes-file: {before} -> {len(rows)} rows")
    _scope = (f"category={args.category!r}" if args.category
              else f"popularity >= {args.min_popularity}")
    print(f"{len(rows)} rows to enrich for {args.provider} ({_scope})")
    if not rows:
        return 0

    client = httpx.Client()
    updated = failed = 0
    t0 = time.time()
    for i in range(0, len(rows), BATCH_SIZE):
        batch = rows[i:i + BATCH_SIZE]
        prompt = PROMPT.format(series_block=build_series_block(batch))
        try:
            mapping = call_llm(client, prompt)
        except Exception as exc:  # noqa: BLE001 - batch isolation
            print(f"  batch {i // BATCH_SIZE}: LLM failed ({exc}); skipping")
            failed += len(batch)
            continue
        for code, name, *_rest in batch:
            phrases = clean_phrases(mapping.get(code), code, name)
            if not phrases:
                failed += 1
                continue
            if args.dry_run:
                print(f"  {code}: {phrases}")
            else:
                if args.append:
                    conn.execute(
                        """UPDATE indicators
                           SET synonyms = CASE
                               WHEN synonyms IS NULL OR synonyms = '' THEN ?
                               ELSE synonyms || ', ' || ?
                           END
                           WHERE provider = ? AND code = ?""",
                        (phrases, phrases, args.provider, code),
                    )
                else:
                    conn.execute(
                        "UPDATE indicators SET synonyms = ? WHERE provider = ? AND code = ?",
                        (phrases, args.provider, code),
                    )
            updated += 1
        if not args.dry_run:
            conn.commit()
        done = min(i + BATCH_SIZE, len(rows))
        rate = done / max(time.time() - t0, 1e-9)
        print(f"  {done}/{len(rows)} ({updated} updated, {failed} failed, "
              f"{rate:.1f} rows/s)")

    conn.commit()
    conn.close()
    print(f"DONE: {updated} updated, {failed} failed in {time.time() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
