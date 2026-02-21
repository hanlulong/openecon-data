# FRED API Reference

**Provider**: Federal Reserve Economic Data (FRED)
**Base URL**: `https://api.stlouisfed.org/fred/`
**Data Focus**: US Economic Data
**Documentation**: https://fred.stlouisfed.org/docs/api/fred/

## Overview

FRED provides economic data from 106 sources, including US government agencies, international organizations, and private institutions. Primary focus is US economic indicators.

## Key Capabilities

### Data Types
- **Economic Indicators**: GDP, unemployment, inflation, interest rates
- **Financial Data**: Stock indices, Treasury rates, exchange rates
- **Regional Data**: State-level economic indicators
- **International**: Selected international economic series

### Frequency Support
- Daily
- Weekly (ending Friday, Saturday, Sunday, Monday, or Tuesday)
- Bi-Weekly (ending Wednesday)
- Monthly
- Quarterly
- Semiannual
- Annual

### Geography Support
- **National**: US-level data (primary)
- **State/Regional**: State-level indicators available (e.g., state unemployment rates)
- **International**: Limited international series

## API Parameters

### Date Formats

**realtime_start** and **realtime_end**:
- Format: `YYYY-MM-DD`
- Default: Current date
- Example: `realtime_start=2024-01-01`

**observation_start** and **observation_end**:
- Format: `YYYY-MM-DD`
- Default: Full available range
- Example: `observation_start=2020-01-01&observation_end=2024-12-31`

### Key Parameters

| Parameter | Description | Example Values |
|-----------|-------------|----------------|
| `series_id` | FRED series identifier (REQUIRED) | `UNRATE`, `GDP`, `CPIAUCSL` |
| `api_key` | Your FRED API key (REQUIRED) | `abcdef123456...` |
| `file_type` | Response format | `json`, `xml` (default: xml) |
| `observation_start` | Start date for observations | `2020-01-01` |
| `observation_end` | End date for observations | `2024-12-31` |
| `units` | Data transformation | `lin`, `chg`, `ch1`, `pch`, `pc1`, `pca`, `cch`, `cca`, `log` |
| `frequency` | Aggregation frequency | `d`, `w`, `bw`, `m`, `q`, `sa`, `a` |
| `aggregation_method` | How to aggregate | `avg`, `sum`, `eop` (end of period) |
| `output_type` | Vintage output type | `1` (observations), `2` (observations by vintage), `3` (observations for all  vintages), `4` (observations with release dates) |
| `sort_order` | Sort direction | `asc`, `desc` |
| `limit` | Max results per request | `1-100000` (default: 100000) |
| `offset` | Pagination offset | `0, 1, 2, ...` |

### Units (Data Transformations)

| Unit | Description |
|------|-------------|
| `lin` | Levels (no transformation) |
| `chg` | Change |
| `ch1` | Change from Year Ago |
| `pch` | Percent Change |
| `pc1` | Percent Change from Year Ago |
| `pca` | Compounded Annual Rate of Change |
| `cch` | Continuously Compounded Rate of Change |
| `cca` | Continuously Compounded Annual Rate of Change |
| `log` | Natural Log |

## API Endpoints

### Get Series Observations
```
GET /series/observations
```

**Example Request**:
```bash
curl "https://api.stlouisfed.org/fred/series/observations?series_id=UNRATE&api_key=YOUR_KEY&file_type=json&observation_start=2020-01-01"
```

**Example Response**:
```json
{
  "realtime_start": "2024-11-02",
  "realtime_end": "2024-11-02",
  "observation_start": "2020-01-01",
  "observation_end": "9999-12-31",
  "units": "lin",
  "output_type": 1,
  "file_type": "json",
  "order_by": "observation_date",
  "sort_order": "asc",
  "count": 58,
  "offset": 0,
  "limit": 100000,
  "observations": [
    {
      "realtime_start": "2024-11-02",
      "realtime_end": "2024-11-02",
      "date": "2020-01-01",
      "value": "3.6"
    },
    ...
  ]
}
```

### Get Series Metadata
```
GET /series
```

**Example Request**:
```bash
curl "https://api.stlouisfed.org/fred/series?series_id=UNRATE&api_key=YOUR_KEY&file_type=json"
```

**Example Response**:
```json
{
  "realtime_start": "2024-11-02",
  "realtime_end": "2024-11-02",
  "seriess": [
    {
      "id": "UNRATE",
      "realtime_start": "2024-11-02",
      "realtime_end": "2024-11-02",
      "title": "Unemployment Rate",
      "observation_start": "1948-01-01",
      "observation_end": "2024-10-01",
      "frequency": "Monthly",
      "frequency_short": "M",
      "units": "Percent",
      "units_short": "%",
      "seasonal_adjustment": "Seasonally Adjusted",
      "seasonal_adjustment_short": "SA",
      "last_updated": "2024-11-01 07:42:03-05",
      "popularity": 100,
      "notes": "..."
    }
  ]
}
```

