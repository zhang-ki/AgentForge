"""Offline regression for strict, bounded LLM planning."""

import json
import threading
import unittest
from dataclasses import replace
from unittest.mock import Mock

from agentforge.model_types import LOCAL_QWEN
from agentforge.planner import LLMPlanner, PlanningError, parse_conversation_plan
from agentforge.providers.deepseek import DeepSeekClient, DeepSeekCompletion, DeepSeekProvider
from agentforge.providers.llama_server import LlamaServerProvider


def plan_payload(intent="general", *, external=False, agent_execution=False):
    if intent == "general":
        tasks = [{"task_id": "t1", "task_type": "general", "question": "Explain attention",
                  "requires_retrieval": False, "search_queries": [], "dependencies": []}]
    elif intent == "repository":
        tasks = [{"task_id": "t1", "task_type": "repository", "question": "Find implementation",
                  "requires_retrieval": True, "search_queries": ["implementation"],
                  "dependencies": []}]
    else:
        tasks = [
            {"task_id": "t1", "task_type": "general", "question": "Explain FIFO",
             "requires_retrieval": False, "search_queries": [], "dependencies": []},
            {"task_id": "t2", "task_type": "repository", "question": "Find FIFO implementation",
             "requires_retrieval": True, "search_queries": ["FIFO implementation"],
             "dependencies": ["t1"]},
        ]
    if external:
        tasks.append({"task_id": "t3", "task_type": "external", "question": "Get current weather",
                      "requires_retrieval": False, "search_queries": [], "dependencies": []})
    if agent_execution:
        tasks.append({"task_id": "tool1", "task_type": "tool",
                      "question": "Perform the explicitly requested registered tool action",
                      "requires_retrieval": False, "search_queries": [], "dependencies": []})
    return {"intent": intent, "tasks": tasks,
            "requires_repository": intent in {"repository", "hybrid"},
            "requires_external_data": external,
            "requires_agent_execution": agent_execution,
            "answer_strategy": "compose" if intent == "hybrid" else "direct",
            "clarification_required": False, "confidence": 0.92,
            "reason": "Synthetic plan for offline validation."}


def encoded(payload):
    return json.dumps(payload, ensure_ascii=False)


class FakePlanningProvider:
    descriptor = LOCAL_QWEN
    model = LOCAL_QWEN.model_id
    provider_id = LOCAL_QWEN.provider_id

    def __init__(self, response=None, error=None):
        self.response = response
        self.responses = tuple(response) if isinstance(response, (list, tuple)) else None
        self.error = error
        self.errors = tuple(error) if isinstance(error, (list, tuple)) else None
        self.calls = []

    def plan_completion(self, messages, *, max_output_tokens, timeout_seconds):
        self.calls.append((messages, max_output_tokens, timeout_seconds))
        if self.errors is not None:
            error = self.errors[min(len(self.calls) - 1, len(self.errors) - 1)]
            if error:
                raise error
        elif self.error:
            raise self.error
        if self.responses is not None:
            return self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]
        return self.response


