# API Quick Reference Guide

**Purpose**: Quick reference for implementing comprehensive test cases
**Last Updated**: 2024-11-02

## Date Format Summary

| Provider | Date Format | Multi-Date Ranges | Example |
|----------|-------------|-------------------|---------|
| **FRED** | `YYYY-MM-DD` | Yes (start/end params) | `2024-01-01` |
| **WorldBank** | `YYYY`, `YYYY:YYYY` (range with colon) | Yes (semicolon separated) | `2020:2024` |
| **UN Comtrade** | `YYYY` (annual), `YYYYMM` (monthly) | Yes (12 year max for annual, 1 year for monthly) | `2024`, `202401` |
| **CoinGecko** | ISO `YYYY-MM-DD` or UNIX timestamp | Yes (from/to params) | `2024-01-01` |
| **Statistics Canada** | ISO 8601: `YYYY`, `YYYY-MM`, `YYYY-MM-DD` | Yes (varies by endpoint) | `2024-01` |
| **Dune Analytics** | ISO 8601 `YYYY-MM-DDTHH:MM:SSZ` | Via query parameters | `2024-01-01T00:00:00Z` |

## Multi-Country Support

| Provider | Multi-Country | Separator | Max Countries | Example |
|----------|---------------|-----------|---------------|---------|
| **FRED** | ❌ No | N/A | 1 | Must make separate calls |
| **WorldBank** | ✅ Yes | Semicolon (`;`) | 60 indicators, 1500 chars between `/` | `chn;ago;usa` |
| **UN Comtrade** | ✅ Yes | Comma (`,`) | Multiple (API limit) | `reporterCode=156,840` |
| **CoinGecko** | ✅ Yes | Comma (`,`) | Multiple | `ids=bitcoin,ethereum` |
| **Statistics Canada** | ⚠️ Limited | Varies | Depends on dataset | Check specific table |
| **Dune Analytics** | ✅ Yes | Via SQL query | Unlimited | `WHERE country IN ('US', 'CN')` |

## Provincial/State Support

| Provider | Geographic Levels | Examples |
|----------|-------------------|----------|
| **FRED** | ✅ US States | `CAUR` (California Unemployment), `NYUR` (New York Unemployment) |
| **WorldBank** | ❌ National only | No sub-national data |
| **UN Comtrade** | ❌ National only | Country-level trade only |
| **CoinGecko** | N/A | Not geography-based |
| **Statistics Canada** | ✅ Canadian Provinces | Provincial GDP, unemployment by province |
| **Dune Analytics** | N/A | Blockchain data (not geography-based) |

## Error Codes

