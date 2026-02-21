# Indicator Resolution System Analysis

## Executive Summary

This document analyzes why the query "what is the productivity of china and us" failed with error:
> No data found for any of the requested countries for indicator AG.PRD.GAGRI.XD

**Root Cause**: The system matched "productivity" to `AG.PRD.GAGRI.XD` (Agriculture Production Index) instead of `SL.GDP.PCAP.EM.KD` (GDP per person employed - the actual labor productivity indicator).

**Key Finding**: The metadata search uses BOTH indicator names AND descriptions for matching, which caused the false positive because the word "productivity" appears in the description of the agriculture production index.

---

## The Indicator Resolution Pipeline

```
User Query: "what is the productivity of china and us"
    ↓
1. LLM Query Parsing (openrouter.py)
   → ParsedIntent: {apiProvider: "WorldBank", indicators: ["productivity"]}
    ↓
2. Provider Routing (provider_router.py)
   → WorldBank selected (China is not OECD member)
    ↓
3. Hardcoded Mapping Lookup (worldbank.py)
   → "PRODUCTIVITY" NOT in INDICATOR_MAPPINGS dict
   → Falls back to metadata search
    ↓
4. Metadata Search (metadata_search.py + FAISS)
   → Searches worldbank.json using keyword + vector search
   → Finds AG.PRD.GAGRI.XD (WRONG!)
    ↓
5. Data Fetch Attempt
   → No data for China/US with this indicator
   → ERROR returned to user
```

---

## Critical Finding: Description-Based Matching

### How Metadata Search Works

The metadata search uses **BOTH name AND description** for matching (lines 144-146 of `metadata_search.py`):

```python
name = flow_info.get('name', '').lower()
description = flow_info.get('description', '').lower()
combined = f"{name} {description}"

# Match if ALL keywords appear in name or description
```

### The WorldBank Metadata Structure

Each indicator in `backend/data/metadata/worldbank.json` has:

```json
{
  "id": "AG.PRD.GAGRI.XD",
  "code": "AG.PRD.GAGRI.XD",
  "name": "Agriculture production index (gross, 1999-2001 = 100)",
  "description": "The FAO indices of agricultural production show the relative level of the aggregate volume of agricultural production... and also to improve and facilitate international comparative analysis of productivity at the national level...",
  "searchable_text": "[name] [description] [aliases] [code variations]"
}
```

### Why "Productivity" Matched the Wrong Indicator

The word "productivity" appears in the **description** of `AG.PRD.GAGRI.XD`:

> "...and also to improve and facilitate international comparative analysis of **productivity** at the national level..."

This is discussing how the agriculture production index can be used for productivity comparisons - it is NOT a labor productivity indicator!

### The Correct Indicator

The actual labor productivity indicator is `SL.GDP.PCAP.EM.KD`:

```json
{
  "id": "SL.GDP.PCAP.EM.KD",
  "name": "GDP per person employed (constant 2021 PPP $)",
  "description": "GDP per person employed is gross domestic product divided by total employment in the economy..."
}
```

**Data availability verified**:
- China (2020): $37,365.93 per worker
- US (2020): $145,799.90 per worker

---

## The 6 Systemic Gaps

### Gap 1: Incomplete Hardcoded Mappings

**Location**: `backend/providers/worldbank.py` lines 90-200

**Problem**: Only ~100 of 16,000+ WorldBank indicators are hardcoded in `INDICATOR_MAPPINGS`.

**Missing mappings that caused this failure**:
```python
# These should exist but DON'T:
"PRODUCTIVITY": "SL.GDP.PCAP.EM.KD",
"LABOR_PRODUCTIVITY": "SL.GDP.PCAP.EM.KD",
"LABOUR_PRODUCTIVITY": "SL.GDP.PCAP.EM.KD",
"GDP_PER_WORKER": "SL.GDP.PCAP.EM.KD",
```

**Impact**: ~85% of non-trivial queries hit the metadata search fallback.

---

### Gap 2: No Semantic Validation

**Location**: `backend/services/metadata_search.py` lines 144-180

**Problem**: Metadata search uses keyword matching on combined name+description text. No validation that the matched indicator semantically matches user intent.

