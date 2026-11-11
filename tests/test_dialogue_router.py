import unittest

from agentforge.answer_contract import (
    build_general_contract, verify_generated_answer, verify_hybrid_answer,
)
from agentforge.errors import CitationValidationError, ProviderError
from agentforge.dialogue_router import DialogueMode, plan_dialogue
from agentforge.planner import parse_conversation_plan
from agentforge.evidence_evaluator import assess_evidence
from agentforge.evidence_roles import required_evidence_roles
from agentforge.models import Answer, BuiltContext, Evidence, SearchResult
from types import SimpleNamespace
from agentforge.model_registry import ModelDescriptor
from agentforge.providers.deepseek import DeepSeekClient, DeepSeekCompletion, DeepSeekProvider
from agentforge.providers.llama_server import LlamaServerProvider
from unittest.mock import Mock


def _plan_json(intent, *, agent_execution=False):
    if intent == "general":
        tasks = [{"task_id": "g1", "task_type": "general", "question": "Explain attention",
                  "requires_retrieval": False, "search_queries": [], "dependencies": []}]
        repository = False
    elif intent == "repository":
        tasks = [{"task_id": "r1", "task_type": "repository", "question": "Find symbol",
                  "requires_retrieval": True, "search_queries": ["symbol"], "dependencies": []}]
        repository = True
    else:
        tasks = [
            {"task_id": "g1", "task_type": "general", "question": "Explain FIFO",
             "requires_retrieval": False, "search_queries": [], "dependencies": []},
            {"task_id": "r1", "task_type": "repository", "question": "Find implementation",
             "requires_retrieval": True, "search_queries": ["FIFO implementation"],
             "dependencies": ["g1"]},
        ]
        repository = True
    if agent_execution:
        tasks.append({"task_id": "tool1", "task_type": "tool",
                      "question": "Inspect the requested implementation with an authorized tool",
                      "requires_retrieval": False, "search_queries": [], "dependencies": []})
    import json
    return json.dumps({"intent": intent, "tasks": tasks,
                       "requires_repository": repository,
                       "requires_external_data": False,
                       "requires_agent_execution": agent_execution,
                       "answer_strategy": "compose" if intent == "hybrid" else "direct",
                       "clarification_required": False, "confidence": 0.9,
                       "reason": "Validated synthetic plan."})


