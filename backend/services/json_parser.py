"""
LangChain-based JSON Parser and Validator

Ensures LLM outputs are valid JSON by:
1. Attempting direct JSON parsing
2. Using LangChain output parsers for structured output
3. Auto-fixing common JSON issues (truncation, extra text)
4. Retrying with corrective prompts if needed

Works with any LLM provider (OpenRouter, vLLM, Ollama, etc.)
"""
from __future__ import annotations

import json
import re
import logging
from typing import Optional, Dict, Any, Type, TypeVar
from pydantic import BaseModel, ValidationError

logger = logging.getLogger(__name__)

T = TypeVar('T', bound=BaseModel)


class JSONParseError(Exception):
    """Raised when JSON parsing fails after all attempts"""
    def __init__(self, message: str, raw_content: str, attempts: int = 1):
        super().__init__(message)
        self.raw_content = raw_content
        self.attempts = attempts


def extract_json_from_text(text: str) -> Optional[str]:
    """
    Extract JSON object from text that may contain other content.

    Handles:
    - JSON wrapped in markdown code blocks
    - JSON with leading/trailing text
    - Multiple JSON objects (returns first complete one)
    """
    if not text:
        return None

    text = text.strip()

    # Try direct parse first
    try:
        json.loads(text)
        return text
    except json.JSONDecodeError:
        pass

    # Remove markdown code blocks
    code_block_pattern = r'```(?:json)?\s*([\s\S]*?)\s*```'
    matches = re.findall(code_block_pattern, text)
    if matches:
        for match in matches:
            try:
                json.loads(match.strip())
                return match.strip()
            except json.JSONDecodeError:
                continue

    # Find JSON object boundaries
    # Look for outermost { } pair
    start_idx = text.find('{')
    if start_idx == -1:
        return None

    # Count braces to find matching closing brace
    brace_count = 0
    in_string = False
    escape_next = False

    for i, char in enumerate(text[start_idx:], start_idx):
        if escape_next:
            escape_next = False
            continue

        if char == '\\':
            escape_next = True
            continue

        if char == '"' and not escape_next:
            in_string = not in_string
            continue

        if not in_string:
            if char == '{':
                brace_count += 1
            elif char == '}':
                brace_count -= 1
                if brace_count == 0:
                    # Found complete JSON object
                    candidate = text[start_idx:i+1]
                    try:
                        json.loads(candidate)
                        return candidate
                    except json.JSONDecodeError:
                        pass
                    break

    return None


def fix_truncated_json(text: str) -> Optional[str]:
    """
    Attempt to fix truncated JSON by closing open structures.

    Common patterns:
    - Unclosed strings: add closing quote
    - Unclosed arrays: add ]
    - Unclosed objects: add }
    - Missing value after colon: add null
    """
    if not text:
        return None

    text = text.strip()

    # If already valid, return as-is
    try:
        json.loads(text)
        return text
    except json.JSONDecodeError:
        pass

    # Try to extract any valid JSON first
    extracted = extract_json_from_text(text)
    if extracted:
        return extracted

    # Find the JSON start
    start_idx = text.find('{')
    if start_idx == -1:
        return None

    json_text = text[start_idx:]

    # Track state
    in_string = False
    escape_next = False
    brace_count = 0
    bracket_count = 0
    last_char = ''

    for char in json_text:
        if escape_next:
            escape_next = False
            last_char = char
            continue

        if char == '\\':
            escape_next = True
            last_char = char
            continue

        if char == '"' and not escape_next:
            in_string = not in_string

        if not in_string:
            if char == '{':
                brace_count += 1
            elif char == '}':
                brace_count -= 1
            elif char == '[':
                bracket_count += 1
            elif char == ']':
                bracket_count -= 1

        last_char = char

    # Try to fix common issues
    fixed = json_text

    # Close unclosed string
    if in_string:
        fixed += '"'

    # If last meaningful char was a colon, add null
    stripped = fixed.rstrip()
    if stripped.endswith(':'):
        fixed = stripped + ' null'
    elif stripped.endswith(','):
        fixed = stripped[:-1]  # Remove trailing comma

    # Close arrays
    while bracket_count > 0:
        fixed += ']'
        bracket_count -= 1

    # Close objects
    while brace_count > 0:
        fixed += '}'
        brace_count -= 1

    # Validate the fix
    try:
        json.loads(fixed)
        logger.info(f"Successfully fixed truncated JSON")
        return fixed
    except json.JSONDecodeError as e:
        logger.warning(f"Could not fix JSON: {e}")
        return None


