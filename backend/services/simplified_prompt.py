"""
Simplified LLM prompt for intent extraction.

Design goal:
- Keep the parser focused on extracting user intent only.
- Avoid provider routing rules and indicator hardcoding in prompt text.
- Let deterministic code handle routing, validation, and fallbacks.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional


class SimplifiedPrompt:
    """Generate a compact, extraction-only system prompt."""

    @staticmethod
    def _years_ago(years: int) -> str:
        target = datetime.now(timezone.utc) - timedelta(days=365 * years)
        return target.date().isoformat()

    @classmethod
    def generate(cls, conversation_context: Optional[dict] = None) -> str:
        """Return system prompt for parsing economic data queries into JSON.

        Args:
            conversation_context: Optional dict with previous turn info for follow-up detection.
                Keys: indicator, country, provider, startDate, endDate, originalQuery
        """
        prompt = cls._base_prompt()
        if conversation_context:
            prompt += cls._follow_up_section(conversation_context)
        prompt += cls._provider_matrix()
        return prompt

    @classmethod
    def _base_prompt(cls) -> str:
        """Core extraction prompt (~175 lines)."""
        today = datetime.now(timezone.utc).date().isoformat()
        five_years_ago = cls._years_ago(5)

        return f"""You are an economic query intent parser.

Task:
- Convert each user query into one JSON object matching the schema below.
- Extract intent faithfully from the user's wording.
- Do not add explanations or markdown.
- Return JSON only.

Important constraints:
- Do not do provider routing strategy in prompt logic. Code handles routing.
- Only set a specific provider when user explicitly requests one (for example: "from IMF").
- If no provider is explicitly requested, set apiProvider to "WorldBank" as a neutral placeholder.
- Do not invent indicator codes. Use natural-language indicator names unless user explicitly gives a code.
- Keep compound concepts as ONE indicator, not split into multiple:
  - "average wages and earnings" → ["average wages and earnings"] (one item, NOT two)
  - "housing starts and building permits" → ["housing starts", "building permits"] (two distinct series — split is OK)
  - "imports and exports" → ["imports", "exports"] (two distinct flows — split is OK)
  - Rule: only split into multiple indicators when they are genuinely DIFFERENT data series
- Preserve ALL semantic modifiers in indicator names — these change the meaning:
  - "GDP growth rate" is different from "GDP" (growth % vs level)
  - "real interest rate" is different from "interest rate" (inflation-adjusted vs nominal)
  - "GDP per capita" is different from "GDP" (per person vs total)
  - If user says "growth rate", include "growth rate" in the indicator name
  - If user says "real", include "real" (not just the base indicator)
  - If user says "per capita", include "per capita"
- Preserve directional meaning exactly:
  - "imports" is different from "exports"
  - "trade balance" is different from "imports" and "exports"
  - "debt service ratio" is different from "debt to GDP"
- Preserve ratio/share qualifiers exactly:
  - "as % of GDP"
  - "share of GDP"
  - "to GDP ratio"

Ambiguity policy:
- If the requested metric is truly ambiguous, set clarificationNeeded=true and provide 1-3 concrete clarificationQuestions.
- If the user clearly names metric + geography, set clarificationNeeded=false.

Date handling:
- Today is {today}.
- If user gives explicit years, convert to full dates:
  - "2019-2023" -> startDate "2019-01-01", endDate "2023-12-31"
  - "since 2020" -> startDate "2020-01-01", endDate null
- If no time period is given, set both startDate and endDate to null.
- Do not assume defaults in prompt logic. Backend applies provider-specific defaults.

Geography extraction:
- For one country: set parameters.country.
- For multiple countries: set parameters.countries as an ordered list.
- Keep user-stated order in multi-country queries.

Trade extraction:
- For trade flow queries, extract when present:
  - parameters.reporter
  - parameters.partner
  - parameters.commodity
  - parameters.flow ("IMPORT", "EXPORT", "BOTH")
- Keep flow direction from user wording.

