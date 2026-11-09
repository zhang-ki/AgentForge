import threading
import unittest
from unittest.mock import Mock

from agentforge.conversation_orchestrator import (
    ConversationDependencies, ConversationOrchestrator, ConversationRequest,
    ConversationSession,
)
from agentforge.errors import CitationValidationError
from agentforge.models import Answer
from agentforge.streaming import ConversationCancelled, StreamEventType, StreamingManager


class StreamingManagerTests(unittest.TestCase):
    def test_lifecycle_validation_and_progress_events(self):
        events = []
        manager = StreamingManager(on_event=events.append)
        manager.start()
        with self.assertRaises(RuntimeError):
            manager.start()
        manager.retrieval_progress(1, 2)
        self.assertEqual({"completed": 1, "total": 2}, events[-1].payload)
        for completed, total in ((-1, 2), (2, 1), (True, 1), (0, 0)):
            with self.subTest(completed=completed, total=total), self.assertRaises(ValueError):
                manager.retrieval_progress(completed, total)
        with self.assertRaises(ValueError):
            manager.capture_provider_delta(4)
        with self.assertRaises(ValueError):
            StreamingManager(on_event=object()).start()

    def test_stream_buffer_limit_cancelled_completion_and_safe_error_code(self):
        events = []
        manager = StreamingManager(on_event=events.append, enabled=True)
        manager.start()
        manager.MAX_BUFFER_CHARS = 2
        manager.capture_provider_delta("ab")
        with self.assertRaises(ValueError):
            manager.capture_provider_delta("c")
        manager.fail("bad error code with spaces")
        self.assertEqual({"error_code": "request_failed"}, events[-1].payload)

        cancelled = threading.Event()
        manager = StreamingManager(on_event=events.append, enabled=True, cancel_event=cancelled)
        manager.start()
        cancelled.set()
        with self.assertRaises(ConversationCancelled):
            manager.complete_validated("must not be delivered")
        manager.cancel()
        self.assertEqual(StreamEventType.CANCELLED, events[-1].type)

    def test_provider_deltas_are_not_published_until_answer_is_validated(self):
        events, displayed = [], []
        manager = StreamingManager(on_event=events.append, on_content=displayed.append, enabled=True)
        manager.start()
        manager.plan_ready(type("Plan", (), {
            "mode": type("Mode", (), {"value": "general"})(), "tasks": (1,),
            "planner_source": "explicit",
        })())
        manager.capture_provider_delta("untrusted partial")
        self.assertEqual([], displayed)
        self.assertFalse(any(event.type is StreamEventType.CONTENT_DELTA for event in events))
        manager.complete_validated("validated answer", usage={"completion_tokens": 3, "secret": "omit"})
        self.assertEqual(["validated answer"], displayed)
        self.assertEqual(StreamEventType.FINAL_ANSWER, events[-4].type)
        self.assertEqual({"characters": len("validated answer"), "contract_validated": True},
                         events[-4].payload)
        self.assertEqual(StreamEventType.CONTENT_DELTA, events[-3].type)
        self.assertEqual({"completion_tokens": 3}, events[-2].payload["usage"])
        self.assertEqual(StreamEventType.COMPLETED, events[-1].type)

    def test_failed_or_cancelled_stream_discards_buffer(self):
        events, displayed = [], []
        manager = StreamingManager(on_event=events.append, on_content=displayed.append, enabled=True)
        manager.start()
        manager.capture_provider_delta("partial secret")
        manager.fail("CitationValidationError")
        self.assertEqual([], displayed)
        self.assertEqual(StreamEventType.FAILED, events[-1].type)
        self.assertNotIn("partial secret", repr(events))

        cancellation = threading.Event()
        cancelled_events = []
        cancelled = StreamingManager(on_event=cancelled_events.append, cancel_event=cancellation)
        cancelled.start()
        cancellation.set()
        with self.assertRaises(ConversationCancelled):
            cancelled.check_cancelled()
        cancelled.cancel()
        self.assertEqual(StreamEventType.CANCELLED, cancelled_events[-1].type)

    def test_waiting_approval_suspends_request_without_claiming_completion(self):
        events = []
        manager = StreamingManager(on_event=events.append)
        manager.start()
        manager.suspend("waiting_approval")
        self.assertEqual(StreamEventType.WAITING_APPROVAL, events[-1].type)
        with self.assertRaises(RuntimeError):
            manager.complete_validated("not completed")

    def test_execution_binding_and_agent_event_identity_are_strict(self):
        events = []
        manager = StreamingManager(on_event=events.append)
        for execution_id in ("", "x" * 65, None):
            with self.subTest(execution_id=execution_id), self.assertRaises(ValueError):
                manager.bind_execution(execution_id)
        manager.bind_execution("execution-1")
        with self.assertRaisesRegex(ValueError, "another execution"):
            manager.bind_execution("execution-2")
        manager.start()
        for invalid in (
            None,
            {"type": "NOT_AN_EVENT", "execution_id": "execution-1", "sequence": 1},
            {"type": "OBSERVATION_RECEIVED", "execution_id": "execution-1", "sequence": 0},
            {"type": "OBSERVATION_RECEIVED", "execution_id": "execution-2", "sequence": 1},
        ):
            with self.subTest(event=invalid), self.assertRaises(ValueError):
                manager.publish_agent_event(invalid)

        manager.publish_agent_event({
            "type": "OBSERVATION_RECEIVED", "execution_id": "execution-1",
            "task_id": "task-1", "sequence": 1, "observation_count": 1,
        })
        self.assertEqual(StreamEventType.OBSERVATION_RECEIVED, events[-1].type)
        self.assertEqual("execution-1", events[-1].execution_id)
        self.assertEqual("task-1", events[-1].task_id)
        self.assertEqual(1, events[-1].payload["agent_sequence"])
        with self.assertRaisesRegex(ValueError, "identity or sequence"):
            manager.publish_agent_event({
                "type": "REPLANNING", "execution_id": "execution-1", "sequence": 1,
            })

    def test_stream_terminal_states_and_usage_metadata_are_safe_and_idempotent(self):
        events = []
        manager = StreamingManager(on_event=events.append, enabled=True)
        manager.start()
        with self.assertRaisesRegex(ValueError, "Validated answer must be text"):
            manager.complete_validated(None)
        with self.assertRaisesRegex(ValueError, "checkpoint"):
            manager.suspend("executing")
        manager.complete_validated("valid", usage={
            "completion_tokens": 2, "reasoning_tokens": 1, "api_key": "synthetic-secret",
            "total_tokens": -1, "prompt_tokens": True,
        })
        self.assertEqual({"completion_tokens": 2, "reasoning_tokens": 1},
                         events[-2].payload["usage"])
        completed_count = sum(event.type is StreamEventType.COMPLETED for event in events)
        manager.fail("late_failure")
        manager.cancel()
        self.assertEqual(completed_count,
                         sum(event.type is StreamEventType.COMPLETED for event in events))
        self.assertFalse(any("synthetic-secret" in repr(event.payload) for event in events))