class DialogueRouterTests(unittest.TestCase):
    def test_explicit_modes_are_not_reinterpreted(self):
        for mode in ("general", "repository"):
            with self.subTest(mode=mode):
                plan = plan_dialogue(mode, "Explain this function and the general concept")
                self.assertEqual(mode, plan.mode.value)
        hybrid = parse_conversation_plan(_plan_json("hybrid"), requested_mode="hybrid")
        self.assertEqual("hybrid", plan_dialogue(
            "hybrid", "Explain this function and the general concept",
            planner_result=hybrid,
        ).mode.value)

    def test_auto_uses_validated_planner_result_for_general_question(self):
        planned = parse_conversation_plan(_plan_json("general"), requested_mode="auto")
        plan = plan_dialogue("auto", "Explain the transformer attention principle",
                             planner_result=planned)
        self.assertEqual(DialogueMode.GENERAL, plan.mode)
        self.assertFalse(plan.use_repository)
        self.assertTrue(plan.use_general_knowledge)

    def test_auto_uses_validated_planner_result_for_repository_question(self):
        planned = parse_conversation_plan(_plan_json("repository"), requested_mode="auto")
        plan = plan_dialogue("auto", "Where is function refresh_access_token defined?",
                             planner_result=planned)
        self.assertEqual(DialogueMode.REPOSITORY, plan.mode)
        self.assertTrue(plan.use_repository)
        self.assertFalse(plan.use_general_knowledge)

    def test_auto_projects_retrieval_independently_from_legacy_answer_intent(self):
        import json
        payload = json.loads(_plan_json("repository"))
        payload["intent"] = "general"
        planned = parse_conversation_plan(json.dumps(payload), requested_mode="auto")

        plan = plan_dialogue("auto", "Explain the concept and inspect the current implementation",
                             planner_result=planned)

        self.assertEqual(DialogueMode.GENERAL, plan.mode)
        self.assertTrue(plan.use_repository)
        self.assertFalse(plan.requires_agent_execution)

    def test_tool_execution_flag_does_not_imply_repository_retrieval(self):
        planned = parse_conversation_plan(
            _plan_json("general", agent_execution=True), requested_mode="auto",
        )

        plan = plan_dialogue("auto", "Use the authorized synthetic tool.",
                             planner_result=planned)

        self.assertFalse(plan.use_repository)
        self.assertTrue(plan.requires_agent_execution)

    def test_auto_preserves_validated_agent_execution_decision(self):
        planned = parse_conversation_plan(
            _plan_json("repository", agent_execution=True), requested_mode="auto",
        )
        plan = plan_dialogue("auto", "Inspect the implementation and its callers.",
                             planner_result=planned)
        self.assertTrue(plan.requires_agent_execution)
        self.assertTrue(plan.use_repository)

    def test_auto_uses_validated_planner_result_for_hybrid_question(self):
        planned = parse_conversation_plan(_plan_json("hybrid"), requested_mode="auto")
        plan = plan_dialogue(
            "auto", "Explain the general FIFO principle and how this project implements it in code.",
            planner_result=planned,
        )
        self.assertEqual(DialogueMode.HYBRID, plan.mode)
        self.assertTrue(plan.use_repository)
        self.assertTrue(plan.use_general_knowledge)

    def test_auto_requires_a_validated_plan_and_does_not_lexically_fallback(self):
        with self.assertRaisesRegex(ValueError, "validated LLM Planner"):
            plan_dialogue("auto", "Explain transformer attention")

    def test_invalid_mode_and_empty_question_are_rejected(self):
        with self.assertRaises(ValueError):
            plan_dialogue("external", "question")
        with self.assertRaises(ValueError):
            plan_dialogue("auto", " ")


