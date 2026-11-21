"""Resolve jurisdiction identities before series selection can conflate them."""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Literal, Optional, TYPE_CHECKING

from pydantic import BaseModel, Field

from ..models import ClarificationOption, ParsedIntent, QueryResponse
from ..routing.country_resolver import CountryResolver
from ..utils.jurisdictions import (
    country_display_name,
    country_evidence_text,
    geographic_name_collisions,
    geographic_registry,
    has_geographic_collision,
    normalize_geographic_name,
)
from .conversation import conversation_manager
from .semantic_match_judge import _structured_judgment, bounded_judge_timeout

if TYPE_CHECKING:
    from .conversation_state_v2 import ConversationState, FollowUpDelta


logger = logging.getLogger(__name__)


class GeographicIdentityDecision(BaseModel):
    outcome: Literal["select", "clarify"]
    entity_ids: list[str] = Field(min_length=1)
    reason: str = ""


def _current_identity_ids(
    decision: Optional[GeographicIdentityDecision],
    entities: dict[str, Any],
    peer_ids: set[str],
) -> list[str]:
    if decision is None:
        return []
    returned = list(dict.fromkeys(decision.entity_ids))
    choices = [value for value in returned if value in entities]
    extras = set(returned) - set(choices)
    if decision.outcome == "select":
        return choices if len(choices) == 1 and extras <= peer_ids else []
    return choices if len(choices) >= 2 and not extras else []


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


def _names_scope_type(text: str, name: str, scope_type: str) -> bool:
    if not scope_type:
        return False
    name, scope_type = re.escape(name), re.escape(scope_type)
    return bool(
        re.search(rf"\b{scope_type}\s+(?:of\s+)?{name}(?![a-z0-9])", text)
        or re.search(rf"(?<![a-z0-9]){name}\s*\(?\s*{scope_type}\b", text)
    )


def _qualified_identity(query: str, intent: ParsedIntent, collision: dict[str, Any]) -> tuple[Optional[str], bool]:
    """Corroborate explicit scope syntax with the parser and registry.

    Scope type words do not choose an arbitrary place: they must refer to this
    registered name, and a subdivision also needs its named, parsed parent.
    """
    text = normalize_geographic_name(query)
    params = intent.parameters or {}
    parsed = list(params.get("countries") or []) or ([params["country"]] if params.get("country") else [])
    parsed_codes = {CountryResolver.normalize(str(value)) for value in parsed}
    mentioned_codes = set(CountryResolver.detect_all_countries_in_query(query))
    qualified = bool(intent.subnationalRegion)
    choices = []
    for entity in collision["entities"]:
        kind = "country" if entity["kind"] == "country" else normalize_geographic_name(entity.get("type", ""))
        if not _names_scope_type(text, collision["name"], kind):
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


def preserves_verified_country_collection(state: ConversationState, delta: Optional[FollowUpDelta]) -> bool:
    """Recognize a structural add/remove within an executed national comparison."""
    if not delta or not (delta.added_countries or delta.removed_countries):
        return False
    if delta.model_fields_set - {"raw_query", "delta_type", "added_countries", "removed_countries"}:
        return False
    if state.subnational_region or state.decomposition or state.dimensions:
        return False
    requested = {CountryResolver.normalize(value) for value in state.countries or []}
    served = {
        CountryResolver.normalize(member.country) if member.country else None
        for member in state.active_answer_members or []
    }
    return len(requested) >= 2 and None not in requested and requested == served


def apply_geographic_entity(
    intent: ParsedIntent, entity: dict[str, Any], collision: dict[str, Any],
    *, source_query: str = "",
) -> None:
    """Bind a chosen identity without treating its name as another country."""
    params = dict(intent.parameters or {})
    competing = {entry["country"] for entry in collision["entities"]}
    previous = list(params.get("countries") or []) or ([params["country"]] if params.get("country") else [])
    independent_countries = set()
    if entity["kind"] == "country":
        source_text = normalize_geographic_name(source_query)
        if not any(
            _names_scope_type(source_text, collision["name"], normalize_geographic_name(entry.get("type", "")))
            for entry in collision["entities"] if entry["kind"] == "subdivision"
        ):
            independent_countries = set(
                CountryResolver.detect_all_countries_in_query(country_evidence_text(source_query))
            )
    others = []
    for value in previous:
        code = CountryResolver.normalize(str(value)) or str(value).upper()
        if code != entity["country"] and (code not in competing or code in independent_countries):
            others.append(value)
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
    independently_named = CountryResolver.detect_all_countries_in_query(country_evidence_text(query))
    for collision in geographic_name_collisions(query):
        entities = {entity["id"]: entity for entity in collision["entities"]}
        existing = (intent.parameters or {}).get("__geographic_entity")
        if existing in entities:
            apply_geographic_entity(intent, entities[existing], collision, source_query=query)
            continue
        qualified_id, _ = _qualified_identity(query, intent, collision)
        if qualified_id:
            apply_geographic_entity(intent, entities[qualified_id], collision, source_query=query)
            continue
        timeout = bounded_judge_timeout()
        if timeout is None:
            return _unavailable(conversation_id, intent, processing_steps)
        context = {
            key: getattr(previous_state, key, None)
            for key in ("country", "countries", "subnational_region", "original_query")
        } if previous_state is not None else None
        params = intent.parameters or {}
        parsed_countries = params.get("countries")
        parsed_codes = {
            CountryResolver.normalize(str(value))
            for value in parsed_countries if value
        } if isinstance(parsed_countries, list) else set()
        country_registry = geographic_registry()["countries"]
        peer_ids = {
            f"country:{code}" for code in independently_named if code in parsed_codes and code in country_registry
        } - set(entities)
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
                "subdivision name into an additional country. When an explicitly requested list compares "
                "peer countries, preserve that national scope unless the user singled out a subdivision. "
                "Treat named parent countries used to qualify a subdivision as context, not peer countries. "
                "Answer only for the current shared name. Country peers are context and must not appear "
                "in entity_ids. Return only the supplied candidate IDs for the current name. "
                "Required JSON keys: outcome ('select' or 'clarify'), entity_ids (nonempty list of exact "
                "registry IDs), reason. A clarify decision lists at least two distinct identities."
            ),
            user_prompt=json.dumps({
                "query": query,
                "shared_name": collision["name"],
                "parsed_scope": {
                    "country": params.get("country"),
                    "countries": parsed_countries,
                    "subnational_region": intent.subnationalRegion,
                },
                "countries_named_independently": independently_named,
                "previous_scope": context,
                "candidates": list(entities.values()),
            }, ensure_ascii=False),
            response_model=GeographicIdentityDecision,
            answer_tokens=220,
            label="Geographic identity judge",
            timeout_s=timeout,
        )
        ids = _current_identity_ids(decision, entities, peer_ids)
        if not ids:
            logger.info("Geographic identity unresolved: outcome=%s valid_ids=%d",
                        getattr(decision, "outcome", None),
                        sum(value in entities for value in getattr(decision, "entity_ids", [])))
            qualified_id, qualified = _qualified_identity(query, intent, collision)
            if qualified_id:
                apply_geographic_entity(intent, entities[qualified_id], collision, source_query=query)
                continue
            if qualified:
                return _unavailable(conversation_id, intent, processing_steps)
            # The registry itself proves competing extents. Asking which one
            # the user intends does not guess a semantic winner or a provider.
            ids = list(entities)
            decision = GeographicIdentityDecision(outcome="clarify", entity_ids=ids)
        if decision.outcome == "select" and len(ids) == 1:
            apply_geographic_entity(intent, entities[ids[0]], collision, source_query=query)
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
