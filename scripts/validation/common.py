#!/usr/bin/env python3
from __future__ import annotations

import hashlib
from html import unescape
import json
from functools import lru_cache
import random
import re
import sqlite3
from pathlib import Path
from typing import Any, Iterable

try:
    from backend.routing.country_resolver import CountryResolver
except Exception:  # pragma: no cover - fallback for lightweight script usage
    CountryResolver = None

try:
    from backend.utils.bis_supportability import bis_catalog_sampler_supportability_reason
except Exception:  # pragma: no cover - fallback for lightweight script usage
    def bis_catalog_sampler_supportability_reason(*_args: Any, **_kwargs: Any) -> str | None:
        return None

try:
    from backend.utils.coingecko_supportability import coingecko_catalog_sampler_supportability_reason
except Exception:  # pragma: no cover - fallback for lightweight script usage
    def coingecko_catalog_sampler_supportability_reason(*_args: Any, **_kwargs: Any) -> str | None:
        return None

try:
    from backend.utils.oecd_supportability import oecd_catalog_sampler_supportability_reason
except Exception:  # pragma: no cover - fallback for lightweight script usage
    def oecd_catalog_sampler_supportability_reason(*_args: Any, **_kwargs: Any) -> str | None:
        return None

try:
    from backend.utils.imf_supportability import (
        imf_catalog_sampler_supportability_reason,
        imf_catalog_surface_supportability_reason,
    )
except Exception:  # pragma: no cover - fallback for lightweight script usage
    def imf_catalog_surface_supportability_reason(*_args: Any, **_kwargs: Any) -> str | None:
        return None

    def imf_catalog_sampler_supportability_reason(*_args: Any, **_kwargs: Any) -> str | None:
        return None

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = ROOT / 'backend' / 'data' / 'indicators.db'
VALIDATION_PRIVATE = ROOT / 'validation_private'
CERTIFICATION_TARGET_LEGACY_CATALOG_REPLAY = 'legacy_catalog_replay'
CERTIFICATION_TARGET_USER_ANSWERABILITY = 'user_answerability'
CERTIFICATION_TARGETS = (
    CERTIFICATION_TARGET_LEGACY_CATALOG_REPLAY,
    CERTIFICATION_TARGET_USER_ANSWERABILITY,
)
USER_ANSWERABILITY_INVENTORY_ONLY_RISK_REASONS = {
    'coin_low_viability_family',
    'coin_slug_query',
    'eurostat_agri_breakdown_query',
    'eurostat_cross_tab_query',
    'eurostat_dimension_fragment_query',
    'eurostat_forestry_material_flow_query',
    'eurostat_transport_port_query',
    'fred_hicp_catalog_family',
    'fred_low_viability_family',
    'imf_complex_finance_family',
    'imf_low_viability_family',
    'imf_price_or_memorandum_family',
    'imf_query_only_public_surface_family',
    'oecd_low_viability_family',
    'oecd_non_production_dataflow',
    'worldbank_country_availability_surface',
    'worldbank_ddh_prevalence_family',
    'worldbank_niche_catalog_family',
    'worldbank_specialized_source_family',
}
_EMPIRICAL_CATEGORY_PRIORS: dict[tuple[str, str], tuple[int, int]] | None = None
_EMPIRICAL_FAMILY_PRIORS: dict[tuple[str, str], tuple[int, int]] | None = None
_EMPIRICAL_SUBFAMILY_PRIORS: dict[tuple[str, str], tuple[int, int]] | None = None
_EMPIRICAL_COUNTRY_SUBFAMILY_PRIORS: dict[tuple[str, str, str], tuple[int, int]] | None = None
_EMPIRICAL_COUNTRY_CATEGORY_PRIORS: dict[tuple[str, str, str], tuple[int, int]] | None = None

DEFAULT_COUNTRIES_BY_PROVIDER: dict[str, list[str]] = {
    'FRED': ['US'],
    'IMF': ['United States', 'China', 'Germany', 'Japan', 'India', 'Brazil'],
    'WorldBank': ['United States', 'China', 'India', 'Brazil', 'Japan', 'Germany'],
    'CoinGecko': ['Bitcoin', 'Ethereum', 'Solana', 'Dogecoin'],
    # Long-tail HS subheadings are reporter-sparse.  Use reporters that are
    # empirically high coverage in the Comtrade holdout and include India so
    # arbitrary user-answerability prompts do not over-index on China/US for
    # niche subheadings with older or sparse reporter availability.
    'Comtrade': ['China', 'India', 'Germany', 'France', 'Japan'],
    'Eurostat': ['France', 'Germany', 'Italy', 'Spain'],
    'StatsCan': ['Canada'],
    # Japan and the United States are deliberately excluded as arbitrary OECD
    # defaults: several long-tail Education-at-a-Glance and distributional
    # national-accounts tables advertise those REF_AREA values but have no
    # observations for the provider-native default selection.  Keeping the
    # default set to broad high-coverage countries makes user-answerability
    # prompts ask an answerable country-specific question without adding any
    # title/code semantic shortcut.
    'OECD': ['Canada', 'Germany'],
    'BIS': ['United States', 'China', 'Japan'],
    'ExchangeRate': ['USD to EUR', 'USD to GBP', 'USD to JPY'],
}


def _is_eurostat_aggregate_coverage(coverage: str) -> bool:
    normalized = re.sub(r'[^a-z0-9]+', ' ', str(coverage or '').lower()).strip()
    if not normalized:
        return False
    return normalized in {
        'eu',
        'european union',
        'euro area',
        'eurozone',
        'eu aggregate',
        'eu aggregates',
        'europe',
    } or bool(re.fullmatch(r'(?:eu|ea)\s*\d{2}(?:\s*\d{4})?', normalized))

DIRECT_QUERY_JARGON_PATTERNS = (
    r'\bBPM6\b',
    r'\bPISA\b',
    r'\bMICS\b',
    r'\bManual\b',
    r'\bQuintile\b',
    r'\bFinancial Soundness Indicators\b',
    r'\bEmployment and Social Development Canada\b',
    r'\bCanadian System of National Accounts\b',
)

_IMF_NOISE_SEGMENTS = {
    "prices",
    "national accounts",
    "index",
    "national currency",
    "us dollars",
    "euros",
    "european coicop",
}

_SAFE_DIRECT_ACRONYMS = {
    'GDP',
    'CPI',
    'PPI',
    'USD',
    'EUR',
    'GBP',
    'JPY',
    'CAD',
    'CHF',
}

if CountryResolver is not None:
    _COUNTRY_QUERY_TERMS = {
        alias.lower()
        for alias in CountryResolver.COUNTRY_ALIASES
        if len(alias) >= 4 or alias.lower() in {'us', 'usa', 'uk', 'uae', 'eu'}
    }
else:
    _COUNTRY_QUERY_TERMS = {
        'united states',
        'us',
        'usa',
        'china',
        'japan',
        'germany',
        'france',
        'italy',
        'brazil',
        'canada',
        'india',
        'united kingdom',
        'uk',
        'nigeria',
    }
_AMBIGUOUS_COUNTRY_TERMS = {
    'america',
    'are',
    'can',
    'per',
    # ISO alpha-3 for Norway is NOR, but lower-case "nor" is a common
    # conjunction in indicator and commodity titles (for example "neither
    # crushed nor ground").  Treating it as country scope generated invalid
    # direct-cert queries such as "Nor exports..." for Comtrade rows.
    'nor',
    # ISO alpha-3 for Tonga is TON, but lower-case "ton" is overwhelmingly a
    # unit in economic indicator titles/descriptions.  Treating it as country
    # scope generated invalid direct-cert queries such as "Ton Adjusted
    # savings..." from WorldBank carbon-damage descriptions.
    'ton',
    'world',
}
_COUNTRY_QUERY_TERMS -= _AMBIGUOUS_COUNTRY_TERMS
_GENERIC_CONTEXT_STOPWORDS = {
    'this',
    'dataset',
    'provides',
    'data',
    'number',
    'value',
    'values',
    'indicator',
    'indicators',
    'statistics',
    'statistic',
    'database',
    'table',
    'tables',
    'source',
    'sources',
    'methods',
    'method',
    'please',
    'refer',
    'detailed',
    'country',
    'specific',
    'information',
}
_GENERIC_SHORT_TITLE_TOKENS = {
    'age',
    'average',
    'graduates',
    'physicians',
    'population',
    'urban',
    'rural',
    'female',
    'male',
    'total',
}


if CountryResolver is not None:
    _SAFE_SHORT_COUNTRY_ALIASES = {'us', 'usa', 'uk', 'eu', 'uae'}
    _COUNTRY_ALIAS_PATTERN_ENTRIES = [
        (
            re.compile(rf'(?<![a-z0-9]){re.escape(str(alias).strip().lower())}(?![a-z0-9])'),
            CountryResolver.normalize(str(alias).strip()),
            str(alias).strip(),
        )
        for alias in sorted(CountryResolver.COUNTRY_ALIASES.keys(), key=len, reverse=True)
        if str(alias).strip()
        and str(alias).strip().lower() not in _AMBIGUOUS_COUNTRY_TERMS
        and CountryResolver.normalize(str(alias).strip())
        and (len(str(alias).strip()) > 2 or str(alias).strip().lower() in _SAFE_SHORT_COUNTRY_ALIASES)
    ]
else:
    _COUNTRY_ALIAS_PATTERN_ENTRIES = []


def _ignore_country_alias_match(normalized: str, following_text: str) -> bool:
    return normalized == 'US' and re.match(r'\s*(?:dollars?\b|\$)', following_text) is not None


def detect_country_codes_in_text(text: str) -> set[str]:
    if CountryResolver is None:
        return set()
    lowered = str(text or '').lower()
    if not lowered:
        return set()
    codes: set[str] = set()
    for pattern, normalized, _alias_text in _COUNTRY_ALIAS_PATTERN_ENTRIES:
        match = pattern.search(lowered)
        if not match:
            continue
        if _ignore_country_alias_match(normalized, lowered[match.end():]):
            continue
        codes.add(normalized)
    return codes


def detect_single_country_from_text(text: str) -> str | None:
    if CountryResolver is None:
        return None
    lowered = str(text or '').lower()
    if not lowered:
        return None
    matches: list[tuple[int, str, str]] = []
    for pattern, normalized, alias_text in _COUNTRY_ALIAS_PATTERN_ENTRIES:
        match = pattern.search(lowered)
        if not match:
            continue
        if _ignore_country_alias_match(normalized, lowered[match.end():]):
            continue
        matches.append((len(alias_text), normalized, alias_text))
    codes = {code for _, code, _ in matches}
    if len(codes) != 1 or not matches:
        return None
    best_alias = max(matches)[2]
    return best_alias.title() if len(best_alias) > 2 else best_alias.upper()


def slugify(text: str) -> str:
    return re.sub(r'[^a-z0-9]+', '-', text.lower()).strip('-') or 'item'


def normalize_certification_target(value: object, *, default: str = CERTIFICATION_TARGET_LEGACY_CATALOG_REPLAY) -> str:
    raw = str(value or '').strip().lower()
    aliases = {
        'legacy': CERTIFICATION_TARGET_LEGACY_CATALOG_REPLAY,
        'catalog': CERTIFICATION_TARGET_LEGACY_CATALOG_REPLAY,
        'catalog_replay': CERTIFICATION_TARGET_LEGACY_CATALOG_REPLAY,
        'legacy_catalog': CERTIFICATION_TARGET_LEGACY_CATALOG_REPLAY,
        'legacy_catalog_replay': CERTIFICATION_TARGET_LEGACY_CATALOG_REPLAY,
        'user': CERTIFICATION_TARGET_USER_ANSWERABILITY,
        'real_user': CERTIFICATION_TARGET_USER_ANSWERABILITY,
        'user_answerability': CERTIFICATION_TARGET_USER_ANSWERABILITY,
        'real_user_answerability': CERTIFICATION_TARGET_USER_ANSWERABILITY,
        'answerability': CERTIFICATION_TARGET_USER_ANSWERABILITY,
    }
    if raw in aliases:
        return aliases[raw]
    if default in CERTIFICATION_TARGETS:
        return default
    return CERTIFICATION_TARGET_LEGACY_CATALOG_REPLAY


def certification_target_for_row(
    row: dict[str, Any],
    *,
    default: str = CERTIFICATION_TARGET_LEGACY_CATALOG_REPLAY,
) -> str:
    provenance = row.get('provenance') if isinstance(row.get('provenance'), dict) else {}
    gold = row.get('gold') if isinstance(row.get('gold'), dict) else {}
    return normalize_certification_target(
        row.get('evaluation_target')
        or row.get('certification_target')
        or provenance.get('certification_target')
        or provenance.get('evaluation_target')
        or gold.get('evaluation_target')
        or gold.get('certification_target'),
        default=default,
    )


def is_user_answerability_row(row: dict[str, Any]) -> bool:
    return certification_target_for_row(row) == CERTIFICATION_TARGET_USER_ANSWERABILITY


def stable_seed(*parts: object) -> int:
    digest = hashlib.sha256('||'.join(str(part) for part in parts).encode('utf-8')).hexdigest()
    return int(digest[:16], 16)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding='utf-8'))


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8') as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + '\n')


def _jsonl_rows_by_id(path: Path) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for line in path.read_text(encoding='utf-8').splitlines():
        if line.strip():
            row = json.loads(line)
            rows[str(row.get('id') or '')] = row
    return rows


def _empirical_probe_dataset_paths(report_path: Path, report: dict[str, Any]) -> list[Path]:
    paths: list[Path] = []
    explicit_dataset = str(report.get('dataset_path') or '').strip()
    if explicit_dataset:
        paths.append(Path(explicit_dataset))

    datasets_dir = VALIDATION_PRIVATE / 'datasets' / 'batch_review'
    if report_path.name.endswith('_weak_provider_viability_fast20.json'):
        stem = report_path.stem.replace('_weak_provider_viability_fast20', '')
        paths.append(datasets_dir / stem / 'next_batch_direct_weak_providers.jsonl')

    version_match = re.search(r'next200_20260425_(v\d+)_direct_full_probe_incremental$', report_path.stem)
    if version_match:
        paths.append(datasets_dir / f"next200_20260425_{version_match.group(1)}" / 'next_batch_direct.jsonl')

    unique: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        resolved_key = str(path.expanduser())
        if resolved_key not in seen:
            seen.add(resolved_key)
            unique.append(path)
    return unique


def _iter_empirical_direct_probe_rows() -> Iterable[tuple[dict[str, Any], dict[str, Any]]]:
    reports_dir = VALIDATION_PRIVATE / 'reports'
    report_paths = [
        *reports_dir.glob('next200_v*_weak_provider_viability_fast20.json'),
        *reports_dir.glob('cert_30k_v37_next200_*_direct_full_probe_incremental.json'),
    ]
    for report_path in sorted(set(report_paths)):
        try:
            report = json.loads(report_path.read_text(encoding='utf-8'))
        except Exception:
            continue
        if report_path.name.endswith('_direct_full_probe_incremental.json') and report.get('complete') is False:
            continue

        dataset_rows: dict[str, dict[str, Any]] = {}
        for dataset_path in _empirical_probe_dataset_paths(report_path, report):
            if not dataset_path.exists():
                continue
            try:
                dataset_rows = _jsonl_rows_by_id(dataset_path)
            except Exception:
                continue
            if dataset_rows:
                break
        if not dataset_rows:
            continue

        for result in report.get('results', []):
            row = dataset_rows.get(str(result.get('session_id') or ''))
            if row:
                yield result, row


def _load_empirical_category_priors() -> dict[tuple[str, str], tuple[int, int]]:
    global _EMPIRICAL_CATEGORY_PRIORS
    if _EMPIRICAL_CATEGORY_PRIORS is not None:
        return _EMPIRICAL_CATEGORY_PRIORS

    priors: dict[tuple[str, str], list[int]] = {}
    for result, row in _iter_empirical_direct_probe_rows():
        provider = str(result.get('provider_stratum') or '').upper()
        category = str((row.get('origin') or {}).get('category') or '').strip()
        if not provider or not category:
            continue
        bucket = priors.setdefault((provider, category), [0, 0])
        if result.get('viability_pass'):
            bucket[0] += 1
        else:
            bucket[1] += 1

    _EMPIRICAL_CATEGORY_PRIORS = {
        key: (counts[0], counts[1]) for key, counts in priors.items()
    }
    return _EMPIRICAL_CATEGORY_PRIORS


def category_success_adjustment(provider: str, category: str) -> int:
    provider_key = str(provider or '').upper().strip()
    category_key = str(category or '').strip()
    if not provider_key or not category_key:
        return 0

    priors = _load_empirical_category_priors()
    passed, failed = priors.get((provider_key, category_key), (0, 0))
    total = passed + failed
    if total < 2:
        return 0

    rate = passed / total if total else 0.0
    if rate == 0.0 and total >= 2:
        return -7
    if rate <= 0.15 and total >= 3:
        return -5
    if rate <= 0.25 and total >= 3:
        return -3
    if rate >= 0.5 and total >= 4:
        return 2
    return 0


def provider_family_key(provider: str, name: str) -> str:
    text = str(name or '').lower().strip()
    provider_norm = str(provider or '').upper().strip()
    if provider_norm == 'WORLDBANK':
        text = re.sub(r'^world bank:\s*', '', text)
    text = re.sub(r'\([^)]*\)', ' ', text)
    text = re.sub(r'[^a-z0-9]+', ' ', text)
    tokens = [token for token in text.split() if token]
    stopwords = {
        'world', 'bank', 'male', 'female', 'males', 'females', 'persons', 'person',
        'total', 'adjusted', 'all', 'public', 'private',
    }
    if provider_norm == 'IMF':
        stopwords |= {'definition', 'fiscal', 'government', 'general', 'central'}
    tokens = [token for token in tokens if token not in stopwords]
    return ' '.join(tokens[:4]).strip()


def provider_subfamily_key(provider: str, name: str) -> str:
    text = str(name or '').lower().strip()
    provider_norm = str(provider or '').upper().strip()
    if provider_norm == 'WORLDBANK':
        text = re.sub(r'^world bank:\s*', '', text)
    text = re.sub(r'\([^)]*\)', ' ', text)
    text = re.sub(r'[^a-z0-9]+', ' ', text)
    tokens = [token for token in text.split() if token]
    stopwords = {
        'world', 'bank', 'persons', 'person', 'total', 'all', 'public', 'private',
    }
    if provider_norm == 'IMF':
        stopwords |= {'definition', 'fiscal', 'government', 'general', 'central'}
    tokens = [token for token in tokens if token not in stopwords]
    return ' '.join(tokens[:6]).strip()


