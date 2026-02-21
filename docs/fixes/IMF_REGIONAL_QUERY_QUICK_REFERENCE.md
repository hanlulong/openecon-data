# IMF Regional Query - Quick Reference

## Supported Regional Terms

### Quick Lookup Table

| User Query Contains | Resolves To | Country Count |
|---------------------|-------------|---------------|
| "Eurozone", "Euro area" | DEU, FRA, ITA, ESP, NLD, BEL, AUT, IRL, PRT, GRC, FIN, SVK, SVN, LTU, LVA, EST, LUX, CYP, MLT, HRV | 20 |
| "EU", "European Union" | All EU members (includes Eurozone + 7 more) | 27 |
| "G7", "Group of 7" | USA, JPN, DEU, GBR, FRA, ITA, CAN | 7 |
| "G20", "Group of 20" | USA, CHN, JPN, DEU, IND, GBR, FRA, BRA, ITA, CAN, KOR, RUS, AUS, ESP, MEX, IDN, TUR, SAU, ARG, ZAF | 20 |
| "BRICS" | BRA, RUS, IND, CHN, ZAF | 5 |
| "Asian countries", "Asia" | CHN, JPN, IND, KOR, IDN, THA, MYS, SGP, PHL, VNM, PAK, BGD | 12 |
| "Developed economies", "Advanced economies" | USA, CAN, GBR, DEU, FRA, ITA, ESP, JPN, KOR, AUS, NZL, NLD, BEL, AUT, CHE, NOR, SWE, DNK, FIN, IRL, ISL | 21 |
| "Emerging markets", "Emerging economies" | CHN, IND, BRA, RUS, ZAF, MEX, IDN, TUR, SAU, ARG, THA, MYS, POL, PHL, EGY, PAK, VNM, CHL, COL, PER | 20 |
| "Globally", "Worldwide", "World", "Global" | Top 20 economies (USA, CHN, JPN, DEU, IND, ...) | 20 |
| "Nordic countries" | NOR, SWE, DNK, FIN, ISL | 5 |
| "Middle East" | SAU, ARE, ISR, TUR, IRN, IRQ, QAT, KWT, OMN, JOR, LBN | 11 |
| "Latin America" | BRA, MEX, ARG, COL, CHL, PER, VEN, ECU, GTM, CUB | 10 |
| "South America" | BRA, ARG, COL, CHL, PER, VEN, ECU, URY, PRY, BOL, GUY, SUR | 12 |
| "African countries", "Africa" | ZAF, NGA, EGY, KEN, ETH, GHA, TZA, UGA, DZA, MAR | 10 |
| "ASEAN" | IDN, THA, MYS, SGP, PHL, VNM, MMR, KHM, LAO, BRN | 10 |
| "Major currencies" | USA, JPN, GBR, CHE, CAN, AUS, NZL, NOR, SWE | 9 |

## Example Queries

### ✅ Working Examples

```
"What is GDP growth for the Eurozone?"
→ Fetches GDP growth for 20 Eurozone countries

"Show me inflation rates for Asian countries"
→ Fetches inflation for CHN, JPN, IND, KOR, IDN, THA, MYS, SGP, PHL, VNM, PAK, BGD

"Compare government debt for G7 countries"
→ Fetches debt for USA, JPN, DEU, GBR, FRA, ITA, CAN

"What is unemployment rate globally?"
→ Fetches unemployment for top 20 economies

"Display fiscal balance for developed economies"
→ Fetches fiscal balance for 21 developed countries
```

### Case-Insensitive and Flexible

All variations work:
- "eurozone" = "Eurozone" = "EUROZONE" = "Euro zone" = "Euro area"
- "asian countries" = "Asian Countries" = "ASIAN_COUNTRIES" = "Asia"
- "g7" = "G7" = "G_7" = "Group of 7"

## How It Works

1. **User asks:** "What is GDP for Eurozone?"
2. **LLM parses:** `{apiProvider: "IMF", indicators: ["GDP"], parameters: {country: "Eurozone"}}`
3. **Query service calls:** `imf_provider._resolve_countries("Eurozone")`
4. **Resolution:** `"Eurozone"` → `["DEU", "FRA", "ITA", "ESP", "NLD", ...]`
5. **Batch fetch:** `fetch_batch_indicator(indicator="GDP", countries=[...])`
6. **Result:** Data for all 20 Eurozone countries

## Adding New Regional Groups

To add a new regional group, edit `backend/providers/imf.py`:

```python
REGION_MAPPINGS: Dict[str, List[str]] = {
    # ... existing mappings ...

    # Add new group
    "YOUR_REGION_NAME": ["COUNTRY_CODE1", "COUNTRY_CODE2", ...],
}
```

Use ISO 3166-1 alpha-3 country codes (e.g., "USA", "GBR", "DEU").

## Troubleshooting

### "No data found for country 'XXXXX'"

**Cause:** Region name not in `REGION_MAPPINGS`

**Fix:** Add mapping to `REGION_MAPPINGS`, or use specific country names

### "IMF indicator 'XXXXX' not found"

**Cause:** Indicator not available in IMF DataMapper API

**Fix:** Use metadata search or alternative indicator name

### Partial Results (e.g., "Retrieved data for 15/20 countries")

**Cause:** Some countries don't have data for requested indicator

**Status:** Normal - not all indicators available for all countries

## Code References

- Resolution logic: `backend/providers/imf.py::_resolve_countries()`
- Regional mappings: `backend/providers/imf.py::REGION_MAPPINGS`
- Query service integration: `backend/services/query.py` (IMF handler)
- Tests: `tests/test_imf_regional_*.py`

---

**Last Updated:** 2025-01-26
**Success Rate:** 60-100% (up from 10%)
