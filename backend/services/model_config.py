"""
Model Configuration System for econ-data-mcp LLM Integration

Supports model-specific prompts and configurations for different LLM types:
- OpenAI/OpenRouter models (GPT-4o, etc.)
- vLLM-served models (Qwen3, DeepSeek, etc.)
- Ollama local models
- Reasoning models with thinking tags

Created: 2025-01-20
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Dict, Any, List
import logging
import re

logger = logging.getLogger(__name__)


class ModelFamily(str, Enum):
    """Model family types with different prompt requirements"""
    OPENAI = "openai"           # GPT-4, GPT-4o, etc.
    QWEN3_THINKING = "qwen3_thinking"  # Qwen3 with reasoning/thinking mode
    QWEN3_INSTRUCT = "qwen3_instruct"  # Qwen3 instruct models
    DEEPSEEK_R1 = "deepseek_r1"  # DeepSeek R1 reasoning model
    LLAMA = "llama"             # Meta LLaMA models
    MISTRAL = "mistral"         # Mistral models
    CLAUDE = "claude"           # Anthropic Claude
    GENERIC = "generic"         # Generic OpenAI-compatible


@dataclass
class ModelConfig:
    """Configuration for a specific model family"""

    # Model identification
    family: ModelFamily

    # Prompt formatting
    system_prefix: str = ""           # Prefix before system prompt
    system_suffix: str = ""           # Suffix after system prompt
    user_prefix: str = ""             # Prefix before user message
    user_suffix: str = ""             # Suffix after user message
    assistant_prefix: str = ""        # Prefix for assistant response

    # Reasoning model support
    supports_thinking: bool = False   # Whether model outputs thinking tags
    thinking_start_tag: str = "<think>"
    thinking_end_tag: str = "</think>"
    strip_thinking: bool = True       # Remove thinking from final output

    # JSON handling
    supports_json_mode: bool = True   # Whether model supports response_format: json
    json_instruction: str = ""        # Extra instruction to ensure JSON output

    # Model-specific parameters
    default_temperature: float = 0.1
    default_max_tokens: int = 4096
    context_window: int = 128000      # Default context window

    # Additional options
    extra_params: Dict[str, Any] = field(default_factory=dict)

    def format_system_prompt(self, prompt: str) -> str:
        """Format system prompt with model-specific prefixes/suffixes"""
        formatted = f"{self.system_prefix}{prompt}{self.system_suffix}"
        if self.json_instruction and not self.supports_json_mode:
            formatted += f"\n\n{self.json_instruction}"
        return formatted

    def format_user_prompt(self, prompt: str) -> str:
        """Format user prompt with model-specific prefixes/suffixes"""
        return f"{self.user_prefix}{prompt}{self.user_suffix}"

    def extract_response(self, raw_response: str) -> str:
        """Extract final response, optionally stripping thinking tags"""
        if not self.supports_thinking or not self.strip_thinking:
            return raw_response

        # Remove thinking tags and content
        pattern = re.escape(self.thinking_start_tag) + r".*?" + re.escape(self.thinking_end_tag)
        cleaned = re.sub(pattern, "", raw_response, flags=re.DOTALL)
        return cleaned.strip()

    def extract_thinking(self, raw_response: str) -> Optional[str]:
        """Extract thinking/reasoning content from response"""
        if not self.supports_thinking:
            return None

        pattern = re.escape(self.thinking_start_tag) + r"(.*?)" + re.escape(self.thinking_end_tag)
        match = re.search(pattern, raw_response, flags=re.DOTALL)
        return match.group(1).strip() if match else None


# Pre-configured model configurations
MODEL_CONFIGS: Dict[ModelFamily, ModelConfig] = {
    ModelFamily.OPENAI: ModelConfig(
        family=ModelFamily.OPENAI,
        supports_json_mode=True,
        default_temperature=0.0,
        context_window=128000,
    ),

    ModelFamily.QWEN3_THINKING: ModelConfig(
        family=ModelFamily.QWEN3_THINKING,
        supports_thinking=True,
        thinking_start_tag="<think>",
        thinking_end_tag="</think>",
        strip_thinking=True,
        supports_json_mode=True,  # vLLM supports JSON mode
        default_temperature=0.6,  # Reasoning models work better with some temperature
        context_window=128000,
        json_instruction="IMPORTANT: After your thinking, output ONLY valid JSON with no additional text.",
    ),

    ModelFamily.QWEN3_INSTRUCT: ModelConfig(
        family=ModelFamily.QWEN3_INSTRUCT,
        supports_thinking=False,
        supports_json_mode=True,
        default_temperature=0.1,
        context_window=128000,
    ),

    ModelFamily.DEEPSEEK_R1: ModelConfig(
        family=ModelFamily.DEEPSEEK_R1,
        supports_thinking=True,
        thinking_start_tag="<think>",
        thinking_end_tag="</think>",
        strip_thinking=True,
        supports_json_mode=True,
        default_temperature=0.6,
        context_window=64000,
        json_instruction="After reasoning, output ONLY the final JSON object.",
    ),

    ModelFamily.LLAMA: ModelConfig(
        family=ModelFamily.LLAMA,
        supports_json_mode=False,  # Some Llama versions don't support it well
        json_instruction="You MUST respond with ONLY valid JSON. No explanations, no markdown code blocks, just the raw JSON object.",
        default_temperature=0.1,
        context_window=8192,
    ),

    ModelFamily.MISTRAL: ModelConfig(
        family=ModelFamily.MISTRAL,
        supports_json_mode=True,
        default_temperature=0.1,
        context_window=32000,
    ),

    ModelFamily.CLAUDE: ModelConfig(
        family=ModelFamily.CLAUDE,
        supports_json_mode=False,  # Claude uses different format
        json_instruction="Respond with ONLY a valid JSON object. No preamble, no explanation, just the JSON.",
        default_temperature=0.0,
        context_window=200000,
    ),

    ModelFamily.GENERIC: ModelConfig(
        family=ModelFamily.GENERIC,
        supports_json_mode=True,
        json_instruction="Respond with ONLY valid JSON.",
        default_temperature=0.1,
        context_window=8192,
    ),
}


def detect_model_family(model_name: str) -> ModelFamily:
    """
    Detect model family from model name.

    Args:
        model_name: Model identifier (e.g., "Qwen3-Next-80B-A3B-Thinking-FP8")

    Returns:
        Detected ModelFamily enum
    """
    model_lower = model_name.lower()

    # OpenAI models
    if any(x in model_lower for x in ["gpt-4", "gpt-3.5", "openai/", "o1-", "o3-"]):
        return ModelFamily.OPENAI

    # Qwen models
    if "qwen" in model_lower:
        if "thinking" in model_lower or "r1" in model_lower:
            return ModelFamily.QWEN3_THINKING
        return ModelFamily.QWEN3_INSTRUCT

    # DeepSeek models
    if "deepseek" in model_lower:
        if "r1" in model_lower or "reasoner" in model_lower:
            return ModelFamily.DEEPSEEK_R1
        return ModelFamily.GENERIC

    # LLaMA models
    if any(x in model_lower for x in ["llama", "meta-llama"]):
        return ModelFamily.LLAMA

    # Mistral models
    if any(x in model_lower for x in ["mistral", "mixtral"]):
        return ModelFamily.MISTRAL

    # Claude models
    if "claude" in model_lower or "anthropic" in model_lower:
        return ModelFamily.CLAUDE

    # GPT-OSS and similar
    if "gpt-oss" in model_lower:
        return ModelFamily.GENERIC

    # Default to generic
    logger.info(f"Unknown model family for '{model_name}', using GENERIC config")
    return ModelFamily.GENERIC


def get_model_config(model_name: str) -> ModelConfig:
    """
    Get configuration for a model based on its name.

    Args:
        model_name: Model identifier

    Returns:
        ModelConfig for the detected model family
    """
    family = detect_model_family(model_name)
    config = MODEL_CONFIGS[family]
    logger.debug(f"Model '{model_name}' detected as family '{family.value}'")
    return config


# Common model name mappings for convenience
MODEL_ALIASES: Dict[str, str] = {
    # Short names to full model IDs
    "gpt4o": "openai/gpt-4o",
    "gpt4o-mini": "openai/gpt-4o-mini",
    "gpt4": "openai/gpt-4-turbo",
    "claude3": "anthropic/claude-3-opus",
    "claude3-sonnet": "anthropic/claude-3-sonnet",
    "qwen-thinking": "Qwen3-Next-80B-A3B-Thinking-FP8",
    "qwen-instruct": "Qwen3-Next-80B-A3B-Instruct-FP8",
    "deepseek-r1": "deepseek-ai/deepseek-r1",
}


def resolve_model_alias(model_name: str) -> str:
    """Resolve model alias to full model ID"""
    return MODEL_ALIASES.get(model_name.lower(), model_name)
