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
        self.assertEqual(decision.semantic_authority, "llm_adjudication")
        self.assertTrue(decision.final_authority)
        self.assertIn("IMF", decision.candidate_providers)

    def test_prompt_does_not_encode_keyword_provider_shortcut_rules(self):
        router = HybridRouter()

        prompt = router._build_prompt(  # pylint: disable=protected-access
            "property prices in Canada",
            indicators=["property prices"],
            country="CA",
            countries=[],
            candidates=["BIS", "StatsCan", "WorldBank"],
        )

        self.assertNotIn("Property prices -> BIS", prompt)
        self.assertNotIn("Canada official statistics -> StatsCan", prompt)
        self.assertNotIn("->", prompt)
        self.assertIn("Do not apply hidden keyword-to-provider rules", prompt)

    async def test_wrong_llm_semantic_choice_is_not_fixed_by_catalog_shortcut(self):
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

        self.assertEqual(decision.provider, "WorldBank")
        self.assertEqual(decision.match_type, "hybrid_llm")


if __name__ == "__main__":
    unittest.main()