class PlanSchemaTests(unittest.TestCase):
    def test_accepts_general_repository_hybrid_and_external_task_plans(self):
        for intent, external in (("general", False), ("repository", False),
                                 ("hybrid", False), ("general", True)):
            with self.subTest(intent=intent, external=external):
                result = parse_conversation_plan(encoded(plan_payload(intent, external=external)),
                                                 requested_mode="auto")
                self.assertEqual(intent, result.intent)
                self.assertEqual(external, result.requires_external_data)

    def test_intent_task_compatibility_accepts_the_three_validator_combinations(self):
        for intent in ("general", "repository", "hybrid"):
            with self.subTest(intent=intent):
                result = parse_conversation_plan(encoded(plan_payload(intent)), requested_mode="auto")
                self.assertEqual(intent, result.intent)

    def test_retrieval_flag_mismatch_has_specific_content_free_diagnostic(self):
        payload = plan_payload("general")
        payload["tasks"][0]["requires_retrieval"] = True
        payload["tasks"][0]["question"] = "PRIVATE_TASK_TEXT"

        with self.assertRaises(PlanningError) as raised:
            parse_conversation_plan(encoded(payload), requested_mode="auto")

        self.assertEqual("task_retrieval_type_mismatch", raised.exception.diagnostic_code)
        self.assertEqual({
            "task_id": "t1",
            "task_type": "general",
            "requires_retrieval": True,
            "expected_requires_retrieval": False,
        }, raised.exception.diagnostic_details)
        self.assertNotIn("PRIVATE_TASK_TEXT", str(raised.exception.diagnostic_details))

    def test_general_answer_intent_can_independently_request_tool_execution(self):
        payload = plan_payload("general", agent_execution=True)
        parsed = parse_conversation_plan(encoded(payload), requested_mode="auto")

        self.assertEqual("general", parsed.intent)
        self.assertTrue(parsed.requires_agent_execution)
        self.assertFalse(parsed.requires_repository)

    def test_tool_task_is_an_independent_execution_axis_for_repository_intent(self):
        payload = plan_payload("repository", agent_execution=True)
        parsed = parse_conversation_plan(
            encoded(payload), requested_mode="auto", agent_execution_available=True,
        )

        self.assertEqual("repository", parsed.intent)
        self.assertEqual({"repository", "tool"}, {task.task_type for task in parsed.tasks})
        self.assertTrue(parsed.requires_repository)
        self.assertTrue(parsed.requires_agent_execution)

    def test_agent_execution_flag_must_match_executable_task_count(self):
        missing_tool_task = plan_payload("repository", agent_execution=True)
        missing_tool_task["tasks"] = [task for task in missing_tool_task["tasks"]
                                       if task["task_type"] != "tool"]
        with self.assertRaises(PlanningError) as no_task:
            parse_conversation_plan(encoded(missing_tool_task), requested_mode="auto")
        self.assertEqual("agent_execution_task_mismatch", no_task.exception.diagnostic_code)
        self.assertEqual(0, no_task.exception.diagnostic_details["executable_task_count"])

        missing_flag = plan_payload("general", agent_execution=True)
        missing_flag["requires_agent_execution"] = False
        with self.assertRaises(PlanningError) as no_flag:
            parse_conversation_plan(encoded(missing_flag), requested_mode="auto")
        self.assertEqual("agent_execution_task_mismatch", no_flag.exception.diagnostic_code)
        self.assertEqual(1, no_flag.exception.diagnostic_details["executable_task_count"])

    def test_tool_task_is_rejected_when_no_authorized_agent_executor_is_available(self):
        with self.assertRaises(PlanningError) as raised:
            parse_conversation_plan(
                encoded(plan_payload("general", agent_execution=True)),
                requested_mode="auto", agent_execution_available=False,
            )
        self.assertEqual("agent_execution_unavailable", raised.exception.diagnostic_code)

    def test_agent_execution_flag_correction_is_bounded_and_preserves_tool_task(self):
        invalid = plan_payload("general", agent_execution=True)
        invalid["requires_agent_execution"] = False
        corrected = plan_payload("general", agent_execution=True)
        provider = FakePlanningProvider([encoded(invalid), encoded(corrected)])
        tool = {"tool_id": "synthetic_read", "description": "Read a synthetic value.",
                "side_effect": False, "requires_approval": False}
        planner = LLMPlanner()

        parsed = planner.plan(
            provider, "Use synthetic_read to inspect a value.",
            agent_execution_available=True, available_tools=(tool,),
        )

        self.assertTrue(parsed.requires_agent_execution)
        self.assertEqual(2, len(provider.calls))
        correction = provider.calls[1][0][-1]["content"]
        self.assertIn("agent_execution_task_mismatch", correction)
        self.assertIn("tool task count=1", correction)
        self.assertEqual(1, planner.last_request_diagnostics["correction_attempt"])
        self.assertEqual(1, planner.last_request_diagnostics["executable_task_count"])
        self.assertEqual("passed_after_correction",
                         planner.last_request_diagnostics["final_validation_status"])

    def test_intent_task_mismatch_has_safe_specific_diagnostics(self):
        invalid = []
        general_with_repository = plan_payload("repository")
        general_with_repository["intent"] = "general"
        invalid.append((general_with_repository, "general", ["repository"], ["general"]))

        repository_without_repository = plan_payload("general")
        repository_without_repository["intent"] = "repository"
        invalid.append((repository_without_repository, "repository", ["general"], ["repository"]))

        hybrid_without_general = plan_payload("repository")
        hybrid_without_general["intent"] = "hybrid"
        invalid.append((hybrid_without_general, "hybrid", [], ["general"]))

        hybrid_without_repository = plan_payload("general")
        hybrid_without_repository["intent"] = "hybrid"
        invalid.append((hybrid_without_repository, "hybrid", [], ["repository"]))

        for payload, intent, forbidden, missing in invalid:
            with self.subTest(intent=intent, forbidden=forbidden, missing=missing):
                with self.assertRaises(PlanningError) as raised:
                    parse_conversation_plan(encoded(payload), requested_mode="auto")
                error = raised.exception
                self.assertEqual("intent_task_mismatch", error.diagnostic_code)
                self.assertEqual(intent, error.diagnostic_details["intent"])
                self.assertEqual(forbidden, error.diagnostic_details["forbidden_task_types_present"])
                self.assertEqual(missing, error.diagnostic_details["missing_task_types"])
                self.assertEqual({"external": 0, "general": 0, "repository": 1, "tool": 0}
                                 if payload["tasks"][0]["task_type"] == "repository"
                                 else {"external": 0, "general": 1, "repository": 0, "tool": 0},
                                 error.diagnostic_details["task_type_counts"])

    def test_agent_execution_flag_is_independent_of_answer_intent(self):
        payload = plan_payload("repository", agent_execution=True)
        parsed = parse_conversation_plan(encoded(payload), requested_mode="auto")
        self.assertTrue(parsed.requires_agent_execution)
        payload = plan_payload("general", agent_execution=True)
        parsed = parse_conversation_plan(encoded(payload), requested_mode="auto")
        self.assertEqual("general", parsed.intent)
        self.assertTrue(parsed.requires_agent_execution)

    def test_explicit_mode_mismatch_is_rejected(self):
        with self.assertRaises(PlanningError):
            parse_conversation_plan(encoded(plan_payload("general")), requested_mode="repository")

    def test_rejects_malformed_json_and_extra_execution_fields(self):
        with self.assertRaisesRegex(PlanningError, "invalid JSON"):
            parse_conversation_plan("not-json", requested_mode="auto")
        payload = plan_payload("general")
        payload["tool_name"] = "run_shell"
        with self.assertRaisesRegex(PlanningError, "schema"):
            parse_conversation_plan(encoded(payload), requested_mode="auto")

    def test_rejects_inconsistent_flags_and_forward_dependencies(self):
        payload = plan_payload("repository")
        payload["requires_repository"] = False
        with self.assertRaisesRegex(PlanningError, "capability flags"):
            parse_conversation_plan(encoded(payload), requested_mode="auto")
        payload = plan_payload("repository")
        payload["tasks"][0]["dependencies"] = ["later"]
        with self.assertRaisesRegex(PlanningError, "earlier task IDs"):
            parse_conversation_plan(encoded(payload), requested_mode="auto")

    def test_unhashable_schema_values_fail_as_planning_errors(self):
        payload = plan_payload("general")
        payload["intent"] = []
        with self.assertRaises(PlanningError):
            parse_conversation_plan(encoded(payload), requested_mode="auto")

    def test_oversized_reason_is_rejected_with_bounded_diagnostic(self):
        payload = plan_payload("general")
        payload["reason"] = "x" * 281
        with self.assertRaisesRegex(PlanningError, "reason exceeds 280 characters"):
            parse_conversation_plan(encoded(payload), requested_mode="auto")

    def test_rejects_out_of_range_and_inconsistent_task_fields(self):
        invalid_payloads = []

        payload = plan_payload("general")
        payload["tasks"] = []
        invalid_payloads.append(payload)
        payload = plan_payload("general")
        payload["tasks"] = [plan_payload("general")["tasks"][0] for _ in range(7)]
        invalid_payloads.append(payload)
        payload = plan_payload("general")
        payload["tasks"][0]["task_id"] = "bad id"
        invalid_payloads.append(payload)
        payload = plan_payload("general")
        payload["tasks"][0]["task_type"] = "external"
        invalid_payloads.append(payload)
        payload = plan_payload("general")
        payload["tasks"][0]["question"] = " "
        invalid_payloads.append(payload)
        payload = plan_payload("general")
        payload["tasks"][0]["question"] = "q" * 2001
        invalid_payloads.append(payload)
        payload = plan_payload("repository")
        payload["tasks"][0]["search_queries"] = []
        invalid_payloads.append(payload)
        payload = plan_payload("repository")
        payload["tasks"][0]["search_queries"] = ["x"] * 4
        invalid_payloads.append(payload)
        payload = plan_payload("repository")
        payload["tasks"][0]["search_queries"] = [""]
        invalid_payloads.append(payload)
        payload = plan_payload("repository")
        payload["tasks"][0]["dependencies"] = ["t1", "t1"]
        invalid_payloads.append(payload)
        payload = plan_payload("general")
        payload["answer_strategy"] = "execute_shell"
        invalid_payloads.append(payload)
        payload = plan_payload("general")
        payload["clarification_required"] = "false"
        invalid_payloads.append(payload)
        payload = plan_payload("general")
        payload["confidence"] = float("nan")
        invalid_payloads.append(payload)
        payload = plan_payload("general")
        payload["reason"] = " "
        invalid_payloads.append(payload)

        for payload in invalid_payloads:
            with self.subTest(payload=payload), self.assertRaises(PlanningError):
                parse_conversation_plan(encoded(payload), requested_mode="auto")

    def test_invalid_search_queries_report_shape_counts_without_query_text(self):
        payload = plan_payload("repository")
        payload["tasks"][0]["search_queries"] = ["valid query", "  ", "x" * 513, 7]

        with self.assertRaises(PlanningError) as raised:
            parse_conversation_plan(encoded(payload), requested_mode="auto")

        error = raised.exception
        self.assertEqual("invalid_bounded_search_queries", error.diagnostic_code)
        self.assertEqual({
            "query_count": 4,
            "non_string_count": 1,
            "empty_query_count": 1,
            "oversized_query_count": 1,
            "max_query_chars": 513,
        }, error.diagnostic_details)
        self.assertNotIn("valid query", str(error.diagnostic_details))
        self.assertNotIn("x" * 513, str(error.diagnostic_details))

    def test_non_list_search_queries_report_only_json_type(self):
        payload = plan_payload("repository")
        payload["tasks"][0]["search_queries"] = "synthetic private query text"

        with self.assertRaises(PlanningError) as raised:
            parse_conversation_plan(encoded(payload), requested_mode="auto")

        self.assertEqual({"value_type": "str"}, raised.exception.diagnostic_details)
        self.assertNotIn("synthetic private query text", str(raised.exception.diagnostic_details))

    def test_search_query_upper_bounds_remain_valid(self):
        payload = plan_payload("repository")
        payload["tasks"][0]["search_queries"] = ["a" * 512, "b" * 512, "c" * 512]

        result = parse_conversation_plan(encoded(payload), requested_mode="auto")

        self.assertEqual(3, len(result.tasks[0].search_queries))
        self.assertTrue(all(len(query) == 512 for query in result.tasks[0].search_queries))

    def test_rejects_non_object_json_oversized_input_and_unknown_mode(self):
        for raw, mode in (("[]", "auto"), (" " * 32_001, "auto"),
                          (encoded(plan_payload()), "not-a-mode")):
            with self.subTest(mode=mode, length=len(raw)), self.assertRaises(PlanningError):
                parse_conversation_plan(raw, requested_mode=mode)
        payload = plan_payload("general")
        payload["tasks"][0]["task_type"] = []
        with self.assertRaises(PlanningError):
            parse_conversation_plan(encoded(payload), requested_mode="auto")


