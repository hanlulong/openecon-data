# Eurostat API Complete Guide

**Date**: 2025-11-21
**Purpose**: Comprehensive guide to Eurostat Statistics API (JSON-stat 2.0) and SDMX 2.1 API

## Executive Summary

Eurostat provides two main APIs for accessing EU economic and social statistics:
1. **Statistics API** (JSON-stat 2.0) - **RECOMMENDED** - Simpler, more reliable
2. **SDMX 2.1 API** - More powerful but complex, has known issues (406 errors)

**Current econ-data-mcp Implementation**: Uses Statistics API (JSON-stat 2.0) for maximum reliability.

---

## Statistics API (JSON-stat 2.0)

### Base URL Structure

```
https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/{datasetCode}?{parameters}
```

### Key Parameters

| Parameter | Type | Description | Examples |
|-----------|------|-------------|----------|
| `geo` | Filter | Geographic entity | `DE`, `FR`, `EU27_2020` |
| `freq` | Filter | Frequency | `A` (annual), `Q` (quarterly), `M` (monthly) |
| `time` / `time_period` | Filter | Specific period(s) | `2023`, `2020+2021+2022` |
| `sinceTimePeriod` | Range | Data from year onward | `2019` |
| `untilTimePeriod` | Range | Data through year | `2024` |
| `lastTimePeriod` | Numeric | Most recent N periods | `5` |
| `geoLevel` | Special | Filter by NUTS level | `country`, `nuts1`, `aggregate` |
| `format` | Fixed | Response format | `JSON` (only option) |
| `lang` | Optional | Language | `EN`, `FR`, `DE` (default: EN) |

### Common Dimension Codes

#### Frequency (freq)
- `A` - Annual
- `Q` - Quarterly
- `M` - Monthly

#### Geography (geo)
- Country codes: `DE`, `FR`, `IT`, `ES`, `NL`, `PL`, `BE`, `SE`, `AT`, `DK`, `FI`
- EU aggregates: `EU27_2020`, `EU28`, `EU15`, `EA19`
- Use `geoLevel=country` to get all EU member states

#### Age Groups (age)
- `TOTAL` - All ages
- `Y15-74` - From 15 to 74 years (unemployment)
- `Y15-64` - From 15 to 64 years (employment)

#### Sex (sex)
- `T` - Total (both sexes)
- `M` - Male
- `F` - Female

---

## Key Datasets

### 1. GDP (nama_10_gdp)

**Full name**: Gross domestic product (GDP) and main components

**Dimensions**: `[FREQ].[UNIT].[NA_ITEM].[GEO]`

**Key Dimension Values**:
- `na_item`:
  - `B1GQ` - Gross domestic product at market prices
  - `P3` - Final consumption expenditure
  - `P31_S14` - Household final consumption expenditure
  - `P5G` - Gross capital formation
- `unit`:
  - `CP_MEUR` - Current prices, million euro
  - `CLV10_MEUR` - Chain linked volumes (2010), million euro
  - `CP_EUR_HAB` - Current prices, euro per capita
  - `PD_PCH_PRE` - Price deflator, percentage change on previous period

**Example Query**:
```bash
# Germany GDP in current prices, 2019-2024
https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/nama_10_gdp?geo=DE&freq=A&na_item=B1GQ&unit=CP_MEUR&sinceTimePeriod=2019
```

**Response Structure**:
```json
{
  "version": "2.0",
  "class": "dataset",
  "label": "Gross domestic product...",
  "updated": "2025-11-17T23:00:00+0100",
  "value": {
    "0": 3537280.0,
    "1": 3450720.0,
    "2": 3682340.0,
    "3": 3989390.0,
    "4": 4219310.0,
    "5": 4328970.0
  },
  "dimension": {
    "time": {
      "category": {
        "index": {"2019": 0, "2020": 1, "2021": 2, "2022": 3, "2023": 4, "2024": 5}
      }
    }
  }
}
```

---

### 2. Unemployment Rate (une_rt_a / une_rt_m)

**Full name**: Unemployment by sex and age

**Datasets**:
- `une_rt_a` - Annual data
- `une_rt_m` - Monthly data

**Dimensions**: `[FREQ].[AGE].[UNIT].[SEX].[GEO]`

**Key Dimension Values**:
- `age`:
  - `Y15-74` - From 15 to 74 years (standard for unemployment)
  - `Y_LT25` - Less than 25 years (youth unemployment)
  - `Y25-74` - From 25 to 74 years
