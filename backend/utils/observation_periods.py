"""Calendar-structural observation-period helpers shared by providers and the
fetch layer.

Pure normalization and calendar arithmetic (no semantic decision): a
frequency label -> period code, the calendar bucket an observation date
belongs to, and whether a series' LATEST observation is older than its
frequency's release cycle allows. Moved here from services/data_fetcher so a
provider can use the same period math without importing the orchestrator.
"""
from __future__ import annotations

import calendar
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Optional

# Closed structural table: provider frequency labels / SDMX codes -> period code.
# Normalization only (no semantic decision); anything unrecognized is None and
# callers fall back to their previous year-level behavior.
FREQUENCY_WORD_CODES: Dict[str, str] = {
    "annual": "A", "annually": "A", "yearly": "A", "year": "A", "years": "A",
    "semiannual": "S", "semiannually": "S", "biannual": "S", "semester": "S",
    "half": "S", "halfyearly": "S",
    "quarterly": "Q", "quarter": "Q", "quarters": "Q",
    "monthly": "M", "month": "M", "months": "M",
    "weekly": "W", "week": "W", "weeks": "W",
    "daily": "D", "day": "D", "days": "D",
}
FREQUENCY_LETTER_CODES: Dict[str, str] = {
    "A": "A", "Y": "A", "S": "S", "H": "S", "Q": "Q", "M": "M", "W": "W", "D": "D", "B": "D",
}


def frequency_code(value: Any) -> Optional[str]:
    """Normalize a frequency label ("Monthly", "Quarterly, End of Period", "M",
    "semi-annual", "Weekly, Ending Friday") to D/W/M/Q/S/A, or None."""
    text = str(value or "").strip()
    if not text:
        return None
    if len(text) == 1:
        return FREQUENCY_LETTER_CODES.get(text.upper())
    lowered = text.lower().replace("semi-annual", "semiannual").replace("half-year", "halfyearly")
    for token in re.findall(r"[a-z]+", lowered):
        code = FREQUENCY_WORD_CODES.get(token)
        if code:
            return code
    return None


def observation_period(obs_date: date, period_code: Optional[str]) -> tuple[date, date]:
    """Calendar bucket CONTAINING ``obs_date`` at the series frequency.

    Containment (not date+length) is deliberate: providers stamp periods at
    the start (2026-10-01) OR the end (2026-10-31) — both lie in October.
    Weekly uses a +/-6-day span so week-start and week-ending stamps both
    cover their week. Unknown frequency -> the whole calendar year (the
    previous year-level convention).
    """
    if period_code == "D":
        return obs_date, obs_date
    if period_code == "W":
        return obs_date - timedelta(days=6), obs_date + timedelta(days=6)
    year = obs_date.year
    if period_code == "M":
        first_month = last_month = obs_date.month
    elif period_code == "Q":
        first_month = ((obs_date.month - 1) // 3) * 3 + 1
        last_month = first_month + 2
    elif period_code == "S":
        first_month = 1 if obs_date.month <= 6 else 7
        last_month = first_month + 5
    else:
        first_month, last_month = 1, 12
    last_day = calendar.monthrange(year, last_month)[1]
    return date(year, first_month, 1), date(year, last_month, last_day)


# Freshness budget: the maximum days between the END of a series' latest
# observed period and the reference date that its frequency's release cycle
# plus a typical publication lag allows. The newest published period stays
# the latest until the NEXT release, so each budget is about one period plus
# the slowest routine lag (e.g. BIS total credit publishes a quarter ~5-6
# months after it ends). Calendar-structural: one entry per frequency, never
# per series or provider.
LATEST_PERIOD_AGE_BUDGET_DAYS: Dict[str, int] = {
    "D": 14, "W": 21, "M": 92, "Q": 275, "S": 400, "A": 550,
}


def _iso_date(value: Any) -> Optional[date]:
    text = str(value or "").strip()
    for size, fmt in ((10, "%Y-%m-%d"), (7, "%Y-%m"), (4, "%Y")):
        if len(text) >= size:
            try:
                return datetime.strptime(text[:size], fmt).date()
            except ValueError:
                continue
    return None


def latest_period_is_stale(
    latest_observation: Any,
    frequency: Any,
    *,
    as_of: Optional[date] = None,
) -> bool:
    """True when a series' latest observation is older than its frequency's
    release cycle allows, measured from the end of the period it belongs to.

    ``as_of`` is the reference date (default: today, UTC); pass the end of a
    user-requested window that ends in the past so a series that reaches the
    window's end is current for that request. An unparseable date or an
    unknown frequency is never stale — absence of evidence is not staleness.
    """
    observed = _iso_date(latest_observation)
    code = frequency_code(frequency)
    if observed is None or code is None:
        return False
    reference = as_of or datetime.now(timezone.utc).date()
    _, period_end = observation_period(observed, code)
    return (reference - period_end).days > LATEST_PERIOD_AGE_BUDGET_DAYS[code]
