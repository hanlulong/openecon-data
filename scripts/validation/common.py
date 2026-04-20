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

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = ROOT / 'backend' / 'data' / 'indicators.db'
VALIDATION_PRIVATE = ROOT / 'validation_private'
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
    'Comtrade': ['United States', 'China', 'Germany', 'France', 'Japan'],
    'Eurostat': ['France', 'Germany', 'Italy', 'Spain'],
    'StatsCan': ['Canada'],
    'OECD': ['United States', 'Japan', 'Germany', 'Canada'],
    'BIS': ['United States', 'China', 'Japan'],
    'ExchangeRate': ['USD to EUR', 'USD to GBP', 'USD to JPY'],
}

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
    'can',
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
        and CountryResolver.normalize(str(alias).strip())
        and (len(str(alias).strip()) > 2 or str(alias).strip().lower() in _SAFE_SHORT_COUNTRY_ALIASES)
    ]
else:
    _COUNTRY_ALIAS_PATTERN_ENTRIES = []


def detect_country_codes_in_text(text: str) -> set[str]:
    if CountryResolver is None:
        return set()
    lowered = str(text or '').lower()
    if not lowered:
        return set()
    codes: set[str] = set()
    for pattern, normalized, _alias_text in _COUNTRY_ALIAS_PATTERN_ENTRIES:
        if pattern.search(lowered):
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
        if pattern.search(lowered):
            matches.append((len(alias_text), normalized, alias_text))
    codes = {code for _, code, _ in matches}
    if len(codes) != 1 or not matches:
        return None
    best_alias = max(matches)[2]
    return best_alias.title() if len(best_alias) > 2 else best_alias.upper()


def slugify(text: str) -> str:
    return re.sub(r'[^a-z0-9]+', '-', text.lower()).strip('-') or 'item'


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


