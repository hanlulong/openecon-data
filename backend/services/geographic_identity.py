"""Resolve jurisdiction identities before series selection can conflate them."""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

from ..models import ClarificationOption, ParsedIntent, QueryResponse
from ..routing.country_resolver import CountryResolver
from ..utils.jurisdictions import (
    country_display_name,
    geographic_name_collisions,
    has_geographic_collision,
    normalize_geographic_name,
)
from .conversation import conversation_manager
from .semantic_match_judge import _structured_judgment, bounded_judge_timeout


logger = logging.getLogger(__name__)


class GeographicIdentityDecision(BaseModel):
    outcome: Literal["select", "clarify"]
    entity_ids: list[str] = Field(min_length=1)
    reason: str = ""


def _entity_label(entity: dict[str, Any], language: str = "en") -> str:
    if entity["kind"] == "country":
        return f"{entity['name']}（国家）" if language == "zh" else f"{entity['name']} (country)"
    parent = country_display_name(entity["country"])
    return (
        f"{entity['name']}（{parent}的行政区）"
        if language == "zh" else f"{entity['name']} ({entity.get('type', 'subdivision')} in {parent})"
    )


def geographic_requery(query: str, entity: dict[str, Any]) -> str:
    return f"{query.strip()} — Geographic scope: {_entity_label(entity)}."


def _qualified_identity(query: str, intent: ParsedIntent, collision: dict[str, Any]) -> tuple[Optional[str], bool]:
    """Corroborate explicit scope syntax with the parser and registry.

    Scope type words do not choose an arbitrary place: they must refer to this
    registered name, and a subdivision also needs its named, parsed parent.
    """
    text = normalize_geographic_name(query)
    name = re.escape(collision["name"])
    params = intent.parameters or {}
    parsed = list(params.get("countries") or []) or ([params["country"]] if params.get("country") else [])
    parsed_codes = {CountryResolver.normalize(str(value)) for value in parsed}
    mentioned_codes = set(CountryResolver.detect_all_countries_in_query(query))
    qualified = bool(intent.subnationalRegion)
    choices = []
    for entity in collision["entities"]:
        kind = "country" if entity["kind"] == "country" else normalize_geographic_name(entity.get("type", ""))
        if not kind:
            continue
        type_names_place = re.search(rf"\b{re.escape(kind)}\s+(?:of\s+)?{name}(?![a-z0-9])", text)
        place_names_type = re.search(rf"(?<![a-z0-9]){name}\s*\(?\s*{re.escape(kind)}\b", text)
        if not (type_names_place or place_names_type):
            continue
        qualified = True
        if entity["country"] not in parsed_codes:
            continue
        if entity["kind"] == "country":
            if not intent.subnationalRegion:
                choices.append(entity["id"])
        elif (
            entity["country"] in mentioned_codes
            and normalize_geographic_name(intent.subnationalRegion) == normalize_geographic_name(entity["name"])
        ):
            choices.append(entity["id"])
    return (choices[0] if len(choices) == 1 else None), qualified


def apply_geographic_entity(intent: ParsedIntent, entity: dict[str, Any], collision: dict[str, Any]) -> None:
    """Bind a chosen identity without treating its name as another country."""
    params = dict(intent.parameters or {})
    competing = {entry["country"] for entry in collision["entities"]}
    previous = list(params.get("countries") or []) or ([params["country"]] if params.get("country") else [])
    others = [
        value for value in previous
        if (CountryResolver.normalize(str(value)) or str(value).upper()) not in competing
    ]
    countries = list(dict.fromkeys([entity["country"], *others]))
    if len(countries) == 1:
        params["country"] = countries[0]
        params.pop("countries", None)
    else:
        params["countries"] = countries
        params.pop("country", None)
    params["__geographic_entity"] = entity["id"]
    intent.parameters = params
    intent.subnationalRegion = entity["name"] if entity["kind"] == "subdivision" else None


def _unavailable(conversation_id: str, intent: ParsedIntent, steps: Optional[list[Any]]) -> QueryResponse:
    message = (
        "暂时无法确认请求中的地理范围。请明确国家或行政区名称后重试。"
        if intent.language == "zh" else
        "I could not verify the geographic scope right now. Specify the country or subdivision and try again."
    )
    return QueryResponse(conversationId=conversation_id, intent=intent, clarificationNeeded=False,
                         error="verification_unavailable", message=message, processingSteps=steps)