def _load_empirical_family_priors() -> dict[tuple[str, str], tuple[int, int]]:
    global _EMPIRICAL_FAMILY_PRIORS
    if _EMPIRICAL_FAMILY_PRIORS is not None:
        return _EMPIRICAL_FAMILY_PRIORS

    priors: dict[tuple[str, str], list[int]] = {}
    for result, row in _iter_empirical_direct_probe_rows():
        provider = str(result.get('provider_stratum') or '').upper()
        name = str((row.get('origin') or {}).get('name') or row.get('name') or '').strip()
        family = provider_family_key(provider, name)
        if not provider or not family:
            continue
        bucket = priors.setdefault((provider, family), [0, 0])
        if result.get('viability_pass'):
            bucket[0] += 1
        else:
            bucket[1] += 1

    _EMPIRICAL_FAMILY_PRIORS = {
        key: (counts[0], counts[1]) for key, counts in priors.items()
    }
    return _EMPIRICAL_FAMILY_PRIORS


def family_success_adjustment(provider: str, name: str) -> int:
    provider_key = str(provider or '').upper().strip()
    family_key = provider_family_key(provider_key, name)
    if not provider_key or not family_key:
        return 0

    priors = _load_empirical_family_priors()
    passed, failed = priors.get((provider_key, family_key), (0, 0))
    total = passed + failed
    if total < 2:
        return 0

    rate = passed / total if total else 0.0
    if rate == 0.0 and total >= 2:
        return -8
    if rate <= 0.20 and total >= 3:
        return -5
    if rate >= 0.75 and total >= 3:
        return 4
    if rate >= 0.5 and total >= 4:
        return 2
    return 0


def _load_empirical_subfamily_priors() -> dict[tuple[str, str], tuple[int, int]]:
    global _EMPIRICAL_SUBFAMILY_PRIORS
    if _EMPIRICAL_SUBFAMILY_PRIORS is not None:
        return _EMPIRICAL_SUBFAMILY_PRIORS

    priors: dict[tuple[str, str], list[int]] = {}
    for result, row in _iter_empirical_direct_probe_rows():
        provider = str(result.get('provider_stratum') or '').upper()
        name = str((row.get('origin') or {}).get('name') or row.get('name') or '').strip()
        subfamily = provider_subfamily_key(provider, name)
        if not provider or not subfamily:
            continue
        bucket = priors.setdefault((provider, subfamily), [0, 0])
        if result.get('viability_pass'):
            bucket[0] += 1
        else:
            bucket[1] += 1

    _EMPIRICAL_SUBFAMILY_PRIORS = {
        key: (counts[0], counts[1]) for key, counts in priors.items()
    }
    return _EMPIRICAL_SUBFAMILY_PRIORS


def subfamily_success_adjustment(provider: str, name: str) -> int:
    provider_key = str(provider or '').upper().strip()
    subfamily_key = provider_subfamily_key(provider_key, name)
    if not provider_key or not subfamily_key:
        return 0

    priors = _load_empirical_subfamily_priors()
    passed, failed = priors.get((provider_key, subfamily_key), (0, 0))
    total = passed + failed
    if total < 1:
        return 0

    rate = passed / total if total else 0.0
    if rate == 1.0 and total >= 1:
        return 4
    if rate == 0.0 and total >= 1:
        return -4
    if rate <= 0.25 and total >= 2:
        return -3
    if rate >= 0.75 and total >= 2:
        return 3
    return 0


def _leading_default_country(query: str, provider: str) -> str | None:
    text = str(query or '').strip().lower()
    provider_value = str(provider or '').strip().lower()
    choices_source = []
    for key, values in DEFAULT_COUNTRIES_BY_PROVIDER.items():
        if str(key).strip().lower() == provider_value:
            choices_source = values
            break
    choices = [str(choice) for choice in choices_source if isinstance(choice, str)]
    for choice in sorted(choices, key=len, reverse=True):
        choice_lower = choice.lower()
        if text == choice_lower or text.startswith(choice_lower + ' '):
            return choice
    return None


def _load_empirical_country_subfamily_priors() -> dict[tuple[str, str, str], tuple[int, int]]:
    global _EMPIRICAL_COUNTRY_SUBFAMILY_PRIORS
    if _EMPIRICAL_COUNTRY_SUBFAMILY_PRIORS is not None:
        return _EMPIRICAL_COUNTRY_SUBFAMILY_PRIORS

    priors: dict[tuple[str, str, str], list[int]] = {}
    for result, row in _iter_empirical_direct_probe_rows():
        provider = str(result.get('provider_stratum') or '').upper()
        name = str((row.get('origin') or {}).get('name') or row.get('name') or '').strip()
        subfamily = provider_subfamily_key(provider, name)
        country = _leading_default_country(str(row.get('query') or ''), provider)
        if not provider or not subfamily or not country:
            continue
        bucket = priors.setdefault((provider, subfamily, country), [0, 0])
        if result.get('viability_pass'):
            bucket[0] += 1
        else:
            bucket[1] += 1

    _EMPIRICAL_COUNTRY_SUBFAMILY_PRIORS = {
        key: (counts[0], counts[1]) for key, counts in priors.items()
    }
    return _EMPIRICAL_COUNTRY_SUBFAMILY_PRIORS


def _load_empirical_country_category_priors() -> dict[tuple[str, str, str], tuple[int, int]]:
    global _EMPIRICAL_COUNTRY_CATEGORY_PRIORS
    if _EMPIRICAL_COUNTRY_CATEGORY_PRIORS is not None:
        return _EMPIRICAL_COUNTRY_CATEGORY_PRIORS

    priors: dict[tuple[str, str, str], list[int]] = {}
    for result, row in _iter_empirical_direct_probe_rows():
        provider = str(result.get('provider_stratum') or '').upper()
        category = str((row.get('origin') or {}).get('category') or '').strip()
        country = _leading_default_country(str(row.get('query') or ''), provider)
        if not provider or not category or not country:
            continue
        bucket = priors.setdefault((provider, category, country), [0, 0])
        if result.get('viability_pass'):
            bucket[0] += 1
        else:
            bucket[1] += 1

    _EMPIRICAL_COUNTRY_CATEGORY_PRIORS = {
        key: (counts[0], counts[1]) for key, counts in priors.items()
    }
    return _EMPIRICAL_COUNTRY_CATEGORY_PRIORS


def preferred_default_country(provider: str, name: str, choices: list[str], fallback_choice: str) -> str:
    provider_key = str(provider or '').upper().strip()
    if provider_key not in {'WORLDBANK', 'IMF'}:
        return fallback_choice
    subfamily = provider_subfamily_key(provider_key, name)
    if not subfamily:
        return fallback_choice

    priors = _load_empirical_country_subfamily_priors()
    category_priors = _load_empirical_country_category_priors()
    scored_choices: list[tuple[int, int, int, int, str]] = []
    category = ""
    # Best-effort category recovery from embedded provider metadata/row name path callers.
    # Synthesizer calls this with only provider/name, so category-level priors are an additive
    # fallback only when the caller doesn't provide a richer subfamily prior hit.
    for choice in choices:
        passed, failed = priors.get((provider_key, subfamily, str(choice)), (0, 0))
        total = passed + failed
        score = 0
        if total >= 1:
            rate = passed / total if total else 0.0
            if rate == 1.0:
                score = 5
            elif rate >= 0.5:
                score = 3
            elif rate == 0.0 and total >= 2:
                score = -3
            elif rate == 0.0:
                score = -1
        evidence = passed - failed
        scored_choices.append((score, evidence, passed, -failed, str(choice)))

    best_score = max(score for score, *_rest in scored_choices) if scored_choices else 0
    if best_score <= 0:
        return fallback_choice

    best_tuple = max(scored_choices)
    best_choices = [choice for score, evidence, passed, neg_failed, choice in scored_choices if (score, evidence, passed, neg_failed) == best_tuple[:4]]
    if fallback_choice in best_choices:
        return fallback_choice
    return sorted(best_choices)[0]


def preferred_default_country_for_record(provider: str, category: str, name: str, choices: list[str], fallback_choice: str) -> str:
    provider_key = str(provider or '').upper().strip()
    if provider_key not in {'WORLDBANK', 'IMF'}:
        return fallback_choice

    category_key = str(category or '').strip()
    if provider_key == 'IMF':
        regional_defaults = {
            # Regional Economic Outlook indicators are not universal
            # DataMapper/WEO series.  Pick an in-region default country so
            # certification query synthesis does not create impossible
            # country/indicator pairs such as India for AFRREO.
            'AFRREO': 'Nigeria',
        }
        if category_key.upper() in regional_defaults:
            return regional_defaults[category_key.upper()]
    subfamily = provider_subfamily_key(provider_key, name)
    priors = _load_empirical_country_subfamily_priors()
    category_priors = _load_empirical_country_category_priors()

    scored_choices: list[tuple[int, int, int, int, str]] = []
    for choice in choices:
        passed, failed = priors.get((provider_key, subfamily, str(choice)), (0, 0))
        total = passed + failed
        score = 0
        if total >= 1:
            rate = passed / total if total else 0.0
            if rate == 1.0:
                score = 5
            elif rate >= 0.5:
                score = 3
            elif rate == 0.0 and total >= 2:
                score = -3
            elif rate == 0.0:
                score = -1
        c_passed = c_failed = 0
        if category_key:
            c_passed, c_failed = category_priors.get((provider_key, category_key, str(choice)), (0, 0))
            c_total = c_passed + c_failed
            if c_total >= 2:
                c_rate = c_passed / c_total if c_total else 0.0
                if c_rate >= 0.75:
                    score += 3
                elif c_rate >= 0.6:
                    score += 2
                elif c_rate >= 0.5:
                    score += 1
                elif c_rate == 0.0:
                    score -= 3
        evidence = (passed - failed) + (c_passed - c_failed)
        total_pass = passed + c_passed
        total_fail = failed + c_failed
        scored_choices.append((score, evidence, total_pass, -total_fail, str(choice)))

    best_score = max(score for score, *_rest in scored_choices) if scored_choices else 0
    if best_score <= 0:
        return fallback_choice
    best_tuple = max(scored_choices)
    best_choices = [choice for score, evidence, passed, neg_failed, choice in scored_choices if (score, evidence, passed, neg_failed) == best_tuple[:4]]
    if fallback_choice in best_choices:
        return fallback_choice
    return sorted(best_choices)[0]


def heuristic_subfamily_adjustment(provider: str, category: str, name: str) -> int:
    provider_key = str(provider or '').upper().strip()
    category_key = str(category or '').strip().lower()
    subfamily_key = provider_subfamily_key(provider_key, name)
    if provider_key == 'WORLDBANK':
        if any(token in subfamily_key for token in [
            'learning deprivation gap',
            'learning deprivation severity',
            'financial management country systems',
            'tracking climate related expenditure',
            'regulatory capital to risk',
            'maternal mortality ratio',
            'prevalence of anemia among',
        ]):
            return -5
        if category_key == 'education statistics' and any(token in subfamily_key for token in [
            'timss',
            'pirls',
            'llece',
            'piaac',
            'saber',
            'qualified teachers',
            'pupil trained teacher ratio',
            'repeaters in grade',
            'out of school rate',
            'government expenditure on education',
            'ratio of expenditures per',
        ]):
            return -4
    if provider_key == 'IMF':
        if any(token in subfamily_key for token in [
            'current account goods',
            'current account secondary',
            'social security revenue other',
        ]):
            return 2
        if any(token in subfamily_key for token in [
            'royalties and license fees',
            'taxes income profits government',
        ]):
            return -2
    return 0


def provider_counts(db_path: Path = DEFAULT_DB) -> list[tuple[str, int]]:
    con = sqlite3.connect(str(db_path))
    cur = con.cursor()
    rows = [(str(provider), int(count)) for provider, count in cur.execute(
        'SELECT provider, COUNT(*) FROM indicators GROUP BY provider ORDER BY COUNT(*) DESC'
    ).fetchall()]
    con.close()
    return rows


def sample_indicator_rows(provider: str, count: int, *, db_path: Path = DEFAULT_DB, seed: int = 20260414) -> list[dict[str, Any]]:
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    cur = con.cursor()
    rows = cur.execute(
        'SELECT id, provider, code, name, description, category, subcategory, unit, frequency, coverage, start_date, end_date, keywords, synonyms, raw_metadata, popularity, last_updated FROM indicators WHERE provider = ? ORDER BY id',
        (provider,),
    ).fetchall()
    con.close()
    payload = [dict(row) for row in rows]
    if count >= len(payload):
        return payload
    rng = random.Random(stable_seed(seed, provider, len(payload), count))
    indices = list(range(len(payload)))
    rng.shuffle(indices)
    selected = sorted(indices[:count])
    return [payload[i] for i in selected]


def top_tokens(*parts: str, limit: int = 6) -> list[str]:
    text = ' '.join(part for part in parts if part)
    tokens = []
    seen = set()
    stopwords = {
        'the',
        'and',
        'for',
        'with',
        'from',
        'into',
        'onto',
        'than',
        'that',
        'this',
        'those',
        'these',
        'their',
        'there',
        'where',
        'which',
        'would',
        'could',
        'should',
        'below',
        'above',
        'among',
        'using',
        'based',
        'adjusted',
    }
    for token in re.findall(r'[A-Za-z0-9]+', text.lower()):
        if len(token) <= 2:
            continue
        if token in stopwords or token in {'series', 'indicator', 'index', 'rate', 'data', 'table'}:
            continue
        if token not in seen:
            seen.add(token)
            tokens.append(token)
        if len(tokens) >= limit:
            break
    return tokens or ['economic']


def humanize_slug(text: str) -> str:
    cleaned = re.sub(r'[-_]+', ' ', str(text or '').strip())
    cleaned = re.sub(r'\b\d+\b', ' ', cleaned)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned


def strip_html_text(text: str) -> str:
    value = unescape(str(text or ''))
    value = re.sub(r'<li[^>]*>', '; ', value, flags=re.IGNORECASE)
    value = re.sub(r'</li>', ' ', value, flags=re.IGNORECASE)
    value = re.sub(r'<[^>]+>', ' ', value)
    value = re.sub(r'\s+', ' ', value).strip()
    return value


def informative_tokens(text: str) -> list[str]:
    tokens: list[str] = []
    for token in re.findall(r'[A-Za-z0-9]+', str(text or '').lower()):
        if len(token) <= 1 or token in _GENERIC_CONTEXT_STOPWORDS:
            continue
        tokens.append(token)
    return tokens


@lru_cache(maxsize=20000)
def description_context_phrase(description: str) -> str:
    raw_description = str(description or '').strip()
    if not raw_description:
        return ''

    list_items = [
        strip_html_text(item)
        for item in re.findall(r'<li[^>]*>(.*?)</li>', raw_description, flags=re.IGNORECASE | re.DOTALL)
    ]
    list_items = [item for item in list_items if len(informative_tokens(item)) >= 2]
    if list_items:
        return list_items[0]

    cleaned = strip_html_text(raw_description)
    cleaned = re.split(r'please refer\b', cleaned, maxsplit=1, flags=re.IGNORECASE)[0].strip()
    if not cleaned:
        return ''

    cleaned = re.sub(r'^this dataset provides data on the number of\s+', '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'^this dataset provides data on\s+', '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'^this dataset provides\s+', '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'^data on\s+', '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'^the number of\s+', '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'\(ie\.[^)]+\)', '', cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip(' ;,')

    sentences = [segment.strip(' ;,') for segment in re.split(r'[.;]', cleaned) if segment.strip()]
    for sentence in sentences:
        tokens = informative_tokens(sentence)
        if len(tokens) >= 2:
            return ' '.join(sentence.split()[:8]).strip(' ,;')

    return ' '.join(cleaned.split()[:8]).strip(' ,;')


def provider_metadata_context(record: dict[str, Any]) -> tuple[dict[str, Any], str]:
    origin = dict(record.get('origin') or {})
    raw_value = (
        origin.get('raw_metadata')
        or record.get('raw_metadata')
        or record.get('metadata')
    )
    provider = str(record.get('provider_stratum') or record.get('provider') or origin.get('source_provider') or '').upper()
    description = str(origin.get('description') or record.get('description') or '').strip()
    keywords = str(origin.get('keywords') or record.get('keywords') or '').strip()
    synonyms = str(origin.get('synonyms') or record.get('synonyms') or '').strip()
    category = str(origin.get('category') or record.get('category') or '').strip()
    subcategory = str(origin.get('subcategory') or record.get('subcategory') or '').strip()

    # Most direct-query quality heuristics only need rich metadata for providers
    # with extremely broad catalogs and verbose source notes.
    if provider not in {'IMF', 'WORLDBANK', 'OECD', 'EUROSTAT'}:
        metadata_text = ' '.join(
            piece for piece in [description, keywords, synonyms, category, subcategory] if piece
        ).lower()
        return {}, metadata_text

    parsed: dict[str, Any] = {}
    if isinstance(raw_value, dict):
        parsed = raw_value
    elif isinstance(raw_value, str) and raw_value.strip():
        try:
            loaded = json.loads(raw_value)
            if isinstance(loaded, dict):
                parsed = loaded
        except Exception:
            parsed = {}

    source = parsed.get('source') or {}
    if isinstance(source, str):
        source_value = source.strip()
    elif isinstance(source, dict):
        source_value = str(source.get('value') or '').strip()
    else:
        source_value = ''
    topics = parsed.get('topics') or []
    topic_values = [
        str(topic.get('value') or '').strip()
        for topic in topics
        if isinstance(topic, dict) and str(topic.get('value') or '').strip()
    ]
    pieces = [
        description,
        keywords,
        synonyms,
        category,
        subcategory,
        source_value,
        str(parsed.get('sourceNote') or '').strip(),
        str(parsed.get('sourceOrganization') or '').strip(),
        ' '.join(topic_values).strip(),
    ]
    metadata_text = ' '.join(piece for piece in pieces if piece).lower()
    return parsed, metadata_text


@lru_cache(maxsize=50000)
def worldbank_source_id_for_code(code: str, db_path: str = str(DEFAULT_DB)) -> str:
    """Return WorldBank metadata source id for an exact provider code."""
    indicator_code = str(code or '').strip()
    if not indicator_code:
        return ''
    try:
        con = sqlite3.connect(db_path)
        con.row_factory = sqlite3.Row
        row = con.execute(
            "SELECT raw_metadata FROM indicators WHERE provider = ? AND code = ? LIMIT 1",
            ("WorldBank", indicator_code),
        ).fetchone()
        con.close()
    except Exception:
        return ''
    if not row:
        return ''
    raw = row['raw_metadata']
    try:
        parsed = json.loads(raw) if isinstance(raw, str) and raw.strip() else {}
    except Exception:
        parsed = {}
    source = parsed.get('source') if isinstance(parsed, dict) else None
    if isinstance(source, dict):
        return str(source.get('id') or '').strip()
    return ''