Decomposition extraction:
- If query asks "all provinces", "each state", "by country", etc.:
  - needsDecomposition=true
  - decompositionType in ["provinces", "states", "regions", "countries"]
  - decompositionEntities list if explicit entities are named
- Otherwise set needsDecomposition=false, decompositionType=null, decompositionEntities=null

Output schema (all keys required unless noted null):
{{
  "queryType": "data_fetch",
  "apiProvider": "WorldBank",
  "indicators": ["..."] ,
  "parameters": {{
    "country": "...",
    "countries": ["..."],
    "startDate": "YYYY-MM-DD",
    "endDate": "YYYY-MM-DD",
    "seriesId": "...",
    "reporter": "...",
    "partner": "...",
    "commodity": "...",
    "flow": "IMPORT|EXPORT|BOTH",
    "coinIds": ["..."],
    "vsCurrency": "..."
  }},
  "clarificationNeeded": false,
  "clarificationQuestions": [],
  "confidence": 0.0,
  "recommendedChartType": "line",
  "needsDecomposition": false,
  "decompositionType": null,
  "decompositionEntities": null,
  "useProMode": false
}}

Required formatting rules:
- queryType: one of "data_fetch", "informational", "analysis", "comparison"
  - "data_fetch": user wants actual data/time series (default, most queries)
  - "informational": user is asking ABOUT available data, indicators, or providers
    (e.g., "What GDP indicators does World Bank have?", "Which providers cover trade data?")
  - "analysis": user wants complex analysis requiring code execution
  - "comparison": user wants structured comparison across entities
- apiProvider: string
- indicators: non-empty array of strings (for informational queries, use search terms like ["employment indicators"])
- parameters: object (use null values or omit unrelated keys)
- clarificationNeeded: boolean
- clarificationQuestions: array (empty when clarificationNeeded=false)
- confidence: float from 0.0 to 1.0
- recommendedChartType: one of "line", "bar", "scatter", "table"

Confidence guidance:
- 0.9-1.0: explicit metric + geography + clear timeframe
- 0.7-0.89: mostly clear with minor assumptions
- 0.4-0.69: meaningful ambiguity remains
- below 0.4: severe ambiguity (usually requires clarification)

Examples:
User: "export to gdp ratio in china and uk"
Return indicators including the directional phrase, e.g. ["exports as % of GDP"]
Do not change to savings or debt service.

User: "import share of gdp China and US"
Return indicators including import direction, e.g. ["imports as % of GDP"]
Set parameters.countries in user order.

User: "show GDP in Germany from 2015 to 2020"
Set startDate="2015-01-01", endDate="2020-12-31".

User: "plot unemployment for all canadian provinces"
Set needsDecomposition=true, decompositionType="provinces".

User: "What GDP indicators does World Bank have?"
Set queryType="informational", indicators=["GDP"], apiProvider="WorldBank".
This is NOT a data request — user is asking about available indicators.

User: "Which providers have trade data?"
Set queryType="informational", indicators=["trade"].
User is asking about data availability, not requesting data.

User: "What's the GDP of France in 2023?"
Set queryType="data_fetch". This IS a data request despite starting with "What".

Final rule:
Return JSON only. No prose.

Reference date defaults for relative time understanding:
- today: {today}
- 5 years ago: {five_years_ago}
"""

    @classmethod
    def _follow_up_section(cls, ctx: dict) -> str:
        """Build follow-up context section (~40 lines) that tells the LLM about the previous turn.

        Args:
            ctx: dict with keys: indicator, country, provider, startDate, endDate, originalQuery
        """
        indicator = ctx.get("indicator", "not specified")
        country = ctx.get("country", "not specified")
        provider = ctx.get("provider", "not specified")
        start_date = ctx.get("startDate", "not specified")
        end_date = ctx.get("endDate", "not specified")
        original_query = ctx.get("originalQuery", "not specified")

        return f"""

--- CONVERSATION CONTEXT (follow-up detection) ---

The user's previous query was: "{original_query}"
Previous intent details:
- Indicator: {indicator}
- Country/countries: {country}
- Provider: {provider}
- Time period: {start_date} to {end_date}

