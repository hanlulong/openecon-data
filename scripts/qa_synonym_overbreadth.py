#!/usr/bin/env python3
"""QA sweep: find and fix OVER-BROAD LLM-generated synonyms in indicators.db.

WHY: scripts/enrich_indicator_synonyms.py populates the `synonyms` column with
user-vocabulary search phrases. Its contract forbids a synonym BROADER than the
series' own concept. One confirmed violation shipped a production bug: the
HOUSEHOLD-debt series HDTGPDUSQ163N ("Household Debt to GDP for United States")
carried the synonym "debt to gdp" — the phrase dropped the title's qualifying
word "household", so generic debt-to-GDP queries retrieved the household series.

STRUCTURE DETECTS, LLM ADJUDICATES (project rule: semantics live in the LLM,
never in hardcoded rules). This script only STRUCTURALLY flags a signature and
then asks the local LLM whether each flagged phrase truly reads as the broader
concept. The structural signature (mechanical, no semantics):

    A synonym S is SUSPECT when S occurs as a contiguous token-subsequence of
    the series title AND the title token immediately preceding that occurrence
    is a CONTENT word (not a stopword/preposition/article). That preceding
    content word is the "dropped qualifier".

  title "household debt to gdp ..."  syn "debt to gdp"  -> pred "household"  SUSPECT
  title "gross domestic product"     syn "gdp"          -> not a subsequence  ok
                                                            (abbreviation, fine)

A SECOND, order-insensitive signature (the REORDERING class) catches the same
class WHEN THE SYNONYM REORDERS the title's words so it is no longer contiguous:

    title "Household Debt to GDP for Canada", synonym "canada debt to gdp"
    -> S's content tokens {canada, debt, gdp} are a SUBSET of the title's
       content tokens {household, debt, gdp, canada}, and the title carries a
       content word ("household") absent from S -> dropped qualifier -> SUSPECT.

  Reorderings that drop NO content word ("gdp debt" of "Debt to GDP") are NOT
  suspects. It is deduped against the contiguous pass (structurally + report).

Phases (run in order; each is a separate invocation so the DB WRITE stays short):

  scan        READ-ONLY. Emit ..._report_<date>.json (contiguous-subsequence
              suspects) and a provider summary. No DB writes.
  adjudicate  Read the report, ask the local gpt-oss-120b REMOVE/KEEP for each
              suspect, write ..._decisions_<date>.json. READ-ONLY on the DB.
  apply       Read the decisions, dump undo JSON, then remove REMOVE-flagged
              phrases in ONE batched transaction. FTS stays in sync via the
              indicators_au external-content trigger (same as enrichment).
  scan-reorder / adjudicate-reorder / apply-reorder
              The identical pipeline for the reordering signature, writing the
              *_reorder_<date>.json report/decisions/undo files.
  changed-rows  Union of {provider, code} changed across both passes -> the
              targeted re-embed list for the next scheduled index re-blend.

Usage:
    source backend/.venv/bin/activate
    python scripts/qa_synonym_overbreadth.py scan
    python scripts/qa_synonym_overbreadth.py adjudicate           # after scan
    python scripts/qa_synonym_overbreadth.py apply                # after adjudicate
    python scripts/qa_synonym_overbreadth.py scan-reorder         # after scan
    python scripts/qa_synonym_overbreadth.py adjudicate-reorder
    python scripts/qa_synonym_overbreadth.py apply-reorder
    python scripts/qa_synonym_overbreadth.py changed-rows
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
import unicodedata
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

DB_PATH = Path(__file__).resolve().parents[1] / "backend" / "data" / "indicators.db"
SCRIPTS_DIR = Path(__file__).resolve().parent
TODAY = date.today().isoformat()
REPORT_PATH = SCRIPTS_DIR / f"qa_synonym_overbreadth_report_{TODAY}.json"
DECISIONS_PATH = SCRIPTS_DIR / f"qa_synonym_overbreadth_decisions_{TODAY}.json"
UNDO_PATH = SCRIPTS_DIR / f"qa_synonym_overbreadth_undo_{TODAY}.json"

# Second pass: the REORDERING-class signature (order-insensitive subset).
REORDER_REPORT_PATH = SCRIPTS_DIR / f"qa_synonym_overbreadth_report_reorder_{TODAY}.json"
REORDER_DECISIONS_PATH = SCRIPTS_DIR / f"qa_synonym_overbreadth_decisions_reorder_{TODAY}.json"
REORDER_UNDO_PATH = SCRIPTS_DIR / f"qa_synonym_overbreadth_undo_reorder_{TODAY}.json"
CHANGED_ROWS_PATH = SCRIPTS_DIR / f"qa_synonym_overbreadth_changed_rows_{TODAY}.json"

LOCAL_URL = "http://localhost:8000/v1/chat/completions"
LOCAL_MODEL = "gpt-oss-120b"
ADJUDICATE_BATCH = 20

# Function words only (articles, prepositions, conjunctions). A synonym whose
# dropped predecessor is one of these is NOT narrowing the concept — it merely
# starts after a preposition ("rate OF unemployment" -> "unemployment"). The
# dropped word must be a genuine CONTENT qualifier (household, core, youth,
# female, real, a tenor number) for the phrase to be structurally suspect.
STOPWORDS = {
    "a", "an", "the",
    "of", "for", "to", "in", "on", "at", "by", "with", "from", "as", "per",
    "and", "or", "nor", "vs", "versus",
}

# Adjudication scope. The suspect set exceeds the >500 threshold, so scope is
# bounded — but by ROUTING STAKES, not the popularity cap: popularity is ~0 for
# every non-FRED row (measured: 1929/1932 non-FRED/IMF suspects have popularity
# 0), so "top slice by popularity" degenerates to noise outside FRED. Instead
# adjudicate every ECONOMIC provider (the real routing stakes, incl. all FRED
# and all IMF) and DEFER CoinGecko: its suspects are a systematically different
# pattern (crypto token-name drops like "coin" <- "8-Bit Coin"), the largest
# bulk, and the lowest routing stakes. Deferred rows are reported as backlog.
DEFER_PROVIDERS = {"CoinGecko"}
HIGH_STAKES_PROVIDERS = {"FRED", "IMF"}  # always in-scope even if DEFER grows


def _fold(text: str) -> str:
    """Lowercase + strip accents (NFKD, drop combining marks)."""
    norm = unicodedata.normalize("NFKD", str(text or ""))
    return "".join(c for c in norm if not unicodedata.combining(c)).lower()


def tokenize(text: str) -> List[str]:
    """Lowercase, accent-fold, split on any non-alphanumeric run.

    '10-Year' -> ['10', 'year'] ; 'GDP' -> ['gdp'] ; keeps numbers (tenors are
    load-bearing qualifiers) and 2-letter tokens (M2, US)."""
    return [t for t in re.split(r"[^a-z0-9]+", _fold(text)) if t]


def _subsequence_starts(hay: List[str], needle: List[str]) -> List[int]:
    """All start indices where `needle` occurs as a contiguous run in `hay`."""
    if not needle or len(needle) > len(hay):
        return []
    starts = []
    last = len(hay) - len(needle)
    for i in range(last + 1):
        if hay[i:i + len(needle)] == needle:
            starts.append(i)
    return starts


def split_synonyms(raw: str) -> List[str]:
    """Split the stored synonyms column (comma-separated) into phrases."""
    return [p.strip() for p in str(raw or "").split(",") if p.strip()]


def detect_suspects_for_row(
    code: str, title: str, synonyms_raw: str
) -> List[Dict[str, str]]:
    """Return the structural suspects for one row (may be empty).

    A phrase is suspect iff it is a contiguous token-subsequence of the title
    whose immediately-preceding title token is a content word, AND it does NOT
    also occur at the title's start (index 0 = the head concept, not a drop),
    AND it is neither the whole title nor the provider code itself.
    """
    title_tokens = tokenize(title)
    if not title_tokens:
        return []
    code_l = _fold(code).strip()
    suspects: List[Dict[str, str]] = []
    for phrase in split_synonyms(synonyms_raw):
        syn_tokens = tokenize(phrase)
        if not syn_tokens:
            continue
        # The provider code alias is a legitimate paste-in search term, never a
        # dropped-qualifier concept.
        if _fold(phrase).strip() == code_l:
            continue
        # A synonym equal to the entire title cannot have dropped a qualifier.
        if syn_tokens == title_tokens:
            continue
        starts = _subsequence_starts(title_tokens, syn_tokens)
        if not starts:
            continue
        # Occurs at the head of the title -> it IS the leading concept, not a
        # narrowing drop; skip even if it also appears later.
        if 0 in starts:
            continue
        # Flag on the first occurrence whose predecessor is a content word.
        dropped = None
        for s in starts:
            pred = title_tokens[s - 1]
            if pred not in STOPWORDS and len(pred) >= 2:
                dropped = pred
                break
        if dropped is None:
            continue
        suspects.append(
            {
                "suspect_synonym": phrase.strip(),
                "dropped_qualifier": dropped,
            }
        )
    return suspects


def _content_tokens(text: str) -> List[str]:
    """Title/synonym tokens with function words (STOPWORDS: articles,
    prepositions, conjunctions, "for"/"of"/country connectors) and 1-char
    tokens dropped — i.e. the CONTENT words that carry the concept."""
    return [t for t in tokenize(text) if t not in STOPWORDS and len(t) >= 2]


def detect_reorder_suspects_for_row(
    code: str, title: str, synonyms_raw: str, sig1_pairs: set,
    max_dropped: Optional[int] = None,
) -> List[Dict[str, str]]:
    """REORDERING-class suspects (second structural signature).

    A synonym S is suspect when, ORDER-INSENSITIVELY:
      (a) S's content tokens are a SUBSET of the title's content tokens
          (accent-folded, stopwords/prepositions/connectors dropped), AND
      (b) the title has >=1 content word absent from S (the dropped qualifier),
          AND
      (c) S is NOT already caught by the contiguous-subsequence signature —
          checked structurally (detect_suspects_for_row) AND deduped against the
          first pass's report (``sig1_pairs`` = {(code, folded_synonym)}).

    Shape (the original production-bug class that reordering hides from the
    contiguous signature): title "Household Debt to GDP for Canada",
    synonym "canada debt to gdp" -> tokens {canada, debt, gdp} subset of
    {household, debt, gdp, canada}; dropped {household} -> SUSPECT.

    Reorderings that drop NO content word (e.g. "gdp debt" of "Debt to GDP")
    fail (b) and are NOT suspects. Abbreviations (syn "gdp" of spelled-out
    "Gross Domestic Product") fail (a) — "gdp" is not a title content token.
    """
    title_content = set(_content_tokens(title))
    # Need >=2 distinct content words for a drop to be possible at all.
    if len(title_content) < 2:
        return []
    code_l = _fold(code).strip()
    suspects: List[Dict[str, str]] = []
    for phrase in split_synonyms(synonyms_raw):
        if not tokenize(phrase):
            continue
        folded = _fold(phrase).strip()
        if folded == code_l:
            continue
        if (code, folded) in sig1_pairs:
            continue
        # (c) structural: skip anything the contiguous signature would flag.
        if detect_suspects_for_row(code, title, phrase):
            continue
        syn_content = set(_content_tokens(phrase))
        if not syn_content:
            continue
        # (a) order-insensitive subset.
        if not syn_content <= title_content:
            continue
        # (b) at least one dropped content word.
        dropped = title_content - syn_content
        if not dropped:
            continue
        # Precision knob: the over-broad-by-ONE-qualifier bug shape drops a
        # SMALL number of content words (the household case drops exactly 1).
        # A synonym dropping MANY content words is almost always a legitimate
        # short alias of a verbose title (e.g. "ground beef price per pound" of
        # "Average Price: Ground Beef, 100% Beef (Cost per Pound/453.6 Grams)"),
        # NOT over-broad. max_dropped bounds the pass to the bug shape.
        if max_dropped is not None and len(dropped) > max_dropped:
            continue
        suspects.append(
            {
                "suspect_synonym": phrase.strip(),
                "dropped_qualifier": ", ".join(sorted(dropped)),
            }
        )
    return suspects


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=60)
    # Match the enrichment writer: the production backend reads this DB
    # concurrently, so WAL + a long busy timeout let us commit without
    # "database is locked" and let readers keep going during the scan.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=60000")
    return conn


def cmd_scan(_args) -> int:
    conn = _connect()
    cur = conn.execute(
        "SELECT provider, code, name, synonyms, COALESCE(popularity, 0) "
        "FROM indicators WHERE synonyms IS NOT NULL AND synonyms != ''"
    )
    rows = cur.fetchall()
    conn.close()

    suspects: List[Dict] = []
    by_provider: Dict[str, int] = {}
    scanned = 0
    for provider, code, name, synonyms_raw, popularity in rows:
        scanned += 1
        for hit in detect_suspects_for_row(code, name, synonyms_raw):
            rec = {
                "provider": provider,
                "code": code,
                "title": name,
                "synonyms_full": synonyms_raw,
                "suspect_synonym": hit["suspect_synonym"],
                "dropped_qualifier": hit["dropped_qualifier"],
                "popularity": int(popularity or 0),
            }
            suspects.append(rec)
            by_provider[provider] = by_provider.get(provider, 0) + 1

    report = {
        "generated": TODAY,
        "db_path": str(DB_PATH),
        "rows_scanned": scanned,
        "total_suspects": len(suspects),
        "suspects_by_provider": dict(
            sorted(by_provider.items(), key=lambda kv: kv[1], reverse=True)
        ),
        "suspects": suspects,
    }
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"scanned {scanned} rows with synonyms")
    print(f"total structural suspects: {len(suspects)}")
    for prov, n in report["suspects_by_provider"].items():
        print(f"  {prov:12s} {n}")
    print(f"report -> {REPORT_PATH}")
    return 0


def _load_sig1_pairs() -> set:
    """{(code, folded_synonym)} from the first-pass report, for dedupe (c)."""
    if not REPORT_PATH.exists():
        return set()
    rep = json.loads(REPORT_PATH.read_text())
    return {
        (s["code"], _fold(s["suspect_synonym"]).strip())
        for s in rep.get("suspects", [])
    }


def cmd_scan_reorder(args) -> int:
    """Read-only scan for the REORDERING-class signature (second pass)."""
    if not REPORT_PATH.exists():
        print(f"missing first-pass report {REPORT_PATH} — run `scan` first so the "
              f"reorder pass can dedupe against it", file=sys.stderr)
        return 1
    max_dropped = getattr(args, "max_dropped", None)
    sig1_pairs = _load_sig1_pairs()
    conn = _connect()
    rows = conn.execute(
        "SELECT provider, code, name, synonyms, COALESCE(popularity, 0) "
        "FROM indicators WHERE synonyms IS NOT NULL AND synonyms != ''"
    ).fetchall()
    conn.close()

    suspects: List[Dict] = []
    by_provider: Dict[str, int] = {}
    scanned = 0
    for provider, code, name, synonyms_raw, popularity in rows:
        scanned += 1
        for hit in detect_reorder_suspects_for_row(
            code, name, synonyms_raw, sig1_pairs, max_dropped=max_dropped
        ):
            suspects.append({
                "provider": provider, "code": code, "title": name,
                "synonyms_full": synonyms_raw,
                "suspect_synonym": hit["suspect_synonym"],
                "dropped_qualifier": hit["dropped_qualifier"],
                "popularity": int(popularity or 0),
            })
            by_provider[provider] = by_provider.get(provider, 0) + 1

    report = {
        "generated": TODAY,
        "db_path": str(DB_PATH),
        "signature": "reordering (order-insensitive subset + dropped content word)",
        "max_dropped": max_dropped,
        "rows_scanned": scanned,
        "total_suspects": len(suspects),
        "suspects_by_provider": dict(
            sorted(by_provider.items(), key=lambda kv: kv[1], reverse=True)
        ),
        "suspects": suspects,
    }
    REORDER_REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    # Economic-provider count = the >~3000 gate the lead set.
    econ = sum(n for p, n in by_provider.items() if p not in DEFER_PROVIDERS)
    print(f"scanned {scanned} rows with synonyms")
    print(f"total reorder suspects: {len(suspects)} (economic providers: {econ})")
    for prov, n in report["suspects_by_provider"].items():
        print(f"  {prov:12s} {n}")
    print(f"report -> {REORDER_REPORT_PATH}")
    return 0


def _in_adjudication_scope(suspects: List[Dict]) -> Tuple[List[Dict], List[Dict]]:
    """Split suspects into (adjudicated, deferred) by routing stakes.

    Adjudicate every economic-provider suspect (all FRED + IMF are always in
    scope); defer DEFER_PROVIDERS (CoinGecko) as reported backlog.
    """
    def _defer(s: Dict) -> bool:
        return (
            s["provider"] in DEFER_PROVIDERS
            and s["provider"] not in HIGH_STAKES_PROVIDERS
        )

    adjudicate = [s for s in suspects if not _defer(s)]
    deferred = [s for s in suspects if _defer(s)]
    return adjudicate, deferred


ADJUDICATE_PROMPT = """You are auditing search-alias metadata for an economic \
data catalog. Each series has a precise title. A synonym phrase was generated \
to help users find that series, but some synonyms are TOO BROAD: standing \
alone, they name a MORE GENERAL concept than this specific series, so they \
would wrongly capture generic queries.

