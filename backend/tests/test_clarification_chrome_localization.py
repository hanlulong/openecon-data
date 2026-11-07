"""Clarification-menu chrome renders in the user's language (mechanism tests).

The chrome of every indicator-clarification menu (headers, "please choose"
prompts, reply hints, retry notices, "current match" labels) is rendered
through ONE chokepoint (``indicator_clarification._chrome``) keyed on ONE
language resolver (``clarification_language``) that reads only
already-detected tags: the pending payload's language (reply turns) and
``ParsedIntent.language`` (build turns). These tests pin:

- catalog completeness: every ``clarify_*`` id has en + zh with IDENTICAL
  placeholder sets (zh can never silently fall back to en or drop a value);
- byte-identity: every call site's inline English default equals the en
  catalog template, and English menus are unchanged;
- zh rendering across builders, with option lines ("N. <provider title>"),
  executed option VALUES, and series ids left untouched;
- reply-turn language: a bare-digit reply carries no language signal, so the
  re-prompt must render in the language of the turn that BUILT the menu.
"""
from __future__ import annotations

import ast
import string
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from backend.models import ClarificationOption, ParsedIntent, QueryResponse
from backend.services import indicator_clarification as ic
from backend.services.cache import cache_service
from backend.services.conversation import conversation_manager
from backend.services.query import QueryService
from backend.services.user_messages import _MESSAGES, get_message
from backend.tests.utils import run

_CLARIFICATION_SRC = Path(ic.__file__).read_text(encoding="utf-8")
_ZH_HINT_PREFIX = "请回复选项编号"
_EN_HINT_PREFIX = "Reply with the option number"


def _placeholders(template: str) -> set[str]:
    return {field for _, field, _, _ in string.Formatter().parse(template) if field}


def _chrome_call_sites() -> list[tuple[str, str]]:
    """(message_id, inline English default) for every _chrome(...) call."""
    sites = []
    for node in ast.walk(ast.parse(_CLARIFICATION_SRC)):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "_chrome":
            sites.append((node.args[0].value, node.args[2].value))
    return sites