def _metadata_annotation_is_true(parsed_metadata: dict[str, Any], annotation_type: str) -> bool:
    target = str(annotation_type or '').strip().lower()
    if not target:
        return False
    annotations = parsed_metadata.get('annotations') or []
    if not isinstance(annotations, list):
        return False
    for annotation in annotations:
        if not isinstance(annotation, dict):
            continue
        annotation_kind = str(annotation.get('type') or '').strip().lower()
        if annotation_kind != target:
            continue
        value = annotation.get('text')
        if value is None:
            texts = annotation.get('texts') or {}
            if isinstance(texts, dict):
                value = texts.get('en')
        return str(value or '').strip().lower() == 'true'
    return False


def direct_query_specificity_score(record: dict[str, Any]) -> int:
    origin = dict(record.get('origin') or {})
    query = str(record.get('query') or '')
    name = str(origin.get('name') or record.get('name') or '').strip()
    description = str(origin.get('description') or record.get('description') or '').strip()
    provider = str(record.get('provider_stratum') or record.get('provider') or origin.get('source_provider') or '').upper()
    lowered = f"{query} {name} {description}".lower()
    category = str(origin.get('category') or '').lower()
    metadata_text = ''
    enriched = lowered

    score = 0
    score += len({token for token in informative_tokens(name)})

    contextual = description_context_phrase(description)
    if contextual and contextual.lower() != name.lower():
        score += min(3, len({token for token in informative_tokens(contextual)}))

    normalized_query = re.sub(
        r'\b(from|world bank|eurostat|oecd|fred|imf|statistics canada|statscan|bis|coingecko|comtrade)\b',
        ' ',
        query,
        flags=re.IGNORECASE,
    )
    score += min(3, len({token for token in informative_tokens(normalized_query)}))
    score += category_success_adjustment(provider, str(origin.get('category') or record.get('category') or ''))
    score += family_success_adjustment(provider, name)
    score += subfamily_success_adjustment(provider, name)
    score += heuristic_subfamily_adjustment(provider, str(origin.get('category') or record.get('category') or ''), name)
    if provider in {'IMF', 'WORLDBANK', 'OECD', 'EUROSTAT'}:
        _, metadata_text = provider_metadata_context(record)
        enriched = f"{lowered} {metadata_text}".strip()
    if provider == 'IMF':
        if any(term in enriched for term in ['consumer prices', 'producer price', 'harmonized', 'expenditure of households', 'index']):
            score += 4
        if any(term in enriched for term in ['current account', 'balance of payments', 'revenue', 'taxes', 'income, profits, and capital gains']):
            score += 3
        if any(term in enriched for term in ['positions', 'resident financial intermediaries', 'openness index', 'reserve money', 'dataset:', 'claims', 'liabilities']):
            score -= 4
        if any(term in enriched for term in ['debt-service payment schedule', 'more than 9 and up to 12 months', 'more than 18 and less than 24 months', 'by cofog', 'wages and salaries in kind']):
            score -= 5
        if 'industrial production' in enriched and 'manufacture of' in enriched:
            score -= 4
        if any(term in enriched for term in ['other postal services', 'equities domestic company', 'financial market prices end of period']):
            score -= 4
        if any(term in enriched for term in ['memorandum items', 'producer price index', 'consumer price index']) and any(term in enriched for term in ['definition', 'organic acids', 'food and non-alcoholic beverages', 'cash, national currency']):
            score -= 4
    if provider == 'WORLDBANK':
        if any(term in category for term in ['global jobs indicators', 'global findex', 'health nutrition and population statistics by wealth quintile', 'health equity and financial protection', 'atlas of social protection', 'wdi database archives', 'statistical performance indicators', 'country climate and development report', 'indonesia database for policy and economic research', 'joint external debt hub', 'identification for development (id4d) data', 'g20 financial inclusion indicators']):
            score -= 4
        if any(term in category for term in ['quarterly external debt statistics', 'global public procurement']):
            score -= 7
        if any(term in category for term in ['quarterly public sector debt', 'exporter dynamics database', 'gender statistics']):
            score -= 6
        if any(term in enriched for term in ['q1', 'q2', 'q3', 'q4', 'q5', 'de facto', '1st graders', 'moving average', 'mobile phone', 'mobile money account', 'wage gap', 'grace period', 'held by nonresidents']):
            score -= 3
        if len(re.findall(r'\b[a-z]{2,6}\.', enriched)) >= 2:
            score -= 6
        if any(term in enriched for term in ['learning deprivation', 'timss', 'pirls', 'mpl low']):
            score -= 4
        if any(term in enriched for term in ['net attendance rate', 'out-of-school rate', 'gender parity index', 'gpia', 'attendance ratio', 'youth idle rate', 'household survey data']):
            score -= 4
        if any(term in enriched for term in ['contract teachers', 'off-budget', 'salary expenditures per teacher', 'share of tertiary expenditures', 'rights to inherit assets', 'are other banks permitted', 'pasec', 'no functional difficulty']):
            score -= 5
        if any(term in enriched for term in ['youth literacy rate', 'population 25-64 years', 'both sexes']) and any(term in enriched for term in ['rural', 'male', 'female', 'literacy rate']):
            score -= 4
        if any(term in enriched for term in ['challenge:', 'without an id', 'formal financial institution', 'smes with at least one female owner', 'pupil/teacher ratio', 'civil service teachers', 'technical/vocational', 'private institution fees', 'egra', 'zero score']):
            score -= 5
        if any(term in enriched for term in ['household spending per student', 'public education expenditure per student', 'share of household consumption for private expenditures', 'national assessment for learning outcomes', 'optimal competency', 'public sector wage premium', 'price level ratio of ppp conversion factor', 'sea-plm', 'elevation is below 5 meters']):
            score -= 5
        if any(term in enriched for term in ['food insecure households', 'adjusted prevalence', 'selfcare difficulty', 'mobility difficulty']):
            score -= 5
        if any(term in enriched for term in ['literacy rate', 'per student expenditure', 'completion rate', 'adjusted location parity index', 'any degree of functional difficulty']):
            score += 3
    if provider == 'COINGECKO':
        code = str(origin.get('source_indicator_code') or record.get('code') or '').lower()
        code_parts = [part for part in code.split('-') if part]
        if code.count('-') >= 3:
            score -= min(6, code.count('-'))
        if any(term in code for term in ['tokenized-stock', 'fan-token', 'memecoin', 'veda-vault', 'wrapped-', 'wrapped', 'defichain', 'seized-by', 'bridged-', '-lp']):
            score -= 4
        if len(code_parts) <= 1 and 3 <= len(code) <= 12:
            score += 3
        if len(str(name).split()) <= 2 and 3 <= len(name) <= 20:
            score += 2
    if provider == 'FRED':
        if any(term in lowered for term in ['average price:', 'cost per pound', 'census region', 'market hotness', 'page view count per property', 'median listing price per square feet', '(cbsa)']):
            score -= 5
        if any(term in lowered for term in ['level rest of the world', 'revaluation state and local governments']):
            score -= 4
        if re.search(r'total revenue for\s+\d{4}', lowered):
            score -= 5
        if any(term in lowered for term in ['net equity in life insurance and pension funds', 'asset (ima)']):
            score -= 5
        if any(term in lowered for term in ['employer firms total revenue', 'total expense for', 'establishments subject to federal income tax', 'defined benefit retirement funds', 'commercial paper; asset']):
            score -= 4
        if any(term in category for term in ['search: inflation', 'search: cpi', 'search: gas price', 'tax data']):
            score += 3
    if provider == 'OECD':
        if any(term in lowered for term in ['all countries', 'growth rate over one year', 'less food and energy', 'coicop', 'sut ', 'taxes less subsidies', 'share of final consumption expenditure', 'public services and governance of the policy cycle', 'classroom teachers and academic staffs']):
            score -= 4
        if 'new entrants to and first-time graduates' in lowered:
            score -= 4
        if 'educational attainment level age group and gender' in lowered:
            score -= 3
        if any(term in lowered for term in ['international poverty line', 'household formality', 'inventory of energy subsidies', 'support measures', "africa's development dynamics", 'afdd', 'table 36', 'incidence of full-time and part-time employment', 'harmonized definition']):
            score -= 5
        if any(term in lowered for term in ['analysis by armed group', 'west africa', 'real labour productivity', 'regions']):
            score -= 4
        if 'share of students enrolled in school and work-based programmes' in lowered:
            score += 4
        if any(term in lowered for term in ['social security contributions and payroll taxes', 'household income and saving in the national accounts']):
            score += 3
    if provider == 'EUROSTAT':
        if any(term in lowered for term in ['wine-grape vine varieties', 'vine variety', 'age of the vines']):
            score -= 5
        if any(term in lowered for term in ['activity limitation', 'poverty threshold', 'previous year']) and any(term in lowered for term in ['age', 'sex', 'most frequent activity']):
            score -= 4
        if any(term in lowered for term in ['household composition', 'degree of urbanisation', 'fixed moment in time']):
            score += 3
    return score


def selection_supportability_reason_for_row(record: dict[str, Any]) -> str | None:
    """Return metadata-only sampler supportability provenance for a direct row.

    This is intentionally a candidate-selection prior, not a runtime blocker or
    scoring exclusion. It may inspect only provider-native catalog metadata
    carried by the row; user query prose is not supportability authority here.
    """
    origin = dict(record.get('origin') or {})
    provider = str(
        record.get('provider_stratum')
        or record.get('provider')
        or origin.get('source_provider')
        or ''
    ).strip().upper()
    code = str(origin.get('source_indicator_code') or record.get('code') or '')
    name = str(origin.get('name') or record.get('name') or '')
    category = str(origin.get('category') or record.get('category') or '')
    raw_metadata = origin.get('raw_metadata') or record.get('raw_metadata')
    if provider == 'IMF':
        return imf_catalog_sampler_supportability_reason(code, name, category)
    if provider == 'BIS':
        return bis_catalog_sampler_supportability_reason(code, name, category)
    if provider == 'COINGECKO':
        return coingecko_catalog_sampler_supportability_reason(code, name, category, raw_metadata)
    if provider == 'OECD':
        return oecd_catalog_sampler_supportability_reason(code, name, category, raw_metadata)
    return None


def answerability_supportability_exclusion_reason(record: dict[str, Any]) -> str | None:
    """Return the supportability reason that excludes a row from answerability evidence.

    ``selection_supportability_reason`` marks provider-native inventory that is
    useful for supportability diagnostics, not ordinary user-answerability
    lower-bound evidence.  The exclusion is intentionally narrow: it applies
    only to real user-answerability rows that already carry provider/catalog
    supportability provenance.  General selection quality reasons such as
    ``imf_low_viability_family`` remain ranking signals rather than hard
    exclusions.
    """
    if certification_target_for_row(record) != CERTIFICATION_TARGET_USER_ANSWERABILITY:
        return None
    provenance = record.get('provenance') if isinstance(record.get('provenance'), dict) else {}
    reason = str(provenance.get('selection_supportability_reason') or '').strip()
    return reason or None


def apply_selection_supportability_probe_query(record: dict[str, Any]) -> dict[str, Any]:
    """Use exact provider-native probe text for known unsupported IMF inventory rows.

    User-answerability prompts normally avoid replaying provider codes.  Rows
    that already carry metadata-only IMF supportability provenance are
    different: attempting a naturalized, title-compressed prompt can push the
    production path into semantic clarification or long provider timeouts.  For
    those rows, carry the exact provider code as a fail-closed diagnostic probe
    while preserving the originally synthesized user prompt in provenance.
    This is mechanical provider-native code transport, not a semantic shortcut
    or a pass override.
    """
    if certification_target_for_row(record) != CERTIFICATION_TARGET_USER_ANSWERABILITY:
        return record

    provenance = record.setdefault('provenance', {})
    if not isinstance(provenance, dict):
        return record
    supportability_reason = str(provenance.get('selection_supportability_reason') or '').strip()
    if supportability_reason != 'imf_non_weo_public_surface_unsupported':
        return record

    origin = dict(record.get('origin') or {})
    provider = str(
        record.get('provider_stratum')
        or record.get('provider')
        or origin.get('source_provider')
        or ''
    ).strip().upper()
    if provider != 'IMF':
        return record

    code = str(origin.get('source_indicator_code') or record.get('code') or '').strip().upper()
    if not code:
        return record
    existing_query = str(record.get('query') or '').strip()
    exact_query = f'{code} from IMF'
    if existing_query and existing_query != exact_query:
        provenance.setdefault('original_user_answerability_query', existing_query)
    provenance['supportability_probe_query'] = 'imf_exact_provider_code'
    record['query'] = exact_query
    return record


def natural_phrase_from_name(name: str, description: str = '') -> str:
    raw = str(name or '').strip()
    if not raw:
        return description_context_phrase(description)
    if ',' not in raw:
        bare_tokens = informative_tokens(raw)
        if len(bare_tokens) <= 2 and set(bare_tokens) <= _GENERIC_SHORT_TITLE_TOKENS:
            return description_context_phrase(description) or raw
        return raw
    parts = [part.strip() for part in raw.split(',') if part.strip()]
    kept = []
    for part in parts:
        lowered = part.lower()
        if lowered in _IMF_NOISE_SEGMENTS:
            continue
        if re.search(r'\b(bpm6|manual|isic|coicop|quintile)\b', lowered):
            continue
        kept.append(part)
    if not kept:
        kept = parts[:2]

    if description and len(kept) == 1 and re.fullmatch(r'[A-Z]{1,3}', kept[0].upper()):
        contextual = description_context_phrase(description)
        if contextual:
            return contextual

    head = kept[0]
    head_is_acronym = re.fullmatch(r'[A-Z]{2,5}', head.strip()) is not None
    prefixes: list[str] = []
    suffixes: list[str] = []
    for part in kept[1:]:
        lowered = part.lower()
        if lowered in {'female', 'male', 'urban', 'rural', 'total'}:
            prefixes.append(part)
            continue
        if re.fullmatch(r'age\s+\d{1,2}', lowered):
            suffixes.append(part)
            continue
        if len(lowered.split()) <= 2 and not re.search(r'[()]', lowered):
            if head_is_acronym:
                suffixes.append(part)
            else:
                prefixes.append(part)
            continue
        suffixes.append(part)

    candidate = ' '.join(prefixes + [head] + suffixes).strip()
    tokens = informative_tokens(candidate)
    if len(tokens) <= 2 and set(tokens) <= _GENERIC_SHORT_TITLE_TOKENS:
        return description_context_phrase(description) or candidate
    return candidate


_COINGECKO_GENERIC_ASSET_LABELS = {
    'asset',
    'coin',
    'crypto',
    'cryptocurrency',
    'token',
}


def _coingecko_metadata(row: dict[str, Any]) -> dict[str, Any]:
    origin = dict(row.get('origin') or {})
    raw_metadata = row.get('raw_metadata') or origin.get('raw_metadata')
    return _raw_metadata_dict(raw_metadata)


def _raw_metadata_dict(raw_metadata: Any) -> dict[str, Any]:
    if isinstance(raw_metadata, dict):
        return raw_metadata
    if isinstance(raw_metadata, str) and raw_metadata.strip():
        try:
            parsed = json.loads(raw_metadata)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _coingecko_alias_candidate(value: Any) -> str:
    alias = str(value or '').strip()
    if not alias:
        return ''
    if alias.lower() in _COINGECKO_GENERIC_ASSET_LABELS:
        return ''
    if re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]{1,20}', alias):
        return alias
    return ''


def _coingecko_provider_native_symbol(row: dict[str, Any]) -> str:
    origin = dict(row.get('origin') or {})
    metadata = _coingecko_metadata(row)
    candidates: list[Any] = []
    synonyms = row.get('synonyms') or origin.get('synonyms')
    if isinstance(synonyms, str):
        candidates.extend(re.split(r'[,;|\s]+', synonyms))
    elif isinstance(synonyms, list):
        candidates.extend(synonyms)
    candidates.append(metadata.get('symbol'))
    for candidate in candidates:
        alias = _coingecko_alias_candidate(candidate)
        if alias:
            return alias
    return ''


def _coingecko_has_informative_ascii_token(value: str) -> bool:
    for token in re.findall(r'[A-Za-z0-9]+', str(value or '')):
        if len(token) >= 2:
            return True
    return False


def _coingecko_humanized_slug_label(code: str) -> str:
    if not code:
        return ''
    slug = re.sub(r'-\d+$', '', code)
    human = humanize_slug(slug)
    if not human:
        return ''
    human_lower = human.lower().strip()
    slug_tokens = [token for token in re.split(r'[-_]+', code) if token]
    nontrailing_numeric_tokens = [
        token
        for idx, token in enumerate(slug_tokens)
        if token.isdigit() and idx != len(slug_tokens) - 1
    ]
    if human_lower in _COINGECKO_GENERIC_ASSET_LABELS or nontrailing_numeric_tokens:
        return code
    return human.title()


def _coingecko_short_asset_label_prefers_slug(name: str, code: str) -> bool:
    """Return True when a short CoinGecko label needs exact slug transport.

    CoinGecko catalog names are sometimes ticker-like labels such as ``3A`` or
    ``AAG`` while the executable provider id is a longer slug.  Humanizing that
    slug invents asset-title words that are not in the provider name/symbol and
    can make the runtime miss the exact provider asset.  Keep this mechanical:
    only use the provider-native slug/code already present in catalog metadata.
    """
    normalized_code = str(code or '').strip()
    if not normalized_code or '-' not in normalized_code:
        return False
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]{1,127}', normalized_code):
        return False

    tokens = re.findall(r'[A-Za-z0-9]+', str(name or ''))
    compact = ''.join(tokens)
    if not compact:
        return False
    if len(compact) <= 4:
        return True
    if len(tokens) == 1 and compact.upper() == compact and len(compact) <= 8:
        return True
    code_tokens = [token for token in re.split(r'[-_]+', normalized_code.lower()) if token]
    name_tokens = [token.lower() for token in tokens]
    if (
        len(code_tokens) > len(name_tokens)
        and name_tokens
        and code_tokens[: len(name_tokens)] == name_tokens
    ):
        # CoinGecko slugs sometimes append a provider-native namespace or
        # product qualifier to an otherwise short display name, e.g.
        # ``Aevo`` -> ``aevo-exchange``.  A humanized slug label ("Aevo
        # Exchange") is not the provider title and can be interpreted as a
        # broader/unknown phrase.  Preserve the exact provider-native slug
        # mechanically instead of inventing display-title words.
        return True
    return False


