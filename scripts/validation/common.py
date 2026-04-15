#!/usr/bin/env python3
from __future__ import annotations

import hashlib
from html import unescape
import json
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
        'SELECT id, provider, code, name, description, category, subcategory, unit, frequency, coverage, start_date, end_date, keywords, synonyms FROM indicators WHERE provider = ? ORDER BY id',
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


def direct_query_specificity_score(record: dict[str, Any]) -> int:
    origin = dict(record.get('origin') or {})
    query = str(record.get('query') or '')
    name = str(origin.get('name') or record.get('name') or '').strip()
    description = str(origin.get('description') or record.get('description') or '').strip()

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
    lowered = str(text or '').lower()
    return any(re.search(rf'\b{re.escape(country)}\b', lowered) for country in _COUNTRY_QUERY_TERMS)


def count_distinct_country_mentions(text: str) -> int:
    lowered = str(text or '').lower()
    matches = {
        country
        for country in _COUNTRY_QUERY_TERMS
        if re.search(rf'\b{re.escape(country)}\b', lowered)
    }
    if {'us', 'usa', 'united states'} & matches:
        matches -= {'us', 'usa', 'united states'}
        matches.add('united states')
    if {'uk', 'united kingdom'} & matches:
        matches -= {'uk', 'united kingdom'}
        matches.add('united kingdom')
    return len(matches)


def synthesize_direct_query_for_row(row: dict[str, Any]) -> str:
    provider = str(row.get('provider') or '')
    provider_upper = provider.upper()
    code = str(row.get('code') or '').strip()
    name = str(row.get('name') or '').strip()
    description = str(row.get('description') or '').strip()
    transform = infer_transform_family(name, description, str(row.get('unit') or ''), str(row.get('code') or ''))
    defaults = DEFAULT_COUNTRIES_BY_PROVIDER.get(provider, ['United States'])
    choice = defaults[stable_seed(provider, name) % len(defaults)]
    phrase = natural_phrase_from_name(name, description)

    if provider_upper == 'COINGECKO':
        return f"{derive_coin_query_name(row)} cryptocurrency price from CoinGecko"
    if provider_upper == 'EXCHANGERATE':
        return choice
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
    reasons: list[str] = []
    query_lower = query.lower()

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
    if count_distinct_country_mentions(query) > 1:
        reasons.append('country_scope_conflict')
    if (
        ('average age' in query_lower and any(term in query_lower for term in ['urban', 'rural', 'female', 'male', 'men', 'women']))
        or (re.search(r'\bage\s+\d{1,2}', query_lower) and any(term in query_lower for term in ['female', 'male', 'men', 'women', 'urban', 'rural']))
    ):
        reasons.append('micro_demographic_slice')
    if (
        (re.search(r'\bage\s+\d{1,2}(?:-\d{1,2})?\b', query_lower) and any(term in query_lower for term in ['school', 'schooling', 'education', 'barro-lee']))
        or ('schools with access to internet' in query_lower and any(term in query_lower for term in ['rural', 'urban', 'male', 'female']))
    ):
        reasons.append('education_subgroup_slice')
    if any(term in query_lower for term in ['poorest', 'richest', 'quintile', 'decile', 'percentile']):
        reasons.append('socioeconomic_slice')
    if 'definition' in query_lower and 'survey' in query_lower:
        reasons.append('definition_survey_query')
    if 'labor markets' in query_lower and 'number of persons' in query_lower:
        reasons.append('classification_labor_query')
    if 'gross value added' in query_lower and any(term in query_lower for term in ['electricity', 'gas', 'water supply']):
        reasons.append('classification_gva_query')
    if re.search(r'^US\s+[A-Z]{2}\b', query) and re.search(r'\b(county|cbsa|msa|metro)\b', query_lower):
        reasons.append('subnational_abbrev_ambiguous')
    if any(marker in query_lower or marker in origin_name_lower for marker in ['sub total', 'exchange difference']):
        reasons.append('accounting_artifact_query')
    if re.search(r'\b(scenario|scenarios|projection|projections|forecast|forecasts|fua|fuas)\b', query_lower):
        reasons.append('scenario_projection_query')
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
        'opaque_acronym_query',
        'country_scope_conflict',
        'micro_demographic_slice',
        'education_subgroup_slice',
        'socioeconomic_slice',
        'definition_survey_query',
        'classification_labor_query',
        'classification_gva_query',
        'subnational_abbrev_ambiguous',
        'accounting_artifact_query',
        'scenario_projection_query',
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