class ChromeCatalogContractTests(unittest.TestCase):
    def test_every_clarify_id_has_en_and_zh_with_identical_placeholders(self) -> None:
        clarify_ids = [mid for mid in _MESSAGES if mid.startswith("clarify_")]
        self.assertGreater(len(clarify_ids), 20)
        for mid in clarify_ids:
            entry = _MESSAGES[mid]
            self.assertIn("en", entry, mid)
            self.assertIn("zh", entry, mid)
            self.assertTrue(entry["zh"].strip(), mid)
            self.assertNotEqual(entry["en"], entry["zh"], mid)
            self.assertEqual(_placeholders(entry["en"]), _placeholders(entry["zh"]), mid)

    def test_every_call_site_default_is_the_byte_identical_english_template(self) -> None:
        sites = _chrome_call_sites()
        self.assertGreater(len(sites), 40)
        for mid, default in sites:
            self.assertIn(mid, _MESSAGES, f"_chrome references unknown id {mid!r}")
            self.assertEqual(_MESSAGES[mid]["en"], default, mid)

    def test_every_option_reply_hint_uses_the_fixed_per_language_prefix(self) -> None:
        # The web UI strips exactly these prefixes next to option buttons
        # (ChatPage.tsx sanitizeClarificationContent).
        for mid in (
            "clarify_reply_hint",
            "clarify_multi_family_reply_hint",
            "clarify_group_scope_reply_hint",
            "clarify_decomp_reply_hint",
        ):
            self.assertTrue(_MESSAGES[mid]["en"].startswith(_EN_HINT_PREFIX), mid)
            self.assertTrue(_MESSAGES[mid]["zh"].startswith(_ZH_HINT_PREFIX), mid)

    def test_web_ui_sanitizer_strips_every_language_hint_prefix(self) -> None:
        # The hint prefixes live in the backend catalog; the regex that hides
        # them next to option buttons lives in ChatPage.tsx. Pin the two
        # together so adding a language cannot leave a duplicate hint line.
        chat_page = (
            Path(__file__).resolve().parents[2]
            / "packages" / "frontend" / "src" / "components" / "ChatPage.tsx"
        ).read_text(encoding="utf-8")
        start = chat_page.index("function sanitizeClarificationContent")
        sanitizer = chat_page[start:start + 2000]
        languages = {lang for mid in _MESSAGES if mid.startswith("clarify_") for lang in _MESSAGES[mid]}
        self.assertEqual(languages, {"en", "zh"}, "new language: add its hint prefix here and in ChatPage.tsx")
        self.assertIn(_EN_HINT_PREFIX.lower(), sanitizer)
        self.assertIn(_ZH_HINT_PREFIX, sanitizer)

    def test_chrome_falls_back_to_inline_default_on_catalog_miss(self) -> None:
        self.assertEqual(
            ic._chrome("clarify_does_not_exist", "zh", "Fallback {x}.", x="ok"),
            "Fallback ok.",
        )

    def test_language_resolver_precedence_and_no_text_detection(self) -> None:
        zh_intent = ParsedIntent(apiProvider="FRED", indicators=["x"], clarificationNeeded=False, language="zh")
        en_intent = ParsedIntent(apiProvider="FRED", indicators=["x"], clarificationNeeded=False, language="en")
        # Pending payload language wins over the reply turn's intent.
        self.assertEqual(ic.clarification_language(en_intent, {"language": "zh"}), "zh")
        # Older payloads without a top-level key fall back to the stored intent.
        self.assertEqual(
            ic.clarification_language(None, {"intent": zh_intent.model_dump()}), "zh"
        )
        self.assertEqual(ic.clarification_language(zh_intent), "zh")
        self.assertIsNone(ic.clarification_language(None))
        # A Chinese query with no language tag is NOT detected from its text.
        untagged = ParsedIntent(
            apiProvider="FRED", indicators=["x"], clarificationNeeded=False, language=None, originalQuery="中国CPI"
        )
        self.assertIsNone(ic.clarification_language(untagged))

    def test_zh_region_subtag_normalizes(self) -> None:
        self.assertEqual(
            ic._chrome("clarify_choose_one", "zh-CN", "Please choose one option:"),
            get_message("clarify_choose_one", "zh"),
        )


