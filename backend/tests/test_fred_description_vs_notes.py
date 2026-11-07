"""FRED series prose belongs in the display-only metadata.about, not in
metadata.notes, and metadata.description stays exactly as before.

metadata.notes is the TRANSPARENCY channel (coverage gaps, substitutions,
defaulted members, aggregation caveats) that the UI always renders. FRED used
to truncate its series notes to 500 chars and split them on periods into up to
three "notes", so descriptive/citation boilerplate surfaced as always-visible
fragments such as "(accessed on (date))." and drowned out real disclosures.

The full prose now travels in metadata.about (rendered collapsed as "About
this series"). metadata.description keeps its historical 200-char head
byte-for-byte: relevance scoring, fallback validation and the country/region
scope checks read description, so its length must not change under them.
"""
from __future__ import annotations

import pytest

from backend.providers.fred import _ABOUT_MAX_CHARS, FREDProvider, _series_about

LONG_NOTES = (
    "The unemployment rate represents the number of unemployed as a percentage "
    "of the labor force. Labor force data are restricted to people 16 years of "
    "age and older, who currently reside in 1 of the 50 states or the District "
    "of Columbia, who do not reside in institutions (e.g., penal and mental "
    "facilities, homes for the aged), and who are not on active duty in the "
    "Armed Forces.\n\nThis rate is also defined as the U-3 measure of labor "
    "underutilization.\n\nThe series comes from the 'Current Population Survey "
    "(Household Survey)'.\n\nSuggested citation: U.S. Bureau of Labor "
    "Statistics, Unemployment Rate [UNRATE], retrieved from FRED, Federal "
    "Reserve Bank of St. Louis (accessed on (date))."
)


class _FakeResponse:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


async def _fetch_with_notes(monkeypatch, notes):
    provider = FREDProvider("test-key")

    async def fake_resolve(indicator, series_id):
        return "UNRATE", None

    info = {
        "id": "UNRATE",
        "title": "Unemployment Rate",
        "units": "Percent",
        "frequency": "Monthly",
        "observation_start": "1948-01-01",
        "observation_end": "2026-10-01",
        "last_updated": "2026-11-07",
        "seasonal_adjustment": "Seasonally Adjusted",
        "notes": notes,
    }
    observations = {"observations": [
        {"date": "2026-08-01", "value": "4.3"},
        {"date": "2026-09-01", "value": "4.4"},
        {"date": "2026-10-01", "value": "4.4"},
    ]}

    async def fake_get_with_retry(client, url, *args, **kwargs):
        if "series/observations" in url:
            return _FakeResponse(observations)
        return _FakeResponse({"seriess": [info]})

    monkeypatch.setattr(provider, "_resolve_series_id_async", fake_resolve)
    monkeypatch.setattr(provider, "_get_with_retry", fake_get_with_retry)
    return await provider.fetch_series({"seriesId": "UNRATE"})


@pytest.mark.asyncio
async def test_series_prose_goes_to_about_not_notes(monkeypatch):
    result = await _fetch_with_notes(monkeypatch, LONG_NOTES)
    meta = result.metadata
    # No descriptive fragments in the always-visible transparency channel.
    assert not meta.notes
    # The display-only about carries the whole prose, whitespace-normalized.
    assert meta.about is not None
    assert meta.about.startswith("The unemployment rate represents")
    assert "U-3 measure of labor underutilization." in meta.about
    assert "Current Population Survey" in meta.about
    assert "\n" not in meta.about


@pytest.mark.asyncio
async def test_description_is_byte_identical_to_the_historical_head(monkeypatch):
    """Runtime checks (series_text_for_relevance, is_fallback_relevant, the
    FRED country-scope and subnational region checks) read description; it
    must stay the same 200-char head of the raw notes it always was."""
    result = await _fetch_with_notes(monkeypatch, LONG_NOTES)
    assert result.metadata.description == LONG_NOTES[:200]


@pytest.mark.asyncio
@pytest.mark.parametrize("empty", ["", None])
async def test_missing_notes_yield_no_description_or_about(monkeypatch, empty):
    result = await _fetch_with_notes(monkeypatch, empty)
    assert result.metadata.description is None
    assert result.metadata.about is None
    assert not result.metadata.notes


@pytest.mark.asyncio
async def test_whitespace_only_notes_yield_no_about(monkeypatch):
    result = await _fetch_with_notes(monkeypatch, "   ")
    assert result.metadata.about is None
    assert not result.metadata.notes


def test_runtime_checks_do_not_read_about():
    """about is display-only. The text the relevance scorer builds from a
    series must not change when about is filled in."""
    from backend.models import Metadata, NormalizedData
    from backend.services.relevance_scorer import series_text_for_relevance

    base = dict(source="FRED", indicator="Unemployment Rate", frequency="monthly",
                unit="Percent", seriesId="UNRATE", description=LONG_NOTES[:200])
    without = NormalizedData(metadata=Metadata(**base), data=[])
    with_about = NormalizedData(metadata=Metadata(**base, about=_series_about(LONG_NOTES)), data=[])
    assert series_text_for_relevance(with_about) == series_text_for_relevance(without)


def test_overlong_about_is_cut_at_a_sentence_boundary():
    sentence = "This sentence describes the series methodology in detail. "
    text = sentence * 100  # ~5,900 chars
    out = _series_about(text)
    assert out is not None
    assert len(out) <= _ABOUT_MAX_CHARS + len(" …")
    assert out.endswith(" …")
    # Never cut mid-sentence: the text before the ellipsis ends a sentence.
    assert out[: -len(" …")].endswith(".")
    # The window is used, not thrown away.
    assert len(out) > _ABOUT_MAX_CHARS // 2


def test_sentence_ending_exactly_at_the_window_is_kept():
    body = "x" * (_ABOUT_MAX_CHARS - 1) + "."  # sentence ends exactly at the bound
    out = _series_about(body + " Next sentence that does not fit.")
    assert out == body + " …"


def test_early_abbreviation_does_not_collapse_the_about():
    """The only '. ' inside the window is "U.S. " near the start; a sentence
    cut there would shrink the text to "The U.S. …". Fall back to the last
    word boundary instead."""
    text = "The U.S. series " + " ".join(["word"] * 400)
    out = _series_about(text)
    assert out is not None
    assert len(out) > _ABOUT_MAX_CHARS // 2
    assert len(out) <= _ABOUT_MAX_CHARS + len(" …")
    assert out.endswith("word …")


def test_short_about_is_unchanged_apart_from_whitespace():
    assert _series_about("  Line one.\n\nLine   two.  ") == "Line one. Line two."
