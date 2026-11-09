"""Offline safety and lifecycle tests for the Phase 3.3 host runner."""
from contextlib import redirect_stdout
import ast
import io
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

import AF033_AGENT_LOOP_HOST_ACCEPTANCE as host
from agentforge.agent_execution import LLMExecutionPlanner


class _PlannerProvider:
    max_output_tokens = 128
    model = "offline-test-only"
    provider_id = "offline"
    context_assembler = None

    class _Client:
        max_output_tokens = 128

    client = _Client()
    descriptor = None

    def plan_completion(self, messages, *, max_output_tokens, timeout_seconds):
        return {"unused": True}

    def request_stats(self):
        return {"attempted": 1, "sent": 1, "succeeded": 1,
                "failed": 0, "retries": 0}


class AgentHostRunnerTests(unittest.TestCase):
    def test_completed_turn_persists_explicit_objective_from_scenario(self):
        class ConversationStore:
            def __init__(self):
                self.messages = []

            def get_messages(self, conversation_id, repository_id):
                return self.messages

        class SessionService:
            def commit_execution_turn(self, store, conversation_id, repository_id, execution_id,
                                      user_content, answer, provider, *, request_diagnostics):
                self.user_content = user_content
                self.request_diagnostics = request_diagnostics
                store.messages = [
                    {"role": "user", "content": user_content, "metadata": {}},
                    {"role": "assistant", "content": answer.text,
                     "metadata": {"agent_execution_id": execution_id}},
                ]

            def load_context(self, store, conversation_id, repository_id):
                return SimpleNamespace(history=[SimpleNamespace(content=store.messages[-1]["content"])])

        service = SessionService()
        store = ConversationStore()
        objective = "Read the synthetic counter and report its value."
        report = SimpleNamespace(status="completed", final_answer="The value is 17.",
                                 execution_id="exec-test")
        planner = SimpleNamespace(request_diagnostics=[])

        restored = host._persist_completed_turn(service, store, "conversation", "repository",
                                                report, objective, _PlannerProvider(), planner)

        self.assertTrue(restored["persisted"])
        self.assertEqual(service.user_content, objective)
        self.assertEqual(store.messages[-1]["metadata"]["agent_execution_id"], "exec-test")

    def test_temporary_chroma_reopen_closes_each_client_before_the_next_open(self):
        clients = []
        collections = set()

        class Collection:
            def __init__(self, name):
                self.name = name

        class Client:
            def __init__(self):
                self.closed = False
                clients.append(self)

            def get_or_create_collection(self, name, metadata):
                collections.add(name)
                return Collection(name)

            def get_collection(self, name):
                if name not in collections:
                    raise LookupError(name)
                return Collection(name)

            def close(self):
                self.closed = True

        def create_client(*, path):
            if clients and not clients[-1].closed:
                raise AssertionError("previous Chroma client still owns its SQLite handles")
            return Client()

        result = host._verify_temporary_chroma_reopen(create_client, Path("isolated"), "synthetic")

        self.assertEqual(result, {"collection_name": "synthetic", "reopened": True})
        self.assertEqual(len(clients), 2)
        self.assertTrue(all(client.closed for client in clients))

    def test_report_starts_with_every_real_scenario_unmeasured(self):
        report = host._build_report()
        self.assertEqual(report["status"], "PRECHECK")
        self.assertEqual(set(report["not_measured"]), set(host.SCENARIO_NAMES))
        self.assertEqual(report["chat_requests"]["sent"], 0)
        self.assertFalse(report["services_started_or_stopped"])
        self.assertEqual(report["cloud_requests"], 0)

    def test_runner_blocks_before_credentials_when_qwen_listener_is_absent(self):
        captured = []
        with patch.dict(os.environ, {}, clear=True), \
                patch.object(host, "_safe_listener_probe", return_value=False), \
                patch.object(host, "_write_report", side_effect=lambda result: (
                    captured.append(result.copy()) or (Path("result.json"), Path("result.md")))), \
                patch.object(host.getpass, "getpass") as prompt, \
                redirect_stdout(io.StringIO()):
            exit_code = host._main()
        self.assertEqual(exit_code, 2)
        self.assertEqual(captured[0]["status"], "BLOCKED")
        self.assertEqual(captured[0]["failure_stage"], "qwen_preflight")
        self.assertEqual(captured[0]["chat_requests"]["sent"], 0)
        prompt.assert_not_called()

    def test_runner_blocks_before_credentials_when_postgres_listener_is_absent(self):
        captured = []
        with patch.dict(os.environ, {"AGENTFORGE_LLAMA_API_KEY": "test-only",
                                     "AGENTFORGE_DB_PASSWORD": "test-only"}, clear=True), \
                patch.object(host, "_safe_listener_probe", side_effect=[True, False]), \
                patch.object(host, "_health_ready", return_value={"status": "ready"}), \
                patch.object(host, "_write_report", side_effect=lambda result: (
                    captured.append(result.copy()) or (Path("result.json"), Path("result.md")))), \
                patch.object(host.getpass, "getpass") as prompt, \
                redirect_stdout(io.StringIO()):
            exit_code = host._main()
        self.assertEqual(exit_code, 2)
        self.assertEqual(captured[0]["failure_stage"], "postgres_preflight")
        self.assertEqual(captured[0]["chat_requests"]["sent"], 0)
        prompt.assert_not_called()

    def test_temporary_scope_accepts_only_owned_run_directory(self):
        parent_existed = host.TEST_TEMP_ROOT.exists()
        temp_root = host.TEST_TEMP_ROOT / f"af033-agent-host-{uuid4().hex}"
        temp_root.mkdir(parents=True)
        try:
            database = "af033_agent_" + uuid4().hex[:20]
            self.assertEqual(host._validate_temp_scope(database, temp_root),
                             (database, temp_root.resolve()))
            with self.assertRaises(host.HostAcceptanceBlocked):
                host._validate_temp_scope("agentforge", temp_root)
            outside = host.ROOT / "tests" / f"unowned-{uuid4().hex}"
            outside.mkdir()
            try:
                with self.assertRaises(host.HostAcceptanceBlocked):
                    host._validate_temp_scope(database, outside)
            finally:
                outside.rmdir()
        finally:
            temp_root.rmdir()
            if not parent_existed:
                try:
                    host.TEST_TEMP_ROOT.rmdir()
                except OSError:
                    pass

    def test_generated_database_identifier_is_strictly_run_scoped(self):
        run_id = uuid4().hex
        self.assertIsNotNone(host.TEST_DATABASE_RE.fullmatch("af033_agent_" + run_id[:20]))
        self.assertIsNone(host.TEST_DATABASE_RE.fullmatch("agentforge"))

    def test_runner_marks_database_owned_at_create_ack_and_cleans_only_owned_database(self):
        tree = ast.parse(Path(host.__file__).read_text(encoding="utf-8"))
        main = next(node for node in tree.body
                    if isinstance(node, ast.FunctionDef) and node.name == "_main")
        create_calls = [node for node in ast.walk(main) if isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Name)
                        and node.func.id == "_create_test_database"]
        self.assertEqual(1, len(create_calls))
        self.assertTrue({item.arg for item in create_calls[0].keywords}.issuperset(
            {"on_stage", "on_database_absent", "on_created"}))
        source = Path(host.__file__).read_text(encoding="utf-8")
        self.assertIn('if result["temporary_resources"].get("database_created") and database', source)

    def test_provision_failure_marks_ambiguous_create_unverified(self):
        result = host._build_report()
        result["database_provision"].update(
            status="running", operation="create_database", database_absence_confirmed=True)
        host._record_database_provision_failure(result, OSError("connection lost"))
        self.assertEqual("creation_outcome_unknown", result["database_provision"]["status"])
        self.assertEqual("unverified", result["cleanup"]["database"])
        self.assertTrue(host._database_create_outcome_ambiguous("08006"))
        self.assertTrue(host._database_create_outcome_ambiguous("57P01"))
        self.assertFalse(host._database_create_outcome_ambiguous("42501"))

    def test_provision_failure_after_create_ack_keeps_owned_cleanup_pending(self):
        result = host._build_report()
        result["temporary_resources"].update(database_created=True,
                                               database_name="af033_agent_" + "a" * 20)
        result["database_provision"].update(status="created", operation="admin_connection_close")
        host._record_database_provision_failure(result, OSError("connection close failed"))
        self.assertEqual("created_cleanup_required", result["database_provision"]["status"])
        self.assertEqual("pending", result["cleanup"]["database"])

    def test_planner_recorder_requires_the_real_execution_planner_type(self):
        delegate = LLMExecutionPlanner(_PlannerProvider(), max_output_tokens=64)
        recorder = host.RecordingRealPlanner(delegate, scenario="offline-unit-test")
        self.assertIs(recorder.delegate, delegate)
        with self.assertRaises(TypeError):
            host.RecordingRealPlanner(object(), scenario="must-not-be-a-mock-planner")

    def test_capped_provider_stops_before_exceeding_request_ceiling(self):
        delegate = _PlannerProvider()
        provider = host.CappedLocalProvider(delegate, limit=1)
        provider.plan_completion([], max_output_tokens=64, timeout_seconds=1)
        with self.assertRaises(host.HostAcceptanceBlocked) as raised:
            provider.plan_completion([], max_output_tokens=64, timeout_seconds=1)
        self.assertEqual(raised.exception.stage, "request_budget")
        self.assertEqual(provider.requests_attempted, 1)

    def test_only_synthetic_scoped_tool_definition_is_registered_by_helper(self):
        registry = host._base_registry()
        host._register_tool(registry, "read_fixture", "Read an in-memory synthetic fixture.", {},
                            lambda _args, _context: {"value": "synthetic"})
        definition = registry.get("read_fixture")
        self.assertEqual(definition.permission_scope, "synthetic:read")
        self.assertFalse(definition.side_effect)
        self.assertEqual(definition.capabilities, frozenset({"synthetic", "acceptance"}))
        self.assertIsNone(registry.get("shell"))
        self.assertIsNone(registry.get("filesystem"))

    def test_large_tool_observation_is_summarized_and_bounded(self):
        safe = host._safe_output({"text": "x" * 10_000}, max_bytes=128)
        self.assertTrue(safe["truncated"])
        self.assertLessEqual(len(safe["preview"].encode("utf-8")), 128)
        self.assertEqual(len(safe["sha256"]), 64)

    def test_blocked_status_is_distinct_from_runner_failure(self):
        report = host._build_report()
        with patch.object(host, "_write_report", return_value=(Path("result"), Path("report"))), \
                redirect_stdout(io.StringIO()):
            code = host._blocked(report, "service_preflight", "local_service_missing")
        self.assertEqual(code, 2)
        self.assertEqual(report["status"], "BLOCKED")
        self.assertEqual(report["failure_stage"], "service_preflight")

    def test_host_runner_does_not_import_process_or_shell_execution(self):
        source = Path(host.__file__).read_text(encoding="utf-8")
        self.assertNotIn("import subprocess", source)
        self.assertNotIn("os.system(", source)
        self.assertNotIn("shell=True", source)

    def test_powershell_entry_never_starts_services_and_reads_credentials_only_after_readiness(self):
        source = (Path(__file__).parents[1] / "AF033_AGENT_LOOP_HOST_ACCEPTANCE.ps1").read_text(
            encoding="utf-8")
        self.assertNotIn("Start-Service", source)
        self.assertNotIn("Stop-Service", source)
        network_gate = source.index("$env:CODEX_SANDBOX_NETWORK_DISABLED -eq '1'")
        readiness = source.index("elseif ($qwenReady -and $postgresReady)")
        self.assertGreater(source.index("Get-ConversationDatabaseKey"), readiness)
        self.assertGreater(source.index("Get-LauncherKey"), readiness)
        self.assertGreater(source.index("Get-ConversationDatabaseKey"), network_gate)

    def test_approval_resume_is_wired_to_the_database_reconnect_callback(self):
        tree = ast.parse(Path(host.__file__).read_text(encoding="utf-8"))
        main = next(node for node in tree.body
                    if isinstance(node, ast.FunctionDef) and node.name == "_main")
        nested_functions = {node.name for node in ast.walk(main) if isinstance(node, ast.FunctionDef)}
        calls = [node for node in ast.walk(main) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Name) and node.func.id == "_run_approval_scenario"]
        self.assertIn("reconnect_database", nested_functions)
        self.assertEqual(len(calls), 1)
        self.assertTrue(any(isinstance(argument, ast.Name)
                            and argument.id == "reconnect_database" for argument in calls[0].args))


if __name__ == "__main__":
    unittest.main()
