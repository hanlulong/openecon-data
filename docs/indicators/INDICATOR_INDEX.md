# Indicator Index

> **Purpose:** This document provides a comprehensive index of economic indicators across all data providers. It serves as a reference for indicator resolution, synonym mapping, and coverage information.

**Last Updated:** 2025-12-25

---

## How to Use This Document

1. **When testing reveals a new indicator:** Add it to this index
2. **When an indicator query fails:** Check if it's properly mapped
3. **When adding a new provider:** Document all its indicators here
4. **Continuously update:** Every testing session should contribute

---

## GDP (Gross Domestic Product)

### Canonical Name
Gross Domestic Product

### Common Variations / Synonyms
- GDP
- Gross Domestic Product
- National Output
- Economic Output
- Total Output
- National Income

### Provider Mappings

| Provider | Indicator Code | Coverage | Frequency | Units | Notes |
|----------|---------------|----------|-----------|-------|-------|
| FRED | GDP | US | Quarterly | Billions USD | Nominal, SAAR |
| FRED | GDPC1 | US | Quarterly | Billions 2017 USD | Real, SAAR |
| FRED | GDPDEF | US | Quarterly | Index | GDP Deflator |
| FRED | A191RL1Q225SBEA | US | Quarterly | Percent | Real GDP growth rate |
| World Bank | NY.GDP.MKTP.CD | 200+ countries | Annual | Current USD | Nominal |
| World Bank | NY.GDP.MKTP.KD | 200+ countries | Annual | Constant USD | Real |
| World Bank | NY.GDP.MKTP.KD.ZG | 200+ countries | Annual | Percent | Annual growth |
| IMF | NGDP | 190+ countries | Annual | National currency | Nominal |
| IMF | NGDPD | 190+ countries | Annual | USD | Nominal in USD |
| IMF | NGDP_RPCH | 190+ countries | Annual | Percent | Real growth |
| Eurostat | nama_10_gdp | EU members | Annual/Quarterly | EUR/National | Multiple measures |
| Eurostat | namq_10_gdp | EU members | Quarterly | EUR/National | Quarterly data |
| OECD | NAEXKP01 | OECD members | Quarterly | Index | GDP volume index |

### Related Indicators
- **GDP per capita:** `NY.GDP.PCAP.CD` (WB), `A939RC0A052NBEA` (FRED)
- **GDP growth:** `NY.GDP.MKTP.KD.ZG` (WB), `A191RL1Q225SBEA` (FRED)
- **Real GDP:** `NY.GDP.MKTP.KD` (WB), `GDPC1` (FRED)
- **GDP Deflator:** `GDPDEF` (FRED), `NY.GDP.DEFL.ZS` (WB)

### Query Patterns That Should Match
- "GDP of [country]"
- "[country] GDP"
- "[country] gross domestic product"
- "economic output of [country]"
- "[country] national output"
- "GDP growth [country]"
- "[country] GDP per capita"
- "real GDP [country]"

### Known Issues
- World Bank data typically has 1-2 year lag
- FRED is US-only for most GDP series
- IMF requires specific country codes (not ISO)
- Eurostat covers EU members only

---

## Unemployment Rate

### Canonical Name
Unemployment Rate

### Common Variations / Synonyms
- Unemployment
- Jobless Rate
- Unemployment Rate
- Joblessness
- Out of Work Rate
- Labor Market Slack

### Provider Mappings

| Provider | Indicator Code | Coverage | Frequency | Units | Notes |
|----------|---------------|----------|-----------|-------|-------|
| FRED | UNRATE | US | Monthly | Percent | Civilian, 16+ |
| FRED | U6RATE | US | Monthly | Percent | Broader measure |
| World Bank | SL.UEM.TOTL.ZS | 200+ countries | Annual | Percent | ILO estimate |
| World Bank | SL.UEM.TOTL.NE.ZS | 200+ countries | Annual | Percent | National estimate |
| IMF | LUR | 190+ countries | Annual | Percent | |
| Eurostat | une_rt_m | EU members | Monthly | Percent | Seasonally adjusted |
| Eurostat | une_rt_a | EU members | Annual | Percent | |
| OECD | LRHUTTTT | OECD members | Monthly | Percent | Harmonized |
| StatsCan | 14-10-0017-01 | Canada | Monthly | Percent | Labour Force Survey |