class LLMPlannerTests(unittest.TestCase):
    def test_calls_the_selected_model_once_with_bounded_output_and_timeout(self):
        provider = FakePlanningProvider(encoded(plan_payload("general")))
        planner = LLMPlanner()
        result = planner.plan(provider, "Explain attention", requested_mode="auto")
        self.assertEqual("general", result.intent)
        self.assertEqual(1, len(provider.calls))
        self.assertEqual("passed", planner.last_request_diagnostics["initial_validation_status"])
        self.assertEqual("passed", planner.last_request_diagnostics["final_validation_status"])
        _, output_tokens, timeout = provider.calls[0]
        self.assertEqual(512, output_tokens)  # Qwen's verified adapter ceiling caps structured planning.
        self.assertEqual(60.0, timeout)
        self.assertLessEqual(timeout, LLMPlanner.TIMEOUT_SECONDS)

    def test_corrects_only_bounded_search_query_shape_once(self):
        invalid = plan_payload("repository")
        invalid["tasks"][0]["search_queries"] = ["PRIVATE_INVALID_QUERY"] * 4
        provider = FakePlanningProvider([
            encoded(invalid), encoded(plan_payload("repository")),
        ])
        planner = LLMPlanner()

        result = planner.plan(provider, "Find Conv2d in the current repository")

        self.assertEqual("repository", result.intent)
        self.assertEqual(2, len(provider.calls))
        self.assertEqual(provider.calls[0][1], provider.calls[1][1])
        correction = json.dumps(provider.calls[1][0], ensure_ascii=False)
        self.assertIn("Return the complete plan JSON only", correction)
        self.assertNotIn("PRIVATE_INVALID_QUERY", correction)
        self.assertEqual(2, planner.last_request_diagnostics["planner_request_count"])
        self.assertTrue(planner.last_request_diagnostics["correction_attempted"])
        self.assertEqual("accepted", planner.last_request_diagnostics["correction_status"])
        self.assertEqual("failed", planner.last_request_diagnostics["initial_validation_status"])
        self.assertEqual("passed_after_correction",
                         planner.last_request_diagnostics["final_validation_status"])
        self.assertEqual(4, planner.last_request_diagnostics["initial_query_shape"]["query_count"])

    def test_prompt_and_validator_share_intent_task_rules(self):
        provider = FakePlanningProvider(encoded(plan_payload("general")))
        LLMPlanner().plan(provider, "Explain attention")
        prompt = provider.calls[0][0][0]["content"]
        self.assertIn("general=general, forbid:repository", prompt)
        self.assertIn("repository=repository, forbid:general", prompt)
        self.assertIn("hybrid=general+repository, forbid:none", prompt)

    def test_prompt_routes_repository_facts_and_delegates_iteration_to_react_agent(self):
        provider = FakePlanningProvider(encoded(plan_payload("general")))
        tool = {"tool_id": "read",
                "description": "Read a scanner-approved source range.",
                "side_effect": False, "requires_approval": False}
        LLMPlanner().plan(provider, "Inspect the repository", agent_execution_available=True,
                          available_tools=(tool,))
        prompt = provider.calls[0][0][0]["content"]
        self.assertIn("Intent rules (same as validator)", prompt)
        self.assertIn("A source lookup/citation requires repository task(s)", prompt)
        self.assertIn("tool use alone does not change intent", prompt)
        self.assertIn("retrieve fresh evidence rather than reuse old citation labels", prompt)
        payload = json.loads(provider.calls[0][0][-1]["content"])
        self.assertEqual(["read"], [item["tool_id"] for item in payload["available_tools"]])
        self.assertIn("exact tool_id", prompt)

    def test_planner_tool_directory_is_bounded_and_never_contains_schemas(self):
        provider = FakePlanningProvider(encoded(plan_payload("general")))
        tool = {"tool_id": "synthetic_calculate",
                "description": "Calculate a synthetic numeric value.",
                "side_effect": False, "requires_approval": False}
        LLMPlanner().plan(provider, "Calculate a value", agent_execution_available=True,
                          available_tools=(tool,))
        payload = json.loads(provider.calls[0][0][-1]["content"])
        self.assertEqual(tool, payload["available_tools"][0])
        self.assertNotIn("input_schema", payload["available_tools"][0])

    def test_planner_rejects_unbounded_or_malformed_tool_directory(self):
        provider = FakePlanningProvider(encoded(plan_payload("general")))
        with self.assertRaises(PlanningError):
            LLMPlanner().plan(provider, "Calculate", agent_execution_available=True,
                              available_tools=({"tool_id": "x"},))
        self.assertEqual([], provider.calls)

        with self.assertRaises(PlanningError):
            LLMPlanner().plan(provider, "Calculate", agent_execution_available=True,
                              available_tools=tuple({"tool_id": str(i)} for i in range(9)))

    def test_explicit_request_for_authorized_tool_is_a_planner_execution_rule(self):
        provider = FakePlanningProvider(encoded(plan_payload("general")))
        tool = {"tool_id": "cancel_synthetic_read",
                "description": "Read synthetic state until cancelled.",
                "side_effect": False, "requires_approval": False}
        LLMPlanner().plan(provider, "Call cancel_synthetic_read once.",
                          agent_execution_available=True, available_tools=(tool,))
        prompt = provider.calls[0][0][0]["content"]
        self.assertIn("Explicit requests for a listed tool require a task_type='tool' task and requires_agent_execution=true", prompt)

    def test_corrects_capability_task_mismatch_once_with_exact_flag_error(self):
        invalid = plan_payload("repository")
        invalid["requires_repository"] = False
        corrected = plan_payload("repository")
        provider = FakePlanningProvider([encoded(invalid), encoded(corrected)])
        planner = LLMPlanner()

        result = planner.plan(provider, "Inspect this repository", requested_mode="auto")

        self.assertEqual("repository", result.intent)
        self.assertEqual(2, len(provider.calls))
        correction = json.dumps(provider.calls[1][0], ensure_ascii=False)
        self.assertIn("capability_task_mismatch", correction)
        self.assertIn("expected repository=True", correction)
        self.assertIn("Preserve all tasks and intent", correction)
        self.assertEqual("passed_after_correction",
                         planner.last_request_diagnostics["final_validation_status"])

    def test_repeated_capability_task_mismatch_fails_closed(self):
        invalid = plan_payload("repository")
        invalid["requires_repository"] = False
        provider = FakePlanningProvider([encoded(invalid), encoded(invalid)])
        with self.assertRaises(PlanningError):
            LLMPlanner().plan(provider, "Inspect this repository", requested_mode="auto")
        self.assertEqual(2, len(provider.calls))

    def test_prompt_does_not_offer_agent_execution_when_service_is_unavailable(self):
        provider = FakePlanningProvider(encoded(plan_payload("general")))
        LLMPlanner().plan(provider, "Explain attention", agent_execution_available=False)
        prompt = provider.calls[0][0][0]["content"]
        self.assertIn("Agent execution unavailable: do not create tool tasks", prompt)
        self.assertNotIn("An Agent execution service is available", prompt)

    def test_corrects_intent_task_mismatch_once_with_concrete_error(self):
        invalid = plan_payload("repository")
        invalid["intent"] = "hybrid"
        invalid["tasks"][0]["question"] = "PRIVATE_MODEL_ONLY_TASK_TEXT"
        provider = FakePlanningProvider([
            encoded(invalid), encoded(plan_payload("hybrid")),
        ])
        planner = LLMPlanner()

        result = planner.plan(provider, "Explain general behavior and inspect the repository")

        self.assertEqual("hybrid", result.intent)
        self.assertEqual({"general", "repository"}, {task.task_type for task in result.tasks})
        self.assertEqual(2, len(provider.calls))
        first_messages, second_messages = provider.calls[0][0], provider.calls[1][0]
        self.assertEqual("Explain general behavior and inspect the repository",
                         json.loads(first_messages[-1]["content"])["question"])
        correction = second_messages[-1]["content"]
        self.assertIn("intent_task_mismatch", correction)
        self.assertIn("intent=hybrid", correction)
        self.assertIn("repository=1", correction)
        self.assertIn("missing=general", correction)
        self.assertIn("preserve request/subtasks", correction)
        self.assertNotIn("PRIVATE_MODEL_ONLY_TASK_TEXT", json.dumps(second_messages))
        self.assertEqual(2, planner.last_request_diagnostics["planner_request_count"])
        self.assertTrue(planner.last_request_diagnostics["correction_attempted"])
        self.assertEqual("accepted", planner.last_request_diagnostics["correction_status"])
        self.assertEqual("intent_task_mismatch", planner.last_request_diagnostics[
            "initial_validation_failure"]["diagnostic_code"])

    def test_corrects_retrieval_flag_mismatch_once_without_reclassifying_the_task(self):
        invalid = plan_payload("general")
        invalid["tasks"][0]["requires_retrieval"] = True
        corrected = plan_payload("general")
        provider = FakePlanningProvider([encoded(invalid), encoded(corrected)])
        planner = LLMPlanner()

        result = planner.plan(provider, "Read a synthetic fact", requested_mode="auto")

        self.assertEqual("general", result.intent)
        self.assertEqual("general", result.tasks[0].task_type)
        self.assertFalse(result.tasks[0].requires_retrieval)
        self.assertEqual(2, len(provider.calls))
        system_prompt = provider.calls[0][0][0]["content"]
        self.assertIn("requires_retrieval must be true exactly when task_type is repository",
                      system_prompt)
        correction = provider.calls[1][0][-1]["content"]
        self.assertIn("task_retrieval_type_mismatch", correction)
        self.assertIn("task 't1'", correction)
        self.assertIn("requires_retrieval must be False", correction)
        self.assertIn("Preserve the task type, task question, intent, task list", correction)
        self.assertEqual("accepted", planner.last_request_diagnostics["correction_status"])
        self.assertEqual("passed_after_correction",
                         planner.last_request_diagnostics["final_validation_status"])

    def test_retrieval_flag_mismatch_correction_is_bounded_and_fails_closed(self):
        invalid = plan_payload("general")
        invalid["tasks"][0]["requires_retrieval"] = True
        provider = FakePlanningProvider([encoded(invalid), encoded(invalid)])
        planner = LLMPlanner()

        with self.assertRaises(PlanningError) as raised:
            planner.plan(provider, "Read a synthetic fact", requested_mode="auto")

        self.assertEqual(2, len(provider.calls))
        self.assertEqual(2, raised.exception.diagnostic_details["planner_request_count"])
        self.assertTrue(raised.exception.diagnostic_details["correction_attempted"])
        self.assertEqual("rejected", raised.exception.diagnostic_details["correction_status"])
        self.assertEqual("task_retrieval_type_mismatch", raised.exception.diagnostic_code)
        self.assertEqual("failed", planner.last_request_diagnostics["final_validation_status"])

    def test_intent_correction_timeout_fails_closed_and_keeps_initial_diagnostic(self):
        invalid = plan_payload("repository")
        invalid["intent"] = "hybrid"
        provider = FakePlanningProvider(
            [encoded(invalid), encoded(plan_payload("hybrid"))],
            error=[None, TimeoutError("private timeout detail")],
        )
        planner = LLMPlanner()

        with self.assertRaises(PlanningError) as raised:
            planner.plan(provider, "Explain and inspect")

        self.assertEqual("planner_correction_request_failed", raised.exception.diagnostic_code)
        self.assertEqual(2, len(provider.calls))
        self.assertEqual(LLMPlanner.TIMEOUT_SECONDS, provider.calls[1][2])
        self.assertEqual("intent_task_mismatch", raised.exception.diagnostic_details[
            "initial_validation_failure"]["diagnostic_code"])
        self.assertNotIn("private timeout detail", str(raised.exception))

    def test_intent_mismatch_correction_remains_bounded_after_second_rejection(self):
        invalid = plan_payload("repository")
        invalid["intent"] = "hybrid"
        still_invalid = plan_payload("general")
        still_invalid["intent"] = "hybrid"
        provider = FakePlanningProvider([encoded(invalid), encoded(still_invalid)])
        planner = LLMPlanner()

        with self.assertRaises(PlanningError) as raised:
            planner.plan(provider, "Preserve both parts of this request")

        self.assertEqual(2, len(provider.calls))
        self.assertEqual("intent_task_mismatch", raised.exception.diagnostic_code)
        self.assertEqual(2, raised.exception.diagnostic_details["planner_request_count"])
        self.assertEqual("rejected", raised.exception.diagnostic_details["correction_status"])
        self.assertEqual("intent_task_mismatch", raised.exception.diagnostic_details[
            "initial_validation_failure"]["diagnostic_code"])

    def test_invalid_corrected_search_query_fails_closed_after_one_correction(self):
        invalid_first = plan_payload("repository")
        invalid_first["tasks"][0]["search_queries"] = ["PRIVATE_FIRST_QUERY"] * 4
        invalid_second = plan_payload("repository")
        invalid_second["tasks"][0]["search_queries"] = ["PRIVATE_SECOND_QUERY"] * 4
        provider = FakePlanningProvider([encoded(invalid_first), encoded(invalid_second)])
        planner = LLMPlanner()

        with self.assertRaises(PlanningError) as raised:
            planner.plan(provider, "Find Conv2d in the current repository")

        self.assertEqual("invalid_bounded_search_queries", raised.exception.diagnostic_code)
        self.assertEqual(2, len(provider.calls))
        self.assertEqual(2, raised.exception.diagnostic_details["planner_request_count"])
        self.assertTrue(raised.exception.diagnostic_details["correction_attempted"])
        self.assertEqual("rejected", raised.exception.diagnostic_details["correction_status"])
        serialized = json.dumps(raised.exception.diagnostic_details)
        self.assertNotIn("PRIVATE_FIRST_QUERY", serialized)
        self.assertNotIn("PRIVATE_SECOND_QUERY", serialized)

    def test_other_plan_schema_errors_do_not_trigger_correction(self):
        invalid = plan_payload("general")
        invalid["reason"] = ""
        provider = FakePlanningProvider(encoded(invalid))
        planner = LLMPlanner()

        with self.assertRaises(PlanningError) as raised:
            planner.plan(provider, "Explain this")

        self.assertEqual(1, len(provider.calls))
        self.assertFalse(raised.exception.diagnostic_details["correction_attempted"])

    def test_planner_output_budget_uses_selected_model_capabilities(self):
        provider = FakePlanningProvider(encoded(plan_payload("general")))
        deepseek = replace(
            LOCAL_QWEN, provider_id="deepseek", model_id="deepseek-synthetic",
            model_type="cloud", context_window=1_048_576,
            max_output_tokens=393_216, adapter_max_output_tokens=512,
        )
        provider.descriptor = deepseek
        planner = LLMPlanner()
        planner.plan(provider, "Explain a synthetic multi-step task", requested_mode="auto")
        self.assertEqual(512, provider.calls[0][1])
        self.assertEqual(512, planner.last_request_diagnostics["requested_output_tokens"])

        provider = FakePlanningProvider(encoded(plan_payload("general")))
        provider.descriptor = replace(deepseek, adapter_max_output_tokens=None)
        planner = LLMPlanner()
        planner.plan(provider, "Explain a synthetic multi-step task", requested_mode="auto")
        self.assertEqual(4_096, provider.calls[0][1])

        provider = FakePlanningProvider(encoded(plan_payload("general")))
        provider.descriptor = replace(LOCAL_QWEN, adapter_max_output_tokens=192)
        planner = LLMPlanner()
        planner.plan(provider, "Explain attention", requested_mode="auto")
        self.assertEqual(192, provider.calls[0][1])
        self.assertEqual(192, planner.last_request_diagnostics["requested_output_tokens"])

    def test_invalid_model_output_cap_fails_before_planner_request(self):
        provider = FakePlanningProvider(encoded(plan_payload("general")))
        provider.descriptor = replace(LOCAL_QWEN, adapter_max_output_tokens=True)
        with self.assertRaisesRegex(PlanningError, "invalid output capability"):
            LLMPlanner().plan(provider, "Explain attention", requested_mode="auto")
        self.assertEqual([], provider.calls)

    def test_prompt_requires_a_general_task_even_for_direct_answers(self):
        provider = FakePlanningProvider(encoded(plan_payload("general")))
        LLMPlanner().plan(provider, "Explain attention", requested_mode="auto")
        system_prompt = provider.calls[0][0][0]["content"]
        self.assertIn("Use 1-6 tasks", system_prompt)
        self.assertIn("direct answers/clarifications use a general task", system_prompt)

    def test_prompt_states_the_reason_field_character_limit(self):
        provider = FakePlanningProvider(encoded(plan_payload("general")))
        LLMPlanner().plan(provider, "Explain attention", requested_mode="auto")
        system_prompt = provider.calls[0][0][0]["content"]
        self.assertIn("reason:non-empty, at most 280 characters", system_prompt)

    def test_prompt_states_bounded_search_query_requirements(self):
        provider = FakePlanningProvider(encoded(plan_payload("general")))
        LLMPlanner().plan(provider, "Explain attention", requested_mode="auto")
        system_prompt = provider.calls[0][0][0]["content"]
        self.assertIn("1-3 non-empty queries (<=512 chars each)", system_prompt)
        self.assertIn("<=512 chars each", system_prompt)

    def test_drops_old_history_before_rejecting_a_too_large_current_request(self):
        provider = FakePlanningProvider(encoded(plan_payload("general")))
        history = [{"role": "user", "content": "old " * 3000},
                   {"role": "assistant", "content": "older " * 3000}]
        plan = LLMPlanner().plan(provider, "Explain attention", history=history)
        messages = provider.calls[0][0]
        self.assertEqual("user", messages[-1]["role"])
        self.assertLess(len(messages), 4)
        self.assertEqual("general", plan.intent)

    def test_invalid_output_or_provider_failure_does_not_fallback(self):
        for provider in (FakePlanningProvider("{}"),
                         FakePlanningProvider(error=TimeoutError("private request text"))):
            with self.subTest(provider=provider), self.assertRaises(PlanningError) as raised:
                LLMPlanner().plan(provider, "Explain attention")
            self.assertNotIn("private request text", str(raised.exception))
            self.assertEqual(1, len(provider.calls))

    def test_missing_model_budget_metadata_stops_before_model_call(self):
        provider = FakePlanningProvider(encoded(plan_payload("general")))
        provider.descriptor = None
        with self.assertRaises(PlanningError):
            LLMPlanner().plan(provider, "Explain attention")
        self.assertEqual([], provider.calls)

    def test_planner_receives_only_approved_scoped_source_linked_memory(self):
        from agentforge.conversation_context import MemoryCandidate, MemorySourceReference

        source = MemorySourceReference("message-7", 7, "user", "a" * 64)
        approved = MemoryCandidate("repo-a", "Prefer parameter list before formula.", True,
            "memory-a", "preference", 2, (source,), "repository")
        unapproved = MemoryCandidate("repo-a", "pending preference", False,
            "memory-b", "preference", 1, (source,), "repository")
        cross_repo = MemoryCandidate("repo-b", "private preference", True,
            "memory-c", "preference", 1, (source,), "repository")
        provider = FakePlanningProvider(encoded(plan_payload("general")))
        planner = LLMPlanner()
        planner.plan(provider, "Ignore old format; answer as a short sentence.",
            requested_mode="auto", memory_context=(approved, unapproved, cross_repo),
            repository_id="repo-a", memory_status={"status": "available", "candidate_count": 3})
        messages = provider.calls[0][0]
        payload = json.loads(messages[-1]["content"])
        self.assertEqual("Ignore old format; answer as a short sentence.", payload["question"])
        self.assertEqual(["memory-a"], [item["memory_id"] for item in payload["memory_context"]])
        self.assertEqual("message-7", payload["memory_context"][0]["sources"][0]["message_id"])
        self.assertIn("current question overrides memory", messages[0]["content"])
        self.assertEqual("injected", planner.last_request_diagnostics["memory"]["status"])

    def test_planner_drops_optional_memory_before_failing_context_budget(self):
        from agentforge.context_assembler import ContextAssembler
        from agentforge.conversation_context import ContextBudgetExceeded, MemoryCandidate, MemorySourceReference

        class BudgetAssembler(ContextAssembler):
            def __init__(self):
                super().__init__()
                self.payloads = []
            def fit_messages(self, messages, **_kwargs):
                self.payloads.append(messages[-1]["content"])
                if len(self.payloads) == 1:
                    raise ContextBudgetExceeded({"budget_unit": "tokens", "required_tokens": 5000,
                        "budget_tokens": 3000, "estimate_kind": "synthetic test estimate"})
                return list(messages), {"phase": "conversation_planner", "estimated_input_tokens": 20}

        candidate = MemoryCandidate("repo-a", "synthetic preference", True, "memory-a", "preference", 1,
            (MemorySourceReference("message-1", 1, "user", "b" * 64),), "repository")
        provider = FakePlanningProvider(encoded(plan_payload("general")))
        provider.context_assembler = BudgetAssembler()
        planner = LLMPlanner()
        planner.plan(provider, "Explain this", memory_context=(candidate,), repository_id="repo-a")
        self.assertIn("synthetic preference", provider.context_assembler.payloads[0])
        self.assertNotIn("synthetic preference", provider.calls[0][0][-1]["content"])
        self.assertEqual("omitted_for_context_budget", planner.last_request_diagnostics["memory"]["status"])
        self.assertTrue(planner.last_request_diagnostics["memory"]["fallback_used"])

    def test_unavailable_memory_is_reported_without_blocking_planning(self):
        provider = FakePlanningProvider(encoded(plan_payload("general")))
        planner = LLMPlanner()
        result = planner.plan(provider, "Explain attention", memory_context=(), repository_id="repo-a",
                              memory_status={"status": "unavailable", "error_type": "ConnectionError"})
        self.assertEqual("general", result.intent)
        self.assertEqual("unavailable", planner.last_request_diagnostics["memory"]["status"])
        self.assertEqual("ConnectionError", planner.last_request_diagnostics["memory"]["error_type"])
        self.assertEqual([], json.loads(provider.calls[0][0][-1]["content"])["memory_context"])


