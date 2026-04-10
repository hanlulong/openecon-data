"""
Query Parsing Service using Flexible LLM Backends

Supports multiple LLM providers:
- OpenRouter (cloud API, default)
- vLLM (local OpenAI-compatible server)
- Ollama (local models)
- LM-Studio (local models)

Configuration via environment variables:
- LLM_PROVIDER: openrouter, vllm, ollama, lm-studio
- LLM_MODEL: Model identifier
- LLM_BASE_URL: Base URL for local providers
- LLM_TIMEOUT: Request timeout in seconds
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx
import instructor
from openai import AsyncOpenAI

from ..models import ParsedIntent
from ..config import Settings, get_settings
from .llm import create_llm_provider, BaseLLMProvider
from .simplified_prompt import SimplifiedPrompt
from .json_parser import parse_json_response, JSONParseError

logger = logging.getLogger(__name__)


class _IntentCache:
    """LRU cache for parsed intents to skip redundant LLM calls.

    Caching the LLM parse result for identical queries saves 4-6 seconds
    per repeat query (the entire LLM round-trip). TTL prevents stale results
    when upstream prompts or models change.

    Only caches queries WITHOUT conversation context (follow-ups need fresh parsing).
    """

    def __init__(self, max_size: int = 256, ttl_seconds: float = 600):
        self._cache: OrderedDict[str, Tuple[float, ParsedIntent]] = OrderedDict()
        self._max_size = max_size
        self._ttl = ttl_seconds
        self._hits = 0
        self._misses = 0

    @staticmethod
    def _key(query: str) -> str:
        normalized = query.strip().lower()
        return hashlib.md5(normalized.encode()).hexdigest()

    def get(self, query: str) -> Optional[ParsedIntent]:
        key = self._key(query)
        entry = self._cache.get(key)
        if entry is None:
            self._misses += 1
            return None
        ts, intent = entry
        if time.time() - ts > self._ttl:
            del self._cache[key]
            self._misses += 1
            return None
        # Move to end (most recently used)
        self._cache.move_to_end(key)
        self._hits += 1
        # Return a deep copy so downstream mutations don't corrupt the cache
        return intent.model_copy(deep=True)

    def put(self, query: str, intent: ParsedIntent) -> None:
        key = self._key(query)
        self._cache[key] = (time.time(), intent.model_copy(deep=True))
        self._cache.move_to_end(key)
        while len(self._cache) > self._max_size:
            self._cache.popitem(last=False)

    @property
    def stats(self) -> Dict[str, int]:
        return {"hits": self._hits, "misses": self._misses, "size": len(self._cache)}


class OpenRouterService:
    """
    Query parsing service using flexible LLM backends.

    Despite the name (kept for backward compatibility), this service
    now supports multiple LLM providers through the LLM abstraction layer.
    """
    BASE_URL = "https://openrouter.ai/api/v1"
    MODEL = "openai/gpt-4o-mini"  # Default model

    def __init__(self, api_key: str, settings: Optional[Settings] = None) -> None:
        """
        Initialize query parsing service.

        Args:
            api_key: OpenRouter API key (for backward compatibility, also used as fallback)
            settings: Optional settings object for advanced LLM configuration
        """
        if not api_key:
            raise ValueError("OpenRouter API key is required")

        self.api_key = api_key
        self.settings = settings or get_settings()
        self._intent_cache = _IntentCache(max_size=256, ttl_seconds=600)

        # Initialize LLM provider based on configuration
        try:
            provider_config = {
                "api_key": api_key,
                "model": self.settings.llm_model or self.MODEL,
                "base_url": self.settings.llm_base_url,
                "timeout": self.settings.llm_timeout,
            }
            self.llm_provider: BaseLLMProvider = create_llm_provider(
                self.settings.llm_provider, provider_config
            )
            logger.info(f"Initialized LLM provider: {self.settings.llm_provider}")
            logger.info(f"  Model: {self.llm_provider.model}")
        except Exception as e:
            logger.warning(f"Failed to initialize LLM provider: {e}")
            logger.warning("Falling back to direct OpenRouter API calls")
            self.llm_provider = None

        # Initialize Instructor client for structured output parsing.
        # Works with any OpenAI-compatible API (OpenRouter, vLLM, LM-Studio).
        self.instructor_client = None
        try:
            llm_provider_name = (self.settings.llm_provider or "openrouter").lower()
            if llm_provider_name in ("openrouter", "vllm", "lm-studio"):
                if llm_provider_name == "openrouter":
                    base_url = "https://openrouter.ai/api/v1"
                    client_api_key = api_key
                else:
                    base_url = (self.settings.llm_base_url or "http://localhost:8000").rstrip("/") + "/v1"
                    client_api_key = self.settings.vllm_api_key or "EMPTY"

                raw_client = AsyncOpenAI(
                    api_key=client_api_key,
                    base_url=base_url,
                    timeout=float(self.settings.llm_timeout or 120),
                    default_headers={
                        "HTTP-Referer": "https://openecon.ai",
                        "X-Title": "OpenEcon Data",
                    },
                )
                self.instructor_client = instructor.from_openai(
                    raw_client, mode=instructor.Mode.JSON
                )
                self.instructor_model = self.settings.llm_model or self.MODEL
                logger.info(f"Instructor client initialized (mode=JSON, provider={llm_provider_name})")
        except Exception as e:
            logger.warning(f"Failed to initialize Instructor client: {e}")
            self.instructor_client = None

    @staticmethod
    def _years_ago(years: int) -> str:
        target = datetime.now(timezone.utc) - timedelta(days=365 * years)
        return target.date().isoformat()

    def _system_prompt(self, conversation_context: Optional[dict] = None) -> str:
        """
        Generate system prompt using SimplifiedPrompt.

        This replaces the old 1,300-line prompt with a concise 200-line version.
        Provider routing is now handled by ProviderRouter (deterministic code).

        Args:
            conversation_context: Optional dict with previous turn info for follow-up detection.
        """
        return SimplifiedPrompt.generate(conversation_context=conversation_context)

    @staticmethod
    def _validate_format(parsed: dict) -> tuple[bool, Optional[str]]:
        """Validate parsed JSON before constructing ParsedIntent.

        ParsedIntent's Pydantic validators enforce the same core rules
        (non-empty apiProvider/indicators, clarification consistency).
        This pre-check gives clearer error messages for the LLM retry loop.
        """
        from pydantic import ValidationError
        try:
            # Pydantic handles: apiProvider non-empty, indicators non-empty,
            # clarificationQuestions required when clarificationNeeded=true
            ParsedIntent.model_validate(parsed)
        except ValidationError as e:
            first_err = e.errors()[0]
            return False, f"{first_err.get('loc', ['?'])}: {first_err['msg']}"

        # StatsCan-specific requirement
        if parsed.get("apiProvider", "").upper() in ("STATSCAN", "STATISTICS CANADA"):
            params = parsed.get("parameters", {})
            indicators = parsed.get("indicators", [])
            if not params.get("indicator") and not params.get("vectorId") and not indicators:
                return False, "StatsCan queries require indicator in parameters or indicators array"

        return True, None

    async def parse_query(
        self,
        query: str,
        conversation_history: Optional[List[str]] = None,
        conversation_context: Optional[dict] = None,
    ) -> ParsedIntent:
        """
        Parse a natural language query into structured intent.

        Uses Instructor for Pydantic-validated structured output when available,
        falling back to manual JSON parsing.

        Args:
            query: Natural language query from user
            conversation_history: Previous messages for context
            conversation_context: Optional dict with previous turn info for follow-up detection.
                Keys: indicator, country, provider, startDate, endDate, originalQuery

        Returns:
            ParsedIntent with extracted intent structure

        Raises:
            RuntimeError: If LLM fails to return valid format after retries
        """
        # Intent-level cache: skip LLM call for identical queries without
        # conversation context. Saves 4-6s per repeat query.
        # Only cache standalone queries — follow-ups need fresh parsing.
        _cacheable = not conversation_context and not conversation_history
        if _cacheable:
            cached_intent = self._intent_cache.get(query)
            if cached_intent is not None:
                logger.info(
                    "Intent cache hit: '%s' → provider=%s, indicators=%s (cache %s)",
                    query[:50], cached_intent.apiProvider, cached_intent.indicators,
                    self._intent_cache.stats,
                )
                cached_intent.originalQuery = query
                return cached_intent

        # Primary path: Instructor-based structured output
        intent: Optional[ParsedIntent] = None
        if self.instructor_client:
            try:
                intent = await self._parse_with_instructor(query, conversation_history, conversation_context)
            except Exception as e:
                logger.warning(f"Instructor parsing failed, falling back to manual: {e}")

        # Fallback: manual JSON parsing
        if intent is None:
            if self.llm_provider:
                intent = await self._parse_with_provider(query, conversation_history, conversation_context)
            else:
                intent = await self._parse_direct(query, conversation_history, conversation_context)

        # Cache the result for future identical queries
        if _cacheable and intent is not None:
            self._intent_cache.put(query, intent)

        return intent

    async def _parse_with_instructor(
        self,
        query: str,
        conversation_history: Optional[List[str]] = None,
        conversation_context: Optional[dict] = None,
    ) -> ParsedIntent:
        """Parse query using Instructor for Pydantic-validated structured output.

        Instructor automatically:
        - Validates LLM output against ParsedIntent schema
        - Retries with corrective prompts on validation failure
        - Handles JSON extraction from raw text
        """
        system_prompt = self._system_prompt(conversation_context=conversation_context)

        # Build messages
        messages = [{"role": "system", "content": system_prompt}]
        if conversation_history:
            for i, msg in enumerate(conversation_history):
                role = "user" if i % 2 == 0 else "assistant"
                messages.append({"role": role, "content": msg})
        messages.append({"role": "user", "content": query})

        # Reasoning models spend tokens on internal reasoning before producing
        # JSON output. 600 is enough for actual output of 500-650 tokens.
        # Previous 1000 was wasteful — saves ~0.5s per query.
        max_tok = 600 if self.settings.llm_provider in ("vllm",) else 500

        llm_start = time.perf_counter()
        intent: ParsedIntent = await self.instructor_client.chat.completions.create(
            model=self.instructor_model,
            messages=messages,
            response_model=ParsedIntent,
            max_retries=3,
            temperature=0.0,
            max_tokens=max_tok,
        )
        llm_elapsed = time.perf_counter() - llm_start
        intent.originalQuery = query
        logger.info(f"LLM parse: {llm_elapsed:.2f}s | provider={intent.apiProvider}, "
                     f"indicators={intent.indicators}, type={intent.queryType}")
        if conversation_context:
            logger.info(f"Follow-up fields: isFollowUp={intent.isFollowUp}, "
                         f"followUpType={intent.followUpType}, resolvedQuery={intent.resolvedQuery}")
        return intent

    async def _parse_with_provider(
        self,
        query: str,
        conversation_history: Optional[List[str]] = None,
        conversation_context: Optional[dict] = None,
    ) -> ParsedIntent:
        """Parse query using LLM provider abstraction (manual JSON fallback)"""

        system_prompt = self._system_prompt(conversation_context=conversation_context)
        max_retries = 3
        last_error = None

        # Build conversation context
        context_parts = []
        if conversation_history:
            for i, msg in enumerate(conversation_history):
                role = "User" if i % 2 == 0 else "Assistant"
                context_parts.append(f"{role}: {msg}")

        for attempt in range(max_retries):
            # Build the user prompt with context
            user_prompt = query
            if context_parts:
                context_str = "\n".join(context_parts)
                user_prompt = f"Previous conversation:\n{context_str}\n\nCurrent query: {query}"

            if attempt > 0 and last_error:
                user_prompt += f"\n\n🚨 PREVIOUS ERROR: {last_error}\nPlease fix and return valid JSON."

            try:
                llm_start = time.perf_counter()
                result = await self.llm_provider.generate(
                    prompt=user_prompt,
                    system_prompt=system_prompt,
                    temperature=0.0,
                    max_tokens=500,
                    response_format={"type": "json_object"}
                )
                llm_elapsed = time.perf_counter() - llm_start
                logger.info(f"LLM parse (provider fallback, attempt {attempt + 1}): {llm_elapsed:.2f}s")

                content = result["choices"][0]["message"]["content"]

                # Log thinking if present (for reasoning models)
                if "_thinking" in result["choices"][0]["message"]:
                    thinking = result["choices"][0]["message"]["_thinking"]
                    logger.debug(f"Model reasoning ({len(thinking)} chars)")

                # Parse JSON with automatic fixing for truncation/malformed output
                try:
                    parsed = parse_json_response(content, fix_truncated=True)
                except JSONParseError as exc:
                    last_error = f"Invalid JSON: {str(exc)}"
                    logger.warning(f"Attempt {attempt + 1}: {last_error}")
                    logger.debug(f"Raw content: {content[:500]}...")
                    continue

                # Validate format
                is_valid, error_msg = self._validate_format(parsed)
                if not is_valid:
                    last_error = error_msg
                    logger.warning(f"Attempt {attempt + 1}: Format error - {error_msg}")
                    continue

                # Success! Return parsed intent
                parsed["originalQuery"] = query
                return ParsedIntent(**parsed)

            except Exception as e:
                last_error = str(e)
                logger.error(f"Attempt {attempt + 1} failed: {e}")
                if attempt == max_retries - 1:
                    raise

        # Translate technical errors to user-friendly messages (cycle 31 fix).
        # The raw error often contains pydantic validation details like
        # "('indicators',): Value error, indicators must contain at least one item"
        # which is meaningless to users.
        user_msg = "I couldn't understand that query. "
        if "indicators" in str(last_error).lower() and "empty" in str(last_error).lower():
            user_msg += 'Try being more specific, like "US GDP" or "inflation in Germany".'
        elif "indicators" in str(last_error).lower():
            user_msg += 'Please specify an economic indicator — for example "GDP", "unemployment", or "inflation".'
        else:
            user_msg += 'Try rephrasing, e.g. "US unemployment rate" or "China GDP growth".'
        raise RuntimeError(user_msg)

    async def _parse_direct(
        self,
        query: str,
        conversation_history: Optional[List[str]] = None,
        conversation_context: Optional[dict] = None,
    ) -> ParsedIntent:
        """Fallback: Parse query using direct OpenRouter API calls"""

        messages: List[dict[str, Any]] = [{"role": "system", "content": self._system_prompt(conversation_context=conversation_context)}]
        if conversation_history:
            for index, content in enumerate(conversation_history):
                role = "user" if index % 2 == 0 else "assistant"
                messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": query})

        max_retries = 2
        last_error = None

        for attempt in range(max_retries):
            llm_start = time.perf_counter()
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    f"{self.BASE_URL}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                        "HTTP-Referer": "https://openecon.ai",
                        "X-Title": "OpenEcon Data",
                    },
                    json={
                        "model": self.MODEL,
                        "messages": messages,
                        "response_format": {"type": "json_object"},
                        "temperature": 0,
                        "max_tokens": 300,
                    },
                )
            llm_elapsed = time.perf_counter() - llm_start
            logger.info(f"LLM parse (direct, attempt {attempt + 1}): {llm_elapsed:.2f}s")

            if response.status_code >= 400:
                content_type = response.headers.get("content-type", "")
                if "application/json" in content_type.lower():
                    detail = response.json().get("error", {}).get("message")
                else:
                    detail = response.text
                raise RuntimeError(f"OpenRouter API error: {detail}")

            payload = response.json()
            content = payload["choices"][0]["message"]["content"]

            try:
                parsed = parse_json_response(content, fix_truncated=True)
            except JSONParseError as exc:
                last_error = f"Invalid JSON response: {str(exc)}"
                messages.append({"role": "assistant", "content": content})
                messages.append({
                    "role": "user",
                    "content": f"🚨 ERROR: Your response was not valid JSON. Error: {str(exc)}\n\nYou MUST return ONLY a valid JSON object with no text before or after. Try again."
                })
                continue

            # Validate format
            is_valid, error_msg = self._validate_format(parsed)
            if not is_valid:
                last_error = error_msg
                messages.append({"role": "assistant", "content": content})
                messages.append({
                    "role": "user",
                    "content": f"🚨 FORMAT ERROR: {error_msg}\n\nReview the required JSON format and provide a corrected response following ALL mandatory requirements."
                })
                continue

            # Format is valid, return the parsed intent with original query attached
            parsed["originalQuery"] = query
            return ParsedIntent(**parsed)

        raise RuntimeError(f"LLM failed to return valid format after {max_retries} attempts. Last error: {last_error}")


# Convenience function for quick LLM provider testing
async def test_llm_connection(provider: str = None, model: str = None) -> dict:
    """
    Test LLM connection with current configuration.

    Args:
        provider: Optional provider override (openrouter, vllm, ollama)
        model: Optional model override

    Returns:
        Dict with test results
    """
    from .llm import test_provider

    settings = get_settings()
    config = {
        "api_key": settings.openrouter_api_key,
        "model": model or settings.llm_model,
        "base_url": settings.llm_base_url,
        "timeout": settings.llm_timeout,
    }

    llm = create_llm_provider(provider or settings.llm_provider, config)
    return await test_provider(llm)