For each item below decide, for the SYNONYM standing completely alone (as a \
user's whole query), whether it reads as the BROADER/general concept rather \
than THIS specific series:
- REMOVE if the synonym alone would be understood as the general concept and a \
  user typing it most likely wants the headline/general series, NOT this \
  narrower one (e.g. title "Household Debt to GDP", synonym "debt to gdp" -> \
  REMOVE; the user wants total/general debt-to-GDP).
- KEEP if the synonym alone still unambiguously means THIS series, or is a \
  standard popular name for it, or the dropped word does not actually narrow \
  the meaning.

Items:
{block}

Respond with ONLY a JSON object mapping each item id (as a string) to \
{{"decision": "REMOVE" or "KEEP", "reason": "<=12 words"}}:
{{"0": {{"decision": "KEEP", "reason": "..."}}, "1": {{"decision": "REMOVE", "reason": "..."}}}}"""


def _adjudicate_batch(client: httpx.Client, batch: List[Tuple[int, Dict]]) -> Dict[int, Dict]:
    lines = []
    for idx, s in batch:
        lines.append(
            f'{idx}. series title: "{s["title"]}" | synonym: "{s["suspect_synonym"]}"'
            f' | dropped word: "{s["dropped_qualifier"]}"'
        )
    prompt = ADJUDICATE_PROMPT.format(block="\n".join(lines))
    resp = client.post(
        LOCAL_URL,
        json={
            "model": LOCAL_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 3000,
            "temperature": 0,
            "reasoning_effort": "low",
        },
        timeout=180,
    )
    resp.raise_for_status()
    content = (resp.json()["choices"][0]["message"].get("content") or "").strip()
    match = re.search(r"\{.*\}", content, re.DOTALL)
    if not match:
        raise ValueError(f"no JSON object in response: {content[:200]}")
    parsed = json.loads(match.group(0))
    out: Dict[int, Dict] = {}
    for idx, _s in batch:
        entry = parsed.get(str(idx)) or parsed.get(idx)
        if isinstance(entry, dict):
            decision = str(entry.get("decision", "")).strip().upper()
            reason = str(entry.get("reason", "")).strip()
            out[idx] = {
                "decision": "REMOVE" if decision == "REMOVE" else "KEEP",
                "reason": reason,
            }
    return out


def _run_adjudicate(report_path: Path, decisions_path: Path) -> int:
    if not report_path.exists():
        print(f"missing report: {report_path} — run the matching scan first",
              file=sys.stderr)
        return 1
    report = json.loads(report_path.read_text())
    suspects = report["suspects"]
    adjudicated, deferred = _in_adjudication_scope(suspects)
    print(f"{len(suspects)} suspects; adjudicating {len(adjudicated)}, "
          f"deferring {len(deferred)}")

    client = httpx.Client()
    decisions: List[Dict] = []
    remove_ct = keep_ct = fail_ct = 0
    t0 = time.time()
    indexed = list(enumerate(adjudicated))
    for i in range(0, len(indexed), ADJUDICATE_BATCH):
        batch = indexed[i:i + ADJUDICATE_BATCH]
        try:
            verdicts = _adjudicate_batch(client, batch)
        except Exception as exc:  # noqa: BLE001 - batch isolation
            print(f"  batch {i // ADJUDICATE_BATCH}: LLM failed ({exc}); KEEP-defaulting")
            verdicts = {}
        for idx, s in batch:
            v = verdicts.get(idx)
            if v is None:
                fail_ct += 1
                decision, reason = "KEEP", "adjudication failed — kept (fail-safe)"
            else:
                decision, reason = v["decision"], v["reason"]
                if decision == "REMOVE":
                    remove_ct += 1
                else:
                    keep_ct += 1
            decisions.append({**s, "decision": decision, "reason": reason})
        done = min(i + ADJUDICATE_BATCH, len(indexed))
        print(f"  {done}/{len(indexed)} ({remove_ct} REMOVE, {keep_ct} KEEP, "
              f"{fail_ct} fail-kept, {done / max(time.time() - t0, 1e-9):.1f}/s)")

    out = {
        "generated": TODAY,
        "adjudicated": len(decisions),
        "deferred": len(deferred),
        "remove": remove_ct,
        "keep": keep_ct,
        "fail_kept": fail_ct,
        "decisions": decisions,
        "deferred_suspects": deferred,
    }
    decisions_path.write_text(json.dumps(out, ensure_ascii=False, indent=2))
    print(f"REMOVE={remove_ct} KEEP={keep_ct} fail-kept={fail_ct} "
          f"deferred={len(deferred)}")
    print(f"decisions -> {decisions_path}")
    return 0


def cmd_adjudicate(_args) -> int:
    return _run_adjudicate(REPORT_PATH, DECISIONS_PATH)


def cmd_adjudicate_reorder(_args) -> int:
    return _run_adjudicate(REORDER_REPORT_PATH, REORDER_DECISIONS_PATH)


def _remove_phrase(synonyms_raw: str, phrase: str) -> str:
    """Remove exactly `phrase` from a comma-joined synonyms string, preserving
    the order and the other phrases. Case/space-insensitive match on the phrase
    token content."""
    target = _fold(phrase).strip()
    kept = [p for p in split_synonyms(synonyms_raw) if _fold(p).strip() != target]
    return ", ".join(kept)


def _run_apply(decisions_path: Path, undo_path: Path) -> int:
    if not decisions_path.exists():
        print(f"missing decisions: {decisions_path} — run the matching adjudicate first",
              file=sys.stderr)
        return 1
    data = json.loads(decisions_path.read_text())
    removes = [d for d in data["decisions"] if d.get("decision") == "REMOVE"]
    if not removes:
        print("no REMOVE decisions — nothing to apply")
        return 0

    # Group removals by (provider, code): a single row may have multiple flagged
    # phrases, and each must be stripped from the same synonyms string.
    by_row: Dict[Tuple[str, str], List[str]] = {}
    for d in removes:
        by_row.setdefault((d["provider"], d["code"]), []).append(d["suspect_synonym"])

    conn = _connect()
    # Snapshot current values FIRST (undo record) and recompute new values from
    # the live DB (not the stale report) so a concurrent enrichment can't be
    # clobbered by a stale synonyms string.
    undo: List[Dict] = []
    updates: List[Tuple[str, str, str]] = []  # (new_synonyms, provider, code)
    skipped = 0
    for (provider, code), phrases in by_row.items():
        row = conn.execute(
            "SELECT synonyms FROM indicators WHERE provider = ? AND code = ?",
            (provider, code),
        ).fetchone()
        if row is None:
            skipped += 1
            continue
        old = row[0] or ""
        new = old
        for phrase in phrases:
            new = _remove_phrase(new, phrase)
        if new == old:
            skipped += 1
            continue
        undo.append({"provider": provider, "code": code, "old_synonyms": old,
                     "new_synonyms": new, "removed_phrases": phrases})
        updates.append((new, provider, code))

    # Write the undo file BEFORE touching the DB — the DB is not in git.
    undo_path.write_text(json.dumps(
        {"generated": TODAY, "db_path": str(DB_PATH), "rows": undo},
        ensure_ascii=False, indent=2,
    ))
    print(f"undo -> {undo_path} ({len(undo)} rows)")

    # Single short transaction: the indicators_au external-content trigger keeps
    # indicators_fts in sync automatically on each UPDATE (verified against the
    # live schema — same mechanism the enrichment script relies on).
    t0 = time.time()
    conn.execute("BEGIN")
    try:
        conn.executemany(
            "UPDATE indicators SET synonyms = ? WHERE provider = ? AND code = ?",
            updates,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        conn.close()
        raise
    conn.close()
    print(f"applied {len(updates)} updates, skipped {skipped}, "
          f"in {time.time() - t0:.2f}s")
    return 0


def cmd_apply(_args) -> int:
    return _run_apply(DECISIONS_PATH, UNDO_PATH)


def cmd_apply_reorder(_args) -> int:
    return _run_apply(REORDER_DECISIONS_PATH, REORDER_UNDO_PATH)


def cmd_changed_rows(_args) -> int:
    """Emit the union of {provider, code} changed across BOTH passes.

    Handed to the next scheduled index re-blend as the targeted re-embed list
    (the re-embed itself is deferred — safe-direction staleness)."""
    seen: set = set()
    rows: List[Dict[str, str]] = []
    for undo_path in (UNDO_PATH, REORDER_UNDO_PATH):
        if not undo_path.exists():
            continue
        for r in json.loads(undo_path.read_text()).get("rows", []):
            key = (r["provider"], r["code"])
            if key in seen:
                continue
            seen.add(key)
            rows.append({"provider": r["provider"], "code": r["code"]})
    CHANGED_ROWS_PATH.write_text(json.dumps(
        {"generated": TODAY, "db_path": str(DB_PATH), "count": len(rows),
         "rows": rows}, ensure_ascii=False, indent=2,
    ))
    print(f"combined changed rows: {len(rows)} -> {CHANGED_ROWS_PATH}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="phase", required=True)
    sub.add_parser("scan", help="read-only contiguous-subsequence scan -> report")
    sub.add_parser("adjudicate", help="LLM REMOVE/KEEP over the scan report")
    sub.add_parser("apply", help="apply REMOVEs in one transaction + undo dump")
    p_reorder = sub.add_parser("scan-reorder", help="read-only reordering-class scan -> report")
    p_reorder.add_argument(
        "--max-dropped", type=int, default=None,
        help="Only flag synonyms dropping <= N content words (the bug shape is a "
             "SMALL drop; many-word drops are legitimate short aliases of verbose "
             "titles). Omit for the full unbounded signature.",
    )
    sub.add_parser("adjudicate-reorder", help="LLM REMOVE/KEEP over the reorder report")
    sub.add_parser("apply-reorder", help="apply reorder REMOVEs + reorder undo dump")
    sub.add_parser("changed-rows", help="union {provider,code} across both passes")
    args = ap.parse_args()
    return {
        "scan": cmd_scan,
        "adjudicate": cmd_adjudicate,
        "apply": cmd_apply,
        "scan-reorder": cmd_scan_reorder,
        "adjudicate-reorder": cmd_adjudicate_reorder,
        "apply-reorder": cmd_apply_reorder,
        "changed-rows": cmd_changed_rows,
    }[args.phase](args)


if __name__ == "__main__":
    raise SystemExit(main())