## Multi-Country/Multi-Series Support

FRED **does not support multi-country queries** in a single API call. Each series must be requested individually.

For multiple series:
- Make separate API calls for each series
- Implement parallel requests for efficiency
- Use batch processing for large numbers of series

## Error Codes

| HTTP Code | Description |
|-----------|-------------|
| 400 | Bad Request - Invalid parameters |
| 401 | Unauthorized - Invalid or missing API key |
| 404 | Not Found - Series ID does not exist |
| 429 | Too Many Requests - Rate limit exceeded |
| 500 | Internal Server Error |

## Rate Limits

- **Free Tier**: 120 requests per 60 seconds
- **No daily limit** on request count
- Implement exponential backoff for rate limit errors

## econ-data-mcp Implementation

### Current Implementation

File: `backend/providers/fred.py`

```python
class FREDProvider:
    async def fetch_series(
        self,
        series_id: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        frequency: Optional[str] = None,
    ) -> List[NormalizedData]:
        """Fetch FRED series data"""
        url = f"{self.base_url}/series/observations"
        params = {
            "series_id": series_id,
            "api_key": self.api_key,
            "file_type": "json",
        }
        if start_date:
            params["observation_start"] = start_date
        if end_date:
            params["observation_end"] = end_date
        # ...
```

### Common Series IDs

| Series ID | Description | Frequency |
|-----------|-------------|-----------|
| `GDP` | Gross Domestic Product | Quarterly |
| `UNRATE` | Unemployment Rate | Monthly |
| `CPIAUCSL` | Consumer Price Index | Monthly |
| `FEDFUNDS` | Federal Funds Rate | Monthly |
| `DGS10` | 10-Year Treasury Rate | Daily |
| `DEXCHUS` | China / US Exchange Rate | Daily |
| `SP500` | S&P 500 Index | Daily |

### State-Level Series Examples

| Series ID | Description |
|-----------|-------------|
| `CAUR` | California Unemployment Rate |
| `NYUR` | New York Unemployment Rate |
| `TXNGSP` | Texas Gross State Product |
| `FLPCPI` | Florida Population |

## Testing Recommendations

### Test Cases for FRED

1. **Basic Series Query**: Single series with date range
2. **No Date Range**: Query without start/end dates (should return all data)
3. **Future Dates**: Request data for future dates (should return empty or error)
4. **Invalid Series ID**: Non-existent series (should return 404)
5. **Data Transformations**: Request percent change, year-over-year
6. **Frequency Aggregation**: Daily data aggregated to monthly
7. **State-Level Data**: Query state unemployment rates
8. **High-Frequency Data**: Daily stock market data
9. **Quarterly Data**: GDP growth rates
10. **Very Old Data**: Historical data from 1940s-1950s

### Example Test Cases

```python
test_cases = [
    {
        "query": "Show me US unemployment rate",
        "expected_series": "UNRATE",
        "expected_frequency": "monthly",
    },
    {
        "query": "California unemployment rate for 2023",
        "expected_series": "CAUR",
        "expected_start": "2023-01",
        "expected_end": "2023-12",
    },
    {
        "query": "GDP growth rate year over year",
        "expected_series": "A191RL1Q225SBEA",  # Real GDP % change
        "expected_frequency": "quarterly",
    },
]
```

## Known Limitations

1. **No Multi-Country Support**: Cannot query multiple countries in one call
2. **US-Centric**: Limited international data
3. **No Provincial Data Outside US**: No support for Canadian provinces, etc.
4. **Rate Limits**: 120 requests per minute may be restrictive for bulk operations
5. **Vintage Data Complexity**: Realtime vs observation dates can be confusing

## Best Practices

1. **Use Series Search API**: Search for series by keywords before querying
2. **Cache Metadata**: Series metadata changes infrequently, cache it
3. **Batch Requests**: Use parallel requests for multiple series
4. **Handle Revisions**: Be aware data can be revised; use realtime dates
5. **Date Validation**: Validate date ranges before making requests
6. **Error Handling**: Implement retry logic for rate limit errors

## Resources

- Official FRED Website: https://fred.stlouisfed.org/
- API Documentation: https://fred.stlouisfed.org/docs/api/fred/
- Get API Key: https://fred.stlouisfed.org/docs/api/api_key.html
- Python Client: https://github.com/mortada/fredapi
- FRED Blog: https://fredblog.stlouisfed.org/

---

**Last Updated**: 2024-11-02
**API Version**: V1 (current)
**econ-data-mcp Implementation**: `backend/providers/fred.py`