class StreamingOrchestratorTests(unittest.TestCase):
    class Provider:
        provider_id = "test"

        def __init__(self, answer):
            self.answer = answer

        def answer_general(self, context, *, on_content=None, **kwargs):
            if on_content:
                on_content("unvalidated partial response")
            return self.answer

        def request_stats(self):
            return {"last_usage": {"completion_tokens": 2}}

    def _orchestrator(self, answer):
        provider = self.Provider(answer)
        dependencies = ConversationDependencies(
            provider_factory=lambda *args, **kwargs: provider,
            model_budget_resolver=lambda *args, **kwargs: {
                "input_budget": 1000, "output_reserve": 256,
                "safety_margin": 128, "context_window": 4096,
                "estimate_kind": "estimated",
            },
        )
        return ConversationOrchestrator(dependencies)

    def test_contract_failure_does_not_publish_raw_model_deltas(self):
        displayed, events = [], []
        orchestrator = self._orchestrator(Answer("Unsupported [E1].", (), False))
        with self.assertRaises(CitationValidationError):
            orchestrator.run(
                ConversationRequest("Explain attention.", "", "test", mode="general", stream=True),
                session=ConversationSession(), on_content=displayed.append, on_event=events.append,
            )
        self.assertEqual([], displayed)
        self.assertEqual(StreamEventType.FAILED, events[-1].type)

    def test_only_validated_answer_is_forwarded_to_legacy_content_callback(self):
        displayed, events = [], []
        orchestrator = self._orchestrator(Answer("Attention mixes token representations.", (), False))
        result = orchestrator.run(
            ConversationRequest("Explain attention.", "", "test", mode="general", stream=True),
            session=ConversationSession(), on_content=displayed.append, on_event=events.append,
        )
        self.assertEqual([result.answer.text], displayed)
        self.assertEqual(StreamEventType.COMPLETED, events[-1].type)
        self.assertNotIn("unvalidated partial response", repr(events))

    def test_cancelled_before_planning_never_calls_provider(self):
        events = []
        cancelled = threading.Event()
        cancelled.set()
        provider = self.Provider(Answer("unused", (), False))
        factory = Mock(return_value=provider)
        orchestrator = ConversationOrchestrator(ConversationDependencies(
            provider_factory=factory,
            model_budget_resolver=lambda *args, **kwargs: {},
        ))
        with self.assertRaises(ConversationCancelled):
            orchestrator.run(
                ConversationRequest("Question", "", "test", mode="general", stream=True),
                cancel_event=cancelled, on_event=events.append,
            )
        factory.assert_not_called()
        self.assertEqual(StreamEventType.CANCELLED, events[-1].type)


if __name__ == "__main__":
    unittest.main()