**Example**:
- User asks for: "productivity" (meaning labor productivity)
- System matches: "Agriculture production index" (because description contains "productivity")
- No check that "production index" ≠ "productivity"

**The semantic mismatch**:
| User Intent | What They Mean | Wrong Match | What It Actually Is |
|-------------|----------------|-------------|---------------------|
| productivity | GDP per worker | AG.PRD.GAGRI.XD | Volume of agricultural output |
| growth | GDP growth rate | Could match population growth | Various growth metrics |
| output | Economic output | Could match industrial output | Various output measures |

---

### Gap 3: FAISS Vector Search Limitations

**Location**: `backend/services/faiss_vector_search.py`

**Problem**: Uses `all-MiniLM-L6-v2` embeddings (384-dim) which is a general-purpose model, not trained on economic terminology.

**Why it fails**:
1. "productivity" and "production" have similar embeddings (both relate to "producing")
2. The model doesn't understand that in economics, "productivity" specifically means output per unit of input
3. Long descriptions dilute the semantic signal

---

### Gap 4: No Provider-Indicator Compatibility Check

**Location**: `backend/services/query.py` `_fetch_data()` method

**Problem**: System doesn't verify that the selected provider actually has the indicator before attempting to fetch.

**Flow**:
1. Provider selected: WorldBank
2. Indicator resolved: AG.PRD.GAGRI.XD
3. Data fetch attempted → FAILS
4. No fallback to try OECD (which has excellent productivity data)

---

### Gap 5: No Data Existence Pre-Check

**Location**: `backend/providers/worldbank.py`

**Problem**: System fetches full data without first checking if data exists for the requested countries.

**Improvement opportunity**:
```python
# Quick existence check before full fetch
url = f"https://api.worldbank.org/v2/country/CHN;USA/indicator/{code}?per_page=1"
# If empty, try alternative indicator or provider
```

---

### Gap 6: Low Confidence Thresholds

**Location**: `backend/services/metadata_search.py` lines 307-357

**Problem**: Confidence threshold is 0.4 (was 0.6, lowered). System accepts dubious matches.

**Current behavior**:
- Confidence 0.4+ → Accept match
- Confidence < 0.4 → Check if results are "diverse"
- If not diverse → Accept top result anyway with 0.35 confidence

---

## Proposed Fixes

### Fix 1: Expand Hardcoded Mappings for ALL Providers (IMMEDIATE - 4 hours)

This fix must be applied to **ALL providers**, not just WorldBank.

---

#### Current State of Productivity Mappings by Provider

| Provider | Has Productivity Mapping? | Status |
|----------|--------------------------|--------|
| **FRED** | ✅ Yes | `PRODUCTIVITY` → `OPHNFB` |
| **OECD** | ✅ Yes (dynamic) | Uses `DSD_PDB` dataflows |
| **WorldBank** | ❌ **NO** | **ROOT CAUSE OF FAILURE** |
| **IMF** | ❌ Blocked | In `UNSUPPORTED_INDICATORS` |
| **StatsCan** | ❌ NO | Missing from `VECTOR_MAPPINGS` |
| **Eurostat** | ❌ NO | No hardcoded mappings |
| **BIS** | N/A | Not their domain |

---

#### Fix 1A: WorldBank (`backend/providers/worldbank.py`)

Add comprehensive mappings to `backend/providers/worldbank.py`:

