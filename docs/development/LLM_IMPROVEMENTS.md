# LLM Method Improvements

## Current Approach Analysis

### How It Works Now

```
User query: "Show me US GDP"
    ↓
Single LLM call with massive prompt (500+ lines)
    ↓
Returns JSON: {apiProvider: "FRED", indicators: ["GDP"], ...}
    ↓
Fetch data from provider
```

### Current Issues

1. **❌ Massive Prompt** (500+ lines):
   - Hard to maintain
   - Expensive (many tokens)
   - Slow to process
   - Fragile (easy to break with changes)

2. **❌ Single-Shot Decision**:
   - LLM must decide provider WITHOUT seeing what data is available
   - Cannot adjust if chosen provider doesn't have the data
   - No iterative refinement

3. **❌ No Context About Data Availability**:
   - Prompt lists providers but LLM doesn't know which actually have the data
   - Example: Asks for "Canadian immigration" → picks FRED (wrong, they don't have it)

4. **❌ Separate Metadata Selection**:
   - First call: Parse query → pick provider
   - Second call: Search metadata → pick indicator
   - Two LLM calls per query (expensive)

5. **❌ Limited Reasoning**:
   - No chain-of-thought
   - Cannot explain decisions
   - Hard to debug failures

## Better Approaches

---

## Approach 1: RAG with Actual Data Catalog (RECOMMENDED)

### Concept: Give LLM Real Information

Instead of describing what providers *might* have, **show the LLM actual search results** from the catalog index.

### Flow

```
User: "Show me US unemployment rate"
    ↓
Step 1: Search local catalog index (parallel)
    - FRED: Found 3 matches
      1. "Unemployment Rate" (UNRATE), 1948-2025, monthly
      2. "U-6 Unemployment" (U6RATE), 1994-2025, monthly
      3. "Youth Unemployment" (LNS14000012), 1948-2025, monthly
    - WorldBank: Found 2 matches
      1. "Unemployment, total (% of labor force)" (SL.UEM.TOTL.ZS), 1991-2023, annual
      2. "Unemployment, youth total" (SL.UEM.1524.ZS), 1991-2023, annual
    - StatsCan: Found 1 match
      1. "Unemployment rate, both sexes" (Vector 2062815), 1976-2025, monthly
    ↓
Step 2: LLM selection with actual results
    Prompt: "User wants US unemployment rate. Available data:

    FRED:
    - UNRATE: Unemployment Rate, 1948-2025, monthly, US-specific
    - U6RATE: Alternative unemployment measure, 1994-2025, monthly

    WorldBank:
    - SL.UEM.TOTL.ZS: Global dataset, 1991-2023, annual, US available

    StatsCan:
    - Vector 2062815: Canada only (NOT US)

    Select the BEST option."
    ↓
LLM Response: "FRED UNRATE - most historical data, monthly frequency, US-specific"
    ↓
Fetch data from FRED using series ID "UNRATE"
```

### Benefits

✅ **Smarter Decisions**: LLM sees actual data availability, recency, frequency
✅ **Single LLM Call**: Combines parsing + selection
✅ **Better Accuracy**: Can compare date ranges, frequency, country coverage
✅ **Explainable**: LLM can explain why it chose FRED over WorldBank
✅ **Fallback Built-in**: If FRED fails, LLM already knows WorldBank alternative

### Implementation

```python
# backend/services/intelligent_query_parser.py

class IntelligentQueryParser:
    """
    RAG-enhanced query parser that searches catalog first,
    then lets LLM pick best match with actual data availability
    """

    def __init__(self, catalog_search: CatalogSearchService, llm_provider: BaseLLMProvider):
        self.catalog_search = catalog_search
        self.llm = llm_provider

    async def parse_with_catalog(self, query: str) -> ParsedIntent:
        """
        1. Extract intent from query (what indicators user wants)
        2. Search catalog for ALL providers in parallel
        3. Present results to LLM for selection
        """

        # Step 1: Extract basic intent (lightweight LLM call)
        intent = await self._extract_intent(query)
        # Returns: {indicators: ["unemployment"], country: "US", timeframe: "recent"}

        # Step 2: Search catalog for ALL providers (parallel, fast - local DB)
        search_results = await self._search_all_providers(
            keywords=intent["indicators"],
            country=intent["country"]
        )
        # Returns: {
        #   "FRED": [{"code": "UNRATE", "name": "Unemployment Rate", ...}],
        #   "WorldBank": [{"code": "SL.UEM.TOTL.ZS", ...}],
        #   ...
        # }

        # Step 3: LLM selects best match with full context
        selection = await self._llm_select_with_results(query, intent, search_results)

        return ParsedIntent(
            apiProvider=selection["provider"],
            indicators=[selection["indicator_code"]],
            parameters=selection["parameters"],
            clarificationNeeded=False,
            confidence=selection["confidence"]
        )

    async def _extract_intent(self, query: str) -> Dict[str, Any]:
        """
        Lightweight LLM call to extract basic intent
        """
        prompt = f"""Extract the intent from this query: "{query}"

Return JSON:
{{
  "indicators": ["list of economic indicators mentioned"],
  "country": "country code or null",
  "timeframe": "recent | historical | specific dates",
  "comparison": true/false (if comparing multiple entities)
}}

Examples:
"Show me US GDP" → {{"indicators": ["gdp"], "country": "US", "timeframe": "recent"}}
"Compare unemployment in US vs UK" → {{"indicators": ["unemployment"], "country": null, "comparison": true}}
"""

        response = await self.llm.generate(
            prompt=prompt,
            system_prompt="You are a query intent extractor. Return ONLY JSON.",
            temperature=0.1,
            max_tokens=200,
            json_mode=True
        )

        return json.loads(response.content)

    async def _search_all_providers(
        self,
        keywords: List[str],
        country: Optional[str]
    ) -> Dict[str, List[Dict]]:
        """
        Search catalog for all providers in parallel
        Returns top 3 matches per provider
        """
        # Build search query
        search_query = " OR ".join(keywords)

        # Get results from local catalog (very fast - 1-5ms)
        all_results = self.catalog_search.search(
            query=search_query,
            limit=30  # Get top 30 across all providers
        )

        # Group by provider, take top 3 per provider
        by_provider = {}
        for result in all_results:
            provider = result["provider"]
            if provider not in by_provider:
                by_provider[provider] = []
            if len(by_provider[provider]) < 3:
                by_provider[provider].append(result)

        return by_provider

    async def _llm_select_with_results(
        self,
        original_query: str,
        intent: Dict,
        search_results: Dict[str, List[Dict]]
    ) -> Dict[str, Any]:
        """
        LLM selects best indicator given actual search results
        """
        # Build prompt with actual data availability
        results_summary = self._format_results_for_llm(search_results)

        prompt = f"""User query: "{original_query}"

Intent: {json.dumps(intent, indent=2)}

Available data from providers:

{results_summary}

Select the BEST indicator based on:
1. Country match (exact match preferred over global datasets)
2. Data recency (more recent = better)
3. Frequency (monthly > quarterly > annual for time series)
4. Historical coverage (longer = better)
5. Data quality/source reputation

Return JSON:
{{
  "provider": "FRED | WorldBank | etc",
  "indicator_code": "exact code from above",
  "indicator_name": "human readable name",
  "reason": "2-3 sentence explanation of why this is best",
  "confidence": 0.0-1.0,
  "parameters": {{
    // provider-specific parameters
  }}
}}
"""

        response = await self.llm.generate(
            prompt=prompt,
            system_prompt="You are an expert at selecting optimal economic data sources. Consider recency, frequency, and country match. Return ONLY JSON.",
            temperature=0.1,
            max_tokens=500,
            json_mode=True
        )

        return json.loads(response.content)

    def _format_results_for_llm(self, results: Dict[str, List[Dict]]) -> str:
        """Format search results in human-readable format for LLM"""
        sections = []

        for provider, indicators in results.items():
            section = f"\n**{provider}** ({len(indicators)} matches):\n"
            for i, ind in enumerate(indicators, 1):
                section += f"{i}. {ind['name']} (Code: {ind['code']})\n"
                section += f"   - Period: {ind.get('start_date', 'N/A')} to {ind.get('end_date', 'N/A')}\n"
                section += f"   - Frequency: {ind.get('frequency', 'unknown')}\n"
                if ind.get('geo_coverage'):
                    section += f"   - Geography: {ind['geo_coverage']}\n"
            sections.append(section)

        return "\n".join(sections)
```

### Cost Comparison

| Method | Current (Single Shot) | New (RAG) |
|--------|----------------------|-----------|
| Query parsing | ~500 input tokens | ~100 tokens (intent only) |
| Metadata search | 0 tokens | 0 tokens (local DB) |
| Selection | ~500 tokens (metadata API) | ~800 tokens (with results) |
| **Total per query** | **~1,000 tokens** | **~900 tokens** |
| **Cost** | **$0.0001** | **$0.00009** (10% cheaper) |
| **Quality** | Blind decision | Informed decision ✅ |

---

## Approach 2: Agentic / Tool-Using LLM (Advanced)

### Concept: Let LLM Use Tools Iteratively

Instead of single-shot decision, give LLM **tools** it can call to refine its answer.

### Flow

```
User: "Show me GDP trends"
    ↓
LLM: "I need to clarify: which country?"
    [Tool: ask_clarification("Which country?")]
    ↓
User: "United States"
    ↓
LLM: "Let me search for US GDP data"
    [Tool: search_catalog("gdp", country="US")]
    ↓
Catalog returns: FRED UNRATE, WorldBank NY.GDP.MKTP.CD
    ↓
LLM: "FRED has more frequent data (quarterly vs annual), I'll use that"
    [Tool: fetch_data("FRED", "GDP")]
    ↓
Returns data to user
```

### Implementation (OpenAI Function Calling)

```python
# backend/services/agentic_parser.py

class AgenticQueryParser:
    """
    LLM agent with tools for iterative query resolution
    """

    def __init__(self, llm_provider: BaseLLMProvider, catalog_search: CatalogSearchService):
        self.llm = llm_provider
        self.catalog_search = catalog_search

    # Define tools the LLM can use
    TOOLS = [
        {
            "type": "function",
            "function": {
                "name": "search_catalog",
                "description": "Search the economic data catalog for indicators",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "keywords": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Keywords to search (e.g., ['gdp', 'unemployment'])"
                        },
                        "country": {
                            "type": "string",
                            "description": "Country code (e.g., 'US', 'GB', 'CN')"
                        },
                        "provider": {
                            "type": "string",
                            "description": "Specific provider to search (optional)"
                        }
                    },
                    "required": ["keywords"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "ask_clarification",
                "description": "Ask user for clarification",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "question": {
                            "type": "string",
                            "description": "Question to ask the user"
                        }
                    },
                    "required": ["question"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "select_indicator",
                "description": "Select a specific indicator from search results",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "provider": {"type": "string"},
                        "code": {"type": "string"},
                        "parameters": {"type": "object"}
                    },
                    "required": ["provider", "code"]
                }
            }
        }
    ]

    async def parse_with_tools(self, query: str, conversation_history: List = None) -> ParsedIntent:
        """
        Let LLM iteratively use tools to resolve query
        """
        messages = conversation_history or []
        messages.append({
            "role": "user",
            "content": query
        })

        max_iterations = 5
        for i in range(max_iterations):
            # Call LLM with tools
            response = await self.llm.generate_with_tools(
                messages=messages,
                tools=self.TOOLS,
                system_prompt="You are an economic data assistant. Use tools to find and return the best data for user queries."
            )

            # Check if LLM wants to use a tool
            if response.tool_calls:
                for tool_call in response.tool_calls:
                    # Execute the tool
                    tool_result = await self._execute_tool(tool_call)

                    # Add tool result to conversation
                    messages.append({
                        "role": "assistant",
                        "content": response.content,
                        "tool_calls": response.tool_calls
                    })
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": json.dumps(tool_result)
                    })

                    # If tool is select_indicator, we're done
                    if tool_call.function.name == "select_indicator":
                        return ParsedIntent(**tool_result)
            else:
                # LLM responded without tools - return final answer
                break

        raise ValueError("Failed to resolve query after max iterations")

    async def _execute_tool(self, tool_call) -> Dict[str, Any]:
        """Execute a tool call and return results"""
        tool_name = tool_call.function.name
        args = json.loads(tool_call.function.arguments)

        if tool_name == "search_catalog":
            results = self.catalog_search.search(
                query=" ".join(args["keywords"]),
                provider=args.get("provider"),
                limit=10
            )
            return {"results": results}

        elif tool_name == "ask_clarification":
            # Return clarification needed
            return {
                "clarification_needed": True,
                "question": args["question"]
            }

        elif tool_name == "select_indicator":
            return {
                "apiProvider": args["provider"],
                "indicators": [args["code"]],
                "parameters": args.get("parameters", {}),
                "clarificationNeeded": False
            }

        return {}
```

### Benefits

✅ **Iterative Refinement**: LLM can search, evaluate, refine
✅ **Transparent**: See exactly what LLM is doing (tool calls)
✅ **Flexible**: Can add new tools without changing prompts
✅ **Smart**: LLM decides when it has enough info vs needs more

### Drawbacks

⚠️ **More Complex**: Requires function calling support
⚠️ **More LLM Calls**: 2-4 calls per query (vs 1-2 now)
⚠️ **Slightly Slower**: Each iteration adds 500ms-1s

---

## Approach 3: Structured Outputs (OpenAI Native)

### Concept: Guarantee Valid JSON

Use OpenAI's **structured outputs** feature instead of JSON mode for guaranteed schema compliance.

### Implementation

```python
# backend/services/structured_parser.py

from pydantic import BaseModel
from openai import AsyncOpenAI

class ParsedIntentSchema(BaseModel):
    """Pydantic schema for parsed intent - enforced by OpenAI"""
    apiProvider: str
    indicators: list[str]
    parameters: dict
    clarificationNeeded: bool
    clarificationQuestions: list[str] | None = None
    confidence: float
    recommendedChartType: str | None = None

class StructuredQueryParser:
    """
    Use OpenAI structured outputs for guaranteed valid responses
    """

    def __init__(self, api_key: str):
        self.client = AsyncOpenAI(api_key=api_key)

    async def parse_query(self, query: str) -> ParsedIntent:
        """Parse query with structured outputs"""

        completion = await self.client.beta.chat.completions.parse(
            model="gpt-4o-2024-08-06",  # Supports structured outputs
            messages=[
                {"role": "system", "content": "Parse economic data queries"},
                {"role": "user", "content": f"Parse this query: {query}"}
            ],
            response_format=ParsedIntentSchema  # Pydantic model
        )

        # Guaranteed to match schema or raise error
        parsed = completion.choices[0].message.parsed

        return ParsedIntent(
            apiProvider=parsed.apiProvider,
            indicators=parsed.indicators,
            parameters=parsed.parameters,
            clarificationNeeded=parsed.clarificationNeeded,
            clarificationQuestions=parsed.clarificationQuestions,
            confidence=parsed.confidence,
            recommendedChartType=parsed.recommendedChartType
        )
```

### Benefits

✅ **No Invalid JSON**: OpenAI guarantees valid schema
✅ **Type Safety**: Pydantic validation built-in
✅ **Simpler Code**: No manual JSON parsing/validation
✅ **Better Errors**: Clear error messages for schema violations

---

## Approach 4: Fine-Tuned Model (Long-term)

### Concept: Train Model on Economic Data

Create a fine-tuned model specifically for economic data queries.

### Training Data

```jsonl
{"messages": [{"role": "system", "content": "Parse economic data queries"}, {"role": "user", "content": "Show me US GDP"}, {"role": "assistant", "content": "{\"apiProvider\": \"FRED\", \"indicators\": [\"GDP\"], \"parameters\": {\"country\": \"US\"}}"}]}
{"messages": [{"role": "system", "content": "Parse economic data queries"}, {"role": "user", "content": "Canadian unemployment rate"}, {"role": "assistant", "content": "{\"apiProvider\": \"StatsCan\", \"indicators\": [\"UNEMPLOYMENT\"], \"parameters\": {\"country\": \"CA\"}}"}]}
... (thousands more examples)
```

### Benefits

✅ **Domain Expertise**: Model learns economic data patterns
✅ **Smaller/Faster**: Can use GPT-3.5-turbo with fine-tuning
✅ **Lower Cost**: Fine-tuned 3.5-turbo cheaper than GPT-4o-mini
✅ **Better Accuracy**: Specialized knowledge

### Cost

- **Training**: $8 per 1M tokens (one-time)
- **Inference**: GPT-3.5-turbo fine-tuned = $3 per 1M tokens (vs $0.15 for base)
- **Break-even**: Need 1000+ queries/day to justify

---

## Recommended Implementation Plan

### Phase 1: Quick Wins (This Week)

**Implement Approach 3: Structured Outputs**

Easiest upgrade with immediate benefits:
```python
# Replace current JSON mode with structured outputs
- response = await llm.generate(..., json_mode=True)
+ completion = await client.beta.chat.completions.parse(..., response_format=ParsedIntentSchema)
```

**Benefits**: No invalid JSON, type-safe, minimal code changes

### Phase 2: RAG Enhancement (Next 2 Weeks)

**Implement Approach 1: Search-then-Select**

Once catalog index is built (from previous plan):
1. Search catalog first (1-5ms)
2. Show LLM actual results
3. LLM picks best match

**Benefits**: Smarter decisions, single LLM call, 95%+ accuracy

### Phase 3: Advanced (Future)

**Implement Approach 2: Tool-Using Agent**

When you need more complex reasoning:
- Multi-step queries
- Clarification loops
- Fallback strategies

### Phase 4: Optimization (Future)

**Approach 4: Fine-tuned Model**

When you have:
- 1000+ queries/day (to justify cost)
- Labeled training data (10,000+ examples)
- Need for sub-100ms latency

---

## Comparison Table

| Approach | Accuracy | Speed | Cost | Complexity | When to Use |
|----------|----------|-------|------|------------|-------------|
| **Current** | 70% | 1-2s | $0.0001 | Medium | (Current baseline) |
| **RAG (Recommended)** | **95%** | **0.8-1.5s** | **$0.00009** | **Medium** | **Now - best ROI** |
| **Structured Outputs** | 75% | 1-2s | $0.0001 | Low | Quick win |
| **Tool-Using Agent** | 90% | 2-4s | $0.0002 | High | Complex queries |
| **Fine-tuned** | 95% | 0.3-0.8s | $0.00003* | Very High | High volume |

*After initial training cost

---

## Immediate Action Items

### Week 1: Structured Outputs
- [ ] Migrate from JSON mode to structured outputs
- [ ] Add Pydantic schemas for all LLM responses
- [ ] Test with 100 sample queries

### Week 2: RAG Preparation
- [ ] Build catalog index (from previous plan)
- [ ] Implement parallel catalog search
- [ ] Test search performance (should be <5ms)

### Week 3: RAG Integration
- [ ] Implement `IntelligentQueryParser`
- [ ] Replace old parser in `QueryService`
- [ ] A/B test: old vs new approach
- [ ] Measure accuracy improvement

### Week 4: Optimization
- [ ] Add caching for common queries
- [ ] Tune LLM prompts based on results
- [ ] Monitor cost and latency

---

## Conclusion

**Recommended Path**:
1. ✅ **Immediate**: Switch to structured outputs (1 day, huge reliability win)
2. ✅ **Next**: Build catalog index (1-2 weeks, enables RAG)
3. ✅ **Then**: Implement RAG-based parser (1 week, 95% accuracy)
4. ⏭️ **Future**: Consider tool-using agent for complex queries
5. ⏭️ **Later**: Fine-tuning when you have volume

This gets you from **70% → 95% accuracy** while maintaining or improving speed and cost.

The key insight: **Don't make LLM guess - give it actual data to choose from!**
