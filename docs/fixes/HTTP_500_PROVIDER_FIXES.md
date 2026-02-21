# HTTP 500 Errors Fix - IMF and World Bank Providers

## Overview

Fixed HTTP 500 crashes in IMF and World Bank providers caused by:
1. Missing country code mappings (IMF)
2. Invalid country code format (World Bank)

All specified test queries now complete successfully.

## Root Cause Analysis

### IMF Provider Issue
The IMF DataMapper API expects ISO3 country codes (ESP, PRT, SWE, HRV), but the provider's `COUNTRY_MAPPINGS` dictionary did not include mappings for Spain, Portugal, Sweden, and Croatia.

When users queried these countries, the provider would:
1. Receive country name (e.g., "Spain")
2. Use default fallback: `.upper()` → "SPAIN"
3. Submit "SPAIN" to IMF API which expects "ESP"
4. Receive empty results
5. Throw RuntimeError with HTTP 500

### World Bank Provider Issue
The World Bank API expects ISO2 or ISO3 codes (ID, IDN), but the provider was returning full country names (INDONESIA).

When users queried Indonesia:
1. Receive country name "Indonesia"
2. Fallback mapping returns "INDONESIA"
3. Submit "INDONESIA" to World Bank API
4. API rejects with "Invalid value" error
5. Throw DataNotAvailableError with HTTP 500

## Solutions Implemented

### 1. IMF Provider Fixes

**File**: `backend/providers/imf.py`

#### Added Country Code Mappings
```python
COUNTRY_MAPPINGS: Dict[str, str] = {
    # ... existing mappings ...

    # European countries (NEW)
    "SPAIN": "ESP",
    "ES": "ESP",
    "PORTUGAL": "PRT",
    "PT": "PRT",
    "SWEDEN": "SWE",
    "SE": "SWE",
    "CROATIA": "HRV",
    "HR": "HRV",
    # ... more countries ...
}
```

#### Enhanced Error Messages
When a country has no data, the error message now includes:
- Sample available countries from the API response
- List of countries that were available in the response

**Before**:
```
RuntimeError: No data found for any of the requested countries in IMF indicator NGDP_RPCH
```

**After**:
```
RuntimeError: No data found for any of the requested countries in IMF indicator NGDP_RPCH.
The requested countries may not have data available for this indicator.
Sample available countries: ABW, ADVEC, AFG, AFQ, AGO, ALB, AND, APQ, ARE, ARG, ARM, AS5, ATG, AUS, AUT...
```

### 2. World Bank Provider Fixes

**File**: `backend/providers/worldbank.py`

#### Fixed Country Code Mappings
Changed from full country names to ISO2 codes that World Bank API accepts:

```python
COUNTRY_MAPPINGS: Dict[str, str] = {
    # ISO2 codes (World Bank API accepts both ISO2 and ISO3)
    "DE": "DE",
    "FR": "FR",
    "JP": "JP",
    "CN": "CN",
    "IN": "IN",
    "CA": "CA",
    "BR": "BR",
    "RU": "RU",
    "AU": "AU",
    "ES": "ES",
    "PT": "PT",
    "SE": "SE",
    "HR": "HR",
    "ID": "ID",

    # Country names to ISO2 codes
    "GERMANY": "DE",
    "FRANCE": "FR",
    "JAPAN": "JP",
    "CHINA": "CN",
    "INDIA": "IN",
    "CANADA": "CA",
    "BRAZIL": "BR",
    "RUSSIA": "RU",
    "AUSTRALIA": "AU",
    "SPAIN": "ES",
    "PORTUGAL": "PT",
    "SWEDEN": "SE",
    "CROATIA": "HR",
    "INDONESIA": "ID",
}
```

#### Added Logging
```python
import logging
logger = logging.getLogger(__name__)
```

