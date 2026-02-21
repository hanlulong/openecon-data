#!/usr/bin/env python3
"""
vLLM Performance Test Script

Tests the local vLLM server with various queries and measures performance.
Supports different models and compares with OpenRouter.

Usage:
    python scripts/test_vllm.py                    # Test default model
    python scripts/test_vllm.py --model gpt-oss-120b
    python scripts/test_vllm.py --url http://hansearch.com:8000
    python scripts/test_vllm.py --compare          # Compare with OpenRouter
"""

import asyncio
import argparse
import json
import time
import sys
from pathlib import Path

# Add backend to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from backend.services.llm import create_llm_provider, VLLMProvider
from backend.config import get_settings


async def check_server(base_url: str) -> dict:
    """Check if vLLM server is accessible and get model info"""
    import httpx

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"{base_url}/v1/models")
            if response.status_code == 200:
                data = response.json()
                models = [m["id"] for m in data.get("data", [])]
                return {"available": True, "models": models}
            return {"available": False, "error": f"Status {response.status_code}"}
    except httpx.ConnectError as e:
        return {"available": False, "error": f"Connection failed: {e}"}
    except Exception as e:
        return {"available": False, "error": str(e)}


async def run_test(provider, name: str, prompt: str, system_prompt: str = None,
                   max_tokens: int = 100, json_mode: bool = False) -> dict:
    """Run a single test and return results"""
    start = time.time()

    try:
        response_format = {"type": "json_object"} if json_mode else None

        result = await provider.generate(
            prompt=prompt,
            system_prompt=system_prompt,
            temperature=0.0,
            max_tokens=max_tokens,
            response_format=response_format
        )

        elapsed = time.time() - start
        content = result["choices"][0]["message"].get("content") or ""
        usage = result.get("usage", {})

        # Check JSON validity if json_mode
        json_valid = None
        if json_mode:
            try:
                json.loads(content)
                json_valid = True
            except json.JSONDecodeError:
                json_valid = False

        return {
            "name": name,
            "success": True,
            "time": elapsed,
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "content": content,
            "json_valid": json_valid
        }

    except Exception as e:
        return {
            "name": name,
            "success": False,
            "time": time.time() - start,
            "error": str(e)
        }


