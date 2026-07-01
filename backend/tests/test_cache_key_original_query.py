"""Raw query text must not fragment the cache for ordinary providers.

__original_query is injected for cache differentiation but was never stripped
from cache_params, so it entered the key for EVERY provider — defeating the
_RAW_QUERY_CACHE_HASH_PROVIDERS gating and causing two phrasings of the same
resolved query to miss each other and double-fetch. It is now popped from the
key; only the gated providers (StatsCan dimension extraction) still get a
_query_hash derived from it.
"""

from backend.services.query import QueryService, _RAW_QUERY_CACHE_HASH_PROVIDERS


def _qs():
    qs = QueryService.__new__(QueryService)
    qs.CACHE_KEY_VERSION = "test-v1"
    return qs


_BASE = {
    "indicator": "GDP",
    "country": "US",
    "startDate": "2020-01-01",
    "endDate": "2024-01-01",
}


def test_original_query_stripped_for_non_gated_provider():
    p = _qs()._build_cache_params("FRED", {**_BASE, "__original_query": "US GDP 2020-2024"})
    assert "__original_query" not in p
    assert "_query_hash" not in p


def test_non_gated_key_stable_across_phrasings():
    qs = _qs()
    a = qs._build_cache_params("FRED", {**_BASE, "__original_query": "US GDP 2020-2024"})
    b = qs._build_cache_params("FRED", {**_BASE, "__original_query": "gross domestic product United States 2020 to 2024"})
    assert a == b


def test_gated_provider_still_differentiates_on_query():
    assert "STATSCAN" in _RAW_QUERY_CACHE_HASH_PROVIDERS
    qs = _qs()
    a = qs._build_cache_params("STATSCAN", {**_BASE, "__original_query": "unemployment rate for men"})
    b = qs._build_cache_params("STATSCAN", {**_BASE, "__original_query": "unemployment rate for women"})
    assert "__original_query" not in a
    assert a.get("_query_hash") and b.get("_query_hash")
    assert a["_query_hash"] != b["_query_hash"]
