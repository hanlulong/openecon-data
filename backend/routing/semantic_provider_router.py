"""
Semantic + LLM Provider Router

Provider routing strategy:
1. Deterministic baseline from UnifiedRouter (always available)
2. semantic-router similarity routing over candidate providers
3. LiteLLM JSON routing fallback when semantic confidence is low

The router is designed as a general framework:
- No query-specific hardcoded patches
- Candidate set comes from deterministic routing + fallbacks
- Semantic/LLM stages only choose among valid candidates
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Tuple

from ..config import Settings
from ..embedding_utils import (
    is_openai_embedding_model,
    normalize_embedding_model_name,
)
from ..services.json_parser import extract_json_from_text
from ..utils.providers import normalize_provider_name
from .unified_router import RoutingDecision, UnifiedRouter

logger = logging.getLogger(__name__)

try:
    from semantic_router import Route
    from semantic_router.encoders import HuggingFaceEncoder, OpenAIEncoder
    from semantic_router.routers import SemanticRouter

    SEMANTIC_ROUTER_AVAILABLE = True
except Exception:  # pragma: no cover - optional dependency
    Route = None
    HuggingFaceEncoder = None
    OpenAIEncoder = None
    SemanticRouter = None
    SEMANTIC_ROUTER_AVAILABLE = False

try:
    import litellm

    LITELLM_AVAILABLE = True
except Exception:  # pragma: no cover - optional dependency
    litellm = None
    LITELLM_AVAILABLE = False


class SemanticProviderRouter:
    """Semantic provider router with LiteLLM fallback."""

    _SHARED_ENGINE: Optional[Any] = None
    _SHARED_ENGINE_KEY: Optional[Tuple[str, str, Optional[int], Optional[str]]] = None
    _SHARED_INIT_FAILED: bool = False

    _PROVIDER_HINTS = {
        "WORLDBANK": "cross-country development indicators and macro ratios",
        "FRED": "US macro and financial time series",
        "IMF": "global macro, fiscal, balance-of-payments, and international finance",
        "BIS": "property prices, credit, and central bank policy/financial stability",
        "EUROSTAT": "official EU/euro-area statistics",
        "OECD": "OECD comparative policy and macro statistics",
        "COMTRADE": "bilateral imports/exports and detailed merchandise trade flows",
        "STATSCAN": "official Canada statistics",
        "EXCHANGERATE": "FX conversion and exchange rates",
        "COINGECKO": "crypto prices and market metrics",
    }

    _ROUTE_UTTERANCES = {
        "WORLDBANK": [
            "exports as percent of gdp across countries",
            "imports share of gdp for china and united states",
            "poverty rate in developing countries",
            "life expectancy by country",
            "cross-country macro ratios",
        ],
        "FRED": [
            "us unemployment rate",
            "federal funds rate history",
            "us gdp quarterly",
            "consumer price index united states",
            "st louis fed macro series",
        ],
        "IMF": [
            "government debt to gdp",
            "current account balance",
            "balance of payments and fiscal deficit",
            "global inflation outlook",
            "sovereign debt metrics",
        ],
        "BIS": [
            "house price index and property prices",
            "credit to private non-financial sector",
            "policy rate and debt service ratio",
            "banking stability indicators",
        ],
        "EUROSTAT": [
            "germany hicp inflation",
            "eu unemployment statistics",
            "euro area macro indicators",
            "france gdp official eu data",
        ],
        "OECD": [
            "oecd tax revenue to gdp",
            "oecd productivity comparison",
            "oecd average economic indicators",
            "research and development expenditure oecd",
        ],
        "COMTRADE": [
            "exports to partner country by commodity",
            "bilateral trade flows between countries",
            "imports from china by hs code",
            "trade surplus by reporter and partner",
        ],
        "STATSCAN": [
            "canada gdp and unemployment",
            "ontario inflation statistics",
            "statistics canada provincial data",
            "canadian labor force survey",
        ],
        "EXCHANGERATE": [
            "usd to eur exchange rate",
            "gbp to usd forex",
            "currency conversion rates",
            "fx pair history",
        ],
        "COINGECKO": [
            "bitcoin market cap",
            "ethereum price history",
            "crypto trading volume",
            "altcoin performance",
        ],
    }

    def __init__(
        self,
        settings: Settings,
        deterministic_router: Optional[UnifiedRouter] = None,
        semantic_engine: Optional[Any] = None,
    ):
        self.settings = settings
        self._deterministic = deterministic_router or UnifiedRouter()
        self._semantic_engine = semantic_engine
        self._semantic_initialized = semantic_engine is not None
        self._enabled = bool(self.settings.use_semantic_provider_router)
        self._similarity_threshold = float(self.settings.semantic_router_similarity_threshold)
        self._top_k = max(1, int(self.settings.semantic_router_top_k))

    @classmethod
    def _normalize_provider(cls, provider: Optional[str]) -> Optional[str]:
        """Normalize a provider name using the shared canonical normalization.

        Delegates to ``utils.providers.normalize_provider_name`` so that every
        component in the codebase agrees on canonical (uppercase) names.
        """
        if not provider:
            return None
        normalized = normalize_provider_name(str(provider))
        return normalized if normalized else None

    def _build_candidates(
        self,
        baseline: RoutingDecision,
        llm_provider_hint: Optional[str],
    ) -> List[str]:
        candidates: List[str] = []
        for provider in [baseline.provider, *baseline.fallbacks, llm_provider_hint]:
            normalized = self._normalize_provider(provider)
            if normalized and normalized not in candidates:
                candidates.append(normalized)

        if not candidates:
            candidates = [self._normalize_provider(baseline.provider) or "WORLDBANK"]
        return candidates

    def _init_semantic_engine(self) -> Optional[Any]:
        if self._semantic_initialized:
            return self._semantic_engine

        self._semantic_initialized = True
        if not self._enabled:
            return None
        if not SEMANTIC_ROUTER_AVAILABLE:
            logger.info("semantic-router package unavailable, routing will use deterministic/LiteLLM fallback")
            return None
        if self.__class__._SHARED_INIT_FAILED:
            return None

        model_name = self.settings.semantic_router_encoder_model or "sentence-transformers/all-MiniLM-L6-v2"
        engine_key = self._semantic_engine_key(model_name)
        if (
            self.__class__._SHARED_ENGINE is not None
            and self.__class__._SHARED_ENGINE_KEY == engine_key
        ):
            self._semantic_engine = self.__class__._SHARED_ENGINE
            return self._semantic_engine

        try:
            routes = [
                Route(name=name, utterances=utterances)
                for name, utterances in self._ROUTE_UTTERANCES.items()
            ]
            encoder = self._build_semantic_encoder(model_name)
            router = SemanticRouter(
                encoder=encoder,
                routes=routes,
                top_k=max(1, self._top_k),
            )
            # Local sync is required before first call for LocalIndex.
            try:
                router.sync("local")
            except Exception as sync_exc:
                logger.debug("semantic-router local sync warning: %s", sync_exc)
            self._semantic_engine = router
            self.__class__._SHARED_ENGINE = router
            self.__class__._SHARED_ENGINE_KEY = engine_key
            logger.info("🧭 SemanticProviderRouter initialized with semantic-router")
        except Exception as exc:
            logger.warning("SemanticProviderRouter initialization failed, falling back: %s", exc)
            self._semantic_engine = None
            self.__class__._SHARED_INIT_FAILED = True
        return self._semantic_engine

    def _semantic_engine_key(
        self,
        model_name: str,
    ) -> Tuple[str, str, Optional[int], Optional[str]]:
        normalized_model = normalize_embedding_model_name(model_name)
        if is_openai_embedding_model(normalized_model):
            return (
                "openai",
                normalized_model,
                self.settings.embedding_dimensions,
                self.settings.openai_base_url,
            )
        return ("huggingface", normalized_model, None, None)

    def _build_semantic_encoder(self, model_name: str) -> Any:
        normalized_model = normalize_embedding_model_name(model_name)
        if is_openai_embedding_model(normalized_model):
            if OpenAIEncoder is None:
                raise RuntimeError("semantic-router OpenAI encoder is unavailable")

            encoder_kwargs: Dict[str, Any] = {
                "name": normalized_model,
                "openai_api_key": self.settings.openai_api_key,
                "openai_base_url": self.settings.openai_base_url,
                "openai_org_id": self.settings.openai_org_id,
            }
            if self.settings.embedding_dimensions is not None:
                encoder_kwargs["dimensions"] = self.settings.embedding_dimensions
            return OpenAIEncoder(**encoder_kwargs)

        if HuggingFaceEncoder is None:
            raise RuntimeError("semantic-router HuggingFace encoder is unavailable")
        return HuggingFaceEncoder(name=model_name)

    def _semantic_route_choice(
        self,
        query: str,
        candidates: List[str],
    ) -> Optional[Tuple[str, float]]:
        engine = self._init_semantic_engine()
        if not engine:
            return None

        try:
            limit = min(max(1, self._top_k), len(candidates))
            choice = engine(query, route_filter=candidates, limit=limit)
            choice_item = choice[0] if isinstance(choice, list) else choice
            provider = self._normalize_provider(getattr(choice_item, "name", None))
            if not provider or provider not in candidates:
                return None
            similarity = float(
                getattr(choice_item, "similarity_score", None)
                or getattr(choice_item, "score", 0.0)
                or 0.0
            )
            return provider, max(0.0, min(1.0, similarity))
        except Exception as exc:
            logger.debug("semantic-router route failed: %s", exc)
            return None

    def _litellm_kwargs(self) -> Dict[str, Any]:
        provider = (self.settings.llm_provider or "").strip().lower()
        model = self.settings.llm_model or "gpt-4o-mini"

        if provider == "openrouter":
            return {
                "model": f"openrouter/{model}",
                "api_key": self.settings.openrouter_api_key,
                "timeout": self.settings.semantic_router_litellm_timeout,
            }

        if provider in {"vllm", "lm-studio", "lm_studio"}:
            return {
                "model": f"openai/{model}",
                "api_base": self.settings.llm_base_url,
                "api_key": self.settings.vllm_api_key or "EMPTY",
                "timeout": self.settings.semantic_router_litellm_timeout,
            }

        if provider == "ollama":
            return {
                "model": f"ollama/{model}",
                "api_base": self.settings.llm_base_url,
                "timeout": self.settings.semantic_router_litellm_timeout,
            }

        # Generic fallback.
        return {
            "model": f"openai/{model}",
            "api_base": self.settings.llm_base_url,
            "api_key": self.settings.vllm_api_key or "EMPTY",
            "timeout": self.settings.semantic_router_litellm_timeout,
        }

    async def _litellm_route_choice(
        self,
        query: str,
        indicators: List[str],
        country: Optional[str],
        countries: List[str],
        candidates: List[str],
    ) -> Optional[Tuple[str, float, str]]:
        if not self.settings.use_litellm_router_fallback:
            return None
        if not LITELLM_AVAILABLE:
            return None

        indicator_text = ", ".join(indicators) if indicators else "None"
        geo = countries[:] if countries else ([country] if country else [])
        geo_text = ", ".join([str(g) for g in geo if g]) if geo else "None"
        candidate_lines = [
            f"- {provider}: {self._PROVIDER_HINTS.get(provider, '')}" for provider in candidates
        ]

        system_prompt = (
            "Route the economic data query to exactly one provider from the candidate list. "
            "Return ONLY valid JSON with keys provider, confidence, reasoning."
        )
        user_prompt = (
            f"Query: {query}\n"
            f"Parsed indicators: {indicator_text}\n"
            f"Country context: {geo_text}\n"
            "Candidates:\n"
            + "\n".join(candidate_lines)
            + "\nConstraints:\n"
            "- provider must be one of the candidate names exactly.\n"
            "- confidence must be between 0 and 1.\n"
        )

        try:
            kwargs = self._litellm_kwargs()
            response = await litellm.acompletion(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.0,
                max_tokens=180,
                response_format={"type": "json_object"},
                **kwargs,
            )
            content = response.choices[0].message.content if response and response.choices else ""
            parsed: Dict[str, Any]
            if isinstance(content, dict):
                parsed = content
            else:
                json_text = extract_json_from_text(str(content) if content is not None else "")
                parsed = json.loads(json_text) if json_text else {}

            provider = self._normalize_provider(parsed.get("provider"))
            if not provider or provider not in candidates:
                return None

            confidence = float(parsed.get("confidence", 0.58))
            confidence = max(0.0, min(1.0, confidence))
            reasoning = str(parsed.get("reasoning") or "LiteLLM provider routing decision")
            return provider, confidence, reasoning
        except Exception as exc:
            logger.debug("LiteLLM routing fallback failed: %s", exc)
            return None

    @staticmethod
    def _fallbacks_for(provider: str, candidates: List[str]) -> List[str]:
        return [candidate for candidate in candidates if candidate != provider][:4]

    async def route(
        self,
        query: str,
        indicators: Optional[List[str]] = None,
        country: Optional[str] = None,
        countries: Optional[List[str]] = None,
        llm_provider_hint: Optional[str] = None,
        baseline_decision: Optional[RoutingDecision] = None,
    ) -> RoutingDecision:
        """Route provider using semantic-router then LiteLLM fallback."""
        indicators = indicators or []
        countries = countries or []

        baseline = baseline_decision or self._deterministic.route(
            query=query,
            indicators=indicators,
            country=country,
            countries=countries,
            llm_provider=llm_provider_hint,
        )

        if not self._enabled:
            return baseline

        candidates = self._build_candidates(baseline, llm_provider_hint)

        semantic_choice = self._semantic_route_choice(query=query, candidates=candidates)
        if semantic_choice:
            provider, similarity = semantic_choice
            if similarity >= self._similarity_threshold:
                return RoutingDecision(
                    provider=provider,
                    confidence=similarity,
                    fallbacks=self._fallbacks_for(provider, candidates),
                    reasoning=f"semantic-router similarity match ({similarity:.2f})",
                    match_type="semantic",
                    matched_pattern="semantic-router",
                )

        litellm_choice = await self._litellm_route_choice(
            query=query,
            indicators=indicators,
            country=country,
            countries=countries,
            candidates=candidates,
        )
        if litellm_choice:
            provider, confidence, reasoning = litellm_choice
            return RoutingDecision(
                provider=provider,
                confidence=confidence,
                fallbacks=self._fallbacks_for(provider, candidates),
                reasoning=reasoning,
                match_type="litellm",
                matched_pattern="litellm",
            )

        return baseline
