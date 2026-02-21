"""
Grok service for Pro Mode code generation

This module provides AI-powered Python code generation for advanced economic
data analysis and visualization. Features include:
- Multiple visualization types (line, bar, heatmap, scatter, area)
- Automatic error recovery and code fixing
- Session context awareness
- Statistics Canada and other API integrations

Updated: 2025-12-25 - Enhanced prompts and visualization support
"""
import logging
from typing import Dict, Any, Optional, AsyncIterator, List
import httpx
import json
import re

from backend.config import get_settings

logger = logging.getLogger(__name__)

# Visualization type templates
VISUALIZATION_TEMPLATES = {
    "line": {
        "description": "Time series line chart",
        "template": """
plt.figure(figsize=(12, 6))
plt.plot(df['date'], df['value'], marker='o', linewidth=2)
plt.xlabel('Date')
plt.ylabel('Value')
plt.title('Time Series')
plt.grid(True, alpha=0.3)
plt.xticks(rotation=45)
plt.tight_layout()
""",
    },
    "bar": {
        "description": "Bar chart for comparisons",
        "template": """
plt.figure(figsize=(12, 6))
plt.bar(df['category'], df['value'], color='steelblue')
plt.xlabel('Category')
plt.ylabel('Value')
plt.title('Comparison')
plt.xticks(rotation=45, ha='right')
plt.tight_layout()
""",
    },
    "heatmap": {
        "description": "Heatmap for correlation matrices",
        "template": """
import seaborn as sns
plt.figure(figsize=(10, 8))
sns.heatmap(correlation_matrix, annot=True, cmap='coolwarm', center=0)
plt.title('Correlation Heatmap')
plt.tight_layout()
""",
    },
    "scatter": {
        "description": "Scatter plot for relationship analysis",
        "template": """
plt.figure(figsize=(10, 8))
plt.scatter(df['x'], df['y'], alpha=0.6, edgecolors='black', linewidth=0.5)
plt.xlabel('X Variable')
plt.ylabel('Y Variable')
plt.title('Scatter Plot')
plt.grid(True, alpha=0.3)
plt.tight_layout()
""",
    },
    "area": {
        "description": "Stacked area chart",
        "template": """
plt.figure(figsize=(12, 6))
plt.stackplot(dates, values, labels=labels, alpha=0.8)
plt.xlabel('Date')
plt.ylabel('Value')
plt.title('Stacked Area Chart')
plt.legend(loc='upper left')
plt.tight_layout()
""",
    },
    "dual_axis": {
        "description": "Dual Y-axis chart for different scales",
        "template": """
fig, ax1 = plt.subplots(figsize=(12, 6))
ax1.plot(dates, values1, 'b-', label='Series 1')
ax1.set_xlabel('Date')
ax1.set_ylabel('Series 1', color='blue')
ax2 = ax1.twinx()
ax2.plot(dates, values2, 'r-', label='Series 2')
ax2.set_ylabel('Series 2', color='red')
plt.title('Dual Axis Chart')
fig.tight_layout()
""",
    },
}


