# SDMX API Research and Documentation
**Date**: 2025-11-15
**Purpose**: Research findings for implementing general solutions for SDMX-based providers (OECD, Eurostat, BIS)

---

## Executive Summary

**Key Finding**: SDMX REST APIs are complex and require dataset-specific dimension knowledge. Each dataflow has unique dimension structures that must be discovered dynamically for a general solution.

**General Solution Approach**:
1. Query Data Structure Definition (DSD) endpoint to discover dimensions
2. Use wildcards (`.`) for unknown dimensions
3. Cache DSD results to avoid repeated lookups

---

## OECD SDMX REST API

### Base URL
```
https://sdmx.oecd.org/public/rest
```

### API Endpoints

#### 1. Data Structure Definition (DSD) Query
**Purpose**: Get metadata about a dataflow's dimensions and codelists

**Format**:
```
GET /datastructure/{agency}/{dsd_id}/{version}?references=children&detail=full
```

**Headers**:
```
Accept: application/vnd.sdmx.structure+json;version=1.0
```

**Example**:
```
https://sdmx.oecd.org/public/rest/datastructure/OECD.SDD.TPS/DSD_LFS/1.0?references=children&detail=full
```

**Response Structure**:
```json
{
  "data": {
    "dataStructures": [{
      "id": "DSD_LFS",
      "name": "Labour force statistics",
      "dataStructureComponents": {
        "dimensionList": {
          "dimensions": [
            {
              "id": "REF_AREA",
              "position": 0,
              "localRepresentation": {
                "enumeration": "urn:sdmx:org.sdmx.infomodel.codelist.Codelist=OECD:CL_AREA(1.1)"
              }
            },
            // ... more dimensions
          ]
        }
      }
    }]
  }
}
```

#### 2. Data Query
**Format**:
```
GET /data/{agency},{dsd}@{dataflow},{version}/{dimension_key}?parameters
```

**Dimension Key Format**: Values separated by `.` (dot)
- Use actual value for known dimensions
- Use `.` (empty) for wildcard (all values)
- Number of positions must match DSD dimension count

**Example**: Unemployment data (9 dimensions required)
```
https://sdmx.oecd.org/public/rest/data/OECD.SDD.TPS,DSD_LFS@DF_IALFS_UNE_M,1.0/DEU........M
                                                                            ↑           ↑
                                                               Country (pos 0)    Freq (pos 8)
```

**Headers**:
```
Accept: application/vnd.sdmx.data+json;version=2.0.0
```

**Query Parameters**:
- `startPeriod=YYYY-MM` - Start date
- `endPeriod=YYYY-MM` - End date
- `dimensionAtObservation=AllDimensions` - Include all dimensions

### OECD Dataflow Examples

#### DF_IALFS_UNE_M (Unemployment - Monthly)
**DSD**: `DSD_LFS`
**Agency**: `OECD.SDD.TPS`
**Dimensions** (9 total):
1. `REF_AREA` (position 0) - Country code (e.g., `DEU`, `USA`, `FRA`)
2. `MEASURE` (position 1) - Measurement type
3. `UNIT_MEASURE` (position 2) - Units (e.g., `PT_LF_SUB`, `PC`)
4. `TRANSFORMATION` (position 3) - Data transformation (e.g., `_Z` for none)
5. `ADJUSTMENT` (position 4) - Seasonal adjustment (e.g., `Y`/`N`)
6. `SEX` (position 5) - Gender (e.g., `_T` for total)
7. `AGE` (position 6) - Age group (e.g., `Y_GE15`)
8. `ACTIVITY` (position 7) - Economic activity sector
9. `FREQ` (position 8) - Frequency (e.g., `M` for monthly, `A` for annual)

**Working Query Pattern**:
```
/data/OECD.SDD.TPS,DSD_LFS@DF_IALFS_UNE_M,1.0/{country}........{freq}?startPeriod={date}
```

