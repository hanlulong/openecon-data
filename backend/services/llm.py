"""
LLM Provider Abstraction Layer

Supports multiple LLM backends:
- OpenRouter (cloud API)
- vLLM (local OpenAI-compatible server)
- Ollama (local models)
- LM-Studio (local models)

Created: 2025-11-20
Updated: 2025-01-20 - Added vLLM support with model-specific prompts
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Dict, Any, Optional, List
from enum import Enum
import httpx
import logging
import json
import asyncio

from ..config import get_settings
from .model_config import (
    ModelConfig, ModelFamily, get_model_config, resolve_model_alias,
    detect_model_family
)

logger = logging.getLogger(__name__)


class LLMProvider(str, Enum):
    """Supported LLM providers"""
    OPENROUTER = "openrouter"
    VLLM = "vllm"
    OLLAMA = "ollama"
    LM_STUDIO = "lm-studio"
    ANTHROPIC = "anthropic"


class BaseLLMProvider(ABC):
    """Base class for all LLM providers"""

    def __init__(self, model: str = ""):
        self.model = model
        self.model_config = get_model_config(model) if model else None

    @abstractmethod
    async def generate(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        response_format: Optional[Dict] = None,
        **kwargs
    ) -> Dict[str, Any]:
        """
        Generate completion from LLM

        Args:
            prompt: User prompt
            system_prompt: System instructions
            temperature: Randomness (0.0-1.0)
            max_tokens: Maximum response length
            response_format: JSON schema for response
            **kwargs: Provider-specific options

        Returns:
            Dict with 'choices' containing completions
        """
        pass

    @abstractmethod
    async def health_check(self) -> bool:
        """Check if provider is available"""
        pass

    def _get_temperature(self, temperature: Optional[float]) -> float:
        """Get temperature, using model default if not specified"""
        if temperature is not None:
            return temperature
        if self.model_config:
            return self.model_config.default_temperature
        return 0.1

    def _get_max_tokens(self, max_tokens: Optional[int]) -> int:
        """Get max tokens, using model default if not specified"""
        if max_tokens is not None:
            return max_tokens
        if self.model_config:
            return self.model_config.default_max_tokens
        return 4096

    def _format_system_prompt(self, prompt: str) -> str:
        """Format system prompt for this model"""
        if self.model_config:
            return self.model_config.format_system_prompt(prompt)
        return prompt

    def _format_user_prompt(self, prompt: str) -> str:
        """Format user prompt for this model"""
        if self.model_config:
            return self.model_config.format_user_prompt(prompt)
        return prompt

    def _extract_response(self, raw_response: str) -> str:
        """Extract final response, handling thinking tags if needed"""
        if self.model_config:
            return self.model_config.extract_response(raw_response)
        return raw_response

    def _should_use_json_mode(self, response_format: Optional[Dict]) -> bool:
        """Check if JSON mode should be used"""
        if not response_format:
            return False
        if self.model_config and not self.model_config.supports_json_mode:
            return False
        return True


class OpenRouterProvider(BaseLLMProvider):
    """OpenRouter cloud API provider"""

    def __init__(
        self,
        api_key: str,
        model: str = "openai/gpt-4o-mini",
        timeout: float = 30.0
    ):
        super().__init__(model)
        if not api_key:
            raise ValueError("OpenRouter API key is required")

        self.api_key = api_key
        self.base_url = "https://openrouter.ai/api/v1"
        self.timeout = timeout

    async def generate(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        response_format: Optional[Dict] = None,
        **kwargs
    ) -> Dict[str, Any]:
        """Generate completion using OpenRouter API"""

        # Build messages array
        messages = []
        if system_prompt:
            formatted_system = self._format_system_prompt(system_prompt)
            messages.append({"role": "system", "content": formatted_system})

        formatted_prompt = self._format_user_prompt(prompt)
        messages.append({"role": "user", "content": formatted_prompt})

        # Build request payload
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": self._get_temperature(temperature),
            "max_tokens": self._get_max_tokens(max_tokens),
            **kwargs
        }

        if self._should_use_json_mode(response_format):
            payload["response_format"] = response_format

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    f"{self.base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "HTTP-Referer": "https://openecon.ai",
                        "X-Title": "econ-data-mcp"
                    },
                    json=payload
                )
                response.raise_for_status()

                result = response.json()

                # Process response to handle thinking tags
                if result.get("choices"):
                    content = result["choices"][0]["message"]["content"]
                    processed = self._extract_response(content)
                    result["choices"][0]["message"]["content"] = processed
                    # Store original for debugging
                    result["choices"][0]["message"]["_original_content"] = content

                return result

        except httpx.HTTPError as e:
            logger.error(f"OpenRouter API error: {e}")
            raise

    async def health_check(self) -> bool:
        """Check OpenRouter API availability"""
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(
                    f"{self.base_url}/models",
                    headers={"Authorization": f"Bearer {self.api_key}"}
                )
                return response.status_code == 200
        except Exception as e:
            logger.warning(f"OpenRouter health check failed: {e}")
            return False


class VLLMProvider(BaseLLMProvider):
    """
    vLLM OpenAI-compatible API provider

    Supports local vLLM servers with OpenAI-compatible endpoints.
    Handles model-specific prompts for reasoning models (Qwen3, DeepSeek, etc.)
    """

    def __init__(
        self,
        base_url: str = "http://localhost:8000",
        model: str = "default",
        api_key: Optional[str] = None,  # vLLM usually doesn't require API key
        timeout: float = 120.0,  # Longer timeout for local models
        reasoning_effort: Optional[str] = "low"  # "low" reduces token usage for reasoning models
    ):
        super().__init__(model)
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key or "EMPTY"  # vLLM accepts any key or "EMPTY"
        self.timeout = timeout
        self.reasoning_effort = reasoning_effort

        logger.info(f"VLLMProvider initialized: {base_url}, model: {model}")
        if self.model_config:
            logger.info(f"  Model family: {self.model_config.family.value}")
            logger.info(f"  Supports thinking: {self.model_config.supports_thinking}")
        if reasoning_effort:
            logger.info(f"  Reasoning effort: {reasoning_effort}")

    async def generate(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        response_format: Optional[Dict] = None,
        **kwargs
    ) -> Dict[str, Any]:
        """Generate completion using vLLM OpenAI-compatible API"""

        # Build messages array
        messages = []
        if system_prompt:
            formatted_system = self._format_system_prompt(system_prompt)
            messages.append({"role": "system", "content": formatted_system})

        formatted_prompt = self._format_user_prompt(prompt)
        messages.append({"role": "user", "content": formatted_prompt})

        # Build request payload
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": self._get_temperature(temperature),
            "max_tokens": self._get_max_tokens(max_tokens),
            **kwargs
        }

        # Add JSON mode if supported
        if self._should_use_json_mode(response_format):
            payload["response_format"] = response_format

        # Add reasoning effort for reasoning models (reduces token usage)
        if self.reasoning_effort:
            payload["reasoning_effort"] = self.reasoning_effort

        logger.debug(f"vLLM request to {self.base_url}/v1/chat/completions")
        logger.debug(f"  Model: {self.model}")
        logger.debug(f"  Temperature: {payload['temperature']}")

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    f"{self.base_url}/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json"
                    },
                    json=payload
                )
                response.raise_for_status()

                result = response.json()

                # Process response to handle thinking tags
                if result.get("choices"):
                    message = result["choices"][0]["message"]
                    content = message.get("content") or ""

                    # Handle thinking tags in content (e.g., <think>...</think>)
                    processed = self._extract_response(content)
                    result["choices"][0]["message"]["content"] = processed
                    # Store original for debugging (useful for reasoning models)
                    result["choices"][0]["message"]["_original_content"] = content

                    # Extract thinking from content tags if present
                    if self.model_config and self.model_config.supports_thinking:
                        thinking = self.model_config.extract_thinking(content)
                        if thinking:
                            result["choices"][0]["message"]["_thinking"] = thinking
                            logger.debug(f"Extracted thinking from tags ({len(thinking)} chars)")

                    # Also handle reasoning_content field (returned by some models like gpt-oss-120b)
                    reasoning_content = message.get("reasoning_content")
                    if reasoning_content:
                        result["choices"][0]["message"]["_thinking"] = reasoning_content
                        logger.debug(f"Found reasoning_content ({len(reasoning_content)} chars)")

                return result

        except httpx.TimeoutException:
            logger.error(f"vLLM request timed out after {self.timeout}s")
            raise
        except httpx.HTTPError as e:
            logger.error(f"vLLM API error: {e}")
            raise

    async def health_check(self) -> bool:
        """Check vLLM server availability"""
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(f"{self.base_url}/v1/models")
                if response.status_code == 200:
                    models = response.json()
                    logger.info(f"vLLM models available: {models}")
                    return True
                return False
        except Exception as e:
            logger.warning(f"vLLM health check failed: {e}")
            return False

    async def list_models(self) -> List[str]:
        """List available models on the vLLM server"""
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(f"{self.base_url}/v1/models")
                if response.status_code == 200:
                    data = response.json()
                    return [m["id"] for m in data.get("data", [])]
        except Exception as e:
            logger.warning(f"Failed to list vLLM models: {e}")
        return []


class OllamaProvider(BaseLLMProvider):
    """Ollama local model provider"""

    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        model: str = "llama2",
        timeout: float = 120.0
    ):
        super().__init__(model)
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    async def generate(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        response_format: Optional[Dict] = None,
        **kwargs
    ) -> Dict[str, Any]:
        """Generate completion using Ollama API"""

        # Try chat endpoint first (newer Ollama versions)
        messages = []
        if system_prompt:
            formatted_system = self._format_system_prompt(system_prompt)
            messages.append({"role": "system", "content": formatted_system})

        formatted_prompt = self._format_user_prompt(prompt)
        messages.append({"role": "user", "content": formatted_prompt})

        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": self._get_temperature(temperature),
            }
        }

        if max_tokens:
            payload["options"]["num_predict"] = max_tokens

        # Ollama supports JSON mode via format parameter
        if response_format and response_format.get("type") == "json_object":
            payload["format"] = "json"

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    f"{self.base_url}/api/chat",
                    json=payload
                )
                response.raise_for_status()

                ollama_response = response.json()

                # Convert Ollama format to OpenAI format
                content = ollama_response.get("message", {}).get("content", "")
                processed = self._extract_response(content)

                return {
                    "choices": [{
                        "message": {
                            "content": processed,
                            "_original_content": content
                        }
                    }],
                    "model": self.model,
                    "usage": {
                        "prompt_tokens": ollama_response.get("prompt_eval_count", 0),
                        "completion_tokens": ollama_response.get("eval_count", 0),
                    }
                }

        except httpx.HTTPError as e:
            logger.error(f"Ollama API error: {e}")
            raise

    async def health_check(self) -> bool:
        """Check Ollama availability"""
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(f"{self.base_url}/api/tags")
                return response.status_code == 200
        except Exception:
            return False


def create_llm_provider(
    provider: Optional[str] = None,
    config: Optional[Dict[str, Any]] = None,
    **kwargs
) -> BaseLLMProvider:
    """
    Factory to create LLM provider instances

    Args:
        provider: Provider name (openrouter, vllm, ollama, lm-studio)
        config: Provider configuration dict with api_key, model, base_url, timeout
        **kwargs: Alternative provider-specific arguments (for backward compatibility)

    Returns:
        LLM provider instance

    Environment Variables:
        LLM_PROVIDER: Default provider (openrouter, vllm, ollama, lm-studio)
        LLM_MODEL: Model to use
        LLM_BASE_URL: Base URL for local providers
        LLM_TIMEOUT: Request timeout in seconds
        OPENROUTER_API_KEY: OpenRouter API key
        VLLM_API_KEY: Optional vLLM API key

    Examples:
        # Use OpenRouter (default)
        provider = create_llm_provider()

        # Use local vLLM server
        provider = create_llm_provider("vllm", {
            "base_url": "http://localhost:8000",
            "model": "Qwen3-Next-80B-A3B-Thinking-FP8"
        })

        # Use SSH-tunneled vLLM server
        provider = create_llm_provider("vllm", {
            "base_url": "http://hansearch.com:8000",
            "model": "qwen-thinking"
        })

    Raises:
        ValueError: If provider is unknown or credentials missing
    """
    settings = get_settings()
    config = config or {}

    # Determine provider
    provider = provider or settings.llm_provider or "openrouter"
    provider = provider.lower()

    logger.info(f"Creating LLM provider: {provider}")

    if provider == "openrouter":
        api_key = config.get("api_key") or kwargs.get("api_key") or settings.openrouter_api_key
        if not api_key:
            raise ValueError("OpenRouter API key required. Set OPENROUTER_API_KEY env var.")

        model = config.get("model") or kwargs.get("model") or settings.llm_model or "openai/gpt-4o-mini"
        model = resolve_model_alias(model)
        timeout = config.get("timeout") or kwargs.get("timeout") or settings.llm_timeout or 30

        return OpenRouterProvider(api_key=api_key, model=model, timeout=timeout)

    elif provider == "vllm":
        base_url = (
            config.get("base_url") or
            kwargs.get("base_url") or
            settings.llm_base_url or
            "http://localhost:8000"
        )
        model = config.get("model") or kwargs.get("model") or settings.llm_model or "default"
        model = resolve_model_alias(model)
        api_key = config.get("api_key") or kwargs.get("api_key") or None
        timeout = config.get("timeout") or kwargs.get("timeout") or settings.llm_timeout or 120

        return VLLMProvider(base_url=base_url, model=model, api_key=api_key, timeout=timeout)

    elif provider == "ollama":
        base_url = (
            config.get("base_url") or
            kwargs.get("base_url") or
            settings.llm_base_url or
            "http://localhost:11434"
        )
        model = config.get("model") or kwargs.get("model") or settings.llm_model or "llama2"
        timeout = config.get("timeout") or kwargs.get("timeout") or settings.llm_timeout or 120

        return OllamaProvider(base_url=base_url, model=model, timeout=timeout)

    elif provider == "lm-studio":
        # LM Studio uses OpenAI-compatible API, similar to vLLM
        base_url = (
            config.get("base_url") or
            kwargs.get("base_url") or
            settings.llm_base_url or
            "http://localhost:1234"
        )
        model = config.get("model") or kwargs.get("model") or settings.llm_model or "local-model"
        timeout = config.get("timeout") or kwargs.get("timeout") or settings.llm_timeout or 120

        return VLLMProvider(base_url=base_url, model=model, timeout=timeout)

    else:
        raise ValueError(f"Unknown LLM provider: {provider}. Supported: openrouter, vllm, ollama, lm-studio")


async def test_provider(provider: BaseLLMProvider) -> Dict[str, Any]:
    """
    Test an LLM provider with a simple query

    Args:
        provider: LLM provider instance

    Returns:
        Dict with test results
    """
    logger.info(f"Testing provider: {type(provider).__name__}")

    # Health check
    is_healthy = await provider.health_check()
    if not is_healthy:
        return {"success": False, "error": "Health check failed"}

    # Simple test query
    try:
        result = await provider.generate(
            prompt="What is 2 + 2? Answer with just the number.",
            system_prompt="You are a helpful assistant. Answer concisely.",
            max_tokens=10,
            temperature=0.0
        )

        content = result["choices"][0]["message"]["content"]
        return {
            "success": True,
            "model": provider.model,
            "response": content,
            "raw_result": result
        }

    except Exception as e:
        return {"success": False, "error": str(e)}
