# FRED Series ID Validation Fix - Summary

## Problem

FRED API requires series IDs to be ≤25 alphanumeric characters, but the system was passing indicator names like:
- "GDP growth"
- "unemployment_rate"
- "10_YEAR_TREASURY"
- "MORTGAGE_RATE"
- "consumer_confidence_index"
- "retail_sales_growth"

These were being rejected by FRED API with:
```
Bad Request. Invalid value for variable series_id. Series IDs should be 25 or less alphanumeric characters.
```

## Root Cause

The LLM query parser returns natural language indicator names in the `indicators` array (e.g., "GDP growth", "unemployment rate"). The FRED provider's `_series_id()` method was:

1. Only doing basic normalization (uppercase, replace spaces with underscores)
2. Not handling all naming variations (e.g., "GDP growth" vs "GDP_GROWTH" vs "gdp growth")
3. Falling back to passing through the raw indicator name, which often violated FRED API constraints

## Solution

Enhanced `/home/hanlulong/econ-data-mcp/backend/providers/fred.py` with:

### 1. Expanded Indicator Mappings

Added missing mappings to `SERIES_MAPPINGS`:
- `"INFLATION_RATE"` → `"CPIAUCSL"`
- `"RETAIL_SALES_GROWTH"` → `"RSXFS"`

### 2. Improved `_series_id()` Method

The method now:

1. **Handles multiple name formats**:
   - Natural language: "GDP growth" → "A191RL1Q225SBEA"
   - Underscore format: "GDP_GROWTH" → "A191RL1Q225SBEA"
   - Mixed case: "Gdp Growth" → "A191RL1Q225SBEA"

2. **Smart normalization**:
   - Converts spaces to underscores preserving common patterns
   - Strips whitespace
   - Handles case variations

3. **Fuzzy matching with variations**:
   - Tries exact match first
   - Falls back to common variations (removing "_RATE", "_GROWTH", "_INDEX")
   - Adds "_RATE" suffix for indicators like "UNEMPLOYMENT"

4. **Validation before fallback**:
   - Only passes through if ≤25 chars and alphanumeric
   - Otherwise raises helpful `DataNotAvailableError` with guidance

5. **Better error messages**:
   ```
   Unknown FRED indicator: 'xyz'.
   Please use a known indicator name (e.g., 'GDP', 'unemployment', 'inflation', 'housing starts')
   or provide an explicit FRED series ID via the 'seriesId' parameter.
   See https://fred.stlouisfed.org for available series.
   ```

## Testing

### Unit Tests Added

Added comprehensive tests in `/home/hanlulong/econ-data-mcp/backend/tests/test_providers.py`:

1. **`test_fred_series_id_mapping()`**: Tests 22 indicator variations including:
   - Natural language with spaces
   - Underscore format
   - Short forms
   - Case variations
   - Direct series IDs

2. **`test_fred_series_id_explicit_override()`**: Verifies explicit `seriesId` parameter overrides indicator mapping

### Test Results

All FRED tests passing:
```
test_fred_series_id_mapping ... ok
test_fred_series_id_explicit_override ... ok
test_fred_fetch_series ... ok
```

### Validation Results

Verified all 94 series IDs in mapping:
- ✅ All IDs are ≤25 characters
- ✅ All IDs are alphanumeric (with underscores)
- ✅ All problematic cases now map correctly

## Common Mappings Reference

| Indicator Name | FRED Series ID |
|----------------|----------------|
| GDP | GDP |
| GDP growth | A191RL1Q225SBEA |
| Unemployment / Unemployment rate | UNRATE |
| Federal funds rate | FEDFUNDS |
| 10-year Treasury | DGS10 |
| 30-year mortgage | MORTGAGE30US |
| Consumer confidence index | UMCSENT |
| Retail sales / Retail sales growth | RSXFS |
| Inflation / CPI | CPIAUCSL |
| Core CPI | CPILFESL |
| Housing starts | HOUST |
| Industrial production | INDPRO |

See `backend/providers/fred.py` `SERIES_MAPPINGS` for complete list of 94 indicators.

## Files Modified

1. `/home/hanlulong/econ-data-mcp/backend/providers/fred.py`
   - Enhanced `_series_id()` method with smart normalization and fuzzy matching
   - Added missing mappings (`INFLATION_RATE`, `RETAIL_SALES_GROWTH`)

2. `/home/hanlulong/econ-data-mcp/backend/tests/test_providers.py`
   - Added `test_fred_series_id_mapping()` test
   - Added `test_fred_series_id_explicit_override()` test

## Impact

This fix resolves FRED API validation errors by ensuring all indicator names are properly mapped to valid FRED series IDs. The system now:

1. **Handles natural language queries** like "show me GDP growth" correctly
2. **Supports multiple naming conventions** from the LLM parser
3. **Provides helpful error messages** when unknown indicators are requested
4. **Maintains backward compatibility** with explicit series IDs

## Future Improvements

Consider implementing:

1. **Metadata search for FRED**: Similar to World Bank, StatsCan, IMF providers
2. **Fuzzy search API**: Use FRED's search API to discover series by keyword
3. **Category browsing**: Allow users to explore FRED categories (e.g., "show me all housing indicators")
4. **Series suggestions**: When indicator not found, suggest similar indicators

## Related Documentation

- FRED API Documentation: https://fred.stlouisfed.org/docs/api/fred/
- econ-data-mcp Provider Architecture: `backend/providers/README.md`
- Testing Guide: `docs/guides/testing.md`
