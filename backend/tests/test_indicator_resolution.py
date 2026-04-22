from __future__ import annotations

import unittest

from backend.services.indicator_resolution import find_exact_provider_title_match


class IndicatorResolutionTests(unittest.TestCase):
    def test_find_exact_provider_title_match_prefers_closest_worldbank_completion_variant(self) -> None:
        match = find_exact_provider_title_match(
            "Completion rate, upper secondary education, female (%)",
            "WorldBank",
        )

        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match.get("code"), "UIS.CR.3.F")


if __name__ == "__main__":
    unittest.main()