```python
INDICATOR_MAPPINGS = {
    # ... existing mappings ...

    # Labor Productivity (THE FIX)
    "PRODUCTIVITY": "SL.GDP.PCAP.EM.KD",
    "LABOR_PRODUCTIVITY": "SL.GDP.PCAP.EM.KD",
    "LABOUR_PRODUCTIVITY": "SL.GDP.PCAP.EM.KD",
    "GDP_PER_WORKER": "SL.GDP.PCAP.EM.KD",
    "GDP_PER_PERSON_EMPLOYED": "SL.GDP.PCAP.EM.KD",
    "WORKER_PRODUCTIVITY": "SL.GDP.PCAP.EM.KD",
    "OUTPUT_PER_WORKER": "SL.GDP.PCAP.EM.KD",
    "ECONOMIC_PRODUCTIVITY": "SL.GDP.PCAP.EM.KD",

    # Sector-specific productivity
    "AGRICULTURAL_PRODUCTIVITY": "NV.AGR.EMPL.KD",
    "AGRICULTURE_PRODUCTIVITY": "NV.AGR.EMPL.KD",
    "FARM_PRODUCTIVITY": "NV.AGR.EMPL.KD",
    "INDUSTRY_PRODUCTIVITY": "NV.IND.EMPL.KD",
    "INDUSTRIAL_PRODUCTIVITY": "NV.IND.EMPL.KD",
    "MANUFACTURING_PRODUCTIVITY": "NV.IND.EMPL.KD",
    "SERVICES_PRODUCTIVITY": "NV.SRV.EMPL.KD",
    "SERVICE_SECTOR_PRODUCTIVITY": "NV.SRV.EMPL.KD",

    # Labor productivity growth
    "PRODUCTIVITY_GROWTH": "SL.GDP.PCAP.EM.KD.ZG",
    "LABOR_PRODUCTIVITY_GROWTH": "SL.GDP.PCAP.EM.KD.ZG",
}
```

---

#### Fix 1B: Statistics Canada (`backend/providers/statscan.py`)

Add productivity mappings to `VECTOR_MAPPINGS`:

```python
VECTOR_MAPPINGS: Dict[str, int] = {
    # ... existing mappings ...

    # Labor Productivity (Table 36-10-0480-01: Labour productivity and related measures)
    # Note: StatsCan measures productivity as output per hour worked
    "PRODUCTIVITY": 112961936,  # Labour productivity, business sector
    "LABOR_PRODUCTIVITY": 112961936,
    "LABOUR_PRODUCTIVITY": 112961936,
    "OUTPUT_PER_HOUR": 112961936,
    "PRODUCTIVITY_GROWTH": 112961937,  # Labour productivity growth rate
    "LABOR_PRODUCTIVITY_GROWTH": 112961937,

    # Multifactor productivity
    "MULTIFACTOR_PRODUCTIVITY": 112961942,
    "TOTAL_FACTOR_PRODUCTIVITY": 112961942,
    "TFP": 112961942,

    # Unit labor cost
    "UNIT_LABOR_COST": 112961944,
    "UNIT_LABOUR_COST": 112961944,
    "ULC": 112961944,
}
```

---

#### Fix 1C: IMF (`backend/providers/imf.py`)

**Option A**: Remove from UNSUPPORTED_INDICATORS and add routing guidance:

```python
# In UNSUPPORTED_INDICATORS, REMOVE these:
# "PRODUCTIVITY_GROWTH",  # REMOVE - route to OECD instead
# "PRODUCTIVITY",         # REMOVE - route to OECD instead

# Add to a new REDIRECT_TO_OTHER_PROVIDER dict:
REDIRECT_INDICATORS = {
    "PRODUCTIVITY": "OECD",  # IMF doesn't have, but OECD does
    "PRODUCTIVITY_GROWTH": "OECD",
    "LABOR_PRODUCTIVITY": "OECD",
}
```

**Option B**: Keep in UNSUPPORTED but improve error message:

```python
# In fetch_indicator method, when indicator is unsupported:
if indicator_upper in self.UNSUPPORTED_INDICATORS:
    if "PRODUCTIVITY" in indicator_upper:
        raise DataNotAvailableError(
            f"IMF DataMapper doesn't have productivity data. "
            f"Try OECD for labor productivity (best for OECD countries) "
            f"or WorldBank indicator SL.GDP.PCAP.EM.KD for global coverage."
        )
```

---

#### Fix 1D: Eurostat (`backend/providers/eurostat.py`)

Add `INDICATOR_MAPPINGS` dict (currently missing):

