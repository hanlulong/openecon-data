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