### Related Indicators
- **Youth unemployment:** `SL.UEM.1524.ZS` (WB)
- **Long-term unemployment:** `SL.UEM.LTRM.ZS` (WB)
- **Employment rate:** `SL.EMP.TOTL.SP.ZS` (WB)
- **Labor force participation:** `SL.TLF.ACTI.ZS` (WB)

### Query Patterns That Should Match
- "unemployment rate [country]"
- "[country] unemployment"
- "jobless rate [country]"
- "[country] joblessness"
- "how many unemployed in [country]"

### Known Issues
- Different countries use different definitions
- ILO estimates may differ from national statistics
- Seasonal adjustments vary by provider

---

## Inflation / Consumer Price Index (CPI)

### Canonical Name
Inflation Rate / Consumer Price Index

### Common Variations / Synonyms
- Inflation
- CPI
- Consumer Price Index
- Price Increase
- Cost of Living
- Consumer Prices
- Price Level

### Provider Mappings

| Provider | Indicator Code | Coverage | Frequency | Units | Notes |
|----------|---------------|----------|-----------|-------|-------|
| FRED | CPIAUCSL | US | Monthly | Index | All Urban Consumers |
| FRED | CPILFESL | US | Monthly | Index | Core CPI (ex food/energy) |
| FRED | FPCPITOTLZGUSA | US | Annual | Percent | Annual change |
| World Bank | FP.CPI.TOTL.ZG | 200+ countries | Annual | Percent | Annual inflation |
| World Bank | FP.CPI.TOTL | 200+ countries | Annual | Index | CPI index |
| IMF | PCPIPCH | 190+ countries | Annual | Percent | CPI change |
| Eurostat | prc_hicp_midx | EU members | Monthly | Index | HICP |
| Eurostat | prc_hicp_aind | EU members | Annual | Index | Annual HICP |
| OECD | CPALTT01 | OECD members | Monthly | Index | CPI all items |

### Related Indicators
- **Core inflation:** `CPILFESL` (FRED)
- **PPI:** `PPIACO` (FRED)
- **GDP Deflator:** `GDPDEF` (FRED), `NY.GDP.DEFL.ZS` (WB)
- **Food prices:** `FP.CPI.TOTL.ZG` subset (WB)

### Query Patterns That Should Match
- "inflation rate [country]"
- "[country] inflation"
- "CPI [country]"
- "[country] consumer price index"
- "cost of living [country]"
- "[country] price increase"

### Known Issues
- CPI methodologies differ between countries
- HICP (EU) vs CPI (US) have different baskets
- Headline vs core inflation distinction important

---

## Interest Rates

### Canonical Name
Interest Rate / Policy Rate

### Common Variations / Synonyms
- Interest Rate
- Policy Rate
- Central Bank Rate
- Base Rate
- Benchmark Rate
- Fed Funds Rate (US)
- Bank Rate

### Provider Mappings

| Provider | Indicator Code | Coverage | Frequency | Units | Notes |
|----------|---------------|----------|-----------|-------|-------|
| FRED | FEDFUNDS | US | Monthly | Percent | Federal Funds Rate |
| FRED | DFF | US | Daily | Percent | Fed Funds Effective |
| FRED | PRIME | US | Monthly | Percent | Bank Prime Loan Rate |
| World Bank | FR.INR.RINR | 200+ countries | Annual | Percent | Real interest rate |
| IMF | FPOLM | 190+ countries | Monthly | Percent | Policy rate |
| BIS | Q:5A:A:A:A:A:5:A | Global | Quarterly | Percent | Policy rates |
| Eurostat | irt_st_m | EU | Monthly | Percent | Money market rates |

### Related Indicators
- **Long-term rates:** Treasury yields (FRED: GS10, GS2)
- **Mortgage rates:** `MORTGAGE30US` (FRED)
- **LIBOR/SOFR:** `SOFR` (FRED)

