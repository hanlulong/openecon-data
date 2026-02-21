# Trade Data Reference (UN Comtrade)

This note consolidates the Comtrade integration guide and the HS code mapping that were previously spread across multiple files.

## 1. Integration status

- Simple import/export, commodity, and bilateral queries are supported.
- Trade balance queries work for a single reporter/partner pair (the service computes exports − imports after fetching both flows).
- The provider currently uses the annual (`freqCode=A`) commodity (`typeCode=C`) endpoint: `https://comtradeapi.un.org/data/v1/get/C/A/HS`.

Environment variables:

```
COMTRADE_API_KEY=<optional subscription key>
```

If omitted, the API still works with heavier throttling.

## 2. Supported query patterns

Examples that the OpenRouter prompt understands:

```
"Show me China exports from 2020 to 2022"
"US imports of product code 640110 in 2021"
"Vietnam smartphone exports from 2019 to 2022"
"What is the trade balance between Canada and US from 2015 to 2020"
```

The parser accepts either commodity names (“shoes”, “smartphones”, “oil”) or explicit HS codes.

## 3. HS code quick reference

Use HS codes when you want fine-grained control. The provider maps common product names to the following codes:

### Footwear & apparel

| Name | Code | Notes |
|------|------|-------|
| Footwear / shoes | 64 | All footwear |
| Boots | 6401 | Waterproof footwear |
| Sneakers | 6404 | Sports footwear with textile uppers |
| Leather shoes | 6403 / 640340 | Rubber/plastic soles with leather uppers |
| Clothing (general) | 62 | Non-knit apparel |
| Shirts | 6205 | Men’s shirts |
| Dresses | 6204 | Women’s dresses |

### Electronics

| Name | Code | Notes |
|------|------|-------|
| Computers | 8471 |
| Laptops | 847130 |
| Smartphones | 851712 |
| Phones (general) | 8517 |
| Televisions / monitors | 8528 |
| Semiconductors / chips | 8542 |
| Batteries | 8506 |

### Vehicles & transport

| Name | Code | Notes |
|------|------|-------|
| Vehicles (all) | 87 |
| Cars | 8703 |
| Electric vehicles | 870380 |
| Trucks | 8704 |
| Motorcycles | 8711 |
| Bicycles | 8712 |
| Aircraft / airplanes | 8802 |

### Energy & resources

| Name | Code | Notes |
|------|------|-------|
| Mineral fuels | 27 |
| Crude oil | 2709 |
| Natural gas | 2711 |
| Coal | 2701 |
| Gold | 7108 |
| Silver | 7106 |
| Copper | 74 |
| Aluminum | 76 |

### Agricultural staples

| Name | Code | Notes |
|------|------|-------|
| Wheat | 1001 |
| Rice | 1006 |
| Corn / maize | 1005 |
| Soybeans | 1201 |
| Coffee | 0901 |
| Tea | 0902 |
| Meat | 02 |
| Fish | 03 |
| Dairy | 04 |
| Vegetables | 07 |
| Fruits & nuts | 08 |

### Chemicals, plastics, wood

| Name | Code | Notes |
|------|------|-------|
| Pharmaceuticals | 30 |
| Organic chemicals | 29 |
| Plastics | 39 |
| Rubber | 40 |
| Wood | 44 |
| Paper | 48 |

Feel free to pass either the code or the friendly name in your prompt; the intent parser normalises both.

## 4. Country codes

The provider auto-maps common ISO alpha-2/alpha-3 names to UN numerical codes. A few frequently used mappings:

| Country | UN Code |
|---------|---------|
| United States (`US`, `USA`) | 842 |
| China (`CN`, `CHN`) | 156 |
| Canada (`CA`, `CAN`) | 124 |
| Germany (`DE`, `DEU`) | 276 |
| United Kingdom (`UK`, `GB`, `GBR`) | 826 |
| Japan (`JP`, `JPN`) | 392 |
| India (`IN`, `IND`) | 699 |

If you need a code not listed here, pass the ISO alpha-3 code directly (e.g., `reporter: "BRA"`).

## 5. API URL shape