class DialogueContractTests(unittest.TestCase):
    def _context(self, evidence=True):
        if not evidence:
            return BuiltContext("How does it work?", (), "", 0, 1000)
        source = SearchResult(
            "repo-id", "src/fifo.py", 1, 3,
            "def pop():\n    return items.pop(0)\n", "a" * 64, 1.0,
            ("definition",),
        )
        return BuiltContext("How does it work?", (Evidence("E1", source),),
                            "[E1] [[src/fifo.py:1-3]]\n", 20, 1000)

    def test_general_answer_is_uncited_and_keeps_limited_semantics(self):
        contract = build_general_contract(Answer("FIFO keeps insertion order.", (), False))
        self.assertEqual("general", contract.route)
        self.assertEqual("limited", contract.status)
        self.assertEqual("general", contract.claims[0].origin)
        self.assertEqual((), contract.claims[0].citations)

    def test_general_contract_rejects_model_invented_repository_citation(self):
        for text in ("Answer with source [E1].", "See project/module.py:12-18."):
            with self.subTest(text=text), self.assertRaises(CitationValidationError):
                build_general_contract(Answer(text, (), False))

    def test_repository_contract_rejects_invalid_id_after_provider_returns_text(self):
        with self.assertRaises(CitationValidationError):
            verify_generated_answer(
                Answer("Unsupported source [E99].", (), False), self._context(),
                route="repository", route_confidence="high",
            )

    def test_repository_contract_accepts_id_but_rejects_app_supplied_location_text(self):
        accepted = verify_generated_answer(
            Answer("The function removes the first item [E1].", (), False), self._context(),
            route="repository", route_confidence="high",
        )
        self.assertEqual(("src/fifo.py:1-3",), accepted.claims[0].citations)
        with self.assertRaises(CitationValidationError) as caught:
            verify_generated_answer(
                Answer("The function removes the first item [E1] (src/fifo.py:1-3).", (), False),
                self._context(), route="repository", route_confidence="high",
            )
        self.assertEqual("model_supplied_path_location_forbidden", caught.exception.code)

    def test_hybrid_contract_separates_general_and_cited_repository_claim(self):
        answer = Answer(
            "General knowledge:\nFIFO removes the earliest item first.\n"
            "Repository evidence:\nThe implementation removes index zero [E1].",
            ("src/fifo.py:1-3",), False,
        )
        contract = verify_hybrid_answer(answer, self._context())
        self.assertEqual("hybrid", contract.route)
        self.assertEqual(["general", "repository"], [claim.origin for claim in contract.claims])
        self.assertEqual((), contract.claims[0].citations)
        self.assertEqual(("src/fifo.py:1-3",), contract.claims[1].citations)

    def test_hybrid_allows_general_answer_and_explicit_missing_repository_part(self):
        answer = Answer(
            "General knowledge:\nFIFO removes the earliest item first.\n"
            "Repository evidence:\nI cannot confirm this from repository evidence.",
            (), False,
        )
        contract = verify_hybrid_answer(answer, self._context(evidence=False))
        self.assertEqual("limited", contract.status)
        self.assertEqual("unknown", contract.claims[1].kind)
        self.assertEqual("general", contract.claims[0].origin)

    def test_hybrid_rejects_missing_sections_or_uncited_repository_facts(self):
        with self.assertRaises(ProviderError):
            verify_hybrid_answer(Answer("FIFO works this way.", (), False), self._context())
        answer = Answer(
            "General knowledge:\nFIFO removes the earliest item first.\n"
            "Repository evidence:\nThe implementation uses a list.", (), False,
        )
        with self.assertRaises(ProviderError):
            verify_hybrid_answer(answer, self._context())

    def test_hybrid_rejects_repository_citation_in_general_section(self):
        answer = Answer(
            "General knowledge:\nFIFO removes the earliest item [E1].\n"
            "Repository evidence:\nThe implementation removes index zero [E1].",
            ("src/fifo.py:1-3",), False,
        )
        with self.assertRaises(CitationValidationError):
            verify_hybrid_answer(answer, self._context())

    def test_contract_validator_rejects_invalid_route_and_general_claim_provenance(self):
        from dataclasses import replace
        from agentforge.answer_contract import (
            AnswerClaim, validate_answer_contract,
        )

        context = self._context()
        valid = verify_generated_answer(
            Answer("The function removes the first item [E1].", (), False), context,
            route="repository", route_confidence="high",
        )
        with self.subTest("invalid route"):
            with self.assertRaisesRegex(ProviderError, "route is invalid"):
                validate_answer_contract(replace(valid, route=""), context)
        with self.subTest("general claim cannot borrow repository citation"):
            claim = replace(valid.claims[0], origin="general")
            with self.assertRaisesRegex(ProviderError, "General knowledge claims"):
                validate_answer_contract(replace(valid, claims=(claim,)), context)

    def test_confirmed_claim_requires_exact_excerpt_and_refusal_cannot_confirm(self):
        from dataclasses import replace
        from agentforge.answer_contract import AnswerClaim, validate_answer_contract

        context = self._context()
        inference = verify_generated_answer(
            Answer("The function removes the first item [E1].", (), False), context,
            route="repository", route_confidence="high",
        )
        with self.subTest("confirmation is exact excerpt only"):
            confirmed = replace(inference.claims[0], kind="confirmed")
            with self.assertRaisesRegex(ProviderError, "exact excerpt"):
                validate_answer_contract(replace(inference, claims=(confirmed,)), context)

        source_text = context.evidence[0].result.text
        exact = replace(
            inference,
            status="refused",
            content=source_text,
            claims=(AnswerClaim(source_text, "confirmed", ("src/fifo.py:1-3",)),),
        )
        with self.subTest("refused answer cannot carry confirmed claim"):
            with self.assertRaisesRegex(ProviderError, "cannot contain confirmed claims"):
                validate_answer_contract(exact, context)

    def test_general_and_planner_stop_contracts_fail_closed_at_input_boundaries(self):
        from agentforge.answer_contract import build_planner_stop_contract

        with self.subTest("empty general answer"):
            with self.assertRaisesRegex(ProviderError, "no answer text"):
                build_general_contract(Answer("  ", (), False))

        offline = build_general_contract(Answer("No model is configured.", (), True))
        self.assertEqual("refused", offline.status)
        self.assertIn("No generative provider", offline.reason)

        with self.subTest("invalid planner stop metadata"):
            with self.assertRaisesRegex(ProviderError, "metadata is invalid"):
                build_planner_stop_contract(
                    Answer("Stopped.", (), False), route="auto", reason="clarify",
                    origin="repository",
                )
        with self.subTest("empty planner stop answer"):
            with self.assertRaisesRegex(ProviderError, "response is empty"):
                build_planner_stop_contract(
                    Answer("", (), False), route="auto", reason="clarify",
                )

    def test_hybrid_rejects_invalid_answer_and_empty_required_section(self):
        with self.subTest("invalid answer type"):
            with self.assertRaisesRegex(ProviderError, "invalid answer"):
                verify_hybrid_answer(None, self._context())

        with self.subTest("empty repository section"):
            answer = Answer(
                "General knowledge:\nFIFO removes the earliest item first.\n"
                "Repository evidence:\n   ", (), False,
            )
            with self.assertRaisesRegex(ProviderError, "both non-empty answer sections"):
                verify_hybrid_answer(answer, self._context())


