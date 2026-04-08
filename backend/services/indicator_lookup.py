"""
Backwards-compatibility re-export stub.

All functionality has been merged into indicator_database.py.
Import from there instead:

    from backend.services.indicator_database import IndicatorLookup, get_indicator_lookup
"""

from .indicator_database import IndicatorLookup, get_indicator_lookup

__all__ = ["IndicatorLookup", "get_indicator_lookup"]
