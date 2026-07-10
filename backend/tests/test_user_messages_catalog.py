"""Guard tests for the localized user-facing message catalog (Proposal C).

English is the source of truth and the always-available fallback; a missing
language, message id, or placeholder must NEVER raise, because these strings
render on live response paths.
"""
from __future__ import annotations

from backend.services.user_messages import get_message


def test_zh_no_data_message_rendered():
    msg = get_message("no_data_finalization", "zh", scope="**GDP**")
    assert "**GDP**" in msg
    # Native Simplified Chinese framing, not the English sentence.
    assert "无可用数据" in msg
    assert "No data found" not in msg


def test_english_default_when_language_none():
    msg = get_message("no_data_finalization", None, scope="**GDP**")
    assert msg.startswith("⚠️ **No Data Available**")


def test_unknown_language_falls_back_to_english():
    msg = get_message(
        "subnational_national_only", "xx",
        region="Beijing", country="China", provider="WorldBank",
    )
    assert "not published there" in msg  # English fallback


def test_region_tag_normalized_to_primary_subtag():
    # "zh-CN" / "zh_Hans" must resolve to the zh catalog entry.
    for tag in ("zh-CN", "zh_Hans", "ZH"):
        msg = get_message("no_data_finalization", tag, scope="**GDP**")
        assert "无可用数据" in msg


def test_unknown_message_id_returns_empty_never_raises():
    assert get_message("does_not_exist", "en") == ""


def test_missing_placeholder_never_raises():
    # Caller forgot kwargs — must not raise; returns a best-effort template.
    msg = get_message("subnational_national_only", "zh")
    assert isinstance(msg, str)


def test_multi_indicator_partial_zh():
    msg = get_message("multi_indicator_partial", "zh", missing="inflation, CPI")
    assert "inflation, CPI" in msg
    assert "无法获取" in msg


def test_coverage_partial_variants():
    with_avail = get_message(
        "country_coverage_partial_with_available", "en",
        missing="Brazil", available="India",
    )
    assert "Missing: Brazil" in with_avail and "Available: India" in with_avail
    missing_only = get_message(
        "country_coverage_partial_missing_only", "zh", missing="Brazil",
    )
    assert "Brazil" in missing_only and "缺失" in missing_only
