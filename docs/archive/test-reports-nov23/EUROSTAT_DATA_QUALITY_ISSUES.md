# Eurostat Provider Data Quality Issues

This document details specific data quality problems found during production testing.

## Issue 1: Wrong Indicator Returned for Unemployment Query

### Query #2: "What is the unemployment rate in the European Union?"

**Expected:**
- Indicator: Unemployment rate
- Values: 6-8% (typical EU unemployment range)
- Unit: percent

**Actual:**
- Indicator: "Gross domestic product (GDP) and main components (output, expenditure and income)"
- Values: [13578816.3, 14792288.9, 16169776.2, 17257316.2, 18015434.3]
- Unit: million EUR

**Analysis:**
This is a critical bug. The query explicitly asks for "unemployment rate" but the system returns GDP data instead. This suggests the indicator selection logic in the Eurostat provider is broken.

**Impact:** HIGH - Users get completely wrong data

---

## Issue 2: Index Values Instead of Rates

### Query #3: "Get EU inflation rate from 2020 to 2024"

**Expected:**
- Indicator: Inflation rate (year-over-year % change)
- Values: Something like [-0.3%, 2.9%, 9.2%, 6.4%, 2.6%]
- Unit: percent change

**Actual:**
- Indicator: "HICP - annual data (average index and rate of change)"
- Values: [105.76, 108.82, 118.82, 126.38, 129.67]
- Unit: percent (but these are index values, not rates)

**Analysis:**
The Eurostat API returns HICP index values (base 2015=100), but the user asked for "inflation rate" which should be the percentage change. The system needs to calculate:
- 2020: (105.76 - 100) / 100 = 5.76% (cumulative since 2015)
- 2021: (108.82 - 105.76) / 105.76 = 2.89% (year-over-year)
- 2022: (118.82 - 108.82) / 108.82 = 9.19%
- 2023: (126.38 - 118.82) / 118.82 = 6.36%
- 2024: (129.67 - 126.38) / 126.38 = 2.60%

**Impact:** MEDIUM - Data is technically correct but not in the format users expect

**Affected Queries:**
- #3: EU inflation rate
- #8: Germany inflation rate
- #17: Belgium's inflation rate

---

## Issue 3: GDP Levels Instead of Growth Rates

### Query #1: "Show me EU GDP growth rate for the last 5 years"

**Expected:**
- Indicator: GDP growth rate (year-over-year % change)
- Values: Something like [-5.6%, 5.4%, 3.4%, 0.5%, 0.8%]
- Unit: percent change

**Actual:**
- Indicator: "Gross domestic product (GDP) and main components"
- Values: [13578816.3, 14792288.9, 16169776.2, 17257316.2, 18015434.3]
- Unit: million EUR (absolute GDP levels)

**Analysis:**
User asked for "GDP growth rate" but got GDP levels. The system should calculate:
- 2020: Baseline year (COVID impact)
- 2021: (14792288.9 - 13578816.3) / 13578816.3 = 8.9% growth
- 2022: (16169776.2 - 14792288.9) / 14792288.9 = 9.3% growth
- 2023: (17257316.2 - 16169776.2) / 16169776.2 = 6.7% growth
- 2024: (18015434.3 - 17257316.2) / 17257316.2 = 4.4% growth

**Impact:** MEDIUM - Data is correct but not in the format users expect

**Affected Queries:**
- #1: EU GDP growth rate
- #9: France GDP growth quarterly
- #14: Spain GDP growth rate

---

## Issue 4: Employment vs. Unemployment Confusion

### Query #25: "Show EU employment rate"

**Expected:**
- Indicator: Employment rate (% of working-age population employed)
- Values: 60-75% (typical employment rates)
- Unit: percent

**Actual:**
- Indicator: "Unemployment by sex and age - annual data"
- Values: [7.2, 7.1, 6.2, 6.1, 5.9]
- Unit: percent

**Analysis:**
User asked for "employment rate" but got unemployment rate. These are related but different metrics:
- Employment rate = (employed / working-age population) * 100
- Unemployment rate = (unemployed / labor force) * 100
- Relationship: employment rate ≈ 100% - unemployment rate (approximately, not exact)

The system returned unemployment rate (5-7%) when it should return employment rate (typically 65-75% for EU).

**Impact:** MEDIUM - Data is related but not what was asked for

---

## Root Causes

### 1. Missing Calculation Layer

The Eurostat provider returns raw data from the API without post-processing. When users ask for:
- "growth rate" → Need to calculate % change from levels
- "inflation rate" → Need to calculate % change from index
- "change" or "increase" → Need to calculate differences