#### Added Comprehensive Error Handling
```python
async with httpx.AsyncClient(timeout=30.0, headers=headers) as client:
    for country_code_raw in country_list:
        try:
            country_code = self._country_code(country_code_raw)
            # ... API request ...

            # Check for error messages from World Bank API
            if isinstance(payload, list) and len(payload) > 0:
                if isinstance(payload[0], dict) and "message" in payload[0]:
                    error_msg = payload[0]["message"]
                    if isinstance(error_msg, list) and len(error_msg) > 0:
                        error_detail = error_msg[0].get("value", "Unknown error")
                        logger.warning(
                            f"World Bank API error for country {country_code_raw} ({country_code}) "
                            f"indicator {indic}: {error_detail}. Skipping this country."
                        )
                        continue

        except httpx.HTTPError as e:
            logger.warning(
                f"HTTP error fetching data for {country_code_raw} ({country_code}): {e}. "
                f"Skipping this country."
            )
            continue
        except Exception as e:
            logger.warning(
                f"Error processing {country_code_raw}: {e}. Skipping this country."
            )
            continue

# If no results found, provide helpful error message
if not results:
    raise DataNotAvailableError(
        f"No data found for any of the requested countries for indicator {indic}. "
        f"The data may not be available for the specified countries or indicator."
    )
```

**Key Features**:
- Try-catch around each country's request (graceful degradation)
- Logs detailed error information for each country
- Skips countries with errors and continues processing others
- Returns error only if ALL countries fail

## Test Results

All previously failing queries now work correctly:

### IMF Provider
| Query | Country | Indicator | Records | Latest Value | Status |
|-------|---------|-----------|---------|--------------|--------|
| Spain GDP growth | Spain | GDP | 51 | 1.6% | ✓ PASS |
| Portugal debt to GDP | Portugal | GOVT_DEBT | 41 | 77.4% | ✓ PASS |
| Sweden unemployment | Sweden | UNEMPLOYMENT | 51 | 7.9% | ✓ PASS |
| Croatia current account | Croatia | CURRENT_ACCOUNT | 39 | -0.7% | ✓ PASS |

### World Bank Provider
| Query | Country | Indicator | Records | Latest Value | Status |
|-------|---------|-----------|---------|--------------|--------|
| Indonesia economic growth | Indonesia | GDP_GROWTH | 64 | 5.03% | ✓ PASS |

## Error Handling Strategy

### Before Fixes
```
HTTP 500 → User gets generic server error → No data available → Poor UX
```

### After Fixes
```
Country Code Mapping → Correct ISO Code → API Request → Graceful Error Handling
                                              ↓
                          Response Error → Log & Skip → Continue Processing
                                              ↓
                               Partial Data → Return ✓ | All Failed → Return Error
```

## Provider Tests
All existing provider tests pass:
```
backend/tests/test_providers.py::ProviderTests::test_imf_metadata_discovery PASSED
backend/tests/test_providers.py::ProviderTests::test_worldbank_fetch_indicator PASSED
backend/tests/test_providers.py::ProviderTests::test_worldbank_metadata_discovery PASSED
```

## Files Modified

1. **backend/providers/imf.py**
   - Added country mappings for Spain, Portugal, Sweden, Croatia
   - Enhanced error messages with available countries list
   - 36 lines added/modified

2. **backend/providers/worldbank.py**
   - Added logging import
   - Fixed country code mappings (name → ISO2)
   - Added try-catch error handling for each country
   - Added final "no results" error check
   - 69 lines added/modified

## Deployment Notes

### No Breaking Changes
- Country code mappings are backward compatible
- Existing queries continue to work
- Only adds new functionality

### Performance
- No additional API calls
- Error handling happens locally
- Graceful degradation (skip failed countries, continue processing)

### Monitoring
Monitor logs for:
```
World Bank API error for country {country} ({code}) indicator {indic}: {error}
HTTP error fetching data for {country} ({code}): {error}
Error processing {country}: {error}
```

These indicate countries where data retrieval failed, but the query completes successfully with partial results.

## Future Improvements

1. **Country Code Database**: Create a comprehensive country code mapping including:
   - ISO2, ISO3, numeric codes
   - Common country names and abbreviations
   - Multiple languages

2. **Validation Layer**: Validate country codes before making API requests
   - Cache valid codes from each provider
   - Return clarification questions for invalid codes

3. **Monitoring Dashboard**: Track which countries fail most often
   - Identify provider API issues
   - Prioritize provider integrations

4. **User Feedback**: Detect partial failures and inform users:
   - "Data available for X of Y countries"
   - Show which countries had no data