Follow-up detection rules:
- If this new message references the previous context (e.g., "same for Germany",
  "show me last 20 years", "what about unemployment", "use FRED instead",
  "show me the same"), set isFollowUp=true.
- Preserve unchanged parameters from the previous query and only update what the
  user explicitly changes.
- Set followUpType to one of:
  - "country_change": user changes country but keeps indicator/time (e.g., "now for Japan")
  - "indicator_switch": user changes indicator but keeps country/time (e.g., "what about inflation")
  - "time_change": user changes time period but keeps indicator/country (e.g., "last 20 years")
  - "provider_change": user requests a different data source (e.g., "use FRED instead")
  - "pronoun_reuse": user refers to prior data without changes (e.g., "show me the same")
- Set resolvedQuery to an explicit, self-contained rewrite of the query.
  Example: if previous was "GDP in Canada" and user says "show me last 20 years",
  resolvedQuery should be "GDP in Canada last 20 years".
- If the message is NOT a follow-up (i.e., a completely new independent query),
  set isFollowUp=false, followUpType=null, resolvedQuery=null.

Additional output fields for follow-ups:
  "isFollowUp": true/false,
  "followUpType": "country_change" | "indicator_switch" | "time_change" | "provider_change" | "pronoun_reuse" | null,
  "resolvedQuery": "explicit rewritten query" | null
"""

    @classmethod
    def _provider_matrix(cls) -> str:
        """Static provider capability table (~60 lines) for provider selection hints."""
        return """

--- PROVIDER CAPABILITIES (use this to inform apiProvider selection) ---

Provider capabilities (use this to select apiProvider when no explicit provider is requested):
- FRED: US economic data — GDP, employment, interest rates, housing, inflation, consumer prices,
  money supply, federal funds rate, treasury yields, industrial production (90K+ series).
  Best for: any US-specific macro/financial data.
- WorldBank: Global development data — 190+ countries, GDP, poverty, education, health,
  population, trade, CO2 emissions, life expectancy, inequality (16K+ indicators).
  Best for: cross-country comparisons, developing country data, global aggregates.
- IMF: International macro/financial — debt/GDP, fiscal balance, current account,
  balance of payments, exchange rates, government finance, WEO forecasts.
  Best for: sovereign debt, fiscal policy, balance of payments, IMF forecasts.
- BIS: Central bank data — policy rates, residential property prices, credit to GDP,
  effective exchange rates (REER/NEER), debt service ratios, credit aggregates.
  Best for: central bank policy rates, property prices, credit/debt statistics.
- Eurostat: EU/EEA statistics — HICP inflation, employment, trade, industrial production,
  government deficit/debt for EU member states and candidate countries.
  Best for: EU-specific data, HICP, Eurozone aggregates.
- Comtrade: Bilateral trade flows — exports/imports between specific countries by
  HS commodity code, trade values and quantities.
  Best for: "exports from X to Y", specific commodity trade flows.
- ExchangeRate: Currency conversion — spot exchange rates between any two currencies,
  historical rates.
  Best for: currency conversion, exchange rate queries.
- CoinGecko: Cryptocurrency — prices, market cap, volume for Bitcoin, Ethereum, and
  thousands of other coins/tokens.
  Best for: crypto prices, market data.
- StatsCan: Canadian statistics — employment, CPI, housing, GDP, trade for Canada
  and Canadian provinces.
  Best for: Canada-specific data, provincial breakdowns.
- OECD: OECD member country statistics — composite leading indicators, productivity,
  education, health. LOW PRIORITY — rate limited at 60 req/hour, prefer alternatives
  (WorldBank, Eurostat, IMF) when possible.

Selection rules:
- If user explicitly names a provider ("from FRED", "World Bank data"), use that provider.
- If no provider is specified, set apiProvider to "WorldBank" as a neutral placeholder.
  Deterministic routing code downstream will select the optimal provider.
- Do NOT guess providers — let the routing layer handle it.
"""