#### DF_NAAG_I (GDP - National Accounts)
**DSD**: `DSD_NAAG`
**Agency**: `OECD.SDD.NAD`
**Dimensions** (5 total):
1. `FREQ` (position 0) - Frequency
2. `REF_AREA` (position 1) - Country
3. `MEASURE` (position 2) - Measurement type
4. `UNIT_MEASURE` (position 3) - Units
5. `CHAPTER` (position 4) - Chapter/category

**Working Query Pattern**:
```
/data/OECD.SDD.NAD,DSD_NAAG@DF_NAAG_I,1.0/{freq}.{country}...
```

### Known Issues

1. **HTTP 406 Error**: Wrong `Accept` header
   - Solution: Use `application/vnd.sdmx.structure+json` for structure queries
   - Use `application/vnd.sdmx.data+json` for data queries

2. **HTTP 422 "Not enough key values"**: Incomplete dimension key
   - Solution: Provide all required dimensions (use `.` for wildcards)
   - Example: 9 dimensions needed → provide 9 positions

3. **HTTP 404 "NoResultsFound"**: Invalid dimension values or no data
   - Solution: Query DSD to find valid codelist values
   - Use wildcards if unsure of valid values

### Alternative OECD APIs

#### Legacy stats.oecd.org API (as of 2024)
**Format**:
```
https://stats.oecd.org/SDMX-JSON/data/{dataset}/{key}/all?contentType=csv
```

**Note**: This API was updated in July 2024. Some endpoints may be deprecated.

#### pandasdmx Python Library
**Recommended for Python applications**: Handles OECD complexity automatically

```python
import pandasdmx as pdmx

oecd = pdmx.Request("OECD")
data = oecd.data(
    resource_id="DF_IALFS_UNE_M",
    key="DEU/all?startTime=2023-01"
).to_pandas()
```

---

## Eurostat SDMX 2.1 API

### Base URL
```
https://ec.europa.eu/eurostat/api/dissemination/sdmx/2.1
```

### Key Differences from OECD
1. **Query Parameter Filtering**: Dimensions passed as query parameters, not in URL path
2. **Simpler Structure**: More forgiving with missing dimensions
3. **Dataset-Specific Defaults**: Each dataset has common default dimensions

### Query Format

**Structure Query**:
```
GET /datastructure/ESTAT/{dataset_code}
Accept: application/vnd.sdmx.structure+json
```

**Data Query**:
```
GET /data/{dataset_code}?format=JSON&lang=EN&geo={country}&freq={freq}&startPeriod={year}&endPeriod={year}&{dimension}={value}...
```

### Example: Unemployment (`une_rt_a`)
```
https://ec.europa.eu/eurostat/api/dissemination/sdmx/2.1/data/une_rt_a?format=JSON&lang=EN&geo=DE&freq=A&startPeriod=2023&endPeriod=2023&age=Y15-74&sex=T
```

**Required Dimensions**:
- `geo` - Country (e.g., `DE`, `FR`, `IT`)
- `freq` - Frequency (e.g., `A` annual, `M` monthly)
- `age` - Age group (e.g., `Y15-74`)
- `sex` - Gender (e.g., `T` total, `M` male, `F` female)
- `startPeriod`, `endPeriod` - Time range (YYYY format for annual)

### Eurostat Dataset Default Dimensions

**Reference**: See `backend/providers/eurostat.py` line 47-90

**Pattern**: Each dataset has a set of default dimension values that work for most queries.

Example:
```python
DATASET_DEFAULT_FILTERS = {
    "une_rt_a": {"age": "Y15-74", "sex": "T"},  # Unemployment rate
    "nama_10_gdp": {"na_item": "B1GQ", "unit": "CP_MEUR"},  # GDP
    "prc_hicp_aind": {"coicop": "CP00"},  # Inflation
}
```