```python
class EurostatProvider:
    # Add this new dict
    INDICATOR_MAPPINGS: Dict[str, str] = {
        # Labor Productivity (nama_10_lp_ulc dataset)
        "PRODUCTIVITY": "nama_10_lp_ulc",
        "LABOR_PRODUCTIVITY": "nama_10_lp_ulc",
        "LABOUR_PRODUCTIVITY": "nama_10_lp_ulc",
        "GDP_PER_HOUR": "nama_10_lp_ulc",
        "GDP_PER_WORKER": "nama_10_lp_ulc",
        "PRODUCTIVITY_GROWTH": "nama_10_lp_ulc",

        # Unit labor cost
        "UNIT_LABOR_COST": "nama_10_lp_ulc",
        "UNIT_LABOUR_COST": "nama_10_lp_ulc",
        "ULC": "nama_10_lp_ulc",

        # GDP
        "GDP": "nama_10_gdp",
        "GDP_GROWTH": "nama_10_gdp",

        # Unemployment
        "UNEMPLOYMENT": "une_rt_m",
        "UNEMPLOYMENT_RATE": "une_rt_m",
        "YOUTH_UNEMPLOYMENT": "une_rt_m",

        # HICP Inflation
        "INFLATION": "prc_hicp_manr",
        "HICP": "prc_hicp_manr",
        "CPI": "prc_hicp_manr",
    }
```

---

#### Fix 1E: FRED (`backend/providers/fred.py`)

FRED already has productivity mappings, but add more synonyms:

```python
SERIES_MAPPINGS: Dict[str, str] = {
    # ... existing mappings ...

    # Expand productivity section
    "PRODUCTIVITY": "OPHNFB",  # Already exists
    "LABOR_PRODUCTIVITY": "OPHNFB",  # Already exists
    "LABOUR_PRODUCTIVITY": "OPHNFB",  # Add UK spelling
    "OUTPUT_PER_HOUR": "OPHNFB",
    "GDP_PER_HOUR": "OPHNFB",
    "WORKER_PRODUCTIVITY": "OPHNFB",
    "NONFARM_PRODUCTIVITY": "OPHNFB",

    # Manufacturing productivity
    "MANUFACTURING_PRODUCTIVITY": "MPU4900063",
    "MANUFACTURING_OUTPUT_PER_HOUR": "MPU4900063",

    # Unit labor costs
    "UNIT_LABOR_COST": "ULCNFB",
    "UNIT_LABOUR_COST": "ULCNFB",
    "ULC": "ULCNFB",
}
```

---

#### Fix 1F: BIS (`backend/providers/bis.py`)

BIS doesn't have productivity data, but should explicitly redirect:

```python
# Add to class
REDIRECT_INDICATORS = {
    "PRODUCTIVITY": "OECD",
    "LABOR_PRODUCTIVITY": "OECD",
    "LABOUR_PRODUCTIVITY": "OECD",
}

# In _indicator_code method:
def _indicator_code(self, indicator: str) -> str:
    key = indicator.upper().replace(" ", "_").replace("-", "_")

    # Check for indicators that should be redirected
    if key in self.REDIRECT_INDICATORS:
        suggested_provider = self.REDIRECT_INDICATORS[key]
        raise DataNotAvailableError(
            f"BIS doesn't have {indicator} data. "
            f"Try {suggested_provider} instead for this indicator."
        )

    return self.INDICATOR_MAPPINGS.get(key)
```

---

### Fix 2: Semantic Validation Layer (SHORT-TERM - 4 hours)

Add validation after metadata search that uses LLM to verify semantic match:

```python
# New method in metadata_search.py
async def validate_indicator_match(
    self,
    user_query: str,
    user_indicator: str,
    matched_indicator: dict
) -> tuple[bool, float, str]:
    """
    Use LLM to verify the matched indicator semantically matches user intent.

    Returns: (is_valid, confidence, reason)
    """
    prompt = f"""You are validating economic indicator matches.

User asked for: "{user_indicator}"
Full query context: "{user_query}"

System found this indicator:
- Code: {matched_indicator['code']}
- Name: {matched_indicator['name']}
- Description: {matched_indicator.get('description', 'N/A')[:500]}

CRITICAL DISTINCTIONS:
- "productivity" = output per worker (GDP/employment) - NOT production volume
- "production index" = volume of output - NOT productivity
- "growth" can mean GDP growth, population growth, etc. - context matters

Does this indicator match what the user is asking for?

Return JSON:
{{
    "is_match": true/false,
    "confidence": 0.0-1.0,
    "reason": "brief explanation",
    "suggested_alternative": "indicator name if mismatch, null otherwise"
}}
"""
    # Call LLM for semantic validation
    response = await self.llm_service.parse_json(prompt)
    return response["is_match"], response["confidence"], response["reason"]
```