- `sex`:
  - `T` - Total
  - `M` - Male
  - `F` - Female
- `unit`:
  - `PC_ACT` - Percentage of active population (**RECOMMENDED**)
  - `PC_POP` - Percentage of total population
  - `THS_PER` - Thousand persons

**Example Query**:
```bash
# Germany unemployment rate (annual), 2019-2024
https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/une_rt_a?geo=DE&freq=A&age=Y15-74&sex=T&unit=PC_ACT&sinceTimePeriod=2019
```

**Response Data**:
```json
{
  "value": {
    "0": 2.9,  // 2019
    "1": 3.6,  // 2020
    "2": 3.6,  // 2021
    "3": 3.1,  // 2022
    "4": 3.1,  // 2023
    "5": 3.4   // 2024
  }
}
```

---

### 3. Inflation / HICP (prc_hicp_aind / prc_hicp_manr / prc_hicp_midx)

**Full name**: Harmonised Index of Consumer Prices (HICP)

**Datasets**:
- `prc_hicp_aind` - Annual data (average index and rate of change)
- `prc_hicp_manr` - Monthly annual rate of change
- `prc_hicp_midx` - Monthly index (2015=100)

**Dimensions**: `[FREQ].[UNIT].[COICOP].[GEO]` or `[FREQ].[COICOP].[GEO]`

**Key Dimension Values**:
- `coicop`:
  - `CP00` - All-items HICP (overall inflation)
  - `CP01` - Food and non-alcoholic beverages
  - `CP02` - Alcoholic beverages, tobacco
  - `CP04` - Housing, water, electricity, gas
  - `CP07` - Transport
- `unit` (for prc_hicp_aind):
  - `INX_A_AVG` - Annual average index (2015=100)
  - `RCH_A_AVG` - Annual average rate of change (%)

**Example Query**:
```bash
# France inflation (annual average index), 2019-2024
https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/prc_hicp_aind?geo=FR&freq=A&coicop=CP00&unit=INX_A_AVG&sinceTimePeriod=2019
```

**Response Data**:
```json
{
  "value": {
    "0": 104.95,  // 2019
    "1": 105.5,   // 2020
    "2": 107.68,  // 2021
    "3": 114.04,  // 2022
    "4": 120.5,   // 2023
    "5": 123.29   // 2024
  }
}
```

---

### 4. Population (demo_pjan)

**Full name**: Population on 1 January by age and sex

**Dimensions**: `[FREQ].[AGE].[SEX].[GEO]`

**Key Dimension Values**:
- `age`:
  - `TOTAL` - All ages
  - `Y_LT5` - Less than 5 years
  - `Y15-64` - From 15 to 64 years (working age)
- `sex`:
  - `T` - Total
  - `M` - Male
  - `F` - Female

**Example Query**:
```bash
# Germany total population, 2019-2024
https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/demo_pjan?geo=DE&freq=A&age=TOTAL&sex=T&sinceTimePeriod=2019
```

---

### 5. Employment (lfsi_emp_a / lfsq_egan)

**Full name**: Employment by sex and age

**Datasets**:
- `lfsi_emp_a` - Annual employment
- `lfsq_egan` - Quarterly employment rate

**Dimensions**: `[FREQ].[AGE].[SEX].[GEO]` or `[FREQ].[AGE].[SEX].[WSTATUS].[GEO]`

**Key Dimension Values**:
- `age`:
  - `Y15-64` - From 15 to 64 years (standard for employment)
  - `TOTAL` - All ages
- `wstatus`:
  - `EMP` - Employed persons
  - `POP` - Total population

**Example Query**:
```bash
# Italy employment rate (annual), 2019-2024
https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/lfsi_emp_a?geo=IT&freq=A&age=Y15-64&sex=T&sinceTimePeriod=2019
```

---

## Response Format (JSON-stat 2.0)

### Structure

```json
{
  "version": "2.0",
  "class": "dataset",
  "label": "Dataset title",
  "source": "ESTAT",
  "updated": "2025-11-17T23:00:00+0100",

  // Data values (flat array indexed by position)
  "value": {
    "0": 3537280.0,
    "1": 3450720.0,
    "2": 3682340.0
  },

  // Status flags (optional)
  "status": {
    "3": "p",  // provisional
    "4": "p"
  },

  // Dimension IDs (order matters!)
  "id": ["freq", "unit", "na_item", "geo", "time"],

  // Dimension sizes
  "size": [1, 1, 1, 1, 6],

  // Dimension metadata
  "dimension": {
    "freq": {
      "label": "Time frequency",
      "category": {
        "index": {"A": 0},
        "label": {"A": "Annual"}
      }
    },
    "time": {
      "label": "Time",
      "category": {
        "index": {
          "2019": 0,
          "2020": 1,
          "2021": 2
        },
        "label": {
          "2019": "2019",
          "2020": "2020",
          "2021": "2021"
        }
      }
    }
  }
}
```

