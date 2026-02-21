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
from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional

import httpx

from ..models import ParsedIntent
from ..config import Settings, get_settings
from .llm import create_llm_provider, BaseLLMProvider
from .simplified_prompt import SimplifiedPrompt
from .json_parser import parse_json_response, JSONParseError

logger = logging.getLogger(__name__)


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

    @staticmethod
    def _years_ago(years: int) -> str:
        target = datetime.now(timezone.utc) - timedelta(days=365 * years)
        return target.date().isoformat()

    def _system_prompt(self) -> str:
        """
        Generate system prompt using SimplifiedPrompt.

        This replaces the old 1,300-line prompt with a concise 200-line version.
        Provider routing is now handled by ProviderRouter (deterministic code).
        """
        return SimplifiedPrompt.generate()

    @staticmethod
    def _validate_format(parsed: dict) -> tuple[bool, Optional[str]]:
        """Validate that the parsed JSON has the required format"""
        # Check required fields
        if not parsed.get("apiProvider"):
            return False, "Missing required field: apiProvider"

        if not parsed.get("indicators"):
            return False, "Missing required field: indicators"

        if not isinstance(parsed.get("indicators"), list):
            return False, "Field 'indicators' must be an array"

        if len(parsed.get("indicators", [])) == 0:
            return False, "Field 'indicators' cannot be empty"

        # Check clarificationNeeded logic
        if "clarificationNeeded" not in parsed:
            return False, "Missing required field: clarificationNeeded"

        if parsed.get("clarificationNeeded") is True:
            if not parsed.get("clarificationQuestions"):
                return False, "If clarificationNeeded=true, must include clarificationQuestions"
            if not isinstance(parsed.get("clarificationQuestions"), list):
                return False, "Field 'clarificationQuestions' must be an array"
            if len(parsed.get("clarificationQuestions", [])) == 0:
                return False, "Field 'clarificationQuestions' cannot be empty when clarificationNeeded=true"

        # Check StatsCan-specific requirements
        if parsed.get("apiProvider", "").upper() in ("STATSCAN", "STATISTICS CANADA"):
            params = parsed.get("parameters", {})
            indicators = parsed.get("indicators", [])
            if not params.get("indicator") and not params.get("vectorId") and not indicators:
                return False, "StatsCan queries require indicator in parameters or indicators array"

        return True, None

    async def parse_query(
        self, query: str, conversation_history: Optional[List[str]] = None
    ) -> ParsedIntent:
        """
        Parse a natural language query into structured intent.

        Uses the configured LLM provider (OpenRouter, vLLM, Ollama, etc.)
        with model-specific prompt handling.

        Args:
            query: Natural language query from user
            conversation_history: Previous messages for context

        Returns:
            ParsedIntent with extracted intent structure

        Raises:
            RuntimeError: If LLM fails to return valid format after retries
        """
        # Use LLM provider abstraction if available
        if self.llm_provider:
            return await self._parse_with_provider(query, conversation_history)
        else:
            return await self._parse_direct(query, conversation_history)

    async def _parse_with_provider(
        self, query: str, conversation_history: Optional[List[str]] = None
    ) -> ParsedIntent:
        """Parse query using LLM provider abstraction"""

        system_prompt = self._system_prompt()
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
                result = await self.llm_provider.generate(
                    prompt=user_prompt,
                    system_prompt=system_prompt,
                    temperature=0.0,
                    max_tokens=500,
                    response_format={"type": "json_object"}
                )

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

        raise RuntimeError(f"LLM failed after {max_retries} attempts. Last error: {last_error}")

    async def _parse_direct(
        self, query: str, conversation_history: Optional[List[str]] = None
    ) -> ParsedIntent:
        """Fallback: Parse query using direct OpenRouter API calls"""

        messages: List[dict[str, Any]] = [{"role": "system", "content": self._system_prompt()}]
        if conversation_history:
            for index, content in enumerate(conversation_history):
                role = "user" if index % 2 == 0 else "assistant"
                messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": query})

        max_retries = 2
        last_error = None

        for attempt in range(max_retries):
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    f"{self.BASE_URL}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                        "HTTP-Referer": "https://openecon.ai",
                        "X-Title": "econ-data-mcp",
                    },
                    json={
                        "model": self.MODEL,
                        "messages": messages,
                        "response_format": {"type": "json_object"},
                        "temperature": 0,
                        "max_tokens": 300,
                    },
                )

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