### Query Patterns That Should Match
- "interest rate [country]"
- "[country] interest rate"
- "central bank rate [country]"
- "[country] policy rate"
- "fed funds rate" (US specific)
- "[country] base rate"

---

## Exchange Rates

### Canonical Name
Exchange Rate

### Common Variations / Synonyms
- Exchange Rate
- FX Rate
- Forex
- Currency Rate
- Currency Pair
- Conversion Rate

### Provider Mappings

| Provider | Indicator Code | Coverage | Frequency | Units | Notes |
|----------|---------------|----------|-----------|-------|-------|
| ExchangeRate-API | [BASE]/[QUOTE] | 160+ currencies | Real-time | Rate | Live rates |
| FRED | DEXUSEU | USD/EUR | Daily | USD per EUR | Historical |
| FRED | DEXJPUS | JPY/USD | Daily | JPY per USD | Historical |
| IMF | ENDA | 190+ countries | Monthly | SDR | Exchange rate |
| World Bank | PA.NUS.FCRF | 200+ countries | Annual | LCU per USD | Official rate |

### Related Indicators
- **Real effective exchange rate:** `PX.REX.REER` (WB)
- **Trade-weighted dollar:** `TWEXB` (FRED)

### Query Patterns That Should Match
- "[CUR1]/[CUR2] exchange rate"
- "[CUR1] to [CUR2]"
- "convert [CUR1] to [CUR2]"
- "[currency] exchange rate"
- "forex [CUR1]/[CUR2]"

---

## Trade Balance

### Canonical Name
Trade Balance / Net Exports

### Common Variations / Synonyms
- Trade Balance
- Net Exports
- Trade Surplus
- Trade Deficit
- External Balance
- Goods Trade

### Provider Mappings

| Provider | Indicator Code | Coverage | Frequency | Units | Notes |
|----------|---------------|----------|-----------|-------|-------|
| FRED | BOPGSTB | US | Monthly | Millions USD | Goods & Services |
| World Bank | NE.RSB.GNFS.ZS | 200+ countries | Annual | % of GDP | External balance |
| World Bank | BN.GSR.GNFS.CD | 200+ countries | Annual | Current USD | Net trade |
| Comtrade | HS codes | 200+ countries | Annual | USD | Detailed trade |
| IMF | BCA | 190+ countries | Annual | USD | Current account |

### Related Indicators
- **Exports:** `NE.EXP.GNFS.CD` (WB)
- **Imports:** `NE.IMP.GNFS.CD` (WB)
- **Current account:** `BN.CAB.XOKA.CD` (WB)

### Query Patterns That Should Match
- "trade balance [country]"
- "[country] trade balance"
- "[country] net exports"
- "[country] trade surplus/deficit"
- "exports minus imports [country]"

---

## Population

### Canonical Name
Population

### Common Variations / Synonyms
- Population
- Total Population
- Inhabitants
- Residents

### Provider Mappings

| Provider | Indicator Code | Coverage | Frequency | Units | Notes |
|----------|---------------|----------|-----------|-------|-------|
| World Bank | SP.POP.TOTL | 200+ countries | Annual | Persons | Total population |
| FRED | POPTHM | US | Monthly | Thousands | US population |
| IMF | LP | 190+ countries | Annual | Persons | |
| Eurostat | demo_pjan | EU | Annual | Persons | Population on Jan 1 |

### Related Indicators
- **Population growth:** `SP.POP.GROW` (WB)
- **Urban population:** `SP.URB.TOTL` (WB)
- **Working age population:** `SP.POP.1564.TO` (WB)

### Query Patterns That Should Match
- "population of [country]"
- "[country] population"
- "how many people in [country]"
- "[country] inhabitants"

---

## Government Debt

### Canonical Name
Government Debt / Public Debt

### Common Variations / Synonyms
- Government Debt
- Public Debt
- National Debt
- Sovereign Debt
- Debt to GDP

### Provider Mappings