The provider returns an `apiUrl` field in `NormalizedData.metadata`, masked to show where to insert the subscription key:

```
https://comtradeapi.un.org/data/v1/get/C/A/HS?typeCode=C&freqCode=A&clCode=HS&reporterCode=842&period=2018,2022&partnerCode=0&cmdCode=27&flowCode=M,X&subscription-key=YOUR_KEY
```

Copying the link from the UI allows analysts to reproduce the request directly.


---

## Legacy Appendix: Original Integration Guide

# Comtrade API Integration - Complete Guide

## ✅ Integration Status

The UN Comtrade API has been successfully integrated into your econ-data-mcp application!

### What's Working

1. **Simple Trade Queries** ✅
   - Country exports (e.g., "Show me China exports from 2020 to 2022")
   - Country imports (e.g., "Show me US imports from 2021 to 2022")
   - Commodity-specific queries (e.g., "Show me US oil imports from 2021 to 2022")
   - Bilateral trade (e.g., "Show me US imports from China in 2020")

2. **Backend Configuration** ✅
   - API key configured in `.env`
   - Provider integrated into QueryService
   - OpenRouter LLM knows how to parse Comtrade queries
   - Health check shows Comtrade status

3. **Frontend Support** ✅
   - Charts display Comtrade data
   - Each message maintains independent chart state
   - Export to CSV/JSON works

### Known Limitations

1. **Trade Balance Queries** ⚠️
   - Currently experiencing issues when querying bilateral trade balance
   - Simple export/import queries work fine
   - Needs additional debugging for bilateral trade with BOTH flows

## How to Use

### Example Queries That Work

**General Trade Queries:**
```
"Show me China exports from 2020 to 2022"
"What are US oil imports from 2021 to 2022?"
"Show me US imports from China in 2020"
"Germany total exports from 2019 to 2022"
```

**Product-Level Queries (NEW!):**
```
"Show me China shoe exports from 2020 to 2021"
"US imports of product code 640110 in 2021"
"Vietnam smartphone exports from 2019 to 2022"
"Germany car exports in 2021"
"US imports of HS code 8703 from 2020 to 2022"
```

### API Details

- **Base URL**: `https://comtradeapi.un.org/data/v1/get/C/A/HS`
- **Authentication**: Subscription key via query parameter
- **Your API Key**: `1f3189586c8d4bd2b243c4445f0a25d7`
- **Data Format**: Annual trade data in US Dollars
- **Classification**: Harmonized System (HS)

### Supported Product Mappings

The system automatically maps product names to HS codes. You can also use:
- **Product names** (e.g., "shoes", "smartphones", "cars")
- **Direct HS codes** (e.g., "64", "8703", "640110")

**How HS Codes Work:**
- 2-digit: Chapter (broad category, e.g., 64 = Footwear)
- 4-digit: Heading (subcategory, e.g., 6401 = Waterproof footwear)
- 6-digit: Subheading (specific product, e.g., 640110 = Waterproof footwear with rubber/plastic soles)

**Common Product Mappings:**

| Product Name | HS Code | Description |
|-------------|---------|-------------|
| **Footwear & Apparel** | | |
| shoes, footwear | 64 | All footwear |
| boots | 6401 | Waterproof footwear |
| sneakers | 6404 | Sports footwear |
| clothing, apparel | 62 | Articles of apparel |
| shirts | 6205 | Men's/boys' shirts |
| **Electronics** | | |
| smartphones | 851712 | Phones for cellular networks |
| phones | 8517 | Telephone sets |
| computers | 8471 | Automatic data processing machines |
| televisions, TVs | 8528 | Monitors and projectors |
| semiconductors, chips | 8542 | Electronic integrated circuits |
| **Vehicles** | | |
| cars, automobiles | 8703 | Motor cars and vehicles |
| trucks | 8704 | Motor vehicles for transport of goods |
| motorcycles | 8711 | Motorcycles |
| electric vehicles | 870380 | Electric passenger vehicles |
| **Energy & Resources** | | |
| oil, petroleum | 27 | Mineral fuels, oils |
| crude oil | 2709 | Petroleum oils, crude |
| natural gas | 2711 | Petroleum gas |
| coal | 2701 | Coal |
| **Agricultural Products** | | |
| wheat | 1001 | Wheat and meslin |
| rice | 1006 | Rice |
| coffee | 0901 | Coffee |
| soybeans | 1201 | Soya beans |
| **Metals** | | |
| steel, iron | 72 | Iron and steel |
| aluminum | 76 | Aluminum |
| copper | 74 | Copper |
| gold | 7108 | Gold |
| **Other Categories** | | |
| pharmaceuticals, medicines | 30 | Pharmaceutical products |
| furniture | 94 | Furniture |
| toys | 95 | Toys and games |
| aircraft, airplanes | 8802 | Aircraft and spacecraft |

