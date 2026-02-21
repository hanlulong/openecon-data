"""
Metadata Search Service

Intelligently discovers indicator codes/IDs from provider metadata catalogs
using LLM-powered selection when hardcoded mappings don't exist.

Priority hierarchy:
1. SDMX catalogs (IMF, OECD, ESTAT, BIS, ECB, ILO, UNSD, ABS, WB)
2. Provider-specific APIs (StatsCan, WorldBank REST, etc.)
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional, Any, Tuple
from pathlib import Path
import json
import httpx

from ..services.cache import cache_service
from ..services.llm import BaseLLMProvider
from ..utils.processing_steps import get_processing_tracker

logger = logging.getLogger(__name__)


class MetadataSearchService:
    """
    Service for discovering indicator codes from provider metadata catalogs.

    Uses LLM to intelligently select the best matching indicator when
    hardcoded mappings are not available.
    """

    # SDMX provider name mappings (from download script to canonical names)
    # NOTE: Some providers have multiple source IDs with different capabilities
    # See docs/sdmx-provider-ids-corrected.md for details
    SDMX_PROVIDER_MAP = {
        'IMF': 'IMF',              # Structure-only metadata (101 dataflows)
        'IMF_DATA': 'IMF',         # Data endpoint (71 dataflows) - maps to IMF for metadata
        'IMF_DATA3': 'IMF',        # SDMX 3.0 data endpoint (71 dataflows)
        'WB': 'WorldBank',         # Trade solution focus (4 dataflows)
        'OECD': 'OECD',           # 1,429 dataflows
        'ESTAT': 'Eurostat',       # 7,986 dataflows
        'BIS': 'BIS',             # 29 dataflows
        'ECB': 'ECB',             # 100 dataflows
        'UNSD': 'UN',             # UN Statistics Division
        'ILO': 'ILO',             # International Labour Organization
        'ABS': 'ABS',             # Australian Bureau of Statistics
    }

    def __init__(self, llm_provider: BaseLLMProvider):
        """
        Initialize metadata search service

        Args:
            llm_provider: LLM provider for intelligent selection
        """
        self.llm_provider = llm_provider
        self._sdmx_catalogs: Optional[Dict[str, Dict[str, Any]]] = None

    def _load_sdmx_catalogs(self) -> Dict[str, Dict[str, Any]]:
        """
        Load all SDMX dataflow catalogs from disk (lazy loading with caching)

        Returns:
            Dict mapping provider names to their dataflow catalogs
        """
        if self._sdmx_catalogs is not None:
            return self._sdmx_catalogs

        sdmx_dir = Path(__file__).parent.parent / 'data' / 'metadata' / 'sdmx'
        catalogs = {}

        if not sdmx_dir.exists():
            logger.warning(f"SDMX metadata directory not found: {sdmx_dir}")
            self._sdmx_catalogs = {}
            return self._sdmx_catalogs

        for file_path in sdmx_dir.glob('*_dataflows.json'):
            try:
                provider_key = file_path.stem.replace('_dataflows', '').upper()
                canonical_name = self.SDMX_PROVIDER_MAP.get(provider_key, provider_key)

                with open(file_path, 'r', encoding='utf-8') as f:
                    catalogs[canonical_name] = json.load(f)

                logger.info(f"Loaded {len(catalogs[canonical_name])} dataflows from {canonical_name}")
            except Exception as e:
                logger.error(f"Failed to load SDMX catalog {file_path}: {e}")

        self._sdmx_catalogs = catalogs
        logger.info(f"Successfully loaded SDMX catalogs from {len(catalogs)} providers")
        return self._sdmx_catalogs

    async def search_sdmx(self, keyword: str, provider_filter: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Search across all SDMX dataflow catalogs (PRIMARY metadata source)

        Args:
            keyword: Search term (e.g., "GDP", "inflation", "trade")
            provider_filter: Optional provider name to restrict search

        Returns:
            List of matching dataflows with code, name, description, provider, structure
        """
        # Check cache first
        cache_key = f"sdmx_search:{keyword.lower()}:{provider_filter or 'all'}"
        cached = cache_service.get(cache_key)
        if cached:
            logger.info(f"Cache hit for SDMX search: {keyword} ({len(cached)} results)")
            tracker = get_processing_tracker()
            if tracker:
                with tracker.track(
                    "searching_sdmx",
                    "🔎 Searching SDMX catalogs...",
                    {
                        "keyword": keyword,
                        "provider_filter": provider_filter,
                        "cached": True,
                    },
                ) as update_metadata:
                    update_metadata({"result_count": len(cached)})
            return cached

        try:
            tracker = get_processing_tracker()
            catalogs = self._load_sdmx_catalogs()

            if not catalogs:
                logger.warning("No SDMX catalogs loaded")
                return []

            # Split query into keywords for order-independent matching
            keywords_lower = [kw.strip().lower() for kw in keyword.split() if kw.strip()]
            results = []

            # Search across all provider catalogs
            for provider, dataflows in catalogs.items():
                # Skip if provider filter specified and doesn't match
                if provider_filter and provider.upper() != provider_filter.upper():
                    continue

                for flow_id, flow_info in dataflows.items():
                    name = flow_info.get('name', '').lower()
                    description = flow_info.get('description', '').lower()
                    combined = f"{name} {description}"

                    # Match if ALL keywords appear in name or description
                    if all(kw in combined for kw in keywords_lower):
                        # Extract structure if present (important for OECD SDMX URLs)
                        structure = flow_info.get('structure')

                        result = {
                            'provider': provider,
                            'code': flow_id,
                            'id': flow_id,
                            'name': flow_info.get('name', ''),
                            'description': flow_info.get('description', ''),
                            'source': 'SDMX',
                        }

                        # Include structure if available
                        if structure:
                            result['structure'] = structure

                        # For OECD, derive agency from structure if not present
                        if provider == 'OECD' and 'agency' not in result:
                            result['agency'] = self._derive_oecd_agency(structure, flow_id)

                        results.append(result)

            # Sort by relevance (exact matches first, then partial matches)
            def relevance_score(result):
                name_lower = result['name'].lower()
                desc_lower = result['description'].lower()
                combined = f"{name_lower} {desc_lower}"

                # Count how many keywords appear in name vs description
                name_match_count = sum(1 for kw in keywords_lower if kw in name_lower)
                desc_match_count = sum(1 for kw in keywords_lower if kw in desc_lower)

                # All keywords in name = highest priority
                if name_match_count == len(keywords_lower):
                    return 4
                # Most keywords in name
                if name_match_count > len(keywords_lower) // 2:
                    return 3
                # Some keywords in name
                if name_match_count > 0:
                    return 2
                # All keywords in description only
                return 1

            results.sort(key=relevance_score, reverse=True)

            # Cache for 24 hours
            cache_service.set(cache_key, results, ttl=86400)

            if tracker:
                with tracker.track(
                    "searching_sdmx",
                    "🔎 Searching SDMX catalogs...",
                    {
                        "keyword": keyword,
                        "provider_filter": provider_filter,
                        "cached": False,
                    },
                ) as update_metadata:
                    update_metadata({"result_count": len(results)})

            logger.info(f"Found {len(results)} SDMX dataflows matching '{keyword}'")
            return results

        except Exception as e:
            logger.exception(f"Error searching SDMX catalogs: {e}")
            return []

    async def discover_indicator(
        self,
        provider: str,
        indicator_name: str,
        search_results: List[Dict[str, Any]],
        max_results: int = 10
    ) -> Optional[Dict[str, Any]]:
        """
        Use LLM to select the best matching indicator from search results

        Args:
            provider: Provider name (StatsCan, WorldBank, etc.)
            indicator_name: User's requested indicator (e.g., "immigration")
            search_results: List of metadata search results
            max_results: Maximum results to send to LLM

        Returns:
            Dict with 'code', 'name', 'description', 'confidence'
            None if no good match found
        """
        if not search_results:
            logger.warning(f"No search results for {provider}:{indicator_name}")
            return None

        # INFRASTRUCTURE FIX: Pre-filter monetary aggregate results
        # When query specifically mentions M1/M2/M3, filter to only those indicators
        # This prevents "China money supply M2" returning 38 diverse options
        search_results = self._prefilter_monetary_results(search_results, indicator_name)

        # Limit results to avoid token overflow
        limited_results = search_results[:max_results]

        # Build prompt for LLM
        prompt = self._build_selection_prompt(
            provider, indicator_name, limited_results
        )

        tracker = get_processing_tracker()
        metadata_context = {
            "provider": provider,
            "indicator": indicator_name,
            "candidate_count": len(limited_results),
        }

        try:
            if tracker:
                with tracker.track(
                    "llm_selection",
                    "🤖 Using AI to select best match...",
                    metadata_context,
                ) as update_llm_metadata:
                    response = await self.llm_provider.generate(
                        prompt=prompt,
                        system_prompt="You are an expert at matching economic indicator requests to official statistical data codes. Return ONLY valid JSON.",
                        temperature=0.1,  # Low temperature for consistent selection
                        max_tokens=500,
                        json_mode=True,
                    )
                    import json

                    # Response is already a dict when json_mode=True
                    selection = response if isinstance(response, dict) else json.loads(response)
                    update_llm_metadata({
                        "match_found": selection.get("match_found", False),
                        "confidence": selection.get("confidence"),
                        "selected_code": selection.get("code"),
                    })
            else:
                response = await self.llm_provider.generate(
                    prompt=prompt,
                    system_prompt="You are an expert at matching economic indicator requests to official statistical data codes. Return ONLY valid JSON.",
                    temperature=0.1,  # Low temperature for consistent selection
                    max_tokens=500,
                    json_mode=True,
                )
                import json

                # LLM provider returns dict from response.json(), not raw string
                if isinstance(response, dict):
                    # Extract content from OpenRouter response format
                    if "choices" in response and response["choices"]:
                        response_text = response["choices"][0].get("message", {}).get("content", "{}")
                        selection = json.loads(response_text)
                    else:
                        # Fallback: assume response is already the selection dict
                        selection = response
                else:
                    selection = json.loads(response.content)

            # Phase 3A Fix: Lowered confidence threshold from 0.6 to 0.4
            # This addresses the critical blocker where 92% of failures were due to
            # LLM rejecting valid metadata matches with confidence scores between 0.4-0.6
            confidence_threshold = 0.4

            if selection.get("match_found") and selection.get("confidence", 0) > confidence_threshold:
                logger.info(
                    f"✅ Discovered {provider} indicator: {indicator_name} → {selection['code']} (confidence: {selection['confidence']})"
                )

                # Find the selected result in search_results to get additional metadata like agency
                selected_code = selection["code"]
                selected_result = next(
                    (r for r in search_results if r.get("code") == selected_code or r.get("id") == selected_code),
                    None
                )

                # INFRASTRUCTURE FIX: Semantic validation before accepting match
                # This prevents false positives like "repo rate" matching education indicators
                matched_indicator = {
                    "code": selection["code"],
                    "name": selection["name"],
                    "description": selection.get("description", ""),
                }
                is_valid, val_confidence, reason, suggested = await self.validate_indicator_match(
                    user_query=indicator_name,
                    user_indicator=indicator_name,
                    matched_indicator=matched_indicator
                )

                if not is_valid:
                    logger.warning(
                        f"⚠️ Semantic validation REJECTED {selection['code']} for '{indicator_name}': {reason}"
                    )
                    # Return None to trigger fallback or ask for clarification
                    return None

                # Extract agency if available (important for OECD SDMX URLs)
                agency = selected_result.get("agency") if selected_result else None

                result = {
                    "code": selection["code"],
                    "name": selection["name"],
                    "description": selection.get("description", ""),
                    "confidence": selection["confidence"]
                }

                # Include agency if found (critical for OECD)
                if agency:
                    result["agency"] = agency

                return result

            # Fallback mechanism: If LLM confidence is low but search returned results,
            # Check if results are diverse (ambiguous query) or similar (safe to pick first)
            elif search_results and len(search_results) > 0:
                # Check for ambiguity: if there are multiple diverse results, ask for clarification
                if len(search_results) >= 3 and self._are_results_diverse(search_results[:5]):
                    logger.info(
                        f"⚠️ Ambiguous query detected for {provider}:{indicator_name} - "
                        f"found {len(search_results)} diverse options, requesting clarification"
                    )
                    # Return ambiguity flag with top options for user to choose
                    options = []
                    for r in search_results[:5]:
                        options.append({
                            "code": r.get("code", r.get("id")),
                            "name": r.get("name", r.get("title", "")),
                            "description": r.get("description", r.get("desc", ""))[:100]
                        })
                    return {
                        "ambiguous": True,
                        "options": options,
                        "message": f"Multiple datasets found for '{indicator_name}'. Please specify which one you need."
                    }

                # Safe fallback: results are similar enough, pick the first one
                logger.info(
                    f"🔄 Using fallback: LLM confidence too low ({selection.get('confidence', 0)}), "
                    f"checking top vector search result for {provider}:{indicator_name}"
                )
                top_result = search_results[0]

                # INFRASTRUCTURE FIX: Semantic validation for fallback results too
                # This prevents wrong matches when the LLM has low confidence
                matched_indicator = {
                    "code": top_result.get("code", top_result.get("id")),
                    "name": top_result.get("name", top_result.get("title", indicator_name)),
                    "description": top_result.get("description", top_result.get("desc", "")),
                }
                is_valid, val_confidence, reason, suggested = await self.validate_indicator_match(
                    user_query=indicator_name,
                    user_indicator=indicator_name,
                    matched_indicator=matched_indicator
                )

                if not is_valid:
                    logger.warning(
                        f"⚠️ Fallback REJECTED - semantic validation failed for '{indicator_name}' → {matched_indicator['code']}: {reason}"
                    )
                    # Don't accept wrong matches - return None to trigger proper fallback
                    return None

                # Extract agency if available
                agency = top_result.get("agency")

                result = {
                    "code": top_result.get("code", top_result.get("id")),
                    "name": top_result.get("name", top_result.get("title", indicator_name)),
                    "description": top_result.get("description", top_result.get("desc", "")),
                    "confidence": 0.35  # Mark as fallback confidence
                }

                if agency:
                    result["agency"] = agency

                return result

            logger.warning(
                f"⚠️ Low confidence match for {provider}:{indicator_name} (confidence: {selection.get('confidence', 0)}) and no fallback available"
            )
            return None

        except Exception as e:
            logger.exception(f"Error in LLM indicator selection: {e}")
            return None

    def _prefilter_monetary_results(
        self, results: List[Dict[str, Any]], indicator_name: str
    ) -> List[Dict[str, Any]]:
        """
        Pre-filter search results for monetary aggregate queries.

        Infrastructure fix: When query specifically mentions M1/M2/M3,
        filter results to only those indicators. This prevents queries like
        "China money supply M2" from returning 38 diverse options (M1, M2, M3,
        broad money, etc.) and triggering unnecessary clarification.

        Additionally applies subject vs reference detection:
        - SUBJECT indicators: "Money and quasi money (M2) as % of GDP" - M2 is the topic
        - REFERENCE indicators: "Claims on governments (as % of M2)" - M2 is just a denominator
        Reference-only indicators are excluded as they're not what users typically want.

        Args:
            results: Search results from provider API
            indicator_name: User's requested indicator (e.g., "M2", "money supply M2")

        Returns:
            Filtered results if monetary aggregate pattern detected, otherwise original results
        """
        if not results:
            return results

        indicator_lower = indicator_name.lower()

        # Check for specific monetary aggregate mention (priority order: M3 > M2 > M1)
        # More specific aggregates take priority
        for m_type in ['m3', 'm2', 'm1']:
            # Look for M1/M2/M3 as standalone or with common suffixes
            if m_type in indicator_lower.split() or f"{m_type} " in indicator_lower or indicator_lower.endswith(m_type):
                # Filter to results containing this specific aggregate
                # Apply subject vs reference detection to exclude misleading indicators
                filtered = []
                for r in results:
                    code = (r.get('code') or r.get('id') or '').lower()
                    name = (r.get('name') or r.get('title') or '').lower()

                    # Check if result is for this specific aggregate
                    # Match patterns like "M2", "M2SL", "M2 Money Supply", etc.
                    is_match = (m_type in code.split('_') or
                        code.startswith(m_type) or
                        f" {m_type} " in f" {name} " or
                        f"({m_type})" in name or
                        name.startswith(m_type))

                    if not is_match:
                        continue

                    # INFRASTRUCTURE FIX: Subject vs Reference detection
                    # Exclude indicators where M1/M2/M3 appears only as a REFERENCE (denominator)
                    # Example: "Claims on governments (as % of M2)" - M2 is reference, not subject
                    # These are false positives that mislead indicator selection
                    is_reference_only = (
                        f"% of {m_type}" in name or
                        f"of {m_type})" in name or
                        name.endswith(f"of {m_type}")
                    )

                    # Also check if M2 is the subject (appears early or in parentheses as definition)
                    is_subject = (
                        name.startswith(m_type) or
                        name.startswith("money") or
                        f"({m_type})" in name  # e.g., "Money and quasi money (M2)"
                    )

                    # Exclude reference-only indicators
                    if is_reference_only and not is_subject:
                        logger.debug(f"📊 Excluding reference-only indicator: {code} - {name}")
                        continue

                    filtered.append(r)

                if filtered:
                    logger.info(
                        f"📊 Monetary pre-filter: '{indicator_name}' → filtered {len(results)} results to {len(filtered)} {m_type.upper()} indicators (subject-only)"
                    )
                    return filtered

        return results

    def _are_results_diverse(self, results: List[Dict[str, Any]]) -> bool:
        """
        Check if search results are diverse enough to require user clarification.

        Returns True if results represent genuinely different datasets that
        the user should choose from, False if they're similar enough that
        picking the first one is safe.

        Diversity is determined by:
        1. Counting unique "key terms" in result names
        2. If names share less than 50% of key terms, they're diverse
        """
        if len(results) < 2:
            return False

        def extract_key_terms(text: str) -> set:
            """Extract significant terms from a dataset name, ignoring common words."""
            if not text:
                return set()
            # Common words to ignore in economic data names
            stop_words = {
                'data', 'statistics', 'stat', 'total', 'annual', 'quarterly',
                'monthly', 'index', 'rate', 'by', 'and', 'the', 'of', 'for',
                'in', 'to', 'a', 'an', 'all', 'from', 'with', 'as', 'at',
                'flow', 'flows', 'stock', 'stocks', 'value', 'values',
                'consolidated', 'non', 'excluding', 'including', 'million',
                'billion', 'percent', 'percentage', 'level', 'levels'
            }
            terms = set()
            for word in text.lower().split():
                # Remove punctuation and short words
                clean = ''.join(c for c in word if c.isalnum())
                if len(clean) > 2 and clean not in stop_words:
                    terms.add(clean)
            return terms

        # Extract key terms from each result's name
        all_terms = []
        for r in results:
            name = r.get("name", r.get("title", ""))
            terms = extract_key_terms(name)
            all_terms.append(terms)

        # Check if results are diverse by comparing term overlap
        # If first result shares less than 50% of terms with others, results are diverse
        if not all_terms or not all_terms[0]:
            return True  # Can't determine, assume diverse

        first_terms = all_terms[0]
        similar_count = 0

        for terms in all_terms[1:]:
            if not terms:
                continue
            # Calculate Jaccard similarity: intersection / union
            intersection = len(first_terms & terms)
            union = len(first_terms | terms)
            if union > 0:
                similarity = intersection / union
                if similarity > 0.3:  # More than 30% overlap = similar
                    similar_count += 1

        # If less than half of results are similar to the first, results are diverse
        diversity_threshold = len(results) // 2
        return similar_count < diversity_threshold

    async def validate_indicator_match(
        self,
        user_query: str,
        user_indicator: str,
        matched_indicator: Dict[str, Any]
    ) -> Tuple[bool, float, str, Optional[str]]:
        """
        Use LLM to verify the matched indicator semantically matches user intent.

        This is a secondary validation layer to catch false positives from metadata search,
        especially for ambiguous terms like "productivity" vs "production index".

        Args:
            user_query: The full original user query
            user_indicator: The indicator term extracted from the query
            matched_indicator: The indicator that was matched (with code, name, description)

        Returns:
            Tuple of (is_valid, confidence, reason, suggested_alternative)
        """
        prompt = f"""You are validating economic indicator matches. Your job is to catch WRONG matches.

User asked for: "{user_indicator}"
Full query context: "{user_query}"

System found this indicator:
- Code: {matched_indicator.get('code', 'N/A')}
- Name: {matched_indicator.get('name', 'N/A')}
- Description: {matched_indicator.get('description', 'N/A')[:500]}

CRITICAL DISTINCTIONS (these are commonly confused):

CENTRAL BANK / POLICY RATES:
- "repo rate", "policy rate", "cash rate", "base rate", "discount rate" = CENTRAL BANK INTEREST RATES
- These are set by central banks (RBI, Fed, ECB, BOJ, etc.) for monetary policy
- They should match indicators about monetary policy, interest rates, or central bank operations
- They should NEVER match education indicators, success rates, or exam scores!
- If user asks for "Reserve Bank of India repo rate" and indicator shows "brevet success rate" → WRONG MATCH

EDUCATION:
- "enrollment rate" = PERCENTAGE of children enrolled → codes like SE.PRM.ENRR (%)
- "expected years of schooling" = YEARS, not percentage → codes like HD.HCI.EYRS
- These are COMPLETELY DIFFERENT! Enrollment rate is % enrolled, expected years is duration.
- If user asks for "enrollment rate" and indicator shows "years of schooling" → WRONG MATCH
- Education indicators should NEVER match monetary policy queries!

INFLATION:
- "headline CPI" or "CPI" = ALL items including food and energy → full index
- "core inflation" = EXCLUDING food and energy → look for "ex-food", "core", "CP00-FOOD-NRG"
- If user asks for "core inflation" and indicator is headline CPI → WRONG MATCH

LABOR:
- "unemployment rate" = % unemployed out of labor force
- "employment rate" = % employed out of working-age population
- "labor participation" = % of population in labor force
- These are different metrics!

OTHER:
- "productivity" = output per worker (GDP/employment) - NOT production volume or production index
- "production index" = volume of output - NOT productivity
- "growth" can mean GDP growth, population growth, employment growth - context matters
- "output" can mean GDP, industrial output, agricultural output - context matters
- "income" can mean household income, national income (GNI), GDP per capita - context matters
- "investment" can mean FDI, gross capital formation, portfolio investment - context matters
- "debt" can mean government debt, household debt, external debt, corporate debt - context matters
- "rate" in banking context = interest rate; "rate" in education = success/pass rate - context matters!

Does this indicator ACTUALLY measure what the user is asking for?

Return JSON:
{{
    "is_match": true/false,
    "confidence": 0.0-1.0,
    "reason": "brief explanation of why this is or isn't a match",
    "suggested_alternative": "if not a match, suggest what indicator name to search for, otherwise null"
}}

Be STRICT - if there's any doubt about whether the indicator measures what the user wants, return is_match=false."""

        try:
            response = await self.llm_provider.generate(
                prompt=prompt,
                system_prompt="You are an expert economist validating economic data queries. Return ONLY valid JSON.",
                temperature=0.1,
                max_tokens=300,
                json_mode=True,
            )

            import json
            if isinstance(response, dict):
                if "choices" in response and response["choices"]:
                    response_text = response["choices"][0].get("message", {}).get("content", "{}")
                    result = json.loads(response_text)
                else:
                    result = response
            else:
                result = json.loads(response)

            is_match = result.get("is_match", False)
            confidence = result.get("confidence", 0.0)
            reason = result.get("reason", "No reason provided")
            suggested = result.get("suggested_alternative")

            if not is_match:
                logger.warning(
                    f"⚠️ Semantic validation FAILED for '{user_indicator}' → {matched_indicator.get('code')}: {reason}"
                )
            else:
                logger.info(
                    f"✅ Semantic validation PASSED for '{user_indicator}' → {matched_indicator.get('code')} (confidence: {confidence})"
                )

            return is_match, confidence, reason, suggested

        except Exception as e:
            logger.exception(f"Error in semantic validation: {e}")
            # On error, assume match is valid to avoid blocking queries
            return True, 0.5, f"Validation error: {e}", None

    def _build_selection_prompt(
        self,
        provider: str,
        indicator_name: str,
        results: List[Dict[str, Any]]
    ) -> str:
        """Build prompt for LLM to select best matching indicator"""

        # Build results text with optional date information
        results_text_lines = []
        for r in results:
            code = r.get('code', r.get('id', '?'))
            name = r.get('name', r.get('title', '?'))
            desc = r.get('description', r.get('desc', 'N/A'))

            # Include date range if available (important for StatsCan)
            start_date = r.get('startDate', '')
            end_date = r.get('endDate', '')

            if start_date and end_date:
                # Extract year from ISO date (e.g., "2009-12-01" -> "2009")
                start_year = start_date[:4] if len(start_date) >= 4 else start_date
                end_year = end_date[:4] if len(end_date) >= 4 else end_date
                date_info = f", Data Range: {start_year}-{end_year}"
            else:
                date_info = ""

            results_text_lines.append(
                f"- Code: {code}, Name: {name}, Description: {desc}{date_info}"
            )

        results_text = "\n".join(results_text_lines)

        return f"""The user wants {provider} data for: "{indicator_name}"

Available indicators from {provider} metadata catalog:
{results_text}

Select the BEST matching indicator. Consider:
1. **SEMANTIC MATCH IS CRITICAL** - The indicator must measure the SAME THING the user asked for
2. Exact name matches (highest priority)
3. Description relevance - core concepts must match, not just incidental words
4. Data currency - prefer datasets with end dates in 2020 or later

**CRITICAL SEMANTIC DISTINCTIONS** (these are commonly confused - PAY CLOSE ATTENTION):

EDUCATION INDICATORS:
- "enrollment rate" or "enrolment rate" = PERCENTAGE of children enrolled in school → look for "enrollment" or "enrolment" (NOT "years of schooling")
- "primary school enrollment" = % of children in primary education → codes like SE.PRM.ENRR, SE.PRM.NENR
- "secondary school enrollment" = % of children in secondary education → codes like SE.SEC.ENRR
- "expected years of schooling" = AVERAGE YEARS expected in school → codes like HD.HCI.EYRS (this is NOT an enrollment rate!)
- "literacy rate" = % of people who can read/write → codes like SE.ADT.LITR
- "school life expectancy" = years of schooling expected → NOT the same as enrollment rate!

INFLATION INDICATORS:
- "headline inflation" or "CPI" = ALL items Consumer Price Index → includes food and energy
- "core inflation" = CPI EXCLUDING food and energy → look for "excluding food", "ex-food", "CP00-FOOD-NRG", "core CPI"
- "HICP" = Harmonized Index of Consumer Prices (EU standard) → headline unless specified as "core"
- "food inflation" = price changes for food only
- Do NOT confuse "CPI" with "core CPI" - they are different measures!

LABOR INDICATORS:
- "unemployment rate" = % of labor force without jobs → NOT total unemployed persons
- "employment rate" = % of working-age population employed → NOT unemployment rate
- "labor force participation" = % of working-age population in labor force

OTHER DISTINCTIONS:
- "non-financial corporations" means corporations that are NOT banks/financial institutions
- "total assets" is a specific balance sheet item, NOT "fixed assets" or "expenditure"
- "government expenditure" is NOT the same as "corporation assets"
- "productivity" = output per worker (GDP/employment) → NOT production volume or production index
- "production index" = volume of output → NOT productivity
- Do NOT match based on single shared words (e.g., don't match "assets" to any dataset containing "assets")
- The CORE SUBJECT must match: corporations→corporations, government→government, household→household
- The CORE METRIC must match: assets→assets, debt→debt, income→income, expenditure→expenditure

Return JSON in this format:
{{
  "match_found": true/false,
  "code": "selected_code_or_id",
  "name": "official_indicator_name",
  "description": "why_this_was_selected",
  "confidence": 0.0-1.0
}}

If no GOOD SEMANTIC match exists, set match_found=false with confidence 0.
Don't force a match - it's better to return no match than a wrong match.
Return ONLY the JSON object, no other text."""

    async def search_statscan(self, keyword: str) -> List[Dict[str, Any]]:
        """
        Search Statistics Canada metadata catalog

        Args:
            keyword: Search term (e.g., "immigration", "GDP")

        Returns:
            List of matching data cubes with productId, title, description
        """
        # Check cache first
        cache_key = f"statscan_search:{keyword.lower()}"
        cached = cache_service.get(cache_key)
        if cached:
            logger.info(f"Cache hit for StatsCan search: {keyword}")
            tracker = get_processing_tracker()
            if tracker:
                with tracker.track(
                    "searching_metadata",
                    "🔎 Searching provider catalog...",
                    {
                        "provider": "StatsCan",
                        "keyword": keyword,
                        "cached": True,
                    },
                ) as update_metadata:
                    update_metadata({"result_count": len(cached)})
            return cached

        try:
            tracker = get_processing_tracker()
            if tracker:
                with tracker.track(
                    "searching_metadata",
                    "🔎 Searching provider catalog...",
                    {
                        "provider": "StatsCan",
                        "keyword": keyword,
                        "cached": False,
                    },
                ) as update_metadata:
                    matching = await self._fetch_statscan_matches(keyword)
                    update_metadata({"result_count": len(matching)})
            else:
                matching = await self._fetch_statscan_matches(keyword)

            # Cache for 24 hours
            cache_service.set(cache_key, matching, ttl=86400)

            logger.info(f"Found {len(matching)} StatsCan cubes matching '{keyword}'")
            return matching

        except Exception as e:
            logger.exception(f"Error searching StatsCan metadata: {e}")
            return []

    async def _fetch_statscan_matches(self, keyword: str) -> List[Dict[str, Any]]:
        """Fetch matching StatsCan metadata entries from remote WDS API.

        Uses the getAllCubesListLite endpoint to dynamically discover available
        data products. Prioritizes active (non-archived) products with recent data.

        KEYWORD MATCHING: Split query into keywords and match ALL keywords (order-independent).
        Example: "GDP per capita PPP" matches products containing "GDP" AND "capita" AND "PPP"
        """
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(
                    "https://www150.statcan.gc.ca/t1/wds/rest/getAllCubesListLite",
                    timeout=30.0
                )
                response.raise_for_status()
                cubes = response.json()

            # Split query into keywords for order-independent matching
            keywords_lower = [kw.strip().lower() for kw in keyword.split() if kw.strip()]

            matching = []
            for cube in cubes:
                title_lower = cube.get("cubeTitleEn", "").lower()

                # Check if ALL keywords are present
                if all(kw in title_lower for kw in keywords_lower):
                    matching.append({
                        "code": str(cube["productId"]),
                        "id": str(cube["productId"]),
                        "name": cube.get("cubeTitleEn", ""),
                        "title": cube.get("cubeTitleEn", ""),
                        "description": cube.get("cubeTitleEn", ""),
                        "startDate": cube.get("cubeStartDate"),
                        "endDate": cube.get("cubeEndDate"),
                        "archived": cube.get("archived", "1"),
                    })

            # Prioritize active (non-archived) products with recent data
            def sort_key(cube):
                is_active = 1 if cube.get("archived") == "2" else 0
                end_year = int(cube.get("endDate", "2000-01-01")[:4])
                is_recent = 1 if end_year >= 2024 else (0.5 if end_year >= 2020 else 0)
                return (is_active, is_recent)

            matching.sort(key=sort_key, reverse=True)
            logger.info(f"Found {len(matching)} StatsCan products matching '{keyword}'")
            return matching

        except Exception as e:
            logger.error(f"Error fetching StatsCan matches: {e}")
            raise

    async def search_worldbank(self, keyword: str) -> List[Dict[str, Any]]:
        """
        Search World Bank indicators catalog

        Args:
            keyword: Search term (e.g., "GDP", "inflation")

        Returns:
            List of matching indicators with ID, name, description
        """
        # Check cache first
        cache_key = f"worldbank_search:{keyword.lower()}"
        cached = cache_service.get(cache_key)
        if cached:
            logger.info(f"Cache hit for WorldBank search: {keyword}")
            tracker = get_processing_tracker()
            if tracker:
                with tracker.track(
                    "searching_metadata",
                    "🔎 Searching provider catalog...",
                    {
                        "provider": "WorldBank",
                        "keyword": keyword,
                        "cached": True,
                    },
                ) as update_metadata:
                    update_metadata({"result_count": len(cached)})
            return cached

        try:
            tracker = get_processing_tracker()
            if tracker:
                with tracker.track(
                    "searching_metadata",
                    "🔎 Searching provider catalog...",
                    {
                        "provider": "WorldBank",
                        "keyword": keyword,
                        "cached": False,
                    },
                ) as update_metadata:
                    matching = await self._fetch_worldbank_matches(keyword)
                    update_metadata({"result_count": len(matching)})
            else:
                matching = await self._fetch_worldbank_matches(keyword)

            # Cache for 24 hours
            cache_service.set(cache_key, matching, ttl=86400)

            logger.info(f"Found {len(matching)} WorldBank indicators matching '{keyword}'")
            return matching

        except Exception as e:
            logger.exception(f"Error searching WorldBank metadata: {e}")
            return []

    async def _fetch_worldbank_matches(self, keyword: str) -> List[Dict[str, Any]]:
        """Fetch World Bank indicator metadata from API and filter by keyword.

        WorldBank has 16,000+ indicators. We fetch multiple pages to ensure comprehensive coverage.
        The API supports pagination with per_page and page parameters.

        IMPORTANT: Previous implementation only fetched 100 indicators (first page), which caused
        vector search failures for common indicators like "female_labor_force_participation_rate"
        and "urban_population_percentage". This fix fetches up to 2,500 indicators across 5 pages,
        with early exit if we find enough matches. This ensures comprehensive coverage while
        maintaining reasonable performance.

        KEYWORD MATCHING: Split query into keywords and match ALL keywords (order-independent).
        Example: "GDP per capita PPP" matches indicators containing "GDP" AND "capita" AND "PPP"
        """
        # Split query into keywords for order-independent matching
        keywords_lower = [kw.strip().lower() for kw in keyword.split() if kw.strip()]
        all_matching = []

        # Fetch up to 15,000 indicators across multiple pages (balanced coverage)
        # WorldBank has 29,310 total indicators (as of Nov 2025)
        # Common indicators (GDP, CO2, poverty, etc.) are in first 20 pages
        # Life expectancy indicators are on pages 50-58 (25,000-29,000)
        # This balances coverage with performance - most queries satisfied by first 30 pages
        async with httpx.AsyncClient(timeout=90.0) as client:
            for page in range(1, 31):  # 30 pages * 500 per_page = 15000 indicators
                try:
                    response = await client.get(
                        "https://api.worldbank.org/v2/indicator",
                        params={
                            "format": "json",
                            "per_page": 500,
                            "page": page,
                        },
                        timeout=10.0,  # 10s timeout per page
                    )
                    response.raise_for_status()
                    data = response.json()

                    if len(data) < 2 or not data[1]:
                        # No more pages
                        break

                    indicators = data[1]

                    # Filter by keywords - ALL keywords must match (order-independent)
                    for ind in indicators:
                        name_lower = ind.get("name", "").lower()
                        desc_lower = ind.get("sourceNote", "").lower()
                        combined = f"{name_lower} {desc_lower}"

                        # Check if ALL keywords are present
                        if all(kw in combined for kw in keywords_lower):
                            all_matching.append({
                                "code": ind["id"],
                                "id": ind["id"],
                                "name": ind.get("name", ""),
                                "description": ind.get("sourceNote", ""),
                            })

                    # Early exit if we found enough matches (avoid unnecessary API calls)
                    if len(all_matching) >= 50:
                        logger.info(f"Found {len(all_matching)} WorldBank matches, stopping early")
                        break

                except Exception as e:
                    logger.warning(f"Error fetching WorldBank page {page}: {e}")
                    # Continue with what we have
                    break

        logger.info(f"Fetched {len(all_matching)} WorldBank indicators matching '{keyword}'")
        return all_matching

    async def search_imf(self, keyword: str) -> List[Dict[str, Any]]:
        """Search IMF DataMapper metadata for indicators.

        KEYWORD MATCHING: Split query into keywords and match ALL keywords (order-independent).
        Example: "GDP per capita PPP" matches indicators containing "GDP" AND "capita" AND "PPP"
        """
        cache_key = f"imf_search:{keyword.lower()}"
        cached = cache_service.get(cache_key)
        if cached:
            logger.info(f"Cache hit for IMF search: {keyword}")
            return cached

        try:
            metadata_entries = await self._fetch_imf_metadata()

            # Split query into keywords for order-independent matching
            keywords_lower = [kw.strip().lower() for kw in keyword.split() if kw.strip()]

            matching = []
            for entry in metadata_entries:
                # Null-safe string handling - handles None values
                name = entry.get("name") or ""
                name_lower = name.lower() if name else ""
                desc = entry.get("description") or ""
                desc_lower = desc.lower() if desc else ""
                combined = f"{name_lower} {desc_lower}"

                # Check if ALL keywords are present
                if all(kw in combined for kw in keywords_lower):
                    matching.append(entry)

            cache_service.set(cache_key, matching, ttl=86400)
            logger.info(f"Found {len(matching)} IMF indicators matching '{keyword}'")
            return matching
        except Exception as exc:
            logger.exception(f"Error searching IMF metadata: {exc}")
            return []

    async def _fetch_imf_metadata(self) -> List[Dict[str, Any]]:
        """Fetch IMF DataMapper metadata and normalize structure.

        KEYWORD MATCHING: Caller (search_imf) filters results using keyword matching.
        This method just fetches and normalizes all indicators from the API.
        """
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.get(
                "https://www.imf.org/external/datamapper/api/v1/indicators"
            )
            response.raise_for_status()
            payload = response.json()

        # IMF DataMapper API returns format: {"indicators": {"CODE": {"label": "...", "unit": "..."}, ...}}
        normalized: Dict[str, Dict[str, str]] = {}

        if isinstance(payload, dict) and "indicators" in payload:
            indicators = payload["indicators"]
            for code, info in indicators.items():
                if not isinstance(info, dict):
                    continue

                name = info.get("label", info.get("name", str(code)))
                unit = info.get("unit", "")
                description = f"{name}. Unit: {unit}" if unit else name

                normalized[str(code)] = {
                    "code": str(code),
                    "id": str(code),
                    "name": str(name),
                    "description": description,
                }

        return list(normalized.values())

    async def search_bis(self, keyword: str) -> List[Dict[str, Any]]:
        """Search BIS dataflow catalog.

        KEYWORD MATCHING: Split query into keywords and match ALL keywords (order-independent).
        Example: "GDP per capita PPP" matches dataflows containing "GDP" AND "capita" AND "PPP"
        """
        cache_key = f"bis_search:{keyword.lower()}"
        cached = cache_service.get(cache_key)
        if cached:
            logger.info(f"Cache hit for BIS search: {keyword}")
            return cached

        try:
            dataflows = await self._fetch_bis_dataflows()

            # Split query into keywords for order-independent matching
            keywords_lower = [kw.strip().lower() for kw in keyword.split() if kw.strip()]

            matching = []
            for flow in dataflows:
                # Null-safe string handling - handles None values
                name = flow.get("name") or ""
                name_lower = name.lower() if name else ""
                desc = flow.get("description") or ""
                desc_lower = desc.lower() if desc else ""
                combined = f"{name_lower} {desc_lower}"

                # Check if ALL keywords are present
                if all(kw in combined for kw in keywords_lower):
                    matching.append(flow)

            cache_service.set(cache_key, matching, ttl=86400)
            logger.info(f"Found {len(matching)} BIS dataflows matching '{keyword}'")
            return matching
        except Exception as exc:
            logger.exception(f"Error searching BIS metadata: {exc}")
            return []

    async def _fetch_bis_dataflows(self) -> List[Dict[str, Any]]:
        """Fetch BIS dataflow definitions in SDMX-JSON format."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(
                "https://stats.bis.org/api/v1/bis/dataflow",
                headers={"Accept": "application/vnd.sdmx.structure+json;version=1.0.0"},
            )
            response.raise_for_status()
            payload = response.json()

        flows = (
            payload.get("structure", {})
            .get("dataflows", {})
            .get("dataflow", [])
        )
        results: Dict[str, Dict[str, str]] = {}
        for flow in flows:
            if not isinstance(flow, dict):
                continue
            code = flow.get("id")
            if not code:
                continue
            name = self._extract_text(flow.get("name")) or self._extract_text(flow.get("names")) or str(code)
            description = self._extract_text(flow.get("description")) or self._extract_text(flow.get("descriptions")) or ""
            results[str(code)] = {
                "code": str(code),
                "id": str(code),
                "name": name,
                "description": description,
            }
        return list(results.values())

    async def search_eurostat(self, keyword: str) -> List[Dict[str, Any]]:
        """Search Eurostat dataset catalog.

        KEYWORD MATCHING: Split query into keywords and match ALL keywords (order-independent).
        Example: "GDP per capita PPP" matches datasets containing "GDP" AND "capita" AND "PPP"
        """
        cache_key = f"eurostat_search:{keyword.lower()}"
        cached = cache_service.get(cache_key)
        if cached:
            logger.info(f"Cache hit for Eurostat search: {keyword}")
            return cached

        try:
            datasets = await self._fetch_eurostat_datasets()

            # Split query into keywords for order-independent matching
            keywords_lower = [kw.strip().lower() for kw in keyword.split() if kw.strip()]

            matching = []
            for dataset in datasets:
                # Null-safe string handling - handles None values
                name = dataset.get("name") or ""
                name_lower = name.lower() if name else ""
                desc = dataset.get("description") or ""
                desc_lower = desc.lower() if desc else ""
                combined = f"{name_lower} {desc_lower}"

                # Check if ALL keywords are present
                if all(kw in combined for kw in keywords_lower):
                    matching.append(dataset)

            cache_service.set(cache_key, matching, ttl=86400)
            logger.info(f"Found {len(matching)} Eurostat datasets matching '{keyword}'")
            return matching
        except Exception as exc:
            logger.exception(f"Error searching Eurostat metadata: {exc}")
            return []

    async def _fetch_eurostat_datasets(self) -> List[Dict[str, Any]]:
        """Fetch Eurostat dataset catalog and normalize entries."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(
                "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/datasets",
                params={"lang": "EN"},
            )
            response.raise_for_status()
            payload = response.json()

        datasets = payload.get("datasets") if isinstance(payload, dict) else []
        results: Dict[str, Dict[str, str]] = {}
        for dataset in datasets or []:
            if not isinstance(dataset, dict):
                continue
            code = dataset.get("code") or dataset.get("id")
            if not code:
                continue
            name = dataset.get("title") or dataset.get("label") or dataset.get("name") or str(code)
            description = dataset.get("description") or dataset.get("notes") or ""
            results[str(code)] = {
                "code": str(code),
                "id": str(code),
                "name": str(name),
                "description": str(description),
            }
        return list(results.values())

    async def search_with_sdmx_fallback(
        self,
        provider: str,
        indicator: str,
        max_results: int = 10
    ) -> List[Dict[str, Any]]:
        """
        Hierarchical metadata search with multiple fallback layers.

        Search order:
        1. SDMX catalogs (cross-provider standard) - PRIMARY SOURCE
        2. Provider-specific APIs (direct search) - SECONDARY SOURCE
        3. Vector search (semantic similarity) - TERTIARY SOURCE

        Args:
            provider: Provider name (e.g., "WorldBank", "StatsCan", "IMF")
            indicator: Search keyword (e.g., "GDP", "inflation")
            max_results: Maximum number of results to return

        Returns:
            List of matching metadata entries with code, name, description
        """
        logger.info(f"🔍 Searching metadata for {provider}:{indicator} (multi-layer fallback)")

        # Step 1: Try SDMX first (PRIMARY SOURCE)
        try:
            # Map provider name to SDMX provider key
            provider_upper = provider.upper().replace(" ", "")

            # Try exact match first
            sdmx_provider_filter = None
            for sdmx_key, canonical_name in self.SDMX_PROVIDER_MAP.items():
                canonical_upper = canonical_name.upper().replace(" ", "")
                if provider_upper in canonical_upper or canonical_upper in provider_upper:
                    sdmx_provider_filter = canonical_name
                    break

            # Search SDMX catalogs
            sdmx_results = await self.search_sdmx(indicator, provider_filter=sdmx_provider_filter)

            if sdmx_results:
                logger.info(
                    f"✅ Found {len(sdmx_results)} results from SDMX for {provider}:{indicator} "
                    f"(filtered by: {sdmx_provider_filter or 'all providers'})"
                )
                return sdmx_results[:max_results]
            else:
                logger.info(f"⚠️ No SDMX results for {provider}:{indicator}, falling back to provider API...")

        except Exception as e:
            logger.warning(f"SDMX search failed for {provider}:{indicator}: {e}, falling back to provider API...")

        # Step 2: Fall back to provider-specific search (SECONDARY SOURCE)
        provider_normalized = provider.upper().replace(" ", "")
        provider_results = []

        try:
            if provider_normalized in {"STATSCAN", "STATISTICSCANADA"}:
                logger.info(f"📍 Using StatsCan API search for: {indicator}")
                provider_results = await self.search_statscan(indicator)

            elif provider_normalized in {"WORLDBANK", "WB"}:
                logger.info(f"📍 Using WorldBank REST API search for: {indicator}")
                provider_results = await self.search_worldbank(indicator)

            elif provider_normalized == "IMF":
                logger.info(f"📍 Using IMF DataMapper API search for: {indicator}")
                provider_results = await self.search_imf(indicator)

            elif provider_normalized == "BIS":
                logger.info(f"📍 Using BIS API search for: {indicator}")
                provider_results = await self.search_bis(indicator)

            elif provider_normalized in {"EUROSTAT", "ESTAT"}:
                logger.info(f"📍 Using Eurostat API search for: {indicator}")
                provider_results = await self.search_eurostat(indicator)

            if provider_results:
                logger.info(f"✅ Found {len(provider_results)} results from provider API")
                return provider_results[:max_results]
            else:
                logger.info(f"⚠️ No results from provider API, trying vector search...")

        except Exception as e:
            logger.warning(f"Provider API search failed: {e}, trying vector search...")

        # Step 3: Fall back to vector search (TERTIARY SOURCE)
        try:
            from .vector_search import get_vector_search_service, VECTOR_SEARCH_AVAILABLE

            if VECTOR_SEARCH_AVAILABLE:
                logger.info(f"📍 Using vector search for: {indicator}")
                vector_search = get_vector_search_service()

                if vector_search.is_indexed():
                    # Search with provider filter
                    vector_results = vector_search.search(
                        query=indicator,
                        limit=max_results,
                        where={"provider": provider_normalized} if provider_normalized else None
                    )

                    if vector_results:
                        # Convert VectorSearchResult to metadata dict format
                        metadata_results = [
                            {
                                "code": result.code,
                                "id": result.code,
                                "name": result.name,
                                "provider": result.provider,
                                "description": result.name,  # Vector search doesn't store full description
                                "source": "vector_search",
                                "similarity": result.similarity
                            }
                            for result in vector_results
                        ]

                        logger.info(f"✅ Found {len(metadata_results)} results from vector search")
                        return metadata_results
                    else:
                        logger.warning(f"⚠️ Vector search returned no results")
                else:
                    logger.warning(f"⚠️ Vector search index not built")
            else:
                logger.warning(f"⚠️ Vector search not available")

        except Exception as e:
            logger.warning(f"Vector search failed: {e}")

        # If all methods failed
        logger.warning(
            f"❌ All search methods failed for {provider}:{indicator}. "
            f"Try building the vector index or updating metadata catalogs."
        )
        return []

    @staticmethod
    def _derive_oecd_agency(structure: Optional[str], dataflow_code: str) -> str:
        """Derive OECD agency code from dataflow structure.

        OECD uses several agencies:
        - OECD.SDD.NAD - National Accounts Division (GDP, QNA, NAMAIN, etc.)
        - OECD.SDD.TPS - Labour and Social Statistics (Employment, Unemployment, LFS, etc.)
        - OECD.ECO.MAD - Economic Outlook (Inflation, Prices, CPI, etc.)
        - OECD.CFE.EDS - Centre for Entrepreneurship, SMEs and Regions (Regional stats)
        - OECD.STI.PIE - Science, Technology and Industry (Patents, Innovation)
        - Others...

        Args:
            structure: DSD structure ID (e.g., "SEEAAIR", "DSD_NAMAIN1", "DSD_LFS")
            dataflow_code: Full dataflow code (e.g., "DSD_NAMAIN1@DF_QNA")

        Returns:
            Agency code for SDMX URL
        """
        # Map common structure prefixes to agencies
        structure_upper = (structure or "").upper()
        dataflow_upper = dataflow_code.upper()

        # National accounts (GDP, QNA, National Accounts)
        if any(x in structure_upper for x in ["NAMAIN", "TABLE1", "ANA_MAIN", "NPS"]):
            return "OECD.SDD.NAD"
        if "QNA" in dataflow_upper:
            return "OECD.SDD.NAD"

        # Education Statistics (EAG = Education at a Glance) - check BEFORE labor market
        if "EAG" in structure_upper:
            return "OECD.SDD.EDSTAT"

        # Labor force statistics (Unemployment, Employment, LSO)
        # Note: LSO = Labour Force Survey, IALFS = International Active Labour Force Statistics
        if any(x in structure_upper for x in ["LFS", "LABOUR", "LAB", "LSO"]):
            return "OECD.SDD.TPS"
        if any(x in dataflow_upper for x in ["IALFS", "UNEMP"]):
            return "OECD.SDD.TPS"

        # Economic outlook (Inflation, CPI, Prices)
        if "PRICES" in structure_upper or "PRICES_ALL" in dataflow_upper:
            return "OECD.ECO.MAD"
        if "EO" in dataflow_upper:
            return "OECD.ECO.MAD"

        # Regional statistics (TL2, TL3, FUA, Metro, Regional)
        if any(x in structure_upper for x in ["REG_", "FUA", "METRO", "TL2", "TL3"]):
            return "OECD.CFE.EDS"

        # Patents and Innovation
        if "PATENT" in structure_upper:
            return "OECD.STI.PIE"

        # Environment and Sustainable Development
        if any(x in structure_upper for x in ["SEEA", "ENVIR", "ENV"]):
            return "OECD.ENV"

        # Trade and Competitiveness
        if any(x in structure_upper for x in ["TRADE", "EXPORT", "IMPORT", "TRAD"]):
            return "OECD.TAD"

        # Default fallback (most common is SDD.NAD)
        return "OECD.SDD.NAD"

    @staticmethod
    def _extract_text(value: Any) -> str:
        """Extract English text from a SDMX-style multilingual structure."""
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            for key in ("en", "EN", "value", "text"):
                text = value.get(key)
                if isinstance(text, str):
                    return text
                if isinstance(text, list) and text:
                    return MetadataSearchService._extract_text(text[0])
            return ""
        if isinstance(value, list):
            # Prefer entries with language set to English
            for item in value:
                if isinstance(item, dict):
                    lang = item.get("lang") or item.get("locale")
                    if isinstance(lang, str) and lang.lower() == "en":
                        text = item.get("value") or item.get("text")
                        if isinstance(text, str):
                            return text
            # Fallback to first element
            return MetadataSearchService._extract_text(value[0]) if value else ""
        return ""


def get_metadata_search_service(llm_provider: BaseLLMProvider) -> MetadataSearchService:
    """Factory function to get metadata search service instance"""
    return MetadataSearchService(llm_provider)
