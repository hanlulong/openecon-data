"""Guard: FTS query building must survive arbitrary punctuation.

The FTS5 index tokenizer (unicode61) splits on every non-alphanumeric
character, so the query builder must too. An enumerated escape list left
commas inside quoted tokens ('"2025,"*'), an FTS5 syntax error that killed
the whole lexical retrieval arm for any query with stray punctuation
(observed in production logs 2026-07-10).
"""

from backend.services.indicator_database import get_indicator_lookup


def test_punctuated_query_does_not_raise():
    lookup = get_indicator_lookup()
    # Would previously raise sqlite3.OperationalError inside search() and be
    # logged as "Search error"; must return a list (possibly empty) instead.
    for q in (
        "digital economy 2020 2025, gdp",
        "gdp; unemployment!",
        "cpi (monthly) [2024]",
        "what's the m2, exactly?",
        "。，！？gdp",
    ):
        result = lookup.search(q, provider="FRED", limit=3)
        assert isinstance(result, list)


def test_normal_and_code_queries_still_resolve():
    lookup = get_indicator_lookup()
    top = lookup.search("unemployment rate", provider="FRED", limit=5)
    assert top and top[0]["code"] == "UNRATE"
    dotted = lookup.search("9.0.Unemp.All", provider="WORLDBANK", limit=3)
    assert dotted and dotted[0]["code"] == "9.0.Unemp.All"