class EvidenceEvaluationTests(unittest.TestCase):
    def test_route_confidence_and_required_roles_are_separate_from_semantic_claim(self):
        candidate = SimpleNamespace(score=1.0, evidence_roles=("definition",),
                                    relative_path="src/fifo.py", text="def pop(): return items.pop(0)")
        assessment = assess_evidence(
            "Where is function pop defined?",
            SimpleNamespace(confidence="high"), (candidate,),
        )
        self.assertEqual("route_supported", assessment.relevance)
        self.assertEqual("sufficient", assessment.sufficiency)
        self.assertIn("semantic entailment is not established", assessment.reason)

    def test_low_confidence_lookalike_is_not_promoted(self):
        candidate = SimpleNamespace(score=0.9, evidence_roles=("definition",))
        assessment = assess_evidence(
            "What does missing_symbol do?",
            SimpleNamespace(confidence="low"), (candidate,),
        )
        self.assertEqual((), assessment.relevant_candidates)
        self.assertEqual("insufficient", assessment.sufficiency)

    def test_missing_required_role_is_partial_not_sufficient(self):
        candidate = SimpleNamespace(score=1.0, evidence_roles=("callsite",),
                                    relative_path="src/app.py", text="run_job()")
        assessment = assess_evidence(
            "Where does the caller call function run_job and where is it defined?",
            SimpleNamespace(confidence="high"), (candidate,),
        )
        self.assertEqual("partial", assessment.sufficiency)
        self.assertIn("target_definition", assessment.missing_roles)

    def test_similar_symbol_candidate_is_unverified_and_not_selected(self):
        candidate = SimpleNamespace(score=1.0, evidence_roles=("definition",),
                                    relative_path="src/user.py",
                                    text="def fetch_user_profile(user_id): return None")
        assessment = assess_evidence(
            "What does function fetch_user do?",
            SimpleNamespace(confidence="high"), (candidate,),
        )
        self.assertEqual("unverified", assessment.relevance)
        self.assertEqual((), assessment.relevant_candidates)
        self.assertEqual("unverified", assessment.candidate_results[0]["status"])
        self.assertEqual("NO_DETERMINISTIC_RELEVANCE",
                         assessment.candidate_results[0]["reason_code"])

    def test_low_confidence_route_has_stable_rejection_code(self):
        candidate = SimpleNamespace(score=1.0, evidence_roles=("definition",),
                                    relative_path="src/weighted_total.py",
                                    text="def calculate_weighted_total(amount, fee_rate): return amount * fee_rate")
        assessment = assess_evidence(
            "What formula does calculate_weighted_total use?",
            SimpleNamespace(confidence="low"), (candidate,),
        )

        self.assertEqual((), assessment.relevant_candidates)
        self.assertEqual("LOW_CONFIDENCE_ROUTE",
                         assessment.candidate_results[0]["reason_code"])

    def test_multiple_directly_matched_sources_can_jointly_cover_required_roles(self):
        callsite = SimpleNamespace(score=2.0, evidence_roles=("callsite",),
                                   relative_path="src/app.py", text="return run_job()")
        definition = SimpleNamespace(score=1.0, evidence_roles=("target_definition",),
                                     relative_path="src/jobs.py", text="def run_job(): return True")
        assessment = assess_evidence(
            "Where is run_job called and defined?",
            SimpleNamespace(confidence="high"), (callsite, definition),
        )
        self.assertEqual(2, len(assessment.relevant_candidates))
        self.assertEqual("sufficient", assessment.sufficiency)

    def test_internal_method_call_flow_uses_definition_evidence_without_cross_file_gate(self):
        question = "分析当前仓库 Conv2d 的实现，说明 forward 的调用过程，并给出源码引用。"
        candidate = SimpleNamespace(
            score=1.0,
            evidence_roles=("definition",),
            relative_path="eneuro/nn/module.py",
            text="class Conv2d:\n    def forward(self, x):\n        return conv2d(x, self.W)",
        )

        self.assertEqual((), required_evidence_roles(question))
        assessment = assess_evidence(question, SimpleNamespace(confidence="high"), (candidate,))

        self.assertEqual("sufficient", assessment.sufficiency)
        self.assertEqual((candidate,), assessment.relevant_candidates)

    def test_explicit_call_chain_still_requires_callsite_and_target_definition(self):
        question = "说明 run_queue 的调用链，并给出实际调用位置和目标函数定义。"
        self.assertEqual(
            ("callsite", "target_definition"), required_evidence_roles(question),
        )

    def test_malformed_candidate_metadata_and_nonpositive_scores_are_unverified(self):
        malformed = SimpleNamespace(
            score=1.0, evidence_roles=("definition",), relative_path=None,
            text="def compute_total(): return 42",
        )
        invalid_score = SimpleNamespace(
            score=True, evidence_roles=("definition",), relative_path="src/math_ops.py",
            text="def compute_total(): return 42",
        )
        assessment = assess_evidence(
            "What does compute_total do?", SimpleNamespace(confidence="high"),
            (malformed, invalid_score),
        )

        self.assertEqual((), assessment.relevant_candidates)
        self.assertEqual(["unverified", "unverified"],
                         [item["status"] for item in assessment.candidate_results])
        self.assertIn("metadata", assessment.candidate_results[0]["reason"])
        self.assertIn("positive retrieval score", assessment.candidate_results[1]["reason"])
        self.assertEqual(
            ["CANDIDATE_METADATA_MISSING", "INVALID_RETRIEVAL_SCORE"],
            [item["reason_code"] for item in assessment.candidate_results],
        )

    def test_chinese_overlap_can_support_a_relevance_signal_but_unmatched_text_cannot(self):
        chinese = SimpleNamespace(
            score=1.0, evidence_roles=("definition",), relative_path="src/queue.py",
            text="队列按照先进先出的顺序处理任务",
        )
        unrelated = SimpleNamespace(
            score=1.0, evidence_roles=("definition",), relative_path="src/storage.py",
            text="class Archive: pass",
        )
        assessment = assess_evidence(
            "队列如何按照先进先出顺序处理任务？",
            SimpleNamespace(confidence="high"), (chinese, unrelated),
        )

        self.assertEqual((chinese,), assessment.relevant_candidates)
        self.assertEqual("relevant", assessment.candidate_results[0]["status"])
        self.assertEqual("unverified", assessment.candidate_results[1]["status"])
        self.assertIn("Chinese character pairs", assessment.candidate_results[0]["reason"])
        self.assertEqual("RELEVANCE_SIGNAL", assessment.candidate_results[0]["reason_code"])
        self.assertEqual("NO_DETERMINISTIC_RELEVANCE",
                         assessment.candidate_results[1]["reason_code"])

    def test_internal_forward_flow_with_definition_reaches_answer_provider(self):
        from agentforge.conversation_orchestrator import (
            ConversationDependencies, ConversationOrchestrator, ConversationRequest,
            ConversationSession,
        )
        from agentforge.product_retrieval import ProductCandidate, ProductRetrievalOutcome
        from agentforge.query_routing import RouteDecision

        question = "分析当前仓库 Conv2d 的实现，说明 forward 的调用过程，并给出源码引用。"
        source = ProductCandidate(
            "synthetic-repo", "eneuro/nn/module.py", 157, 264,
            "class Conv2d:\n    def forward(self, x):\n        return conv2d(x, self.W)",
            "a" * 64, 1.0, ("symbols",), (("symbols", 1.0),),
            resolution_status="uncertain", evidence_roles=("definition",),
        )
        outcome = ProductRetrievalOutcome(
            question, "synthetic", RouteDecision(
                "exact_symbol", ("symbols",), "exact_symbol", "Exact symbol", "high",
            ), (source,), (), (), "A static relationship is uncertain.",
        )

        class Retriever:
            def search(self, _query, *, top_k):
                return outcome

        class Provider:
            provider_id = "llama-server"

            def __init__(self):
                self.answer_calls = 0

            def answer(self, context, **_kwargs):
                self.answer_calls += 1
                self.context = context
                return Answer(
                    "forward calls conv2d with the stored weights [E1].",
                    ("eneuro/nn/module.py:157-264",), False,
                )

            @staticmethod
            def request_stats():
                return {}

        provider = Provider()
        orchestrator = ConversationOrchestrator(ConversationDependencies(
            provider_factory=Mock(return_value=provider),
            model_budget_resolver=lambda *args, **kwargs: {
                "input_budget": 3000, "output_reserve": 256, "safety_margin": 128,
                "context_window": 4096, "estimate_kind": "estimated",
            },
            retriever_factory=lambda *_args, **_kwargs: Retriever(),
        ))
        result = orchestrator.run(
            ConversationRequest(question, "synthetic-repo", "llama-server", mode="repository"),
            index_store=object(),
            session=ConversationSession(
                provider=provider, repository_id="synthetic-repo",
                conversation={"repository_id": "synthetic-repo"},
            ),
        )

        self.assertEqual(1, provider.answer_calls)
        self.assertEqual("sufficient", result.retrieval["evidence_assessment"]["sufficiency"])
        self.assertEqual("limited", result.contract.status)
        self.assertIn("[E1]", result.answer.text)