class PlannerOrchestratorFlowTests(unittest.TestCase):
    class Provider(FakePlanningProvider):
        def __init__(self, response):
            super().__init__(response)
            self.provider_id = "llama-server"
            self.model = LOCAL_QWEN.model_id
            self.answer_calls = 0
            self.answer_context = None
            self.answer_kwargs = None

        def answer_general(self, context, **kwargs):
            from agentforge.models import Answer
            self.answer_calls += 1
            self.answer_context = context
            self.answer_kwargs = kwargs
            return Answer("Attention assigns weights to token representations.", (), False)

        def request_stats(self):
            return {}

    def test_auto_general_plan_skips_repository_and_reaches_answer_context(self):
        from agentforge.conversation_orchestrator import (
            ConversationDependencies, ConversationOrchestrator, ConversationRequest,
        )

        payload = plan_payload("general")
        payload["tasks"][0]["question"] = "Explain attention weights"
        provider = self.Provider(encoded(payload))
        retriever_factory = Mock(side_effect=AssertionError("General must not construct repository retrieval"))
        dependencies = ConversationDependencies(
            provider_factory=Mock(return_value=provider),
            model_budget_resolver=lambda *args, **kwargs: {
                "input_budget": 3000, "output_reserve": 256, "safety_margin": 128,
                "context_window": 4096, "estimate_kind": "estimated",
            },
            retriever_factory=retriever_factory,
        )
        result = ConversationOrchestrator(dependencies).run(
            ConversationRequest("What are attention weights?", "", "llama-server", mode="auto"),
        )
        self.assertEqual("general", result.retrieval["mode"])
        self.assertEqual(1, provider.answer_calls)
        self.assertEqual("Explain attention weights", provider.answer_context.planned_tasks[0]["question"])
        retriever_factory.assert_not_called()

    def test_default_conversation_request_uses_llm_planner_for_intent_routing(self):
        from agentforge.conversation_orchestrator import (
            ConversationDependencies, ConversationOrchestrator, ConversationRequest,
        )

        provider = self.Provider(encoded(plan_payload("general")))
        request = ConversationRequest("Explain attention", "", "llama-server")
        orchestrator = ConversationOrchestrator(ConversationDependencies(
            provider_factory=Mock(return_value=provider),
            model_budget_resolver=lambda *args, **kwargs: {
                "input_budget": 3000, "output_reserve": 256, "safety_margin": 128,
                "context_window": 4096, "estimate_kind": "estimated",
            },
        ))

        result = orchestrator.run(request)

        self.assertEqual("auto", request.mode)
        self.assertEqual("general", result.retrieval["mode"])
        self.assertEqual(1, len(provider.calls))
        self.assertEqual(1, provider.answer_calls)

    def test_orchestrator_tells_planner_when_agent_execution_is_available(self):
        from agentforge.conversation_orchestrator import (
            ConversationDependencies, ConversationOrchestrator, ConversationRequest,
        )

        provider = self.Provider(encoded(plan_payload("general")))
        tool = {"tool_id": "synthetic_action",
                "description": "Perform a bounded in-memory synthetic action.",
                "side_effect": True, "requires_approval": True}

        class CatalogExecutor:
            def execution_available_for(self, _request):
                return True

            def planner_tool_catalog_for(self, _request, **_kwargs):
                return (tool,)

        executor = CatalogExecutor()
        orchestrator = ConversationOrchestrator(ConversationDependencies(
            provider_factory=Mock(return_value=provider),
            model_budget_resolver=lambda *args, **kwargs: {
                "input_budget": 3000, "output_reserve": 256, "safety_margin": 128,
                "context_window": 4096, "estimate_kind": "estimated",
            },
            agent_executor=executor,
        ))

        orchestrator.plan(ConversationRequest("Explain attention", "repo", "llama-server"))

        self.assertIn("Use only listed tools and exact tool_id values",
                      provider.calls[0][0][0]["content"])
        payload = json.loads(provider.calls[0][0][-1]["content"])
        self.assertEqual("synthetic_action", payload["available_tools"][0]["tool_id"])

    def test_invalid_corrected_plan_never_enters_agent_executor(self):
        from agentforge.conversation_orchestrator import (
            ConversationDependencies, ConversationOrchestrator, ConversationRequest,
        )

        invalid = plan_payload("repository")
        invalid["intent"] = "hybrid"
        invalid_again = plan_payload("general")
        invalid_again["intent"] = "hybrid"
        provider = self.Provider([encoded(invalid), encoded(invalid_again)])
        executor = Mock()
        orchestrator = ConversationOrchestrator(ConversationDependencies(
            provider_factory=Mock(return_value=provider),
            model_budget_resolver=lambda *args, **kwargs: {
                "input_budget": 3000, "output_reserve": 256, "safety_margin": 128,
                "context_window": 4096, "estimate_kind": "estimated",
            },
            agent_executor=executor,
        ))

        with self.assertRaises(PlanningError):
            orchestrator.run(ConversationRequest(
                "Inspect this repository", "synthetic", "llama-server", mode="auto",
            ))

        self.assertEqual(2, len(provider.calls))
        executor.execute.assert_not_called()
        self.assertEqual(0, provider.answer_calls)

    def test_invalid_retrieval_flag_correction_never_enters_agent_executor(self):
        from agentforge.conversation_orchestrator import (
            ConversationDependencies, ConversationOrchestrator, ConversationRequest,
        )

        invalid = plan_payload("general")
        invalid["tasks"][0]["requires_retrieval"] = True
        provider = self.Provider([encoded(invalid), encoded(invalid)])
        executor = Mock()
        orchestrator = ConversationOrchestrator(ConversationDependencies(
            provider_factory=Mock(return_value=provider),
            model_budget_resolver=lambda *args, **kwargs: {
                "input_budget": 3000, "output_reserve": 256, "safety_margin": 128,
                "context_window": 4096, "estimate_kind": "estimated",
            },
            agent_executor=executor,
        ))

        with self.assertRaises(PlanningError) as raised:
            orchestrator.run(ConversationRequest(
                "Read a synthetic fact", "synthetic", "llama-server", mode="auto",
            ))

        self.assertEqual("task_retrieval_type_mismatch", raised.exception.diagnostic_code)
        self.assertEqual(2, len(provider.calls))
        executor.execute.assert_not_called()
        self.assertEqual(0, provider.answer_calls)

    def test_cancellation_during_intent_correction_stops_before_agent_execution(self):
        from agentforge.conversation_orchestrator import (
            ConversationDependencies, ConversationOrchestrator, ConversationRequest,
        )
        from agentforge.streaming import ConversationCancelled

        cancelled = threading.Event()
        invalid = plan_payload("repository")
        invalid["intent"] = "hybrid"
        valid = plan_payload("hybrid")

        class CancelDuringCorrection(self.Provider):
            def plan_completion(self, messages, **kwargs):
                response = super().plan_completion(messages, **kwargs)
                if len(self.calls) == 2:
                    cancelled.set()
                return response

        provider = CancelDuringCorrection([encoded(invalid), encoded(valid)])
        executor = Mock()
        orchestrator = ConversationOrchestrator(ConversationDependencies(
            provider_factory=Mock(return_value=provider),
            model_budget_resolver=lambda *args, **kwargs: {
                "input_budget": 3000, "output_reserve": 256, "safety_margin": 128,
                "context_window": 4096, "estimate_kind": "estimated",
            },
            agent_executor=executor,
        ))

        with self.assertRaises(ConversationCancelled):
            orchestrator.run(
                ConversationRequest("Inspect this repository", "synthetic", "llama-server",
                                    mode="auto"),
                cancel_event=cancelled,
            )

        self.assertEqual(2, len(provider.calls))
        executor.execute.assert_not_called()

    def test_orchestrator_passes_scoped_memory_to_the_routing_planner(self):
        from agentforge.conversation_context import MemoryCandidate, MemorySourceReference
        from agentforge.conversation_orchestrator import (
            ConversationDependencies, ConversationOrchestrator, ConversationRequest,
            ConversationSession,
        )

        payload = plan_payload("general")
        provider = self.Provider(encoded(payload))
        source = MemorySourceReference("message-4", 4, "user", "c" * 64)
        approved = MemoryCandidate("repo-a", "Prefer concise explanations.", True,
            "memory-a", "preference", 1, (source,), "repository")
        pending = MemoryCandidate("repo-a", "Pending instructions.", False,
            "memory-pending", "preference", 1, (source,), "repository")
        other_repository = MemoryCandidate("repo-b", "Private other repository fact.", True,
            "memory-b", "project_fact", 1, (source,), "repository")
        orchestrator = ConversationOrchestrator(ConversationDependencies(
            provider_factory=Mock(return_value=provider),
            model_budget_resolver=lambda *args, **kwargs: {
                "input_budget": 3000, "output_reserve": 256, "safety_margin": 128,
                "context_window": 4096, "estimate_kind": "estimated",
            },
        ))
        session = ConversationSession(provider=provider, repository_id="repo-a", conversation={
            "repository_id": "repo-a", "memories": (approved, pending, other_repository),
            "memory_retrieval": {"status": "available", "candidate_count": 3},
        })

        orchestrator.plan(
            ConversationRequest("What should I keep in mind?", "", "llama-server", mode="auto"),
            session=session,
        )

        payload_sent = json.loads(provider.calls[0][0][-1]["content"])
        self.assertEqual(["memory-a"], [item["memory_id"] for item in payload_sent["memory_context"]])
        self.assertEqual("repo-a", payload_sent["memory_context"][0]["repository_id"])
        self.assertEqual("injected", session.phase_diagnostics["planner"]["memory"]["status"])

    def test_external_task_is_marked_unavailable_without_repository_retrieval(self):
        from agentforge.conversation_orchestrator import (
            ConversationDependencies, ConversationOrchestrator, ConversationRequest,
        )

        payload = plan_payload("general", external=True)
        provider = self.Provider(encoded(payload))
        retriever_factory = Mock(side_effect=AssertionError("External-only must not retrieve a repository"))
        result = ConversationOrchestrator(ConversationDependencies(
            provider_factory=Mock(return_value=provider),
            model_budget_resolver=lambda *args, **kwargs: {
                "input_budget": 3000, "output_reserve": 256, "safety_margin": 128,
                "context_window": 4096, "estimate_kind": "estimated",
            },
            retriever_factory=retriever_factory,
        )).run(ConversationRequest("What is the weather now?", "", "llama-server", mode="auto"))
        self.assertEqual("llama-server", result.provider_mode)
        self.assertEqual("limited", result.contract.status)
        self.assertEqual(1, provider.answer_calls)
        self.assertTrue(provider.answer_context.planned_tasks)
        self.assertTrue(provider.answer_kwargs["external_data_unavailable"])
        retriever_factory.assert_not_called()