**💡 Tip:** You can use either the product name OR the HS code directly in your queries!

### Supported Country Codes

| Name | UN Code |
|------|---------|
| US, USA | 842 |
| CN, CHINA | 156 |
| DE, GERMANY | 276 |
| JP, JAPAN | 392 |
| UK, GB | 826 |
| FR, FRANCE | 251 |
| IN, INDIA | 699 |
| CA, CANADA | 124 |

## Test Files

Three test files have been created for you:

### 1. Comprehensive General Tests
```bash
cd /Users/hanlulong/Library/CloudStorage/Dropbox/Programs/econ-data-mcp/packages/backend
npx ts-node src/test-comtrade.ts
```

Tests:
- China exports (2020-2022)
- US oil imports (2021-2022)
- US-China trade balance (2020-2022)
- Germany total trade (2021-2022)

### 2. Product-Level Query Tests (NEW!)
```bash
npx ts-node src/test-comtrade-products.ts
```

Tests:
- Product name lookup: "shoes" → HS code 64
- Specific 6-digit HS code: 640110 (waterproof footwear)
- Modern electronics: "smartphones"
- Product categories: "cars" → HS code 8703
- Agricultural products: "coffee" → HS code 0901
- Direct chapter code: 64 (all footwear)

### 3. Simple API Test
```bash
npx ts-node src/test-comtrade-simple.ts
```

Shows raw API request/response for basic China exports query.

## Recent Fixes Applied

1. **Metadata Display** ✅
   - Fixed null values in indicator and country fields
   - Now shows proper names like "Exports - Total Trade" instead of "null - null"

2. **Chart Independence** ✅
   - Each message now maintains its own chart
   - Charts don't update when new queries are posted
   - Extracted `MessageChart` component for better state management

3. **Conversation Memory** ✅
   - Verified ConversationManager is working
   - LLM receives conversation history for context

4. **Product-Level Queries** ✅ (NEW!)
   - Added comprehensive product name → HS code mappings (90+ products)
   - Support for direct HS code input (2-digit, 4-digit, 6-digit)
   - Updated LLM prompt with product query examples
   - System automatically recognizes both product names and HS codes
   - Examples: "shoes" → 64, "640110" → 640110, "smartphones" → 851712

## Architecture

```
User Query
    ↓
Frontend (React)
    ↓
POST /api/query
    ↓
QueryService
    ↓
OpenRouter LLM (parses query)
    ↓
ComtradeProvider.fetchTradeData()
    ↓
UN Comtrade API
    ↓
NormalizedData
    ↓
Frontend Charts (Recharts)
```

## Files Modified

1. `/packages/backend/src/providers/comtrade.ts` - API provider implementation
2. `/packages/backend/src/services/query.ts` - Integration with query service
3. `/packages/backend/src/index.ts` - API key configuration
4. `/packages/backend/src/services/openrouter.ts` - LLM prompt with Comtrade examples
5. `/packages/frontend/src/components/MessageChart.tsx` - Chart component (new)
6. `/packages/frontend/src/App.tsx` - Chart state management fix
7. `/.env` - API key storage

## Next Steps (Optional Improvements)

1. **Fix Trade Balance** - Debug bilateral trade balance queries
2. **Add More Commodities** - Expand commodity code mappings
3. **Add More Countries** - Expand country code mappings
4. **Monthly Data** - Currently uses annual data, could add monthly frequency
5. **Partner Analysis** - Add specific partner country charts
6. **Rate Limiting** - Add rate limit handling for API quotas

