"""Offline safety checks for the Phase 4.2 unified real-host runner."""

import ast
import hashlib
import os
from pathlib import Path
import sys
import tempfile
import threading
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

import AF042_UNIFIED_AGENT_HOST_ACCEPTANCE as host
import AF042_PENDING_DATABASE_INSPECTION as pending_database


class UnifiedHostRunnerTests(unittest.TestCase):
    def test_dependency_order_does_not_assume_a_total_tool_call_count(self):
        self.assertTrue(host._required_tools_ran_in_order(
            ["open_synthetic_bundle", "read_context", "inspect_synthetic_bundle"],
            ("open_synthetic_bundle", "inspect_synthetic_bundle")))
        self.assertTrue(host._required_tools_ran_in_order(
            ["open_synthetic_bundle", "inspect_synthetic_bundle", "read_more"],
            ("open_synthetic_bundle", "inspect_synthetic_bundle")))
        self.assertFalse(host._required_tools_ran_in_order(
            ["inspect_synthetic_bundle", "open_synthetic_bundle"],
            ("open_synthetic_bundle", "inspect_synthetic_bundle")))
        self.assertFalse(host._required_tools_ran_in_order(
            ["open_synthetic_bundle"],
            ("open_synthetic_bundle", "inspect_synthetic_bundle")))

    def test_single_tool_assertion_allows_repository_retrieval_but_requires_one_synthetic_call(self):
        trace = {
            "execution_status": "completed",
            "tool_calls": [
                {"tool_id": "read_synthetic_fact", "status": "completed"},
                {"tool_id": "search", "status": "completed"},
                {"tool_id": "read", "status": "completed"},
            ],
        }
        self.assertTrue(host._single_synthetic_tool_call_completed(
            trace, "read_synthetic_fact"))
        trace["tool_calls"].append(
            {"tool_id": "read_synthetic_fact", "status": "completed"})
        self.assertFalse(host._single_synthetic_tool_call_completed(
            trace, "read_synthetic_fact"))
        trace["tool_calls"].pop()
        trace["tool_calls"][0]["status"] = "failed"
        self.assertFalse(host._single_synthetic_tool_call_completed(
            trace, "read_synthetic_fact"))

    def test_completed_execution_checkpoint_survives_unrelated_scenario_assertion_failure(self):
        scenario = {
            "status": "FAIL",
            "failure_code": "scenario_assertion_failed",
            "trace": {
                "execution_status": "completed",
                "session_id": "session-one",
                "execution_id": "execution-one",
            },
        }
        self.assertEqual({"conversation_id": "session-one", "execution_id": "execution-one"},
                         host._completed_agent_checkpoint(scenario))
        scenario["trace"]["execution_status"] = "failed"
        self.assertIsNone(host._completed_agent_checkpoint(scenario))

    def test_restored_host_session_exposes_conversation_id_to_context_assembler(self):
        conversation_id = "conversation-restore-1"
        repository_id = "synthetic-repository-1"
        store = SimpleNamespace(get_conversation=lambda *_: {
            "id": conversation_id, "repository_id": repository_id,
        })
        service = SimpleNamespace(load_context=lambda *_: SimpleNamespace(
            rows=(), summary=None,
        ))

        session = host._fresh_session(
            store, service, conversation_id, repository_id, provider=object())

        self.assertEqual(conversation_id, session.conversation["id"])
        self.assertEqual(conversation_id, session.conversation["conversation_id"])
        self.assertEqual(repository_id, session.conversation["repository_id"])

    def test_host_agent_step_budget_matches_product_bound_and_fits_observed_plan(self):
        from agentforge.agent_loop import MAX_AGENT_STEPS

        # The real host trace completed three repository searches before the
        # next valid four-call dependency plan. Keep the host bound sufficient
        # for that plan while inheriting the product's existing hard cap.
        self.assertEqual(MAX_AGENT_STEPS, host.AGENT_EXECUTION_MAX_STEPS)
        self.assertGreaterEqual(host.AGENT_EXECUTION_MAX_STEPS, 7)

    def _provision_result(self):
        result = host._result_template()
        result["cleanup"]["database"] = "pending"
        return result

    def _provisioner_that_fails(self, operation, error, *, confirm_absent=False,
                                confirm_created=False):
        def provisioner(run_id, _password, *, on_stage, on_database_absent, on_created):
            database = host._database_name(run_id)
            on_stage("admin_connection")
            if operation != "admin_connection":
                on_stage("database_existence_check")
            if operation == "database_existence_check":
                raise error
            if confirm_absent:
                on_database_absent(database)
            if operation == "create_database":
                on_stage("create_database")
                raise error
            if confirm_created:
                on_stage("create_database")
                on_created(database)
            on_stage(operation)
            raise error
        return provisioner

    def test_admin_authentication_failure_is_identified_and_redacted(self):
        class PgError(Exception):
            sqlstate = "28P01"

        result = self._provision_result()
        with self.assertRaises(PgError):
            host._provision_database(result, "a" * 32, "secret-pass",
                provisioner=self._provisioner_that_fails(
                    "admin_connection", PgError("password=secret-pass authentication failed")))
        diagnostic = result["database_provision"]["diagnostic"]
        self.assertEqual("admin_connection", diagnostic["operation"])
        self.assertEqual("28P01", diagnostic["sqlstate"])
        self.assertNotIn("secret-pass", str(diagnostic))
        self.assertEqual("not_created", result["cleanup"]["database"])

    def test_create_database_permission_failure_is_not_reported_as_unknown(self):
        class PgError(Exception):
            sqlstate = "42501"

        result = self._provision_result()
        with self.assertRaises(PgError):
            host._provision_database(result, "b" * 32, "unused",
                provisioner=self._provisioner_that_fails(
                    "create_database", PgError("permission denied"), confirm_absent=True))
        self.assertEqual("create_database", result["database_provision"]["diagnostic"]["operation"])
        self.assertEqual("42501", result["database_provision"]["diagnostic"]["sqlstate"])
        self.assertEqual("not_created", result["cleanup"]["database"])

    def test_database_name_collision_is_never_claimed_or_dropped(self):
        result = self._provision_result()
        error = host.AcceptanceBlocked("database_provision", "generated_database_already_exists")
        with self.assertRaises(host.AcceptanceBlocked):
            host._provision_database(result, "c" * 32, "unused",
                provisioner=self._provisioner_that_fails("database_existence_check", error))
        self.assertFalse(result["temporary_resources"]["database_created"])
        self.assertEqual("not_created", result["cleanup"]["database"])
        self.assertEqual("database_existence_check",
                         result["database_provision"]["diagnostic"]["operation"])

    def test_ambiguous_create_disconnect_is_unverified_and_not_dropped(self):
        result = self._provision_result()
        error = OSError("connection dropped after CREATE request")
        with self.assertRaises(OSError):
            host._provision_database(result, "d" * 32, "unused",
                provisioner=self._provisioner_that_fails(
                    "create_database", error, confirm_absent=True))
        self.assertEqual("creation_outcome_unknown", result["database_provision"]["status"])
        self.assertEqual("unverified", host._cleanup_database(result,
            host._database_name("d" * 32), "unused", dropper=lambda *_: self.fail("must not drop")))

        class ConnectionFailure(Exception):
            sqlstate = "08006"

        result_with_sqlstate = self._provision_result()
        with self.assertRaises(ConnectionFailure):
            host._provision_database(result_with_sqlstate, "6" * 32, "unused",
                provisioner=self._provisioner_that_fails(
                    "create_database", ConnectionFailure("connection lost"), confirm_absent=True))
        self.assertEqual("creation_outcome_unknown",
                         result_with_sqlstate["database_provision"]["status"])
        self.assertEqual("unverified", result_with_sqlstate["cleanup"]["database"])

    def test_created_database_is_cleaned_after_later_provision_failure(self):
        result = self._provision_result()
        error = OSError("admin connection close failed")
        with self.assertRaises(OSError):
            host._provision_database(result, "e" * 32, "unused",
                provisioner=self._provisioner_that_fails(
                    "admin_connection_close", error, confirm_absent=True, confirm_created=True))
        self.assertTrue(result["temporary_resources"]["database_created"])
        self.assertTrue(result["database_provision"]["created_by_run"])
        drops = []
        status = host._cleanup_database(result, host._database_name("e" * 32), "unused",
                                        dropper=lambda name, _password: drops.append(name))
        self.assertEqual("removed", status)
        self.assertEqual([host._database_name("e" * 32)], drops)
        self.assertFalse(result["temporary_resources"]["database_created"])
        self.assertTrue(result["database_provision"]["created_by_run"])

    def test_repeated_database_cleanup_is_idempotent(self):
        result = self._provision_result()
        name = host._database_name("f" * 32)
        result["temporary_resources"].update(database_created=True, database_name=name)
        drops = []
        dropper = lambda database, _password: drops.append(database)
        self.assertEqual("removed", host._cleanup_database(result, name, "unused", dropper=dropper))
        self.assertEqual("removed", host._cleanup_database(result, name, "unused", dropper=dropper))
        self.assertEqual([name], drops)

    def test_formal_database_name_is_never_passed_to_dropper(self):
        result = self._provision_result()
        result["temporary_resources"].update(database_created=True, database_name="agentforge")
        called = []
        status = host._cleanup_database(result, "agentforge", "unused",
                                        dropper=lambda *args: called.append(args))
        self.assertEqual("failed", status)
        self.assertEqual([], called)
        self.assertEqual({"code": "refusing_unowned_database"},
                         result["cleanup"]["database_diagnostic"])

    def test_pending_database_inspector_is_exact_run_scoped_and_read_only(self):
        source = Path(pending_database.__file__).read_text(encoding="utf-8")
        self.assertEqual("e52101603777468db6b1ec1739c0d292", pending_database.RUN_ID)
        self.assertEqual("af033_agent_e52101603777468db6b1", pending_database.EXPECTED_DATABASE)
        self.assertIn("requires_provenance_review", source)
        for statement in ("DROP DATABASE", "CREATE DATABASE", "ALTER DATABASE", "pg_terminate_backend"):
            self.assertNotIn(statement, source)

    def _patch_psycopg(self, connection_factory):
        psycopg = ModuleType("psycopg")
        psycopg.connect = connection_factory

        class SqlTemplate:
            def __init__(self, value):
                self.value = value

            def format(self, identifier):
                return self.value.format(str(identifier))

            def __str__(self):
                return self.value

        sql = ModuleType("psycopg.sql")
        sql.SQL = SqlTemplate
        sql.Identifier = lambda value: value
        psycopg.sql = sql
        return patch.dict(sys.modules, {"psycopg": psycopg, "psycopg.sql": sql})

    def test_real_database_helper_reports_create_ack_before_connection_close(self):
        events = []

        class Cursor:
            def fetchone(self):
                return None

        class Connection:
            def execute(self, statement, _params=None):
                events.append("create" if str(statement).startswith("CREATE DATABASE") else "check")
                return Cursor()

            def close(self):
                events.append("close")
                raise OSError("connection close failed")

        result = self._provision_result()
        run_id = "1" * 32
        with self._patch_psycopg(lambda **_kwargs: Connection()):
            with self.assertRaises(OSError):
                host._provision_database(result, run_id, "unused")
        self.assertEqual(["check", "create", "close"], events)
        self.assertTrue(result["temporary_resources"]["database_created"])
        self.assertEqual("created_cleanup_required", result["database_provision"]["status"])
        self.assertEqual("admin_connection_close",
                         result["database_provision"]["diagnostic"]["operation"])

    def test_close_failure_does_not_mask_create_database_permission_error(self):
        class PgError(Exception):
            sqlstate = "42501"

        class Cursor:
            def fetchone(self):
                return None

        class Connection:
            def execute(self, statement, _params=None):
                if str(statement).startswith("CREATE DATABASE"):
                    raise PgError("permission denied")
                return Cursor()

            def close(self):
                raise OSError("secondary close error")

        result = self._provision_result()
        run_id = "5" * 32
        with self._patch_psycopg(lambda **_kwargs: Connection()):
            with self.assertRaises(PgError):
                host._provision_database(result, run_id, "unused")
        diagnostic = result["database_provision"]["diagnostic"]
        self.assertEqual("create_database", diagnostic["operation"])
        self.assertEqual("42501", diagnostic["sqlstate"])
        self.assertEqual("not_created", result["cleanup"]["database"])

    def test_real_database_helper_authentication_failure_records_connect_stage(self):
        class PgError(Exception):
            sqlstate = "28P01"

        result = self._provision_result()
        run_id = "2" * 32
        with self._patch_psycopg(lambda **_kwargs: (_ for _ in ()).throw(PgError("bad password"))):
            with self.assertRaises(PgError):
                host._provision_database(result, run_id, "unused")
        self.assertEqual("admin_connection", result["database_provision"]["diagnostic"]["operation"])
        self.assertEqual("28P01", result["database_provision"]["diagnostic"]["sqlstate"])
        self.assertEqual("not_created", result["cleanup"]["database"])

    def test_real_database_helper_permission_and_existing_database_fail_before_ownership(self):
        class PgError(Exception):
            sqlstate = "42501"

        class Cursor:
            def __init__(self, row):
                self.row = row

            def fetchone(self):
                return self.row

        class Connection:
            def __init__(self, *, exists=False, create_error=None):
                self.exists = exists
                self.create_error = create_error

            def execute(self, statement, _params=None):
                if str(statement).startswith("CREATE DATABASE"):
                    if self.create_error:
                        raise self.create_error
                    return Cursor(None)
                return Cursor((1,) if self.exists else None)

            def close(self):
                pass

        denied = self._provision_result()
        with self._patch_psycopg(lambda **_kwargs: Connection(create_error=PgError("permission denied"))):
            with self.assertRaises(PgError):
                host._provision_database(denied, "3" * 32, "unused")
        self.assertEqual("42501", denied["database_provision"]["diagnostic"]["sqlstate"])
        self.assertEqual("not_created", denied["cleanup"]["database"])

        collision = self._provision_result()
        with self._patch_psycopg(lambda **_kwargs: Connection(exists=True)):
            with self.assertRaises(host.AcceptanceBlocked):
                host._provision_database(collision, "4" * 32, "unused")
        self.assertFalse(collision["temporary_resources"]["database_created"])
        self.assertEqual("not_created", collision["cleanup"]["database"])

    def test_report_starts_with_every_required_case_unmeasured(self):
        result = host._result_template()
        self.assertEqual(set(host.SCENARIOS), set(result["scenarios"]))
        self.assertTrue(all(item["status"] == "NOT_MEASURED"
                            for item in result["scenarios"].values()))
        self.assertEqual(0, result["chat_requests"]["sent"])
        self.assertEqual("NOT_MEASURED_SEPARATE_PHASE", result["provider"]["deepseek"])

    def test_deepseek_host_request_cap_is_separate_from_model_token_budget(self):
        result = host._result_template(("repository_auto",), provider_id="deepseek",
                                       model_id="deepseek-flash")
        self.assertEqual(128, result["request_limit"])
        self.assertEqual(128, host._result_template()["request_limit"])

    def test_environment_gate_stops_before_listener_or_credential_access(self):
        with patch.dict(os.environ, {"CODEX_SANDBOX_NETWORK_DISABLED": "1"}, clear=True), \
                patch.object(host.host_infra, "_safe_listener_probe",
                             side_effect=AssertionError("listener probe must not run")), \
                patch.object(host.getpass, "getpass",
                             side_effect=AssertionError("credential prompt must not run")), \
                patch.object(host.sys, "argv", [str(host.__file__)]), \
                patch.object(host, "_finish", side_effect=lambda result, status, stage, code:
                             (status, stage, code)) as finish:
            outcome = host._main()
        self.assertEqual(("BLOCKED", "environment_gate", "network_disabled_by_environment"), outcome)
        self.assertEqual("BLOCKED", finish.call_args.args[0]["checks"]["host_network"])

    def test_request_cap_counts_calls_and_never_retries(self):
        class Provider:
            descriptor = object()
            context_assembler = object()
            max_output_tokens = 32
            model = "synthetic"
            provider_id = "test"
            client = object()

            def plan_completion(self, *args, **kwargs):
                return "one call"

            def request_stats(self):
                return {"sent": 1, "retries": 0}

        provider = host.CappedConversationProvider(Provider(), limit=1)
        self.assertEqual("one call", provider.plan_completion([], max_output_tokens=1,
                                                               timeout_seconds=1))
        with self.assertRaises(host.AcceptanceBlocked) as raised:
            provider.answer(None)
        self.assertEqual("request_budget", raised.exception.stage)
        self.assertEqual(1, provider.requests_attempted)

    def test_per_request_usage_is_aggregated_without_prompt_content(self):
        requests = [
            {"usage": {"prompt_tokens": 20, "completion_tokens": 5,
                       "total_tokens": 25, "raw_prompt": "must not persist"},
             "operation": "plan_completion"},
            {"usage": {"prompt_tokens": 30, "completion_tokens": 7,
                       "total_tokens": 37}, "operation": "answer"},
            {"usage": None, "operation": "failed_without_usage"},
        ]
        summary = host._usage_summary(requests)
        self.assertEqual(2, summary["requests_with_usage"])
        self.assertEqual(50, summary["totals"]["prompt_tokens"])
        self.assertEqual(12, summary["totals"]["completion_tokens"])
        self.assertNotIn("must not persist", str(summary))

    def test_agent_planner_diagnostic_retains_budget_failure_without_prompt_text(self):
        safe = host._safe_agent_planner_diagnostic({
            "phase": "agent_execution_planner",
            "failure_code": "CONTEXT_BUDGET_EXCEEDED",
            "required_tokens": 4_500,
            "budget_tokens": 3_000,
            "components_tokens": {"assembled_request": 4_500, "unsafe key": 999},
            "request_sent": False,
            "planner_catalog_tool_count": 2,
            "planner_schema_count": 1,
            "planner_schema_tool_ids": ["search", "read tool schema"],
            "planner_payload_utf8_bytes": 12_345,
            "exception_type": "ProviderError",
            "request_attempted_delta": 0,
            "provider_failure_code": "incomplete_planning_completion",
            "provider_failure_phase": "planning_response",
            "response_shape": {"content_chars": 512, "response_text": "private response"},
            "prompt": "synthetic-private-prompt",
            "exception": "synthetic-private-error",
        })
        self.assertEqual("CONTEXT_BUDGET_EXCEEDED", safe["failure_code"])
        self.assertEqual(4_500, safe["required_tokens"])
        self.assertEqual(3_000, safe["budget_tokens"])
        self.assertEqual({"assembled_request": 4_500}, safe["components_tokens"])
        self.assertFalse(safe["request_sent"])
        self.assertEqual(2, safe["planner_catalog_tool_count"])
        self.assertEqual(1, safe["planner_schema_count"])
        self.assertEqual(["search"], safe["planner_schema_tool_ids"])
        self.assertEqual(12_345, safe["planner_payload_utf8_bytes"])
        self.assertEqual("ProviderError", safe["exception_type"])
        self.assertEqual(0, safe["request_attempted_delta"])
        self.assertEqual("incomplete_planning_completion", safe["provider_failure_code"])
        self.assertEqual({"content_chars": 512}, safe["response_shape"])
        self.assertNotIn("response_text", str(safe))
        self.assertNotIn("synthetic-private", str(safe))

    def test_agent_planner_diagnostic_retains_undiscovered_tool_id_without_prompt_text(self):
        safe = host._safe_agent_planner_diagnostic({
            "phase": "agent_execution_planner",
            "planning_failure_phase": "schema_discovery",
            "planning_failure_code": "TOOL_NOT_FOUND",
            "planning_failure_reason": "undiscovered_selected_tool",
            "planning_failure_details": {
                "action": "plan", "undiscovered_tool_ids": ["synthetic_action"],
                "loaded_schema_tool_ids": ["read"], "model_output": "private output",
            },
        })
        self.assertEqual(["synthetic_action"], safe["planning_failure_details"]["undiscovered_tool_ids"])
        self.assertEqual(["read"], safe["planning_failure_details"]["loaded_schema_tool_ids"])
        self.assertNotIn("private output", str(safe))

    def test_scenario_failure_diagnostic_keeps_citation_ids_but_never_answer_snippet(self):
        from agentforge.errors import CitationValidationError

        error = CitationValidationError("invalid source id", code="unknown_source_id", diagnostic={
            "rule_id": "unknown_source_id", "matched_source_ids": ("E9", "E9"),
            "validated_source_ids": ("E1",), "validation_result": "rejected",
            "snippet": "private answer fragment",
        })
        safe = host._safe_scenario_failure_diagnostic(
            error, SimpleNamespace(request_stats=lambda: {}))

        self.assertEqual({
            "rule_id": "unknown_source_id", "matched_source_ids": ["E9"],
            "validated_source_ids": ["E1"], "validation_result": "rejected",
        }, safe["citation_validation"])
        self.assertNotIn("private answer fragment", str(safe))
        self.assertNotIn("snippet", safe["citation_validation"])

    def test_host_failure_diagnostic_preserves_safe_plan_route_and_tool_catalog(self):
        safe = host._safe_host_failure_diagnostics({
            "agent_planners": [{"decisions": [{
                "planner_round": 2, "catalog_tool_ids": ["read", "search"],
                "selected_calls": [], "done": False,
                "failure": {"code": "TOOL_NOT_FOUND", "phase": "schema_discovery",
                    "reason": "undiscovered_selected_tool",
                    "details": {"undiscovered_tool_ids": ["synthetic_action"],
                                "model_text": "private planner response"}},
            }]}],
            "events": [{"type": "PLAN_READY", "sequence": 2, "mode": "repository",
                        "task_count": 1, "requires_agent_execution": True}],
        })

        planner = safe["agent_planners"][0]["decisions"][0]
        self.assertEqual(["read", "search"], planner["catalog_tool_ids"])
        self.assertEqual(["synthetic_action"],
                         planner["failure"]["details"]["undiscovered_tool_ids"])
        self.assertEqual({"type": "PLAN_READY", "sequence": 2, "mode": "repository",
                          "task_count": 1, "requires_agent_execution": True},
                         safe["event_metadata"][0])
        self.assertNotIn("private planner response", str(safe))

    def test_successful_decision_sanitizer_retains_only_catalog_tool_ids(self):
        safe = host._sanitize_decisions([{
            "planner_round": 1, "observation_count": 0, "observations": [],
            "selected_calls": [], "catalog_tool_ids": ["read", "search"],
            "private": "must not persist", "done": True,
        }])
        self.assertEqual(["read", "search"], safe[0]["catalog_tool_ids"])
        self.assertNotIn("must not persist", str(safe))

    def test_schema_request_failure_diagnostic_retains_counts_without_tool_content(self):
        safe = host._safe_agent_planner_diagnostic({
            "phase": "agent_execution_planner",
            "planning_failure_phase": "schema_discovery",
            "planning_failure_code": "INVALID_ARGUMENTS",
            "planning_failure_reason": "invalid_schema_set",
            "planning_failure_details": {
                "action": "load_schemas", "requested_count": 5, "unique_count": 5,
                "duplicate_count": 0, "catalog_count": 5, "schema_limit": 8,
                "empty_set": False, "over_limit": False,
                "tool_ids": ["synthetic-private-tool-name"],
            },
        })
        self.assertEqual({
            "action": "load_schemas", "requested_count": 5, "unique_count": 5,
            "duplicate_count": 0, "catalog_count": 5, "schema_limit": 8,
            "empty_set": False, "over_limit": False,
        }, safe["planning_failure_details"])
        self.assertNotIn("synthetic-private-tool-name", str(safe))

    def test_agent_replan_failure_records_exact_safe_validation_reason(self):
        from agentforge.agent_execution import LLMExecutionPlanner, ToolResult
        from agentforge.model_types import LOCAL_QWEN

        class Provider:
            descriptor = LOCAL_QWEN
            def plan_completion(self, *_args, **_kwargs):
                return '{"action":"finish","final_answer":"private model response"}'
            def request_stats(self):
                return {"sent": 1, "succeeded": 1, "failed": 0, "retries": 0,
                        "last_finish_reason": "stop",
                        "last_usage": {"prompt_tokens": 12, "completion_tokens": 5},
                        "last_response_diagnostic": {"content_chars": 68, "content_type": "str"}}

        planner = LLMExecutionPlanner(Provider(), tools_only=True)
        recorder = host.host_infra.RecordingRealPlanner(planner, scenario="repository_auto")
        observation = ToolResult("synthetic-call", "synthetic-task", "synthetic_read",
                                 "completed", {"fact": "synthetic"})
        with self.assertRaises(ValueError):
            recorder.plan("inspect the observation", (observation,), (), lambda _tool_id: {})

        decisions = host._sanitize_decisions(recorder.decisions)
        self.assertEqual(1, decisions[0]["observation_count"])
        self.assertEqual({"code": "INVALID_ARGUMENTS", "phase": "response_validation",
                          "reason": "action_fields_unexpected",
                          "details": {"action": "finish", "missing_fields": [],
                                      "unexpected_fields": ["final_answer"],
                                      "other_unexpected_field_count": 0}}, decisions[0]["failure"])
        self.assertNotIn("private model response", repr(decisions))
        diagnostic = host._safe_agent_planner_diagnostic(planner.last_request_diagnostics)
        self.assertEqual("rejected", diagnostic["response_validation"])
        self.assertEqual("action_fields_unexpected", diagnostic["planning_failure_reason"])
        self.assertEqual(["final_answer"], diagnostic["planning_failure_details"]["unexpected_fields"])
        attempts, successful = host._replanning_counts([{"type": "REPLANNING"},
            {"type": "PLANNER_STARTED"}, {"type": "PLAN_REJECTED"}])
        self.assertEqual((1, 0), (attempts, successful))

    def test_replanning_counts_only_engine_accepted_plans_as_successful(self):
        events = [
            {"type": "REPLANNING"}, {"type": "PLANNER_STARTED"}, {"type": "PLAN_READY"},
            {"type": "REPLANNING"}, {"type": "PLANNER_STARTED"}, {"type": "PLAN_REJECTED"},
        ]
        self.assertEqual((2, 1), host._replanning_counts(events))

    def test_planner_response_alone_does_not_count_as_successful_replanning(self):
        events = [{"type": "REPLANNING"}, {"type": "PLANNER_STARTED"}]
        self.assertEqual((1, 0), host._replanning_counts(events))

    def test_specific_planner_failure_code_precedes_generic_failed_event(self):
        code = host._agent_failure_code([
            {"type": "PLANNER_FAILED", "error_code": "PROVIDER_ERROR"},
            {"type": "FAILED", "error_code": "FAILED"},
        ])
        self.assertEqual("PROVIDER_ERROR", code)

    def test_plan_validation_audit_row_is_not_reported_as_a_tool_execution(self):
        results = [
            {"call_id": "rejected-plan-call", "tool_id": "open_synthetic_bundle",
             "status": "failed", "error_code": "INVALID_ARGUMENTS",
             "execution_metadata": {"stage": "plan_validation"}},
            {"call_id": "executed-call", "tool_id": "read_synthetic_fact",
             "status": "completed", "execution_metadata": {"elapsed_seconds": 0.01}},
        ]
        self.assertEqual([results[1]], host._execution_tool_results(results))

    def test_rejected_agent_plan_is_not_reported_as_valid(self):
        events = [
            {"type": "PLAN_READY", "sequence": 2},
            {"type": "PLAN_REJECTED", "sequence": 6, "error_code": "duplicate_task_ids"},
        ]
        self.assertEqual("failed", host._plan_validity(events))
        self.assertEqual("passed", host._plan_validity([{"type": "PLAN_READY"}]))
        self.assertEqual("NOT_MEASURED", host._plan_validity([]))

    def test_stream_event_diagnostic_keeps_only_normalized_error_code(self):
        from agentforge.streaming import StreamingEvent, StreamEventType

        event = StreamingEvent(
            StreamEventType.PLANNER_FAILED,
            payload={"error_code": "CONTEXT_BUDGET_EXCEEDED", "prompt": "do not save"},
            execution_id="synthetic-execution",
            sequence=3,
        )
        safe = host._safe_event(event)
        self.assertEqual("CONTEXT_BUDGET_EXCEEDED", safe["error_code"])
        self.assertNotIn("prompt", safe)
        self.assertNotIn("do not save", str(safe))

    def test_failed_agent_turn_does_not_report_missing_contract_or_messages_as_pass(self):
        from types import SimpleNamespace

        failed = SimpleNamespace(execution_status="failed", contract=None)
        assertions = host._lifecycle_assertions(failed, {"persisted_message_count": 0})
        self.assertFalse(assertions["answer_contract_observed"])
        self.assertFalse(assertions["session_messages_persisted"])

    def test_expected_non_answer_checkpoint_marks_answer_checks_not_applicable(self):
        from types import SimpleNamespace

        paused = SimpleNamespace(execution_status="waiting_approval", contract=None)
        assertions = host._lifecycle_assertions(paused, {"persisted_message_count": 0})
        self.assertEqual("NOT_APPLICABLE", assertions["answer_contract_observed"])
        self.assertEqual("NOT_APPLICABLE", assertions["session_messages_persisted"])

    def test_unsent_provider_failure_does_not_reuse_previous_usage(self):
        class Provider:
            max_output_tokens = 32

            def __init__(self):
                self.stats = {"sent": 0, "succeeded": 0, "failed": 0, "retries": 0,
                              "last_usage": {"prompt_tokens": 10, "completion_tokens": 2},
                              "last_finish_reason": "stop"}
                self.calls = 0

            def request_stats(self):
                return dict(self.stats)

            def plan_completion(self, *_args, **_kwargs):
                self.calls += 1
                if self.calls == 1:
                    self.stats["sent"] += 1
                    self.stats["succeeded"] += 1
                    return "response"
                raise RuntimeError("failed before transport")

        provider = Provider()
        capped = host.CappedConversationProvider(provider, limit=3)
        capped.plan_completion([], max_output_tokens=1, timeout_seconds=1)
        with self.assertRaises(RuntimeError):
            capped.plan_completion([], max_output_tokens=1, timeout_seconds=1)
        self.assertEqual({"prompt_tokens": 10, "completion_tokens": 2},
                         capped.request_telemetry[0]["usage"])
        self.assertIsNone(capped.request_telemetry[1]["usage"])
        self.assertIsNone(capped.request_telemetry[1]["finish_reason"])

    def test_scenario_failure_diagnostic_preserves_safe_request_and_exception_location(self):
        class Provider:
            def request_stats(self):
                return {"attempted": 1, "sent": 1, "succeeded": 1,
                        "failed": 0, "retries": 0, "last_finish_reason": "stop"}

        def raise_private_value_error():
            raise ValueError("private prompt and API key must not be recorded")

        try:
            raise_private_value_error()
        except ValueError as exc:
            exc.agentforge_host_diagnostics = {
                "request_telemetry": [{
                    "sequence": 3, "operation": "plan_completion", "sent": 1,
                    "succeeded": True, "failed": 0, "retries": 0,
                    "requested_output_tokens": 4096, "http_status": 200,
                    "finish_reason": "stop",
                    "usage": {"prompt_tokens": 40, "completion_tokens": 20,
                              "api_key": "must-not-be-recorded"},
                    "response_shape": {"choice_count": 1, "content_chars": 73,
                                       "raw_content": "must-not-be-recorded"},
                    "prompt": "must-not-be-recorded",
                }],
                "conversation_planner": {
                    "input_budget_tokens": 900_000,
                    "components_tokens": {"question": 12, "prompt": 34,
                                          "raw": "must-not-be-recorded"},
                    "raw_response": "must-not-be-recorded",
                },
                "events": [{"type": "PLAN_REJECTED", "error_code": "invalid_plan",
                            "payload": "must-not-be-recorded"}],
            }
            diagnostic = host._safe_scenario_failure_diagnostic(exc, Provider())

        self.assertEqual("ValueError", diagnostic["exception_type"])
        self.assertEqual("raise_private_value_error",
                         diagnostic["exception_location"]["function"])
        row = diagnostic["host_diagnostics"]["request_telemetry"][0]
        self.assertEqual(4096, row["requested_output_tokens"])
        self.assertEqual("stop", row["finish_reason"])
        self.assertEqual(20, row["usage"]["completion_tokens"])
        self.assertEqual(73, row["response_shape"]["content_chars"])
        self.assertEqual({"question": 12, "prompt": 34},
                         diagnostic["host_diagnostics"]["conversation_planner"]
                         ["components_tokens"])
        self.assertNotIn("must-not-be-recorded", str(diagnostic))
        self.assertNotIn("private prompt", str(diagnostic))

    def test_targeted_scenario_failure_is_not_mislabeled_as_transport_failure(self):
        scenario = {"scenario": "multi_tool_dependency", "failure_stage": "multi_tool_dependency",
                    "failure_code": "operation_failed"}
        self.assertEqual(("multi_tool_dependency", "operation_failed"),
                         host._scenario_stop_failure_code(
                             scenario, {"failed": 0, "retries": 0}))
        self.assertEqual(("multi_tool_dependency", "model_request_failed_or_retried"),
                         host._scenario_stop_failure_code(
                             scenario, {"failed": 1, "retries": 0}))

    def test_orchestrated_failure_capture_attaches_planner_tool_and_event_metadata(self):
        from types import SimpleNamespace

        class Provider:
            request_telemetry = [{"sequence": 1, "operation": "plan_completion",
                                  "sent": 1, "succeeded": True, "failed": 0,
                                  "retries": 0, "requested_output_tokens": 4096,
                                  "finish_reason": "stop", "prompt": "private"}]

            def request_stats(self):
                return {"attempted": 1, "sent": 1, "succeeded": 1,
                        "failed": 0, "retries": 0}

        class Recorder:
            delegate = SimpleNamespace(request_count=1, request_diagnostics=[{
                "phase": "agent_execution_planner", "max_output_tokens": 4096,
                "finish_reason": "stop", "request_sent": True,
                "raw_response": "private",
            }])
            decisions = [{"planner_round": 1, "observation_count": 0,
                          "selected_calls": [{"tool_id": "open_synthetic_bundle",
                                              "arguments": {"secret": "private"},
                                              "dependencies": []}],
                          "final_answer": "private"}]

        def raise_synthetic_value_error():
            raise ValueError("private failure text")

        try:
            raise_synthetic_value_error()
        except ValueError as exc:
            host._attach_scenario_failure_diagnostics(
                exc, provider=Provider(), telemetry_before=0,
                orchestrator=SimpleNamespace(planner=SimpleNamespace(
                    last_request_diagnostics={"phase": "conversation_planner",
                                              "input_budget_tokens": 900_000,
                                              "question": "private"})),
                recorders=[Recorder()],
                events=[{"type": "PLAN_REJECTED", "sequence": 4,
                         "error_code": "invalid_plan", "prompt": "private"}],
            )
            diagnostic = host._safe_scenario_failure_diagnostic(exc, Provider())

        host_diagnostics = diagnostic["host_diagnostics"]
        self.assertEqual(4096, host_diagnostics["request_telemetry"][0]
                         ["requested_output_tokens"])
        self.assertEqual(4096, host_diagnostics["agent_planners"][0]
                         ["request_diagnostics"][0]["max_output_tokens"])
        self.assertEqual("open_synthetic_bundle", host_diagnostics["agent_planners"][0]
                         ["decisions"][0]["selected_calls"][0]["tool_id"])
        self.assertEqual("invalid_plan", host_diagnostics["event_metadata"][0]["error_code"])
        self.assertNotIn("private", str(host_diagnostics))

    def test_capped_provider_records_output_limit_and_response_shape_without_content(self):
        class Provider:
            def __init__(self):
                self.stats = {"sent": 0, "succeeded": 0, "failed": 0, "retries": 0}

            def request_stats(self):
                return dict(self.stats)

            def plan_completion(self, *_args, **_kwargs):
                self.stats.update(sent=1, succeeded=1, last_http_status=200,
                                  last_finish_reason="stop",
                                  last_usage={"prompt_tokens": 8, "completion_tokens": 4},
                                  last_response_diagnostic={"content_chars": 20,
                                                            "raw_content": "private"})
                return "{}"

        capped = host.CappedConversationProvider(Provider(), limit=2)
        capped.plan_completion([], max_output_tokens=4096, timeout_seconds=1)
        safe = host._safe_host_failure_diagnostics(
            {"request_telemetry": capped.request_telemetry})["request_telemetry"][0]
        self.assertEqual(4096, safe["requested_output_tokens"])
        self.assertEqual(200, safe["http_status"])
        self.assertEqual(20, safe["response_shape"]["content_chars"])
        self.assertNotIn("private", str(safe))

    def test_real_scenario_turn_uses_orchestrator_not_an_engine_shortcut(self):
        source = Path(host.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        orchestrator_runs = [node for node in ast.walk(tree)
                             if isinstance(node, ast.Call)
                             and isinstance(node.func, ast.Attribute)
                             and node.func.attr == "run"
                             and isinstance(node.func.value, ast.Name)
                             and node.func.value.id == "orchestrator"]
        self.assertEqual(1, len(orchestrator_runs))
        self.assertNotIn("AgentExecutionEngine(", source)
        self.assertIn("ConversationOrchestrator(dependencies)", source)

    def test_real_agent_deadline_leaves_room_for_planner_calls_and_keeps_tool_limit(self):
        from agentforge.agent_execution import LLMExecutionPlanner

        self.assertEqual(5.0, host.AGENT_TOOL_TIMEOUT_SECONDS)
        self.assertGreaterEqual(host.AGENT_RUN_TIMEOUT_SECONDS,
                                2 * LLMExecutionPlanner.TIMEOUT_SECONDS)
        tree = ast.parse(Path(host.__file__).read_text(encoding="utf-8"))
        call = next(node for node in ast.walk(tree)
                    if isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "ConversationAgentExecutor")
        keywords = {item.arg: item.value for item in call.keywords}
        self.assertEqual("AGENT_TOOL_TIMEOUT_SECONDS",
                         keywords["tool_timeout_seconds"].id)
        self.assertEqual("AGENT_RUN_TIMEOUT_SECONDS",
                         keywords["run_timeout_seconds"].id)

    def test_timeout_and_cancellation_are_separate_scenarios_with_strict_outcomes(self):
        self.assertIn("tool_timeout", host.SCENARIOS)
        self.assertIn("tool_cancel", host.SCENARIOS)
        self.assertNotIn("timeout_and_cancel", host.SCENARIOS)
        self.assertTrue(host._tool_timeout_was_observed({
            "execution_status": "tool_timeout",
            "tool_calls": [{"tool_id": "slow_synthetic_read", "status": "failed",
                            "error_code": "TIMEOUT"}],
        }))
        self.assertFalse(host._tool_timeout_was_observed({
            "execution_status": "completed",
            "tool_calls": [{"tool_id": "slow_synthetic_read", "status": "failed",
                            "error_code": "TIMEOUT"}],
        }))
        self.assertFalse(host._tool_timeout_was_observed({
            "execution_status": "tool_timeout",
            "tool_calls": [{"tool_id": "other_tool", "status": "failed",
                            "error_code": "TIMEOUT"}],
        }))

    def test_host_output_budget_uses_shared_model_resolver_and_long_mode_is_deepseek_only(self):
        from types import SimpleNamespace
        from agentforge.context_assembler import ContextAssembler
        from agentforge.model_types import LOCAL_QWEN, ModelDescriptor

        deepseek = ModelDescriptor(
            provider_id="deepseek", model_id="deepseek-flash", display_name="Synthetic",
            model_type="cloud", context_window=1_048_576, max_output_tokens=393_216,
        )
        deepseek_delegate = SimpleNamespace(
            provider_id="deepseek", descriptor=deepseek,
            context_assembler=ContextAssembler(), max_output_tokens=256,
        )
        provider = host.CappedConversationProvider(deepseek_delegate, limit=8)
        standard = provider.select_output_budget("standard")
        self.assertEqual(16_384, standard.effective_tokens)
        long = provider.select_output_budget("long")
        self.assertEqual(65_536, long.effective_tokens)
        self.assertEqual(65_536, deepseek_delegate.max_output_tokens)
        self.assertEqual("long", host._safe_output_budget(provider)["mode"])

        qwen_delegate = SimpleNamespace(
            provider_id="llama-server", descriptor=LOCAL_QWEN,
            context_assembler=ContextAssembler(), max_output_tokens=256,
        )
        qwen = host.CappedConversationProvider(qwen_delegate, limit=8)
        self.assertEqual(512, qwen.select_output_budget("standard").effective_tokens)
        self.assertEqual(512, qwen_delegate.max_output_tokens)
        not_applicable = host._run_scenario(
            "long_answer", provider=qwen, engine=None, index_store=None,
            conversation_store=None, session_service=None, repository=None,
            repository_id=None, run_id="synthetic",
        )
        self.assertEqual("NOT_APPLICABLE", not_applicable["status"])

    def test_host_orchestrated_turn_does_not_pin_a_second_answer_token_limit(self):
        tree = ast.parse(Path(host.__file__).read_text(encoding="utf-8"))
        request = next(node for node in ast.walk(tree)
                       if isinstance(node, ast.Call)
                       and isinstance(node.func, ast.Name)
                       and node.func.id == "ConversationRequest")
        keywords = {item.arg: item.value for item in request.keywords}
        self.assertIsInstance(keywords["max_output_tokens"], ast.Constant)
        self.assertIsNone(keywords["max_output_tokens"].value)
        self.assertIsInstance(keywords["output_budget_mode"], ast.Name)
        self.assertEqual("output_budget_mode", keywords["output_budget_mode"].id)

    def test_long_answer_scenario_uses_synthetic_general_question_and_is_registered(self):
        self.assertIn("long_answer", host.SCENARIOS)
        self.assertEqual("long_answer", host._parse_runner_args([
            "--provider", "deepseek", "--model", "deepseek-flash",
            "--scenario", "long_answer",
        ]).scenario)
        question = host._scenario_question("long_answer")
        self.assertIn("at least 900 words", question)
        self.assertIn("fictional Amber Ledger", question)
        self.assertIn("Do not add features", question)

    def test_approval_resume_requires_explicit_runner_opt_in(self):
        default = host._parse_runner_args(["--scenario", "approval_pause_resume"])
        self.assertFalse(default.approve_synthetic_actions)
        opted_in = host._parse_runner_args([
            "--scenario", "approval_pause_resume", "--approve-synthetic-actions",
        ])
        self.assertTrue(opted_in.approve_synthetic_actions)

    def test_long_result_pages_reconstruct_full_utf8_content_and_digest(self):
        from agentforge.agent_execution import InMemoryToolResultStore

        content = ("关键证据-αβ-" * 7_000) + "tail"
        store = InMemoryToolResultStore()
        result_ref = store.put("exec-pages", "call-pages", content)
        probe = host._read_result_pages(
            store, result_ref,
            expected_sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
            page_chars=997,
        )
        self.assertTrue(probe["complete"])
        self.assertEqual(len(content), probe["total_chars"])
        self.assertEqual(len(content), probe["characters_read"])
        self.assertGreater(probe["pages_read"], 1)
        self.assertEqual(hashlib.sha256(content.encode("utf-8")).hexdigest(), probe["sha256"])
        self.assertTrue(probe["matches_expected"])

    def test_long_result_paging_does_not_accept_missing_or_corrupt_content(self):
        from agentforge.agent_execution import InMemoryToolResultStore

        content = "complete synthetic evidence-" * 500
        store = InMemoryToolResultStore()
        result_ref = store.put("exec-pages", "call-pages", content)
        probe = host._read_result_pages(
            store, result_ref, expected_sha256="0" * 64, page_chars=128,
        )
        self.assertTrue(probe["complete"])
        self.assertFalse(probe["matches_expected"])

        class GappedPageStore:
            def page(self, ref, *, offset, limit):
                return {
                    "result_ref": ref, "offset": offset, "total_chars": 5,
                    "next_offset": offset + 4, "complete": False, "text": "abc",
                }

        with self.assertRaises(ValueError):
            host._read_result_pages(
                GappedPageStore(), "agent-result:exec:call",
                expected_sha256="0" * 64, page_chars=3,
            )

    def test_long_tool_answer_requires_usable_cited_supported_contract(self):
        evidence = SimpleNamespace(
            citation="src/metrics.py:1-2",
            excerpt="def compute_total(value, delta):\n    return value + delta",
        )
        claim = SimpleNamespace(
            origin="repository", kind="inference",
            text="compute_total returns the sum of value and delta.",
            citations=(evidence.citation,),
        )
        answer = SimpleNamespace(
            text="compute_total returns the sum of value and delta [E1].",
        )
        valid = SimpleNamespace(
            answer=answer,
            contract=SimpleNamespace(status="limited", claims=(claim,), evidence=(evidence,)),
        )
        self.assertTrue(host._long_tool_answer_is_supported(valid))

        refused = SimpleNamespace(
            answer=answer,
            contract=SimpleNamespace(status="refused", claims=(claim,), evidence=(evidence,)),
        )
        self.assertFalse(host._long_tool_answer_is_supported(refused))
        uncited = SimpleNamespace(
            answer=answer,
            contract=SimpleNamespace(
                status="limited", claims=(SimpleNamespace(
                    origin="repository", kind="inference", text=claim.text, citations=()),
                ), evidence=(evidence,),
            ),
        )
        self.assertFalse(host._long_tool_answer_is_supported(uncited))

    def test_scenario_failure_diagnostic_excludes_raw_exception_text(self):
        class Provider:
            def request_stats(self):
                return {
                    "attempted": 3, "sent": 3, "succeeded": 3, "failed": 0,
                    "retries": 0, "last_http_status": 200,
                    "last_application_failure_phase": "planning_response",
                    "last_application_error_code": "invalid_planning_completion",
                    "last_finish_reason": "length", "last_response_chars": 2224,
                    "last_response_diagnostic": {
                        "content_chars": 2224, "finish_reason": "length",
                        "raw_content": "must-not-be-recorded",
                    },
                    "last_usage": {"completion_tokens": 512, "total_tokens": 800,
                                   "raw": "must-not-be-recorded"},
                }

        diagnostic = host._safe_scenario_failure_diagnostic(
            ValueError("private query text and secret must-not-be-recorded"), Provider())
        self.assertEqual("ValueError", diagnostic["exception_type"])
        self.assertEqual("invalid_planning_completion",
                         diagnostic["provider"]["last_application_error_code"])
        self.assertEqual("length", diagnostic["response_shape"]["finish_reason"])
        self.assertNotIn("must-not-be-recorded", str(diagnostic))

    def test_agent_schema_failure_diagnostic_names_missing_tool_ids_only(self):
        safe = host._safe_agent_planner_diagnostic({
            "phase": "agent_execution_planner",
            "planning_failure_details": {
                "missing_tool_ids": ["read", "search", "unsafe tool text"],
                "loaded_schema_tool_ids": ["list-symbols"],
                "schema_round": 2,
                "raw_arguments": "private model output",
            },
        })
        self.assertEqual({
            "action": "unknown",
            "missing_tool_ids": ["read", "search"],
            "loaded_schema_tool_ids": ["list-symbols"],
            "schema_round": 2,
        }, safe["planning_failure_details"])
        self.assertNotIn("private model output", str(safe))

    def test_large_output_case_reads_the_durable_result_by_reference(self):
        source = Path(host.__file__).read_text(encoding="utf-8")
        self.assertIn("_read_result_pages(", source)
        self.assertIn('result_page["total_chars"] > 16_000', source)
        self.assertIn('result_page.get("matches_expected") is True', source)
        self.assertIn('"long_tool_final_answer_supported"', source)

    def test_synthetic_cancel_tool_waits_for_host_cancellation(self):
        state = {"tool_started": threading.Event()}
        registry = host._scenario_registry("tool_cancel", state)
        definitions = registry.definitions()
        self.assertEqual(["cancel_synthetic_read"], [item.tool_id for item in definitions])
        cancelled = threading.Event()
        result = []
        context = type("Context", (), {"cancel_event": cancelled})()
        worker = threading.Thread(target=lambda: result.append(
            registry.handler("cancel_synthetic_read")({}, context)))
        worker.start()
        self.assertTrue(state["tool_started"].wait(1.0))
        self.assertTrue(worker.is_alive())
        cancelled.set()
        worker.join(timeout=1.0)
        self.assertFalse(worker.is_alive())
        self.assertEqual({"status": "cooperatively_cancelled"}, result[0])

    def test_direct_general_scenarios_have_empty_tool_catalog(self):
        class Toolbox:
            def __init__(self):
                self.calls = []

            def execute(self, call):
                self.calls.append(call)
                return {"source": "synthetic"}

        for scenario_name in ("general_direct", "long_answer"):
            for toolbox in (None, Toolbox()):
                with self.subTest(scenario=scenario_name, toolbox=toolbox is not None):
                    registry = host._scenario_registry(scenario_name, {}, toolbox)
                    self.assertEqual((), registry.definitions())
                    if toolbox is not None:
                        self.assertEqual([], toolbox.calls)

    def test_direct_general_expectation_rejects_any_tool_call(self):
        self.assertTrue(host._direct_general_answer_without_tools({
            "selected_route": "general", "tool_calls": [],
        }))
        self.assertFalse(host._direct_general_answer_without_tools({
            "selected_route": "general", "tool_calls": [{"tool_id": "search"}],
        }))
        self.assertFalse(host._direct_general_answer_without_tools({
            "selected_route": "repository", "tool_calls": [],
        }))
        self.assertFalse(host._direct_general_answer_without_tools(None))

    def test_repository_scenario_registry_uses_only_isolated_read_tools_and_no_noop(self):
        class Toolbox:
            def __init__(self):
                self.calls = []

            def execute(self, call):
                self.calls.append(call)
                return {"source": "synthetic"}

        for name in ("repository_auto", "hybrid_knowledge"):
            with self.subTest(scenario=name):
                toolbox = Toolbox()
                registry = host._scenario_registry(name, {}, toolbox)
                tool_ids = {item.tool_id for item in registry.definitions()}
                self.assertEqual({"search", "read"}, tool_ids)
                self.assertNotIn("index", tool_ids)
                self.assertNotIn("record_synthetic_inspection", tool_ids)
                self.assertEqual({"source": "synthetic"}, registry.handler("search")(
                    {"query": "compute_total"}, None))
                self.assertEqual("search", toolbox.calls[0].name)

    def test_streaming_lifecycle_registry_is_synthetic_only_and_question_has_no_repository_claim(self):
        registry = host._scenario_registry("streaming_lifecycle", {})
        self.assertEqual({"stream_synthetic_status"},
                         {item.tool_id for item in registry.definitions()})
        question = host._scenario_question("streaming_lifecycle")
        self.assertIn("stream_synthetic_status", question)
        self.assertNotIn("compute_total", question)
        self.assertNotIn("citation", question.casefold())

    def test_multi_turn_scenario_keeps_its_synthetic_fact_tool(self):
        class Toolbox:
            def execute(self, _call):
                return {}

        registry = host._scenario_registry("multi_turn", {}, Toolbox())
        tool_ids = {item.tool_id for item in registry.definitions()}
        self.assertIn("read_synthetic_fact", tool_ids)
        self.assertTrue({"search", "read", "list-symbols"}.issubset(tool_ids))
        self.assertNotIn("index", tool_ids)

    def test_multi_turn_host_scenario_uses_separate_general_tool_and_history_turns(self):
        marker = "AF42-01234567"
        queries = host._multi_turn_queries("0123456789abcdef")

        self.assertEqual(3, len(queries))
        self.assertIn(marker, queries[0])
        self.assertNotIn("read_synthetic_fact", queries[0])
        self.assertIn("read_synthetic_fact", queries[1])
        self.assertNotIn("compute_total", queries[1])
        self.assertIn("first turn", queries[2])
        self.assertIn("second turn", queries[2])
        self.assertIn("Without calling any tools", queries[2])
        self.assertNotIn("compute_total", queries[2])

    def test_multi_turn_runner_reuses_one_session_and_checks_prior_turn_content(self):
        from types import SimpleNamespace

        run_id = "0123456789abcdef"
        queries = host._multi_turn_queries(run_id)
        conversation_store = SimpleNamespace(
            create_conversation=lambda *_: {"id": "multi-turn-session"},
        )
        provider = SimpleNamespace(
            provider_id="offline-test", model="offline-model",
            select_output_budget=lambda mode: SimpleNamespace(
                mode=mode, effective_tokens=4096,
            ),
        )
        calls = []

        def orchestrated_turn(**kwargs):
            turn = len(calls)
            calls.append(kwargs)
            answer = (
                "AF42-01234567" if turn == 0 else
                "phase4-two-local-only" if turn == 1 else
                "AF42-01234567\nphase4-two-local-only"
            )
            tool_calls = ([{"tool_id": "read_synthetic_fact", "status": "completed"}]
                          if turn == 1 else [])
            trace = {
                "provider": "offline-test", "model": "offline-model",
                "session_id": "multi-turn-session", "planner_request_count": 1,
                "selected_route": "general", "requires_agent_execution": bool(tool_calls),
                "planned_task_types": ["general", "tool"] if tool_calls else ["general"],
                "task_count": 2 if tool_calls else 1,
                "task_type_counts": {"general": 1, "repository": 0,
                                     "external": 0, "tool": len(tool_calls)},
                "executable_task_count": len(tool_calls), "execution_started": bool(tool_calls),
                "plan_validity": "passed", "tool_calls": tool_calls,
                "plan_validation_diagnostics": [], "result_page_consumption": {"tool_calls": []},
                "observation_count": len(tool_calls), "replanning_count": 0,
                "successful_replanning_count": 0, "execution_status": "completed",
                "answer_contract_status": "answered", "contract_failure_code": None,
                "session_restore_status": "messages_reloaded",
                "persisted_message_count": (turn + 1) * 2,
                "token_usage": {"requests_with_usage": 1}, "finish_reason": "stop",
                "request_counts": {"attempted": 1, "sent": 1, "succeeded": 1,
                                   "failed": 0, "retries": 0},
                "planner_decisions": [], "plan_rejections": [],
                "agent_planner_request_diagnostics": [], "agent_failure_code": None,
                "streaming_events": [], "answer_digest": {"sha256": "0" * 64},
                "answer_characters": len(answer), "output_budget": {},
                "answer_request_output_tokens": 4096,
                "execution_id": "multi-turn-execution" if tool_calls else None,
                "state": {},
            }
            result = SimpleNamespace(
                execution_status="completed", contract=object(),
                answer=SimpleNamespace(text=answer), retrieval={},
            )
            return result, trace

        with patch.object(host, "_orchestrated_turn", side_effect=orchestrated_turn):
            scenario = host._run_scenario(
                "multi_turn", provider=provider, engine=object(), index_store=object(),
                conversation_store=conversation_store, session_service=object(),
                repository=Path("synthetic"), repository_id="synthetic-repository",
                run_id=run_id,
            )

        self.assertEqual("PASS", scenario["status"])
        self.assertEqual(3, len(calls))
        self.assertEqual(list(queries), [call["query"] for call in calls])
        self.assertEqual({"multi-turn-session"}, {call["conversation_id"] for call in calls})
        self.assertEqual([2, 4, 6], [item["trace"]["persisted_message_count"]
                                    for item in scenario["turn_traces"]])
        self.assertTrue(scenario["assertions"]["second_turn_used_tool_result"])
        self.assertTrue(scenario["assertions"]["followup_restored_session_context"])
        self.assertTrue(scenario["assertions"]["same_session_history_restored_each_turn"])

    def test_tool_failure_recovery_scenario_is_independent_of_repository_citation(self):
        question = host._scenario_question("tool_failure_recovery")
        self.assertIn("primary_synthetic_read", question)
        self.assertIn("TOOL_UNAVAILABLE", question)
        self.assertIn("fallback_synthetic_read", question)
        self.assertNotIn("compute_total", question)
        self.assertNotIn("citation", question.casefold())

    def test_failure_stage_is_advanced_before_each_mutating_setup_phase(self):
        source = Path(host.__file__).read_text(encoding="utf-8")
        for stage in ("database_provision", "database_migration", "chroma_setup",
                      "synthetic_repository_setup", "provider_setup",
                      "synthetic_index_setup", "synthetic_index_build", "scenarios"):
            self.assertIn(f'failure_stage = "{stage}"', source)

    def test_runner_failure_diagnostic_names_operation_and_redacts_secret(self):
        from agentforge.errors import StoreError

        secret = "synthetic-secret-value"
        diagnostic = host._safe_runner_error(
            StoreError(f"Storage setup failed using {secret}."),
            "open_isolated_sqlite_memory_index",
            secrets=(secret,),
        )
        self.assertEqual("open_isolated_sqlite_memory_index", diagnostic["operation"])
        self.assertEqual("StoreError", diagnostic["exception_type"])
        self.assertNotIn(secret, str(diagnostic))

    def test_planning_failure_diagnostic_keeps_safe_reason_and_response_shape(self):
        from agentforge.planner import PlanningError

        class Provider:
            def request_stats(self):
                return {
                    "attempted": 1, "sent": 1, "succeeded": 1, "failed": 0,
                    "retries": 0, "last_http_status": 200,
                    "last_failure_phase": "response body", "last_elapsed_seconds": 20.0,
                    "last_finish_reason": "stop", "last_response_chars": 215,
                    "last_response_diagnostic": {
                        "body_bytes": 800, "root_type": "dict", "choices_type": "list",
                        "choice_count": 1, "content_type": "str", "content_chars": 215,
                        "raw_content": "must-not-be-recorded",
                    },
                    "last_usage": {"prompt_tokens": 100, "completion_tokens": 50,
                                   "total_tokens": 150, "secret": "must-not-be-recorded"},
                }

        diagnostic = host._safe_planning_failure_diagnostic(
            PlanningError("LLM Planner output does not match the required plan schema.",
                          diagnostic_code="invalid_bounded_search_queries",
                          diagnostic_details={
                              "query_count": 4, "non_string_count": 1,
                              "empty_query_count": 1, "oversized_query_count": 1,
                              "max_query_chars": 513,
                              "raw_query": "must-not-be-recorded",
                          }),
            Provider(),
        )
        self.assertEqual("conversation_planner", diagnostic["phase"])
        self.assertEqual("LLM Planner output does not match the required plan schema.",
                         diagnostic["reason"])
        self.assertEqual("response body", diagnostic["provider"]["last_failure_phase"])
        self.assertEqual(20.0, diagnostic["provider"]["last_elapsed_seconds"])
        self.assertEqual(215, diagnostic["response_shape"]["content_chars"])
        self.assertEqual(150, diagnostic["usage"]["total_tokens"])
        self.assertEqual("invalid_bounded_search_queries", diagnostic["planning_failure_code"])
        self.assertEqual({
            "query_count": 4, "non_string_count": 1,
            "empty_query_count": 1, "oversized_query_count": 1,
            "max_query_chars": 513,
        }, diagnostic["planning_failure_details"])
        self.assertNotIn("must-not-be-recorded", str(diagnostic))

    def test_planning_diagnostic_drops_unrecognized_details_and_bounds_types(self):
        from agentforge.planner import PlanningError

        diagnostic = host._safe_planning_failure_diagnostic(
            PlanningError("Invalid query shape.", diagnostic_code="invalid_bounded_search_queries",
                          diagnostic_details={
                              "query_count": 40_000, "max_query_chars": -1,
                              "value_type": "PrivateObject",
                              "raw_response": "private model output",
                          }),
            object(),
        )

        self.assertNotIn("planning_failure_details", diagnostic)
        self.assertNotIn("private model output", str(diagnostic))

    def test_retrieval_type_correction_diagnostic_keeps_only_safe_task_metadata(self):
        from agentforge.planner import PlanningError

        diagnostic = host._safe_planning_failure_diagnostic(
            PlanningError(
                "Task retrieval flag must match its task type.",
                diagnostic_code="task_retrieval_type_mismatch",
                diagnostic_details={
                    "task_id": "task_1",
                    "task_type": "general",
                    "requires_retrieval": True,
                    "expected_requires_retrieval": False,
                    "question": "private user content",
                    "raw_response": "private model output",
                },
            ),
            object(),
        )

        self.assertEqual("task_retrieval_type_mismatch", diagnostic["planning_failure_code"])
        self.assertEqual({
            "task_id": "task_1",
            "task_type": "general",
            "requires_retrieval": True,
            "expected_requires_retrieval": False,
        }, diagnostic["planning_failure_details"])
        self.assertNotIn("private", str(diagnostic))

    def test_intent_mismatch_failure_diagnostic_preserves_both_validation_stages_safely(self):
        from agentforge.planner import PlanningError

        initial = {
            "diagnostic_code": "intent_task_mismatch",
            "diagnostic_details": {
                "intent": "hybrid",
                "task_type_counts": {"general": 0, "repository": 1, "external": 0},
                "required_task_types": ["general", "repository"],
                "missing_task_types": ["general"],
                "model_text": "must-not-be-recorded",
            },
        }
        final = {
            "diagnostic_code": "intent_task_mismatch",
            "diagnostic_details": {
                "intent": "hybrid",
                "task_type_counts": {"general": 0, "repository": 1, "external": 0},
                "required_task_types": ["general", "repository"],
                "missing_task_types": ["general"],
                "model_text": "must-not-be-recorded",
            },
        }

        diagnostic = host._safe_planning_failure_diagnostic(
            PlanningError(
                "Plan tasks are inconsistent with the selected intent.",
                diagnostic_code="intent_task_mismatch",
                diagnostic_details={
                    **final["diagnostic_details"],
                    "planner_request_count": 2,
                    "correction_attempted": True,
                    "correction_attempt": 1,
                    "correction_status": "rejected",
                    "initial_validation_failure": initial,
                    "final_validation_failure": final,
                },
            ),
            object(),
        )

        self.assertEqual(2, diagnostic["planner_request_count"])
        self.assertTrue(diagnostic["correction_attempted"])
        self.assertEqual(1, diagnostic["correction_attempt"])
        self.assertEqual("rejected", diagnostic["correction_status"])
        self.assertEqual("intent_task_mismatch",
                         diagnostic["initial_validation_failure"]["diagnostic_code"])
        self.assertEqual(["general"], diagnostic["initial_validation_failure"]
                         ["diagnostic_details"]["missing_task_types"])
        self.assertEqual("intent_task_mismatch",
                         diagnostic["final_validation_failure"]["diagnostic_code"])
        self.assertNotIn("must-not-be-recorded", str(diagnostic))

    def test_result_page_trace_verifies_contiguous_complete_pages_without_content(self):
        first_text = "page-one"
        second_text = "page-two"
        source = first_text + second_text
        source_hash = hashlib.sha256(source.encode("utf-8")).hexdigest()
        results = [
            {"call_id": "page-1", "tool_id": "agent.read_result_page", "status": "completed",
             "output": {"result_ref": "agent-result:test:large", "source_call_id": "large-1",
                        "source_sha256": source_hash, "page_sha256": hashlib.sha256(
                            first_text.encode("utf-8")).hexdigest(), "page_number": 1,
                        "offset": 0, "next_offset": len(first_text), "total_chars": len(source),
                        "complete": False, "text": first_text}},
            {"call_id": "page-2", "tool_id": "agent.read_result_page", "status": "completed",
             "output": {"result_ref": "agent-result:test:large", "source_call_id": "large-1",
                        "source_sha256": source_hash, "page_sha256": hashlib.sha256(
                            second_text.encode("utf-8")).hexdigest(), "page_number": 2,
                        "offset": len(first_text), "next_offset": len(source),
                        "total_chars": len(source), "complete": True, "text": second_text}},
        ]

        trace = host._result_page_consumption(results)

        self.assertEqual(2, trace["groups"][0]["page_count"])
        self.assertTrue(trace["groups"][0]["complete"])
        self.assertEqual(source_hash, trace["groups"][0]["reconstructed_sha256"])
        self.assertNotIn(first_text, str(trace))
        results[1]["output"]["offset"] += 1
        gap_trace = host._result_page_consumption(results)
        self.assertFalse(gap_trace["groups"][0]["complete"])

    def test_successful_planner_correction_trace_keeps_states_without_plan_content(self):
        diagnostic = host._safe_conversation_planner_diagnostic({
            "planner_request_count": 2,
            "initial_validation_status": "failed",
            "final_validation_status": "passed_after_correction",
            "correction_attempted": True,
            "correction_status": "accepted",
            "validation_failure_code": "intent_task_mismatch",
            "initial_validation_failure": {
                "diagnostic_code": "intent_task_mismatch",
                "diagnostic_details": {
                    "intent": "hybrid",
                    "task_type_counts": {"general": 0, "repository": 1},
                    "missing_task_types": ["general"],
                },
            },
            "question": "private user question",
            "raw_plan": "private model output",
        })

        self.assertEqual(2, diagnostic["planner_request_count"])
        self.assertEqual("failed", diagnostic["initial_validation_status"])
        self.assertEqual("passed_after_correction", diagnostic["final_validation_status"])
        self.assertEqual("accepted", diagnostic["correction_status"])
        self.assertEqual("intent_task_mismatch",
                         diagnostic["initial_validation_failure"]["diagnostic_code"])
        self.assertNotIn("private", str(diagnostic))

    def test_unreached_agent_scenarios_are_not_misreported_as_engine_failure(self):
        scenarios = {name: {"status": "NOT_MEASURED"} for name in host.SCENARIOS}
        scenarios["general_direct"] = {"status": "FAIL", "trace": None}
        self.assertEqual("NOT_MEASURED", host._agent_engine_status(scenarios))
        scenarios["single_tool"] = {"status": "FAIL", "trace": None}
        self.assertEqual("NOT_MEASURED", host._agent_engine_status(scenarios))
        scenarios["single_tool"] = {"status": "FAIL", "trace": {"tool_calls": []}}
        self.assertEqual("FAIL", host._agent_engine_status(scenarios))
        scenarios["single_tool"] = {"status": "PASS", "trace": {"tool_calls": [{"tool_id": "read"}]}}
        self.assertEqual("PASS", host._agent_engine_status(scenarios))

    def test_resumed_agent_execution_counts_as_real_engine_use(self):
        scenarios = {name: {"status": "NOT_MEASURED"} for name in host.SCENARIOS}
        scenarios["approval_pause_resume"] = {
            "status": "PASS",
            "trace": {"execution_status": "waiting_approval", "tool_calls": []},
            "resume_trace": {"execution_status": "completed",
                             "tool_calls": [{"tool_id": "approved_synthetic_action"}]},
        }
        self.assertEqual("PASS", host._agent_engine_status(scenarios))

    def test_host_runner_can_select_one_scenario_without_claiming_full_acceptance(self):
        self.assertEqual(("general_direct",), host._selected_scenarios(
            ["--scenario", "general_direct"]))
        self.assertEqual(host._full_suite_scenarios("llama-server"),
                         host._selected_scenarios([]))
        targeted = host._result_template(("general_direct",))
        self.assertEqual("targeted", targeted["acceptance_scope"])
        self.assertEqual(["general_direct"], targeted["selected_scenarios"])

    def test_deepseek_default_matrix_remains_all_fifteen_scenarios(self):
        selected = host._selected_scenarios([
            "--provider", "deepseek", "--model", "deepseek-flash",
        ])
        self.assertEqual(15, len(selected))
        self.assertEqual(host.SCENARIOS, selected)
        self.assertIn("approval_pause_resume", selected)
        self.assertIn("session_process_restore", selected)

    def test_powershell_launcher_forwards_approval_only_after_exact_user_confirmation(self):
        launcher = (Path(host.ROOT) / "AF042_UNIFIED_AGENT_HOST_ACCEPTANCE.ps1").read_text(
            encoding="utf-8")
        self.assertIn("[switch] $ApproveSyntheticActions", launcher)
        self.assertIn("approved_action_ran=true", launcher)
        confirmation = "if ($approvalDecision -ceq 'APPROVE SYNTHETIC ACTION')"
        self.assertIn(confirmation, launcher)
        self.assertIn("if ($approvalGranted) { $runnerArgs += '--approve-synthetic-actions' }",
                      launcher)
        self.assertLess(launcher.index(confirmation),
                        launcher.index("$runnerArgs += '--approve-synthetic-actions'"))
        self.assertIn("$approvalDecision -cne 'DENY SYNTHETIC ACTION'", launcher)
        self.assertIn("$stopBeforeRunner = $true", launcher)
        self.assertIn("if (-not $stopBeforeRunner)", launcher)
        self.assertEqual(1, launcher.count("--approve-synthetic-actions"))

    def test_powershell_launcher_preserves_environment_and_keeps_credentials_out_of_output(self):
        launcher = (Path(host.ROOT) / "AF042_UNIFIED_AGENT_HOST_ACCEPTANCE.ps1").read_text(
            encoding="utf-8")
        self.assertIn("$hadDbPassword = $processEnvironment.Contains('AGENTFORGE_DB_PASSWORD')",
                      launcher)
        self.assertIn("$hadLlamaApiKey = $processEnvironment.Contains('AGENTFORGE_LLAMA_API_KEY')",
                      launcher)
        self.assertIn("SetEnvironmentVariable('AGENTFORGE_DB_PASSWORD', $originalDbPassword, 'Process')",
                      launcher)
        self.assertIn("SetEnvironmentVariable('AGENTFORGE_LLAMA_API_KEY', $originalLlamaApiKey, 'Process')",
                      launcher)
        approval = launcher.index("$approvalDecision = Read-Host $approvalPrompt")
        credential_read = launcher.index("Get-ConversationDatabaseKey")
        self.assertLess(approval, credential_read)
        for line in launcher.splitlines():
            if "Write-Host" in line:
                self.assertNotIn("$originalDbPassword", line)
                self.assertNotIn("$originalLlamaApiKey", line)
                self.assertNotIn("$env:AGENTFORGE_DB_PASSWORD", line)
                self.assertNotIn("$env:AGENTFORGE_LLAMA_API_KEY", line)

    def test_full_suite_continues_after_assertion_failure_but_stops_on_model_fault(self):
        self.assertFalse(host._stop_after_scenario(
            full_suite=True, scenario_status="FAIL",
            request_stats={"failed": 0, "retries": 0}))
        self.assertTrue(host._stop_after_scenario(
            full_suite=True, scenario_status="FAIL",
            request_stats={"failed": 1, "retries": 0}))
        self.assertTrue(host._stop_after_scenario(
            full_suite=True, scenario_status="FAIL",
            request_stats={"failed": 0, "retries": 1}))
        self.assertTrue(host._stop_after_scenario(
            full_suite=False, scenario_status="FAIL",
            request_stats={"failed": 0, "retries": 0}))

    def test_failure_diagnostic_capture_is_wired_to_orchestrator_and_runner_classifies_stage(self):
        source = Path(host.__file__).read_text(encoding="utf-8")
        self.assertIn("_attach_scenario_failure_diagnostics(", source)
        self.assertIn("failure_stage, failure_code = _scenario_stop_failure_code(scenario, stats)",
                      source)

    def test_deepseek_streaming_requires_verified_capability_and_is_reported_as_partial(self):
        self.assertFalse(host._deepseek_streaming_enabled_for_scenario(
            "deepseek", "streaming_lifecycle", None))
        self.assertFalse(host._deepseek_streaming_enabled_for_scenario(
            "deepseek", "streaming_lifecycle", False))
        self.assertTrue(host._deepseek_streaming_enabled_for_scenario(
            "deepseek", "streaming_lifecycle", True))
        self.assertEqual("PARTIAL", host._selected_scenario_status(
            ("streaming_lifecycle",),
            {"streaming_lifecycle": {"status": "PARTIAL"}}))
        self.assertEqual("FAIL", host._selected_scenario_status(
            ("general_direct", "streaming_lifecycle"),
            {"general_direct": {"status": "PASS"},
             "streaming_lifecycle": {"status": "FAIL"}}))

    def test_application_streaming_requires_ordered_agent_lifecycle_and_observed_answer(self):
        required = ("STARTED", "PLAN_READY", "TOOL_CALL_STARTED", "TOOL_CALL_COMPLETED",
                    "OBSERVATION_RECEIVED", "REPLANNING", "FINAL_ANSWER", "COMPLETED")
        result = SimpleNamespace(
            contract=SimpleNamespace(status="answered"),
            answer=SimpleNamespace(text="The synthetic status is ready."),
        )
        trace = {
            "streaming_events": [{"type": event} for event in required],
            "tool_calls": [{"tool_id": "stream_synthetic_status", "status": "completed"}],
        }
        self.assertEqual({
            "application_event_lifecycle_order": True,
            "synthetic_tool_observation_completed": True,
            "final_answer_uses_observation": True,
        }, host._streaming_lifecycle_assertions(result, trace))
        trace["streaming_events"].remove({"type": "REPLANNING"})
        self.assertFalse(host._streaming_lifecycle_assertions(result, trace)[
            "application_event_lifecycle_order"])
        result.answer.text = "The status was unavailable."
        self.assertFalse(host._streaming_lifecycle_assertions(result, trace)[
            "final_answer_uses_observation"])

    def test_tool_argument_trace_contains_schema_shape_without_values(self):
        secret = "synthetic-private-tool-value"
        item = {
            "call_id": "call-1", "task_id": "task-1", "tool_id": "read",
            "status": "failed", "error_code": "INVALID_ARGUMENTS",
            "execution_metadata": {"argument_diagnostic": {
                "tool_id": "read", "call_id": "call-1", "task_id": "task-1",
                "argument_fields": ["path"], "argument_types": {"path": "string"},
                "schema_error_code": "handler_rejected_arguments",
                "private": secret,
            }},
        }
        safe = host._safe_tool_call_trace(item)
        self.assertEqual("read", safe["tool_id"])
        self.assertEqual({"path": "string"}, safe["argument_diagnostic"]["argument_types"])
        self.assertNotIn(secret, str(safe))

    def test_plan_validation_trace_retains_safe_argument_diagnostic_without_values(self):
        secret = "synthetic-private-tool-value"
        rows = host._safe_plan_validation_trace([{
            "call_id": "call-1", "task_id": "task-1", "tool_id": "read",
            "status": "failed", "error_code": "INVALID_ARGUMENTS",
            "execution_metadata": {
                "stage": "plan_validation", "diagnostic_code": "invalid_tool_arguments",
                "diagnostic_details": {
                    "tool_id": "read", "call_id": "call-1", "task_id": "task-1",
                    "schema_error_code": "type_mismatch",
                    "argument_fields": ["limit"], "argument_types": {"limit": "string"},
                    "raw_value": secret,
                },
            },
        }])
        self.assertEqual(1, len(rows))
        self.assertEqual("invalid_tool_arguments", rows[0]["diagnostic_code"])
        self.assertEqual({"limit": "string"}, rows[0]["diagnostic_details"]["argument_types"])
        self.assertNotIn(secret, str(rows))

    def test_partial_streaming_capability_is_visible_in_markdown_report(self):
        result = host._result_template(("streaming_lifecycle",),
                                       provider_id="deepseek", model_id="deepseek-flash")
        result["scenarios"]["streaming_lifecycle"].update(
            status="PARTIAL",
            partial_reason="provider_token_streaming_not_measured",
            provider_token_streaming="NOT_MEASURED",
        )
        test_temp_root = Path(host.ROOT) / "tests" / ".tmp"
        test_temp_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=test_temp_root) as directory:
            with patch.object(host, "ROOT", Path(directory)):
                _json_path, report_path = host._write_result(result)
            report = report_path.read_text(encoding="utf-8")
        self.assertIn("provider_token_streaming_not_measured", report)
        self.assertIn("NOT_MEASURED", report)

    def test_deepseek_full_suite_and_targeted_scenarios_require_explicit_model(self):
        with self.assertRaises(SystemExit):
            host._parse_runner_args(["--provider", "deepseek"])
        args = host._parse_runner_args([
            "--provider", "deepseek", "--model", "deepseek-flash",
        ])
        self.assertEqual("deepseek", args.provider)
        self.assertEqual(host.SCENARIOS, host._selected_scenarios([
            "--provider", "deepseek", "--model", "deepseek-flash",
        ]))
        with self.assertRaises(SystemExit):
            host._parse_runner_args(["--provider", "deepseek", "--scenario", "repository_auto"])
        args = host._parse_runner_args([
            "--provider", "deepseek", "--model", "deepseek-v4-pro",
            "--scenario", "repository_auto",
        ])
        self.assertEqual("deepseek", args.provider)
        self.assertEqual("deepseek-v4-pro", args.model)
        self.assertEqual(("repository_auto",), host._selected_scenarios([
            "--provider", "deepseek", "--model", "deepseek-flash",
            "--scenario", "repository_auto",
        ]))

    def test_deepseek_cloud_consent_requires_exact_confirmation(self):
        prompts = []
        self.assertTrue(host._confirm_deepseek_consent(
            lambda prompt: prompts.append(prompt) or host.DEEPSEEK_CONSENT_PHRASE))
        self.assertIn("Up to 128 DeepSeek chat requests", prompts[0])
        self.assertIn("model discovery is counted separately", prompts[0])
        self.assertFalse(host._confirm_deepseek_consent(lambda _prompt: "yes"))
        self.assertFalse(host._confirm_deepseek_consent(
            lambda _prompt: " " + host.DEEPSEEK_CONSENT_PHRASE))
        self.assertFalse(host._confirm_deepseek_consent(
            lambda _prompt: (_ for _ in ()).throw(EOFError())))

    def test_deepseek_declined_consent_stops_before_database_prompt_or_provider_request(self):
        from agentforge.credential_store import CredentialStore

        with patch.dict(os.environ, {host.DB_KEY_ENV: "synthetic-db-key"}, clear=True), \
                patch.object(host.host_infra, "_safe_listener_probe", return_value=True), \
                patch.object(CredentialStore, "status", return_value={"configured": True}), \
                patch.object(host, "_confirm_deepseek_consent", return_value=False), \
                patch.object(host.getpass, "getpass", side_effect=AssertionError(
                    "database prompt must not run after cloud consent is declined")), \
                patch.object(host, "_create_real_provider", side_effect=AssertionError(
                    "model discovery/chat must not run after cloud consent is declined")), \
                patch.object(host.sys, "argv", [str(host.__file__), "--provider", "deepseek",
                                                  "--model", "deepseek-flash", "--scenario",
                                                  "repository_auto"]), \
                patch.object(host, "_finish", side_effect=lambda result, status, stage, code:
                             (status, stage, code)):
            outcome = host._main()
        self.assertEqual(("BLOCKED", "cloud_consent", "synthetic_cloud_consent_not_confirmed"),
                         outcome)

    def test_deepseek_provider_uses_fresh_temp_metadata_and_preserves_model_window(self):
        from agentforge.credential_store import CredentialStore
        from agentforge.cli import _model_input_budget
        from agentforge.model_registry import ModelRegistry
        from agentforge.model_types import ModelDescriptor
        from agentforge.providers.deepseek import DeepSeekClient, DeepSeekProvider

        descriptor = ModelDescriptor(
            provider_id="deepseek", model_id="deepseek-v4-pro", display_name="Synthetic",
            model_type="cloud", context_window=1_048_576, max_output_tokens=32_768,
            supports_streaming=True,
            capability_source="DeepSeek official /models API response",
            capability_verified_at="2026-11-07T00:00:00+00:00",
        )

        class Client:
            def __init__(self, key):
                self.key = key
                self.credential_source = None

            def set_credential_source(self, value):
                self.credential_source = value

            def request_stats(self):
                return {"discovery_attempted": 1}

        class Registry:
            created_paths = []

            def __init__(self, cache_path):
                self.cache_path = Path(cache_path)
                self.created_paths.append(self.cache_path)

            def refresh_deepseek(self, client):
                self.client = client
                return (descriptor,)

            def get_for_request(self, model_id):
                self.requested_model = model_id
                return descriptor

        test_temp_root = Path(host.ROOT) / "tests" / ".tmp"
        test_temp_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=test_temp_root) as temp:
            root = Path(temp)
            result = host._result_template(("repository_auto",), provider_id="deepseek",
                                           model_id="deepseek-v4-pro")
            with patch.object(CredentialStore, "resolve", return_value=("synthetic-key", "dpapi")), \
                    patch("agentforge.providers.deepseek.DeepSeekClient", Client), \
                    patch("agentforge.model_registry.ModelRegistry", Registry):
                provider = host._create_real_provider(
                    "deepseek", "deepseek-v4-pro", temp_root=root, result=result, stream=True)

        registry_path = Registry.created_paths[-1]
        self.assertEqual(root / "model_registry" / "deepseek.json", registry_path)
        self.assertEqual("deepseek-v4-pro", provider.model)
        self.assertEqual(descriptor, provider.descriptor)
        self.assertTrue(provider.stream)
        self.assertEqual("standard", provider.output_budget.mode)
        self.assertEqual(16_384, provider.max_output_tokens)
        self.assertEqual(1_048_576, result["provider"]["context_window"])
        self.assertTrue(result["provider"]["supports_streaming"])
        self.assertEqual(1, result["model_discovery"]["requests"])
        self.assertEqual("PASS", result["model_discovery"]["status"])
        budget = _model_input_budget(
            provider, 256, provider_id="deepseek", model_id="deepseek-v4-pro")
        self.assertEqual(1_048_576, budget["context_window"])
        self.assertGreater(budget["input_budget"], 4_096)

    def test_deepseek_provider_does_not_discover_without_a_credential(self):
        from agentforge.credential_store import CredentialStore
        from agentforge.model_registry import ModelRegistry

        result = host._result_template(("repository_auto",), provider_id="deepseek",
                                       model_id="deepseek-flash")
        test_temp_root = Path(host.ROOT) / "tests" / ".tmp"
        test_temp_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=test_temp_root) as temp, \
                patch.object(CredentialStore, "resolve", return_value=(None, "none")), \
                patch("agentforge.model_registry.ModelRegistry", side_effect=AssertionError(
                    "discovery must not start without a key")):
            with self.assertRaises(host.AcceptanceBlocked) as raised:
                host._create_real_provider("deepseek", "deepseek-flash",
                                           temp_root=Path(temp), result=result)
        self.assertEqual("deepseek_credential_missing", raised.exception.code)
        self.assertEqual("NOT_APPLICABLE", result["model_discovery"]["status"])

    def test_host_runner_uses_sqlite_memory_for_synthetic_index(self):
        from agentforge.errors import StoreError
        from agentforge.indexer import index_repository
        from agentforge.store import IndexStore

        test_temp_root = Path(host.ROOT) / "tests" / ".tmp"
        test_temp_root.mkdir(parents=True, exist_ok=True)
        try:
            with tempfile.TemporaryDirectory(prefix="af042-synthetic-index-",
                                             dir=test_temp_root) as temp:
                root = Path(temp) / "repository"
                root.mkdir()
                source = root / "metrics.py"
                source.write_text(
                    "def compute_total(value, delta):\n    return value + delta\n",
                    encoding="utf-8",
                )

                # Arbitrary disk locations are rejected by the production Store's
                # managed-index boundary. The runner must use isolated SQLite memory.
                with self.assertRaisesRegex(StoreError, "Database path must be"):
                    IndexStore(str(Path(temp) / "synthetic-index.sqlite3"))

                with IndexStore(":memory:") as store:
                    indexed = index_repository(root, store)
                    self.assertEqual(1, indexed["files"])
                    self.assertEqual(1, indexed["chunks"])
        finally:
            try:
                test_temp_root.rmdir()
            except OSError:
                pass

        runner_source = Path(host.__file__).read_text(encoding="utf-8")
        self.assertIn('with IndexStore(":memory:") as active_index:', runner_source)
        self.assertNotIn('IndexStore(str(temp_root / "synthetic-index.sqlite3"))',
                         runner_source)
        self.assertIn('result["checks"]["synthetic_sqlite_index"] = "PASS"',
                      runner_source)

    def test_trace_redacts_answer_and_observation_content_to_digests(self):
        sanitized = host._sanitize_decisions([{
            "planner_round": 1, "observation_count": 1,
            "observations": [{"tool_id": "synthetic", "status": "completed",
                              "error_code": None, "output": {"secret": "synthetic-value"}}],
            "selected_calls": [{"call_id": "c1", "task_id": "t1", "tool_id": "synthetic",
                                "dependencies": [], "arguments": {"value": "synthetic-value"}}],
            "done": False, "final_answer": "must not be persisted",
        }])[0]
        encoded = str(sanitized)
        self.assertNotIn("synthetic-value", encoded)
        self.assertNotIn("must not be persisted", encoded)
        self.assertIn("arguments_digest", encoded)
        self.assertIn("output_digest", encoded)

    def test_powershell_runner_never_changes_service_lifecycle(self):
        source = (Path(host.__file__).with_suffix(".ps1")).read_text(encoding="utf-8-sig")
        self.assertNotIn("Start-Service", source)
        self.assertNotIn("Stop-Service", source)
        self.assertIn("[string] $Scenario", source)
        self.assertIn("'--scenario', $Scenario", source)
        self.assertNotIn("DeepSeek host acceptance requires", source)
        self.assertIn("$safeStartupMessages -contains $_.Exception.Message", source)
        self.assertIn('Write-Host ("Host runner could not start: " + $_.Exception.Message)', source)
        gate = source.index("$env:CODEX_SANDBOX_NETWORK_DISABLED -eq '1'")
        credential_load = source.index("Get-ConversationDatabaseKey")
        self.assertGreater(credential_load, gate)
        self.assertIn("Remove-Item Env:AGENTFORGE_DB_PASSWORD", source)


if __name__ == "__main__":
    unittest.main()
