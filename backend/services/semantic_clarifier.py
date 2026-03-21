from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence


@dataclass(frozen=True)
class SemanticAmbiguityProfile:
    key: str
    concept_label: str
    trigger_patterns: Sequence[str]
    replacement_patterns: Sequence[str]
    block_patterns: Sequence[str]
    option_labels: Sequence[str]


class SemanticClarifier:
    """
    Detect broad economic concepts that are too underspecified to fetch safely.

    The goal is not to guess the "best" indicator. The goal is to stop before
    fetch and ask the user for a more precise metric when a query uses a broad
    concept that maps to multiple common indicators.
    """

    PROFILES: Sequence[SemanticAmbiguityProfile] = (
        SemanticAmbiguityProfile(
            key="employment_metric",
            concept_label="employment",
            trigger_patterns=(
                r"\btotal employment\b",
                r"\bemployment\b",
                r"\bemployed\b",
                r"\bjobs\b",
                r"\blabor market\b",
                r"\blabour market\b",
            ),
            replacement_patterns=(
                r"\btotal employment\b",
                r"\bemployment\b",
                r"\bemployed\b",
                r"\bjobs\b",
                r"\blabor market\b",
                r"\blabour market\b",
            ),
            block_patterns=(
                r"\bunemployment\b",
                r"\bnumber\s+employed\b",
                r"\bnumber\s+of\s+(?:people\s+)?employed\b",
                r"\bemployed\s+persons?\b",
                r"\bemployment\s+rate\b",
                r"\bemployment[-\s]+to[-\s]+population\b",
                r"\bemployment[-\s]+population\b",
                r"\blabor force participation\b",
                r"\blabour force participation\b",
                r"\bnonfarm payrolls?\b",
                r"\bpayroll employment\b",
                r"\bemployment growth\b",
                r"\bjob growth\b",
                r"\bhours worked\b",
                r"\bemployment by\b",
            ),
            option_labels=(
                "number employed",
                "employment rate",
                "employment-to-population ratio",
            ),
        ),
        SemanticAmbiguityProfile(
            key="interest_rate_metric",
            concept_label="interest rates",
            trigger_patterns=(
                r"\binterest rates?\b",
            ),
            replacement_patterns=(
                r"\binterest rates?\b",
            ),
            block_patterns=(
                r"\bpolicy rate\b",
                r"\brepo rate\b",
                r"\bfed(?:eral)? funds\b",
                r"\bgovernment bond\b",
                r"\bbond yield\b",
                r"\btreasury yield\b",
                r"\bmortgage rate\b",
                r"\blending rate\b",
                r"\bdeposit rate\b",
                r"\bshort[-\s]?term rate\b",
                r"\blong[-\s]?term rate\b",
                r"\b\d{1,2}[-\s]?year\b",
                r"\breal interest rate\b",
            ),
            option_labels=(
                "policy rate",
                "government bond yield",
                "lending rate",
                "mortgage rate",
            ),
        ),
        SemanticAmbiguityProfile(
            key="productivity_metric",
            concept_label="productivity",
            trigger_patterns=(
                r"\bproductivity\b",
            ),
            replacement_patterns=(
                r"\bproductivity\b",
            ),
            block_patterns=(
                r"\blabor productivity\b",
                r"\blabour productivity\b",
                r"\btotal factor productivity\b",
                r"\btfp\b",
                r"\bmultifactor productivity\b",
                r"\bper worker\b",
                r"\bper hour\b",
                r"\bproductivity growth\b",
            ),
            option_labels=(
                "labor productivity",
                "labor productivity growth",
                "total factor productivity",
            ),
        ),
        SemanticAmbiguityProfile(
            key="debt_metric",
            concept_label="debt",
            trigger_patterns=(
                r"\bdebt\b",
            ),
            replacement_patterns=(
                r"\bdebt\b",
            ),
            block_patterns=(
                r"\bgovernment debt\b",
                r"\bpublic debt\b",
                r"\bsovereign debt\b",
                r"\bhousehold debt\b",
                r"\bexternal debt\b",
                r"\bprivate sector credit\b",
                r"\bdebt service\b",
                r"\bdebt[-\s]+to[-\s]+gdp\b",
                r"\bgdp[-\s]+to[-\s]+debt\b",
                r"\bgdp/debt\b",
                r"\bdebt ratio\b",
                r"\bmortgage debt\b",
            ),
            option_labels=(
                "government debt (% of GDP)",
                "household debt",
                "private sector credit",
                "external debt",
            ),
        ),
        SemanticAmbiguityProfile(
            key="trade_metric",
            concept_label="trade",
            trigger_patterns=(
                r"\btrade\b",
            ),
            replacement_patterns=(
                r"\btrade\b",
            ),
            block_patterns=(
                r"\bimports?\b",
                r"\bexports?\b",
                r"\btrade balance\b",
                r"\btrade openness\b",
                r"\bcurrent account\b",
                r"\bterms of trade\b",
                r"\btariffs?\b",
                r"\btrade war\b",
            ),
            option_labels=(
                "exports",
                "imports",
                "trade balance",
                "trade openness",
            ),
        ),
        SemanticAmbiguityProfile(
            key="wages_metric",
            concept_label="wages or earnings",
            trigger_patterns=(
                r"\bwages?\b",
                r"\bearnings?\b",
            ),
            replacement_patterns=(
                r"\bwages?\b",
                r"\bearnings?\b",
            ),
            block_patterns=(
                r"\breal wages?\b",
                r"\bnominal wages?\b",
                r"\bhourly earnings?\b",
                r"\bweekly earnings?\b",
                r"\bmonthly earnings?\b",
                r"\bmedian earnings?\b",
                r"\baverage earnings?\b",
                r"\bcompensation\b",
                r"\bunit labor costs?\b",
                r"\bunit labour costs?\b",
            ),
            option_labels=(
                "nominal wages",
                "real wages",
                "hourly earnings",
                "median earnings",
            ),
        ),
        SemanticAmbiguityProfile(
            key="prices_metric",
            concept_label="prices",
            trigger_patterns=(
                r"\bprices\b",
                r"\bprice level\b",
            ),
            replacement_patterns=(
                r"\bprices\b",
                r"\bprice level\b",
            ),
            block_patterns=(
                r"\binflation\b",
                r"\bcpi\b",
                r"\bhicp\b",
                r"\bproducer prices?\b",
                r"\bppi\b",
                r"\bhouse prices?\b",
                r"\bgdp deflator\b",
                r"\bprice index\b",
            ),
            option_labels=(
                "consumer price inflation",
                "CPI index level",
                "producer price inflation",
                "house price index",
            ),
        ),
        SemanticAmbiguityProfile(
            key="broad_economic_concept",
            concept_label="economic indicators",
            trigger_patterns=(
                r"\beconomic\s+data\b",
                r"\beconomic\s+indicators?\b",
                r"\beconomy\s+doing\b",
                r"\beconomy\b",
                r"\beconomic\s+(?:performance|outlook|situation|conditions?)\b",
            ),
            replacement_patterns=(
                r"\beconomic\s+data\b",
                r"\beconomic\s+indicators?\b",
                r"\b(?:how\s+is\s+)?(?:the\s+)?economy\s+doing\b",
                r"\beconomy\b",
                r"\beconomic\s+(?:performance|outlook|situation|conditions?)\b",
            ),
            block_patterns=(
                r"\bgdp\b",
                r"\binflation\b",
                r"\bcpi\b",
                r"\bunemployment\b",
                r"\binterest\s+rate\b",
                r"\btrade\b",
                r"\bdebt\b",
                r"\bpopulation\b",
                r"\bexports?\b",
                r"\bimports?\b",
                r"\bwages?\b",
                r"\bproductivity\b",
                r"\bprice\b",
                r"\bprices\b",
            ),
            option_labels=(
                "GDP (economic output)",
                "inflation rate (price stability)",
                "unemployment rate (labor market)",
            ),
        ),
    )

    # Patterns indicating the user is asking ABOUT available data/indicators,
    # not requesting data.  When these match, semantic ambiguity clarification
    # is skipped so the query reaches the LLM for queryType classification.
    _INFORMATIONAL_BYPASS_PATTERNS = (
        r"\bwhat\s+.*\s+(?:series|indicators?|datasets?|metrics?)\b",
        r"\bavailable\s+(?:\w+\s+)*(?:indicators?|series|data)\b",
        r"\blist\s+(?:\w+\s+)*(?:indicators?|series|data)\b",
        r"\bwhich\s+(?:\w+\s+)*(?:indicators?|series|data)\b",
        r"\bdoes\s+\w+\s+(?:have|offer|provide)\s+(?:\w+\s+)*(?:data|indicators?|series)\b",
        r"\bwhat\s+(?:data|indicators?|series)\s+(?:is|are)\s+available\b",
        r"\b(?:browse|search|find)\s+(?:\w+\s+)*(?:indicators?|series)\b",
    )

    def detect(self, query: str) -> Optional[Dict[str, Any]]:
        query_text = str(query or "").strip()
        if not query_text:
            return None

        query_lower = query_text.lower()

        # Skip semantic ambiguity for informational/metadata queries.
        # These should reach the LLM for proper queryType classification.
        for pattern in self._INFORMATIONAL_BYPASS_PATTERNS:
            if re.search(pattern, query_lower):
                return None
        for profile in self.PROFILES:
            if any(re.search(pattern, query_lower) for pattern in profile.block_patterns):
                continue
            if not any(re.search(pattern, query_lower) for pattern in profile.trigger_patterns):
                continue

            options = [
                {
                    "label": option_label,
                    "value": self._rewrite_query(query_text, profile, option_label),
                }
                for option_label in profile.option_labels
            ]

            return {
                "kind": profile.key,
                "concept_label": profile.concept_label,
                "original_query": query_text,
                "question_lines": [
                    f"Your query uses a broad concept: {profile.concept_label}.",
                    "To avoid guessing the wrong indicator, choose the metric you want:",
                ],
                "options": options,
            }

        return None

    def build_custom_query(self, payload: Dict[str, Any], reply: str) -> Optional[str]:
        query_text = str(payload.get("original_query") or "").strip()
        if not query_text:
            return None

        user_text = str(reply or "").strip()
        if not user_text:
            return None

        profile_key = str(payload.get("kind") or "").strip()
        profile = next((item for item in self.PROFILES if item.key == profile_key), None)
        if not profile:
            return None

        return self._rewrite_query(query_text, profile, user_text)

    def _rewrite_query(
        self,
        original_query: str,
        profile: SemanticAmbiguityProfile,
        replacement_text: str,
    ) -> str:
        replacement = str(replacement_text or "").strip()
        if not replacement:
            return original_query

        for pattern in profile.replacement_patterns:
            if re.search(pattern, original_query, flags=re.IGNORECASE):
                return re.sub(pattern, replacement, original_query, count=1, flags=re.IGNORECASE)

        return f"{replacement} for {original_query}"