async def resolve_geographic_identity(
    qs: Any,
    query: str,
    intent: ParsedIntent,
    conversation_id: str,
    previous_state: Any = None,
    processing_steps: Optional[list[Any]] = None,
) -> Optional[QueryResponse]:
    """Resolve known name collisions with the model, or ask a real scope choice.

    Registry entries supply identities only. Provider preference, the first
    search result, and a country-name substring cannot resolve an ambiguity.
    Informational questions stay on their existing path.
    """
    if str(intent.queryType or "data_fetch") not in {"data_fetch", "analysis", "comparison"} or intent.clarificationNeeded:
        return None
    for collision in geographic_name_collisions(query):
        entities = {entity["id"]: entity for entity in collision["entities"]}
        existing = (intent.parameters or {}).get("__geographic_entity")
        if existing in entities:
            apply_geographic_entity(intent, entities[existing], collision)
            continue
        qualified_id, _ = _qualified_identity(query, intent, collision)
        if qualified_id:
            apply_geographic_entity(intent, entities[qualified_id], collision)
            continue
        timeout = bounded_judge_timeout()
        if timeout is None:
            return _unavailable(conversation_id, intent, processing_steps)
        context = {
            key: getattr(previous_state, key, None)
            for key in ("country", "countries", "subnational_region", "original_query")
        } if previous_state is not None else None
        decision = await _structured_judgment(
            qs,
            system_prompt=(
                "Resolve geographic identity for an economic-data request. The registry lists possible "
                "country and subdivision identities sharing a name. Use the user's explicit wording and "
                "relevant conversation context; a parser's confident guess alone is not evidence. "
                "Never choose a place because a provider has data for it. Select exactly one ID in entity_ids "
                "only when the intended extent is clear. Otherwise clarify with at least two materially "
                "distinct intended extents. Different administrative classifications of the SAME territory "
                "are not distinct extents and do not require a question. Explicit country/state/province "
                "wording and a named parent resolve the corresponding interpretation. Do not turn a "
                "subdivision name into an additional country. Return only IDs from the supplied registry. "
                "Required JSON keys: outcome ('select' or 'clarify'), entity_ids (nonempty list of exact "
                "registry IDs), reason. A clarify decision lists at least two distinct identities."
            ),
            user_prompt=json.dumps({"query": query, "parsed_scope": {
                "country": (intent.parameters or {}).get("country"),
                "countries": (intent.parameters or {}).get("countries"),
                "subnational_region": intent.subnationalRegion,
            }, "previous_scope": context, "candidates": list(entities.values())}, ensure_ascii=False),
            response_model=GeographicIdentityDecision,
            answer_tokens=220,
            label="Geographic identity judge",
            timeout_s=timeout,
        )
        valid = bool(decision and decision.entity_ids and all(value in entities for value in decision.entity_ids))
        if not valid or (decision.outcome == "clarify" and len(set(decision.entity_ids)) < 2):
            logger.info("Geographic identity unresolved: outcome=%s valid_ids=%d",
                        getattr(decision, "outcome", None),
                        sum(value in entities for value in getattr(decision, "entity_ids", [])))
            qualified_id, qualified = _qualified_identity(query, intent, collision)
            if qualified_id:
                apply_geographic_entity(intent, entities[qualified_id], collision)
                continue
            if qualified:
                return _unavailable(conversation_id, intent, processing_steps)
            # The registry itself proves competing extents. Asking which one
            # the user intends does not guess a semantic winner or a provider.
            decision = GeographicIdentityDecision(outcome="clarify", entity_ids=list(entities))
        ids = list(dict.fromkeys(decision.entity_ids))
        if decision.outcome == "select" and len(ids) == 1:
            apply_geographic_entity(intent, entities[ids[0]], collision)
            continue
        if decision.outcome != "clarify" or len(ids) < 2:
            return _unavailable(conversation_id, intent, processing_steps)
        language = "zh" if intent.language == "zh" else "en"
        options = [ClarificationOption(
            id=str(index), label=_entity_label(entities[entity_id], language),
            value=geographic_requery(query, entities[entity_id]),
            query=geographic_requery(query, entities[entity_id]),
        ) for index, entity_id in enumerate(ids, start=1)]
        question = (
            f"这里的“{collision['name']}”指哪个地区？"
            if language == "zh" else f"Which place do you mean by “{collision['name']}”?"
        )
        conversation_manager.set_pending_semantic_clarification(conversation_id, {
            "kind": "geography", "original_query": query, "language": language,
            "options": [option.model_dump() for option in options],
            "entities": {str(index): entities[entity_id] for index, entity_id in enumerate(ids, start=1)},
        })
        return QueryResponse(conversationId=conversation_id, intent=intent, clarificationNeeded=True,
                             clarificationQuestions=[question], clarificationOptions=options,
                             processingSteps=processing_steps)
    return None