class ClarificationBuilderLocalizationTests(unittest.TestCase):
    OPTIONS = [
        "[IMF] General government gross debt (% of GDP) (GGXWDG_NGDP)",
        "[WorldBank] Central government debt, total (% of GDP) (GC.DOD.TOTL.GD.ZS)",
    ]

    def setUp(self) -> None:
        cache_service.clear()
        self.service = QueryService(openrouter_key="test", fred_key="fred", comtrade_key="demo")
        self.service.settings.use_outcome_decision_stage = False
        self.service.settings.use_post_fetch_semantic_judge = False
        self.service.settings.use_staged_state_commit = False

    def _intent(self, language: str | None, **overrides) -> ParsedIntent:
        fields = dict(
            apiProvider="BIS",
            indicators=["GDP to Debt Ratio"],
            parameters={"country": "CN"},
            clarificationNeeded=False,
            originalQuery="gdp to debt ratio in china",
            language=language,
        )
        fields.update(overrides)
        return ParsedIntent(**fields)

    def _no_data_menu(self, language: str | None, conv_name: str):
        conv_id = conversation_manager.get_or_create(conv_name)
        conversation_manager.clear_pending_indicator_options(conv_id)
        intent = self._intent(language)
        with patch.object(self.service, "_collect_indicator_choice_options", return_value=self.OPTIONS):
            response = self.service._build_no_data_indicator_clarification(  # pylint: disable=protected-access
                conversation_id=conv_id,
                query=intent.originalQuery or "",
                intent=intent,
                processing_steps=None,
            )
        assert response is not None
        return conv_id, response

    def _option_lines(self, response) -> list[str]:
        return [
            line for line in (response.clarificationQuestions or [])
            if line[:1].isdigit() and ". " in line
        ]

    def test_no_data_menu_english_is_byte_identical(self) -> None:
        _, response = self._no_data_menu("en", "conv-chrome-en")
        self.assertEqual(
            response.clarificationQuestions,
            [
                "I couldn't find data with the current indicator selection.",
                "Please choose the indicator you intended:",
                "1. " + self.OPTIONS[0],
                "2. " + self.OPTIONS[1],
                "Reply with the option number (for example, 1) or the exact indicator text you want.",
            ],
        )

    def test_no_data_menu_zh_localizes_chrome_but_not_options(self) -> None:
        conv_id, zh = self._no_data_menu("zh", "conv-chrome-zh")
        _, en = self._no_data_menu(None, "conv-chrome-none")
        self.assertEqual(
            zh.clarificationQuestions[:2],
            [get_message("clarify_no_data_header", "zh"), get_message("clarify_choose_intended", "zh")],
        )
        self.assertEqual(zh.clarificationQuestions[-1], get_message("clarify_reply_hint", "zh"))
        # Option lines, structured options and stored options are untouched.
        self.assertEqual(self._option_lines(zh), self._option_lines(en))
        self.assertEqual(
            [o.model_dump() for o in zh.clarificationOptions or []],
            [o.model_dump() for o in en.clarificationOptions or []],
        )
        pending = conversation_manager.get_pending_indicator_options(conv_id)
        assert pending is not None
        self.assertEqual(pending["language"], "zh")
        self.assertEqual(pending["question_lines"], zh.clarificationQuestions)
        # No English chrome survives in a zh menu.
        for line in zh.clarificationQuestions:
            if line not in self._option_lines(zh):
                self.assertNotIn("Please", line)
                self.assertNotIn("Reply", line)

    def test_prefetch_menu_zh_has_no_routing_jargon(self) -> None:
        conv_id = conversation_manager.get_or_create("conv-chrome-prefetch-zh")
        conversation_manager.clear_pending_indicator_options(conv_id)
        intent = self._intent("zh")
        with patch.object(self.service, "_collect_indicator_choice_options", return_value=self.OPTIONS), \
             patch.object(self.service, "_filter_viable_indicator_choice_options", AsyncMock(return_value=self.OPTIONS)), \
             patch("backend.services.indicator_selector.IndicatorSelector.select", AsyncMock(return_value=None)):
            response = run(
                self.service._build_prefetch_indicator_choice_clarification(  # pylint: disable=protected-access
                    conversation_id=conv_id,
                    query="gdp to debt ratio in china",
                    intent=intent,
                    explicit_provider=None,
                    is_multi_indicator=False,
                    processing_steps=None,
                )
            )
        assert response is not None
        lines = response.clarificationQuestions or []
        self.assertEqual(lines[0], get_message("clarify_prefetch_header", "zh"))
        self.assertEqual(
            lines[1],
            get_message(
                "clarify_current_routed_match",
                "zh",
                label=get_message("clarify_current_unresolved", "zh", provider="BIS"),
            ),
        )
        self.assertNotIn("routing guess", "\n".join(lines))
        self.assertTrue(lines[-1].startswith(_ZH_HINT_PREFIX))

    def test_reply_turn_reprompt_uses_menu_language_not_reply_language(self) -> None:
        conv_id, _ = self._no_data_menu("zh", "conv-chrome-reply-zh")
        # "9" is out of range; the reply turn itself has no language signal.
        response = run(
            self.service._try_resolve_pending_indicator_choice(  # pylint: disable=protected-access
                query="9", conversation_id=conv_id, tracker=None,
            )
        )
        assert response is not None
        self.assertTrue(response.clarificationNeeded)
        self.assertEqual(response.message, get_message("clarify_invalid_option_number", "zh"))
        # The replayed menu stays in zh.
        self.assertEqual(response.clarificationQuestions[0], get_message("clarify_no_data_header", "zh"))

    def test_failed_option_reprompt_stays_in_menu_language(self) -> None:
        conv_id, _ = self._no_data_menu("zh", "conv-chrome-failed-zh")
        with patch.object(self.service, "_fetch_data", return_value=[]):
            response = run(
                self.service._try_resolve_pending_indicator_choice(  # pylint: disable=protected-access
                    query="1", conversation_id=conv_id, tracker=None,
                )
            )
        assert response is not None
        self.assertEqual(response.message, get_message("clarify_option_failed", "zh"))
        self.assertEqual(response.clarificationQuestions[0], get_message("clarify_no_data_header", "zh"))

    def test_english_reply_turn_messages_unchanged(self) -> None:
        conv_id, _ = self._no_data_menu("en", "conv-chrome-reply-en")
        response = run(
            self.service._try_resolve_pending_indicator_choice(  # pylint: disable=protected-access
                query="9", conversation_id=conv_id, tracker=None,
            )
        )
        assert response is not None
        self.assertEqual(response.message, "Please choose one of the listed option numbers.")

    def test_group_scope_zh_localizes_labels_but_not_executed_values(self) -> None:
        def build(language):
            conv_id = conversation_manager.get_or_create(f"conv-chrome-group-{language}")
            conversation_manager.clear_pending_semantic_clarification(conv_id)
            intent = ParsedIntent(
                apiProvider="IMF",
                indicators=["economic data"],
                parameters={},
                clarificationNeeded=False,
                originalQuery="economic data for G20",
                language=language,
            )
            response = self.service._build_group_scope_clarification(  # pylint: disable=protected-access
                conversation_id=conv_id,
                query="economic data for G20",
                intent=intent,
                is_multi_indicator=False,
                processing_steps=None,
            )
            return conv_id, response

        conv_zh, zh = build("zh")
        _, en = build("en")
        assert zh is not None and en is not None
        self.assertEqual(
            [o.label for o in zh.clarificationOptions or []],
            [
                get_message("clarify_group_scope_option_compare", "zh"),
                get_message("clarify_group_scope_option_group_value", "zh"),
            ],
        )
        self.assertEqual(
            [o.value for o in zh.clarificationOptions or []],
            [o.value for o in en.clarificationOptions or []],
        )
        self.assertEqual(
            [o.label for o in en.clarificationOptions or []],
            ["compare member countries", "one overall group value (aggregate/average if available)"],
        )
        self.assertTrue(zh.clarificationQuestions[-1].startswith(_ZH_HINT_PREFIX))
        self.assertEqual(en.clarificationQuestions[-1], "Reply with the option number, or type a different scope.")
        pending = conversation_manager.get_pending_semantic_clarification(conv_zh)
        assert pending is not None
        self.assertEqual(pending["language"], "zh")

    def test_decomposition_and_low_confidence_zh(self) -> None:
        intent = ParsedIntent(
            apiProvider="WORLDBANK",
            indicators=["inflation"],
            parameters={"country": "FR"},
            clarificationNeeded=False,
            originalQuery="France inflation by category",
            needsDecomposition=True,
            decompositionType="category",
            language="zh",
        )
        conv_id = conversation_manager.get_or_create("conv-chrome-decomp-zh")
        decomp = ic.build_missing_decomposition_entities_clarification(
            conversation_id=conv_id, query=intent.originalQuery, intent=intent,
        )
        assert decomp is not None
        noun = get_message("clarify_decomp_noun_categories", "zh")
        self.assertEqual(
            decomp.clarificationQuestions[0],
            get_message(
                "clarify_decomp_missing",
                "zh",
                category_label=get_message("clarify_decomp_noun_category", "zh"),
                category_plural=noun,
            ),
        )
        # Executed values stay English rewritten queries.
        self.assertEqual(decomp.clarificationOptions[1].value, "France inflation")

        intent.language = "en"
        en_decomp = ic.build_missing_decomposition_entities_clarification(
            conversation_id=conv_id, query=intent.originalQuery, intent=intent,
        )
        assert en_decomp is not None
        self.assertEqual(
            en_decomp.clarificationQuestions[0],
            "Your query asks for a category breakdown, but no specific categories were named.",
        )
        self.assertEqual(
            [o.value for o in decomp.clarificationOptions], [o.value for o in en_decomp.clarificationOptions]
        )

        low_zh = ic.build_low_confidence_intent_response("conv-x", self._intent("zh"), None)
        reason = get_message("clarify_low_confidence_reason_default", "zh")
        self.assertEqual(
            low_zh.clarificationQuestions[0],
            get_message("clarify_low_confidence_uncertain", "zh", reason=reason),
        )
        self.assertEqual(low_zh.message, get_message("clarify_low_confidence_message", "zh", reason=reason))
        low_en = ic.build_low_confidence_intent_response("conv-x", self._intent(None), "vague")
        self.assertEqual(
            low_en.message,
            "**Uncertain Query**\nvague\n\nPlease provide more details or use Pro Mode for better results.",
        )