def _load_empirical_category_priors() -> dict[tuple[str, str], tuple[int, int]]:
    global _EMPIRICAL_CATEGORY_PRIORS
    if _EMPIRICAL_CATEGORY_PRIORS is not None:
        return _EMPIRICAL_CATEGORY_PRIORS

    priors: dict[tuple[str, str], list[int]] = {}
    reports_dir = VALIDATION_PRIVATE / 'reports'
    datasets_dir = VALIDATION_PRIVATE / 'datasets' / 'batch_review'
    for report_path in sorted(reports_dir.glob('next200_v*_weak_provider_viability_fast20.json')):
        stem = report_path.stem.replace('_weak_provider_viability_fast20', '')
        dataset_path = datasets_dir / stem / 'next_batch_direct_weak_providers.jsonl'
        if not dataset_path.exists():
            continue
        try:
            dataset_rows = {}
            for line in dataset_path.read_text(encoding='utf-8').splitlines():
                if line.strip():
                    row = json.loads(line)
                    dataset_rows[str(row.get('id') or '')] = row
            report = json.loads(report_path.read_text(encoding='utf-8'))
        except Exception:
            continue

        for result in report.get('results', []):
            row = dataset_rows.get(str(result.get('session_id') or ''))
            if not row:
                continue
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
    reports_dir = VALIDATION_PRIVATE / 'reports'
    datasets_dir = VALIDATION_PRIVATE / 'datasets' / 'batch_review'
    for report_path in sorted(reports_dir.glob('next200_v*_weak_provider_viability_fast20.json')):
        stem = report_path.stem.replace('_weak_provider_viability_fast20', '')
        dataset_path = datasets_dir / stem / 'next_batch_direct_weak_providers.jsonl'
        if not dataset_path.exists():
            continue
        try:
            dataset_rows = {}
            for line in dataset_path.read_text(encoding='utf-8').splitlines():
                if line.strip():
                    row = json.loads(line)
                    dataset_rows[str(row.get('id') or '')] = row
            report = json.loads(report_path.read_text(encoding='utf-8'))
        except Exception:
            continue

        for result in report.get('results', []):
            row = dataset_rows.get(str(result.get('session_id') or ''))
            if not row:
                continue
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
    reports_dir = VALIDATION_PRIVATE / 'reports'
    datasets_dir = VALIDATION_PRIVATE / 'datasets' / 'batch_review'
    for report_path in sorted(reports_dir.glob('next200_v*_weak_provider_viability_fast20.json')):
        stem = report_path.stem.replace('_weak_provider_viability_fast20', '')
        dataset_path = datasets_dir / stem / 'next_batch_direct_weak_providers.jsonl'
        if not dataset_path.exists():
            continue
        try:
            dataset_rows = {}
            for line in dataset_path.read_text(encoding='utf-8').splitlines():
                if line.strip():
                    row = json.loads(line)
                    dataset_rows[str(row.get('id') or '')] = row
            report = json.loads(report_path.read_text(encoding='utf-8'))
        except Exception:
            continue

        for result in report.get('results', []):
            row = dataset_rows.get(str(result.get('session_id') or ''))
            if not row:
                continue
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
    reports_dir = VALIDATION_PRIVATE / 'reports'
    datasets_dir = VALIDATION_PRIVATE / 'datasets' / 'batch_review'
    for report_path in sorted(reports_dir.glob('next200_v*_weak_provider_viability_fast20.json')):
        stem = report_path.stem.replace('_weak_provider_viability_fast20', '')
        dataset_path = datasets_dir / stem / 'next_batch_direct_weak_providers.jsonl'
        if not dataset_path.exists():
            continue
        try:
            dataset_rows = {}
            for line in dataset_path.read_text(encoding='utf-8').splitlines():
                if line.strip():
                    row = json.loads(line)
                    dataset_rows[str(row.get('id') or '')] = row
            report = json.loads(report_path.read_text(encoding='utf-8'))
        except Exception:
            continue

        for result in report.get('results', []):
            row = dataset_rows.get(str(result.get('session_id') or ''))
            if not row:
                continue
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
    reports_dir = VALIDATION_PRIVATE / 'reports'
    datasets_dir = VALIDATION_PRIVATE / 'datasets' / 'batch_review'
    for report_path in sorted(reports_dir.glob('next200_v*_weak_provider_viability_fast20.json')):
        stem = report_path.stem.replace('_weak_provider_viability_fast20', '')
        dataset_path = datasets_dir / stem / 'next_batch_direct_weak_providers.jsonl'
        if not dataset_path.exists():
            continue
        try:
            dataset_rows = {}
            for line in dataset_path.read_text(encoding='utf-8').splitlines():
                if line.strip():
                    row = json.loads(line)
                    dataset_rows[str(row.get('id') or '')] = row
            report = json.loads(report_path.read_text(encoding='utf-8'))
        except Exception:
            continue

        for result in report.get('results', []):
            row = dataset_rows.get(str(result.get('session_id') or ''))
            if not row:
                continue
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

    subfamily = provider_subfamily_key(provider_key, name)
    priors = _load_empirical_country_subfamily_priors()
    category_priors = _load_empirical_country_category_priors()
    category_key = str(category or '').strip()

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
    for token in re.findall(r'[A-Za-z0-9]+', text.lower()):
        if len(token) <= 2:
            continue
        if token in {'series', 'indicator', 'index', 'rate', 'data', 'table'}:
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

    if description and re.fullmatch(r'[A-Z]{1,3}', kept[0].upper()):
        contextual = description_context_phrase(description)
        if contextual:
            return contextual

    head = kept[0]
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
            prefixes.append(part)
            continue
        suffixes.append(part)

    candidate = ' '.join(prefixes + [head] + suffixes).strip()
    tokens = informative_tokens(candidate)
    if len(tokens) <= 2 and set(tokens) <= _GENERIC_SHORT_TITLE_TOKENS:
        return description_context_phrase(description) or candidate
    return candidate