### Parsing Algorithm

1. **Get dimension order**: Read `id` array (e.g., `["freq", "unit", "na_item", "geo", "time"]`)
2. **Get dimension sizes**: Read `size` array (e.g., `[1, 1, 1, 1, 6]`)
3. **Get time dimension**: Find "time" in dimensions, extract `category.index`
4. **Calculate value positions**:
   - For datasets with multiple dimensions, position = calculated index based on dimension sizes
   - For simple queries (one value per dimension), position = time_index

**Example** (une_rt_a with multiple units):
```python
# If dimensions are: [freq, age, unit, sex, geo, time]
# And sizes are:     [1,    1,    3,    1,   1,   6]
# Position calculation:
position = time_idx + (unit_idx * 6) + (sex_idx * 18) + ...
```

**econ-data-mcp Implementation** (backend/providers/eurostat.py:516-579):
- Uses `id` and `size` arrays to calculate flattened positions
- Handles unemployment rate special case (selects `PC_ACT` unit)
- Falls back to simple time-based indexing for basic queries

---

## Error Codes

| Code | HTTP Status | Issue | Solution |
|------|------------|-------|----------|
| 100 | 400/404 | Empty result set | Check dimension values exist |
| 140 | 400 | Syntax error | Verify parameter format |
| 150 | 400 | Semantic validation failure | Check dimension compatibility |
| 500 | 500 | Server-side failure | Retry with exponential backoff |

**Special Case**: `geo` and `geoLevel` cannot be used together (400 error)

---

## Best Practices

### 1. Always Specify Core Dimensions

**Required for most datasets**:
- `geo` - Geographic entity
- `freq` - Frequency (A/Q/M)

**Dataset-specific** (check DSD):
- `age` - For labor market data
- `sex` - For demographic data
- `unit` - For national accounts
- `coicop` - For price indices
- `na_item` - For GDP components

### 2. Use Time Range Parameters

**Prefer**:
```bash
?sinceTimePeriod=2019
```

**Avoid**:
```bash
?time=2019&time=2020&time=2021&time=2022&time=2023&time=2024
```

**Why**: Shorter URLs, better caching, more flexible

### 3. Handle Multiple Units

For datasets with multiple units (e.g., unemployment can be % or thousands):
- Specify `unit` parameter explicitly
- If not specified, response includes ALL units (complex parsing)
- Check `dimension.unit.category.index` to select correct values

### 4. Frequency Auto-Detection

**econ-data-mcp logic** (backend/providers/eurostat.py:173-180):
```python
if "_10q_" in dataset_code or dataset_code.endswith("_q"):
    freq = "Q"  # Quarterly
elif "_m" in dataset_code or dataset_code.startswith("ext_"):
    freq = "M"  # Monthly
else:
    freq = "A"  # Annual (default)
```

### 5. Country Code Mapping

**Common mappings**:
- `GERMANY` → `DE`
- `FRANCE` → `FR`
- `ITALY` → `IT`
- `SPAIN` → `ES`
- `EU` → `EU27_2020` (current EU composition)

---

## Testing Checklist

### Core Indicators

- [ ] **GDP**: `nama_10_gdp?geo=DE&freq=A&na_item=B1GQ&unit=CP_MEUR&sinceTimePeriod=2019`
- [ ] **Unemployment**: `une_rt_a?geo=DE&freq=A&age=Y15-74&sex=T&unit=PC_ACT&sinceTimePeriod=2019`
- [ ] **Inflation**: `prc_hicp_aind?geo=FR&freq=A&coicop=CP00&unit=INX_A_AVG&sinceTimePeriod=2019`
- [ ] **Population**: `demo_pjan?geo=DE&freq=A&age=TOTAL&sex=T&sinceTimePeriod=2019`
- [ ] **Employment**: `lfsi_emp_a?geo=IT&freq=A&age=Y15-64&sex=T&sinceTimePeriod=2019`

### Edge Cases