class ProviderPlanningPortTests(unittest.TestCase):
    def test_qwen_plan_port_uses_selected_local_model_and_bounded_request(self):
        client = Mock()
        client.total_timeout = 45.0
        client.chat.return_value = encoded(plan_payload("general"))
        client.request_stats.return_value = {"last_finish_reason": "stop"}
        provider = LlamaServerProvider(client, "qwen-local-test")
        output = provider.plan_completion(
            [{"role": "system", "content": "planner"},
             {"role": "user", "content": "question"}],
            max_output_tokens=128, timeout_seconds=15.0,
        )
        self.assertEqual(client.chat.return_value, output)
        self.assertEqual("qwen-local-test", client.chat.call_args.kwargs["model"])
        self.assertEqual(128, client.chat.call_args.kwargs["max_output_tokens"])
        self.assertEqual(15.0, client.chat.call_args.kwargs["timeout_seconds"])

    def test_planner_provider_ports_keep_preassembled_history_and_summary(self):
        from agentforge.model_registry import ModelDescriptor

        messages = [
            {"role": "system", "content": "Planner instructions"},
            {"role": "user", "content": "Untrusted prior conversation summary: decision A"},
            {"role": "user", "content": "Earlier question"},
            {"role": "assistant", "content": "Earlier answer"},
            {"role": "user", "content": "Current task"},
        ]

        qwen_client = Mock()
        qwen_client.total_timeout = 45.0
        qwen_client.chat.return_value = encoded(plan_payload("general"))
        qwen_client.request_stats.return_value = {"last_finish_reason": "stop"}
        qwen = LlamaServerProvider(qwen_client, "qwen-local-test")
        qwen.plan_completion(messages, max_output_tokens=128, timeout_seconds=15.0)
        self.assertEqual(messages, qwen_client.chat.call_args.args[0])

        descriptor = ModelDescriptor(
            provider_id="deepseek", model_id="deepseek-test", display_name="synthetic",
            model_type="cloud", context_window=8192, max_output_tokens=512,
            supports_streaming=True, supports_thinking=True,
        )
        deepseek_client = DeepSeekClient("synthetic-test-key")
        deepseek_client.chat = Mock(return_value=DeepSeekCompletion(
            encoded(plan_payload("general")), None, "stop", (), {}, None,
        ))
        deepseek = DeepSeekProvider(deepseek_client, "deepseek-test", descriptor=descriptor)
        deepseek.plan_completion(messages, max_output_tokens=128, timeout_seconds=15.0)
        self.assertEqual(messages, deepseek_client.chat.call_args.args[0])

    def test_planner_provider_port_rejects_oversized_preassembled_context_before_send(self):
        from agentforge.conversation_context import ContextBudgetExceeded

        client = Mock()
        client.total_timeout = 45.0
        client.request_stats.return_value = {"last_finish_reason": "stop"}
        provider = LlamaServerProvider(client, "qwen-local-test")
        messages = [
            {"role": "system", "content": "planner"},
            {"role": "user", "content": "synthetic-history " * 4000},
            {"role": "user", "content": "current task"},
        ]

        with self.assertRaises(ContextBudgetExceeded):
            provider.plan_completion(messages, max_output_tokens=128, timeout_seconds=15.0)
        client.chat.assert_not_called()

    def test_qwen_plan_port_rejects_unverified_or_truncated_completion(self):
        for finish_reason in (None, "length"):
            with self.subTest(finish_reason=finish_reason):
                client = Mock()
                client.total_timeout = 45.0
                client.chat.return_value = encoded(plan_payload("general"))
                client.request_stats.return_value = {"last_finish_reason": finish_reason}
                provider = LlamaServerProvider(client, "qwen-local-test")
                with self.assertRaisesRegex(Exception, "did not complete"):
                    provider.plan_completion(
                        [{"role": "system", "content": "planner"},
                         {"role": "user", "content": "question"}],
                        max_output_tokens=128, timeout_seconds=15.0,
                    )

    def test_planned_subquestions_reach_both_provider_modes_without_becoming_evidence(self):
        from agentforge.models import BuiltContext
        from agentforge.providers.dialogue_protocol import with_planner_task_outline

        task = {"task_id": "t1", "task_type": "general",
                "question": "Explain FIFO", "dependencies": ()}
        context = BuiltContext("Explain FIFO and the project implementation.", (), "", 0, 1000,
                               planned_tasks=(task,))
        user = with_planner_task_outline(context, "Question:\n" + context.question)
        self.assertIn('"question":"Explain FIFO"', user)
        self.assertIn("untrusted data", user)
        self.assertEqual((), context.evidence)

    def test_deepseek_plan_port_disables_thinking_tools_and_streaming(self):
        from agentforge.model_registry import ModelDescriptor

        descriptor = ModelDescriptor(
            provider_id="deepseek", model_id="synthetic", display_name="synthetic",
            model_type="cloud", context_window=8192, max_output_tokens=512,
            supports_streaming=True, supports_thinking=True,
        )
        client = DeepSeekClient("synthetic-key", total_timeout=50.0)
        client.chat = Mock(return_value=DeepSeekCompletion(
            encoded(plan_payload("general")), None, "stop", (), {}, None,
        ))
        provider = DeepSeekProvider(client, "synthetic", descriptor=descriptor)
        provider.plan_completion(
            [{"role": "system", "content": "planner"},
             {"role": "user", "content": "question"}],
            max_output_tokens=128, timeout_seconds=12.0,
        )
        kwargs = client.chat.call_args.kwargs
        self.assertFalse(kwargs["thinking"])
        self.assertFalse(kwargs["stream"])
        self.assertEqual((), kwargs["tools"] if kwargs.get("tools") else ())
        self.assertEqual(12.0, kwargs["timeout_seconds"])

    def test_deepseek_planner_rejects_tool_calls_and_non_stop_finish(self):
        from agentforge.model_registry import ModelDescriptor

        descriptor = ModelDescriptor(
            provider_id="deepseek", model_id="synthetic", display_name="synthetic",
            model_type="cloud", context_window=8192, max_output_tokens=512,
            supports_streaming=True, supports_thinking=True,
        )
        for completion in (
            DeepSeekCompletion("{}", None, "tool_calls", ({"name": "x"},), {}, None),
            DeepSeekCompletion("{}", None, "length", (), {}, None),
        ):
            with self.subTest(completion=completion):
                client = DeepSeekClient("synthetic-key")
                client.chat = Mock(return_value=completion)
                provider = DeepSeekProvider(client, "synthetic", descriptor=descriptor)
                with self.assertRaises(Exception):
                    provider.plan_completion(
                        [{"role": "system", "content": "planner"},
                         {"role": "user", "content": "question"}],
                        max_output_tokens=128, timeout_seconds=12.0,
                    )


