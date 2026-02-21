# Multi-Country Query Infrastructure Fix Plan

## Executive Summary

Multi-country queries (G7, BRICS, EU, ASEAN) are failing because the system has **no region expansion mechanism**. The prompt claims "Backend will automatically expand regions to individual country codes" but this functionality **doesn't exist**.

## Root Cause Analysis

### Issue 1: No Region Expansion Method

**File:** `backend/routing/country_resolver.py`

The `CountryResolver` class has all region definitions:
- `G7_MEMBERS`: CA, FR, DE, IT, JP, GB, US
- `BRICS_MEMBERS`: BR, RU, IN, CN, ZA
- `ASEAN_MEMBERS`: BN, KH, ID, LA, MY, MM, PH, SG, TH, VN
- `EU_MEMBERS`: 27 countries

**But NO method to expand a region name to its members!**

```python
# EXISTS: Check if country is in G7
is_g7_member("US") → True

# MISSING: Expand G7 to all members
expand_region("G7") → ["CA", "FR", "DE", "IT", "JP", "GB", "US"]
```

### Issue 2: Deep Agent Doesn't Recognize Region Groups

**File:** `backend/services/deep_agent_orchestrator.py`

The `_analyze_query_complexity()` method has hardcoded country patterns:
```python
country_patterns = [
    "us", "usa", "united states", "america",
    "uk", "britain", "united kingdom",
    "germany", "france", "italy", "spain",
    # ... individual countries only
]
```

**Missing:** "g7", "brics", "asean", "eu", "nordic", "g20"

When query says "Compare G7 GDP", the system:
1. Detects "g7" as country (maybe)
2. Passes `countries: ["g7"]` to fetch
3. Provider doesn't understand "g7"
4. Returns 0-1 countries instead of 7

### Issue 3: Provider Routing Ignores Multi-Country Need

**File:** `backend/services/deep_agent_orchestrator.py`

The `select_best_provider()` function:
```python
def select_best_provider(indicator: str, country: str = None):
    # Only looks at single country
    # Doesn't consider multi-country coverage
```

For G7 query, it might select FRED (US-only) instead of WorldBank (global).

## Detailed Fix Plan

### Fix 1: Add Region Expansion to CountryResolver

**File:** `backend/routing/country_resolver.py`

Add new method:
```python
@classmethod
def expand_region(cls, region: str) -> Optional[List[str]]:
    """
    Expand a region name to its member countries.

    Args:
        region: Region name (G7, BRICS, EU, ASEAN, etc.)

    Returns:
        List of ISO Alpha-2 codes, or None if not a region
    """
    region_upper = region.upper().strip().replace(" ", "_")

    REGION_MAPPINGS = {
        "G7": cls.G7_MEMBERS,
        "G7_COUNTRIES": cls.G7_MEMBERS,
        "G20": cls.G20_MEMBERS,
        "G20_COUNTRIES": cls.G20_MEMBERS,
        "BRICS": cls.BRICS_MEMBERS,
        "BRICS_COUNTRIES": cls.BRICS_MEMBERS,
        "BRICS_PLUS": cls.BRICS_PLUS_MEMBERS,
        "EU": cls.EU_MEMBERS,
        "EU_COUNTRIES": cls.EU_MEMBERS,
        "EUROPEAN_UNION": cls.EU_MEMBERS,
        "EUROZONE": cls.EUROZONE_MEMBERS,
        "ASEAN": cls.ASEAN_MEMBERS,
        "ASEAN_COUNTRIES": cls.ASEAN_MEMBERS,
        "OECD": cls.OECD_MEMBERS,
        "OECD_COUNTRIES": cls.OECD_MEMBERS,
        "NORDIC": frozenset({"DK", "FI", "IS", "NO", "SE"}),
        "NORDIC_COUNTRIES": frozenset({"DK", "FI", "IS", "NO", "SE"}),
    }

    if region_upper in REGION_MAPPINGS:
        return list(REGION_MAPPINGS[region_upper])

    return None

@classmethod
def is_region(cls, text: str) -> bool:
    """Check if text is a recognized region name."""
    return cls.expand_region(text) is not None
```

### Fix 2: Update DeepAgentOrchestrator Analysis

**File:** `backend/services/deep_agent_orchestrator.py`

