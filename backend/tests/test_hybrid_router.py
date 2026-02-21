from __future__ import annotations

import json
import unittest

from backend.routing.hybrid_router import HybridRouter


class _FakeLLMProvider:
    def __init__(self, payload: dict):
        self.payload = payload

    async def generate(self, *args, **kwargs):
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(self.payload),
                    }
                }
            ]
        }

    async def health_check(self) -> bool:
        return True


class HybridRouterTests(unittest.IsolatedAsyncioTestCase):
    async def test_falls_back_when_llm_returns_invalid_provider(self):
        router = HybridRouter(
            llm_provider=_FakeLLMProvider(
                {
                    "provider": "MadeUpProvider",
                    "confidence": 0.99,
                    "reasoning": "invalid choice",
                }
            )
        )

        decision = await router.route("US exports to China", indicators=["exports"])

        self.assertEqual(decision.provider, "Comtrade")
        self.assertNotEqual(decision.match_type, "hybrid_llm")

    async def test_explicit_provider_guardrail_wins(self):
        router = HybridRouter(
            llm_provider=_FakeLLMProvider(
                {
                    "provider": "WorldBank",
                    "confidence": 0.8,
                    "reasoning": "wrong for explicit request",
                }
            )
        )

        decision = await router.route("from OECD unemployment rate in Japan", indicators=["unemployment rate"])

        self.assertEqual(decision.provider, "OECD")

    async def test_llm_can_override_within_candidate_set(self):
        router = HybridRouter(
            llm_provider=_FakeLLMProvider(
                {
                    "provider": "IMF",
                    "confidence": 0.84,
                    "reasoning": "prefer IMF fiscal series",
                    "fallbacks": ["WorldBank"],
                    "concept": "government_debt",
                }
            )
        )

        decision = await router.route("Italy government debt 2015-2023", indicators=["government debt"], country="IT")

        self.assertEqual(decision.provider, "IMF")
        self.assertEqual(decision.match_type, "hybrid_llm")
        self.assertEqual(decision.matched_pattern, "concept:government_debt")

    async def test_catalog_guardrail_rejects_unavailable_provider(self):
        router = HybridRouter(
            llm_provider=_FakeLLMProvider(
                {
                    "provider": "WorldBank",
                    "confidence": 0.9,
                    "reasoning": "wrong for crypto",
                }
            )
        )

        decision = await router.route("bitcoin price history", indicators=["bitcoin"])

        self.assertEqual(decision.provider, "CoinGecko")
        self.assertNotEqual(decision.match_type, "hybrid_llm")


if __name__ == "__main__":
    unittest.main()