class PlannerCoverageBehaviorTests(unittest.TestCase):
    def test_plan_parser_accepts_fenced_json_and_rejects_task_shape_flag_and_reason_errors(self):
        fenced = "```json\n" + encoded(plan_payload("general")) + "\n```"
        parsed = parse_conversation_plan(fenced, requested_mode="auto")
        self.assertEqual("general", parsed.intent)
        self.assertEqual("t1", parsed.tasks[0].task_id)

        bad_task = plan_payload("general")
        bad_task["tasks"][0]["extra"] = "ignored fields are not accepted"
        with self.assertRaisesRegex(PlanningError, "task does not match"):
            parse_conversation_plan(encoded(bad_task), requested_mode="auto")

        bad_flag = plan_payload("general")
        bad_flag["requires_repository"] = "false"
        with self.assertRaisesRegex(PlanningError, "capability flags"):
            parse_conversation_plan(encoded(bad_flag), requested_mode="auto")

        bad_reason = plan_payload("general")
        bad_reason["reason"] = None
        with self.assertRaisesRegex(PlanningError, "reason must be a string"):
            parse_conversation_plan(encoded(bad_reason), requested_mode="auto")

    def test_planner_rejects_invalid_tool_metadata_and_unavailable_execution_service(self):
        provider = FakePlanningProvider(encoded(plan_payload("general")))
        invalid_tool = {
            "tool_id": "lookup", "description": "read synthetic data",
            "side_effect": "false", "requires_approval": False,
        }
        with self.assertRaisesRegex(PlanningError, "invalid metadata"):
            LLMPlanner().plan(provider, "look up synthetic data",
                agent_execution_available=True, available_tools=(invalid_tool,))
        self.assertEqual([], provider.calls)

        safe_tool = {**invalid_tool, "side_effect": False}
        with self.assertRaisesRegex(PlanningError, "execution is unavailable"):
            LLMPlanner().plan(provider, "look up synthetic data",
                agent_execution_available=False, available_tools=(safe_tool,))
        self.assertEqual([], provider.calls)

    def test_planner_preflight_rejects_missing_capability_and_empty_input_budget(self):
        class NoPlanningMethod:
            descriptor = LOCAL_QWEN

        class NoVerifiedDescriptor:
            descriptor = None
            def plan_completion(self, *_args, **_kwargs):
                self.fail("provider must not be called without verified model metadata")

        for provider, kwargs, message in (
            (NoPlanningMethod(), {}, "planning capability"),
            (NoVerifiedDescriptor(), {}, "verified model budget metadata"),
            (FakePlanningProvider(encoded(plan_payload("general"))), {"input_budget": 0}, "No input budget"),
        ):
            with self.subTest(message=message), self.assertRaisesRegex(PlanningError, message):
                LLMPlanner().plan(provider, "Explain the synthetic example", **kwargs)

    def test_planner_sanitizes_context_assembly_failure_before_model_request(self):
        from agentforge.context_assembler import ContextAssembler

        class BrokenAssembler(ContextAssembler):
            def fit_messages(self, *_args, **_kwargs):
                raise ValueError("synthetic invalid context settings")

        provider = FakePlanningProvider(encoded(plan_payload("general")))
        provider.context_assembler = BrokenAssembler()
        with self.assertRaisesRegex(PlanningError, r"context could not be assembled \(ValueError\)"):
            LLMPlanner().plan(provider, "Explain the synthetic example")
        self.assertEqual([], provider.calls)


if __name__ == "__main__":
    unittest.main()
