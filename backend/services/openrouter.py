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


def _history_role_and_content(entry: Any, index: int) -> Tuple[str, Any]:
    """Normalize a conversation-history entry to (role, content).

    History may arrive as role-tagged dicts ({"role", "content"}) or as bare
    content strings (legacy). Using the REAL stored role — rather than deriving
    it from array position (i % 2) — prevents a desync: turns that store a user
    message with no matching assistant reply (clarification / no-data / invalid
    turns) leave two consecutive user entries, after which parity flips every
    later role and degrades follow-up detection. Falls back to parity only when
    no usable role is present.
    """
    if isinstance(entry, dict):
        role = str(entry.get("role") or "").strip().lower()
        content = entry.get("content", "")
        if role in ("user", "assistant", "system"):
            return role, content
        return ("user" if index % 2 == 0 else "assistant"), content
    return ("user" if index % 2 == 0 else "assistant"), entry


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
        # NOTE: intent caching lives in ONE place — services/query.py caches
        # the full ParseRouteResult (parse + routing) for context-free queries.
        # A second cache here would layer divergent TTL/key policies on the
        # same call chain (staleness laundering); do not re-add one.

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
        # Primary path: Instructor-based structured output
        intent: Optional[ParsedIntent] = None
        if self.instructor_client:
            try:
                intent = await self._parse_with_instructor(query, conversation_history, conversation_context)
            except Exception as e:
                # InstructorRetryException embeds every failed generation —
                # including full model reasoning — in str(e); dumping that to
                # the journal is thousands of lines per failure. Log a bounded
                # excerpt; full payloads belong in DEBUG-level tooling.
                err_text = " ".join(str(e).split())
                logger.warning(
                    "Instructor parsing failed, falling back to manual: %s",
                    err_text[:300] + ("…" if len(err_text) > 300 else ""),
                )

        # Fallback: manual JSON parsing against the configured provider
        if intent is None and self.llm_provider:
            try:
                intent = await self._parse_with_provider(query, conversation_history, conversation_context)
            except Exception as e:
                # Runtime failure of a non-OpenRouter provider (e.g. the local
                # vLLM tunnel dropped mid-flight) must degrade COST, not
                # availability: fall through to the hosted last resort. When
                # the configured provider IS OpenRouter there is nothing
                # different to try, so propagate.
                if (self.settings.llm_provider or "openrouter").lower() == "openrouter":
                    raise
                logger.error(
                    f"Configured LLM provider '{self.settings.llm_provider}' failed at "
                    f"runtime ({e}); falling back to hosted "
                    f"{self.settings.llm_fallback_model} via OpenRouter"
                )

        # Last resort: direct OpenRouter call with the hosted fallback model
        if intent is None:
            intent = await self._parse_direct(query, conversation_history, conversation_context)

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
                role, content = _history_role_and_content(msg, i)
                messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": query})

        # Reasoning models spend completion tokens on reasoning BEFORE the
        # JSON answer. Two controls keep that bounded, mirroring what
        # VLLMProvider.generate already does on the manual-fallback path:
        # reasoning_effort=low (vLLM) caps the ramble at the source, and a
        # generous max_tokens stops residual long-reasoning cases from
        # truncating mid-JSON — truncation fails Pydantic validation and
        # burns a full instructor retry (8-20s each) per attempt.
        is_vllm = self.settings.llm_provider in ("vllm",)
        max_tok = 1500 if is_vllm else 500
        extra_body = {"reasoning_effort": "low"} if is_vllm else {}

        llm_start = time.perf_counter()
        intent: ParsedIntent = await self.instructor_client.chat.completions.create(
            model=self.instructor_model,
            messages=messages,
            response_model=ParsedIntent,
            max_retries=3,
            temperature=0.0,
            max_tokens=max_tok,
            extra_body=extra_body,
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
                role, content = _history_role_and_content(msg, i)
                context_parts.append(f"{role.capitalize()}: {content}")

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
        """Last-resort parse via direct OpenRouter call with the hosted
        fallback model (settings.llm_fallback_model). Reached when the
        configured provider failed at init OR at runtime (tunnel outage)."""

        messages: List[dict[str, Any]] = [{"role": "system", "content": self._system_prompt(conversation_context=conversation_context)}]
        if conversation_history:
            for index, entry in enumerate(conversation_history):
                role, content = _history_role_and_content(entry, index)
                messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": query})

        fallback_model = self.settings.llm_fallback_model or self.MODEL
        max_retries = 2
        last_error = None
        # Reasoning models (gpt-oss-120b) spend completion tokens on reasoning
        # before emitting the JSON answer; a 300-token cap starves them.
        use_response_format = True

        for attempt in range(max_retries):
            llm_start = time.perf_counter()
            request_body: dict[str, Any] = {
                "model": fallback_model,
                "messages": messages,
                "temperature": 0,
                "max_tokens": 2000,
            }
            if use_response_format:
                request_body["response_format"] = {"type": "json_object"}
            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.post(
                    f"{self.BASE_URL}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                        "HTTP-Referer": "https://openecon.ai",
                        "X-Title": "OpenEcon Data",
                    },
                    json=request_body,
                )
            if (
                response.status_code == 400
                and use_response_format
                and "response_format" in response.text
            ):
                # Some upstream hosts for a model don't support structured
                # output; retry without it — parse_json_response handles
                # JSON embedded in prose.
                use_response_format = False
                async with httpx.AsyncClient(timeout=60.0) as client:
                    request_body.pop("response_format", None)
                    response = await client.post(
                        f"{self.BASE_URL}/chat/completions",
                        headers={
                            "Authorization": f"Bearer {self.api_key}",
                            "Content-Type": "application/json",
                            "HTTP-Referer": "https://openecon.ai",
                            "X-Title": "OpenEcon Data",
                        },
                        json=request_body,
                    )
            llm_elapsed = time.perf_counter() - llm_start
            logger.info(f"LLM parse (direct, {fallback_model}, attempt {attempt + 1}): {llm_elapsed:.2f}s")

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