## API Documentation

Official Comtrade API docs: https://comtradedeveloper.un.org/

## Your Servers

- **Backend**: http://localhost:3001
- **Frontend**: http://localhost:5175

Both servers are running and ready to use!

---

## Legacy Appendix: Original HS Reference

# HS Code Quick Reference Guide

## What are HS Codes?

The **Harmonized System (HS)** is an international nomenclature for the classification of products. It allows participating countries to classify traded goods on a common basis for customs purposes.

### HS Code Structure

```
XX      XXXX      XXXXXX
│       │         │
│       │         └─ 6-digit: Subheading (most specific)
│       └─────────── 4-digit: Heading
└─────────────────── 2-digit: Chapter (broad category)
```

**Example: 640110**
- `64` = Chapter 64: Footwear
- `6401` = Heading: Waterproof footwear
- `640110` = Subheading: Waterproof footwear with outer soles and uppers of rubber or plastics

## How to Use in econ-data-mcp

You can query trade data using:

1. **Product names**: `"Show me China shoe exports"`
2. **HS codes**: `"US imports of HS code 640110 in 2021"`
3. **Mixed**: `"Vietnam footwear (HS 64) exports from 2020 to 2022"`

## Complete Product Mapping

### 01-24: Live Animals & Food Products

| HS Code | Product | Description |
|---------|---------|-------------|
| 02 | Meat | Meat and edible meat offal |
| 03 | Fish | Fish and crustaceans |
| 04 | Dairy | Dairy produce, eggs, honey |
| 07 | Vegetables | Edible vegetables |
| 08 | Fruits | Edible fruit and nuts |
| 09 | Coffee, Tea, Spices | - |
| 0901 | Coffee | Coffee, whether or not roasted |
| 0902 | Tea | Tea, whether or not flavoured |
| 10 | Cereals | - |
| 1001 | Wheat | Wheat and meslin |
| 1005 | Corn | Maize (corn) |
| 1006 | Rice | Rice |
| 12 | Oil Seeds | - |
| 1201 | Soybeans | Soya beans |

### 25-27: Mineral Products

| HS Code | Product | Description |
|---------|---------|-------------|
| 27 | Mineral Fuels | Mineral fuels, oils |
| 2701 | Coal | Coal |
| 2709 | Crude Oil | Petroleum oils, crude |
| 2711 | Natural Gas | Petroleum gas and other gaseous hydrocarbons |

### 28-38: Chemicals

| HS Code | Product | Description |
|---------|---------|-------------|
| 28 | Inorganic Chemicals | Inorganic chemicals |
| 29 | Organic Chemicals | Organic chemicals |
| 30 | Pharmaceuticals | Pharmaceutical products |

### 39-40: Plastics & Rubber

| HS Code | Product | Description |
|---------|---------|-------------|
| 39 | Plastics | Plastics and articles thereof |
| 40 | Rubber | Rubber and articles thereof |

### 44-49: Wood & Paper

| HS Code | Product | Description |
|---------|---------|-------------|
| 44 | Wood | Wood and articles of wood |
| 48 | Paper | Paper and paperboard |

### 50-63: Textiles

| HS Code | Product | Description |
|---------|---------|-------------|
| 50 | Silk | Silk |
| 52 | Cotton | Cotton |
| 54 | Fabric | Man-made filaments |
| 61 | Knit Clothing | Knitted or crocheted apparel |
| 62 | Clothing | Articles of apparel, not knitted |
| 6203 | Trousers | Men's suits, trousers |
| 6204 | Dresses | Women's suits, dresses |
| 6205 | Shirts | Men's shirts |

### 64: Footwear

| HS Code | Product | Description |
|---------|---------|-------------|
| 64 | Footwear | Footwear, gaiters and the like |
| 6401 | Boots | Waterproof footwear |
| 640110 | Rubber Boots | With outer soles/uppers of rubber/plastics |
| 6402 | Sports Footwear | With rubber/plastics outer soles |
| 6403 | Leather Footwear | With rubber/plastics soles and leather uppers |
| 640340 | Leather Shoes | Leather footwear with soles other than rubber |
| 6404 | Sneakers | Footwear with textile uppers |
| 640411 | Athletic Shoes | Sports footwear, tennis shoes, basketball shoes |