def derive_coin_query_name(row: dict[str, Any]) -> str:
    name = str(row.get('name') or '').strip()
    code = str(row.get('code') or '').strip()
    if code and '-' in code:
        if len(name.split()) >= 4 or len(name) >= 24 or any(symbol in name for symbol in {'€', '$', '£'}):
            return code
    if len(name) > 4 and not re.fullmatch(r'[A-Z0-9]{1,5}', name):
        return name
    slug = re.sub(r'-\d+$', '', code)
    human = humanize_slug(slug)
    if human:
        return human.title()
    return name.title() if name else code.title()


def query_mentions_country(text: str) -> bool:
    return bool(detect_country_codes_in_text(text))


def count_distinct_country_mentions(text: str) -> int:
    return len(detect_country_codes_in_text(text))


def synthesize_direct_query_for_row(row: dict[str, Any]) -> str:
    provider = str(row.get('provider') or '')
    provider_upper = provider.upper()
    code = str(row.get('code') or '').strip()
    name = str(row.get('name') or '').strip()
    description = str(row.get('description') or '').strip()
    transform = infer_transform_family(name, description, str(row.get('unit') or ''), str(row.get('code') or ''))
    defaults = DEFAULT_COUNTRIES_BY_PROVIDER.get(provider, ['United States'])
    choice = defaults[stable_seed(provider, name) % len(defaults)]
    choice = preferred_default_country_for_record(provider_upper, str(row.get('category') or ''), name, defaults, choice)
    if provider_upper not in {'EXCHANGERATE', 'COINGECKO'}:
        inferred_country = (
            detect_single_country_from_text(name)
            or detect_single_country_from_text(str(row.get('coverage') or ''))
            or detect_single_country_from_text(description)
        )
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
        commodity = re.sub(r'^\d+\s*-\s*', '', name).strip() or name
        return f"{choice} exports of {commodity} from Comtrade"
    if provider_upper == 'STATSCAN':
        return f"Canada {phrase} from Statistics Canada"
    if provider_upper == 'IMF':
        prefix = '' if query_mentions_country(phrase) else f"{choice} "
        return f"{prefix}{phrase} from IMF".strip()
    if provider_upper == 'WORLDBANK':
        prefix = '' if query_mentions_country(phrase) else f"{choice} "
        return f"{prefix}{phrase} from World Bank".strip()
    if provider_upper == 'OECD':
        if re.fullmatch(r'[A-Z0-9]{1,6}', phrase):
            phrase = f"{provider} indicator {phrase}"
        prefix = '' if query_mentions_country(phrase) else f"{choice} "
        return f"{prefix}{phrase} from OECD".strip()
    if provider_upper == 'EUROSTAT':
        prefix = '' if query_mentions_country(phrase) else f"{choice} "
        return f"{prefix}{phrase} from Eurostat".strip()
    if provider_upper == 'BIS':
        if re.fullmatch(r'[A-Z0-9]{1,8}', phrase):
            phrase = f"{provider} indicator {phrase}"
        prefix = '' if query_mentions_country(phrase) else f"{choice} "
        return f"{prefix}{phrase} from BIS".strip()
    if provider_upper == 'FRED':
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


def default_query_for_row(row: dict[str, Any]) -> str:
    return synthesize_direct_query_for_row(row)


