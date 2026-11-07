"""Chinese explicit-provider requests (directive grammar + zh identity names).

~90% of production traffic is Chinese.  A Chinese source marker
(来自 / 使用 / 用 / 根据 / 依据 / 据 / 从 / 数据来源 / 来源) is directive syntax —
the same class as "from" / "using" — and official Chinese provider names are
identity reference data.  A marker alone never matches; a registered alias
must follow directly.
"""

from __future__ import annotations

import pytest

from backend.routing.unified_router import detect_explicit_provider_match
from backend.utils.providers import PROVIDER_SPECS, normalize_provider_name

_ZH_MARKERS = ("来自", "使用", "用", "根据", "依据", "据", "从", "数据来源：", "来源:", "来自于")


@pytest.mark.parametrize("marker", _ZH_MARKERS)
@pytest.mark.parametrize("spec", PROVIDER_SPECS, ids=lambda s: s.canonical)
def test_every_provider_is_detectable_after_every_zh_marker(spec, marker) -> None:
    # English alias after a Chinese marker ("中国GDP 来自IMF").
    alias = spec.directive_aliases[0]
    match = detect_explicit_provider_match(f"中国GDP {marker}{alias}")
    assert match is not None and match[0] == spec.name


def _is_cjk(text: str) -> bool:
    return any("一" <= ch <= "鿿" for ch in text)


_ZH_ALIAS_CASES = [
    (spec.name, alias)
    for spec in PROVIDER_SPECS
    for alias in spec.directive_aliases
    if _is_cjk(alias)
]


@pytest.mark.parametrize(("provider", "alias"), _ZH_ALIAS_CASES)
def test_zh_official_names_are_detectable_after_a_marker(provider, alias) -> None:
    for marker in ("来自", "使用", "根据"):
        match = detect_explicit_provider_match(f"中国GDP {marker}{alias}")
        assert match is not None and match[0] == provider
        assert normalize_provider_name(alias) == normalize_provider_name(provider)


@pytest.mark.parametrize(
    ("query", "provider"),
    [
        ("中国GDP 世界银行", "WorldBank"),
        ("国际货币基金组织 中国GDP", "IMF"),
        ("中国信贷 国际清算银行", "BIS"),
        ("德国通胀 欧盟统计局", "Eurostat"),
        ("加拿大统计局 CPI", "StatsCan"),
        ("中国PMI chinamacro", "ChinaMacro"),
    ],
)
def test_unambiguous_zh_official_names_match_bare(query, provider) -> None:
    match = detect_explicit_provider_match(query)
    assert match is not None and match[0] == provider


@pytest.mark.parametrize(
    "query",
    [
        "经合组织国家平均失业率",  # "OECD countries average" is geography
        "经合组织成员国GDP",
        "中国宏观数据",  # descriptive phrase, never a provider
        "中国GDP数据IMF",  # 数据 ("data") is not the 据 marker
        "中国信用 数据",
        "美联储利率",  # the Fed as a topic, not a source directive
        "用电量 中国",
        "从2010年到2020年中国GDP",
        "根据最新数据中国CPI",
    ],
)
def test_zh_ordinary_language_is_never_an_explicit_provider(query) -> None:
    assert detect_explicit_provider_match(query) is None


def test_later_zh_directive_wins_like_english() -> None:
    assert detect_explicit_provider_match("不用IMF，用世界银行的中国GDP")[0] == "WorldBank"


# ---------------------------------------------------------------------------
# Linear-time guard (the detector runs synchronously on the event loop, many
# times per request, on anonymous input up to the 5000-char QueryRequest cap)
# ---------------------------------------------------------------------------

_MAX_QUERY_CHARS = 5000


@pytest.mark.parametrize(
    "marker",
    ["数据来源", "来源", "来源于", "数据来源：", "来源:", "来自", "使用", "根据", "据", "从", "用",
     "from", "using", "according to", "from the"],
)
@pytest.mark.parametrize("whitespace", [" ", "　", "\t", "\n", " 　"])
def test_marker_followed_by_long_whitespace_run_stays_linear(marker, whitespace) -> None:
    import time

    from backend.routing.unified_router import _detect_explicit_provider_match_lower

    prefix = f"中国gdp {marker}"
    run = whitespace * ((_MAX_QUERY_CHARS - len(prefix) - 1) // len(whitespace))
    query = f"{prefix}{run}x"
    assert len(query) <= _MAX_QUERY_CHARS
    started = time.perf_counter()
    # Bypass the memo so the regex work itself is timed.
    assert _detect_explicit_provider_match_lower.__wrapped__(query) is None
    # Linear: ~tens of ms even on a loaded host.  The quadratic form this
    # guards against took ~50 s at this length.
    assert time.perf_counter() - started < 2.0


@pytest.mark.parametrize(
    ("query", "keyword"),
    [
        ("数据来源：IMF", "数据来源：imf"),
        ("数据来源: imf", "数据来源: imf"),
        ("来源于IMF", "来源于imf"),
        ("来源 ： IMF", "来源 ： imf"),
        ("数据来源 IMF", "数据来源 imf"),
        ("来源：  imf", "来源：  imf"),
    ],
)
def test_source_marker_colon_and_spacing_forms_still_match(query, keyword) -> None:
    assert detect_explicit_provider_match(query) == ("IMF", keyword)


# ---------------------------------------------------------------------------
# Documented known limits of regex directive grammar (strict xfail: the day a
# mechanism fixes one, the XPASS fails and the entry must be removed).
# Negation and temporal "since" are SEMANTIC — they must be solved by a
# parse-LLM explicit-provider field validated against the registry, never by
# a growing list of compound-word exceptions in the regex.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("query", "intended"),
    [
        # 自从 = "since": temporal, not a source directive.
        ("自从IMF援助以来希腊GDP", None),
        ("从美联储加息以来人民币汇率走势", None),
        # 不用 = "do not use": negation flips the intent.
        ("中国GDP 用世界银行，不用IMF", "WorldBank"),
        ("不用IMF的数据，中国GDP", None),
        # English parity (pre-existing): same negation limit.
        ("not from IMF, China GDP", None),
        # CJK has no word boundary: 世界银行业 = "world banking industry".
        ("世界银行业发展", None),
    ],
)
@pytest.mark.xfail(strict=True, reason="known limit: semantic negation/temporal/compound needs LLM explicit-provider field")
def test_known_limits_of_regex_directive_grammar(query, intended) -> None:
    match = detect_explicit_provider_match(query)
    assert (match[0] if match else None) == intended