def parse_json_response(
    content: str,
    schema: Optional[Type[T]] = None,
    fix_truncated: bool = True
) -> Dict[str, Any]:
    """
    Parse JSON from LLM response with automatic fixing.

    Args:
        content: Raw LLM response content
        schema: Optional Pydantic model for validation
        fix_truncated: Whether to attempt fixing truncated JSON

    Returns:
        Parsed JSON as dictionary

    Raises:
        JSONParseError: If parsing fails after all attempts
    """
    if not content:
        raise JSONParseError("Empty content", "", 0)

    # Attempt 1: Direct parse
    try:
        parsed = json.loads(content)
        if schema:
            schema.model_validate(parsed)
        return parsed
    except (json.JSONDecodeError, ValidationError):
        pass

    # Attempt 2: Extract JSON from text
    extracted = extract_json_from_text(content)
    if extracted:
        try:
            parsed = json.loads(extracted)
            if schema:
                schema.model_validate(parsed)
            return parsed
        except (json.JSONDecodeError, ValidationError):
            pass

    # Attempt 3: Fix truncated JSON
    if fix_truncated:
        fixed = fix_truncated_json(content)
        if fixed:
            try:
                parsed = json.loads(fixed)
                if schema:
                    schema.model_validate(parsed)
                logger.info("Used fixed JSON")
                return parsed
            except (json.JSONDecodeError, ValidationError):
                pass

    raise JSONParseError(
        f"Failed to parse JSON from response",
        content,
        3
    )


class LLMJSONParser:
    """
    LangChain-style JSON parser that can retry with corrective prompts.

    Usage:
        parser = LLMJSONParser(llm_provider)
        result = await parser.parse(prompt, system_prompt, schema=MyModel)
    """

    def __init__(self, llm_provider, max_retries: int = 2):
        """
        Args:
            llm_provider: BaseLLMProvider instance
            max_retries: Maximum retry attempts for invalid JSON
        """
        self.llm = llm_provider
        self.max_retries = max_retries

    async def parse(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        schema: Optional[Type[T]] = None,
        max_tokens: int = 500,
        temperature: float = 0.0,
        **kwargs
    ) -> Dict[str, Any]:
        """
        Generate and parse JSON response with automatic retry.

        Args:
            prompt: User prompt
            system_prompt: System prompt (JSON instruction added automatically)
            schema: Optional Pydantic model for validation
            max_tokens: Maximum tokens for response
            temperature: LLM temperature
            **kwargs: Additional LLM parameters

        Returns:
            Parsed JSON dictionary

        Raises:
            JSONParseError: If parsing fails after all retries
        """
        # Enhance system prompt with JSON instruction
        json_instruction = "\n\nIMPORTANT: Respond with ONLY valid JSON. No text before or after."
        enhanced_system = (system_prompt or "") + json_instruction

        last_error = None
        last_content = ""

        for attempt in range(self.max_retries + 1):
            try:
                # Build prompt with error feedback
                current_prompt = prompt
                if attempt > 0 and last_error:
                    current_prompt = f"""{prompt}

PREVIOUS ATTEMPT FAILED: {last_error}
Please fix the JSON and try again. Ensure the response is complete and valid JSON."""

                # Generate response
                result = await self.llm.generate(
                    prompt=current_prompt,
                    system_prompt=enhanced_system,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    response_format={"type": "json_object"},
                    **kwargs
                )

                content = result["choices"][0]["message"]["content"]
                last_content = content

                # Parse with automatic fixing
                parsed = parse_json_response(content, schema, fix_truncated=True)
                return parsed

            except JSONParseError as e:
                last_error = str(e)
                last_content = e.raw_content
                logger.warning(f"Attempt {attempt + 1} failed: {e}")

            except Exception as e:
                last_error = str(e)
                logger.error(f"Attempt {attempt + 1} error: {e}")

        raise JSONParseError(
            f"Failed after {self.max_retries + 1} attempts: {last_error}",
            last_content,
            self.max_retries + 1
        )


# Convenience function for one-off parsing
async def parse_llm_json(
    llm_provider,
    prompt: str,
    system_prompt: Optional[str] = None,
    max_tokens: int = 500,
    max_retries: int = 2
) -> Dict[str, Any]:
    """
    Parse JSON from LLM with automatic retry and fixing.

    Args:
        llm_provider: BaseLLMProvider instance
        prompt: User prompt
        system_prompt: Optional system prompt
        max_tokens: Maximum response tokens
        max_retries: Maximum retry attempts

    Returns:
        Parsed JSON dictionary
    """
    parser = LLMJSONParser(llm_provider, max_retries=max_retries)
    return await parser.parse(
        prompt=prompt,
        system_prompt=system_prompt,
        max_tokens=max_tokens
    )