def audit_direct_query_shape(row: dict[str, Any]) -> dict[str, Any]:
    query = str(row.get('query') or default_query_for_row(row) or '')
    origin = dict(row.get('origin') or {})
    origin_name = str(origin.get('name') or row.get('name') or '').strip()
    origin_name_lower = origin_name.lower()
    provider = str(row.get('provider') or row.get('provider_stratum') or origin.get('source_provider') or '').strip()
    reasons: list[str] = []
    query_lower = query.lower()
    metadata_text = ''
    if provider.upper() in {'IMF', 'WORLDBANK', 'OECD', 'EUROSTAT'}:
        _, metadata_text = provider_metadata_context(row)
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
    if any(term in query_lower for term in ['debt-service payment schedule', 'wages and salaries in kind', 'other postal services', 'equities domestic company', 'financial market prices end of period']) or ('industrial production' in query_lower and 'manufacture of' in query_lower) or ('producer price index' in query_lower and 'weight' in query_lower and 'manufacture of' in query_lower):
        reasons.append('imf_low_viability_family')
    if 'merchandise trade value of imports' in query_lower and 'chapter' in query_lower:
        reasons.append('imf_low_viability_family')
    if 'definition central government operations' in query_lower and 'funds for redundant labor' in query_lower:
        reasons.append('definition_financial_query')
    if any(term in metadata_text for term in ['global jobs indicators database', 'global findex', 'health nutrition and population statistics by wealth quintile']):
        reasons.append('worldbank_niche_catalog_family')
    category_lower = str(origin.get('category') or '').lower()
    if any(term in category_lower for term in ['global jobs indicators database', 'global findex', 'health nutrition and population statistics by wealth quintile', 'health equity and financial protection', 'atlas of social protection', 'wdi database archives', 'statistical performance indicators', 'country climate and development report', 'indonesia database for policy and economic research', 'joint external debt hub']):
        reasons.append('worldbank_niche_catalog_family')
    if any(term in category_lower for term in ['quarterly external debt statistics', 'global public procurement']):
        reasons.append('worldbank_specialized_source_family')
    if any(term in category_lower for term in ['quarterly public sector debt', 'exporter dynamics database', 'gender statistics']):
        reasons.append('worldbank_specialized_source_family')
    if any(term in worldbank_text for term in ['rights to inherit assets', 'are other banks permitted']):
        reasons.append('worldbank_binary_policy_query')
    if any(term in worldbank_text for term in ['contract teachers', 'salary expenditures per teacher', 'off-budget', 'share of tertiary expenditures', 'pasec', 'pupil/teacher ratio', 'civil service teachers', 'technical/vocational', 'private institution fees', 'egra', 'zero score']):
        reasons.append('worldbank_education_finance_query')
    if any(term in worldbank_text for term in ['no functional difficulty', 'youth literacy rate', 'population 25-64 years']) and any(term in worldbank_text for term in ['rural', 'male', 'female', 'both sexes', 'literacy rate']):
        reasons.append('worldbank_demographic_literacy_slice')
    if any(term in worldbank_text for term in ['seeing difficulty', 'hearing difficulty']) and 'literacy rate' in worldbank_text:
        reasons.append('worldbank_demographic_literacy_slice')
    if any(term in worldbank_text for term in ['challenge:', 'without an id', 'formal financial institution', 'smes with at least one female owner']):
        reasons.append('worldbank_id_financial_inclusion_query')
    if any(term in worldbank_text for term in ['ease of doing business score', 'db15 methodology']):
        reasons.append('worldbank_specialized_source_family')
    if 'reference year' in worldbank_text and 'population age' in worldbank_text and any(term in worldbank_text for term in ['male', 'female']):
        reasons.append('worldbank_demographic_literacy_slice')
    if any(term in worldbank_text for term in ['household spending per student', 'public education expenditure per student', 'share of household consumption for private expenditures']):
        reasons.append('worldbank_education_expenditure_family')
    if any(term in worldbank_text for term in ['teacher salary', 'teachers holding more than one job', 'basic education', 'pre-primary education spending on rural areas', 'pre-primary education spending on urban areas']):
        reasons.append('worldbank_education_expenditure_family')
    if any(term in worldbank_text for term in [
        'public capital expenditure on education',
        'share of total education revenue from school fees',
        'share of total expenditures for goods and services',
        'share of primary education spending on rural areas',
        'share of primary education spending on urban areas',
    ]):
        reasons.append('worldbank_education_expenditure_family')
    if any(term in worldbank_text for term in ['national assessment for learning outcomes', 'optimal competency', 'sea-plm', 'above proficiency']):
        reasons.append('worldbank_assessment_family')
    if any(term in worldbank_text for term in ['public sector wage premium', 'price level ratio of ppp conversion factor', 'elevation is below 5 meters']):
        reasons.append('worldbank_macro_exposure_family')
    if any(term in worldbank_text for term in ['food insecure households', 'adjusted prevalence', 'selfcare difficulty', 'mobility difficulty']):
        reasons.append('worldbank_ddh_prevalence_family')
    if any(term in query_lower for term in ['international poverty line', 'household formality', 'inventory of energy subsidies', 'support measures', "africa's development dynamics", 'afdd', 'table 36', 'incidence of full-time and part-time employment', 'harmonized definition', 'analysis by armed group', 'west africa', 'real labour productivity', 'taking up work when claiming unemployment benefits', 'temporary employment by permanency of the job']):
        reasons.append('oecd_low_viability_family')
    if provider == 'OECD' and 'population in the national accounts' in query_lower and 'distributions by' in query_lower:
        reasons.append('oecd_low_viability_family')
    if any(term in query_lower for term in ['memorandum items', 'producer price index', 'consumer price index']) and any(term in query_lower for term in ['definition', 'organic acids', 'food and non-alcoholic beverages', 'cash government and public sector finance', 'cash, national currency', 'gross value added', 'previous year prices', 'base year']):
        reasons.append('imf_price_or_memorandum_family')
    if 'weight' in query_lower and 'consumer prices' in query_lower and 'from imf' in query_lower:
        reasons.append('imf_price_or_memorandum_family')
    if any(term in query_lower for term in ['wine-grape vine varieties', 'vine variety', 'age of the vines']):
        reasons.append('eurostat_agri_breakdown_query')
    if any(term in query_lower for term in ['activity limitation', 'poverty threshold', 'previous year']) and any(term in query_lower for term in ['sex', 'age', 'most frequent activity']):
        reasons.append('eurostat_cross_tab_query')
    if provider == 'Eurostat' and any(term in query_lower for term in ['household composition', 'degree of urbanisation']) and any(term in origin_name_lower for term in ['participating', 'active citizenship', 'voluntary activities']):
        reasons.append('eurostat_dimension_fragment_query')
    if provider == 'Eurostat' and all(term in query_lower for term in ['household composition', 'degree of urbanisation', 'frequency']):
        reasons.append('eurostat_dimension_fragment_query')
    if re.search(r'^US\s+[A-Z]{2}\b', query) and re.search(r'\b(county|cbsa|msa|metro)\b', query_lower):
        reasons.append('subnational_abbrev_ambiguous')
    if any(marker in query_lower or marker in origin_name_lower for marker in ['sub total', 'exchange difference']):
        reasons.append('accounting_artifact_query')
    if re.search(r'\b(scenario|scenarios|projection|projections|forecast|forecasts|fua|fuas)\b', query_lower):
        reasons.append('scenario_projection_query')
    if re.search(r'total revenue for\s+\d{4}', query_lower) or any(term in query_lower for term in ['net equity in life insurance and pension funds', 'asset (ima)', 'employer firms total revenue', 'total expense for', 'establishments subject to federal income tax', 'defined benefit retirement funds', 'commercial paper; asset']):
        reasons.append('fred_low_viability_family')
    if provider == 'FRED' and 'consumer price indices' in query_lower and 'hicp' in query_lower:
        reasons.append('fred_hicp_catalog_family')
    if provider == 'OECD' and 'share of students enrolled in school and work-based programmes' in query_lower:
        reasons.append('oecd_education_programme_share_query')
    if provider == 'CoinGecko' and re.search(r'\b[a-z0-9]+_[a-z0-9_]+\b', query):
        reasons.append('coin_slug_query')
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
        'oecd_low_viability_family',
        'oecd_education_programme_share_query',
        'imf_price_or_memorandum_family',
        'eurostat_agri_breakdown_query',
        'eurostat_cross_tab_query',
        'eurostat_dimension_fragment_query',
        'subnational_abbrev_ambiguous',
        'accounting_artifact_query',
        'scenario_projection_query',
        'fred_low_viability_family',
        'fred_hicp_catalog_family',
        'coin_slug_query',
        'methodology_dense',
    ]):
        risk_level = 'high'
    elif reasons:
        risk_level = 'medium'

    return {
        'risk_level': risk_level,
        'reasons': reasons,
        'query_length': len(query),
        'punctuation_hits': punctuation_hits,
    }