### General Solution for Eurostat
1. Query dataset structure (optional - can use defaults)
2. Apply dataset-specific default filters
3. Always include: `geo`, `freq`, `startPeriod`, `endPeriod`
4. Add dataset-specific dimensions from defaults

---

## BIS SDMX API

### Base URL
```
https://stats.bis.org/api/v1
```

### Query Format

**Data Query**:
```
GET /data/{dataflow_code}/{dimension_key}?startPeriod={date}&endPeriod={date}
Accept: application/vnd.sdmx.data+json;version=1.0.0
```

### BIS Dataflow Examples

#### WS_CBPOL (Central Bank Policy Rates)
**Dimensions**: `{freq}.{country}`

```
https://stats.bis.org/api/v1/data/WS_CBPOL/M.US?startPeriod=2023-01&endPeriod=2023-12
```

#### WS_CREDIT_GAP (Credit-to-GDP Gap)
**Dimensions**: `{freq}.{country}.{sector}`

```
https://stats.bis.org/api/v1/data/WS_CREDIT_GAP/Q.US.P?startPeriod=2023-Q1&endPeriod=2023-Q4
```

**Sector Codes**:
- `P` - Private non-financial sector
- `G` - Government
- `H` - Households

#### WS_CREDIT (Credit Data)
**Dimensions**: `{freq}.{country}.{sector}.{unit}`

```
https://stats.bis.org/api/v1/data/WS_CREDIT/Q.US.P.770
```

**Unit Codes**:
- `770` - All sectors

### BIS Dimension Patterns

**Reference**: See `backend/providers/bis.py` line 165-196

**Simple datasets** (2 dimensions):
- `WS_CBPOL`, `WS_XRU`, `WS_EER`, `WS_PP` → `{freq}.{country}`

**Complex datasets** (3-4 dimensions):
- `WS_CREDIT`, `WS_DSR` → `{freq}.{country}.P.770`
- `WS_DBS` → `{freq}.{country}.A.A.A`
- `WS_GLI` → `{freq}.{country}.A.M`

### General Solution for BIS
1. Map indicator name to BIS dataflow code
2. Use dataflow-specific dimension pattern
3. Default to `M` (monthly) frequency if not specified
4. Use ISO 2-letter country codes (US, GB, DE, etc.)

---

## General SDMX Implementation Strategy

### Recommended Approach: Dynamic DSD Lookup with Caching

**Step 1: DSD Cache**
```python
class DSDCache:
    """Cache Data Structure Definitions to avoid repeated API calls."""

    def __init__(self):
        self.cache = {}  # {dataflow_id: dsd_metadata}

    async def get_dsd(self, provider, agency, dsd_id, version):
        cache_key = f"{provider}:{agency}:{dsd_id}:{version}"

        if cache_key not in self.cache:
            # Fetch from API
            dsd = await self._fetch_dsd(provider, agency, dsd_id, version)
            self.cache[cache_key] = dsd

        return self.cache[cache_key]
```

**Step 2: Dimension Discovery**
```python
async def build_dimension_key(self, dsd, user_params):
    """Build dimension key with wildcards for unknown dimensions."""

    dimensions = dsd['dimensions']  # List of {id, position, codelist}
    key_parts = [''] * len(dimensions)

    # Fill known dimensions
    for dim in dimensions:
        dim_id = dim['id']
        position = dim['position']

        if dim_id in user_params:
            key_parts[position] = user_params[dim_id]
        elif dim_id in DEFAULT_VALUES:
            key_parts[position] = DEFAULT_VALUES[dim_id]
        else:
            key_parts[position] = ''  # Wildcard

    return '.'.join(key_parts)
```