| Provider | Indicator Code | Coverage | Frequency | Units | Notes |
|----------|---------------|----------|-----------|-------|-------|
| FRED | GFDEBTN | US | Quarterly | Millions USD | Total public debt |
| FRED | GFDEGDQ188S | US | Quarterly | Percent | Debt to GDP |
| World Bank | GC.DOD.TOTL.GD.ZS | 200+ countries | Annual | % of GDP | Central govt debt |
| IMF | GGXWDG_NGDP | 190+ countries | Annual | % of GDP | Gross govt debt |

### Query Patterns That Should Match
- "government debt [country]"
- "[country] national debt"
- "[country] debt to GDP"
- "[country] public debt"

---

## Cryptocurrency

### Canonical Name
Cryptocurrency Prices

### Common Variations / Synonyms
- Bitcoin
- BTC
- Ethereum
- ETH
- Crypto
- Cryptocurrency
- Digital Currency

### Provider Mappings

| Provider | Indicator Code | Coverage | Frequency | Units | Notes |
|----------|---------------|----------|-----------|-------|-------|
| CoinGecko | bitcoin | Global | Real-time | USD/others | BTC price |
| CoinGecko | ethereum | Global | Real-time | USD/others | ETH price |
| CoinGecko | [coin-id] | 10000+ coins | Real-time | USD/others | Any listed coin |

### Query Patterns That Should Match
- "bitcoin price"
- "BTC price"
- "ethereum price"
- "ETH price"
- "[cryptocurrency] price"
- "crypto market cap"

---

## Commodities

### Canonical Name
Commodity Prices (Gold, Silver, Oil, Copper, Natural Gas)

### Common Variations / Synonyms
- Gold price
- Silver price
- Oil price
- Crude oil
- WTI oil
- Natural gas price
- Copper price

### Provider Mappings

| Provider | Indicator Code | Coverage | Frequency | Units | Notes |
|----------|---------------|----------|-----------|-------|-------|
| FRED | WPU10210501 | US | Monthly | Index | Gold Ores PPI |
| FRED | WPU10210601 | US | Monthly | Index | Silver Ores PPI |
| FRED | DCOILWTICO | Global | Daily | USD/barrel | WTI Crude Oil |
| FRED | DHHNGSP | US | Daily | USD/MMBtu | Henry Hub Natural Gas |
| FRED | PCOPPUSDM | Global | Monthly | USD/MT | Copper Price |
| CoinGecko | gold | Global | Real-time | USD | Gold spot (via crypto API) |

### Query Patterns That Should Match
- "gold price"
- "silver price"
- "oil price" / "crude oil price"
- "WTI oil"
- "natural gas price"
- "copper price"

### Known Issues
- FRED uses Producer Price Indices rather than spot prices for gold/silver
- For real-time commodity spot prices, external providers may be needed
- CoinGecko may route gold queries to crypto context

---

## Adding New Indicators

When you discover a new indicator during testing:

1. **Add a new section** following the template above
2. **Document all provider codes** you discover
3. **List synonyms** that should match
4. **Note coverage** (countries, frequency, units)
5. **Document known issues** or caveats
6. **Update provider_codes.json** if applicable

### Template for New Indicators

```markdown
## [Indicator Name]

### Canonical Name
[Full official name]

### Common Variations / Synonyms
- [variation 1]
- [variation 2]

### Provider Mappings

| Provider | Indicator Code | Coverage | Frequency | Units | Notes |
|----------|---------------|----------|-----------|-------|-------|
| [Provider] | [Code] | [Countries] | [Freq] | [Units] | [Notes] |

### Related Indicators
- [Related indicator 1]
- [Related indicator 2]

### Query Patterns That Should Match
- "[pattern 1]"
- "[pattern 2]"

### Known Issues
- [Issue 1]
- [Issue 2]
```

---

## Maintenance Log

| Date | Contributor | Changes |
|------|-------------|---------|
| 2025-12-25 | Claude Code | Initial index creation with GDP, Unemployment, Inflation, Interest Rates, Exchange Rates, Trade Balance, Population, Government Debt, Cryptocurrency |
| 2025-12-25 | Claude Code | Added Commodities section (Gold, Silver, Oil, Natural Gas, Copper); Fixed FRED series mappings for gold (WPU10210501) and silver (WPU10210601) |

---

*This document should be continuously updated as new indicators are discovered and mapped.*