Update `_analyze_query_complexity()`:
```python
def _analyze_query_complexity(self, query: str) -> Dict[str, Any]:
    query_lower = query.lower()

    # Import CountryResolver for region expansion
    from ..routing.country_resolver import CountryResolver

    # Detect region groups FIRST
    region_patterns = {
        "g7": "G7", "g-7": "G7",
        "g20": "G20", "g-20": "G20",
        "brics": "BRICS", "brics+": "BRICS_PLUS",
        "eu": "EU", "european union": "EU",
        "eurozone": "EUROZONE",
        "asean": "ASEAN",
        "nordic": "NORDIC", "nordic countries": "NORDIC",
        "oecd": "OECD",
    }

    countries_mentioned = []
    regions_detected = []

    # Check for region groups first
    for pattern, region_name in region_patterns.items():
        if pattern in query_lower:
            expanded = CountryResolver.expand_region(region_name)
            if expanded:
                regions_detected.append(region_name)
                countries_mentioned.extend(expanded)

    # Then check individual countries
    # ... existing country detection ...

    return {
        "needs_planning": True if regions_detected else ...,
        "countries": countries_mentioned,
        "regions": regions_detected,  # NEW
        "providers": providers_mentioned,
        "indicators": indicators_mentioned,
        "is_comparison": is_comparison,
        "is_multi_country": len(countries_mentioned) > 1,  # NEW
    }
```

### Fix 3: Update Orchestrator _should_use_deep_agent

**File:** `backend/agents/orchestrator.py`

Add region detection:
```python
def _should_use_deep_agent(self, query: str, routing_result: RoutingResult) -> bool:
    if not self._deep_agent:
        return False

    query_lower = query.lower()

    # Check for region groups
    region_keywords = ["g7", "g20", "brics", "eu", "asean", "nordic", "oecd"]
    has_region = any(kw in query_lower for kw in region_keywords)

    if has_region:
        logger.info(f"Using Deep Agent for region-based query")
        return True

    # ... existing logic ...
```

### Fix 4: Update Provider Selection for Multi-Country

**File:** `backend/services/deep_agent_orchestrator.py`

Update `select_best_provider()`:
```python
def select_best_provider(indicator: str, country: str = None, is_multi_country: bool = False) -> str:
    # If multi-country query, prefer providers with global coverage
    if is_multi_country:
        # WorldBank has global coverage
        if indicator.lower() in ["gdp", "unemployment", "population", "poverty"]:
            return "WorldBank"
        # IMF for financial/fiscal
        if indicator.lower() in ["debt", "deficit", "current account", "reserves"]:
            return "IMF"

    # ... existing single-country logic ...
```

## Implementation Order

1. **CountryResolver.expand_region()** - Foundation for all other fixes
2. **CountryResolver.is_region()** - Helper method
3. **DeepAgentOrchestrator._analyze_query_complexity()** - Region detection
4. **AgentOrchestrator._should_use_deep_agent()** - Trigger Deep Agent for regions
5. **select_best_provider()** - Multi-country provider selection

## Testing Strategy

After implementing, test with these queries:

```bash
# G7 queries
"Compare G7 GDP growth"
"G7 unemployment rates 2023"
"G7 inflation comparison"

# BRICS queries
"BRICS GDP comparison"
"Show BRICS current account balance"

# EU queries
"EU inflation rates 2024"
"European Union unemployment"

# ASEAN queries
"ASEAN GDP growth ranking"

# Nordic queries
"Nordic countries unemployment"
"Compare Nordic inflation rates"
```

Each should return data for ALL members of the group.

## Success Criteria

| Query | Expected Countries | Provider |
|-------|-------------------|----------|
| G7 GDP | 7 (CA, FR, DE, IT, JP, GB, US) | WorldBank |
| BRICS GDP | 5 (BR, RU, IN, CN, ZA) | WorldBank |
| Nordic unemployment | 5 (DK, FI, IS, NO, SE) | WorldBank |
| EU inflation | 27 EU members | Eurostat |
| ASEAN GDP | 10 ASEAN members | WorldBank |

## Files to Modify

1. `backend/routing/country_resolver.py` - Add expansion methods
2. `backend/services/deep_agent_orchestrator.py` - Region detection + expansion
3. `backend/agents/orchestrator.py` - Trigger Deep Agent for regions
4. `docs/testing/TEST_TRACKING.md` - Document fix and results

## Estimated Impact

This fix addresses **3 infrastructure issues** identified in testing:
- ISSUE-002: Multi-Country Provider Routing
- ISSUE-003: Country Group Expansion Incomplete
- ISSUE-004: Aggregate vs Individual Results

**Queries affected:** All queries containing G7, BRICS, EU, ASEAN, Nordic, G20, OECD, Eurozone