### 72-83: Metals

| HS Code | Product | Description |
|---------|---------|-------------|
| 72 | Iron & Steel | Iron and steel |
| 74 | Copper | Copper and articles thereof |
| 76 | Aluminum | Aluminum and articles thereof |
| 7106 | Silver | Silver (including plated) |
| 7108 | Gold | Gold (including plated) |

### 84: Machinery & Mechanical Appliances

| HS Code | Product | Description |
|---------|---------|-------------|
| 84 | Machinery | Machinery and mechanical appliances |
| 8415 | Air Conditioners | Air conditioning machines |
| 8418 | Refrigerators | Refrigerators, freezers |
| 8443 | Printers | Printing machinery |
| 8471 | Computers | Automatic data processing machines |
| 847130 | Laptops | Portable computers |

### 85: Electrical Machinery & Equipment

| HS Code | Product | Description |
|---------|---------|-------------|
| 85 | Electronics | Electrical machinery and equipment |
| 8506 | Batteries | Primary cells and batteries |
| 8517 | Phones | Telephone sets, other apparatus |
| 851712 | Smartphones | Phones for cellular networks |
| 8528 | Televisions | Monitors and projectors |
| 8542 | Semiconductors | Electronic integrated circuits |

### 87: Vehicles

| HS Code | Product | Description |
|---------|---------|-------------|
| 87 | Vehicles | Vehicles other than railway |
| 8703 | Cars | Motor cars and other motor vehicles |
| 870380 | Electric Vehicles | Electric passenger vehicles |
| 8704 | Trucks | Motor vehicles for transport of goods |
| 8711 | Motorcycles | Motorcycles |
| 8712 | Bicycles | Bicycles and other cycles |

### 88: Aircraft

| HS Code | Product | Description |
|---------|---------|-------------|
| 88 | Aircraft | Aircraft, spacecraft |
| 8802 | Airplanes | Aircraft and spacecraft |

### 90: Optical & Medical Instruments

| HS Code | Product | Description |
|---------|---------|-------------|
| 90 | Instruments | Optical, photographic, medical instruments |

### 94-95: Miscellaneous

| HS Code | Product | Description |
|---------|---------|-------------|
| 94 | Furniture | Furniture, bedding, mattresses |
| 9401 | Chairs | Seats (other than those of heading 9402) |
| 9403 | Tables | Other furniture and parts thereof |
| 95 | Toys | Toys, games, sports requisites |

## Tips for Finding HS Codes

1. **Start broad, then narrow**: Begin with the 2-digit chapter, then drill down to 4-digit or 6-digit codes
2. **Use product names first**: The system has 90+ product mappings - try the common name first
3. **Check official resources**: For unlisted products, visit https://www.foreign-trade.com/reference/hscode.htm

## Example Queries by Industry

### Technology
```
"China smartphone exports from 2020 to 2022"
"US semiconductor imports in 2021"
"Vietnam computer exports using HS code 8471"
```

### Automotive
```
"Germany car exports from 2019 to 2022"
"US electric vehicle imports"
"Japan motorcycle exports to US"
```

### Fashion & Apparel
```
"China shoe exports from 2020 to 2021"
"Vietnam footwear exports (HS 64)"
"Bangladesh clothing exports"
```

### Agriculture
```
"Brazil coffee exports from 2020 to 2021"
"US soybean exports"
"Thailand rice exports"
```

### Energy
```
"Saudi Arabia oil exports"
"Russia natural gas exports"
"Australia coal exports"
```

## Need More Products?

If you need a product that's not in the mapping, you can:

1. **Look up the HS code** at: https://www.foreign-trade.com/reference/hscode.htm
2. **Use the code directly** in your query: `"US imports of HS code XXXXXX"`
3. **Request it to be added** to the mapping for easier future use

---

**📚 Official HS Classification**: https://www.wcoomd.org/en/topics/nomenclature/overview/what-is-the-harmonized-system.aspx