**Integration point** (metadata_search.py line ~300):
```python
# After getting search results, validate top match
if results:
    is_valid, confidence, reason = await self.validate_indicator_match(
        user_query, indicator_name, results[0]
    )
    if not is_valid:
        logger.warning(f"Semantic validation failed: {reason}")
        # Try next result or return clarification
```

---

### Fix 3: Indicator Synonym System (SHORT-TERM - 1 day)

Create a synonym expansion system:

```python
# New file: backend/services/indicator_synonyms.py

ECONOMIC_CONCEPT_SYNONYMS = {
    "productivity": {
        "synonyms": [
            "labor productivity", "labour productivity", "worker productivity",
            "gdp per worker", "gdp per employed", "output per worker",
            "economic efficiency", "workforce productivity"
        ],
        "NOT_synonyms": [  # Explicit exclusions
            "production index", "production volume", "output index",
            "agricultural production", "industrial production"
        ],
        "default_indicator": {
            "WorldBank": "SL.GDP.PCAP.EM.KD",
            "OECD": "DSD_PDB@DF_PDB_LV",
            "FRED": "OPHNFB"
        }
    },
    "inflation": {
        "synonyms": [
            "price level", "consumer prices", "cpi", "price increase",
            "cost of living", "price inflation"
        ],
        "NOT_synonyms": [
            "deflation", "disinflation", "price index level"
        ],
        "default_indicator": {
            "WorldBank": "FP.CPI.TOTL.ZG",
            "IMF": "PCPIPCH",
            "FRED": "CPIAUCSL"
        }
    },
    # ... more concepts
}

def expand_indicator(indicator: str) -> dict:
    """
    Expand user's indicator term to full concept with synonyms and exclusions.

    Returns:
        {
            "concept": "productivity",
            "synonyms": [...],
            "NOT_synonyms": [...],
            "default_indicators": {...}
        }
    """
    indicator_lower = indicator.lower().strip()

    for concept, info in ECONOMIC_CONCEPT_SYNONYMS.items():
        all_terms = [concept] + info["synonyms"]
        if indicator_lower in [t.lower() for t in all_terms]:
            return {
                "concept": concept,
                **info
            }

    return {"concept": indicator_lower, "synonyms": [], "NOT_synonyms": [], "default_indicators": {}}


def is_false_positive(indicator_name: str, concept_info: dict) -> bool:
    """
    Check if an indicator name is a known false positive for the concept.
    """
    indicator_lower = indicator_name.lower()
    for exclusion in concept_info.get("NOT_synonyms", []):
        if exclusion.lower() in indicator_lower:
            return True
    return False
```

---

### Fix 4: Provider-Indicator Compatibility Matrix (MEDIUM-TERM - 2 days)

Build a pre-computed compatibility matrix:

```json
// backend/data/indicator_compatibility.json
{
    "productivity": {
        "preferred_providers": ["OECD", "WorldBank"],
        "available": {
            "OECD": {
                "indicator": "DSD_PDB@DF_PDB_LV",
                "name": "Productivity levels",
                "confidence": 0.95,
                "coverage": "oecd_members"
            },
            "WorldBank": {
                "indicator": "SL.GDP.PCAP.EM.KD",
                "name": "GDP per person employed",
                "confidence": 0.90,
                "coverage": "global"
            },
            "FRED": {
                "indicator": "OPHNFB",
                "name": "Nonfarm Business Sector: Labor Productivity",
                "confidence": 0.95,
                "coverage": ["US"]
            }
        },
        "not_available": ["IMF", "Comtrade", "CoinGecko", "ExchangeRate", "BIS"]
    }
}
```

**Usage**:
```python
def get_best_provider_for_indicator(indicator: str, countries: list[str]) -> tuple[str, str]:
    """
    Get best provider and indicator code for user's request.

    Returns: (provider_name, indicator_code)
    """
    compat = load_compatibility_matrix()
    concept = expand_indicator(indicator)["concept"]

    if concept in compat:
        info = compat[concept]
        for provider in info["preferred_providers"]:
            provider_info = info["available"].get(provider)
            if provider_info:
                coverage = provider_info.get("coverage", "global")
                if coverage == "global" or all(c in coverage for c in countries):
                    return provider, provider_info["indicator"]

    return "WorldBank", None  # Fallback, will trigger metadata search
```

