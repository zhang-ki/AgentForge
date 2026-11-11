import json
import unittest
from unittest.mock import patch

from PHASE5_2_2_3_HOST_ACCEPTANCE import (
    C2_CHAT_REQUEST_CAP, DEFAULT_SYNTHETIC_SCENARIOS, MAX_DEEPSEEK_CHAT_REQUESTS,
    SYNTHETIC_CLOUD_CONSENT,
    _c1_structured_context_check, _c2_execution_checks, _c_scenario_status, _parse_args,
    _chat_request_cap,
    _canonical_sha256, _confirm_synthetic_cloud_requests, _deepseek_chat_wrapper,
    _effective_evidence_count, _load_capability_probe_evidence, _tool_capability_preflight,
    _contract_evidence_summary, _deepseek_connect_wrapper, _safe_failure_diagnostics, _scenario_status,
    _deepseek_network_request_wrapper, _embedding_wrapper, _evidence_identity_sha256,
    _simple_timed_factory, _plan_validation_wrapper, _safe_agent_execution_summary,
    _agent_execution_summary_source,
    _required_c2_tool_results_succeeded,
    _safe_retrieval_summary,
    _ensure_database_password,
    _postgres_primary_key_snapshot, _repository_registry_snapshot_value,
    _should_run_active_repository_diagnostics,
)