**Solution:** Add a calculation layer that:
1. Detects keywords: "rate", "growth", "change", "increase"
2. Fetches level/index data
3. Calculates period-over-period or year-over-year changes
4. Returns calculated values with appropriate units

### 2. Weak Indicator Selection

The LLM is selecting indicators based on partial keyword matching, which fails when:
- Similar indicators exist (employment vs. unemployment)
- User query is ambiguous
- Metadata search returns multiple candidates

**Solution:** Improve indicator selection by:
1. Using semantic similarity instead of keyword matching
2. Adding validation step to check if selected indicator matches query intent
3. Prioritizing exact matches over partial matches

### 3. Missing Data Validation

The system doesn't validate that returned data makes sense for the query:
- Query asks for unemployment (0-20%) → Returns GDP (millions) → No validation error
- Query asks for rates → Returns index values → No validation error

**Solution:** Add validation layer that:
1. Checks if returned values are in reasonable range for indicator type
2. Checks if units match expected units
3. Flags mismatches for manual review or automatic correction

---

## Recommendations for Fixes

### Priority 1: Add Growth/Rate Calculation

```python
def calculate_rates(data_points, calculation_type='yoy'):
    """
    Calculate year-over-year or period-over-period growth rates.
    
    Args:
        data_points: List of {date, value} dictionaries
        calculation_type: 'yoy' (year-over-year) or 'pop' (period-over-period)
    
    Returns:
        List of {date, value} with calculated rates
    """
    if len(data_points) < 2:
        return data_points
    
    rates = []
    for i in range(1, len(data_points)):
        prev_value = data_points[i-1]['value']
        curr_value = data_points[i]['value']
        
        if prev_value and prev_value != 0:
            rate = ((curr_value - prev_value) / prev_value) * 100
            rates.append({
                'date': data_points[i]['date'],
                'value': round(rate, 2)
            })
    
    return rates
```

### Priority 2: Improve Indicator Selection

Add validation after indicator selection:

```python
def validate_indicator(query_intent, selected_indicator):
    """
    Validate that selected indicator matches query intent.
    """
    # Check for keyword mismatches
    query_lower = query_intent.lower()
    indicator_lower = selected_indicator.lower()
    
    # If query asks for unemployment, indicator should contain "unemployment"
    if 'unemployment' in query_lower and 'gdp' in indicator_lower:
        return False, "Query asks for unemployment but indicator is GDP"
    
    # If query asks for employment, indicator should not be unemployment
    if 'employment rate' in query_lower and 'unemployment' in indicator_lower:
        return False, "Query asks for employment rate but indicator is unemployment rate"
    
    return True, "OK"
```

### Priority 3: Add Data Validation

Add range checks for returned values:

```python
EXPECTED_RANGES = {
    'unemployment': (0, 30),  # Unemployment rates
    'employment': (50, 90),   # Employment rates
    'inflation': (-5, 20),    # Inflation rates
    'gdp_growth': (-15, 15),  # GDP growth rates
    'interest_rate': (0, 20), # Interest rates
}

def validate_data_range(indicator_type, values):
    """
    Check if values are in expected range for indicator type.
    """
    if indicator_type not in EXPECTED_RANGES:
        return True, "No range check available"
    
    min_val, max_val = EXPECTED_RANGES[indicator_type]
    
    for value in values:
        if value < min_val or value > max_val:
            return False, f"Value {value} outside expected range [{min_val}, {max_val}]"
    
    return True, "OK"
```

---

## Testing Strategy

After implementing fixes, retest with these specific queries:

1. **Rate Calculation Tests:**
   - "EU inflation rate from 2020 to 2024" → Should return [-0.3, 2.9, 9.2, 6.4, 2.6] not [105.76, 108.82, ...]
   - "EU GDP growth rate for the last 5 years" → Should return [%, %, ...] not absolute values

2. **Indicator Selection Tests:**
   - "What is the unemployment rate in the European Union?" → Should return unemployment, not GDP
   - "Show EU employment rate" → Should return employment rate, not unemployment rate

3. **Validation Tests:**
   - All queries should return data with reasonable values
   - Units should match the query intent (% for rates, absolute for levels)

---

## Files

- Main Report: `/home/hanlulong/econ-data-mcp/EUROSTAT_PRODUCTION_TEST_REPORT.md`
- Test Results: `/home/hanlulong/econ-data-mcp/scripts/eurostat_test_results_20251122_233021.json`
- Test Script: `/home/hanlulong/econ-data-mcp/scripts/test_eurostat_production.py`