def derive_coin_query_name(row: dict[str, Any]) -> str:
    origin = dict(row.get('origin') or {})
    name = str(row.get('name') or origin.get('name') or '').strip()
    code = str(row.get('code') or origin.get('source_indicator_code') or '').strip()
    if not _coingecko_has_informative_ascii_token(name):
        symbol = _coingecko_provider_native_symbol(row)
        if symbol:
            return symbol
        if code:
            return code
    if _coingecko_short_asset_label_prefers_slug(name, code):
        return code
    if code and '-' in code:
        if len(name.split()) >= 4 or len(name) >= 24 or any(symbol in name for symbol in {'€', '$', '£'}):
            return code
    if len(name) > 4 and not re.fullmatch(r'[A-Z0-9]{1,5}', name):
        return name
    slug_label = _coingecko_humanized_slug_label(code)
    if slug_label:
        return slug_label
    return name.title() if name else code.title()


def query_mentions_country(text: str) -> bool:
    return bool(detect_country_codes_in_text(text))


def count_distinct_country_mentions(text: str) -> int:
    return len(detect_country_codes_in_text(text))


def _strip_imf_iso3_country_prefix(code: str) -> str:
    value = str(code or '').strip().upper()
    match = re.match(r'^([A-Z]{3})_', value)
    if not match:
        return value
    prefix = match.group(1)
    if CountryResolver is not None:
        try:
            if CountryResolver.to_iso2(prefix):
                return value[len(prefix) + 1:]
        except Exception:
            pass
    return value


def _is_imf_aggregate_trade_code(code: str) -> bool:
    bare_code = _strip_imf_iso3_country_prefix(code)
    if any(fragment in bare_code for fragment in ('_H5_', '_HS', '_SITC', '_CPC', '_BEC')):
        return False
    return bool(
        re.fullmatch(r'(?:T?[XM]G?|[XM]G)_(?:FOB|CIF)_(?:USD|XDC)', bare_code)
        or re.fullmatch(r'(?:T?[XM]G?|[XM]G)_(?:FOB|CIF)_(?:USD|XDC)_IX', bare_code)
    )


def _is_imf_aggregate_cpi_code(code: str, name: str = '') -> bool:
    bare_code = _strip_imf_iso3_country_prefix(code)
    text = str(name or '').lower()
    if 'weight' in text:
        return False
    if bare_code == 'PCPI_IX':
        return True
    if re.fullmatch(r'PCPI_CP_?\d{2}(?:_BY\d{4}|_BY\d{4}M\d{2})?_IX', bare_code):
        return True
    return False


def _is_imf_aggregate_ppi_code(code: str, name: str = '') -> bool:
    bare_code = _strip_imf_iso3_country_prefix(code)
    text = str(name or '').lower()
    if any(fragment in bare_code for fragment in ('ISIC', 'NACE')):
        return False
    if any(term in text for term in ['by activity', 'commodities by activity', 'manufacture of', 'mining of']):
        return False
    return bare_code in {'PPPI_IX', 'PPI_IX', 'WPI_IX'} or bare_code == 'PPPIA_IX'


def _is_imf_bop_public_sdmx_code(code: str, name: str = '') -> bool:
    bare_code = _strip_imf_iso3_country_prefix(code)
    if bare_code.startswith('BOP_'):
        bare_code = bare_code[len('BOP_'):]
    if re.search(r'(?:^|_)\d+[A-Z]?(?:_|$)', bare_code):
        return False
    text = f"{name or ''} {code or ''}".lower()
    if not (
        'balance of payments' in text
        or 'bpm6' in text
        or '_bp6_' in f"_{bare_code.lower()}_"
    ):
        return False
    if not re.fullmatch(
        r'B[A-Z0-9_]*(?:_BP6)?(?:_FY)?_(?:USD|EUR|XDC|XDR)',
        bare_code,
    ):
        return False
    return True


def imf_public_sdmx_runtime_family(code: str, name: str = '', category: str = '') -> str | None:
    """Return the narrow public IMF.STA SDMX family now executable by runtime.

    This helper is deliberately conservative.  It only marks rows as supported
    when the provider code maps to a documented country-level SDMX 2.1 key.
    Detailed HS/SITC/CPC trade, city CPI, ISIC/NACE PPI, fiscal, monetary,
    and other non-WEO families remain explicit supportability blockers until
    their exact public dimensions are implemented.
    """
    if str(category or '').strip().lower() == 'weo':
        return None
    if _is_imf_aggregate_trade_code(code):
        return 'itg_aggregate'
    if _is_imf_aggregate_cpi_code(code, name):
        return 'cpi_aggregate'
    if _is_imf_aggregate_ppi_code(code, name):
        return 'ppi_aggregate'
    return None


def statscan_title_needs_dimension_evidence(title: str) -> bool:
    """Return True when a StatsCan catalog title advertises required dimensions.

    Exact product ids are safe mechanical table selectors, but code-only probes
    can hide arbitrary provider defaults for tables whose title itself says the
    surface is broken down by a dimension. Keep those rows on the natural-title
    path so runtime must either extract dimensions or fail closed.
    """
    text = str(title or '').lower()
    return bool(
        re.search(r'\bby\s+(?:sex|gender|age|age group|province|territory|geography|occupation|industry|income|education|type)\b', text)
        or any(
            cue in text
            for cue in [
                'provinces',
                'territories',
                'health regions',
                'geography',
                'income quintile',
                'parental education',
            ]
        )
    )


def statscan_title_has_explicit_year_range(title: str) -> bool:
    """Return True when a catalog title carries an explicit historical range."""
    return bool(
        re.search(
            r'(?<!\d)(?:19|20)\d{2}(?!\d)\s*(?:to|-|–|—)\s*(?<!\d)(?:19|20)\d{2}(?!\d)',
            str(title or ''),
            flags=re.IGNORECASE,
        )
    )


def _provider_query_label(provider_upper: str, provider: str) -> str:
    return {
        'COINGECKO': 'CoinGecko',
        'COMTRADE': 'Comtrade',
        'EUROSTAT': 'Eurostat',
        'EXCHANGERATE': 'ExchangeRate',
        'FRED': 'FRED',
        'IMF': 'IMF',
        'OECD': 'OECD',
        'STATSCAN': 'Statistics Canada',
        'WORLDBANK': 'World Bank',
        'BIS': 'BIS',
    }.get(provider_upper, provider or 'OpenEcon')


def _default_user_answerability_country(provider: str, provider_upper: str, category: str, name: str) -> str:
    defaults = DEFAULT_COUNTRIES_BY_PROVIDER.get(provider) or DEFAULT_COUNTRIES_BY_PROVIDER.get(provider_upper) or ['United States']
    choice = defaults[stable_seed(provider or provider_upper, name) % len(defaults)]
    return preferred_default_country_for_record(provider_upper, category, name, defaults, choice)


def _normal_title_key(value: str) -> str:
    return re.sub(r'[^a-z0-9]+', ' ', str(value or '').lower()).strip()


def _has_short_acronym_comma_suffix(value: str) -> bool:
    """Return true for provider titles ending in a short acronym qualifier.

    Titles such as ``Total wages and salaries, BLS`` are already natural enough
    for user-answerability probes.  Moving the acronym to the front makes a less
    literal prompt and can bypass provider exact-title recovery.  This is a
    title-shape preservation rule, not an acronym-to-code mapping.
    """

    parts = [part.strip() for part in str(value or '').split(',') if part.strip()]
    if len(parts) < 2:
        return False
    suffix = parts[-1]
    head = ', '.join(parts[:-1]).strip()
    return bool(
        re.fullmatch(r'[A-Z]{2,8}', suffix)
        and len(informative_tokens(head)) >= 3
    )


def _fred_average_price_title_needs_exact_title(value: str) -> bool:
    """Return true for FRED average-price titles whose scope is in the title.

    FRED/BLS average-price series often differ only by late provider-native
    geography qualifiers such as ``in the Northeast Census Region - Urban`` or
    ``in U.S. City Average``.  Top-token compression drops those qualifiers and
    can produce a broad prompt that exact-title recovery cannot lock to the
    intended public series.  This is a title-shape preservation rule, not a
    commodity-to-code mapping.
    """

    title = re.sub(r'\s+', ' ', str(value or '')).strip()
    if not re.match(r'^Average Price:', title, flags=re.IGNORECASE):
        return False
    return bool(
        re.search(
            r'\bin\s+(?:the\s+)?(?:Northeast|Midwest|South|West)\s+Census\s+Region\b',
            title,
            flags=re.IGNORECASE,
        )
        or re.search(r'\bin\s+U\.S\.\s+City\s+Average\b', title, flags=re.IGNORECASE)
        or re.search(r'\bCBSA\b', title, flags=re.IGNORECASE)
    )


def _title_token_coverage(origin_title: str, query: str) -> float:
    origin_tokens = [
        token
        for token in re.findall(r'[a-z0-9]+', str(origin_title or '').lower())
        if len(token) > 1
    ]
    if not origin_tokens:
        return 0.0
    query_tokens = set(
        token
        for token in re.findall(r'[a-z0-9]+', str(query or '').lower())
        if len(token) > 1
    )
    if not query_tokens:
        return 0.0
    return sum(1 for token in origin_tokens if token in query_tokens) / len(origin_tokens)


def _looks_like_eurostat_dataset_code(value: str) -> bool:
    return re.fullmatch(r'[A-Z][A-Z0-9_]{3,}', str(value or '').strip(), flags=re.IGNORECASE) is not None


def _metadata_unit_from_raw(raw_value: Any) -> str:
    if not raw_value:
        return ''
    try:
        parsed = json.loads(str(raw_value))
    except Exception:
        return ''
    if not isinstance(parsed, dict):
        return ''
    return str(parsed.get('unit') or '').strip()


def _metadata_frequency_from_raw(raw_value: Any) -> str:
    if not raw_value:
        return ''
    try:
        parsed = json.loads(str(raw_value))
    except Exception:
        return ''
    if not isinstance(parsed, dict):
        return ''
    return str(parsed.get('frequency') or parsed.get('frequency_short') or '').strip()


def _row_unit(row: dict[str, Any], origin: dict[str, Any]) -> str:
    return (
        str(row.get('unit') or '').strip()
        or str(origin.get('unit') or '').strip()
        or _metadata_unit_from_raw(origin.get('raw_metadata') or row.get('raw_metadata'))
    )


def _row_frequency(row: dict[str, Any], origin: dict[str, Any]) -> str:
    return (
        str(row.get('frequency') or '').strip()
        or str(origin.get('frequency') or '').strip()
        or _metadata_frequency_from_raw(origin.get('raw_metadata') or row.get('raw_metadata'))
    )


def _query_unit_text(unit: str) -> str:
    """Return unit text suitable for a real-user prompt."""
    value = re.sub(r'\s+', ' ', str(unit or '').replace('%', 'percent')).strip(' ,;:')
    return value


def _raw_metadata_source(raw_metadata: Any) -> str:
    parsed = _raw_metadata_dict(raw_metadata)
    source = parsed.get('source') or ''
    if isinstance(source, dict):
        source = source.get('value') or ''
    return re.sub(r'\s+', ' ', str(source or '')).strip(' ,;:')


def _imf_generic_datamapper_prompt_needs_context(
    *,
    name: str,
    phrase: str,
    category: str,
) -> bool:
    category_upper = str(category or '').strip().upper()
    if not category_upper or category_upper in {'INDICATOR', 'DATAFLOW'}:
        return False
    tokens = informative_tokens(phrase or name)
    if len(tokens) <= 2:
        return True
    return bool(re.fullmatch(r'[A-Z0-9_ -]{2,12}', str(name or '').strip()))


def _append_imf_generic_datamapper_context(
    phrase: str,
    *,
    row: dict[str, Any],
    origin: dict[str, Any],
    name: str,
    category: str,
) -> str:
    """Disambiguate generic IMF DataMapper titles with row-native metadata.

    This is validation query synthesis only: it copies public catalog unit and
    source text into an otherwise too-short generated user prompt.  It never
    appends provider codes or hand-authored semantic expansions.
    """
    if not _imf_generic_datamapper_prompt_needs_context(name=name, phrase=phrase, category=category):
        return phrase

    pieces = [phrase]
    current_tokens = set(informative_tokens(phrase))
    unit = _query_unit_text(_row_unit(row, origin))
    if unit:
        unit_tokens = set(informative_tokens(unit))
        if unit_tokens and not unit_tokens <= current_tokens:
            pieces.append(f"in {unit}")
            current_tokens.update(unit_tokens)

    source = _raw_metadata_source(origin.get('raw_metadata') or row.get('raw_metadata'))
    if source:
        source_tokens = set(informative_tokens(source))
        if source_tokens and not source_tokens <= current_tokens:
            pieces.append(source)

    return re.sub(r'\s+', ' ', ' '.join(piece for piece in pieces if piece)).strip()


@lru_cache(maxsize=32)
def _provider_title_units(provider: str, db_path_text: str = str(DEFAULT_DB)) -> dict[str, list[dict[str, str]]]:
    provider = str(provider or '').strip()
    if not provider:
        return {}
    db_path = Path(db_path_text)
    if not db_path.exists():
        return {}
    try:
        con = sqlite3.connect(str(db_path))
        con.row_factory = sqlite3.Row
        rows = con.execute(
            'SELECT code, name, unit, frequency, raw_metadata FROM indicators WHERE provider = ?',
            (provider,),
        ).fetchall()
        con.close()
    except Exception:
        return {}

    by_title: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        name = str(row['name'] or '')
        key = _normal_title_key(name)
        if not key:
            continue
        unit = str(row['unit'] or '').strip() or _metadata_unit_from_raw(row['raw_metadata'])
        frequency = str(row['frequency'] or '').strip() or _metadata_frequency_from_raw(row['raw_metadata'])
        by_title.setdefault(key, []).append(
            {
                'code': str(row['code'] or '').strip(),
                'unit': unit,
                'frequency': frequency,
                'name': name.strip(),
            }
        )
    return by_title


def _unit_disambiguator_for_duplicate_title(
    row: dict[str, Any],
    *,
    provider: str,
    name: str,
    phrase: str,
    origin: dict[str, Any],
) -> str:
    """Return unit text when a provider-native title maps to multiple codes.

    User-answerability certification should not mark an arbitrary runtime pick
    as correct when the sampled title is duplicated in the provider catalog and
    the query omits the measurement unit that distinguishes the rows.  The unit
    is catalog metadata, not a semantic shortcut: it makes the user prompt
    explicit enough for adjudication.
    """
    title_key = _normal_title_key(name)
    if not title_key:
        return ''
    matches = _provider_title_units(provider).get(title_key) or []
    distinct_codes = {str(match.get('code') or '').upper() for match in matches if match.get('code')}
    if len(distinct_codes) <= 1:
        return ''
    distinct_units = {
        _normal_title_key(str(match.get('unit') or ''))
        for match in matches
        if str(match.get('unit') or '').strip()
    }
    if len(distinct_units) <= 1:
        return ''
    unit = _row_unit(row, origin)
    if not unit:
        code = str(row.get('code') or origin.get('source_indicator_code') or '').strip().upper()
        unit = next(
            (
                str(match.get('unit') or '').strip()
                for match in matches
                if str(match.get('code') or '').strip().upper() == code
            ),
            '',
        )
    if not unit:
        return ''
    unit_tokens = set(re.findall(r'[a-z0-9]+', unit.lower()))
    phrase_tokens = set(re.findall(r'[a-z0-9]+', str(phrase or '').lower()))
    informative_unit_tokens = {token for token in unit_tokens if len(token) > 2 and token not in {'per', 'the', 'and'}}
    if informative_unit_tokens and informative_unit_tokens <= phrase_tokens:
        return ''
    return re.sub(r'\s+', ' ', re.sub(r'[;:]+', ' ', unit)).strip(' ,;')


def _append_unit_disambiguator(phrase: str, unit: str) -> str:
    if not unit:
        return phrase
    return re.sub(r'\s+', ' ', f"{phrase} in {unit}").strip()


def _frequency_disambiguator_for_duplicate_title(
    row: dict[str, Any],
    *,
    provider: str,
    name: str,
    phrase: str,
    origin: dict[str, Any],
) -> str:
    """Return frequency text when a duplicated title differs by cadence.

    This is prompt disambiguation, not a provider-code shortcut.  FRED and
    similar catalogs often publish the same title/unit at monthly, weekly, or
    daily frequencies; a real user must specify cadence if they want a
    particular public series.
    """
    title_key = _normal_title_key(name)
    if not title_key:
        return ''
    matches = _provider_title_units(provider).get(title_key) or []
    distinct_codes = {str(match.get('code') or '').upper() for match in matches if match.get('code')}
    if len(distinct_codes) <= 1:
        return ''
    distinct_frequencies = {
        _normal_title_key(str(match.get('frequency') or ''))
        for match in matches
        if str(match.get('frequency') or '').strip()
    }
    if len(distinct_frequencies) <= 1:
        return ''
    frequency = _row_frequency(row, origin)
    if not frequency:
        code = str(row.get('code') or origin.get('source_indicator_code') or '').strip().upper()
        frequency = next(
            (
                str(match.get('frequency') or '').strip()
                for match in matches
                if str(match.get('code') or '').strip().upper() == code
            ),
            '',
        )
    if not frequency:
        return ''
    frequency_tokens = set(re.findall(r'[a-z0-9]+', frequency.lower()))
    phrase_tokens = set(re.findall(r'[a-z0-9]+', str(phrase or '').lower()))
    informative_frequency_tokens = {
        token for token in frequency_tokens if len(token) > 2 and token not in {'the', 'and'}
    }
    if informative_frequency_tokens and informative_frequency_tokens <= phrase_tokens:
        return ''
    return re.sub(r'\s+', ' ', re.sub(r'[;:]+', ' ', frequency)).strip(' ,;')


def _append_frequency_disambiguator(phrase: str, frequency: str) -> str:
    if not frequency:
        return phrase
    return re.sub(r'\s+', ' ', f"{phrase} ({frequency})").strip()