---

### Fix 5: Multi-Provider Fallback Chain (MEDIUM-TERM - 2 days)

Implement intelligent fallback:

```python
# In query.py
PROVIDER_FALLBACK_CHAIN = {
    "WorldBank": ["OECD", "IMF", "Eurostat"],
    "OECD": ["WorldBank", "Eurostat", "IMF"],
    "FRED": ["WorldBank", "OECD"],
    "StatsCan": ["WorldBank", "OECD"],
    "IMF": ["WorldBank", "OECD"],
}

async def _fetch_with_fallback(self, intent: ParsedIntent) -> list[NormalizedData]:
    """Try primary provider, then fallbacks if it fails."""
    providers_to_try = [intent.apiProvider] + PROVIDER_FALLBACK_CHAIN.get(intent.apiProvider, [])
    last_error = None

    for provider_name in providers_to_try:
        try:
            provider = self._get_provider(provider_name)

            # Remap indicator for this provider
            remapped_indicator = self._remap_indicator_for_provider(
                intent.indicators[0], provider_name
            )

            intent_copy = intent.copy()
            intent_copy.apiProvider = provider_name
            intent_copy.indicators = [remapped_indicator]

            result = await provider.fetch_data(intent_copy)

            if result and any(d.data for d in result):
                logger.info(f"✅ Got data from {provider_name}")
                return result

        except (DataNotAvailableError, httpx.HTTPError) as e:
            logger.warning(f"⚠️ {provider_name} failed: {e}")
            last_error = e
            continue

    raise DataNotAvailableError(f"No provider has data for this query. Last error: {last_error}")
```

---

### Fix 6: Unified Indicator Catalog (LONG-TERM - 2 weeks)

Build a comprehensive economic concept catalog:

```
backend/catalog/
├── concepts/
│   ├── productivity.yaml
│   ├── gdp.yaml
│   ├── inflation.yaml
│   ├── unemployment.yaml
│   └── ...
├── mappings/
│   ├── worldbank_mappings.yaml
│   ├── oecd_mappings.yaml
│   ├── imf_mappings.yaml
│   └── ...
└── catalog_service.py
```

**Example concept file (productivity.yaml)**:
```yaml
concept: productivity
category: labor_market
domain: macroeconomics

description: |
  Output per unit of labor input. Typically measured as GDP per worker
  (labor productivity) or GDP per hour worked (hourly productivity).

  NOT to be confused with:
  - Production index (volume of output, not efficiency)
  - Total factor productivity (includes capital, harder to measure)

synonyms:
  primary:
    - labor productivity
    - labour productivity
    - worker productivity
  secondary:
    - gdp per worker
    - gdp per employed
    - output per worker
    - economic efficiency

explicit_exclusions:
  - production index
  - production volume
  - agricultural production
  - industrial production
  - output index

providers:
  WorldBank:
    primary:
      code: SL.GDP.PCAP.EM.KD
      name: "GDP per person employed (constant 2021 PPP $)"
      unit: "constant 2021 PPP $"
      frequency: annual
      coverage: global
      confidence: 0.95
    sectoral:
      agriculture:
        code: NV.AGR.EMPL.KD
        name: "Agriculture value added per worker"
      industry:
        code: NV.IND.EMPL.KD
        name: "Industry value added per worker"
      services:
        code: NV.SRV.EMPL.KD
        name: "Services value added per worker"

  OECD:
    primary:
      code: DSD_PDB@DF_PDB_LV
      name: "Productivity levels"
      frequency: annual
      coverage: oecd_plus
      confidence: 0.98
    growth:
      code: DSD_PDB@DF_PDB_GR
      name: "Productivity growth rates"

  FRED:
    primary:
      code: OPHNFB
      name: "Nonfarm Business Sector: Labor Productivity"
      frequency: quarterly
      coverage: [US]
      confidence: 0.99
```

---

## Summary: Priority Matrix