**Step 3: Provider-Specific Handlers**
```python
class SDMXProvider:
    def __init__(self, provider_type):
        self.provider_type = provider_type  # 'OECD', 'EUROSTAT', 'BIS'
        self.dsd_cache = DSDCache()

    async def fetch_data(self, dataflow, params):
        # Get DSD
        dsd = await self.dsd_cache.get_dsd(...)

        # Build query based on provider type
        if self.provider_type == 'OECD':
            return await self._fetch_oecd_style(dsd, dataflow, params)
        elif self.provider_type == 'EUROSTAT':
            return await self._fetch_eurostat_style(dsd, dataflow, params)
        elif self.provider_type == 'BIS':
            return await self._fetch_bis_style(dsd, dataflow, params)
```

### Alternative: Use Python SDMX Libraries

**Option 1: pandasdmx**
```bash
pip install pandasdmx
```

**Pros**:
- Handles SDMX complexity automatically
- Supports 20+ data providers
- Active maintenance

**Cons**:
- Additional dependency
- Learning curve
- May not support all our custom requirements

**Option 2: pysdmx**
```bash
pip install pysdmx
```

**Pros**:
- Modern, well-documented
- Async support
- Pythonic API

**Cons**:
- Newer library (less battle-tested)
- May have breaking changes

---

## Testing Scripts

### Test Scripts Created
1. **`scripts/test_oecd_dsd.py`** - Query OECD DSD endpoints
2. **`scripts/test_oecd_wildcards.py`** - Test wildcard dimension patterns

### Running Tests
```bash
python3 scripts/test_oecd_dsd.py
python3 scripts/test_oecd_wildcards.py
```

### Test Results Archived
- `/tmp/oecd_dsd_DF_IALFS_UNE_M_attempt1.json` - Unemployment DSD
- `/tmp/oecd_dsd_DF_NAAG_I_attempt1.json` - GDP DSD
- `/tmp/oecd_dsd_DF_PRICES_ALL_attempt1.json` - Prices DSD

---

## Implementation Recommendations

### Short-term (Quick Wins)
1. **Eurostat**: Already working with dimension defaults ✅
2. **BIS**: Simpler structure - implement dataflow-specific patterns ✅
3. **OECD**: Document as "complex" - requires DSD lookup system

### Medium-term (General Solution)
1. Implement DSD cache system
2. Add dynamic dimension key building
3. Test with top 5-10 dataflows per provider

### Long-term (Comprehensive)
1. Consider integrating pandasdmx library
2. Build admin UI for managing dataflow mappings
3. Implement automatic DSD refresh

---

## References

### Official Documentation
- [OECD SDMX-JSON API](https://data.oecd.org/api/sdmx-json-documentation/)
- [Eurostat SDMX 2.1 API](https://ec.europa.eu/eurostat/web/user-guides/data-browser/api-data-access/)
- [BIS Statistics API](https://stats.bis.org/api/v1/)
- [SDMX REST API Specification (GitHub)](https://github.com/sdmx-twg/sdmx-rest/blob/master/doc/data.md)

### Libraries
- [pandasdmx Documentation](https://pandasdmx.readthedocs.io/)
- [pysdmx Documentation](https://py.sdmx.io/)

### Stack Overflow
- [Download OECD API data using Python and SDMX](https://stackoverflow.com/questions/77806733/)
- [Fetching data from OECD into R via SDMX](https://stackoverflow.com/questions/59529074/)

### Tutorials
- [Getting Data - Coding for Economists](https://aeturrell.github.io/coding-for-economists/data-extraction.html)
- [How to download data from OECD database](https://quant-trading.co/how-to-download-data-oecd-database/)

---

## Conclusion

SDMX APIs are powerful but complex. The key to a general solution is:
1. **Dynamic DSD lookup** - Don't hardcode dimensions
2. **Intelligent defaults** - Use common dimension values as fallbacks
3. **Caching** - Avoid repeated structure queries
4. **Provider-specific handling** - Each API has quirks (query params vs URL path)

**Next Steps**:
1. Implement DSD cache system in `backend/services/dsd_cache.py`
2. Create unified SDMX provider base class
3. Migrate Eurostat, OECD, BIS to use dynamic DSD lookup