def synthesize_user_answerability_query_for_row(row: dict[str, Any]) -> str:
    """Build a real-user-style query from catalog evidence without requiring replay of legacy codes.

    The frozen catalog row can still provide title/category/coverage evidence for
    prompt sampling, but the query should ask for the economic concept the user
    needs.  Provider-native IDs are kept only when they are the ordinary user
    language for the surface (for example FX pairs or a fallback crypto slug),
    not as the success criterion for legacy catalog replay.
    """
    origin = dict(row.get('origin') or {})
    provider = str(row.get('provider') or row.get('provider_stratum') or origin.get('source_provider') or '')
    provider_upper = provider.upper()
    code = str(row.get('code') or origin.get('source_indicator_code') or '').strip()
    name = str(row.get('name') or origin.get('name') or '').strip()
    description = str(row.get('description') or origin.get('description') or '').strip()
    category = str(row.get('category') or origin.get('category') or '')
    choice = _default_user_answerability_country(provider, provider_upper, category, name)
    inferred_country = None
    if provider_upper not in {'EXCHANGERATE', 'COINGECKO', 'COMTRADE'}:
        inferred_country = (
            detect_single_country_from_text(name)
            or detect_single_country_from_text(str(row.get('coverage') or origin.get('coverage') or ''))
        )
        if not inferred_country and provider_upper != 'FRED':
            description_country = detect_single_country_from_text(description)
            if description_country and description_country.lower() not in {'world', 'global', 'worldwide'}:
                inferred_country = description_country
        if inferred_country:
            choice = inferred_country
    phrase = natural_phrase_from_name(name, description) or name or description or code
    provider_label = _provider_query_label(provider_upper, provider)
    preserve_provider_title = (
        provider_upper == 'WORLDBANK'
        and bool(name)
        and (
            str(category or '').strip().lower() == 'world development indicators'
            or not inferred_country
        )
    ) or (
        provider_upper == 'IMF'
        and bool(name)
        and imf_public_sdmx_runtime_family(code, name, category) == 'cpi_aggregate'
    ) or (
        provider_upper == 'EUROSTAT'
        and bool(name)
        and not _looks_like_eurostat_dataset_code(name)
    ) or (
        provider_upper in {'STATSCAN', 'STATISTICS CANADA'}
        and bool(name)
        and not re.fullmatch(r'\d{8,10}', name.strip())
    ) or (
        provider_upper == 'OECD'
        and bool(name)
        and not re.fullmatch(r'[A-Z0-9_@]{1,40}', name.strip())
    ) or (
        provider_upper == 'FRED'
        and bool(name)
        and (
            _has_short_acronym_comma_suffix(name)
            or _fred_average_price_title_needs_exact_title(name)
        )
    )
    if preserve_provider_title and name:
        # Some providers expose many similarly named public tables/datasets that
        # differ only in late title qualifiers.  Collapsing those titles to top
        # tokens creates ambiguous prompts and can certify a wrong neighboring
        # surface.  Keep the provider-native title as the real-user question text
        # and let exact-title resolution/adjudication decide outcome.
        phrase = name
    if (len(phrase) > 120 or len(phrase.split()) > 18) and not preserve_provider_title:
        # The user-answerability target must not turn a frozen catalog title
        # into a giant legacy-row replay prompt.  When a title is too dense,
        # use the row only as sampling context and ask for the core concept
        # tokens a user would reasonably type.
        phrase = ' '.join(
            top_tokens(
                name,
                description,
                category,
                str(origin.get('subcategory') or row.get('subcategory') or ''),
                limit=6,
            )
        )

    phrase = _append_unit_disambiguator(
        phrase,
        _unit_disambiguator_for_duplicate_title(
            row,
            provider=provider,
            name=name,
            phrase=phrase,
            origin=origin,
        ),
    )
    phrase = _append_frequency_disambiguator(
        phrase,
        _frequency_disambiguator_for_duplicate_title(
            row,
            provider=provider,
            name=name,
            phrase=phrase,
            origin=origin,
        ),
    )
    if provider_upper == 'IMF':
        phrase = _append_imf_generic_datamapper_context(
            phrase,
            row=row,
            origin=origin,
            name=name,
            category=category,
        )

    if provider_upper == 'COINGECKO':
        return f"{derive_coin_query_name(row)} cryptocurrency price from CoinGecko".strip()
    if provider_upper == 'EXCHANGERATE':
        target_code = str(row.get('code') or origin.get('source_indicator_code') or '').strip().upper()
        if re.fullmatch(r'[A-Z]{3}', target_code) and target_code != 'USD':
            return f"USD to {target_code} exchange rate from ExchangeRate"
        pair_match = re.search(r'\b([A-Z]{3})\s*(?:to|/|-)\s*([A-Z]{3})\b', name.upper())
        if pair_match:
            return f"{pair_match.group(1)} to {pair_match.group(2)} exchange rate from ExchangeRate"
        return f"{choice} exchange rate from ExchangeRate"
    if provider_upper == 'COMTRADE':
        commodity = re.sub(r'^(?:HS)?\d{2,6}\s*[-:]\s*', '', name, flags=re.IGNORECASE).strip()
        code_numeric = re.sub(r'^HS', '', code.upper()).strip() if code else ''
        if commodity and re.fullmatch(r'\d{2,6}', code_numeric):
            return f"{choice} exports of HS {code_numeric} {commodity} from Comtrade"
        if commodity:
            return f"{choice} exports of {commodity} from Comtrade"
        if re.fullmatch(r'\d{2,6}', code_numeric):
            return f"{choice} exports of HS {code_numeric} from Comtrade"
        return f"{choice} exports from Comtrade"
    if provider_upper == 'STATSCAN':
        prefix = '' if query_mentions_country(phrase) else 'Canada '
        return f"{prefix}{phrase} from Statistics Canada".strip()
    if phrase and code and phrase.upper() == code.upper():
        phrase = description_context_phrase(description) or f"{provider_label} indicator {phrase}"
    if provider_upper == 'OECD' and re.fullmatch(r'[A-Z0-9_@]{1,40}', phrase):
        phrase = description_context_phrase(description) or f"OECD indicator {phrase}"
    if provider_upper == 'EUROSTAT' and _looks_like_eurostat_dataset_code(phrase):
        phrase = description_context_phrase(description) or f"Eurostat dataset {phrase}"
    if provider_upper == 'EUROSTAT':
        # Eurostat rows whose catalog coverage is an EU aggregate should not be
        # turned into arbitrary member-state questions.  The runtime can answer
        # these through Eurostat's provider-native all-available aggregate
        # surface, while a synthetic "Germany/France/..." prompt can be false
        # for historical aggregate-only datasets.
        coverage = str(row.get('coverage') or origin.get('coverage') or '')
        prefix = ''
        if not query_mentions_country(phrase) and not _is_eurostat_aggregate_coverage(coverage):
            prefix = f"{choice} "
        return f"{prefix}{phrase} from {provider_label}".strip()
    if provider_upper == 'WORLDBANK':
        # WorldBank has a native all-country surface.  Do not inject an
        # arbitrary default country into user-answerability prompts unless the
        # catalog evidence itself carries an intrinsic country scope.  Random
        # default countries turn otherwise answerable provider-native series
        # into false no-data failures for sparse education/food-price surfaces.
        prefix = '' if query_mentions_country(phrase) or not inferred_country else f"{choice} "
        return f"{prefix}{phrase} from {provider_label}".strip()
    if provider_upper == 'OECD':
        # OECD dataflows carry provider-native default scopes in their SDMX
        # structure metadata.  User-answerability prompts should ask for the
        # table/concept a user needs, not inject an arbitrary country whose
        # default slice may be unpopulated for long-tail tables.  Explicit or
        # intrinsic country mentions are still preserved above.
        return f"{phrase} from {provider_label}".strip()
    if provider_upper == 'FRED' and _fred_average_price_title_needs_exact_title(name):
        return f"{phrase} from {provider_label}".strip()

    prefix = '' if query_mentions_country(phrase) else f"{choice} "
    return f"{prefix}{phrase} from {provider_label}".strip()


def synthesize_direct_query_for_row(row: dict[str, Any], *, certification_target: str | None = None) -> str:
    target = normalize_certification_target(
        certification_target or certification_target_for_row(row),
        default=CERTIFICATION_TARGET_LEGACY_CATALOG_REPLAY,
    )
    if target == CERTIFICATION_TARGET_USER_ANSWERABILITY:
        return synthesize_user_answerability_query_for_row(row)

    origin = dict(row.get('origin') or {})
    provider = str(row.get('provider') or row.get('provider_stratum') or origin.get('source_provider') or '')
    provider_upper = provider.upper()
    code = str(row.get('code') or origin.get('source_indicator_code') or '').strip()
    name = str(row.get('name') or origin.get('name') or '').strip()
    description = str(row.get('description') or origin.get('description') or '').strip()
    transform = infer_transform_family(name, description, str(row.get('unit') or ''), str(row.get('code') or ''))
    defaults = DEFAULT_COUNTRIES_BY_PROVIDER.get(provider, ['United States'])
    choice = defaults[stable_seed(provider, name) % len(defaults)]
    choice = preferred_default_country_for_record(provider_upper, str(row.get('category') or ''), name, defaults, choice)
    inferred_country = None
    if provider_upper not in {'EXCHANGERATE', 'COINGECKO'}:
        inferred_country = (
            detect_single_country_from_text(name)
            or detect_single_country_from_text(str(row.get('coverage') or ''))
        )
        if not inferred_country and provider_upper != 'FRED':
            description_country = detect_single_country_from_text(description)
            if description_country and description_country.lower() not in {'world', 'global', 'worldwide'}:
                inferred_country = description_country
        if inferred_country:
            choice = inferred_country
    phrase = natural_phrase_from_name(name, description)

    if provider_upper == 'COINGECKO':
        return f"{derive_coin_query_name(row)} cryptocurrency price from CoinGecko"
    if provider_upper == 'EXCHANGERATE':
        target_code = str(row.get('code') or '').strip().upper()
        if re.fullmatch(r'[A-Z]{3}', target_code) and target_code != 'USD':
            return f"USD to {target_code} exchange rate from ExchangeRate"
        return f"{choice} exchange rate from ExchangeRate"
    if provider_upper == 'COMTRADE':
        code_numeric = re.sub(r'^HS', '', code.upper()).strip() if code else ''
        if re.fullmatch(r'\d{2,6}', code_numeric):
            return f"{choice} exports of HS{code_numeric} from Comtrade"
        commodity = re.sub(r'^\d+\s*-\s*', '', name).strip() or name
        return f"{choice} exports of {commodity} from Comtrade"
    if provider_upper == 'STATSCAN':
        statscan_product_id = ''.join(ch for ch in code if ch.isdigit())
        statscan_title = name or description or phrase
        if (
            re.fullmatch(r'\d{8}|\d{10}', statscan_product_id)
            and (
                not statscan_title_needs_dimension_evidence(statscan_title)
                or statscan_title_has_explicit_year_range(statscan_title)
            )
        ):
            # Carry the exact provider-native table/product id while retaining
            # the catalog title as query evidence for downstream dimension
            # extraction and historical-date parsing.  Historical range titles
            # need the product id so default recent windows do not erase the
            # archived provider surface.  This is mechanical product-id
            # transport, not semantic table selection.
            title_evidence = re.sub(r'[,;:]+', ' ', statscan_title)
            title_evidence = re.sub(r'\s+', ' ', title_evidence).strip()
            return f"{statscan_product_id[:8]} {title_evidence} from StatsCan".strip()
        return f"Canada {phrase} from Statistics Canada"
    if provider_upper == 'IMF':
        sdmx_family = imf_public_sdmx_runtime_family(code, name, str(row.get('category') or ''))
        if sdmx_family:
            return f"{code.upper()} from IMF"
        prefix = '' if query_mentions_country(phrase) else f"{choice} "
        natural_query = f"{prefix}{phrase} from IMF".strip()
        return natural_query
    if provider_upper == 'WORLDBANK':
        # The frozen catalog row already carries the provider-native WorldBank
        # code.  Use it directly so certification probes do not enter
        # ambiguous title-search/list-result paths, and so code fragments such
        # as ``.CN``/``.CD`` cannot be mistaken for user-supplied countries.
        # This is exact provider-code transport, not semantic code selection.
        if code and re.fullmatch(r'[A-Za-z][A-Za-z0-9_.-]{1,127}', code):
            coverage_country = detect_single_country_from_text(str(row.get('coverage') or ''))
            prefix = f"{coverage_country} " if coverage_country else ''
            return f"{prefix}{code} from World Bank".strip()

        # WorldBank has a native all-country surface.  Injecting an arbitrary
        # default country into broad catalog-title certification rows turns
        # real provider coverage into false "data not available" failures
        # whenever that indicator is not populated for the sampled default
        # country.  Keep explicit/intrinsic geography, but otherwise certify
        # the provider title against the provider's all-country execution path.
        prefix = ''
        if not query_mentions_country(phrase) and inferred_country:
            prefix = f"{choice} "
        return f"{prefix}{phrase} from World Bank".strip()
    if provider_upper == 'OECD':
        if code and re.fullmatch(r'(?:OECD_)?DSD_[A-Za-z0-9_]+@DF_[A-Za-z0-9_]+', code):
            prefix = '' if query_mentions_country(code) else f"{choice} "
            return f"{prefix}{code.upper()} from OECD".strip()
        if re.fullmatch(r'[A-Z0-9]{1,6}', phrase):
            phrase = f"{provider} indicator {phrase}"
        prefix = '' if query_mentions_country(phrase) else f"{choice} "
        return f"{prefix}{phrase} from OECD".strip()
    if provider_upper == 'EUROSTAT':
        # Eurostat catalog rows carry provider-native dataset IDs.  Use those
        # exact IDs for certification probes so punctuation such as "NACE Rev."
        # cannot be parsed as a dataset name.  This is mechanical code
        # transport, not semantic dataset selection.
        eurostat_code = code.split('$', 1)[0].strip().upper() if code else ''
        if eurostat_code and re.fullmatch(r'[A-Z][A-Z0-9_]{3,}', eurostat_code):
            return f"{eurostat_code} from Eurostat".strip()
        prefix = '' if query_mentions_country(phrase) else f"{choice} "
        return f"{prefix}{phrase} from Eurostat".strip()
    if provider_upper == 'BIS':
        if re.fullmatch(r'[A-Z0-9]{1,8}', phrase):
            phrase = f"{provider} indicator {phrase}"
        prefix = '' if query_mentions_country(phrase) else f"{choice} "
        return f"{prefix}{phrase} from BIS".strip()
    if provider_upper == 'FRED':
        # The frozen catalog row already carries the provider-native FRED
        # series ID.  Use it directly for certification probes so direct-cert
        # does not depend on natural-language title search for country-like,
        # discontinued, or otherwise ambiguous titles.  This is exact
        # provider-code transport, not semantic code selection.
        if code and re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{2,}', code):
            return f"{code.upper()} from FRED".strip()
        prefix = '' if query_mentions_country(phrase) else f"{choice} "
        return f"{prefix}{phrase} from FRED".strip()
    if transform in {'imports', 'exports', 'trade_balance', 'current_account'}:
        return f'{choice} {name}'
    prefix = '' if query_mentions_country(phrase) else f"{choice} "
    return f'{prefix}{phrase}'.strip()


def infer_transform_family(name: str, description: str = '', unit: str = '', code: str = '') -> str:
    text = ' '.join([name or '', description or '', unit or '', code or '']).lower()
    if 'per capita' in text or '.pcap.' in text or 'pcap' in text:
        return 'per_capita'
    if 'ppp' in text:
        return 'ppp'
    if 'deflator' in text:
        return 'deflator'
    if 'growth' in text or 'annual %' in text or 'rate of change' in text:
        return 'growth'
    if 'constant' in text or 'real ' in text:
        return 'real'
    if 'current' in text or 'nominal' in text:
        return 'nominal'
    if 'import' in text:
        return 'imports'
    if 'export' in text:
        return 'exports'
    if 'trade balance' in text or 'current account' in text:
        return 'trade_balance' if 'trade balance' in text else 'current_account'
    if '% of gdp' in text or 'percentage of gdp' in text:
        return 'ratio_percent_of_gdp'
    if 'yield' in text or 'interest rate' in text or 'policy rate' in text:
        return 'rate_yield'
    if 'debt' in text or 'credit' in text:
        return 'debt_credit'
    return 'level'


def infer_scope_family(provider: str, coverage: str | None = None) -> str:
    provider = str(provider)
    coverage_text = (coverage or '').lower()
    if provider == 'StatsCan':
        return 'subnational' if 'canada' in coverage_text else 'single_country'
    if provider == 'Comtrade':
        return 'bilateral'
    if provider == 'ExchangeRate':
        return 'mixed_provider_scope'
    if provider == 'CoinGecko':
        return 'single_country'
    return 'single_country'


def default_query_for_row(row: dict[str, Any], *, certification_target: str | None = None) -> str:
    return synthesize_direct_query_for_row(row, certification_target=certification_target)