- [ ] Multiple countries: `?geo=DE+FR+IT`
- [ ] EU aggregate: `?geo=EU27_2020`
- [ ] Quarterly data: `gov_10q_ggdebt` (government debt)
- [ ] Monthly data: `une_rt_m` (monthly unemployment)
- [ ] Long time series: `sinceTimePeriod=2000`

---

## Common Issues

### Issue 1: "No data returned" (Empty Dataset)

**Causes**:
1. Wrong dimension values (typo in `geo`, `age`, `sex`, etc.)
2. Data not available for specified combination
3. Time period outside available range

**Debug steps**:
1. Test query in browser to see raw JSON
2. Check if `value` object is empty
3. Verify dimension values in Eurostat Data Browser
4. Try removing optional dimensions one by one

**Example**:
```bash
# ❌ Wrong: Germany with code "GER"
?geo=GER&freq=A&age=Y15-74&sex=T&unit=PC_ACT

# ✅ Correct: Germany with code "DE"
?geo=DE&freq=A&age=Y15-74&sex=T&unit=PC_ACT
```

### Issue 2: Wrong Values Extracted

**Cause**: Multiple units in response, wrong unit selected

**Example** (une_rt_a):
- Without `unit` parameter: Response includes `PC_ACT`, `PC_POP`, `THS_PER`
- Need to select correct unit index when parsing `value` array

**Solution** (backend/providers/eurostat.py:530-537):
```python
# For unemployment rate, prefer PC_ACT unit
if dataset_code == "une_rt_a" and "unit" in dimensions:
    unit_dim = dimensions.get("unit", {})
    unit_indexes = unit_dim.get("category", {}).get("index", {})
    if "PC_ACT" in unit_indexes:
        unit_index = unit_indexes["PC_ACT"]
```

### Issue 3: 406 Not Acceptable (SDMX API)

**Cause**: Using SDMX 2.1 API instead of Statistics API

**Symptoms**:
- URL contains `/sdmx/2.1/data/`
- HTTP 406 response

**Solution**: Use Statistics API instead
```bash
# ❌ SDMX API (has issues)
https://ec.europa.eu/eurostat/api/dissemination/sdmx/2.1/data/nama_10_gdp/A.CP_MEUR.B1GQ.DE

# ✅ Statistics API (recommended)
https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/nama_10_gdp?freq=A&unit=CP_MEUR&na_item=B1GQ&geo=DE
```

---

## Implementation Notes (econ-data-mcp)

### Current Approach

**File**: `backend/providers/eurostat.py`

**Strategy**:
1. Use Statistics API (JSON-stat 2.0) for all queries
2. Hardcoded dimension mappings in `DATASET_DEFAULT_FILTERS` (lines 101-143)
3. Automatic frequency detection based on dataset code (lines 173-180)
4. JSON-stat parsing with unit selection logic (lines 516-579)

**Strengths**:
- ✅ Reliable (no 406 errors)
- ✅ Simple query construction
- ✅ Fast response times
- ✅ Works for all major indicators

**Limitations**:
- ⚠️ Hardcoded dimension values (not dynamic)
- ⚠️ Limited to predefined datasets in `DATASET_MAPPINGS`
- ⚠️ Metadata search required for discovery of new datasets

### Potential Improvements

1. **Dynamic dimension discovery**: Query DSD to get valid dimension values
2. **Expand dataset mappings**: Add more indicators to `DATASET_MAPPINGS`
3. **Better error messages**: Explain missing dimensions or invalid combinations
4. **Caching**: Cache DSD responses to reduce API calls

---

## References

**Official Documentation**:
- [API Getting Started](https://ec.europa.eu/eurostat/web/user-guides/data-browser/api-data-access/api-getting-started/api)
- [API Detailed Guidelines](https://ec.europa.eu/eurostat/web/user-guides/data-browser/api-data-access/api-detailed-guidelines/api-statistics)
- [Data Browser](https://ec.europa.eu/eurostat/databrowser/)

**Working Examples**:
- [Germany GDP](https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/nama_10_gdp?geo=DE&freq=A&na_item=B1GQ&unit=CP_MEUR&sinceTimePeriod=2019)
- [Germany Unemployment](https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/une_rt_a?geo=DE&freq=A&age=Y15-74&sex=T&unit=PC_ACT&sinceTimePeriod=2019)
- [France Inflation](https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/prc_hicp_aind?geo=FR&freq=A&coicop=CP00&unit=INX_A_AVG&sinceTimePeriod=2019)

**Last Updated**: 2025-11-21
