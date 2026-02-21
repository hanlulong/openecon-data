# StatsCan Categorical Data Provider

## Overview

The StatsCan categorical data provider enables flexible querying of Statistics Canada data with multi-dimensional filtering capabilities. This feature allows users to query Canadian economic data by any combination of geography, gender, and age group dimensions.

## Features

### Multi-Dimensional Filtering

Query data using any combination of dimensions:

- **Geography**: All 15 Canadian provinces and territories
- **Gender**: Total, Men+, Women+
- **Age Groups**: 20 age ranges (e.g., "25 to 29 years", "65 to 69 years")

### Supported Query Types

1. **Single Dimension Queries**
   - Geography only: "Ontario population"
   - Gender only: "Canadian male population"
   - Age only: "Population aged 25-29"

2. **Multi-Dimension Queries**
   - Geography + Gender: "Ontario male population"
   - Geography + Age: "British Columbia population aged 65-69"
   - Gender + Age: "Canadian males aged 25-29"
   - All dimensions: "Ontario males aged 25-29"

3. **Decomposition Queries**
   - Provincial breakdown: "Population by province"
   - Age group breakdown: "Population by age group"

## Technical Implementation

### Architecture

**Provider**: `backend/providers/statscan.py`
- Method: `fetch_categorical_data()`
- API: Statistics Canada Web Data Service (WDS)
- Endpoint: `getDataFromCubePidCoordAndLatestNPeriods`

**Integration**: `backend/services/query.py`
- Automatic routing based on dimension parameters
- Fallback to vector-based queries for simple indicators

### Coordinate System

The provider uses StatsCan's 10-dimension coordinate format:

```
coordinate = "geography.gender.age.0.0.0.0.0.0.0"
```

Example coordinates:
- Ontario (all): `7.1.1.0.0.0.0.0.0.0`
- Ontario males: `7.2.1.0.0.0.0.0.0.0`
- Ontario males aged 25-29: `7.2.7.0.0.0.0.0.0.0`

### Dimension Mappings

**Geography** (Dimension 0):
```python
{
    "CANADA": 1,
    "NEWFOUNDLAND AND LABRADOR": 2,
    "PRINCE EDWARD ISLAND": 3,
    "NOVA SCOTIA": 4,
    "NEW BRUNSWICK": 5,
    "QUEBEC": 6,
    "ONTARIO": 7,
    "MANITOBA": 8,
    "SASKATCHEWAN": 9,
    "ALBERTA": 10,
    "BRITISH COLUMBIA": 11,
    "YUKON": 12,
    "NORTHWEST TERRITORIES": 13,
    "NUNAVUT": 14,
}
```

**Gender** (Dimension 1):
```python
{
    "TOTAL": 1,
    "MEN+": 2,
    "WOMEN+": 3,
}
```

**Age Groups** (Dimension 2):
```python
{
    "ALL AGES": 1,
    "0 TO 4 YEARS": 2,
    "5 TO 9 YEARS": 3,
    "10 TO 14 YEARS": 4,
    "15 TO 19 YEARS": 5,
    "20 TO 24 YEARS": 6,
    "25 TO 29 YEARS": 7,
    # ... 20 age groups total
}
```

## Usage Examples

### Python API

```python
from backend.providers.statscan import StatsCanProvider

provider = StatsCanProvider()

# Simple geography query
data = await provider.fetch_categorical_data({
    "productId": "17100005",
    "indicator": "Population",
    "periods": 3,
    "dimensions": {
        "geography": "Ontario"
    }
})

# Multi-dimensional query
data = await provider.fetch_categorical_data({
    "productId": "17100005",
    "indicator": "Population",
    "periods": 3,
    "dimensions": {
        "geography": "Ontario",
        "gender": "Men+",
        "age": "25 to 29 years"
    }
})
```

### Query Service Integration

The query service automatically routes requests to the categorical provider when dimensional parameters are detected:

```python
# In query.py
if dimensions or entity:
    # Route to categorical provider
    series = await self.statscan_provider.fetch_categorical_data(params)
else:
    # Fallback to vector-based queries
    series = await self.statscan_provider.fetch_series(params)
```

## Data Products

### Product 17100005
**Title**: Population estimates by age and sex
**Dimensions**: 10 (3 used for categorical queries)
**Frequency**: Annual (July 1st estimates)
**Coverage**: 2021-2025

## Error Handling

The provider includes comprehensive validation:

- **Invalid geography**: Raises `ValueError` with list of valid provinces
- **Invalid gender**: Raises `ValueError` with valid gender options
- **Invalid age group**: Raises `ValueError` with valid age ranges
- **API errors**: Properly handled with informative error messages

## Testing

Comprehensive test suite covers:

1. Single dimension filtering (geography, gender, age)
2. Two-dimension combinations
3. Three-dimension filtering
4. Decomposition with entity parameters
5. Error handling for invalid inputs

Test files:
- `/tmp/test_categorical_data.py` - Unit tests
- `/tmp/test_production_categorical.py` - Production verification
- `/tmp/test_integration_comprehensive.py` - Integration tests

## Performance

- **API Response Time**: Typically 1-3 seconds
- **Caching**: Query results cached with TTL
- **Concurrent Requests**: Supports parallel fetching for decomposition queries

## Future Enhancements

Potential improvements:

1. **Additional Products**: Extend to other StatsCan products beyond 17100005
2. **More Dimensions**: Support additional dimension filtering (e.g., occupation, education)
3. **Time Range Filtering**: Add date range parameters
4. **Aggregation**: Support automatic aggregation across dimensions

## References

- [StatsCan Web Data Service API](https://www.statcan.gc.ca/en/developers/wds)
- [Product 17100005 Documentation](https://www150.statcan.gc.ca/t1/tbl1/en/tv.action?pid=1710000501)
- [WDS Coordinate Format Specification](https://www.statcan.gc.ca/en/developers/wds/user-guide)

## Commits

- Initial implementation: `201b098`
- Query service integration: `e274090`