def audit_direct_query_shape(row: dict[str, Any]) -> dict[str, Any]:
    query = str(row.get('query') or default_query_for_row(row) or '')
    origin = dict(row.get('origin') or {})
    evaluation_target = certification_target_for_row(row)
    origin_name = str(origin.get('name') or row.get('name') or '').strip()
    origin_name_lower = origin_name.lower()
    origin_code_upper = str(
        origin.get('source_indicator_code')
        or row.get('code')
        or ''
    ).strip().upper()
    provider = str(row.get('provider') or row.get('provider_stratum') or origin.get('source_provider') or '').strip()
    provider_upper = provider.upper()
    reasons: list[str] = []
    query_lower = query.lower()
    parsed_metadata: dict[str, Any] = {}
    metadata_text = ''
    if provider_upper in {'IMF', 'WORLDBANK', 'OECD', 'EUROSTAT'}:
        parsed_metadata, metadata_text = provider_metadata_context(row)
    worldbank_text = f"{query_lower} {origin_name_lower} {metadata_text}".strip()
    query_country_codes = detect_country_codes_in_text(query)
    origin_country_codes = detect_country_codes_in_text(origin_name)

    punctuation_hits = sum(query.count(ch) for ch in [',', ';', ':', '(', ')'])
    if len(query) >= 120:
        reasons.append('very_long_query')
    elif len(query) >= 90:
        reasons.append('long_query')
    if punctuation_hits >= 6:
        reasons.append('punctuation_dense')
    if any(re.search(pattern, query) for pattern in DIRECT_QUERY_JARGON_PATTERNS):
        reasons.append('catalog_jargon')
    if origin_name and query.endswith(origin_name) and len(origin_name) >= 60:
        reasons.append('provider_title_like')
    if len(re.findall(r'\b[A-Z]{3,}\b', query)) >= 2:
        reasons.append('acronym_dense')
    query_tail = re.sub(r'^(United States|US|Japan|Germany|France|Italy|China|Brazil|Canada)\s+', '', query, flags=re.IGNORECASE).strip()
    if query_tail and re.fullmatch(r'[A-Z0-9]{1,6}', query_tail) and query_tail.upper() not in _SAFE_DIRECT_ACRONYMS:
        reasons.append('opaque_acronym_query')
    if (
        re.search(r'^\d{4,}[A-Z0-9._-]*\s*:', query_tail)
        or re.search(r'^\d{4,}[A-Z0-9._-]*\s*:', origin_name)
    ):
        reasons.append('indicator_code_prefix')
    if count_distinct_country_mentions(query) > 1:
        reasons.append('country_scope_conflict')
    elif len(query_country_codes) == 1 and len(origin_country_codes) == 1 and query_country_codes != origin_country_codes:
        reasons.append('country_scope_conflict')
    if (
        ('average age' in query_lower and any(term in query_lower for term in ['urban', 'rural', 'female', 'male', 'men', 'women']))
        or (re.search(r'\bage\s+\d{1,2}', query_lower) and any(term in query_lower for term in ['female', 'male', 'men', 'women', 'urban', 'rural']))
    ):
        reasons.append('micro_demographic_slice')
    if (
        (re.search(r'\baged?\s+\d{1,2}(?:-\d{1,2})?\b', query_lower) and any(term in query_lower for term in ['school', 'schooling', 'education', 'barro-lee', 'primary education', 'secondary education', 'tertiary education']))
        or ('schools with access to internet' in query_lower and any(term in query_lower for term in ['rural', 'urban', 'male', 'female']))
    ):
        reasons.append('education_subgroup_slice')
    if any(term in query_lower for term in ['poorest', 'richest', 'quintile', 'decile', 'percentile']):
        reasons.append('socioeconomic_slice')
    if (
        ('% age' in query_lower or 'ages ' in query_lower or 'age ' in query_lower)
        and any(term in query_lower for term in [
            'borrowed',
            'received',
            'can send',
            'can apply',
            'first sexual intercourse',
            'older',
            'laborforce',
            'mobile phone',
            'personal safety',
            'safety concerns',
            'small firms',
        ])
    ):
        reasons.append('survey_micro_slice')
    if any(term in query_lower for term in ['wage gap', 'public to private']) and re.search(r'\baged?\s+\d{1,2}', query_lower):
        reasons.append('gap_subgroup_query')
    if any(term in query_lower for term in ['official flows', 'un agencies', 'unpbf']):
        reasons.append('official_flow_subseries')
    if any(term in query_lower for term in ['bank loan', 'line of credit', 'mobile money account']) and any(term in query_lower for term in ['small firms', 'out of labor force', '% age', 'older']):
        reasons.append('financial_inclusion_slice')
    if any(term in query_lower for term in ['debt securities held by nonresidents', 'held by nonresidents']) and any(term in query_lower for term in ['short term', 'long term', 'total']):
        reasons.append('holder_term_breakdown_query')
    if 'ownership' in query_lower and any(term in query_lower for term in ['private', 'public', 'state']):
        reasons.append('ownership_breakdown_query')
    if 'definition' in query_lower and 'survey' in query_lower:
        reasons.append('definition_survey_query')
    if 'definition' in query_lower and any(term in query_lower for term in ['debt', 'deposits', 'assets', 'liabilities', 'gross value added', 'claims', 'revenue', 'taxes', 'government and public sector finance', 'budgetary central government']):
        reasons.append('definition_financial_query')
    if 'labor markets' in query_lower and 'number of persons' in query_lower:
        reasons.append('classification_labor_query')
    if 'gross value added' in query_lower and any(term in query_lower for term in ['electricity', 'gas', 'water supply']):
        reasons.append('classification_gva_query')
    if any(term in query_lower for term in ['positions', 'resident financial intermediaries', 'openness index', 'reserve money', 'claims', 'liabilities', 'gross external debt position', 'not publicly guranteed', 'not publicly guaranteed']):
        reasons.append('imf_complex_finance_family')
    if provider_upper == 'IMF' and 'sectoral financial' in query_lower and any(term in query_lower for term in ['assets', 'financial derivatives', 'employee stock options']):
        reasons.append('imf_complex_finance_family')
    category_lower = str(origin.get('category') or '').lower()
    # Do not treat every non-WEO IMF catalog category as unsupported. Several
    # IMF DataMapper-backed families in the catalog (for example GDD and
    # high-level fiscal aggregates) are executable through the public runtime.
    # Keep this guard to evidence-backed low-viability code/query families below
    # so certification does not hide runtime-supported IMF rows as
    # supportability blockers.
    if 'current account primary income investment income reserve assets' in query_lower:
        reasons.append('imf_complex_finance_family')
    if 'current account primary income investment income' in query_lower:
        reasons.append('imf_complex_finance_family')
    if 'current account goods net balance of payments goods and services' in query_lower:
        reasons.append('imf_complex_finance_family')
    if 'current account services' in query_lower and 'balance of payments' in query_lower:
        reasons.append('imf_complex_finance_family')
        reasons.append('imf_low_viability_family')
    if 'current account credit' in query_lower and 'balance of payments' in query_lower:
        reasons.append('imf_complex_finance_family')
    if any(term in query_lower for term in ['debt-service payment schedule', 'wages and salaries in kind', 'other postal services', 'equities domestic company', 'financial market prices end of period']) or ('industrial production' in query_lower and 'manufacture of' in query_lower) or ('producer price index' in query_lower and 'weight' in query_lower and 'manufacture of' in query_lower):
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and origin_code_upper.startswith(('TXG_H5_', 'TMG_H5_', 'TX_H5_', 'TM_H5_', 'TXG_SI', 'TMG_SI')):
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and origin_code_upper.startswith('LE_'):
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and re.match(r'^(?:[A-Z]{3}_)?L(?:E|ER|EW|UR|UE|FE|MI|LF|LFPR|PR)(?:_|[A-Z0-9])', origin_code_upper):
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and 'socio demographic indicators' in query_lower and any(term in query_lower for term in ['crude death rate', 'mortality']):
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and 'socio demographic indicators' in query_lower and any(term in query_lower for term in ['fertility', 'total fertility rate']):
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and any(term in query_lower for term in ['socio demographic indicators', 'socio-demographic indicator']) and any(term in query_lower for term in ['population by age', 'population persons', 'youth persons']):
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and 'socio-demographic indicators' in query_lower and 'other changes in volume' in query_lower:
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and origin_code_upper.startswith(('GLR', 'GGR', 'CGR', 'GCR', 'BGR', 'GCGR')) and any(term in query_lower for term in ['fiscal', 'government', 'revenue', 'tax']):
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and origin_code_upper.startswith(('GR_', 'GRG', 'GRK', 'GGRK')) and any(term in query_lower for term in ['government', 'public sector', 'revenue', 'grants', 'fiscal']):
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and ('GRT' in origin_code_upper or origin_code_upper.startswith(('GXRT', 'GXRI'))) and any(term in query_lower for term in ['government and public sector finance', 'central government', 'taxes', 'revenue', 'fiscal']):
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and 'public enterprises' in query_lower and any(term in query_lower for term in ['operation balance', 'fiscal']):
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and 'fiscal' in query_lower and any(term in query_lower for term in ['central government', 'social security central government', 'local government', 'regional government', 'general government']) and any(term in query_lower for term in ['expense', 'revenue', 'tax', 'subsidies', 'social contributions', 'property expense', 'cash inflow', 'financing activities']):
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and 'local government' in query_lower and 'net operating balance' in query_lower:
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and 'general government' in query_lower and 'net operating balance' in query_lower:
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and 'government and public sector finance' in query_lower and any(term in query_lower for term in ['budgetary central government', 'central government']) and any(term in query_lower for term in ['revenue', 'social contributions', 'total financing', 'debt holder']):
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and 'government and public sector finance' in query_lower and 'wealth and debt' in query_lower and 'general government' in query_lower:
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and 'fiscal' in query_lower and 'social contributions' in query_lower and any(term in query_lower for term in ['revenue', 'employee contributions', 'other social contributions']):
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and 'fiscal' in query_lower and any(term in query_lower for term in ['changes in net worth', 'non-interest property expense']):
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and 'fiscal' in query_lower and 'net financial wealth position' in query_lower:
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and 'fiscal' in query_lower and any(term in query_lower for term in ['number of public workers', 'total expenditures -  number']):
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and 'fiscal' in query_lower and 'total expenditures' in query_lower and any(term in query_lower for term in ['wages and salaries', 'mn sur']):
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and any(term in query_lower for term in ['fiscal rule indicator', 'social benefits fiscal']):
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and 'fiscal burden cash fiscal' in query_lower:
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and 'central government' in query_lower and any(term in query_lower for term in ['principle payments', 'tbills', 'lt loans by commercial banks']):
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and 'central government' in query_lower and 'undisbursed balance' in query_lower and 'guarantees' in query_lower:
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and any(term in query_lower for term in ['customs rev', 'excise & fees', 'import duty']) and any(term in query_lower for term in ['final summary', 'revenue']):
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and re.match(r'^F(?:M|O)\d', origin_code_upper):
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and ('merchanting' in query_lower or origin_code_upper.startswith(('BXMGT_', 'BMG_', 'BXS_'))):
        reasons.append('imf_complex_finance_family')
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and origin_code_upper.startswith(('BF', 'BOP_', 'BRN_BOP_', 'DCB_')):
        reasons.append('imf_complex_finance_family')
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and any(term in query_lower for term in ['balance of payments', 'external debt', 'international investment position']):
        reasons.append('imf_complex_finance_family')
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and any(term in query_lower for term in ['international reserves', 'official reserve assets', 'other reserve assets']):
        reasons.append('imf_complex_finance_family')
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and 'definition' in query_lower and any(term in query_lower for term in ['interest rates', 'fiscal', 'government', 'balance of payments']):
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and 'external trade' in query_lower and 'goods' in query_lower:
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and 'external trade' in query_lower and any(term in query_lower for term in ['value of re-exports', 're-exports']):
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and 'industrial production' in query_lower and any(term in query_lower for term in ['manufacturing', 'manufacture']):
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and 'industrial production' in query_lower and any(term in query_lower for term in ['construction', 'economic activity', 'base year', 'business confidence']):
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and 'construction' in query_lower and 'type of good' in query_lower:
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and 'construction' in query_lower and any(term in query_lower for term in ['real reference chained', 'seasonally adjusted industry']):
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and ('memorandum items' in query_lower and 'isic' in origin_code_upper.lower()):
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and 'government and public sector finance' in query_lower and any(term in query_lower for term in ['expenditure', 'expense', 'subsidies']):
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and 'fiscal by functions of government' in query_lower:
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and 'by functions of government' in query_lower and any(term in query_lower for term in ['fiscal', 'central government', 'expenditure']):
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and 'external sector' in query_lower and 'imports of goods and services' in query_lower:
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and 'monetary and financial accounts' in query_lower:
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and 'monetary aggregates' in query_lower and any(term in query_lower for term in ['foreign exchange reserves', ' nbs']):
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and 'monetary aggregates' in query_lower and 'money supply' in query_lower:
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and 'monetary aggregates' in query_lower and 'broad money' in query_lower:
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and any(term in query_lower for term in ["commercial banks' balance sheet", 'commercial banks balance sheet', 'deposits included in broad money']):
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and any(term in query_lower for term in ['monetary microfinance', 'deposit taking institutions', 'balance sheet other items']):
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and any(term in query_lower for term in ['other depository corporations balance sheet', 'gross loans and lease financing']):
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and any(term in query_lower for term in ["credit institutions' balance sheet", 'credit institutions balance sheet', 'nda other items']):
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and any(term in query_lower for term in ['central bank balance sheet', 'monetary gold as sdrs', 'monetary gold and sdrs']):
        reasons.append('imf_complex_finance_family')
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and any(term in query_lower for term in ['monetary other depository corporations survey', 'other depository corporations survey']):
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and 'interest rates' in query_lower:
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and any(term in query_lower for term in ['certificates of deposits', 'percent per annum']):
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and 'exchange rate' in query_lower and any(term in query_lower for term in ['other foreign currency per national currency', 'end of period', 'pound sterling rate', 'real effective exchange rate']):
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and 'exchange rate' in query_lower and 'nominal effective exchange rate' in query_lower:
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and 'exchange rate' in query_lower and any(term in query_lower for term in ['period average', 'official buying rate', 'average rate']):
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and 'us dollars per ounce of gold' in query_lower:
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and 'gross value added' in query_lower and any(term in query_lower for term in ['isic', 'activities', 'activity']):
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and 'gross value added' in query_lower and any(term in query_lower for term in ['previous year prices', 'construction nace2']):
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and 'gross value added' in query_lower and any(term in query_lower for term in ['agriculture nace2', 'forestry and fishing']):
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and 'gross value added' in query_lower and 'fiscal year real' in query_lower:
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and 'gross value added' in query_lower and 'seasonally adjusted' in query_lower:
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and 'economic activity' in query_lower and any(term in query_lower for term in ['oil production', 'foreign direct investment financial and insurance activities', 'tourism arrivals', 'tourist arrivals', 'number of visitors', 'tourism:', 'cruise ship passengers', 'length of stay']):
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and any(term in query_lower for term in ['tourist arrivals', 'tourism arrivals']) and 'traditional countries' in query_lower:
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and 'economic activity' in query_lower and any(term in query_lower for term in ['oil refinery products', 'motor gasoline', 'consumption by product']):
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and 'foreign direct investment approval' in query_lower:
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and 'foreign direct investment' in query_lower and 'approval' not in query_lower and 'financial and insurance activities' not in query_lower:
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and 'remittances' in query_lower and 'income for family remittances' in query_lower:
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and 'economic activity' in query_lower and 'production' in query_lower:
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and any(term in query_lower for term in ['activity real manufacturing', 'mining and quarrying and other industrial activities']):
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and 'other real sector statistics' in query_lower:
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and 'gross value added' in query_lower and '_ISIC' in origin_code_upper:
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and 'ISIC' in origin_code_upper and any(term in query_lower for term in ['transport', 'construction', 'services', 'manufacturing', 'agriculture']):
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and 'production approach' in query_lower and 'gross domestic product' in query_lower and any(term in query_lower for term in ['activity', 'isic', 'manufacturing']):
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and 'gross fixed capital formation' in query_lower and any(term in query_lower for term in ['of which', 'construction', 'previous year prices', 'oil expenditure', 'gross capital formation']):
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and 'gross capital formation' in query_lower and 'change in inventories' in query_lower:
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and 'gross domestic product' in query_lower and 'gross capital formation' in query_lower and 'real expenditure' in query_lower:
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and 'gross domestic product' in query_lower and any(term in query_lower for term in ['real reference chained expenditure', 'external balance of goods and services']):
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and any(term in query_lower for term in ['national income disposable income gross deflator', 'gross deflator']):
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and 'national income' in query_lower and 'consumption of fixed capital' in query_lower:
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and any(term in query_lower for term in ['gdp by activity', 'real reference chained services', 'social and personal service activities', 'public administration human health and social work activities', 'cultivation of tubers']):
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and any(term in query_lower for term in ['other services except government', 'services hotels and restaurants']):
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and 'real services' in query_lower and 'wholesale and retail trade' in query_lower:
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and 'real services' in query_lower and 'from imf' in query_lower:
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and any(term in query_lower for term in ['regional government enterprises', 'government fiscal year nominal gross domestic product']):
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and any(term in query_lower for term in ['total non-monetary gross domestic product', 'real fiscal year total non-monetary']):
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and 'national accounts' in query_lower and any(term in query_lower for term in ['nfc', 'fc', 'hh', 'npish', 'tertiary sector']):
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and 'government consumption expenditure' in query_lower and any(term in query_lower for term in ['donor', 'wages']):
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and any(term in query_lower for term in ['gdp-gnp relation', 'net primary income from abroad', 'taxes on products', 'taxes less subsidies on products', 'statistical discrepancy in gdp']):
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and 'savings and investment memorandum items' in query_lower:
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and 'memorandum items' in query_lower and any(term in query_lower for term in ['gross fixed capital formation', 'real expenditure', 'structures']):
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and 'fiscal' in query_lower and any(term in query_lower for term in ['total outlays', 'revenue grants', 'central government consolidation', 'budgetary central government', 'memo item']):
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and any(term in query_lower for term in [
        'overall primary balance',
        'gross operating balance',
        'cash surplus/deficit',
        'environmental levy',
        'gct (imports)',
        'revenue details',
        'consolidated income and distribution',
        'transfer of funds from special account',
        'deductions federation income and distribution',
    ]):
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and re.search(r'\b(?:labou?r markets?|labor force|labour force)\b', worldbank_text):
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and 'merchandise trade value of exports' in query_lower and 'chapter' in query_lower:
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and 'merchandise trade value of imports' in query_lower and 'chapter' in query_lower:
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and 'external trade by harmonized commodity description and coding systems' in query_lower:
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and any(term in query_lower for term in ['production goods:', 'production goods ']) and any(term in query_lower for term in ['imports', 'exports']):
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and 'external trade' in query_lower and re.search(r'\bhs\b', query_lower):
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and 'by standard international trade classification' in query_lower:
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and 'external trade' in query_lower and re.search(r'\bsitc\b', query_lower):
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and 'merchandise trade' in query_lower and 'central product classification' in query_lower:
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and origin_code_upper.startswith(('TM_HS_', 'TX_HS_', 'TXG_HS', 'TMG_HS', 'TRX_HS_', 'TRM_HS_', 'TXG_CPC', 'TMG_CPC', 'TX_CPC', 'TM_CPC')):
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and origin_code_upper.startswith(('TXGBD', 'TMGBD')):
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and 'merchandise trade' in query_lower and any(term in query_lower for term in ['vanilla', 'definition', ' fob ']):
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and 'merchandise trade' in query_lower and any(term in query_lower for term in ['textiles & fabrics', 'textiles and fabrics']):
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and any(term in query_lower for term in ['manufactured products:', 'manufactured products ']) and any(term in query_lower for term in ['imports', 'exports']):
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and 'dataset:' in query_lower and 'from imf' in query_lower:
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and 'goods total trade external trade' in query_lower:
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and 'manufacture of' in query_lower and 'from imf' in query_lower:
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and 'other manufacturing n.e.c' in query_lower:
        reasons.append('imf_low_viability_family')
    if 'definition central government operations' in query_lower and 'funds for redundant labor' in query_lower:
        reasons.append('definition_financial_query')
    if 'gross value added' in query_lower and 'base year' in query_lower:
        reasons.append('classification_gva_query')
    if any(term in metadata_text for term in ['global jobs indicators database', 'global findex', 'health nutrition and population statistics by wealth quintile']):
        reasons.append('worldbank_niche_catalog_family')
    if any(term in category_lower for term in ['global jobs indicators database', 'global findex', 'global financial inclusion and consumer protection survey', 'health nutrition and population statistics by wealth quintile', 'health equity and financial protection', 'atlas of social protection', 'education statistics', 'wdi database archives', 'statistical performance indicators', 'country climate and development report', 'indonesia database for policy and economic research', 'joint external debt hub', 'fpn datahub archive', 'lac equity lab', 'country partnership strategy', 'disability data hub']):
        reasons.append('worldbank_niche_catalog_family')
    if any(term in category_lower for term in ['quarterly external debt statistics', 'global public procurement', 'pefa', 'doing business']):
        reasons.append('worldbank_specialized_source_family')
    if any(term in category_lower for term in ['quarterly public sector debt', 'exporter dynamics database', 'gender statistics']):
        reasons.append('worldbank_specialized_source_family')
    if any(term in category_lower for term in ['gender disaggregated labor database', 'g20 financial inclusion indicators']):
        reasons.append('worldbank_specialized_source_family')
    worldbank_country_availability_unavailable = False
    if provider_upper == 'WORLDBANK':
        default_query_country_codes = query_country_codes
        # CPIA/WDI policy-assessment indicators are real World Bank catalog rows
        # but are only populated for a constrained country universe.  Default
        # direct-cert countries such as Japan/Germany/US repeatedly produce
        # zero-row API results; keep these as explicit country-availability
        # blockers instead of treating them as runtime framework bugs.
        if origin_code_upper.startswith('IQ.CPA.') and default_query_country_codes.intersection({'US', 'CN', 'BR', 'JP', 'DE'}):
            worldbank_country_availability_unavailable = True
        # Donor-provided ODA indicators are country-role constrained.  Random
        # recipient/emerging-market defaults such as India/China/Brazil are not
        # valid donor-country executions for "provided to" series.
        if (
            origin_code_upper.startswith('DC.ODA.')
            and 'provided' in worldbank_text
            and default_query_country_codes
            and not default_query_country_codes.intersection({
                'AU', 'AT', 'BE', 'CA', 'CZ', 'DK', 'FI', 'FR', 'DE', 'GR',
                'HU', 'IS', 'IE', 'IT', 'JP', 'KR', 'LU', 'NL', 'NZ', 'NO',
                'PL', 'PT', 'SK', 'SI', 'ES', 'SE', 'CH', 'GB', 'US',
            })
        ):
            worldbank_country_availability_unavailable = True
    if worldbank_country_availability_unavailable:
        reasons.append('worldbank_country_availability_surface')
    if any(term in worldbank_text for term in ['rights to inherit assets', 'are other banks permitted', 'commercial banks permitted']):
        reasons.append('worldbank_binary_policy_query')
    if provider_upper == 'WORLDBANK' and (origin_code_upper in {'CC.EST', 'GE.EST', 'PV.EST', 'RQ.EST', 'RL.EST', 'VA.EST'} or 'worldwide governance indicators' in worldbank_text):
        reasons.append('worldbank_specialized_source_family')
    if provider_upper == 'WORLDBANK' and (origin_code_upper.startswith('DC.DAC.') or 'net bilateral aid flows from dac donors' in worldbank_text):
        reasons.append('worldbank_specialized_source_family')
    if any(term in worldbank_text for term in ['contract teachers', 'salary expenditures per teacher', 'off-budget', 'share of tertiary expenditures', "salaries' share of tertiary recurrent expenditures", 'salaries share of tertiary recurrent expenditures', 'pasec', 'pupil/teacher ratio', 'civil service teachers', 'technical/vocational', 'private institution fees', 'egra', 'zero score']):
        reasons.append('worldbank_education_finance_query')
    if provider_upper == 'WORLDBANK' and 'government expenditure' in worldbank_text and 'tertiary education' in worldbank_text and any(term in worldbank_text for term in ['ppp', 'millions']):
        reasons.append('worldbank_education_expenditure_family')
    if provider_upper == 'WORLDBANK' and 'share of schools with double shifts' in worldbank_text:
        reasons.append('worldbank_education_expenditure_family')
    if any(term in worldbank_text for term in ['no functional difficulty', 'youth literacy rate', 'population 25-64 years']) and any(term in worldbank_text for term in ['rural', 'male', 'female', 'both sexes', 'literacy rate']):
        reasons.append('worldbank_demographic_literacy_slice')
    if provider_upper == 'WORLDBANK' and re.search(r'\bages?\s+\d{1,2}\s*(?:-|to)\s*\d{1,2}\b', worldbank_text) and 'population' in worldbank_text and any(term in worldbank_text for term in ['male', 'female', 'total']):
        reasons.append('worldbank_demographic_literacy_slice')
    if any(term in worldbank_text for term in ['seeing difficulty', 'hearing difficulty']) and 'literacy rate' in worldbank_text:
        reasons.append('worldbank_demographic_literacy_slice')
    if 'literacy rate' in worldbank_text and any(term in worldbank_text for term in ['aged 30 to 44 years', 'aged 15 to 29 years']) and 'functional difficulty' in worldbank_text:
        reasons.append('worldbank_demographic_literacy_slice')
    if any(term in worldbank_text for term in ['challenge:', 'without an id', 'formal financial institution', 'smes with at least one female owner']):
        reasons.append('worldbank_id_financial_inclusion_query')
    if 'can be covered using savings seeking help from friends and family' in worldbank_text:
        reasons.append('worldbank_id_financial_inclusion_query')
    if any(term in worldbank_text for term in ['ease of doing business score', 'db15 methodology']):
        reasons.append('worldbank_specialized_source_family')
    if 'reference year' in worldbank_text and 'population age' in worldbank_text and any(term in worldbank_text for term in ['male', 'female']):
        reasons.append('worldbank_demographic_literacy_slice')
    if 'reference year' in worldbank_text and 'population age' in worldbank_text:
        reasons.append('worldbank_demographic_literacy_slice')
    if any(term in worldbank_text for term in ['household spending per student', 'public education expenditure per student', 'share of household consumption for private expenditures']):
        reasons.append('worldbank_education_expenditure_family')
    if any(term in worldbank_text for term in ['teacher salary', 'teachers holding more than one job', 'basic education', 'pre-primary education spending on rural areas', 'pre-primary education spending on urban areas']):
        reasons.append('worldbank_education_expenditure_family')
    if provider_upper == 'WORLDBANK' and origin_code_upper.startswith('PER.OC.GEO.'):
        reasons.append('worldbank_education_expenditure_family')
    if any(term in worldbank_text for term in [
        'public capital expenditure on education',
        'share of total education revenue from school fees',
        'share of total expenditures for goods and services',
        'share of primary education spending on rural areas',
        'share of primary education spending on urban areas',
        'share of tertiary education spending on rural areas',
        'share of tertiary education spending on urban areas',
        'share of secondary education spending on rural areas',
        'share of secondary education spending on urban areas',
        'share of total private spending on education',
        'share of total education expenditures for poor students',
        'share of total household education spending for salaries',
        'share of education spending on rural areas',
        'share of education spending on urban areas',
        'expenditures on females',
        'repetition/drop-out inefficiency cost',
        'capital education budget execution rate',
        'school feeding programs',
        'share of subnational education expenditures for salaries',
        'subnational government share of salaries',
        'share of junior secondary expenditures for administration',
        'education budget execution rate pre-primary',
        'share of secondary schools privately managed',
        'recurrent education budget execution rate',
        'utilities (% of recurrent education expenditure)',
        'wastage index (%) recurrent education expenditures',
        'pupils/class',
    ]):
        reasons.append('worldbank_education_expenditure_family')
    if any(term in worldbank_text for term in ['national assessment for learning outcomes', 'optimal competency', 'sea-plm', 'above proficiency', 'piaac']):
        reasons.append('worldbank_assessment_family')
    if provider_upper == 'WORLDBANK' and any(term in worldbank_text for term in ['national learning goals', 'global education policy dashboard']):
        reasons.append('worldbank_assessment_family')
    if provider_upper == 'WORLDBANK' and (origin_code_upper.startswith('HH.DHS.') or 'demographic and health surveys' in worldbank_text):
        reasons.append('worldbank_education_expenditure_family')
    if provider_upper == 'WORLDBANK' and ('adjusted wealth parity index' in worldbank_text or origin_code_upper.endswith('.WPIA')):
        reasons.append('worldbank_demographic_literacy_slice')
    if 'current allocation - modality' in worldbank_text:
        reasons.append('worldbank_specialized_source_family')
    if any(term in worldbank_text for term in ['public sector wage premium', 'price level ratio of ppp conversion factor', 'elevation is below 5 meters']):
        reasons.append('worldbank_macro_exposure_family')
    if any(term in worldbank_text for term in ['food insecure households', 'adjusted prevalence', 'selfcare difficulty', 'mobility difficulty']):
        reasons.append('worldbank_ddh_prevalence_family')
    if provider_upper == 'WORLDBANK' and 'disability data hub' in category_lower and any(term in worldbank_text for term in ['mobile phone', 'mobility phone', 'persons owing', 'persons in households']):
        reasons.append('worldbank_ddh_prevalence_family')
    if any(term in query_lower for term in ['international poverty line', 'household formality', 'inventory of energy subsidies', 'support measures', "africa's development dynamics", 'afdd', 'table 36', 'incidence of full-time and part-time employment', 'harmonized definition', 'analysis by armed group', 'west africa', 'real labour productivity', 'taking up work when claiming unemployment benefits', 'temporary employment by permanency of the job']):
        reasons.append('oecd_low_viability_family')
    if provider_upper == 'OECD' and _metadata_annotation_is_true(parsed_metadata, 'NonProductionDataflow'):
        reasons.append('oecd_non_production_dataflow')
    if provider_upper == 'OECD' and any(term in worldbank_text for term in ['key indicators of informality', 'formal to informal employees', 'informal employment']):
        reasons.append('oecd_low_viability_family')
    if provider_upper == 'OECD' and (origin_code_upper.startswith('OECD_DSD_TEC') or 'trade in goods by enterprise characteristics' in worldbank_text):
        reasons.append('oecd_low_viability_family')
    if provider_upper == 'OECD' and 'instruction time per subject' in worldbank_text:
        reasons.append('oecd_low_viability_family')
    if provider == 'OECD' and 'population in the national accounts' in query_lower and 'distributions by' in query_lower:
        reasons.append('oecd_low_viability_family')
    if provider == 'OECD' and any(term in query_lower for term in [
        'share of new entrants and graduates in each field of education by gender',
        'effective tax rates on taking up work when claiming minimum income benefits',
        'employment rates of adults by educational attainment age group and gender',
        'share of young adults who are not employment nor in formal education or training',
        'earners distribution based on their level of earnings relative to the overall median',
        'earnings of workers relative to the earnings of',
        'inactivity rates of adults by educational attainment age group and gender',
        'share of enrolled students new entrants and graduates by gender',
        'share of mobile students enrolled at tertiary level by country of origin',
        "teachers' actual salary relative to earnings of tertiary-educated workers",
        "adults' gender distribution by educational attainment level and age group",
        'minimum relative to average wages of full-time workers',
        'tax and non-tax revenues',
        'sustainable development goal',
        'instruction time in compulsory general education',
        'net childcare cost for parents',
        'number of students and repeaters',
        'number of mobile students enrolled and graduated',
        'revenue statistics in asia and pacific',
        'reference series',
        "teachers' actual salaries relative to workers' earnings",
        'quarterly employment by institutional sector',
        'national and regional house price indices',
        'number of national tertiary students enrolled abroad',
        'annual financial accounts (flows)',
    ]):
        reasons.append('oecd_low_viability_family')
    if any(term in query_lower for term in ['memorandum items', 'producer price index', 'consumer price index']) and any(term in query_lower for term in ['definition', 'organic acids', 'food and non-alcoholic beverages', 'cash government and public sector finance', 'cash, national currency', 'gross value added', 'previous year prices', 'base year']):
        reasons.append('imf_price_or_memorandum_family')
    if 'weight' in query_lower and 'consumer prices' in query_lower and 'from imf' in query_lower:
        reasons.append('imf_price_or_memorandum_family')
    if provider_upper == 'IMF' and 'weight' in query_lower and 'consumer price' in query_lower:
        reasons.append('imf_price_or_memorandum_family')
    if provider_upper == 'IMF' and any(term in query_lower for term in ['consumer price index excluding', 'harmonized consumer prices']):
        reasons.append('imf_price_or_memorandum_family')
    if provider_upper == 'IMF' and 'consumer prices' in query_lower and any(term in query_lower for term in ['base year previous period', 'all items special indexes', 'capital city']):
        reasons.append('imf_price_or_memorandum_family')
    if provider_upper == 'IMF' and any(term in query_lower for term in ['harmonized overlap', 'housing gas and other fuels']):
        reasons.append('imf_price_or_memorandum_family')
    if 'consumer prices expenditure of households' in query_lower:
        reasons.append('imf_price_or_memorandum_family')
    if provider_upper == 'IMF' and 'poverty and income distribution indicators' in query_lower:
        reasons.append('imf_low_viability_family')
    if 'all commodities producer price index' in query_lower:
        reasons.append('imf_price_or_memorandum_family')
    if provider_upper == 'IMF' and 'producer price index' in query_lower and any(term in query_lower for term in ['commodities by activity', 'other manufacturing', 'isic rev 4', 'goods', 'mining of coal', 'extraction of peat', 'all commodities', 'production of', 'manufacturing nace2', 'nace2 producer price index', 'extraction of coal and lignite', 'water collection']):
        reasons.append('imf_price_or_memorandum_family')
    if provider_upper == 'IMF' and 'share price index' in query_lower and 'definition' in query_lower:
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and 'stock market' in query_lower and 'definition' in query_lower:
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and 'financial market prices' in query_lower and any(term in query_lower for term in ['primary market instruments', 'equities']):
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and any(term in query_lower for term in ['pension funds assets', 'financial derivatives and employee stock options', 'sectoral financial', 'banking system indicators', 'sectoral distribution of credit', 'sectoral accounts rest of the world', 'currency and deposits', 'liquid assets to total assets']):
        reasons.append('imf_complex_finance_family')
    if provider_upper == 'IMF' and 'assets loans sectoral' in query_lower and any(term in query_lower for term in ['households', 'npish']):
        reasons.append('imf_complex_finance_family')
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and 'financial derivatives' in query_lower and any(term in query_lower for term in ['central bank survey', 'other financial corporations']):
        reasons.append('imf_complex_finance_family')
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and query_lower.strip().endswith('financial derivatives from imf'):
        reasons.append('imf_complex_finance_family')
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and 'sectoral equity and investment fund shares' in query_lower:
        reasons.append('imf_complex_finance_family')
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and any(term in query_lower for term in ['insurance sectoral pension and standardized guarantee schemes', 'central bank assets insurance sectoral']):
        reasons.append('imf_complex_finance_family')
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and any(term in query_lower for term in ['portfolio investment', 'debt securities', ' iip ', 'international investment position']):
        reasons.append('imf_complex_finance_family')
    if provider_upper == 'IMF' and 'loans of banks by sectors' in query_lower:
        reasons.append('imf_complex_finance_family')
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and 'financial corporations financial institutions' in query_lower:
        reasons.append('imf_complex_finance_family')
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and any(term in query_lower for term in ['non-market education', 'market education']):
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and any(term in query_lower for term in ['financial transactions.esa', 'financial transactions esa', 'net acquisition of financial assets']):
        reasons.append('imf_complex_finance_family')
        reasons.append('imf_low_viability_family')
    if provider_upper == 'IMF' and 'definition' in query_lower and any(term in query_lower for term in ['gdp by branches', 'branches of origin', 'crop production by region', 'other real sector indicators']):
        reasons.append('imf_low_viability_family')
    if 'revenue other revenue' in query_lower:
        reasons.append('definition_financial_query')
    if 'taxes income profits government and public sector finance' in query_lower:
        reasons.append('definition_financial_query')
    if any(term in query_lower for term in ['wine-grape vine varieties', 'vine variety', 'age of the vines']):
        reasons.append('eurostat_agri_breakdown_query')
    if any(term in query_lower for term in ['activity limitation', 'poverty threshold', 'previous year']) and any(term in query_lower for term in ['sex', 'age', 'most frequent activity']):
        reasons.append('eurostat_cross_tab_query')
    if provider == 'Eurostat' and any(term in query_lower for term in ['household composition', 'degree of urbanisation']) and any(term in origin_name_lower for term in ['participating', 'active citizenship', 'voluntary activities']):
        reasons.append('eurostat_dimension_fragment_query')
    if provider == 'Eurostat' and all(term in query_lower for term in ['household composition', 'degree of urbanisation', 'frequency']):
        reasons.append('eurostat_dimension_fragment_query')
    if provider == 'Eurostat' and all(term in query_lower for term in ['degree of urbanisation', 'household composition']):
        reasons.append('eurostat_dimension_fragment_query')
    if provider == 'Eurostat' and 'purchasing power parities price level indices' in query_lower:
        reasons.append('eurostat_cross_tab_query')
    if provider == 'Eurostat' and 'full-time/part-time employment and economic activity' in query_lower:
        reasons.append('eurostat_cross_tab_query')
    if provider == 'Eurostat' and 'average actual weekly hours worked' in query_lower and 'professional status' in query_lower:
        reasons.append('eurostat_cross_tab_query')
    if provider == 'Eurostat' and 'not employed persons who would have stayed longer at work' in query_lower:
        reasons.append('eurostat_cross_tab_query')
    if provider == 'Eurostat' and 'ready to have more than one hour travel time each way to work' in query_lower:
        reasons.append('eurostat_cross_tab_query')
    if provider == 'Eurostat' and 'self-reported last colonoscopy' in query_lower:
        reasons.append('eurostat_cross_tab_query')
    if provider == 'Eurostat' and 'self-perceived health by sex age and degree of urbanisation' in query_lower:
        reasons.append('eurostat_cross_tab_query')
    if provider == 'Eurostat' and 'performing (non-work-related) physical activities by sex' in query_lower:
        reasons.append('eurostat_cross_tab_query')
    if provider == 'Eurostat' and 'eu direct investments indicators in % of gdp' in query_lower:
        reasons.append('eurostat_cross_tab_query')
    if provider == 'Eurostat' and 'functional limitations by sex age and degree of urbanisation' in query_lower:
        reasons.append('eurostat_cross_tab_query')
    if provider == 'Eurostat' and 'daily consumption of fruit and vegetables by sex' in query_lower:
        reasons.append('eurostat_cross_tab_query')
    if provider == 'Eurostat' and 'performing health-enhancing physical activity by sex' in query_lower:
        reasons.append('eurostat_cross_tab_query')
    if provider == 'Eurostat' and 'time spent in the main activity by sex and household composition' in query_lower:
        reasons.append('eurostat_cross_tab_query')
    if provider == 'Eurostat' and 'former daily tobacco smokers by sex' in query_lower:
        reasons.append('eurostat_cross_tab_query')
    if provider == 'Eurostat' and any(term in query_lower for term in ['infant deaths occurring in eu by cause and age', 'infant deaths occurring in the eu by cause and age']):
        reasons.append('eurostat_cross_tab_query')
    if provider == 'Eurostat' and 'deaths by week' in query_lower and any(term in query_lower for term in ['nuts2', '5-year age group', 'sex']):
        reasons.append('eurostat_cross_tab_query')
    if provider == 'Eurostat' and 'mean hourly earnings by sex age and economic activity' in query_lower:
        reasons.append('eurostat_cross_tab_query')
    if provider == 'Eurostat' and 'mean annual earnings' in query_lower and any(term in query_lower for term in ['size of the enterprise', 'sex occupation']):
        reasons.append('eurostat_cross_tab_query')
    if provider == 'Eurostat' and 'early leavers from education and training by sex and nuts' in query_lower:
        reasons.append('eurostat_cross_tab_query')
    if provider == 'Eurostat' and any(term in query_lower for term in ['gross weight of goods handled in main ports', 'gross weight of goods transported to/from main ports']):
        reasons.append('eurostat_transport_port_query')
    if provider == 'Eurostat' and 'wood in the rough over bark' in query_lower:
        reasons.append('eurostat_forestry_material_flow_query')
    if re.search(r'^US\s+[A-Z]{2}\b', query) and re.search(r'\b(county|cbsa|msa|metro)\b', query_lower):
        reasons.append('subnational_abbrev_ambiguous')
    if any(marker in query_lower or marker in origin_name_lower for marker in ['sub total', 'exchange difference']):
        reasons.append('accounting_artifact_query')
    if re.search(r'\b(scenario|scenarios|projection|projections|forecast|forecasts|fua|fuas)\b', query_lower):
        reasons.append('scenario_projection_query')
    if re.search(r'total revenue for\s+\d{4}', query_lower) or any(term in query_lower for term in ['net equity in life insurance and pension funds', 'asset (ima)', 'employer firms total revenue', 'total expense for', 'establishments subject to federal income tax', 'defined benefit retirement funds', 'commercial paper; asset']):
        reasons.append('fred_low_viability_family')
    if provider_upper == 'FRED' and any(term in query_lower for term in ['fiscal situation of general government', 'high school graduate or higher (5-year estimate)']):
        reasons.append('fred_low_viability_family')
    fred_catalog_text = f"{query_lower} {origin_name_lower} {origin_code_upper.lower()}"
    if provider_upper == 'FRED' and (
        'hicp' in fred_catalog_text
        or origin_code_upper.startswith('HICP')
    ):
        reasons.append('fred_hicp_catalog_family')
    if provider == 'OECD' and 'share of students enrolled in school and work-based programmes' in query_lower:
        reasons.append('oecd_education_programme_share_query')
    if provider == 'CoinGecko' and re.search(r'\b[a-z0-9]+_[a-z0-9_]+\b', query):
        reasons.append('coin_slug_query')
    if provider == 'CoinGecko' and '[old]' in query_lower:
        reasons.append('coin_slug_query')
    if provider == 'CoinGecko' and any(term in query_lower for term in ['dagknight dog']):
        reasons.append('coin_low_viability_family')
    methodology_markers = [
        'ppp',
        'ppps',
        'current prices',
        'constant prices',
        'seasonally adjusted',
        'annual',
        'quarterly',
        'per capita',
        'definition',
        'survey',
        'national currency',
        'de facto',
        'sub total',
        'exchange difference',
    ]
    methodology_hits = sum(
        1 for marker in methodology_markers if marker in query_lower or marker in origin_name_lower
    )
    if methodology_hits >= 3:
        reasons.append('methodology_dense')
    if origin_name.count(',') >= 3:
        reasons.append('multi_modifier_title')

    exact_imf_code_query = (
        provider_upper == 'IMF'
        and origin_code_upper
        and re.fullmatch(
            rf'{re.escape(origin_code_upper)}\s+from\s+IMF',
            query.strip(),
            flags=re.IGNORECASE,
        )
        is not None
    )
    if exact_imf_code_query and imf_public_sdmx_runtime_family(
        origin_code_upper,
        origin_name,
        str(origin.get('category') or ''),
    ):
        # Exact provider-code probes for public IMF SDMX families are already
        # treated as supportable by the runtime support matrix.  Do not let the
        # long human catalog title reclassify the code-only query as an
        # execution-high-risk natural-language surface.
        reasons = [
            reason for reason in reasons
            if reason not in {
                'imf_complex_finance_family',
                'imf_low_viability_family',
                'imf_query_only_public_surface_family',
                'methodology_dense',
            }
        ]
    if exact_imf_code_query and evaluation_target == CERTIFICATION_TARGET_USER_ANSWERABILITY:
        # Exact unsupported IMF probe rows are mechanical supportability
        # diagnostics.  Keep the long catalog title in origin/provenance, but
        # do not let title-density flags turn the code-only probe into a prompt
        # shape failure before the fail-closed supportability path can run.
        reasons = [
            reason for reason in reasons
            if reason not in {
                'methodology_dense',
                'multi_modifier_title',
            }
        ]
    exact_imf_supported_cpi_title_query = (
        provider_upper == 'IMF'
        and origin_name
        and imf_public_sdmx_runtime_family(
            origin_code_upper,
            origin_name,
            str(origin.get('category') or row.get('category') or ''),
        ) == 'cpi_aggregate'
        and _normal_title_key(origin_name) in _normal_title_key(query)
        and re.search(r'\bfrom\s+IMF$', query.strip(), flags=re.IGNORECASE) is not None
    )
    if exact_imf_supported_cpi_title_query and evaluation_target == CERTIFICATION_TARGET_USER_ANSWERABILITY:
        # Supported IMF aggregate CPI titles need their provider-native
        # COICOP/base-year/index qualifiers to remain answerable through the
        # strict exact-title path.  This is not a code/concept shortcut: the
        # prompt carries the literal public IMF title, and runtime/adjudication
        # still decide whether the provider returns the requested series.
        reasons = [
            reason for reason in reasons
            if reason not in {
                'very_long_query',
                'long_query',
                'punctuation_dense',
                'acronym_dense',
                'provider_title_like',
                'country_scope_conflict',
                'methodology_dense',
                'multi_modifier_title',
            }
        ]
    exact_worldbank_code_query = (
        provider_upper == 'WORLDBANK'
        and origin_code_upper
        and re.search(
            rf'(^|\s){re.escape(str(origin.get("source_indicator_code") or row.get("code") or "").strip())}\s+from\s+World\s+Bank$',
            query.strip(),
            flags=re.IGNORECASE,
        )
        is not None
    )
    if exact_worldbank_code_query:
        # Exact provider-code probes are mechanical WorldBank catalog requests.
        # Do not let the human title/category metadata reclassify the code-only
        # probe as an execution-high-risk natural-language surface. Runtime
        # data availability/supportability remains measured by the replay.
        reasons = [
            reason for reason in reasons
            if not reason.startswith('worldbank_')
            and reason not in {
                'acronym_dense',
                'definition_financial_query',
                'definition_survey_query',
                'methodology_dense',
                'multi_modifier_title',
            }
        ]
    exact_worldbank_title_query = (
        provider_upper == 'WORLDBANK'
        and origin_name
        and _normal_title_key(origin_name) in _normal_title_key(query)
        and re.search(r'\bfrom\s+World\s+Bank$', query.strip(), flags=re.IGNORECASE) is not None
    )
    if exact_worldbank_title_query and evaluation_target == CERTIFICATION_TARGET_USER_ANSWERABILITY:
        # A provider-native WorldBank title is a valid user-answerability probe
        # when the prompt itself carries the title.  Length/subgroup words are
        # not final semantic authority; runtime replay and adjudication must
        # decide whether the public provider returns the requested series.
        reasons = [
            reason for reason in reasons
            if reason not in {
                'very_long_query',
                'long_query',
                'provider_title_like',
                'education_subgroup_slice',
                'micro_demographic_slice',
                'multi_modifier_title',
            }
        ]
    exact_comtrade_hs_code_query = (
        provider_upper == 'COMTRADE'
        and origin_code_upper
        and re.fullmatch(r'\d{2,6}', origin_code_upper)
        and re.search(
            rf'\bHS\s*{re.escape(origin_code_upper)}\b',
            query,
            flags=re.IGNORECASE,
        )
        is not None
        and re.search(r'\bfrom\s+Comtrade$', query.strip(), flags=re.IGNORECASE) is not None
    )
    if exact_comtrade_hs_code_query and evaluation_target == CERTIFICATION_TARGET_USER_ANSWERABILITY:
        # HS codes are the public provider-native language for Comtrade
        # commodity surfaces.  A prompt carrying the exact HS code may also
        # carry a long commodity label for human readability; do not preblock
        # that mechanical exact-code probe only because the label is verbose.
        reasons = [
            reason for reason in reasons
            if reason not in {
                'very_long_query',
                'long_query',
                'punctuation_dense',
                'provider_title_like',
            }
        ]
    if (
        provider_upper == 'WORLDBANK'
        and evaluation_target == CERTIFICATION_TARGET_USER_ANSWERABILITY
        and not query_mentions_country(query)
        and not exact_worldbank_code_query
        and not exact_worldbank_title_query
        and (row.get('scope_family') or infer_scope_family('WorldBank', str(origin.get('coverage') or row.get('coverage') or ''))) == 'single_country'
    ):
        # A countryless WorldBank prompt is only safe claim evidence when the
        # user supplied an exact public title/code, which locks runtime to the
        # provider-native all-country surface.  Collapsed token snippets such as
        # "number people pushed further 2017 ppp" reliably trigger clarification
        # and should be regenerated or excluded by the validation framework
        # rather than repaired by runtime country guessing.
        reasons.append('worldbank_countryless_single_country_query')
    exact_fred_title_query = (
        provider_upper == 'FRED'
        and origin_name
        and (
            _normal_title_key(origin_name) in _normal_title_key(query)
            or _title_token_coverage(origin_name, query) >= 0.90
        )
        and re.search(r'\bfrom\s+FRED$', query.strip(), flags=re.IGNORECASE) is not None
    )
    if exact_fred_title_query and evaluation_target == CERTIFICATION_TARGET_USER_ANSWERABILITY:
        # FRED publishes many provider-native titles that are long because they
        # encode accounting sector/instrument/cadence.  If the user prompt
        # carries that exact public title, prompt length/acronyms are not final
        # semantic authority; runtime plus adjudication owns correctness.
        reasons = [
            reason for reason in reasons
            if reason not in {
                'very_long_query',
                'long_query',
                'punctuation_dense',
                'acronym_dense',
                'provider_title_like',
                'country_scope_conflict',
                'multi_modifier_title',
            }
        ]
    exact_eurostat_title_query = (
        provider_upper == 'EUROSTAT'
        and origin_name
        and _normal_title_key(origin_name) in _normal_title_key(query)
        and re.search(r'\bfrom\s+Eurostat$', query.strip(), flags=re.IGNORECASE) is not None
    )
    if exact_eurostat_title_query and evaluation_target == CERTIFICATION_TARGET_USER_ANSWERABILITY:
        # Eurostat title families are densely qualified and many valid
        # dataflows only differ in late title modifiers.  In the
        # user-answerability lane, an exact provider-native title is the
        # evidence the user supplied, not a rule-based semantic judgment.  Do
        # not preblock solely because the exact public title is long,
        # punctuation-heavy, acronym-heavy, scope-word-heavy, or has many
        # modifiers.
        reasons = [
            reason for reason in reasons
            if reason not in {
                'very_long_query',
                'long_query',
                'punctuation_dense',
                'acronym_dense',
                'provider_title_like',
                'country_scope_conflict',
                'micro_demographic_slice',
                'education_subgroup_slice',
                'socioeconomic_slice',
                'survey_micro_slice',
                'multi_modifier_title',
            }
        ]
    exact_oecd_code_query = (
        provider_upper == 'OECD'
        and origin_code_upper
        and re.search(
            rf'(^|\s){re.escape(str(origin.get("source_indicator_code") or row.get("code") or "").strip())}\s+from\s+OECD$',
            query.strip(),
            flags=re.IGNORECASE,
        )
        is not None
    )
    if exact_oecd_code_query:
        # Exact provider-code probes are mechanical OECD dataflow requests.
        # Runtime availability/supportability remains measured by replay; the
        # audit should not reclassify a code-only probe using the human title's
        # country words or low-viability semantic family hints.
        reasons = [
            reason for reason in reasons
            if not reason.startswith('oecd_')
            and reason not in {
                'acronym_dense',
                'country_scope_conflict',
                'methodology_dense',
                'multi_modifier_title',
            }
        ]
    exact_oecd_title_query = (
        provider_upper == 'OECD'
        and origin_name
        and (
            _normal_title_key(origin_name) in _normal_title_key(query)
            or _title_token_coverage(origin_name, query) >= 0.90
        )
        and re.search(r'\bfrom\s+OECD$', query.strip(), flags=re.IGNORECASE) is not None
    )
    if exact_oecd_title_query and evaluation_target == CERTIFICATION_TARGET_USER_ANSWERABILITY:
        # OECD public dataflow titles are often long and dimension-rich.  Treat
        # exact title prompts as user-supplied provider-native evidence and let
        # runtime/adjudication decide the result instead of preblocking on
        # generic length/acronym/subgroup heuristics.
        reasons = [
            reason for reason in reasons
            if reason not in {
                'very_long_query',
                'long_query',
                'punctuation_dense',
                'acronym_dense',
                'provider_title_like',
                'country_scope_conflict',
                'micro_demographic_slice',
                'education_subgroup_slice',
                'socioeconomic_slice',
                'survey_micro_slice',
                'multi_modifier_title',
            }
        ]
    statscan_product_id = ''.join(
        ch for ch in str(origin.get('source_indicator_code') or row.get('code') or '')
        if ch.isdigit()
    )
    exact_statscan_product_query = (
        provider_upper == 'STATSCAN'
        and re.fullmatch(r'\d{8}|\d{10}', statscan_product_id)
        and re.search(
            rf'^\s*{re.escape(statscan_product_id[:8])}\b.*\s+from\s+(?:StatsCan|Statistics\s+Canada)\s*$',
            query.strip(),
            flags=re.IGNORECASE,
        )
        is not None
    )
    if exact_statscan_product_query:
        # Exact StatsCan product/table probes are mechanical catalog requests.
        # Keep title text in the query so runtime can extract dimensions, but
        # do not let the title's length or subgroup words block execution as a
        # natural-language high-risk surface. Runtime supportability/adjudication
        # still owns the semantic decision.
        reasons = [
            reason for reason in reasons
            if reason not in {
                'very_long_query',
                'long_query',
                'education_subgroup_slice',
                'socioeconomic_slice',
                'multi_modifier_title',
            }
        ]
    exact_statscan_title_query = (
        provider_upper in {'STATSCAN', 'STATISTICS CANADA'}
        and origin_name
        and _normal_title_key(origin_name) in _normal_title_key(query)
        and re.search(r'\bfrom\s+(?:StatsCan|Statistics\s+Canada)$', query.strip(), flags=re.IGNORECASE) is not None
    )
    if exact_statscan_title_query and evaluation_target == CERTIFICATION_TARGET_USER_ANSWERABILITY:
        # Statistics Canada table titles are often long, survey/dimension-rich
        # public labels.  In user-answerability certification, an exact public
        # table title should go through runtime + adjudication rather than being
        # preblocked by generic prompt-shape heuristics.
        reasons = [
            reason for reason in reasons
            if reason not in {
                'very_long_query',
                'long_query',
                'punctuation_dense',
                'acronym_dense',
                'provider_title_like',
                'country_scope_conflict',
                'micro_demographic_slice',
                'education_subgroup_slice',
                'socioeconomic_slice',
                'survey_micro_slice',
                'multi_modifier_title',
            }
        ]
    if evaluation_target == CERTIFICATION_TARGET_USER_ANSWERABILITY:
        # Provider/family supportability hints were built for frozen catalog
        # replay.  In the user-answerability lane they must not become a
        # pre-runtime judgment that a real user prompt is invalid.  Keep generic
        # prompt-quality flags such as long/micro/conflicting-scope, and let the
        # live answer path plus adjudication decide provider outcome.
        reasons = [
            reason
            for reason in reasons
            if reason not in USER_ANSWERABILITY_INVENTORY_ONLY_RISK_REASONS
        ]
    reasons = list(dict.fromkeys(reasons))

    risk_level = 'low'
    if any(reason in reasons for reason in [
        'very_long_query',
        'catalog_jargon',
        'provider_title_like',
        'indicator_code_prefix',
        'opaque_acronym_query',
        'country_scope_conflict',
        'micro_demographic_slice',
        'education_subgroup_slice',
        'socioeconomic_slice',
        'survey_micro_slice',
        'gap_subgroup_query',
        'official_flow_subseries',
        'financial_inclusion_slice',
        'holder_term_breakdown_query',
        'ownership_breakdown_query',
        'definition_survey_query',
        'definition_financial_query',
        'classification_labor_query',
        'classification_gva_query',
        'imf_complex_finance_family',
        'imf_low_viability_family',
        'worldbank_niche_catalog_family',
        'worldbank_specialized_source_family',
        'worldbank_binary_policy_query',
        'worldbank_education_finance_query',
        'worldbank_demographic_literacy_slice',
        'worldbank_id_financial_inclusion_query',
        'worldbank_education_expenditure_family',
        'worldbank_assessment_family',
        'worldbank_macro_exposure_family',
        'worldbank_ddh_prevalence_family',
        'worldbank_country_availability_surface',
        'worldbank_countryless_single_country_query',
        'oecd_low_viability_family',
        'oecd_education_programme_share_query',
        'imf_query_only_public_surface_family',
        'imf_price_or_memorandum_family',
        'eurostat_agri_breakdown_query',
        'eurostat_cross_tab_query',
        'eurostat_dimension_fragment_query',
        'eurostat_transport_port_query',
        'eurostat_forestry_material_flow_query',
        'subnational_abbrev_ambiguous',
        'accounting_artifact_query',
        'scenario_projection_query',
        'fred_low_viability_family',
        'fred_hicp_catalog_family',
        'coin_slug_query',
        'coin_low_viability_family',
        'methodology_dense',
    ]):
        risk_level = 'high'
    elif reasons:
        risk_level = 'medium'

    return {
        'risk_level': risk_level,
        'reasons': reasons,
        'evaluation_target': evaluation_target,
        'query_length': len(query),
        'punctuation_hits': punctuation_hits,
    }