class GrokService:
    """
    Service for generating Python code using x-ai/grok-code-fast-1 model.

    Features:
    - Enhanced prompts for economic data analysis
    - Multiple visualization types support
    - Automatic error recovery and code fixing
    - Session-aware code generation
    """

    def __init__(self):
        settings = get_settings()
        self.api_key = settings.openrouter_api_key
        self.app_url = settings.app_url
        self.base_url = "https://openrouter.ai/api/v1"
        self.model = "x-ai/grok-code-fast-1"
        self.max_retries = 2  # For error recovery
        self.visualization_templates = VISUALIZATION_TEMPLATES

    def _detect_visualization_type(self, query: str) -> Optional[str]:
        """Detect the appropriate visualization type from the query."""
        query_lower = query.lower()

        if any(word in query_lower for word in ["heatmap", "correlation matrix", "correlate"]):
            return "heatmap"
        elif any(word in query_lower for word in ["scatter", "relationship", "vs ", " x y"]):
            return "scatter"
        elif any(word in query_lower for word in ["stacked", "area chart", "cumulative"]):
            return "area"
        elif any(word in query_lower for word in ["bar chart", "compare", "ranking"]):
            return "bar"
        elif any(word in query_lower for word in ["dual axis", "two scales", "different units"]):
            return "dual_axis"
        else:
            return "line"  # Default for time series

    async def fix_code_errors(
        self,
        original_code: str,
        error_message: str,
        attempt: int = 1
    ) -> str:
        """
        Attempt to fix code errors automatically.

        Args:
            original_code: The code that produced the error
            error_message: The error message
            attempt: Current retry attempt number

        Returns:
            Fixed code
        """
        if attempt > self.max_retries:
            raise Exception(f"Max retries ({self.max_retries}) exceeded. Last error: {error_message}")

        fix_prompt = f"""The following Python code produced an error. Fix it.

ORIGINAL CODE:
```python
{original_code}
```

ERROR MESSAGE:
{error_message}

REQUIREMENTS:
1. Fix ONLY the error - don't change working parts
2. If the error is about missing imports, add them
3. If the error is about None values, add null checks
4. If the error is about API failures, add error handling
5. Return ONLY the fixed Python code, no explanations

COMMON FIXES:
- TypeError with None: Add `if value is not None:` checks
- KeyError: Add `.get()` with defaults
- API errors: Add try/except with fallback
- Import errors: Add the missing import

Output ONLY executable Python code."""

        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.post(
                    f"{self.base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                        "HTTP-Referer": self.app_url,
                        "X-Title": "econ-data-mcp Pro Mode Error Fix"
                    },
                    json={
                        "model": self.model,
                        "messages": [
                            {"role": "system", "content": "You are an expert Python debugger. Fix code errors precisely."},
                            {"role": "user", "content": fix_prompt}
                        ],
                        "temperature": 0.1,  # Very low for precise fixes
                        "max_tokens": 4000,
                    }
                )

                if response.status_code != 200:
                    raise Exception(f"API error: {response.status_code}")

                data = response.json()
                fixed_code = data["choices"][0]["message"]["content"]
                fixed_code = self._extract_code_from_markdown(fixed_code)

                logger.info(f"Code fix attempt {attempt} completed")
                return fixed_code

        except Exception as e:
            logger.error(f"Error fixing code: {e}")
            raise

    async def generate_code(
        self,
        query: str,
        conversation_history: Optional[list] = None,
        available_data: Optional[Dict[str, Any]] = None,
        session_id: Optional[str] = None,
        tracker: Optional[Any] = None  # For backward compatibility, not used
    ) -> str:
        """
        Generate Python code based on user query and available economic data

        Args:
            query: User's natural language query
            conversation_history: Previous conversation messages for context
            available_data: Information about available data sources and variables
            session_id: Unique session identifier for file naming

        Returns:
            Generated Python code as a string
        """
        try:
            # Build the system prompt for code generation
            system_prompt = self._build_system_prompt(available_data, session_id)

            # Build the messages for the API call
            messages = []
            if conversation_history:
                messages.extend(conversation_history)

            # Detect visualization type for hints
            viz_type = self._detect_visualization_type(query)
            viz_hint = ""
            if viz_type and viz_type in self.visualization_templates:
                viz_info = self.visualization_templates[viz_type]
                viz_hint = f"\n\nRECOMMENDED VISUALIZATION: {viz_type} chart - {viz_info['description']}"

            messages.append({
                "role": "user",
                "content": f"""Generate Python code to fulfill this request:

{query}

Requirements:
- Write clean, well-commented Python code
- Include all necessary imports
- Use matplotlib for visualizations if needed
- Handle errors gracefully with try/except
- Print results clearly with context
- Keep code concise but functional
- Use pandas for data manipulation if appropriate
- Always check for None/empty values before processing
- Use .get() for dictionary access to avoid KeyError{viz_hint}

VISUALIZATION BEST PRACTICES:
- Use figsize=(12, 6) for standard charts
- Add grid with alpha=0.3 for readability
- Use tight_layout() to prevent label cutoff
- Format dates with matplotlib.dates if applicable
- Add clear titles and axis labels

Output ONLY the Python code, no explanations."""
            })

            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.post(
                    f"{self.base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                        "HTTP-Referer": self.app_url,
                        "X-Title": "econ-data-mcp Pro Mode"
                    },
                    json={
                        "model": self.model,
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            *messages
                        ],
                        "temperature": 0.3,  # Lower temperature for more deterministic code
                        "max_tokens": 4000,
                    }
                )

                if response.status_code != 200:
                    logger.error(f"Grok API error: {response.status_code} - {response.text}")
                    raise Exception(f"Grok API returned status {response.status_code}")

                data = response.json()

                # Defensive parsing of API response
                if not data or "choices" not in data or not data["choices"]:
                    logger.error(f"Grok API returned unexpected response structure: {data}")
                    raise Exception("Grok API returned empty or malformed response")

                first_choice = data["choices"][0]
                if "message" not in first_choice or "content" not in first_choice["message"]:
                    logger.error(f"Grok API response missing message content: {first_choice}")
                    raise Exception("Grok API response missing message content")

                generated_code = first_choice["message"]["content"]

                # Extract code from markdown blocks if present
                generated_code = self._extract_code_from_markdown(generated_code)

                logger.info(f"Generated code length: {len(generated_code)} characters")
                return generated_code

        except Exception as e:
            logger.error(f"Error generating code with Grok: {str(e)}")
            raise

    async def generate_code_stream(
        self,
        query: str,
        conversation_history: Optional[list] = None,
        available_data: Optional[Dict[str, Any]] = None,
        session_id: Optional[str] = None,
        tracker: Optional[Any] = None  # For backward compatibility, not used
    ) -> AsyncIterator[str]:
        """
        Generate Python code with streaming support - yields code chunks as they arrive

        Args:
            query: User's natural language query
            conversation_history: Previous conversation messages for context
            available_data: Information about available data sources and variables
            session_id: Unique session identifier for file naming

        Yields:
            Code chunks as strings as they are generated
        """
        try:
            # Build the system prompt for code generation
            system_prompt = self._build_system_prompt(available_data, session_id)

            # Build the messages for the API call
            messages = []
            if conversation_history:
                messages.extend(conversation_history)

            messages.append({
                "role": "user",
                "content": f"""Generate Python code to fulfill this request:

{query}

Requirements:
- Write clean, well-commented Python code
- Include all necessary imports
- Use matplotlib for visualizations if needed
- Handle errors gracefully
- Print results clearly
- Keep code concise but functional
- Use pandas for data manipulation if appropriate

Output ONLY the Python code, no explanations."""
            })

            request_payload = {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    *messages
                ],
                "temperature": 0.3,
                "max_tokens": 4000,
                "stream": True  # Enable streaming
            }

            async with httpx.AsyncClient(timeout=60.0) as client:
                async with client.stream(
                    "POST",
                    f"{self.base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                        "HTTP-Referer": self.app_url,
                        "X-Title": "econ-data-mcp Pro Mode"
                    },
                    json=request_payload
                ) as response:
                    if response.status_code != 200:
                        error_text = await response.aread()
                        logger.error(f"Grok API error: {response.status_code} - {error_text}")
                        raise Exception(f"Grok API returned status {response.status_code}")

                    # Process streaming response (Server-Sent Events format)
                    async for line in response.aiter_lines():
                        if line.startswith("data: "):
                            data_str = line[6:]  # Remove "data: " prefix

                            if data_str == "[DONE]":
                                break

                            try:
                                data = json.loads(data_str)
                                # Check for content in delta
                                if "choices" in data and len(data["choices"]) > 0:
                                    delta = data["choices"][0].get("delta", {})
                                    if "content" in delta:
                                        yield delta["content"]
                            except json.JSONDecodeError:
                                # Skip malformed JSON chunks
                                continue

        except Exception as e:
            logger.error(f"Error generating code with Grok (streaming): {str(e)}")
            raise

    def _build_system_prompt(self, available_data: Optional[Dict[str, Any]] = None, session_id: Optional[str] = None) -> str:
        """Build system prompt for code generation"""
        file_prefix = session_id if session_id else "output"

        prompt = f"""Expert Python programmer for economic data analysis and visualization.

Generate code that:
- Performs data analysis/calculations/visualizations as requested
- Uses: pandas, numpy, matplotlib, httpx, json, datetime, statistics
- For HTTP requests, use httpx (async: httpx.AsyncClient(), sync: httpx.get/post)
- Clear comments, error handling, descriptive variables
- Builds upon conversation context
- REUSES existing session data (DO NOT refetch if data exists!)

⚠️ **CRITICAL - NEVER USE exit() OR sys.exit()** ⚠️
- Do NOT use exit(), sys.exit(), quit(), or raise SystemExit
- Instead, use if/else to handle errors and print informative messages
- Let the code complete naturally, even on errors
- Example: Instead of `exit()`, use `print("Error: description")` and continue or return

SESSION STORAGE (minimize API calls):
⚠️ CRITICAL: save_session(), load_session(), and list_session_data() are ALREADY DEFINED and INJECTED into the execution environment!
⚠️ DO NOT define these functions yourself! DO NOT create placeholders! Just USE them directly!
⚠️ If you define your own versions, session persistence WILL BREAK!

MANDATORY pattern (always include fallback):
  data = load_session('my_data')  # Returns list/dict (NOT DataFrame!)
  if data is None:
      df = fetch_data()  # Your fetch code
      save_session('my_data', df)  # DataFrames auto-converted to list of dicts
      data = df.to_dict('records') if hasattr(df, 'to_dict') else df
  else:
      df = pd.DataFrame(data)  # Convert loaded data back to DataFrame
  # Now use df

⚠️ IMPORTANT: load_session() returns JSON data (list/dict), NOT DataFrame!
- When loading, always convert: df = pd.DataFrame(load_session('key'))
- When saving, DataFrames are auto-converted to list of dicts
- Use the pattern above to handle both fresh fetches and follow-up queries

Benefits: 10-100x faster follow-ups, data persists 24h, works first query or follow-up

MATPLOTLIB (REQUIRED):
- matplotlib.use('Agg')  # BEFORE pyplot import!
- Save plots: /tmp/promode_{file_prefix}_<descriptive_name>.png
- Professional format: plt.xlabel(), plt.ylabel(), plt.title(), plt.tight_layout(), plt.grid(True, alpha=0.3), figsize=(12,6)
- Date formatting: use matplotlib.dates.DateFormatter, AutoDateLocator
- Print full path to saved files

STATISTICS CANADA API (most common for Canadian data):

**Vector API** (SIMPLE - for national aggregates):
- URL: https://www150.statcan.gc.ca/t1/wds/rest/getDataFromVectorsAndLatestNPeriods
- Common vectors: Unemployment=2062815, GDP=65201210, CPI=41690973
- Usage: POST with [{{"vectorId": 2062815, "latestN": 20}}]
- Parse: data[0]["object"]["vectorDataPoint"], check `if point["value"] is not None`

**Coordinate API** (ADVANCED - for dimensional/categorical data):
- URL: https://www150.statcan.gc.ca/t1/wds/rest/getDataFromCubePidCoordAndLatestNPeriods
- Use for queries requesting breakdowns by geography, gender, age groups, etc.
- Requires proper product ID and coordinates based on cube dimensions

⚠️ **CRITICAL: Statistics Canada API Payload Format** ⚠️
ALL Statistics Canada WDS APIs require payloads as ARRAYS, not single objects!
- ✅ CORRECT: payload = [{{"productId": 14100287}}]  (array with one object)
- ❌ WRONG: payload = {{"productId": 14100287}}  (single object - returns 406 error)

**Metadata API** (if you need cube structure - USE SPARINGLY):
- URL: https://www150.statcan.gc.ca/t1/wds/rest/getCubeMetadata
- Payload: [{{"productId": 14100287}}] - MUST be an array!
- Returns dimension structure with member IDs

**HOW TO USE COORDINATE API:**
1. Check if metadata is available in session context (see AVAILABLE SESSION DATA below)
2. If statscan_metadata exists, use the dimension ID mappings from discovered metadata
3. Construct coordinates using EXACTLY 10 period-separated values (CRITICAL: must be 10 values!)
4. For products with fewer than 10 dimensions, PAD WITH ZEROS at the end
5. Example for product 14100287 (6 dimensions):
   - Product has: Geography, Labour force chars, Gender, Age group, Statistics, Data type
   - Coordinate format: "geo.char.gender.age.stat.dtype.0.0.0.0" (6 dimension IDs + 4 zeros)
   - Specific example: "1.7.1.2.1.1.0.0.0.0" = Canada, Unemployment rate, Total Gender, 15-24 years, Estimate, Seasonally adjusted
6. Make API call with product ID (integer) and 10-value coordinate
7. Handle failures gracefully - if coordinate fails, fall back to Vector API

**COORDINATE FORMAT RULES (CRITICAL):**
- ALWAYS EXACTLY 10 values separated by periods (e.g., "1.7.1.2.1.1.0.0.0.0")
- Use dimension member IDs from discovered metadata (NOT made-up numbers)
- If product has N dimensions where N < 10, use N dimension IDs followed by (10-N) zeros
- Product 14100287 has 6 dimensions, so format is: "dim1.dim2.dim3.dim4.dim5.dim6.0.0.0.0"
- Example working coordinate: "1.7.1.2.1.1.0.0.0.0" successfully returns unemployment data

CRITICAL RULES:
- ✅ Handle None values: `if point["value"] is not None`
- ✅ Check empty DataFrame: `if not df.empty:` before accessing columns
- ✅ Use statscan_metadata from session context when available
- ✅ Print which dimension IDs you're using for transparency
- ⚠️ If coordinate fails or metadata unavailable, fall back to Vector API for aggregates

OTHER APIs:
- FRED: https://api.stlouisfed.org/fred/series/observations?series_id=X&api_key=Y (often unavailable)
- World Bank: https://api.worldbank.org/v2/country/CODE/indicator/IND?format=json

DERIVED METRICS: Fetch raw data, calculate in Python, use hardcoded constants for stable reference data

IMPORTANT: Output ONLY executable Python code. No explanations, no markdown formatting, just pure Python code."""

        if available_data:
            prompt += f"\n\n🔍 AVAILABLE SESSION DATA:\n"

            # Handle session data keys
            if "session_data_available" in available_data:
                prompt += f"The following data is ALREADY LOADED in session storage:\n"
                for key in available_data["session_data_available"]:
                    prompt += f"  - '{key}' (use: load_session('{key}'))\n"
                prompt += f"\n⚡ IMPORTANT: Use load_session() to access this data - DO NOT refetch it!\n\n"

            # Handle Statistics Canada discovered metadata
            if "statscan_metadata" in available_data:
                metadata = available_data["statscan_metadata"]
                product_id = metadata.get("product_id")
                product_title = metadata.get("product_title", "Unknown")
                dimensions = metadata.get("dimensions", {})

                prompt += f"""
📋 STATISTICS CANADA DISCOVERED METADATA:

Product ID: {product_id}
Product Title: {product_title}
Data Range: {metadata.get('cube_start_date', 'N/A')} to {metadata.get('cube_end_date', 'N/A')}

AVAILABLE DIMENSIONS:
"""
                # List all available dimensions
                for dim_name, members in dimensions.items():
                    # Show first few members as examples
                    example_members = list(members.items())[:5]
                    prompt += f"\n  {dim_name}:\n"
                    for member_name, member_id in example_members:
                        prompt += f"    - '{member_name}': {member_id}\n"
                    if len(members) > 5:
                        prompt += f"    ... and {len(members) - 5} more members\n"

                prompt += f"""

HOW TO USE COORDINATE API WITH DISCOVERED METADATA:

1. Build coordinates using dimension member IDs from above
2. Coordinate format: period-separated dimension IDs (e.g., "1.7.1.2.1.1" for 6 dimensions)
3. Number of dimensions = {len(dimensions)} (one ID per dimension)
4. For product {product_id}, dimensions are: {', '.join(dimensions.keys())}

USAGE EXAMPLE (Coordinate API with discovered metadata):
```python
import httpx
import pandas as pd

url = "https://www150.statcan.gc.ca/t1/wds/rest/getDataFromCubePidCoordAndLatestNPeriods"

# Example: For product {product_id} with {len(dimensions)} dimensions
# Build coordinate by looking up member IDs from dimensions dict above
# Format: "dim1_id.dim2_id.dim3_id..." ({len(dimensions)} values)

# Example coordinate (adjust based on your needs):
coordinate = "1.7.1.2.1.1"  # Sample: depends on which categories you want

payload = [{{
    "productId": "{product_id}",
    "coordinate": coordinate,
    "latestN": 24
}}]

# Use httpx synchronous client (async not needed in Pro Mode sandbox)
with httpx.Client(timeout=30) as client:
    response = client.post(url, json=payload)
if response.status_code == 200:
    data = response.json()
    if data and data[0].get("status") == "SUCCESS":
        points = data[0]["object"]["vectorDataPoint"]
        results = []
        for point in points:
            if point["value"] is not None:
                results.append({{
                    'date': point['refPer'],
                    'value': point['value']
                }})
        df = pd.DataFrame(results)
        print(f"Fetched {{len(df)}} data points")
    else:
        print(f"API returned status: {{data[0].get('status')}}")
```

CRITICAL INSTRUCTIONS:
- ✅ HARDCODE dimension member IDs from the discovered metadata shown above
- ✅ Build coordinates using the actual member IDs listed above (e.g., if "15 to 24 years": 2, then use 2 in your coordinate)
- ✅ Always check if point["value"] is not None
- ✅ Handle API failures gracefully
- ✅ For breakdowns by category, loop through relevant dimension members
- ⚠️ DO NOT try to load metadata from session storage - all metadata you need is already provided above
- ⚠️ NEVER define save_session(), load_session(), or list_session_data() - they are ALREADY INJECTED and available!
- ⚠️ DO NOT use any placeholder or incorrect dimension IDs - use ONLY the exact IDs shown in the metadata above

"""

            # Handle Statistics Canada vector IDs (fallback if no metadata discovered)
            elif "statscan_vectors" in available_data:
                vectors = available_data["statscan_vectors"]
                prompt += f"""
📋 STATISTICS CANADA VERIFIED VECTOR IDs:

Available vectors:
"""
                for key, vector_id in vectors.items():
                    if key != "note":
                        prompt += f"  - {key}: {vector_id}\n"

                prompt += f"""

USAGE EXAMPLE (Vector API):
```python
import httpx
import pandas as pd

url = "https://www150.statcan.gc.ca/t1/wds/rest/getDataFromVectorsAndLatestNPeriods"
payload = [{{"vectorId": 2062815, "latestN": 24}}]

with httpx.Client(timeout=30) as client:
    response = client.post(url, json=payload)
if response.status_code == 200:
    data = response.json()
    if data and data[0].get("status") == "SUCCESS":
        points = data[0]["object"]["vectorDataPoint"]
        df = pd.DataFrame([{{'date': p['refPer'], 'value': p['value']}}
                          for p in points if p["value"] is not None])
```

⚠️ Always handle None values: `if point["value"] is not None`
"""

            # Handle DataReferences from previous standard mode queries
            # These are the key for Standard → Pro Mode handoff
            data_ref_keys = [k for k in available_data.keys()
                            if k not in ["session_data_available", "statscan_vectors", "statscan_metadata", "_context"]]

            if data_ref_keys:
                prompt += f"\n\n📊 DATA FROM PREVIOUS QUERIES (ALREADY FETCHED - DO NOT REFETCH!):\n"
                prompt += "The following data was fetched in previous queries and is available for your calculations:\n\n"

                for key in data_ref_keys:
                    ref_data = available_data[key]
                    if isinstance(ref_data, dict) and "data" in ref_data:
                        provider = ref_data.get("provider", "Unknown")
                        unit = ref_data.get("unit", "")
                        metadata = ref_data.get("metadata", {})
                        data_points = ref_data.get("data", [])
                        data_count = len(data_points) if data_points else 0

                        prompt += f"  📈 Variable: `{key}`\n"
                        prompt += f"     Provider: {provider}\n"
                        if unit:
                            prompt += f"     Unit: {unit}\n"
                        prompt += f"     Data Points: {data_count}\n"

                        # Show sample data if available
                        if data_points and len(data_points) > 0:
                            sample = data_points[:3] if len(data_points) >= 3 else data_points
                            prompt += f"     Sample: {sample}\n"

                        prompt += "\n"

                prompt += """
⚡ IMPORTANT - HOW TO USE THIS DATA:
The data above was already fetched in previous standard mode queries.
You can work with it directly in your calculations - NO NEED TO REFETCH!

Example usage:
```python
# Access the pre-fetched data (already available in context)
# For calculations, reference the variable names shown above
# The data is in format: [{"date": "2023-01-01", "value": 123.45}, ...]
```

✅ DO: Use this data for calculations, growth rates, comparisons
❌ DON'T: Make new API calls to fetch the same data again
"""

            # Handle entity context (what entities are being discussed)
            if "_context" in available_data:
                ctx = available_data["_context"]
                prompt += f"\n\n📍 CURRENT CONVERSATION CONTEXT:\n"
                if ctx.get("current_countries"):
                    prompt += f"  Countries in discussion: {', '.join(ctx['current_countries'])}\n"
                if ctx.get("current_indicators"):
                    prompt += f"  Indicators in discussion: {', '.join(ctx['current_indicators'])}\n"
                if ctx.get("current_provider"):
                    prompt += f"  Current provider: {ctx['current_provider']}\n"
                prompt += "\n"

            # Handle other available data (backward compatibility)
            other_data = {k: v for k, v in available_data.items()
                         if k not in ["session_data_available", "statscan_vectors", "statscan_metadata", "_context"]
                         and not isinstance(v, dict)}
            if other_data:
                prompt += f"\nOTHER AVAILABLE CONTEXT:\n{other_data}\n"

        return prompt

    def _extract_code_from_markdown(self, text: str) -> str:
        """Extract Python code from markdown code blocks"""
        # Check if the response contains markdown code blocks
        if "```python" in text:
            # Extract code between ```python and ```
            start = text.find("```python") + len("```python")
            end = text.find("```", start)
            if end != -1:
                return text[start:end].strip()
        elif "```" in text:
            # Generic code block
            start = text.find("```") + 3
            end = text.find("```", start)
            if end != -1:
                return text[start:end].strip()

        # Return as-is if no markdown blocks found
        return text.strip()


# Singleton instance
_grok_service: Optional[GrokService] = None


def get_grok_service() -> GrokService:
    """Get or create GrokService singleton"""
    global _grok_service
    if _grok_service is None:
        _grok_service = GrokService()
    return _grok_service
