# Indicator Resolution System Architecture

## Overview

The indicator resolution system helps map user queries (e.g., "productivity") to the correct provider-specific indicator codes (e.g., `SL.GDP.PCAP.EM.KD` for WorldBank).

## Components

### 1. Hardcoded Provider Mappings (Fix 1) ✅ ACTIVE

Located in each provider file:
- `providers/worldbank.py` - `INDICATOR_MAPPINGS`
- `providers/fred.py` - `INDICATOR_MAPPINGS`
- `providers/eurostat.py` - `DATASET_MAPPINGS`
- `providers/bis.py` - `REDIRECT_INDICATORS`
- `providers/imf.py` - Raises `DataNotAvailableError` with helpful suggestions
- `providers/statscan.py` - `INDICATOR_MAPPINGS` (uses `None` for dynamic discovery)

**Status**: ✅ Fully integrated and working

### 2. Semantic Validation Layer (Fix 2) ⚠️ CREATED BUT NOT INTEGRATED

File: `services/metadata_search.py`
Method: `validate_indicator_match()`

**Purpose**: Uses LLM to verify that matched indicators semantically match user intent. Prevents false positives like returning "production index" for "productivity".

**Status**: ⚠️ Method exists but is NOT called anywhere. Integration needed.

**Integration Point**: Should be called after metadata search returns results, before the provider uses them.

### 3. Indicator Synonym System (Fix 3) ✅ ACTIVE

File: `services/indicator_synonyms.py`

**Purpose**: Provides synonyms and exclusions for economic concepts.

**Key Functions**:
- `expand_indicator(indicator)` - Get full concept info
- `is_false_positive(name, concept_info)` - Check if result is a known false positive
- `find_concept_by_term(term)` - Find canonical concept name
- `get_default_indicator(concept, provider)` - Get default indicator code

**Status**: ✅ Created and used by `indicator_compatibility.py`

### 4. Provider-Indicator Compatibility Matrix (Fix 4) ✅ ACTIVE

Files:
- `data/indicator_compatibility.json` - The compatibility matrix data
- `services/indicator_compatibility.py` - Service to use the matrix

**Purpose**: Maps concepts to best providers and indicator codes with confidence scores.

**Key Functions**:
- `get_best_provider_for_indicator(indicator, countries)` - Get best provider
- `get_fallback_providers(indicator, exclude)` - Get fallback list
- `is_indicator_available(indicator, provider)` - Check availability
- `get_indicator_code(indicator, provider)` - Get specific code

**Status**: ✅ Integrated into `query.py` for fallback provider selection

### 5. Multi-Provider Fallback Chain (Fix 5) ✅ ACTIVE

File: `services/query.py`
Method: `_get_fallback_providers()`

**Purpose**: Provides intelligent fallback when primary provider fails, using both:
1. General fallback chains (based on provider relationships)
2. Indicator-specific fallbacks from compatibility matrix

**Status**: ✅ Fully integrated

### 6. Unified Indicator Catalog (Fix 6) ⚠️ CREATED BUT NOT INTEGRATED

Files:
- `catalog/concepts/productivity.yaml` - Concept definition in YAML
- `services/catalog_service.py` - Service to load YAML catalog

**Purpose**: Provides a single source of truth for indicator definitions in human-readable YAML format.

**Status**: ⚠️ Service exists but is NOT imported or used anywhere. Only 1 concept file exists (productivity.yaml).

## Data Flow

```
User Query: "What is productivity in China?"
           ↓
    [LLM Parser] → ParsedIntent(indicator="productivity", provider="WORLDBANK")
           ↓
    [Provider Mapping Check] ← Fix 1: Hardcoded mappings
           ↓
    If found: Use hardcoded code (SL.GDP.PCAP.EM.KD)
    If not found: ↓
           ↓
    [Metadata Search] → search_worldbank("productivity")
           ↓
    [TODO: Validation] ← Fix 2: validate_indicator_match() NOT INTEGRATED
           ↓
    [Return Results]
           ↓
    If provider fails: [Fallback Chain] ← Fix 4 & 5: Compatibility matrix
```

## Known Issues / TODOs

### High Priority

1. **`validate_indicator_match()` not integrated**
   - Location: `services/metadata_search.py:455`
   - Should be called after metadata search returns results
   - Would prevent false positives from metadata search

2. **`catalog_service.py` not integrated**
   - Service is complete but not used
   - Could consolidate with `indicator_synonyms.py` and `indicator_compatibility.json`

### Medium Priority

3. **Duplication between synonym systems**
   - `indicator_synonyms.py` uses `NOT_synonyms`
   - `productivity.yaml` uses `explicit_exclusions`
   - Should be consolidated

4. **Missing YAML catalog files**
   - Only `productivity.yaml` exists
   - Other 16 concepts need YAML files for catalog to be useful

### Low Priority

5. **Architecture simplification**
   - Currently 4 sources of indicator information
   - Consider consolidating to YAML catalog as single source of truth
   - Generate JSON/Python from YAML via build script

## Testing

Test the system:

```bash
# Test productivity query
curl -X POST http://localhost:3001/api/query \
  -H "Content-Type: application/json" \
  -d '{"query": "What is the productivity in China and US?"}'

# Expected: Returns SL.GDP.PCAP.EM.KD (GDP per person employed)
# NOT: AG.PRD.GAGRI.XD (Agricultural Production Index)
```

## Files Reference

| File | Purpose | Status |
|------|---------|--------|
| `providers/*.py` | Hardcoded mappings | ✅ Active |
| `services/indicator_synonyms.py` | Synonym system | ✅ Active |
| `data/indicator_compatibility.json` | Compatibility matrix | ✅ Active |
| `services/indicator_compatibility.py` | Compatibility service | ✅ Active |
| `services/query.py` | Fallback chain | ✅ Active |
| `services/metadata_search.py` | Search + validation | ⚠️ Validation not called |
| `services/catalog_service.py` | YAML catalog | ⚠️ Not integrated |
| `catalog/concepts/*.yaml` | Concept definitions | ⚠️ Only 1 file exists |
