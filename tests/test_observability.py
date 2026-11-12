import io
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from agentforge.embeddings.base import embed_text
from agentforge.observability import (
    RequestLifecycleTracker, bind_lifecycle, current_stage, finish_provider_stage,
    lifecycle_stage,
)
from agentforge.streaming import ConversationCancelled, StreamEventType, StreamingEvent


class _FlushStream(io.StringIO):
    def __init__(self):
        super().__init__()
        self.flush_count = 0

    def flush(self):
        self.flush_count += 1
        super().flush()


class _CountingEmbedding:
    model_id = "synthetic-embedding-v1"
    dimension = 2

    def __init__(self):
        self.request_counts = {"attempts": 0, "sent": 0, "successes": 0, "failures": 0}

    def embed(self, _text):
        self.request_counts["attempts"] += 1
        self.request_counts["sent"] += 1
        self.request_counts["successes"] += 1
        return (3.0, 4.0)


class RequestLifecycleTests(unittest.TestCase):
    def setUp(self):
        scratch = Path.cwd() / ".agentforge" / "test-observability"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=str(scratch))
        self.log_path = Path(self.temp.name) / "request.jsonl"
        self.stream = _FlushStream()
        self.tracker = RequestLifecycleTracker(
            console_stream=self.stream, log_path=self.log_path,
            wall_clock=lambda: datetime(2026, 11, 10, tzinfo=timezone.utc),
        )
        self.tracker.start_request(provider="deepseek", model="deepseek-flash",
                                   session_id="session-secret-id", streaming_enabled=False)

    def tearDown(self):
        self.temp.cleanup()

    def records(self):
        return [json.loads(line) for line in self.log_path.read_text(encoding="utf-8").splitlines()]

    def test_request_ids_are_unique(self):
        first = RequestLifecycleTracker()
        second = RequestLifecycleTracker()
        self.assertNotEqual(first.request_id, second.request_id)
        self.assertEqual(12, len(first.request_id))

    def test_request_and_stage_events_are_ordered_and_have_elapsed_times(self):
        with self.tracker.stage("planner_validation", metadata={"corrections": 1}):
            pass
        self.tracker.finish()
        records = self.records()
        self.assertEqual("request", records[0]["stage"])
        self.assertEqual("STARTED", records[0]["event_type"])
        self.assertEqual(["STARTED", "COMPLETED"], [
            row["event_type"] for row in records if row["stage"] == "planner_validation"
        ])
        self.assertEqual("SUMMARY", records[-1]["event_type"])
        self.assertTrue(all(row["elapsed_ms"] >= 0 for row in records))
        completed = next(row for row in records if row["stage"] == "planner_validation"
                          and row["event_type"] == "COMPLETED")
        self.assertGreaterEqual(completed["stage_elapsed_ms"], 0)

    def test_stage_exception_is_recorded_and_original_exception_propagates(self):
        with self.assertRaisesRegex(ValueError, "synthetic failure"):
            with self.tracker.stage("repository_retrieval"):
                raise ValueError("synthetic failure")
        self.tracker.finish("FAILED", error_code="retrieval_failed")
        failed = next(row for row in self.records()
                      if row["stage"] == "repository_retrieval" and row["event_type"] == "FAILED")
        self.assertEqual("ValueError", failed["metadata"]["error_type"])
        self.assertNotIn("synthetic failure", self.log_path.read_text(encoding="utf-8"))

    def test_timeout_is_distinct_from_failure(self):
        with self.assertRaises(TimeoutError):
            with self.tracker.stage("provider_request"):
                raise TimeoutError("synthetic timeout")
        self.tracker.finish("TIMEOUT", error_code="timeout")
        row = next(row for row in self.records()
                   if row["stage"] == "provider_request" and row["event_type"] == "TIMEOUT")
        self.assertEqual("TIMEOUT", row["status"])

    def test_existing_cancellation_is_recorded_without_becoming_failure(self):
        with self.assertRaises(ConversationCancelled):
            with self.tracker.stage("agent_execution"):
                raise ConversationCancelled("cancelled")
        self.tracker.finish("CANCELLED")
        row = next(row for row in self.records()
                   if row["stage"] == "agent_execution" and row["event_type"] == "CANCELLED")
        self.assertEqual("CANCELLED", row["status"])

    def test_request_cancellation_closes_an_in_flight_tool_as_cancelled(self):
        self.tracker.record_stream_event(StreamingEvent(StreamEventType.TOOL_CALL_STARTED, {
            "call_id": "synthetic-call-1", "tool_id": "repo.read",
        }))
        self.tracker.finish("CANCELLED")
        row = next(row for row in self.records() if row["stage"] == "tool_execution"
                   and row["event_type"] == "CANCELLED")
        self.assertEqual("request_cancelled", row["error_code"])
        self.assertEqual("CANCELLED", self.records()[-1]["status"])

    def test_approval_and_validated_answer_keep_their_stream_event_types(self):
        self.tracker.record_stream_event(StreamingEvent(StreamEventType.WAITING_APPROVAL, {}))
        self.tracker.finish("PENDING")
        approval_rows = self.records()
        self.assertTrue(any(row["stage"] == "approval"
                            and row["event_type"] == "WAITING_APPROVAL"
                            and row["status"] == "WAITING_APPROVAL" for row in approval_rows))

        answer_log = self.log_path.with_name("answer.jsonl")
        answer_tracker = RequestLifecycleTracker(log_path=answer_log)
        answer_tracker.start_request()
        answer_tracker.record_stream_event(StreamingEvent(StreamEventType.FINAL_ANSWER, {
            "contract_validated": True,
        }))
        answer_tracker.finish("COMPLETED")
        answer_rows = [json.loads(line) for line in answer_log.read_text(encoding="utf-8").splitlines()]
        self.assertTrue(any(row["stage"] == "streaming_completion"
                            and row["event_type"] == "FINAL_ANSWER"
                            and row["status"] == "COMPLETED" for row in answer_rows))

    def test_skipped_stage_does_not_claim_it_started(self):
        self.tracker.skip("repository_retrieval", "planner_did_not_request_repository")
        self.tracker.finish()
        rows = [row for row in self.records() if row["stage"] == "repository_retrieval"]
        self.assertEqual(["SKIPPED"], [row["event_type"] for row in rows])

    def test_degraded_stage_has_reason_and_stays_distinct(self):
        self.tracker.degrade("vector_retrieval", "embedding_service_unavailable",
                             metadata={"error_type": "ProviderError"})
        self.tracker.finish()
        row = next(row for row in self.records() if row["stage"] == "vector_retrieval")
        self.assertEqual("DEGRADED", row["status"])
        self.assertEqual("embedding_service_unavailable", row["error_code"])

    def test_summary_counts_completed_skipped_degraded_and_failed_stages(self):
        with self.tracker.stage("planner"):
            pass
        self.tracker.skip("repository_retrieval", "not_requested")
        self.tracker.degrade("vector_retrieval", "embedding_unavailable")
        with self.assertRaisesRegex(ValueError, "synthetic stage error"):
            with self.tracker.stage("final_answer"):
                raise ValueError("synthetic stage error")

        self.tracker.finish("FAILED")
        summary = self.records()[-1]["metadata"]
        self.assertEqual(1, summary["completed_count"])
        self.assertEqual(1, summary["skipped_count"])
        self.assertEqual(1, summary["degraded_count"])
        self.assertEqual(1, summary["failed_count"])
        self.assertEqual(4, summary["stage_count"])
        self.assertEqual([
            "planner:COMPLETED",
            "repository_retrieval:SKIPPED",
            "vector_retrieval:DEGRADED",
            "final_answer:FAILED",
        ], summary["stage_outcomes"])
        self.assertFalse(summary["stage_outcomes_truncated"])

    def test_summary_bounds_stage_outcomes_and_marks_truncation(self):
        for index in range(130):
            self.tracker.skip(f"stage_{index}", "not_requested")
        self.tracker.finish()
        summary = self.records()[-1]["metadata"]
        self.assertEqual(130, summary["skipped_count"])
        self.assertEqual(130, summary["stage_count"])
        self.assertEqual(128, len(summary["stage_outcomes"]))
        self.assertTrue(summary["stage_outcomes_truncated"])
        self.assertEqual("stage_129:SKIPPED", summary["stage_outcomes"][-1])

    def test_planner_and_final_answer_provider_calls_have_distinct_kinds_and_usage(self):
        stats = {"attempted": 1, "sent": 1, "succeeded": 1, "failed": 0,
                 "last_http_status": 200, "last_finish_reason": "stop",
                 "last_elapsed_seconds": 0.25, "last_first_response_ms": 100,
                 "last_usage": {"prompt_tokens": 20, "completion_tokens": 5,
                                "total_tokens": 25}}
        for parent, expected in (("planner_provider_request", "planner"),
                                 ("final_answer", "final_answer")):
            before = {"attempted": 0, "sent": 0, "succeeded": 0, "failed": 0}
            with self.subTest(parent=parent), bind_lifecycle(self.tracker):
                with lifecycle_stage(parent) as parent_span:
                    with lifecycle_stage("provider_request") as provider_span:
                        finish_provider_stage(provider_span, provider="deepseek", model="deepseek-flash",
                                              before=before, after=stats, parent_stage=parent)
                    parent_span.finish("COMPLETED")
        self.tracker.finish()
        rows = [row for row in self.records() if row["stage"] == "provider_request"
                and row["event_type"] == "COMPLETED"]
        self.assertEqual(["planner", "final_answer"], [
            row["metadata"]["provider_request_kind"] for row in rows
        ])
        self.assertEqual(20, rows[0]["metadata"]["prompt_tokens"])
        self.assertEqual(200, rows[0]["metadata"]["http_status"])

    def test_contract_failure_is_recorded_as_the_failed_request_stage(self):
        with self.tracker.stage("answer_contract") as contract:
            contract.finish("FAILED", error_code="citation_contract_failed",
                            metadata={"answer_contract_status": "rejected"})
        self.tracker.finish("FAILED", error_code="citation_contract_failed")
        self.assertEqual("answer_contract", self.records()[-1]["metadata"]["failure_stage"])

    def test_console_progress_flushes_before_stage_returns(self):
        initial = self.stream.flush_count
        with self.tracker.stage("planner"):
            self.assertGreater(self.stream.flush_count, initial)
            self.assertIn("[Planner] Planning...", self.stream.getvalue())
        self.assertGreater(self.stream.flush_count, initial + 1)

    def test_presentation_keeps_progress_on_stderr_and_answer_on_stdout(self):
        from agentforge.presentation import main

        with patch("agentforge.presentation.default_log_path", return_value=self.log_path):
            def fake_cli(_arguments):
                with lifecycle_stage("planner"):
                    pass
                print(json.dumps({
                    "provider": "deepseek",
                    "answer": "Synthetic answer",
                    "answer_contract": {"status": "answered", "claims": [], "evidence": []},
                }))
                return 0

            stdout, stderr = io.StringIO(), io.StringIO()
            with patch("agentforge.presentation.cli.main", side_effect=fake_cli):
                with patch("sys.stdout", stdout), patch("sys.stderr", stderr):
                    self.assertEqual(0, main(["ask", "synthetic question", "--provider", "deepseek",
                                              "--model", "deepseek-flash", "--mode", "auto",
                                              "--debug"]))
        self.assertIn("Synthetic answer", stdout.getvalue())
        self.assertIn("[Planner] Planning...", stderr.getvalue())
        self.assertNotIn("[Planner]", stdout.getvalue())
        self.assertNotIn("synthetic question", stdout.getvalue() + stderr.getvalue())
        self.assertNotIn("synthetic question", self.log_path.read_text(encoding="utf-8"))

    def test_stream_callback_content_is_not_mixed_with_progress(self):
        from agentforge.presentation import main

        with patch("agentforge.presentation.default_log_path", return_value=self.log_path):
            def fake_cli(_arguments, stream_callback=None):
                with lifecycle_stage("final_answer"):
                    if stream_callback:
                        # Streaming drafts must match the final answer that
                        # receives contract validation; mismatched text is a
                        # protocol failure, not a second answer to print.
                        stream_callback("validated synthetic answer")
                print(json.dumps({
                    "provider": "deepseek", "answer": "validated synthetic answer",
                    "answer_contract": {"status": "answered", "claims": [], "evidence": []},
                }))
                return 0

            stdout, stderr = io.StringIO(), io.StringIO()
            with patch("agentforge.presentation.cli.main", side_effect=fake_cli):
                with patch("sys.stdout", stdout), patch("sys.stderr", stderr):
                    self.assertEqual(0, main(["ask", "synthetic", "--stream", "--debug"]))
        self.assertIn("DeepSeek 草稿（尚未通过引用验证）", stdout.getvalue())
        self.assertIn("validated synthetic answer", stdout.getvalue())
        self.assertEqual(1, stdout.getvalue().count("validated synthetic answer"))
        self.assertIn("回答已通过合同校验并保存", stdout.getvalue())
        self.assertIn("Generating answer", stderr.getvalue())
        self.assertNotIn("Generating answer", stdout.getvalue())

    def test_jsonl_events_are_parseable_and_include_required_fields(self):
        with self.tracker.stage("context_build"):
            pass
        self.tracker.finish()
        rows = self.records()
        self.assertTrue(rows)
        for row in rows:
            self.assertTrue({"request_id", "stage", "event_type", "timestamp",
                             "elapsed_ms", "status", "error_code", "metadata"} <= row.keys())
            self.assertEqual(self.tracker.request_id, row["request_id"])

    def test_jsonl_rotation_keeps_a_bounded_number_of_parseable_backups(self):
        self.tracker.MAX_LOG_BYTES = 1_500
        for index in range(24):
            self.tracker.observe("planner_validation", metadata={"corrections": index})
        self.tracker.finish()
        paths = sorted(self.log_path.parent.glob("request.jsonl*"))
        self.assertGreaterEqual(len(paths), 2)
        self.assertLessEqual(len(paths), self.tracker.LOG_BACKUPS + 1)
        for path in paths:
            for line in path.read_text(encoding="utf-8").splitlines():
                record = json.loads(line)
                self.assertEqual(self.tracker.request_id, record["request_id"])

    def test_logs_omit_prompt_source_credentials_and_raw_tool_call_ids(self):
        secret_prompt = "question-private-942"
        with bind_lifecycle(self.tracker):
            self.tracker.observe("test_stage", metadata={
                "operation": secret_prompt,
                "api_key": "sk-secret-credential-value",
                "source": "src/private.py",
            })
            event = StreamingEvent(StreamEventType.TOOL_CALL_STARTED, {
                "call_id": "private-call-id-42", "tool_id": "repo.read",
                "arguments": {"path": "private-source.py", "token": "private-token"},
            })
            self.tracker.record_stream_event(event)
            self.tracker.record_stream_event(StreamingEvent(StreamEventType.TOOL_CALL_COMPLETED, {
                "call_id": "private-call-id-42", "tool_id": "repo.read",
            }))
        self.tracker.finish()
        text = self.log_path.read_text(encoding="utf-8")
        for secret in (secret_prompt, "sk-secret-credential-value", "private-call-id-42",
                       "private-source.py", "private-token"):
            self.assertNotIn(secret, text)
        tool = next(row for row in self.records() if row["stage"] == "tool_execution"
                    and row["event_type"] == "STARTED")
        self.assertEqual(16, len(tool["correlation_id_hash"]))

    def test_log_write_failure_does_not_break_stage_or_request(self):
        original_open = Path.open

        def denied(path, *args, **kwargs):
            if path == self.log_path:
                raise PermissionError("synthetic log path denial")
            return original_open(path, *args, **kwargs)

        with patch.object(Path, "open", denied):
            with self.tracker.stage("final_answer"):
                self.assertTrue(True)
            self.tracker.finish()
        self.assertIn("Request completed", self.stream.getvalue())

    def test_embedding_request_counts_use_an_immutable_before_snapshot(self):
        provider = _CountingEmbedding()
        before = dict(provider.request_counts)
        vector = embed_text(provider, "synthetic content", role="document")
        self.assertEqual((0.6, 0.8), vector)
        self.assertEqual({"attempts": 0, "sent": 0, "successes": 0, "failures": 0}, before)
        # A request-local tracker verifies the counter deltas rather than merely
        # checking that embedding normalization returned a vector.
        other_log = self.log_path.with_name("embedding.jsonl")
        tracker = RequestLifecycleTracker(log_path=other_log)
        tracker.start_request()
        with bind_lifecycle(tracker):
            embed_text(provider, "synthetic content", role="query")
        tracker.finish()
        row = next(json.loads(line) for line in other_log.read_text(encoding="utf-8").splitlines()
                   if json.loads(line)["stage"] == "embedding_request"
                   and json.loads(line)["event_type"] == "COMPLETED")
        self.assertEqual(1, row["metadata"]["embedding_attempts"])
        self.assertEqual(1, row["metadata"]["embedding_sent"])


if __name__ == "__main__":
    unittest.main()