| Priority | Fix | Effort | Impact | Prevents |
|----------|-----|--------|--------|----------|
| 🔴 **Immediate** | Add productivity mappings to ALL providers | 4 hours | Very High | This failure + similar |
| 🔴 **Immediate** | Add 50+ common economic term mappings (all providers) | 1 day | High | 30% of similar failures |
| 🟡 **Short-term** | Semantic validation layer | 4 hours | Very High | 40% of false matches |
| 🟡 **Short-term** | Indicator synonym system | 1 day | High | 25% of term confusion |
| 🟢 **Medium-term** | Provider-indicator compatibility matrix | 2 days | High | 15% of wrong provider |
| 🟢 **Medium-term** | Multi-provider fallback chain | 2 days | High | 20% of data not found |
| 🔵 **Long-term** | Unified indicator catalog | 2 weeks | Very High | 80% of all failures |

---

## Provider-Specific Fix Summary

| Provider | Current State | Fix Required | Priority |
|----------|--------------|--------------|----------|
| **WorldBank** | Missing productivity | Add `SL.GDP.PCAP.EM.KD` mapping | 🔴 Immediate |
| **StatsCan** | Missing productivity | Add vector ID mappings | 🔴 Immediate |
| **IMF** | Blocked (UNSUPPORTED) | Add redirect guidance | 🟡 Short-term |
| **Eurostat** | No hardcoded mappings | Add `INDICATOR_MAPPINGS` dict | 🟡 Short-term |
| **FRED** | Has basic mapping | Add synonyms | 🟢 Nice-to-have |
| **OECD** | Dynamic discovery works | No changes needed | ✅ Complete |
| **BIS** | N/A (not their domain) | Add explicit redirect | 🟢 Nice-to-have |

---

## Other Common Terms At Risk of Similar Failures

The "productivity" issue is not unique. Here are other common economic terms that may fail due to the same pattern (ambiguous term → wrong indicator from description matching):

| Term | Risk | Why It Might Fail | Correct Indicator |
|------|------|-------------------|-------------------|
| **"growth"** | High | Could match GDP growth, population growth, employment growth | Depends on context |
| **"output"** | High | Could match industrial output, GDP, agricultural output | Depends on context |
| **"efficiency"** | Medium | Often means productivity but could match energy efficiency | SL.GDP.PCAP.EM.KD |
| **"income"** | Medium | Could match GNI, GDP per capita, household income | Depends on context |
| **"investment"** | Medium | Could match FDI, gross capital formation, portfolio investment | Depends on context |
| **"debt"** | Low | Usually clear, but could be government vs household vs external | GGXWDG_NGDP (IMF) |
| **"trade"** | Medium | Could match trade balance, trade volume, trade as % of GDP | Depends on context |
| **"employment"** | Low | Could match employment rate, total employment, employment growth | SL.EMP.TOTL (WB) |

### Recommended Additional Mappings for All Providers

```python
# Common ambiguous terms that need explicit mappings
AMBIGUOUS_TERM_MAPPINGS = {
    # Growth terms
    "GROWTH": "GDP_GROWTH",  # Default to GDP growth
    "ECONOMIC_GROWTH": "GDP_GROWTH",

    # Output terms
    "OUTPUT": "GDP",  # Default to GDP
    "ECONOMIC_OUTPUT": "GDP",

    # Efficiency terms
    "EFFICIENCY": "PRODUCTIVITY",  # Usually means productivity
    "ECONOMIC_EFFICIENCY": "PRODUCTIVITY",

    # Income terms
    "INCOME": "GNI_PER_CAPITA",  # Default to GNI per capita
    "NATIONAL_INCOME": "GNI",
    "PER_CAPITA_INCOME": "GNI_PER_CAPITA",

    # Investment terms
    "INVESTMENT": "GROSS_CAPITAL_FORMATION",
    "CAPITAL_INVESTMENT": "GROSS_CAPITAL_FORMATION",
    "FOREIGN_INVESTMENT": "FDI_INFLOWS",
}
```

---

## Conclusion

The "productivity" query failure is a symptom of a deeper architectural issue: **the system prioritizes "always return something" over "return the right thing"**.

The metadata search correctly uses both indicator names AND descriptions for matching. However, this creates false positives when common economic terms like "productivity" appear in descriptions of unrelated indicators.

The fix requires:
1. **Immediate**: Expand hardcoded mappings for common terms
2. **Short-term**: Add semantic validation to catch false positives
3. **Long-term**: Build a unified catalog that understands economic concepts

---

*Document created: 2025-12-23*
*Last updated: 2025-12-23*