### FRED
- `400`: Bad Request (invalid parameters)
- `401`: Unauthorized (invalid API key)
- `404`: Not Found (series doesn't exist)
- `429`: Rate limit exceeded

### WorldBank
- `400`: Bad Request
- `404`: Indicator not found
- Data availability errors (no standardized error code, check response structure)

### UN Comtrade
- `401`: Invalid API key
- `403`: Forbidden (subscription required)
- `429`: Rate limit exceeded
- `500`: Internal server error

### CoinGecko
- `429`: Rate limit (free tier: 10-50 calls/min)
- `404`: Coin not found
- Historical data limited to 365 days on free tier

### Statistics Canada
- Standard HTTP error codes
- SDMX-specific error messages in response body

### Dune Analytics
- `401`: Invalid API key
- `404`: Query not found
- `429`: Rate limit exceeded
- Query timeout errors (performance-dependent)

## Rate Limits

| Provider | Free Tier Limit | Paid Tier | Notes |
|----------|-----------------|-----------|-------|
| **FRED** | 120 requests/60s | Same | No daily limit |
| **WorldBank** | 15,000 data points/call | Same | No authentication required |
| **UN Comtrade** | 500 calls/day (with token) | 10,000+ (subscription) | Premium required for some data |
| **CoinGecko** | 10-50 calls/min | 500-10,000/min | Free tier limited to 365 days history |
| **Statistics Canada** | No published limit | N/A | Generally generous |
| **Dune Analytics** | 1,000 calls/month | Unlimited | Performance tiers: medium/large |

## Key Parameters for Each Provider

### FRED
```
series_id (REQUIRED)
api_key (REQUIRED)
observation_start (YYYY-MM-DD)
observation_end (YYYY-MM-DD)
frequency (d, w, m, q, sa, a)
units (lin, chg, pch, pc1, log)
```

### WorldBank
```
country/{code}/indicator/{indicator}
date={start}:{end} (year format)
format=json
per_page=1000
mrv=N (most recent N values)
```

### UN Comtrade
```
reporterCode (country ISO)
partnerCode (country ISO or "0" for world)
period (YYYY or YYYYMM)
cmdCode (HS code)
flowCode (M=imports, X=exports)
```

### CoinGecko
```
ids (bitcoin,ethereum)
vs_currencies (usd,eur)
from/to (UNIX timestamp or ISO date)
interval (5m, hourly, daily)
```

### Statistics Canada
```
vectorId (data series ID)
startRefPeriod (YYYY-MM-DD)
endRefPeriod (YYYY-MM-DD)
format (json, csv)
```

### Dune Analytics
```
query_id (Dune query ID)
query_parameters (JSON object)
performance (medium, large)
```

## Data Frequency Support

| Provider | Daily | Monthly | Quarterly | Annual | Notes |
|----------|-------|---------|-----------|--------|-------|
| **FRED** | ✅ | ✅ | ✅ | ✅ | Also weekly, bi-weekly, semiannual |
| **WorldBank** | ❌ | ✅ | ✅ | ✅ | Mostly annual, some quarterly |
| **UN Comtrade** | ❌ | ✅ | ❌ | ✅ | Monthly and annual trade data |
| **CoinGecko** | ✅ | ✅ | ✅ | ✅ | Configurable intervals |
| **Statistics Canada** | ✅ | ✅ | ✅ | ✅ | Frequency varies by dataset |
| **Dune Analytics** | ✅ | ✅ | ✅ | ✅ | Block-level to custom intervals |

## Common Indicator Mappings

### FRED
- GDP: `GDP` (quarterly)
- Unemployment: `UNRATE` (monthly)
- CPI: `CPIAUCSL` (monthly)
- Federal Funds Rate: `FEDFUNDS` (monthly)

### WorldBank
- GDP: `NY.GDP.MKTP.CD` (annual)
- Unemployment: `SL.UEM.TOTL.ZS` (annual)
- Inflation: `FP.CPI.TOTL.ZG` (annual)
- Population: `SP.POP.TOTL` (annual)

### UN Comtrade
- Uses HS codes (6-digit): e.g., `8471` (computers)
- Flow codes: `M` (imports), `X` (exports)

### CoinGecko
- Bitcoin: `bitcoin`
- Ethereum: `ethereum`
- All coin IDs lowercase, hyphenated

### Statistics Canada
- Vector-based IDs (numeric)
- Table-based structure

## Implementation Priorities for 95% Accuracy

### High Priority
1. ✅ Multi-country query support (WorldBank, Comtrade)
2. ✅ Provincial/state data (FRED states, Statistics Canada provinces)
3. ✅ Date format normalization across all providers
4. ✅ Error handling for data availability exceptions

### Medium Priority
5. Multiple series in one query
6. Date range validation before API calls
7. Frequency conversion/aggregation

### Low Priority
8. Advanced data transformations
9. Vintage/revision handling
10. Custom aggregations

## Testing Strategy

For each provider, test:
1. **Basic single query** (country + indicator + date range)
2. **Multi-country query** (where supported)
3. **Provincial/state query** (where supported)
4. **Edge case dates** (very old, very recent, future, invalid)
5. **Invalid indicators** (non-existent codes)
6. **Rate limit handling** (consecutive requests)
7. **Data availability errors** (no data for date range)
8. **Multiple series** (where supported)

## Next Steps

1. Implement multi-country support for WorldBank and Comtrade
2. Add provincial mapping for FRED (US states) and Statistics Canada
3. Create comprehensive date format parser
4. Implement robust error handling
5. Create 100-case test suite based on this reference

---

**Note**: Detailed API documentation for each provider is available in separate files:
- `FRED_API_REFERENCE.md`
- `WORLDBANK_API_REFERENCE.md` (to be created)
- `COMTRADE_API_REFERENCE.md` (to be created)
- `COINGECKO_API_REFERENCE.md` (to be created)
- `STATSCAN_API_REFERENCE.md` (to be created)
- `DUNE_API_REFERENCE.md` (to be created)