class RunnerInstrumentationTests(unittest.TestCase):
    def setUp(self):
        self.metrics = {"phase": "test_phase", "calls": []}

    def test_runner_default_keeps_complete_scenario_matrix_and_c2_filter_is_explicit(self):
        import contextlib
        import io
        self.assertEqual(("A", "B", "C1", "C2"), DEFAULT_SYNTHETIC_SCENARIOS)
        self.assertIsNone(_parse_args([]).scenario)
        self.assertEqual("C2", _parse_args(["--scenario", "C2"]).scenario)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            _parse_args(["--scenario", "A"])

    def test_c2_only_run_skips_mutating_active_repository_diagnostics(self):
        self.assertTrue(_should_run_active_repository_diagnostics(None))
        self.assertFalse(_should_run_active_repository_diagnostics(("C2",)))

    def test_timed_method_factory_wraps_without_invoking_original_until_call(self):
        class Example:
            calls = 0

            def read(self, value):
                type(self).calls += 1
                return value * 2

        factory = _simple_timed_factory(
            self.metrics, "calls", lambda result: {"result": result},
        )
        wrapped = factory(Example.read)
        self.assertEqual(Example.calls, 0)
        with patch.object(Example, "read", wrapped):
            self.assertEqual(Example().read(21), 42)

        self.assertEqual(Example.calls, 1)
        self.assertEqual(len(self.metrics["calls"]), 1)
        self.assertEqual(self.metrics["calls"][0]["phase"], "test_phase")
        self.assertEqual(self.metrics["calls"][0]["result"], 42)
        self.assertGreaterEqual(self.metrics["calls"][0]["elapsed_ms"], 0)

    def test_timed_method_factory_records_failure_and_preserves_exception(self):
        class Example:
            def fail(self):
                raise ValueError("expected synthetic failure")

        wrapped = _simple_timed_factory(self.metrics, "calls")(Example.fail)
        with patch.object(Example, "fail", wrapped):
            with self.assertRaisesRegex(ValueError, "expected synthetic failure"):
                Example().fail()

        self.assertEqual(len(self.metrics["calls"]), 1)
        self.assertEqual(self.metrics["calls"][0]["error_type"], "ValueError")

    def test_agent_execution_summary_keeps_safe_read_diagnostic_without_parameter_values(self):
        import json
        secret = "synthetic-path-value-must-not-be-recorded"
        summary = _safe_agent_execution_summary({
            "status": "failed", "tool_results": [{
                "call_id": "read-call", "task_id": "read-task", "tool_id": "read",
                "status": "failed", "error_code": "INVALID_ARGUMENTS",
                "execution_metadata": {
                    "stage": "tool_execution", "attempts": 1,
                    "argument_diagnostic": {
                        "schema_error_code": "handler_rejected_arguments",
                        "handler_reason_code": "PATH_NOT_ALLOWED",
                        "argument_fields": ["path"],
                        "argument_types": {"path": "string"},
                        "handler_details": {"field": "path"},
                        "raw_value": secret,
                    },
                },
            }],
        })
        serialized = json.dumps(summary, sort_keys=True)
        self.assertIn("PATH_NOT_ALLOWED", serialized)
        self.assertIn('"path": "string"', serialized)
        self.assertNotIn(secret, serialized)
        self.assertNotIn("raw_value", serialized)

    def test_failed_agent_execution_summary_reads_product_tool_execution_payload(self):
        from types import SimpleNamespace
        secret = "synthetic-path-value-must-not-be-recorded"
        value = SimpleNamespace(
            agent_execution={"status": "failed", "tool_call_count": 2},
            retrieval={"tool_execution": {
                "execution_id": "execution-1", "status": "failed", "state": "FAILED",
                "rounds": 2, "tool_call_count": 2, "results": [{
                    "call_id": "read-call", "task_id": "read-task", "tool_id": "read",
                    "status": "failed", "error_code": "INVALID_ARGUMENTS",
                    "failure_phase": "tool_execution",
                    "execution_metadata": {"argument_diagnostic": {
                        "schema_error_code": "handler_rejected_arguments",
                        "handler_reason_code": "PATH_NOT_ALLOWED",
                        "argument_fields": ["path"],
                        "argument_types": {"path": "string"},
                        "raw_value": secret,
                    }},
                }],
            }},
        )

        summary = _safe_agent_execution_summary(_agent_execution_summary_source(value))
        serialized = json.dumps(summary, sort_keys=True)
        self.assertEqual("failed", summary["status"])
        self.assertEqual("INVALID_ARGUMENTS", summary["tool_results"][0]["error_code"])
        self.assertEqual("PATH_NOT_ALLOWED", summary["tool_results"][0][
            "execution_metadata"]["argument_diagnostic"]["handler_reason_code"])
        self.assertNotIn(secret, serialized)
        self.assertNotIn("raw_value", serialized)

    def test_c2_accepts_single_invalid_read_only_after_distinct_read_succeeds(self):
        recovered = [
            {"call_id": "search-1", "task_id": "search-1", "tool_id": "search",
             "status": "completed"},
            {"call_id": "read-1", "task_id": "read-1", "tool_id": "read",
             "status": "failed", "error_code": "INVALID_ARGUMENTS"},
            {"call_id": "read-2", "task_id": "read-2", "tool_id": "read",
             "status": "completed"},
        ]
        self.assertTrue(_required_c2_tool_results_succeeded(recovered))
        self.assertTrue(_required_c2_tool_results_succeeded([
            recovered[0], recovered[2],
        ]))

    def test_c2_recovery_gate_rejects_unrecovered_repeated_or_unrelated_failures(self):
        base = [
            {"call_id": "search-1", "task_id": "search-1", "tool_id": "search",
             "status": "completed"},
            {"call_id": "read-1", "task_id": "read-1", "tool_id": "read",
             "status": "failed", "error_code": "INVALID_ARGUMENTS"},
            {"call_id": "read-2", "task_id": "read-2", "tool_id": "read",
             "status": "completed"},
        ]
        self.assertFalse(_required_c2_tool_results_succeeded(base[:2]))
        repeated = base[:2] + [
            {"call_id": "read-2", "task_id": "read-2", "tool_id": "read",
             "status": "failed", "error_code": "INVALID_ARGUMENTS"},
            base[2],
        ]
        self.assertFalse(_required_c2_tool_results_succeeded(repeated))
        unrelated = base[:]
        unrelated[1] = {**unrelated[1], "error_code": "PERMISSION_DENIED"}
        self.assertFalse(_required_c2_tool_results_succeeded(unrelated))
        duplicate = base[:]
        duplicate[2] = {**duplicate[2], "call_id": "read-1"}
        self.assertFalse(_required_c2_tool_results_succeeded(duplicate))

    def test_retrieval_summary_preserves_evidence_rejection_codes_without_raw_content(self):
        import json
        secret = "synthetic-source-content-must-not-be-recorded"
        source = {
            "retrieval_status": "evidence_available",
            "evidence_assessment": {
                "candidate_count": 1,
                "candidate_decisions": [{
                    "status": "unverified", "reason_code": "LOW_CONFIDENCE_ROUTE",
                }],
                "raw_excerpt": secret,
            },
            "agent_read_validation": {
                "verified_read_count": 1,
                "accepted_read_count": 0,
                "source_rejection_reason_codes": ["CONTENT_HASH_MISMATCH"],
                "relevance_rejection_reason_codes": ["LOW_CONFIDENCE_ROUTE"],
                "user_query_route_confidence": "medium",
                "search_route_confidences": ["low"],
            },
            "request_body": secret,
        }

        summary = _safe_retrieval_summary(source)
        serialized = json.dumps(summary, sort_keys=True)

        self.assertEqual("CONTENT_HASH_MISMATCH", summary["agent_read_validation"][
            "source_rejection_reason_codes"][0])
        self.assertEqual("LOW_CONFIDENCE_ROUTE", summary["evidence_assessment"][
            "candidate_decisions"][0]["reason_code"])
        self.assertEqual("medium", summary["agent_read_validation"][
            "user_query_route_confidence"])
        self.assertNotIn(secret, serialized)
        self.assertNotIn("request_body", serialized)

    def test_repository_registry_snapshot_hashes_formal_rows_without_exposing_paths(self):
        rows = [{
            "repository_id": "synthetic-or-formal-id",
            "canonical_path": "D:/private/source/root",
            "display_name": "Private repository label",
            "index_path": "D:/private/index.sqlite3",
            "indexed_at": "2026-11-11T00:00:00Z",
            "registered_at": "2026-11-10T00:00:00Z",
            "active": True,
        }]

        before = _repository_registry_snapshot_value("synthetic-or-formal-id", rows)
        after = _repository_registry_snapshot_value("synthetic-or-formal-id", [
            {**rows[0], "display_name": "Changed label"},
        ])

        self.assertEqual(1, before["repository_count"])
        self.assertNotEqual(before["registry_sha256"], after["registry_sha256"])
        serialized = json.dumps(before, sort_keys=True)
        self.assertNotIn("D:/private", serialized)
        self.assertNotIn("Private repository label", serialized)

    def test_postgresql_snapshot_is_read_only_and_hashes_primary_keys_only(self):
        from sqlalchemy import Column, Integer, MetaData, String, Table

        metadata = MetaData()
        conversations = Table(
            "synthetic_conversations", metadata,
            Column("id", Integer, primary_key=True),
            Column("content", String),
        )
        messages = Table(
            "synthetic_messages", metadata,
            Column("id", String, primary_key=True),
            Column("content", String),
        )

        class Result:
            def __init__(self, values):
                self.values = values

            def all(self):
                return list(self.values)

        class Connection:
            def __init__(self):
                self.read_only = False

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def begin(self):
                class Transaction:
                    def __enter__(self):
                        return self

                    def __exit__(self, *_args):
                        return False
                return Transaction()

            def execute(self, statement):
                if str(statement) == "SET TRANSACTION READ ONLY":
                    self.read_only = True
                    return Result(())
                table_name = statement.get_final_froms()[0].name
                return Result({
                    "synthetic_conversations": [(1,), (2,)],
                    "synthetic_messages": [("opaque-pk",)],
                }[table_name])

        class Engine:
            def __init__(self):
                self.connection = Connection()

            def connect(self):
                return self.connection

        engine = Engine()
        snapshot = _postgres_primary_key_snapshot(engine, {
            "synthetic_conversations": conversations,
            "synthetic_messages": messages,
        })

        self.assertTrue(engine.connection.read_only)
        self.assertEqual(2, snapshot["rows"]["synthetic_conversations"]["row_count"])
        self.assertEqual(1, snapshot["rows"]["synthetic_messages"]["row_count"])
        serialized = json.dumps(snapshot, sort_keys=True)
        self.assertNotIn("opaque-pk", serialized)
        self.assertNotIn("content", serialized)

    def test_plan_validation_trace_records_safe_call_shape_and_reason_code(self):
        import json
        from types import SimpleNamespace
        from agentforge.agent_execution import PlanValidationError, ToolDefinition, ToolErrorCode

        definition = ToolDefinition(
            "read", "Read", "Synthetic reader", "1", frozenset({"repository.read"}),
            {"type": "object", "properties": {"path": {"type": "string"}},
             "required": ["path"], "additionalProperties": False},
            {"type": "object"}, "repository:read",
        )
        secret = "synthetic-private-path-value"
        call = SimpleNamespace(call_id="call-42", task_id="task-42", tool_id="read",
                               arguments={"path": secret}, dependencies=())
        plan = SimpleNamespace(calls=(call,))
        validator = SimpleNamespace(
            registry=SimpleNamespace(get=lambda tool_id: definition if tool_id == "read" else None),
            authorization=SimpleNamespace(permission_scopes=frozenset({"repository:read"})),
        )
        metrics = {"phase": "scenario_C2_execution"}

        def reject(_self, _plan):
            error = PlanValidationError("synthetic rejection", ToolErrorCode.DUPLICATE_CALL,
                diagnostic_code="failed_retry_budget_exhausted",
                diagnostic_details={"tool_id": "read", "signature_hash": "a" * 16,
                                    "prior_status": "failed", "failed_attempt_count": 2})
            raise error

        wrapped = _plan_validation_wrapper(reject, metrics)
        with self.assertRaises(PlanValidationError):
            wrapped(validator, plan)
        trace = metrics["plan_validations"][0]
        serialized = json.dumps(trace, sort_keys=True)
        self.assertEqual("failed_retry_budget_exhausted", trace["diagnostic_code"])
        self.assertEqual(["path"], trace["calls"][0]["argument_fields"])
        self.assertEqual({"path": "string"}, trace["calls"][0]["argument_types"])
        self.assertEqual(16, len(trace["calls"][0]["call_id_hash"]))
        self.assertNotIn(secret, serialized)

    def test_contract_evidence_without_hash_is_reported_as_missing_not_passed(self):
        from agentforge.answer_contract import ContractEvidence
        from types import SimpleNamespace

        evidence = ContractEvidence(
            source_id="E1", relative_path="synthetic.py", start_line=2, end_line=3,
            excerpt="return 7", channels=("vector", "lexical"),
            resolution_status="not_applicable",
        )
        summary = _contract_evidence_summary(SimpleNamespace(evidence=(evidence,)))

        self.assertEqual("PARTIAL", summary["content_hash_status"])
        self.assertEqual(1, summary["missing_content_hash_count"])
        self.assertEqual("E1", summary["items"][0]["source_id"])
        self.assertEqual("synthetic.py:2-3", summary["items"][0]["citation"])
        self.assertIsNone(summary["items"][0]["content_hash"])
        self.assertFalse(summary["items"][0]["content_hash_available_on_contract"])

    def test_safe_failure_diagnostics_preserve_location_and_redact_credentials(self):
        try:
            raise ValueError("password=private api_key:secret postgres://user:pass@host/db")
        except ValueError as exc:
            result = _safe_failure_diagnostics(exc, "result_extraction")

        self.assertEqual("result_extraction", result["stage"])
        self.assertEqual("ValueError", result["error_type"])
        self.assertTrue(result["traceback_frames"])
        self.assertNotIn("private", result["safe_message"])
        self.assertNotIn("secret", result["safe_message"])
        self.assertNotIn("user:pass", result["safe_message"])
        self.assertFalse(result["request_or_source_content_recorded"])

    def test_scenario_status_distinguishes_pass_limited_and_failed_checks(self):
        complete = {"retrieval": True, "answer": True}
        self.assertEqual("PASS", _scenario_status(complete, "answered"))
        self.assertEqual("PARTIAL", _scenario_status(complete, "limited"))
        self.assertEqual("FAIL", _scenario_status(complete, "refused"))
        self.assertEqual("FAIL", _scenario_status({**complete, "answer": False}, "answered"))

    def test_network_counter_aggregates_successful_chat_at_request_boundary(self):
        class Client:
            def __init__(self):
                self.stats = {"attempted": 0, "sent": 0, "succeeded": 0,
                              "failed": 0, "retries": 0}

            def request_stats(self):
                return dict(self.stats)

        client = Client()

        def send(self, method, path, **_kwargs):
            self.stats.update(attempted=1, sent=1, succeeded=1)
            return {"choices": []}

        wrapped = _deepseek_network_request_wrapper(send, self.metrics)
        self.assertEqual({"choices": []}, wrapped(client, "POST", "/chat/completions"))
        self.assertEqual({"attempted": 1, "sent": 1, "succeeded": 1,
                          "failed": 0, "retries": 0},
                         self.metrics["network_request_counts"]["chat"])

    def test_network_counter_records_failed_unsent_chat_and_model_discovery_separately(self):
        metrics = {"phase": "test", "network_request_counts": {
            "chat": {"attempted": 0, "sent": 0, "succeeded": 0, "failed": 0, "retries": 0},
            "model_discovery": {"attempted": 0, "sent": 0, "succeeded": 0, "failed": 0},
        }}

        class Client:
            def __init__(self):
                self.stats = {"attempted": 0, "sent": 0, "succeeded": 0,
                              "failed": 0, "retries": 0}

            def request_stats(self):
                return dict(self.stats)

        client = Client()

        def fail_unsent(self, method, path, **_kwargs):
            self.stats.update(attempted=1, failed=1)
            raise OSError("synthetic connection failure")

        wrapped_failure = _deepseek_network_request_wrapper(fail_unsent, metrics)
        with self.assertRaises(OSError):
            wrapped_failure(client, "POST", "/chat/completions")

        class Connection:
            def request(self, method, path):
                self.last_request = (method, path)

        def connect(self):
            return Connection()

        client._connect = _deepseek_connect_wrapper(connect, metrics).__get__(client, Client)

        def discover(self, method, path, **_kwargs):
            self._connect().request(method, path)
            return [{"id": "synthetic-model"}]

        wrapped_discovery = _deepseek_network_request_wrapper(discover, metrics)
        self.assertEqual([{"id": "synthetic-model"}],
                         wrapped_discovery(client, "GET", "/models"))
        self.assertEqual({"attempted": 1, "sent": 0, "succeeded": 0,
                          "failed": 1, "retries": 0}, metrics["network_request_counts"]["chat"])
        self.assertEqual({"attempted": 1, "sent": 1, "succeeded": 1,
                          "failed": 0}, metrics["network_request_counts"]["model_discovery"])

    def test_failed_model_discovery_counts_sent_request_at_connection_boundary(self):
        metrics = {"phase": "test", "network_request_counts": {
            "chat": {"attempted": 0, "sent": 0, "succeeded": 0, "failed": 0, "retries": 0},
            "model_discovery": {"attempted": 0, "sent": 0, "succeeded": 0, "failed": 0},
        }}

        class Client:
            def request_stats(self):
                return {"attempted": 0, "sent": 0, "succeeded": 0,
                        "failed": 0, "retries": 0}

        class Connection:
            def request(self, method, path):
                self.last_request = (method, path)

        client = Client()
        client._connect = _deepseek_connect_wrapper(
            lambda _self: Connection(), metrics,
        ).__get__(client, Client)

        def fail_after_send(self, method, path, **_kwargs):
            self._connect().request(method, path)
            raise OSError("synthetic response failure")

        wrapped = _deepseek_network_request_wrapper(fail_after_send, metrics)
        with self.assertRaises(OSError):
            wrapped(client, "GET", "/models")

        self.assertEqual({"attempted": 1, "sent": 1, "succeeded": 0,
                          "failed": 1}, metrics["network_request_counts"]["model_discovery"])

    def test_request_and_embedding_telemetry_aggregates_across_provider_instances(self):
        metrics = {"phase": "test", "network_request_counts": {
            "chat": {"attempted": 0, "sent": 0, "succeeded": 0, "failed": 0, "retries": 0},
            "model_discovery": {"attempted": 0, "sent": 0, "succeeded": 0, "failed": 0},
        }, "network_request_events": [], "embedding_requests": []}

        class Connection:
            def request(self, method, path):
                self.last_request = (method, path)

        class Client:
            def __init__(self, *, fail=False, retries=0):
                self.fail = fail
                self.stats = {"attempted": 0, "sent": 0, "succeeded": 0,
                              "failed": 0, "retries": 0}

            def request_stats(self):
                return dict(self.stats)

            def _connect(self):
                return Connection()

            def _request(self, method, path):
                self.stats["attempted"] += 1
                self.stats["retries"] += 1 if self.fail else 0
                self._connect().request(method, path)
                if self.fail:
                    self.stats["sent"] += 1
                    self.stats["failed"] += 1
                    raise OSError("synthetic request failure")
                self.stats["sent"] += 1
                if method == "GET":
                    return [{"id": "synthetic-model"}]
                self.stats["succeeded"] += 1
                return {"choices": []}

        wrapped_connect = _deepseek_connect_wrapper(Client._connect, metrics)
        wrapped_request = _deepseek_network_request_wrapper(Client._request, metrics)
        clients = (Client(), Client(fail=True))
        clients[0]._connect = wrapped_connect.__get__(clients[0], Client)
        clients[1]._connect = wrapped_connect.__get__(clients[1], Client)
        clients[0]._request = wrapped_request.__get__(clients[0], Client)
        clients[1]._request = wrapped_request.__get__(clients[1], Client)

        self.assertEqual({"choices": []}, clients[0]._request("POST", "/chat/completions"))
        with self.assertRaises(OSError):
            clients[1]._request("POST", "/chat/completions")
        self.assertEqual([{"id": "synthetic-model"}],
                         clients[0]._request("GET", "/models"))
        self.assertEqual({"attempted": 2, "sent": 2, "succeeded": 1,
                          "failed": 1, "retries": 1}, metrics["network_request_counts"]["chat"])
        self.assertEqual({"attempted": 1, "sent": 1, "succeeded": 1,
                          "failed": 0}, metrics["network_request_counts"]["model_discovery"])
        self.assertEqual(3, len(metrics["network_request_events"]))
        self.assertTrue(all("request_body" not in event and "prompt" not in event
                            for event in metrics["network_request_events"]))

        class Embedding:
            def _request_embeddings(self, values):
                return [[float(len(values))]]

        embed_metrics = {"phase": "test", "embedding_requests": []}
        wrapped_embedding = _embedding_wrapper(Embedding._request_embeddings, embed_metrics)
        first, second = Embedding(), Embedding()
        first._request_embeddings = wrapped_embedding.__get__(first, Embedding)
        second._request_embeddings = wrapped_embedding.__get__(second, Embedding)
        first._request_embeddings("query one")
        second._request_embeddings(["document one", "document two"])
        self.assertEqual(["query", "documents"],
                         [item["kind"] for item in embed_metrics["embedding_requests"]])
        self.assertEqual([1, 2],
                         [item["item_count"] for item in embed_metrics["embedding_requests"]])

    def test_cloud_request_consent_requires_exact_fresh_phrase_and_displays_cap(self):
        displayed = []
        self.assertTrue(_confirm_synthetic_cloud_requests(
            input_fn=lambda: SYNTHETIC_CLOUD_CONSENT,
            output_fn=lambda message, **_kwargs: displayed.append(message),
        ))
        self.assertEqual(1, len(displayed))
        self.assertIn(f"Up to {MAX_DEEPSEEK_CHAT_REQUESTS} DeepSeek chat requests", displayed[0])
        self.assertIn("synthetic", displayed[0])

        self.assertFalse(_confirm_synthetic_cloud_requests(
            input_fn=lambda: "yes",
            output_fn=lambda *_args, **_kwargs: None,
        ))

    def test_c2_capability_evidence_is_only_accepted_for_c2_run(self):
        parsed = _parse_args([
            "--scenario", "C2", "--capability-probe-evidence",
            "PHASE5_2_2_3_R4_2_1_CAPABILITY_PROBE_EVIDENCE_20261111_01.json",
        ])
        self.assertEqual("C2", parsed.scenario)
        self.assertEqual(C2_CHAT_REQUEST_CAP, 26)
        self.assertEqual(MAX_DEEPSEEK_CHAT_REQUESTS, 27)
        self.assertEqual(26, _chat_request_cap(capability_probe_used=True))
        self.assertEqual(17, _chat_request_cap(
            capability_probe_used=True, prior_c2_chat_requests=9,
        ))
        self.assertEqual(1, _chat_request_cap(
            capability_probe_used=True, prior_c2_chat_requests=25,
        ))
        with self.assertRaisesRegex(ValueError, "budget remains"):
            _chat_request_cap(capability_probe_used=True, prior_c2_chat_requests=26)
        with self.assertRaisesRegex(ValueError, "non-negative"):
            _chat_request_cap(capability_probe_used=True, prior_c2_chat_requests=-1)
        second_attempt = _parse_args([
            "--scenario", "C2", "--capability-probe-evidence", "probe.json",
            "--prior-c2-chat-requests", "9",
        ])
        self.assertEqual(9, second_attempt.prior_c2_chat_requests)
        with self.assertRaises(SystemExit):
            _parse_args(["--scenario", "C2", "--prior-c2-chat-requests", "9"])
        with self.assertRaises(SystemExit):
            _parse_args([
                "--scenario", "C2", "--capability-probe-evidence", "probe.json",
                "--prior-c2-chat-requests", "26",
            ])
        with self.assertRaises(SystemExit):
            _parse_args(["--capability-probe-evidence", "probe.json"])

    def test_capability_probe_evidence_requires_real_normalized_single_tool_result(self):
        import hashlib
        import json
        import tempfile
        from datetime import datetime, timezone
        from pathlib import Path
        from types import SimpleNamespace

        verified_at = datetime(2026, 11, 11, 13, 0, tzinfo=timezone.utc)
        payload = {
            "schema_version": 1,
            "status": "VERIFIED",
            "provider": "deepseek",
            "model_id": "deepseek-flash",
            "method": "DeepSeekClient.chat normalized response",
            "verified_at": verified_at.isoformat(),
            "request_count": 1,
            "automatic_retries": 0,
            "http_status": 200,
            "finish_reason": "tool_calls",
            "tool_call_count": 1,
            "tool_name": "agentforge_capability_probe",
            "arguments": {"status": "ok"},
            "arguments_sha256": _canonical_sha256({"status": "ok"}),
            "request_id": "synthetic-probe-request",
            "tool_executed": False,
            "source": "single explicit synthetic capability probe",
            "evidence_sha256": "a" * 64,
            "usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14},
        }
        project_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(
                dir=project_root, prefix=".r4_2_1_probe_test_") as directory:
            root = Path(directory)
            evidence_path = root / "PHASE5_2_2_3_R4_2_1_CAPABILITY_PROBE_EVIDENCE_20261111_01.json"
            evidence_path.write_text(json.dumps(payload), encoding="utf-8")
            file_hash = hashlib.sha256(evidence_path.read_bytes()).hexdigest()
            result = _tool_capability_preflight(
                SimpleNamespace(provider_id="deepseek", model_id="deepseek-flash",
                                supports_tool_calling=None),
                evidence_path, root=root, now=datetime(2026, 11, 11, 13, 1, tzinfo=timezone.utc),
                expected_file_sha256=file_hash,
            )
            self.assertEqual("PASS", result["status"])
            self.assertEqual("runtime_tool_call_probe", result["verification"])
            self.assertIsNone(result["metadata_supports_tool_calling"])
            self.assertEqual(1, result["request_count"])
            self.assertFalse(result["tool_executed"])

            with self.assertRaisesRegex(ValueError, "file hash"):
                _load_capability_probe_evidence(
                    evidence_path, root=root,
                    now=datetime(2026, 11, 11, 13, 1, tzinfo=timezone.utc),
                    expected_file_sha256="0" * 64,
                )

            for field, value in (("automatic_retries", 1), ("tool_executed", True),
                                 ("finish_reason", "stop"), ("model_id", "deepseek-v4-pro")):
                invalid = dict(payload)
                invalid[field] = value
                evidence_path.write_text(json.dumps(invalid), encoding="utf-8")
                with self.subTest(field=field), self.assertRaises(ValueError):
                    _load_capability_probe_evidence(
                        evidence_path, root=root,
                        now=datetime(2026, 11, 11, 13, 1, tzinfo=timezone.utc),
                    )

            with self.assertRaisesRegex(ValueError, "explicitly reports"):
                _tool_capability_preflight(
                    SimpleNamespace(provider_id="deepseek", model_id="deepseek-flash",
                                    supports_tool_calling=False), evidence_path,
                    root=root, now=datetime(2026, 11, 11, 13, 1, tzinfo=timezone.utc),
                )

    def test_c2_chat_guard_stops_before_sending_over_budget_request(self):
        metrics = {
            "phase": "scenario_C2_execution",
            "max_deepseek_chat_requests": 1,
            "max_scenario_chat_requests": 1,
        }
        calls = []
        def original(_client, messages, **_kwargs):
            calls.append(messages)
            return "ok"

        wrapped = _deepseek_chat_wrapper(original, metrics)
        self.assertEqual("ok", wrapped(object(), [{"role": "user", "content": "synthetic"}]))
        with self.assertRaisesRegex(RuntimeError, "request not sent"):
            wrapped(object(), [{"role": "user", "content": "synthetic"}])
        self.assertEqual(1, len(calls))
        self.assertEqual(1, metrics["deepseek_chat_attempts"])
        self.assertEqual(1, metrics["scenario_chat_attempts"])

    def test_c2_consent_displays_remaining_chat_cap_after_single_probe(self):
        displayed = []
        self.assertTrue(_confirm_synthetic_cloud_requests(
            input_fn=lambda: SYNTHETIC_CLOUD_CONSENT,
            output_fn=lambda message, **_kwargs: displayed.append(message),
            max_chat_requests=C2_CHAT_REQUEST_CAP,
        ))
        self.assertIn("Up to 26 DeepSeek chat requests", displayed[0])
        self.assertNotIn("27 DeepSeek chat requests", displayed[0])

    def test_cloud_request_consent_fails_closed_on_noninteractive_input(self):
        def no_input():
            raise EOFError

        self.assertFalse(_confirm_synthetic_cloud_requests(
            input_fn=no_input, output_fn=lambda *_args, **_kwargs: None,
        ))

    def test_database_password_uses_hidden_interactive_input_and_fails_closed_without_tty(self):
        env = {}
        prompt_messages = []
        password = "synthetic-db-password-value"

        self.assertFalse(_ensure_database_password(
            environ=env, is_tty=False,
            getpass_fn=lambda prompt: self.fail("non-interactive input must not prompt"),
        ))
        self.assertNotIn("AGENTFORGE_DB_PASSWORD", env)

        self.assertTrue(_ensure_database_password(
            environ=env, is_tty=True,
            getpass_fn=lambda prompt: (prompt_messages.append(prompt), password)[1],
        ))
        self.assertEqual(password, env["AGENTFORGE_DB_PASSWORD"])
        self.assertEqual(1, len(prompt_messages))
        self.assertIn("hidden", prompt_messages[0])

        existing = {"AGENTFORGE_DB_PASSWORD": password}
        self.assertTrue(_ensure_database_password(
            environ=existing, is_tty=False,
            getpass_fn=lambda _prompt: self.fail("configured credential must not prompt"),
        ))
        self.assertFalse(_ensure_database_password(
            environ={}, is_tty=True, getpass_fn=lambda _prompt: "",
        ))

    def test_c1_c2_aggregate_requires_both_independent_subscenarios(self):
        self.assertEqual("PASS", _c_scenario_status(
            {"status": "PASS"}, {"status": "PASS"},
        ))
        self.assertEqual("PARTIAL", _c_scenario_status(
            {"status": "PASS"}, {"status": "NOT_MEASURED"},
        ))
        self.assertEqual("FAIL", _c_scenario_status(
            {"status": "PASS"}, {"status": "FAIL"},
        ))

    def test_c2_requires_real_schema_permission_validation_and_observation(self):
        scenario = {
            "repository_id": "synthetic-repository",
            "synthetic_repository_registration_status": "PASS",
            "plan": {"requires_tool_execution": True},
            "execution_status": "completed",
            "agent_execution": {
                "execution_id": "execution-1",
                "status": "completed",
                "tool_results": [
                    {"call_id": "search-call", "task_id": "find", "tool_id": "search",
                     "status": "completed"},
                    {"call_id": "read-call", "task_id": "open", "tool_id": "read",
                     "status": "completed"},
                ],
            },
            "final_answer_tool_observations": [{
                "count": 1, "contains_expected_marker": True,
                "call_ids": ["search-call", "read-call"],
                "observation_ids": ["search-call", "read-call"],
                "verified_evidence_source_ids": ["E1"],
            }],
            "orchestrator_retrieval_search_count": 1,
            "retrieval": {"retrieval_status": "evidence_available"},
            "built_context_evidence": [{"source_id": "E1"}],
            "contract_evidence": {"content_hash_status": "PASS", "items": [{
                "source_id": "E1", "content_hash": "a" * 64,
                "content_hash_linked_to_built_context": True,
                "provenance": {
                    "source_kind": "verified_agent_read",
                    "repository_id": "synthetic-repository",
                    "execution_id": "execution-1",
                    "search_call_id": "search-call",
                    "tool_call_id": "read-call",
                    "observation_id": "read-call",
                    "tool_source_id": "E1",
                    "content_hash": "a" * 64,
                },
            }]},
            "retrieval_to_context_hashes_match": True,
            "answer_consistent_with_synthetic_evidence": True,
            "session_restore_status": "PASS",
        }
        metrics = {
            "agent_tool_search_calls": [{
                "phase": "scenario_C2_execution", "source_ids": ["E1"],
            }],
            "tool_discovery": [{
                "phase": "scenario_C2_execution",
                "permission_scopes": ["repository:read"],
                "tools": [{"tool_id": "read", "permission_scope": "repository:read",
                           "side_effect": False, "requires_approval": False},
                          {"tool_id": "search", "permission_scope": "repository:read",
                           "side_effect": False, "requires_approval": False}],
            }],
            "tool_schema_loads": [{
                "phase": "scenario_C2_execution", "tool_ids": ["read", "search"],
                "status": "PASS", "input_output_schema_loaded": True,
            }],
            "plan_validations": [{
                "phase": "scenario_C2_execution", "status": "PASS",
                "calls": [{"tool_id": "search", "dependency_count": 0,
                           "permission_granted": True},
                          {"tool_id": "read", "dependency_count": 1,
                           "permission_granted": True}],
            }],
        }
        self.assertTrue(all(_c2_execution_checks(scenario, metrics).values()))

        metrics["tool_schema_loads"].clear()
        checks = _c2_execution_checks(scenario, metrics)
        self.assertFalse(checks["registered_schema_loaded"])
        self.assertTrue(checks["agent_read_provenance_chain_valid"])
        scenario["synthetic_repository_registration_status"] = "NOT_MEASURED"
        self.assertFalse(_c2_execution_checks(scenario, metrics)[
            "synthetic_repository_registration_used"]
        )
        scenario["synthetic_repository_registration_status"] = "PASS"
        scenario["contract_evidence"]["items"][0]["provenance"]["search_call_id"] = "forged-call"
        self.assertFalse(_c2_execution_checks(scenario, metrics)["agent_read_provenance_chain_valid"])

    def test_general_empty_context_evidence_count_defaults_to_zero(self):
        self.assertEqual(0, _effective_evidence_count(
            {"retrieval_status": "no_evidence"}, [],
        ))
        self.assertEqual(0, _effective_evidence_count(
            {"retrieval_status": "unavailable"}, None,
        ))
        self.assertEqual(2, _effective_evidence_count(
            {"retrieval_status": "evidence_available", "evidence_count": 2}, None,
        ))

    def test_c1_provider_context_checks_structured_identity_not_prompt_strings(self):
        import hashlib

        context_summary = [{
            "source_id": "E1", "citation": "synthetic_calculation.py:2-4",
            "relative_path": "synthetic_calculation.py", "start_line": 2,
            "end_line": 4, "content_hash": "b" * 64,
            "content_hash_valid": True,
            "excerpt_sha256": hashlib.sha256(b"def f():\n    return 105").hexdigest(),
        }]
        scenario = {
            "built_context_evidence": context_summary,
            "final_answer_request_evidence_markers": {
                "answer_context_evidence_count": 1,
                "answer_context_evidence_sha256": _evidence_identity_sha256(context_summary),
                "contains_synthetic_source_path": False,
                "contains_all_built_context_citations": False,
            },
        }
        self.assertTrue(_c1_structured_context_check(scenario))
        scenario["final_answer_request_evidence_markers"]["answer_context_evidence_sha256"] = "0" * 64
        self.assertFalse(_c1_structured_context_check(scenario))



if __name__ == "__main__":
    unittest.main()