class SemanticReplyResolutionIsLanguageInvariantTests(unittest.TestCase):
    """Localized option LABELS must never change what a reply executes.

    Group-scope and decomposition option labels are system text rendered in
    the user's language; reply resolution keys on the payload's structural
    ``option_roles`` and the executed option VALUE instead of label wording.
    """

    def setUp(self) -> None:
        cache_service.clear()
        self.service = QueryService(openrouter_key="test", fred_key="fred", comtrade_key="demo")
        self.service.settings.use_outcome_decision_stage = False
        self.service.settings.use_post_fetch_semantic_judge = False
        self.service.settings.use_staged_state_commit = False

    def _group_menu(self, language: str | None, conv_name: str) -> str:
        conv_id = conversation_manager.get_or_create(conv_name)
        conversation_manager.clear_pending_semantic_clarification(conv_id)
        intent = ParsedIntent(
            apiProvider="IMF",
            indicators=["economic data"],
            parameters={},
            clarificationNeeded=False,
            originalQuery="economic data for G20",
            language=language,
        )
        response = self.service._build_group_scope_clarification(  # pylint: disable=protected-access
            conversation_id=conv_id,
            query="economic data for G20",
            intent=intent,
            is_multi_indicator=False,
            processing_steps=None,
        )
        assert response is not None
        return conv_id

    def _resolve(self, conv_id: str, reply: str):
        pending = conversation_manager.get_pending_semantic_clarification(conv_id)
        assert pending is not None
        options = [ClarificationOption.model_validate(o) for o in pending["options"]]
        selected = ic.match_structured_clarification_option(reply, options)
        assert selected is not None
        return self.service._build_intent_from_semantic_clarification(  # pylint: disable=protected-access
            pending=pending, selected_option=selected, refined_query=selected.value,
        )

    @staticmethod
    def _decision_view(intent: ParsedIntent) -> dict:
        return {
            "provider": intent.apiProvider,
            "indicators": intent.indicators,
            "countries": intent.parameters.get("countries"),
            "country": intent.parameters.get("country"),
            "originalQuery": intent.originalQuery,
        }

    def test_compare_members_reply_builds_the_same_intent_in_every_language(self) -> None:
        built = {}
        for language in (None, "en", "zh"):
            conv_id = self._group_menu(language, f"conv-reply-group-{language}")
            pending = conversation_manager.get_pending_semantic_clarification(conv_id)
            assert pending is not None
            # Each persisted role is exactly the scope that produced the value.
            for option in pending["options"]:
                role = pending["option_roles"][option["id"]]
                self.assertEqual(
                    option["value"],
                    ic.rewrite_group_scope_query(pending["original_query"], pending["region"], role),
                )
            intent = self._resolve(conv_id, "1")
            self.assertIsNotNone(intent, language)
            assert intent is not None
            built[language] = intent
        en = self._decision_view(built["en"])
        self.assertGreaterEqual(len(en["countries"] or []), 10)
        self.assertEqual(en["indicators"], ["economic data"])
        self.assertEqual(self._decision_view(built["zh"]), en)
        self.assertEqual(self._decision_view(built[None]), en)
        # The built intent keeps the menu's language for any later menu.
        self.assertEqual(built["zh"].language, "zh")
        self.assertIsNone(built[None].language)

    def test_group_value_reply_still_defers_in_every_language(self) -> None:
        for language in ("en", "zh"):
            conv_id = self._group_menu(language, f"conv-reply-group-value-{language}")
            self.assertIsNone(self._resolve(conv_id, "2"), language)

    def test_legacy_payload_without_roles_keeps_its_english_label_contract(self) -> None:
        legacy = {
            "kind": "group_scope",
            "original_query": "employment rate in G20",
            "options": [],
        }
        compare = ClarificationOption(
            id="1", label="compare member countries", value="employment rate across G20 member countries",
        )
        group_value = ClarificationOption(
            id="2",
            label="one overall group value (aggregate/average if available)",
            value="employment rate for the G20 group as a whole",
        )
        self.assertIsNotNone(
            self.service._build_intent_from_semantic_clarification(  # pylint: disable=protected-access
                pending=legacy, selected_option=compare, refined_query=compare.value,
            )
        )
        self.assertIsNone(
            self.service._build_intent_from_semantic_clarification(  # pylint: disable=protected-access
                pending=legacy, selected_option=group_value, refined_query=group_value.value,
            )
        )

    def test_decomposition_reply_indicator_comes_from_the_value_not_the_label(self) -> None:
        built = {}
        for language in ("en", "zh"):
            conv_id = conversation_manager.get_or_create(f"conv-reply-decomp-{language}")
            conversation_manager.clear_pending_semantic_clarification(conv_id)
            intent = ParsedIntent(
                apiProvider="WORLDBANK",
                indicators=["inflation"],
                parameters={"country": "FR"},
                clarificationNeeded=False,
                originalQuery="France inflation by category",
                needsDecomposition=True,
                decompositionType="category",
                language=language,
            )
            assert ic.build_missing_decomposition_entities_clarification(
                conversation_id=conv_id, query=intent.originalQuery, intent=intent,
            ) is not None
            resolved = self._resolve(conv_id, "2")
            assert resolved is not None
            built[language] = resolved
        self.assertEqual(self._decision_view(built["zh"]), self._decision_view(built["en"]))
        indicator = built["en"].indicators[0]
        self.assertNotIn("overall series", indicator)
        self.assertIn("inflation", indicator.lower())
        self.assertEqual(built["zh"].language, "zh")

    def test_semantic_metric_options_still_use_their_label_as_the_metric(self) -> None:
        # LLM-derived semantic options carry no option_roles: their label IS
        # the metric text (unchanged contract).
        pending = {"kind": "semantic_metric", "original_query": "employment in Canada"}
        option = ClarificationOption(id="1", label="number employed", value="number employed in Canada")
        intent = self.service._build_intent_from_semantic_clarification(  # pylint: disable=protected-access
            pending=pending, selected_option=option, refined_query=option.value,
        )
        assert intent is not None
        self.assertEqual(intent.indicators, ["number employed"])

    def test_zh_bare_number_reply_executes_the_rewritten_compare_query(self) -> None:
        for language in ("zh", "en"):
            conv_id = self._group_menu(language, f"conv-reply-process-{language}")
            pending = conversation_manager.get_pending_semantic_clarification(conv_id)
            assert pending is not None
            expected_value = pending["options"][0]["value"]
            sentinel = QueryResponse(conversationId=conv_id, clarificationNeeded=False)
            execute = AsyncMock(return_value=sentinel)
            with patch.object(self.service, "_execute_resolved_intent", execute):
                response = run(self.service.process_query("1", conversation_id=conv_id))
            self.assertIs(response, sentinel, language)
            execute.assert_awaited_once()
            kwargs = execute.await_args.kwargs
            self.assertEqual(kwargs["query"], expected_value, language)
            self.assertGreaterEqual(len(kwargs["intent"].parameters.get("countries") or []), 10, language)
            self.assertEqual(kwargs["intent"].language, language)
            self.assertIsNone(conversation_manager.get_pending_semantic_clarification(conv_id))


if __name__ == "__main__":
    unittest.main()
