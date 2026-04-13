#!/usr/bin/env python3
"""Check whether built trust assets and live production agree on canonical domain intent."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence
from urllib.parse import urljoin
from xml.etree import ElementTree

import requests


REPO_ROOT = Path(__file__).resolve().parents[1]
REPORT_DIR = REPO_ROOT / ".omx" / "reports"
DEFAULT_BASE_URL = "https://data.openecon.ai"
CANONICAL_DOMAIN = "data.openecon.ai"
LEGACY_APP_DOMAIN = "data.openecon.io"
ROOT_APP_DOMAIN = "openecon.ai"
CANONICAL_BASE_URL = f"https://{CANONICAL_DOMAIN}"


def _contains_legacy_domain(value: str) -> bool:
    return LEGACY_APP_DOMAIN in value


def _contains_root_app_domain(value: str) -> bool:
    return ROOT_APP_DOMAIN in value and CANONICAL_DOMAIN not in value


def _matches_canonical_prefix(value: str, *, suffix: str = "") -> bool:
    return value.startswith(f"{CANONICAL_BASE_URL}{suffix}")


def _extract_html_head_fields(text: str) -> dict[str, list[str]]:
    patterns = {
        "canonical_hrefs": re.compile(r'<link[^>]+rel=["\']canonical["\'][^>]+href=["\']([^"\']+)["\']', re.IGNORECASE),
        "og_urls": re.compile(r'<meta[^>]+property=["\']og:url["\'][^>]+content=["\']([^"\']+)["\']', re.IGNORECASE),
        "twitter_urls": re.compile(r'<meta[^>]+(?:name|property)=["\']twitter:url["\'][^>]+content=["\']([^"\']+)["\']', re.IGNORECASE),
    }
    fields = {name: pattern.findall(text) for name, pattern in patterns.items()}

    schema_urls: list[str] = []
    for match in re.finditer(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', text, re.IGNORECASE | re.DOTALL):
        blob = match.group(1).strip()
        try:
            parsed = json.loads(blob)
        except json.JSONDecodeError:
            continue
        stack: list[Any] = [parsed]
        while stack:
            current = stack.pop()
            if isinstance(current, dict):
                url = current.get("url")
                if isinstance(url, str):
                    schema_urls.append(url)
                stack.extend(current.values())
            elif isinstance(current, list):
                stack.extend(current)
    fields["schema_urls"] = schema_urls
    return fields


def _evaluate_html_structure(text: str) -> dict[str, Any]:
    fields = _extract_html_head_fields(text)
    field_results = {
        "canonical_hrefs": all(_matches_canonical_prefix(value, suffix="/") for value in fields["canonical_hrefs"]) and bool(fields["canonical_hrefs"]),
        "og_urls": all(_matches_canonical_prefix(value, suffix="/") for value in fields["og_urls"]) and bool(fields["og_urls"]),
        "twitter_urls": all(_matches_canonical_prefix(value, suffix="/") for value in fields["twitter_urls"]) and bool(fields["twitter_urls"]),
        "schema_urls": all(_matches_canonical_prefix(value) for value in fields["schema_urls"]) and bool(fields["schema_urls"]),
    }
    problems: list[str] = []
    for field_name, values in fields.items():
        if not values:
            problems.append(f"missing {field_name}")
            continue
        if not field_results[field_name]:
            problems.append(f"non-canonical {field_name}: {values}")
        legacy_values = [value for value in values if _contains_legacy_domain(value)]
        if legacy_values:
            problems.append(f"legacy domain in {field_name}: {legacy_values}")
    return {
        "ok": not problems,
        "fields": fields,
        "problems": problems,
    }


def _evaluate_robots_structure(text: str) -> dict[str, Any]:
    sitemap_lines = [line.split(":", 1)[1].strip() for line in text.splitlines() if line.lower().startswith("sitemap:")]
    problems: list[str] = []
    if not sitemap_lines:
        problems.append("missing Sitemap line")
    elif not all(value == f"{CANONICAL_BASE_URL}/sitemap.xml" for value in sitemap_lines):
        problems.append(f"unexpected sitemap lines: {sitemap_lines}")
    legacy_values = [value for value in sitemap_lines if _contains_legacy_domain(value)]
    if legacy_values:
        problems.append(f"legacy domain in sitemap lines: {legacy_values}")
    return {"ok": not problems, "fields": {"sitemap_lines": sitemap_lines}, "problems": problems}


def _evaluate_sitemap_structure(text: str) -> dict[str, Any]:
    root = ElementTree.fromstring(text)
    loc_values = [elem.text.strip() for elem in root.findall('.//{*}loc') if elem.text]
    problems: list[str] = []
    if not loc_values:
        problems.append("missing <loc> entries")
    elif not all(_matches_canonical_prefix(value, suffix="/") or value == CANONICAL_BASE_URL for value in loc_values):
        problems.append(f"non-canonical loc values: {loc_values}")
    legacy_values = [value for value in loc_values if _contains_legacy_domain(value)]
    if legacy_values:
        problems.append(f"legacy domain in loc values: {legacy_values}")
    return {"ok": not problems, "fields": {"loc_values": loc_values}, "problems": problems}


def _evaluate_llms_structure(text: str) -> dict[str, Any]:
    urls = re.findall(r"https?://[^\s)]+", text)
    problems: list[str] = []
    if f"{CANONICAL_BASE_URL}/chat" not in urls:
        problems.append("missing canonical chat URL")
    legacy_values = [value for value in urls if _contains_legacy_domain(value)]
    if legacy_values:
        problems.append(f"legacy domain URLs present: {legacy_values}")
    root_domain_values = [value for value in urls if _contains_root_app_domain(value)]
    if root_domain_values:
        problems.append(f"root app domain URLs present: {root_domain_values}")
    return {"ok": not problems, "fields": {"urls": urls}, "problems": problems}


def _evaluate_ai_plugin_structure(text: str) -> dict[str, Any]:
    parsed = json.loads(text)
    api_url = parsed.get("api", {}).get("url")
    logo_url = parsed.get("logo_url")
    legal_info_url = parsed.get("legal_info_url")
    fields = {
        "api_url": api_url,
        "logo_url": logo_url,
        "legal_info_url": legal_info_url,
    }
    problems: list[str] = []
    if api_url != f"{CANONICAL_BASE_URL}/api/openapi.json":
        problems.append(f"unexpected api.url: {api_url!r}")
    if logo_url != f"{CANONICAL_BASE_URL}/logo.png":
        problems.append(f"unexpected logo_url: {logo_url!r}")
    if legal_info_url != f"{CANONICAL_BASE_URL}/docs":
        problems.append(f"unexpected legal_info_url: {legal_info_url!r}")
    legacy_values = [value for value in fields.values() if isinstance(value, str) and _contains_legacy_domain(value)]
    if legacy_values:
        problems.append(f"legacy domain values present: {legacy_values}")
    return {"ok": not problems, "fields": fields, "problems": problems}


def _evaluate_security_structure(text: str) -> dict[str, Any]:
    kv: dict[str, str] = {}
    for line in text.splitlines():
        if ":" in line and not line.strip().startswith("#"):
            key, value = line.split(":", 1)
            kv[key.strip()] = value.strip()
    canonical = kv.get("Canonical")
    policy = kv.get("Policy")
    fields = {"canonical": canonical, "policy": policy}
    problems: list[str] = []
    if canonical != f"{CANONICAL_BASE_URL}/.well-known/security.txt":
        problems.append(f"unexpected Canonical: {canonical!r}")
    if policy != f"{CANONICAL_BASE_URL}/docs":
        problems.append(f"unexpected Policy: {policy!r}")
    legacy_values = [value for value in fields.values() if isinstance(value, str) and _contains_legacy_domain(value)]
    if legacy_values:
        problems.append(f"legacy domain values present: {legacy_values}")
    return {"ok": not problems, "fields": fields, "problems": problems}


@dataclass(frozen=True)
class IntegrityCheck:
    name: str
    source_path: str
    built_path: str
    live_path: str
    required_substrings: tuple[str, ...]
    forbidden_substrings: tuple[str, ...] = ()
    structural_evaluator: Callable[[str], dict[str, Any]] | None = None


CHECKS: tuple[IntegrityCheck, ...] = (
    IntegrityCheck(
        name="chat_html",
        source_path="packages/frontend/index.html",
        built_path="packages/frontend/dist/index.html",
        live_path="/chat",
        required_substrings=(f"{CANONICAL_BASE_URL}/", "OpenEcon.ai"),
        forbidden_substrings=(LEGACY_APP_DOMAIN,),
        structural_evaluator=_evaluate_html_structure,
    ),
    IntegrityCheck(
        name="robots",
        source_path="packages/frontend/public/robots.txt",
        built_path="packages/frontend/dist/robots.txt",
        live_path="/robots.txt",
        required_substrings=(f"{CANONICAL_BASE_URL}/sitemap.xml",),
        forbidden_substrings=(LEGACY_APP_DOMAIN,),
        structural_evaluator=_evaluate_robots_structure,
    ),
    IntegrityCheck(
        name="sitemap",
        source_path="packages/frontend/public/sitemap.xml",
        built_path="packages/frontend/dist/sitemap.xml",
        live_path="/sitemap.xml",
        required_substrings=(f"{CANONICAL_BASE_URL}/",),
        forbidden_substrings=(LEGACY_APP_DOMAIN,),
        structural_evaluator=_evaluate_sitemap_structure,
    ),
    IntegrityCheck(
        name="llms",
        source_path="packages/frontend/public/llms.txt",
        built_path="packages/frontend/dist/llms.txt",
        live_path="/llms.txt",
        required_substrings=(f"{CANONICAL_BASE_URL}/chat",),
        forbidden_substrings=(LEGACY_APP_DOMAIN, f"https://{ROOT_APP_DOMAIN}"),
        structural_evaluator=_evaluate_llms_structure,
    ),
    IntegrityCheck(
        name="ai_plugin",
        source_path="packages/frontend/public/.well-known/ai-plugin.json",
        built_path="packages/frontend/dist/.well-known/ai-plugin.json",
        live_path="/.well-known/ai-plugin.json",
        required_substrings=(CANONICAL_DOMAIN,),
        forbidden_substrings=(LEGACY_APP_DOMAIN,),
        structural_evaluator=_evaluate_ai_plugin_structure,
    ),
    IntegrityCheck(
        name="security_txt",
        source_path="packages/frontend/public/.well-known/security.txt",
        built_path="packages/frontend/dist/.well-known/security.txt",
        live_path="/.well-known/security.txt",
        required_substrings=(CANONICAL_DOMAIN,),
        forbidden_substrings=(LEGACY_APP_DOMAIN,),
        structural_evaluator=_evaluate_security_structure,
    ),
)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def evaluate_text(text: str, required: Sequence[str], forbidden: Sequence[str]) -> dict[str, Any]:
    required_present = [needle for needle in required if needle in text]
    required_missing = [needle for needle in required if needle not in text]
    forbidden_present = [needle for needle in forbidden if needle in text]
    ok = not required_missing and not forbidden_present
    return {
        "ok": ok,
        "required_present": required_present,
        "required_missing": required_missing,
        "forbidden_present": forbidden_present,
    }


def fetch_live_text(base_url: str, live_path: str, timeout_seconds: float) -> dict[str, Any]:
    url = urljoin(base_url.rstrip("/") + "/", live_path.lstrip("/"))
    fetched_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    try:
        response = requests.get(url, timeout=timeout_seconds)
        response.raise_for_status()
        return {
            "text": response.text,
            "fetch_error": None,
            "status_code": response.status_code,
            "resolved_url": response.url,
            "fetched_at": fetched_at,
        }
    except requests.RequestException as exc:  # pragma: no cover - exercised via live run
        status_code = getattr(exc.response, "status_code", None)
        resolved_url = getattr(exc.response, "url", url)
        return {
            "text": None,
            "fetch_error": f"{exc.__class__.__name__}: {exc}",
            "status_code": status_code,
            "resolved_url": resolved_url,
            "fetched_at": fetched_at,
        }


def _empty_eval(required_substrings: Sequence[str]) -> dict[str, Any]:
    return {"ok": False, "required_present": [], "required_missing": list(required_substrings), "forbidden_present": []}


def _with_structure(text_eval: dict[str, Any], structure_eval: dict[str, Any] | None) -> dict[str, Any]:
    if structure_eval is None:
        return {**text_eval, "structure": None}
    return {
        **text_eval,
        "ok": bool(text_eval["ok"]) and bool(structure_eval["ok"]),
        "structure": structure_eval,
    }


def classify_check_result(result: dict[str, Any]) -> str:
    local_ok = bool(result["local"]["ok"])
    live_ok = bool(result["live"]["ok"])
    if local_ok and live_ok:
        return "aligned"
    if local_ok and not live_ok:
        return "deploy_drift"
    if not local_ok and not live_ok:
        return "source_and_live_wrong"
    return "local_wrong_live_unexpected"


def resolve_local_artifact(check: IntegrityCheck) -> tuple[Path, str]:
    built_file = REPO_ROOT / check.built_path
    if built_file.exists():
        return built_file, "build"
    return REPO_ROOT / check.source_path, "source"


def run_check(check: IntegrityCheck, *, base_url: str, timeout_seconds: float) -> dict[str, Any]:
    local_file, local_artifact_kind = resolve_local_artifact(check)
    local_text = local_file.read_text(encoding="utf-8")
    live_fetch = fetch_live_text(base_url, check.live_path, timeout_seconds)
    live_text = live_fetch["text"]

    local_eval = evaluate_text(local_text, check.required_substrings, check.forbidden_substrings)
    local_structure = check.structural_evaluator(local_text) if check.structural_evaluator else None
    local_result = {
        **_with_structure(local_eval, local_structure),
        "sha256": sha256_text(local_text),
    }

    live_eval = evaluate_text(live_text, check.required_substrings, check.forbidden_substrings) if live_text is not None else _empty_eval(check.required_substrings)
    live_structure = check.structural_evaluator(live_text) if (check.structural_evaluator and live_text is not None) else None
    live_result = {
        **_with_structure(live_eval, live_structure),
        "sha256": sha256_text(live_text) if live_text is not None else None,
        "fetch_error": live_fetch["fetch_error"],
        "status_code": live_fetch["status_code"],
        "resolved_url": live_fetch["resolved_url"],
        "fetched_at": live_fetch["fetched_at"],
    }

    result = {
        "name": check.name,
        "source_path": check.source_path,
        "built_path": check.built_path,
        "local_path": str(local_file.relative_to(REPO_ROOT)),
        "local_artifact_kind": local_artifact_kind,
        "live_path": check.live_path,
        "required_substrings": list(check.required_substrings),
        "forbidden_substrings": list(check.forbidden_substrings),
        "local": local_result,
        "live": live_result,
        "drift_detected": sha256_text(local_text) != sha256_text(live_text) if live_text is not None else True,
    }
    result["ok"] = bool(local_result["ok"]) and bool(live_result["ok"])
    result["classification"] = classify_check_result(result)
    return result


def build_report(*, base_url: str, timeout_seconds: float) -> dict[str, Any]:
    results = [run_check(check, base_url=base_url, timeout_seconds=timeout_seconds) for check in CHECKS]
    all_ok = all(result["ok"] for result in results)
    return {
        "base_url": base_url,
        "canonical_domain": CANONICAL_DOMAIN,
        "legacy_domain": LEGACY_APP_DOMAIN,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "all_ok": all_ok,
        "summary": {
            "aligned": [result["name"] for result in results if result["classification"] == "aligned"],
            "deploy_drift": [result["name"] for result in results if result["classification"] == "deploy_drift"],
            "source_and_live_wrong": [result["name"] for result in results if result["classification"] == "source_and_live_wrong"],
            "local_wrong_live_unexpected": [result["name"] for result in results if result["classification"] == "local_wrong_live_unexpected"],
        },
        "checks": results,
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Phase 0 Release Integrity Check",
        "",
        f"- Generated at: `{report['generated_at']}`",
        f"- Base URL: `{report['base_url']}`",
        f"- Canonical domain: `{report['canonical_domain']}`",
        f"- Legacy domain forbidden: `{report['legacy_domain']}`",
        "- Local comparison target: built frontend artifacts when present, otherwise source files",
        f"- Overall status: `{'PASS' if report['all_ok'] else 'FAIL'}`",
        "",
        "## Interpretation",
        "",
        f"- Deploy drift (repo corrected, live still stale): `{', '.join(report['summary']['deploy_drift']) or 'none'}`",
        f"- Source and live both still wrong: `{', '.join(report['summary']['source_and_live_wrong']) or 'none'}`",
        "",
        "| Check | Classification | Local OK | Live OK | Drift detected | Live status | Notes |",
        "|---|---|---|---|---|---|---|",
    ]
    for item in report["checks"]:
        local = item["local"]
        live = item["live"]
        notes: list[str] = []
        if local["required_missing"]:
            notes.append(f"local missing {local['required_missing']}")
        if local["forbidden_present"]:
            notes.append(f"local forbidden {local['forbidden_present']}")
        if local.get("structure", {}).get("problems"):
            notes.append(f"local structure {local['structure']['problems']}")
        if live["required_missing"]:
            notes.append(f"live missing {live['required_missing']}")
        if live["forbidden_present"]:
            notes.append(f"live forbidden {live['forbidden_present']}")
        if live.get("structure", {}).get("problems"):
            notes.append(f"live structure {live['structure']['problems']}")
        if live.get("fetch_error"):
            notes.append(f"fetch_error={live['fetch_error']}")
        lines.append(
            f"| {item['name']} | `{item['classification']}` | `{local['ok']}` | `{live['ok']}` | `{item['drift_detected']}` | "
            f"`{live.get('status_code')}` | {'; '.join(notes) if notes else 'aligned'} |"
        )
    return "\n".join(lines) + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--timeout-seconds", type=float, default=20.0)
    parser.add_argument(
        "--output-json",
        default=str(REPORT_DIR / "phase0-release-integrity.json"),
    )
    parser.add_argument(
        "--output-md",
        default=str(REPORT_DIR / "phase0-release-integrity.md"),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = build_report(base_url=args.base_url, timeout_seconds=args.timeout_seconds)

    output_json = Path(args.output_json)
    output_md = Path(args.output_md)
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_md.parent.mkdir(parents=True, exist_ok=True)

    output_json.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    output_md.write_text(render_markdown(report), encoding="utf-8")

    print(output_md)
    return 0 if report["all_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