class ProviderDialogueModeTests(unittest.TestCase):
    def test_qwen_general_request_contains_no_repository_context_or_citations(self):
        client = Mock()
        client.max_output_tokens = 128
        client.chat.return_value = "A transformer applies attention across token representations."
        provider = LlamaServerProvider(client, "local-test")
        context = BuiltContext("Explain a transformer.", (), "", 0, 1000)
        answer = provider.answer_general(context)
        messages = client.chat.call_args.args[0]
        self.assertEqual((), answer.citations)
        self.assertIn("learned knowledge", messages[0]["content"])
        self.assertIn("Explain a transformer.", messages[1]["content"])
        self.assertNotIn("Untrusted current repository evidence", "\n".join(m["content"] for m in messages))

    def test_qwen_hybrid_prompt_scopes_ids_to_repository_section(self):
        client = Mock()
        client.max_output_tokens = 128
        client.chat.return_value = (
            "General knowledge:\nFIFO removes the oldest queued item.\n"
            "Repository evidence:\nThe code removes index zero [E1]."
        )
        provider = LlamaServerProvider(client, "local-test")
        context = DialogueContractTests()._context()
        answer = provider.answer_hybrid(context)
        prompt = "\n".join(item["content"] for item in client.chat.call_args.args[0])
        self.assertEqual((), answer.citations)
        contract = verify_hybrid_answer(answer, context)
        self.assertEqual(("src/fifo.py:1-3",), contract.claims[1].citations)
        self.assertIn("General knowledge:", prompt)
        self.assertIn("Repository evidence:", prompt)
        self.assertIn("Only the Repository evidence section", prompt)
        self.assertIn("Do not include repository paths or line numbers anywhere in the answer", prompt)

    def test_qwen_repository_prompt_uses_ids_without_path_locations(self):
        client = Mock()
        client.max_output_tokens = 128
        client.chat.return_value = "The function returns the input value [E1]."
        provider = LlamaServerProvider(client, "local-test")
        context = DialogueContractTests()._context()
        provider.answer(context)
        system = client.chat.call_args.args[0][0]["content"]
        self.assertIn("using only an applicable current evidence ID such as [E1]", system)
        self.assertIn("Do not include repository paths or line numbers anywhere in the answer", system)

    def test_qwen_external_data_limit_preserves_general_answer_path(self):
        client = Mock()
        client.max_output_tokens = 128
        client.chat.return_value = "I cannot check live weather; generally, weather varies by region."
        provider = LlamaServerProvider(client, "local-test")
        provider.answer_general(
            BuiltContext("What is today's weather?", (), "", 0, 1000),
            external_data_unavailable=True,
        )
        system = client.chat.call_args.args[0][0]["content"]
        self.assertIn("no authorized external-data tool is available", system)
        self.assertIn("Do not imply that live data was checked", system)

    def test_deepseek_general_and_hybrid_share_mode_specific_contract(self):
        descriptor = ModelDescriptor(
            provider_id="deepseek", model_id="test-model", display_name="test",
            model_type="cloud", context_window=4096, max_output_tokens=256,
            supports_streaming=False, supports_thinking=False,
        )
        client = DeepSeekClient("test-key")
        client.chat = Mock(side_effect=[
            DeepSeekCompletion("Attention weights combine token representations.", None,
                               "stop", (), {}, None),
            DeepSeekCompletion(
                "General knowledge:\nFIFO removes the oldest item.\n"
                "Repository evidence:\nThe implementation removes index zero [E1].",
                None, "stop", (), {}, None,
            ),
        ])
        provider = DeepSeekProvider(client, "test-model", descriptor=descriptor)
        general = provider.answer_general(BuiltContext("Explain attention.", (), "", 0, 1000))
        hybrid = provider.answer_hybrid(DialogueContractTests()._context())
        self.assertEqual((), general.citations)
        self.assertEqual((), hybrid.citations)
        contract = verify_hybrid_answer(hybrid, DialogueContractTests()._context())
        self.assertEqual(("src/fifo.py:1-3",), contract.claims[1].citations)
        self.assertEqual(2, client.chat.call_count)
        self.assertIn(
            "Do not include repository paths or line numbers anywhere in the answer",
            client.chat.call_args.args[0][0]["content"],
        )

    def test_deepseek_external_data_limit_is_explicit(self):
        descriptor = ModelDescriptor(
            provider_id="deepseek", model_id="test-model", display_name="test",
            model_type="cloud", context_window=4096, max_output_tokens=256,
            supports_streaming=False, supports_thinking=False,
        )
        client = DeepSeekClient("test-key")
        client.chat = Mock(return_value=DeepSeekCompletion(
            "I cannot check current weather.", None, "stop", (), {}, None,
        ))
        provider = DeepSeekProvider(client, "test-model", descriptor=descriptor)
        provider.answer_general(
            BuiltContext("What is today's weather?", (), "", 0, 1000),
            external_data_unavailable=True,
        )
        system = client.chat.call_args.args[0][0]["content"]
        self.assertIn("Do not imply that live data was checked", system)

    def test_deepseek_general_answer_can_state_repository_evidence_is_unavailable(self):
        descriptor = ModelDescriptor(
            provider_id="deepseek", model_id="test-model", display_name="test",
            model_type="cloud", context_window=4096, max_output_tokens=256,
            supports_streaming=False, supports_thinking=False,
        )
        client = DeepSeekClient("test-key")
        client.chat = Mock(return_value=DeepSeekCompletion(
            "Convolution applies a learned kernel over local input regions.", None,
            "stop", (), {}, None,
        ))
        provider = DeepSeekProvider(client, "test-model", descriptor=descriptor)
        provider.answer_general(
            BuiltContext("How does convolution work?", (), "", 0, 1000),
            repository_evidence_unavailable=True,
        )
        system = client.chat.call_args.args[0][0]["content"]
        self.assertIn("produced no verified source evidence", system)
        self.assertIn("Do not claim what this repository implements", system)
        self.assertIn("answer any general principle", system)


if __name__ == "__main__":
    unittest.main()
