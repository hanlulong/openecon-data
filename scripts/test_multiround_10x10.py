#!/usr/bin/env python3
"""
Oracle-bearing multi-round conversation test harness.

Unlike the older harness, PASS now means the response matched a round-level
semantic oracle rather than merely returning some data.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.multiround_suites import (  # noqa: E402
    DEFAULT_SUITE_NAME,
    SUITES_VERSION,
    RoundCase,
    get_suite_description,
    list_suite_descriptions,
    load_suite,
)

REPORT_DIR = ROOT / "docs" / "testing" / "reports"
BASE_URL = os.environ.get("OPENECON_MULTIROUND_BASE_URL", "http://localhost:3001").rstrip("/")
TIMEOUT = 90
DEFAULT_REPORT_PATH = os.environ.get("OPENECON_MULTIROUND_REPORT")
DEFAULT_SUITE = os.environ.get("OPENECON_MULTIROUND_SUITE", DEFAULT_SUITE_NAME)
MIN_EFFECTIVE_RATE = float(os.environ.get("OPENECON_MULTIROUND_MIN_EFFECTIVE_RATE", "0.90"))
MAX_FAILS = int(os.environ.get("OPENECON_MULTIROUND_MAX_FAILS", "0"))
REQUEST_TIMEOUT = int(os.environ.get("OPENECON_MULTIROUND_REQUEST_TIMEOUT", str(TIMEOUT)))
MAX_RETRIES = int(os.environ.get("OPENECON_MULTIROUND_MAX_RETRIES", "2"))
RETRY_DELAY_SECONDS = float(os.environ.get("OPENECON_MULTIROUND_RETRY_DELAY_SECONDS", "3"))
CONNECTION_RETRY_DELAY_SECONDS = float(os.environ.get("OPENECON_MULTIROUND_CONNECTION_RETRY_DELAY_SECONDS", "5"))
ROUND_DELAY_SECONDS = float(os.environ.get("OPENECON_MULTIROUND_ROUND_DELAY_SECONDS", "2"))
BETWEEN_TEST_DELAY_SECONDS = float(os.environ.get("OPENECON_MULTIROUND_BETWEEN_TEST_DELAY_SECONDS", "3"))
HEALTH_RETRIES = int(os.environ.get("OPENECON_MULTIROUND_HEALTH_RETRIES", "3"))
HEALTH_RETRY_DELAY_SECONDS = float(os.environ.get("OPENECON_MULTIROUND_HEALTH_RETRY_DELAY_SECONDS", "5"))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(line_buffering=True)


def normalize_provider_name(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    collapsed = " ".join(text.replace("_", " ").split()).upper()
    if "WORLD BANK" in collapsed or "WORLDBANK" in collapsed:
        return "WORLDBANK"
    if "STATISTICS CANADA" in collapsed or "STATSCAN" in collapsed:
        return "STATSCAN"
    if "COINGECKO" in collapsed:
        return "COINGECKO"
    if "EXCHANGE" in collapsed:
        return "EXCHANGERATE"
    if "COMTRADE" in collapsed:
        return "COMTRADE"
    if "EUROSTAT" in collapsed:
        return "EUROSTAT"
    if collapsed.startswith("FRED"):
        return "FRED"
    if "IMF" in collapsed:
        return "IMF"
    if "BIS" in collapsed:
        return "BIS"
    if "OECD" in collapsed:
        return "OECD"
    return collapsed.replace(" ", "")


def normalize_country(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        from backend.routing.country_resolver import CountryResolver

        alias = CountryResolver.COUNTRY_ALIASES.get(text.lower())
        if alias:
            return str(alias).upper()
        simplified = re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()
        alias = CountryResolver.COUNTRY_ALIASES.get(simplified)
        if alias:
            return str(alias).upper()
        if "," in text:
            before_comma = text.split(",", 1)[0].strip().lower()
            alias = CountryResolver.COUNTRY_ALIASES.get(before_comma)
            if alias:
                return str(alias).upper()
    except Exception:
        pass
    if len(text) in {2, 3} and text.isalpha():
        return text.upper()
    return text.upper()


_CANADA_SUBNATIONALS = {
    "ALBERTA",
    "BRITISH COLUMBIA",
    "MANITOBA",
    "NEW BRUNSWICK",
    "NEWFOUNDLAND AND LABRADOR",
    "NOVA SCOTIA",
    "ONTARIO",
    "PRINCE EDWARD ISLAND",
    "QUEBEC",
    "SASKATCHEWAN",
    "NUNAVUT",
    "YUKON",
    "NORTHWEST TERRITORIES",
}


def augment_scope_countries(provider: str, country: str) -> set[str]:
    countries = set()
    normalized = normalize_country(country)
    if normalized:
        countries.add(normalized)
    if provider == "STATSCAN" and normalized in _CANADA_SUBNATIONALS:
        countries.add("CA")
    return countries


def normalize_cue_text(text: str) -> str:
    normalized = text.lower().replace("_", " ")
    normalized = re.sub(r"(\d+)\s+to\s+(\d+)", r"\1-\2", normalized)
    normalized = re.sub(r"(\d+)\s*-\s*(\d+)", r"\1-\2", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized


def semantic_tags_from_observed(
    *,
    providers: list[str],
    series_ids: list[str],
    cue_text: str,
) -> set[str]:
    text = normalize_cue_text(cue_text)
    tags: set[str] = set()

    if "gdp" in text or "gross domestic product" in text or any("GDP" in series_id for series_id in series_ids):
        tags.add("gdp")
    if "hicp" in text or any("HICP" in series_id for series_id in series_ids):
        tags.add("hicp")
    if (
        "cpi" in text
        or "consumer price index" in text
        or "consumer prices" in text
        or any("CPI" in series_id for series_id in series_ids)
    ):
        tags.add("cpi")
    if "inflation" in text or "consumer price" in text or "hicp" in text or "cpi" in text or any("CPI" in series_id or "HICP" in series_id for series_id in series_ids):
        tags.add("inflation")
    if "trade balance" in text or "external balance" in text or any("RSB" in series_id for series_id in series_ids):
        tags.add("trade balance")
    if "exchange rate" in text or "currency" in text or " forex " in f" {text} " or " fx " in f" {text} " or "EXCHANGERATE" in providers or any(series_id.startswith("DEX") for series_id in series_ids):
        tags.add("exchange")
    if "employment" in text:
        tags.add("employment")
    if "unemployment" in text:
        tags.add("unemployment")
    if "per capita" in text or any(
        ".PCAP." in series_id
        or series_id in {"NGDPDPC", "NY.GDP.PCAP.CD", "TEC00114", "A939RX0Q048SBEA"}
        for series_id in series_ids
    ):
        tags.add("per capita")
    if "ppp" in text or any(".PP." in series_id for series_id in series_ids):
        tags.add("ppp")
    if "deflator" in text or any("GDPDEF" in series_id or ".DEFL." in series_id for series_id in series_ids):
        tags.add("deflator")
    if "current us$" in text or "nominal" in text or any(series_id in {"GDP", "NY.GDP.MKTP.CD"} for series_id in series_ids):
        tags.add("nominal")
    if "real gdp" in text or "constant prices" in text or "real gross domestic" in text or any(series_id in {"GDPC1", "NGDPRSAXDCUSQ"} for series_id in series_ids):
        tags.add("real")
    if "growth" in text or "annual %" in text or "annual percent" in text or "rate of change" in text or any(
        series_id in {"A191RL1Q225SBEA", "NGDP_RPCH", "NY.GDP.MKTP.KD.ZG"} for series_id in series_ids
    ):
        tags.add("growth")
    if "debt" in text:
        tags.add("debt")
    if "current account" in text or any("BCA" in series_id or "BN.CAB" in series_id for series_id in series_ids):
        tags.add("current account")
    if "export" in text or any(".EXP." in series_id for series_id in series_ids):
        tags.add("export")
    if "import" in text or any(".IMP." in series_id for series_id in series_ids):
        tags.add("import")
    return tags


def detect_clarification(resp_json: dict[str, Any]) -> bool:
    if resp_json.get("clarificationNeeded"):
        return True
    if resp_json.get("clarificationOptions"):
        return True
    if resp_json.get("clarificationQuestions"):
        return True
    error = str(resp_json.get("error") or "")
    if any(word in error.lower() for word in ["clarif", "ambiguous", "did you mean"]):
        return True
    response_text = str(resp_json.get("response") or "")
    return any(
        phrase in response_text.lower()
        for phrase in ["could you clarify", "did you mean", "please specify", "which specific", "ambiguous"]
    )


def dataset_has_values(dataset: dict[str, Any]) -> bool:
    for key in ["data", "values", "observations", "time_series", "timeSeries", "chart_data", "chartData"]:
        value = dataset.get(key)
        if isinstance(value, list) and len(value) > 0:
            return True
        if isinstance(value, dict) and len(value) > 0:
            return True
    return False


def collect_datasets(resp_json: dict[str, Any]) -> list[dict[str, Any]]:
    data = resp_json.get("data")
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        nested = data.get("datasets")
        if isinstance(nested, list):
            return [item for item in nested if isinstance(item, dict)]
        return [data]
    results = resp_json.get("results")
    if isinstance(results, list):
        return [item for item in results if isinstance(item, dict)]
    return []


def _dataset_points(dataset: dict[str, Any]) -> list[Any]:
    for key in ["data", "values", "observations", "time_series", "timeSeries", "chart_data", "chartData"]:
        value = dataset.get(key)
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            return list(value.values())
    return []


def _extract_year(value: Any) -> int | None:
    text = str(value or "").strip()
    match = re.search(r"\b(19\d{2}|20\d{2})\b", text)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _cue_present(cue: str, observed: dict[str, Any]) -> bool:
    cue_norm = normalize_cue_text(cue)
    if not cue_norm:
        return False
    cue_aliases = {
        "male": {"male", "males", "men"},
        "males": {"male", "males", "men"},
        "female": {"female", "females", "women"},
        "females": {"female", "females", "women"},
        "men": {"male", "males", "men"},
        "women": {"female", "females", "women"},
    }
    aliases = cue_aliases.get(cue_norm, {cue_norm})
    if cue_norm in {str(tag).lower() for tag in observed.get("semantic_tags", [])}:
        return True
    cue_text = str(observed.get("cue_text") or "")
    return any(
        re.search(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", cue_text)
        for alias in aliases
    )


def extract_observed(resp_json: dict[str, Any]) -> dict[str, Any]:
    datasets = collect_datasets(resp_json)
    metadata = [(dataset.get("metadata") or {}) for dataset in datasets]
    intent = resp_json.get("intent") or {}
    providers = set()
    countries = set()
    series_ids = set()
    frequencies = set()
    text_parts: list[str] = []
    point_counts: list[int] = []
    years_seen: list[int] = []

    top_level_provider = normalize_provider_name(resp_json.get("source") or resp_json.get("provider") or intent.get("apiProvider"))
    if top_level_provider:
        providers.add(top_level_provider)

    for meta in metadata:
        provider = normalize_provider_name(meta.get("source") or meta.get("provider"))
        if provider:
            providers.add(provider)
        for country in augment_scope_countries(provider, str(meta.get("country") or "")):
            countries.add(country)
        series_id = str(meta.get("seriesId") or meta.get("series_id") or "").strip().upper()
        if series_id:
            series_ids.add(series_id)
        frequency = str(meta.get("frequency") or "").strip().lower()
        if frequency:
            frequencies.add(frequency)
        text_parts.extend(
            [
                str(meta.get("indicator") or ""),
                str(meta.get("description") or ""),
                str(meta.get("seriesId") or ""),
            ]
        )

    for dataset in datasets:
        points = _dataset_points(dataset)
        if points:
            point_counts.append(len(points))
        for point in points:
            if isinstance(point, dict):
                year = _extract_year(point.get("date") or point.get("refPer") or point.get("period"))
            else:
                year = _extract_year(point)
            if year is not None:
                years_seen.append(year)

    response_text = str(resp_json.get("response") or "")
    text_parts.append(response_text)
    text_parts.extend(str(option.get("label") or "") for option in (resp_json.get("clarificationOptions") or []) if isinstance(option, dict))
    text_parts.extend(str(value) for value in (intent.get("indicators") or []) if value)
    params = intent.get("parameters") or {}
    if isinstance(params, dict):
        for key, value in params.items():
            if isinstance(value, list):
                text_parts.extend(str(item) for item in value if item)
            elif isinstance(value, dict):
                text_parts.extend(str(v) for v in value.values() if v)
            elif value:
                text_parts.append(str(value))

    populated_series_count = sum(1 for dataset in datasets if dataset_has_values(dataset))
    if populated_series_count == 0 and datasets:
        populated_series_count = len(datasets)
    scope_cardinality = max(populated_series_count, len(series_ids))
    if scope_cardinality == 0:
        scope_cardinality = len(countries)
    semantic_tags = semantic_tags_from_observed(
        providers=sorted(providers),
        series_ids=sorted(series_ids),
        cue_text=" ".join(part for part in text_parts if part),
    )

    return {
        "providers": sorted(providers),
        "countries": sorted(countries),
        "series_ids": sorted(series_ids),
        "frequencies": sorted(frequencies),
        "series_count": populated_series_count,
        "scope_cardinality": scope_cardinality,
        "min_points_per_series": min(point_counts) if point_counts else 0,
        "max_points_per_series": max(point_counts) if point_counts else 0,
        "earliest_year": min(years_seen) if years_seen else None,
        "latest_year": max(years_seen) if years_seen else None,
        "clarification_detected": detect_clarification(resp_json),
        "cue_text": normalize_cue_text(" ".join(part for part in text_parts if part)),
        "semantic_tags": sorted(semantic_tags),
    }


def evaluate_round(case: RoundCase, resp_json: dict[str, Any]) -> tuple[str, str, str, list[str], dict[str, Any]]:
    oracle = case.oracle
    observed = extract_observed(resp_json)
    reasons: list[str] = []

    if resp_json.get("error") and not observed["clarification_detected"]:
        reasons.append(f"error={resp_json.get('error')}")

    if oracle.expect_clarification:
        if not observed["clarification_detected"]:
            reasons.append("expected_clarification_missing")
        for cue in oracle.required_option_cues:
            if cue.lower() not in observed["cue_text"]:
                reasons.append(f"missing_option_cue={cue}")
    else:
        if observed["clarification_detected"]:
            reasons.append("unexpected_clarification")

    if not oracle.expect_clarification:
        observed_cardinality = max(observed["series_count"], observed["scope_cardinality"])
        if observed_cardinality < oracle.min_series_count:
            reasons.append(f"series_count<{oracle.min_series_count}")
        if oracle.exact_series_count is not None and observed_cardinality != oracle.exact_series_count:
            reasons.append(f"series_count!={oracle.exact_series_count}")
        if oracle.min_points_per_series and observed["min_points_per_series"] < oracle.min_points_per_series:
            reasons.append(f"points_per_series<{oracle.min_points_per_series}")

    if oracle.accepted_providers:
        provider_overlap = set(observed["providers"]) & set(oracle.accepted_providers)
        if not provider_overlap:
            reasons.append(f"provider_mismatch expected~={oracle.accepted_providers} actual={observed['providers']}")

    if oracle.accepted_frequencies:
        observed_freqs = {str(freq).lower() for freq in observed["frequencies"]}
        expected_freqs = {str(freq).lower() for freq in oracle.accepted_frequencies}
        if not (observed_freqs & expected_freqs):
            reasons.append(f"frequency_mismatch expected~={oracle.accepted_frequencies} actual={observed['frequencies']}")

    if oracle.required_countries:
        missing = sorted(set(oracle.required_countries) - set(observed["countries"]))
        if missing:
            reasons.append(f"missing_countries={missing}")

    if oracle.forbidden_countries:
        unexpected = sorted(set(oracle.forbidden_countries) & set(observed["countries"]))
        if unexpected:
            reasons.append(f"forbidden_countries_present={unexpected}")

    if oracle.accepted_series_ids:
        acceptable_series = {series_id.upper() for series_id in oracle.accepted_series_ids}
        overlap = {
            observed_id
            for observed_id in observed["series_ids"]
            if observed_id in acceptable_series or any(observed_id.startswith(prefix) for prefix in acceptable_series)
        }
        if not overlap:
            reasons.append(f"series_id_mismatch expected~={oracle.accepted_series_ids} actual={observed['series_ids']}")

    for cue in oracle.required_indicator_cues:
        if not _cue_present(cue, observed):
            reasons.append(f"missing_indicator_cue={cue}")

    for cue in oracle.forbidden_indicator_cues:
        if _cue_present(cue, observed):
            reasons.append(f"forbidden_indicator_cue_present={cue}")

    if (
        oracle.earliest_year_at_most is not None
        and (
            observed["earliest_year"] is None
            or observed["earliest_year"] > oracle.earliest_year_at_most
        )
    ):
        reasons.append(f"earliest_year>{oracle.earliest_year_at_most}")

    if (
        oracle.latest_year_at_least is not None
        and (
            observed["latest_year"] is None
            or observed["latest_year"] < oracle.latest_year_at_least
        )
    ):
        reasons.append(f"latest_year<{oracle.latest_year_at_least}")

    status = "PASS" if not reasons else "FAIL"
    source_summary = ",".join(observed["providers"][:3])
    indicator_summary = ",".join(observed["series_ids"][:3])
    return status, source_summary[:30], indicator_summary[:20], reasons, observed


def run_round(query: str, conversation_id: str | None = None, max_retries: int = MAX_RETRIES, request_timeout: int = REQUEST_TIMEOUT):
    """Send a single query and return (response_json, conversation_id, elapsed)."""
    payload: dict[str, Any] = {"query": query}
    if conversation_id:
        payload["conversationId"] = conversation_id

    for attempt in range(max_retries + 1):
        start = time.time()
        try:
            resp = requests.post(
                f"{BASE_URL}/api/query",
                json=payload,
                timeout=request_timeout,
                headers={"Content-Type": "application/json"},
            )
            elapsed = time.time() - start
            if resp.status_code != 200:
                if attempt < max_retries:
                    time.sleep(RETRY_DELAY_SECONDS)
                    continue
                return {"error": f"HTTP {resp.status_code}: {resp.text[:100]}"}, conversation_id, elapsed
            data = resp.json()
            cid = data.get("conversationId", data.get("conversation_id", conversation_id))
            return data, cid, elapsed
        except requests.exceptions.Timeout:
            elapsed = time.time() - start
            if attempt < max_retries:
                time.sleep(RETRY_DELAY_SECONDS)
                continue
            return {"error": "TIMEOUT"}, conversation_id, elapsed
        except (requests.exceptions.ConnectionError, ConnectionError) as exc:
            elapsed = time.time() - start
            if attempt < max_retries:
                print(f"       [retry {attempt+1}] Connection error, waiting {CONNECTION_RETRY_DELAY_SECONDS}s...")
                time.sleep(CONNECTION_RETRY_DELAY_SECONDS)
                try:
                    requests.get(f"{BASE_URL}/api/health", timeout=5)
                except Exception:
                    time.sleep(CONNECTION_RETRY_DELAY_SECONDS)
                continue
            return {"error": str(exc)[:100]}, conversation_id, elapsed
        except Exception as exc:  # pragma: no cover - network/runtime guard
            elapsed = time.time() - start
            if attempt < max_retries:
                time.sleep(RETRY_DELAY_SECONDS)
                continue
            return {"error": str(exc)[:100]}, conversation_id, elapsed
    return {"error": "max retries exhausted"}, conversation_id, 0


def run_test(test_name: str, rounds: list[RoundCase]) -> list[dict[str, Any]]:
    print(f"\n{'='*80}")
    print(f"  {test_name}")
    print(f"{'='*80}")

    results: list[dict[str, Any]] = []
    conversation_id = None

    for index, case in enumerate(rounds, 1):
        print(f"\n  R{index:2d}: {case.query}")
        resp_json, conversation_id, elapsed = run_round(case.query, conversation_id)
        status, source, indicator, reasons, observed = evaluate_round(case, resp_json)
        symbol = {"PASS": "+", "FAIL": "X", "CLARIFY": "?", "WARN": "~"}[status]
        reason_suffix = f" | reasons={';'.join(reasons[:2])}" if reasons else ""
        print(f"       [{symbol}] {status:7s} | src={source:20s} | ind={indicator:20s} | {elapsed:.1f}s{reason_suffix}")

        results.append(
            {
                "round": index,
                "query": case.query,
                "status": status,
                "source": source,
                "indicator": indicator,
                "elapsed": elapsed,
                "conversation_id": conversation_id,
                "oracle": asdict(case.oracle),
                "observed": observed,
                "reasons": reasons,
            }
        )
        time.sleep(ROUND_DELAY_SECONDS)

    return results


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default=BASE_URL)
    parser.add_argument("--report", default=DEFAULT_REPORT_PATH)
    parser.add_argument("--suite", default=DEFAULT_SUITE)
    parser.add_argument("--list-suites", action="store_true")
    parser.add_argument("--min-effective-rate", type=float, default=MIN_EFFECTIVE_RATE)
    parser.add_argument("--max-fails", type=int, default=MAX_FAILS)
    parser.add_argument("--request-timeout", type=int, default=REQUEST_TIMEOUT)
    parser.add_argument("--round-delay-seconds", type=float, default=ROUND_DELAY_SECONDS)
    parser.add_argument("--between-test-delay-seconds", type=float, default=BETWEEN_TEST_DELAY_SECONDS)
    parser.add_argument("--max-retries", type=int, default=MAX_RETRIES)
    return parser.parse_args()


def build_report_payload(
    *,
    base_url: str,
    suite: str,
    suite_description: str,
    total_elapsed: float,
    all_results: dict[str, list[dict[str, Any]]],
    min_effective_rate: float,
    max_fails: int,
    completed: bool,
) -> dict[str, Any]:
    total_pass = 0
    total_warn = 0
    total_clarify = 0
    total_fail = 0
    total_rounds = 0

    for results in all_results.values():
        total_pass += sum(1 for r in results if r["status"] == "PASS")
        total_warn += sum(1 for r in results if r["status"] == "WARN")
        total_clarify += sum(1 for r in results if r["status"] == "CLARIFY")
        total_fail += sum(1 for r in results if r["status"] == "FAIL")
        total_rounds += len(results)

    effective_rate_ratio = ((total_pass + total_warn) / total_rounds) if total_rounds else 0.0
    strict_pass_rate_ratio = (total_pass / total_rounds) if total_rounds else 0.0
    ok = completed and total_fail <= max_fails and effective_rate_ratio >= min_effective_rate

    return {
        "timestamp": datetime.now().isoformat(),
        "base_url": base_url,
        "suite": suite,
        "suite_version": SUITES_VERSION,
        "suite_description": suite_description,
        "oracle_bearing": True,
        "completed": completed,
        "total_rounds": total_rounds,
        "pass": total_pass,
        "warn": total_warn,
        "clarify": total_clarify,
        "fail": total_fail,
        "effective_rate": f"{effective_rate_ratio*100:.1f}%",
        "effective_rate_ratio": round(effective_rate_ratio, 4),
        "strict_pass_rate": f"{strict_pass_rate_ratio*100:.1f}%",
        "strict_pass_rate_ratio": round(strict_pass_rate_ratio, 4),
        "min_effective_rate": min_effective_rate,
        "max_fails": max_fails,
        "ok": ok,
        "total_time_seconds": round(total_elapsed, 1),
        "tests": all_results,
    }


def write_progress_report(report_path: str, payload: dict[str, Any]) -> None:
    progress_path = f"{report_path}.inprogress"
    os.makedirs(os.path.dirname(progress_path), exist_ok=True)
    with open(progress_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)


def main() -> None:
    args = parse_args()
    if args.list_suites:
        for suite_name, description in list_suite_descriptions().items():
            print(f"{suite_name}: {description}")
        return

    base_url = str(args.base_url).rstrip("/")
    global BASE_URL, REQUEST_TIMEOUT, ROUND_DELAY_SECONDS, BETWEEN_TEST_DELAY_SECONDS, MAX_RETRIES
    BASE_URL = base_url
    REQUEST_TIMEOUT = args.request_timeout
    ROUND_DELAY_SECONDS = args.round_delay_seconds
    BETWEEN_TEST_DELAY_SECONDS = args.between_test_delay_seconds
    MAX_RETRIES = args.max_retries
    tests = load_suite(args.suite)
    suite_description = get_suite_description(args.suite)

    print("Multi-Round Conversation Test Suite")
    print(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Target: {base_url}")
    print(f"Suite: {args.suite} — {suite_description}")
    print(f"Suite version: {SUITES_VERSION}")
    print(f"Tests: {len(tests)} named tests = {sum(len(v) for v in tests.values())} total rounds")

    try:
        health = requests.get(f"{base_url}/api/health", timeout=30)
        if health.status_code != 200:
            print(f"\nERROR: Backend health check failed: {health.status_code}")
            sys.exit(1)
        print("Backend: healthy")
    except Exception as exc:
        print(f"\nERROR: Backend not reachable: {exc}")
        sys.exit(1)

    all_results: dict[str, list[dict[str, Any]]] = {}
    overall_start = time.time()

    report_path = None
    if args.report:
        report_path = args.report
    else:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"multiround_10x10_{args.suite}_{timestamp}.json" if args.suite != DEFAULT_SUITE_NAME else f"multiround_10x10_{timestamp}.json"
        report_path = str(REPORT_DIR / filename)

    for idx, (test_name, rounds) in enumerate(tests.items()):
        if idx > 0:
            print(f"\n  ... waiting {BETWEEN_TEST_DELAY_SECONDS}s between tests ...")
            time.sleep(BETWEEN_TEST_DELAY_SECONDS)
            for retry in range(HEALTH_RETRIES):
                try:
                    health = requests.get(f"{base_url}/api/health", timeout=30)
                    if health.status_code == 200:
                        break
                except Exception:
                    pass
                print(f"  ... backend not ready, waiting {HEALTH_RETRY_DELAY_SECONDS}s (retry {retry+1}) ...")
                time.sleep(HEALTH_RETRY_DELAY_SECONDS)
        all_results[test_name] = run_test(test_name, rounds)
        if report_path:
            try:
                progress_payload = build_report_payload(
                    base_url=base_url,
                    suite=args.suite,
                    suite_description=suite_description,
                    total_elapsed=time.time() - overall_start,
                    all_results=all_results,
                    min_effective_rate=args.min_effective_rate,
                    max_fails=args.max_fails,
                    completed=False,
                )
                write_progress_report(report_path, progress_payload)
            except Exception as exc:  # pragma: no cover - progress guard
                print(f"  ... progress write failed: {exc}")

    total_elapsed = time.time() - overall_start

    print(f"\n\n{'='*100}")
    print("  SUMMARY")
    print(f"{'='*100}")
    print(f"\n{'Test':<40s} {'PASS':>5s} {'WARN':>5s} {'CLAR':>5s} {'FAIL':>5s} {'Rate':>7s} {'AvgTime':>8s}")
    print(f"{'-'*40} {'-'*5} {'-'*5} {'-'*5} {'-'*5} {'-'*7} {'-'*8}")

    total_pass = 0
    total_warn = 0
    total_clarify = 0
    total_fail = 0
    total_rounds = 0

    for test_name, results in all_results.items():
        passed = sum(1 for r in results if r["status"] == "PASS")
        warned = sum(1 for r in results if r["status"] == "WARN")
        clarified = sum(1 for r in results if r["status"] == "CLARIFY")
        failed = sum(1 for r in results if r["status"] == "FAIL")
        avg_t = sum(r["elapsed"] for r in results) / len(results)
        rate = f"{(passed+warned)*100/len(results):.0f}%"

        total_pass += passed
        total_warn += warned
        total_clarify += clarified
        total_fail += failed
        total_rounds += len(results)

        short_name = test_name[:39]
        print(f"{short_name:<40s} {passed:5d} {warned:5d} {clarified:5d} {failed:5d} {rate:>7s} {avg_t:7.1f}s")

    print(f"{'-'*40} {'-'*5} {'-'*5} {'-'*5} {'-'*5} {'-'*7} {'-'*8}")
    overall_rate = f"{(total_pass+total_warn)*100/total_rounds:.0f}%"
    avg_overall = total_elapsed / total_rounds
    print(f"{'TOTAL':<40s} {total_pass:5d} {total_warn:5d} {total_clarify:5d} {total_fail:5d} {overall_rate:>7s} {avg_overall:7.1f}s")
    print(f"\nTotal time: {total_elapsed:.0f}s ({total_elapsed/60:.1f}min)")
    print(f"Effective rate (PASS+WARN): {(total_pass+total_warn)}/{total_rounds} = {overall_rate}")
    print(f"Strict PASS rate: {total_pass}/{total_rounds} = {total_pass*100/total_rounds:.0f}%")
    print(f"Completed: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    failures = []
    for test_name, results in all_results.items():
        for result in results:
            if result["status"] == "FAIL":
                failures.append((test_name, result))

    if failures:
        print(f"\n\n{'='*100}")
        print(f"  FAILURES ({len(failures)})")
        print(f"{'='*100}")
        for test_name, result in failures:
            short_test = test_name.split(":")[1].strip()[:20] if ":" in test_name else test_name[:20]
            print(f"  [{short_test}] R{result['round']:2d}: {result['query']}")
            print(f"         reasons={'; '.join(result['reasons'])}")

    report = build_report_payload(
        base_url=base_url,
        suite=args.suite,
        suite_description=suite_description,
        total_elapsed=total_elapsed,
        all_results=all_results,
        min_effective_rate=args.min_effective_rate,
        max_fails=args.max_fails,
        completed=True,
    )
    effective_rate_ratio = report["effective_rate_ratio"]
    ok = report["ok"]

    try:
        os.makedirs(os.path.dirname(report_path), exist_ok=True)
        with open(report_path, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2)
        print(f"\nReport saved: {report_path}")
        progress_path = f"{report_path}.inprogress"
        if os.path.exists(progress_path):
            os.remove(progress_path)
    except Exception as exc:  # pragma: no cover - disk guard
        print(f"\nFailed to save report: {exc}")

    if not ok:
        print(
            f"\nFAIL: effective_rate={effective_rate_ratio:.3f} "
            f"(min {args.min_effective_rate:.3f}), fails={total_fail} (max {args.max_fails})"
        )
        sys.exit(1)

    print(
        f"\nPASS: effective_rate={effective_rate_ratio:.3f} "
        f"(min {args.min_effective_rate:.3f}), fails={total_fail} (max {args.max_fails})"
    )


if __name__ == "__main__":
    main()