async def main():
    parser = argparse.ArgumentParser(description="Test vLLM server performance")
    parser.add_argument("--url", default="http://localhost:8000",
                        help="vLLM server URL")
    parser.add_argument("--model", default=None,
                        help="Model to use (auto-detected if not specified)")
    parser.add_argument("--compare", action="store_true",
                        help="Compare with OpenRouter")
    parser.add_argument("--timeout", type=int, default=120,
                        help="Request timeout in seconds")
    args = parser.parse_args()

    print("=" * 70)
    print("vLLM Performance Test")
    print("=" * 70)

    # Check server
    print(f"\nChecking server at {args.url}...")
    server_status = await check_server(args.url)

    if not server_status["available"]:
        print(f"❌ Server not available: {server_status['error']}")
        print("\nTo start vLLM server:")
        print("  python -m vllm.entrypoints.openai.api_server \\")
        print("    --model /path/to/model \\")
        print("    --host 127.0.0.1 --port 8000")
        return 1

    print(f"✅ Server available")
    print(f"   Models: {', '.join(server_status['models'])}")

    # Determine model to use
    model = args.model or (server_status["models"][0] if server_status["models"] else "default")
    print(f"\nUsing model: {model}")

    # Create provider
    vllm = create_llm_provider("vllm", {
        "base_url": args.url,
        "model": model,
        "timeout": args.timeout
    })

    if vllm.model_config:
        print(f"Model family: {vllm.model_config.family.value}")
        print(f"Supports thinking: {vllm.model_config.supports_thinking}")

    # Define test cases
    # Note: Reasoning models (like gpt-oss-120b) need higher max_tokens
    # because they generate thinking content before the actual response
    tests = [
        {
            "name": "Simple math",
            "prompt": "What is 2 + 2? Answer with just the number.",
            "max_tokens": 50  # Reasoning models need more tokens
        },
        {
            "name": "Short response",
            "prompt": "Name three primary colors. Be brief.",
            "max_tokens": 100  # Reasoning models need more tokens
        },
        {
            "name": "JSON parsing (economic query)",
            "system_prompt": """You are an economic data query parser.
Return ONLY valid JSON with this format:
{"apiProvider": "FRED", "indicators": ["GDP"], "parameters": {"country": "US"}, "clarificationNeeded": false}""",
            "prompt": "Show me US GDP for the last 5 years",
            "max_tokens": 300,  # Extra tokens for reasoning models
            "json_mode": True
        },
        {
            "name": "Complex query parsing",
            "system_prompt": """You are an economic data query parser.
Return ONLY valid JSON with this format:
{"apiProvider": "WORLDBANK", "indicators": ["inflation"], "parameters": {"countries": ["US", "UK", "JP"]}, "clarificationNeeded": false}""",
            "prompt": "Compare inflation rates between US, UK, and Japan over the past decade",
            "max_tokens": 500,  # Extra tokens for reasoning + multi-country response
            "json_mode": True
        }
    ]

    # Run tests
    print("\n" + "-" * 70)
    print("Running tests...")
    print("-" * 70)

    results = []
    for test in tests:
        print(f"\n📋 {test['name']}")
        print(f"   Prompt: {test['prompt'][:50]}...")

        result = await run_test(
            vllm,
            test["name"],
            test["prompt"],
            test.get("system_prompt"),
            test.get("max_tokens", 100),
            test.get("json_mode", False)
        )

        if result["success"]:
            print(f"   ✅ Time: {result['time']:.2f}s")
            print(f"   Tokens: prompt={result.get('prompt_tokens', 'N/A')}, completion={result.get('completion_tokens', 'N/A')}")
            if result.get("json_valid") is not None:
                print(f"   JSON valid: {'✅' if result['json_valid'] else '❌'}")
            # Show truncated response
            content = result.get("content") or ""
            if len(content) > 100:
                print(f"   Response: {content[:100]}...")
            else:
                print(f"   Response: {content}")
        else:
            print(f"   ❌ Error: {result['error']}")

        results.append(result)

    # Summary
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)

    successful = [r for r in results if r["success"]]
    failed = [r for r in results if not r["success"]]

    print(f"\nTests: {len(successful)}/{len(results)} passed")

    if successful:
        times = [r["time"] for r in successful]
        tokens = [r.get("completion_tokens", 0) or 0 for r in successful]

        print(f"Average time: {sum(times)/len(times):.2f}s")
        print(f"Min time: {min(times):.2f}s")
        print(f"Max time: {max(times):.2f}s")

        if sum(tokens) > 0:
            total_time = sum(times)
            total_tokens = sum(tokens)
            print(f"Total tokens: {total_tokens}")
            print(f"Tokens/second: {total_tokens/total_time:.1f}")

    if failed:
        print(f"\nFailed tests:")
        for r in failed:
            print(f"  - {r['name']}: {r['error'][:50]}...")

    # Compare with OpenRouter
    if args.compare and successful:
        print("\n" + "=" * 70)
        print("COMPARISON WITH OPENROUTER")
        print("=" * 70)

        settings = get_settings()
        if settings.openrouter_api_key:
            openrouter = create_llm_provider("openrouter", {
                "api_key": settings.openrouter_api_key,
                "model": "openai/gpt-4o-mini"
            })

            # Run same tests
            or_results = []
            for test in tests[:2]:  # Just run first 2 tests
                result = await run_test(
                    openrouter,
                    test["name"],
                    test["prompt"],
                    test.get("system_prompt"),
                    test.get("max_tokens", 100),
                    test.get("json_mode", False)
                )
                or_results.append(result)

            or_successful = [r for r in or_results if r["success"]]
            if or_successful:
                or_avg = sum(r["time"] for r in or_successful) / len(or_successful)
                vllm_avg = sum(r["time"] for r in successful[:2]) / min(2, len(successful))

                print(f"\nOpenRouter (gpt-4o-mini): {or_avg:.2f}s average")
                print(f"vLLM ({model}): {vllm_avg:.2f}s average")

                if or_avg < vllm_avg:
                    print(f"OpenRouter is {vllm_avg/or_avg:.1f}x faster")
                else:
                    print(f"vLLM is {or_avg/vllm_avg:.1f}x faster")
        else:
            print("OpenRouter API key not configured, skipping comparison")

    return 0 if len(failed) == 0 else 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
