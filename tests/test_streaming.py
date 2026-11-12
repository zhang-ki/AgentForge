import threading
import unittest
from unittest.mock import Mock

from agentforge.conversation_orchestrator import (
    ConversationDependencies, ConversationOrchestrator, ConversationRequest,
    ConversationSession,
)
from agentforge.errors import CitationValidationError
from agentforge.models import Answer
from agentforge.streaming import (
    ConversationCancelled, StreamEventType, StreamingManager, StreamingProtocolError,
)


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

    def test_provider_deltas_are_published_as_unverified_drafts_before_validation(self):
        events, displayed = [], []
        manager = StreamingManager(on_event=events.append, on_content=displayed.append, enabled=True)
        manager.start()
        manager.plan_ready(type("Plan", (), {
            "mode": type("Mode", (), {"value": "general"})(), "tasks": (1,),
            "planner_source": "explicit",
        })())
        manager.capture_provider_delta("validated ")
        self.assertEqual(["validated "], displayed)
        self.assertEqual(StreamEventType.CONTENT_DELTA, events[-1].type)
        self.assertEqual({"characters": len("validated "), "draft": True,
                          "contract_validated": False},
                         events[-1].payload)
        manager.capture_provider_delta("answer")
        self.assertEqual(["validated ", "answer"], displayed)
        manager.validate_provider_answer("validated answer")
        manager.complete_validated("validated answer", usage={"completion_tokens": 3, "secret": "omit"})
        self.assertEqual("validated answer", "".join(displayed))
        self.assertEqual(2, sum(event.type is StreamEventType.CONTENT_DELTA for event in events))
        self.assertEqual(StreamEventType.FINAL_ANSWER, events[-3].type)
        self.assertEqual({"characters": len("validated answer"), "contract_validated": True},
                         events[-3].payload)
        self.assertEqual({"completion_tokens": 3}, events[-2].payload["usage"])
        self.assertEqual(StreamEventType.COMPLETED, events[-1].type)

    def test_empty_provider_delta_does_not_break_final_stream_validation(self):
        displayed = []
        manager = StreamingManager(on_content=displayed.append, enabled=True)
        manager.start()
        manager.capture_provider_delta("")
        manager.capture_provider_delta("answer")
        manager.complete_validated("answer")
        self.assertEqual(["answer"], displayed)

    def test_missing_native_deltas_does_not_simulate_streaming_after_completion(self):
        events, displayed = [], []
        manager = StreamingManager(on_event=events.append, on_content=displayed.append, enabled=True)
        manager.start()
        manager.configure_provider_streaming(True)
        manager.complete_validated("Complete buffered answer.")
        self.assertEqual([], displayed)
        self.assertFalse(any(event.type is StreamEventType.CONTENT_DELTA for event in events))
        self.assertEqual(StreamEventType.COMPLETED, events[-1].type)

    def test_no_evidence_stream_is_buffered_until_sanitized_answer_is_contract_validated(self):
        events, displayed = [], []
        manager = StreamingManager(on_event=events.append, on_content=displayed.append, enabled=True)
        manager.start()
        manager.configure_provider_streaming(True)
        manager.defer_content_until_validated()
        provider_draft = "A general answer. [E1]"
        manager.capture_provider_delta(provider_draft)
        self.assertEqual([], displayed)
        self.assertFalse(any(event.type is StreamEventType.CONTENT_DELTA for event in events))

        manager.complete_validated(
            "A general answer.", provider_answer=provider_draft,
        )
        self.assertEqual("buffered_for_contract", manager.status)
        self.assertEqual([], displayed)
        self.assertEqual(StreamEventType.FINAL_ANSWER, events[-2].type)
        self.assertTrue(events[-2].payload["contract_validated"])
        self.assertEqual(StreamEventType.COMPLETED, events[-1].type)

    def test_failed_or_cancelled_stream_marks_already_published_draft_unfinished(self):
        events, displayed = [], []
        manager = StreamingManager(on_event=events.append, on_content=displayed.append, enabled=True)
        manager.start()
        manager.capture_provider_delta("partial secret")
        manager.fail("CitationValidationError")
        self.assertEqual(["partial secret"], displayed)
        self.assertEqual(StreamEventType.FAILED, events[-1].type)
        self.assertNotIn("partial secret", repr(events))
        self.assertFalse(any(event.type is StreamEventType.FINAL_ANSWER for event in events))

        events = []
        manager = StreamingManager(on_event=events.append, enabled=True)
        manager.start()
        manager.plan_ready(type("Plan", (), {
            "mode": type("Mode", (), {"value": "general"})(), "tasks": (),
            "planner_source": "explicit",
        })())
        manager.capture_provider_delta("draft")
        with self.assertRaises(StreamingProtocolError):
            manager.validate_provider_answer("different completion")

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
                midpoint = max(1, len(self.answer.text) // 2)
                on_content(self.answer.text[:midpoint])
                on_content(self.answer.text[midpoint:])
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

    def test_contract_failure_keeps_streamed_text_labeled_as_unverified_draft(self):
        displayed, events = [], []
        orchestrator = self._orchestrator(
            Answer("Unsupported src/private.py:1-2 [E1].", (), False),
        )
        with self.assertRaises(CitationValidationError):
            orchestrator.run(
                ConversationRequest("Explain attention.", "", "test", mode="general", stream=True),
                session=ConversationSession(), on_content=displayed.append, on_event=events.append,
            )
        self.assertEqual([], displayed, "no-evidence drafts remain buffered when the contract rejects")
        self.assertEqual(StreamEventType.FAILED, events[-1].type)
        self.assertFalse(any(event.type is StreamEventType.FINAL_ANSWER for event in events))

    def test_streamed_answer_matches_saved_and_validated_answer_without_duplicate_final_output(self):
        displayed, events = [], []
        orchestrator = self._orchestrator(Answer("Attention mixes token representations.", (), False))
        result = orchestrator.run(
            ConversationRequest("Explain attention.", "", "test", mode="general", stream=True),
            session=ConversationSession(), on_content=displayed.append, on_event=events.append,
        )
        self.assertEqual([], displayed, "General answer output waits for contract validation")
        self.assertEqual("buffered_for_contract", result.retrieval["streaming_status"])
        self.assertFalse(any(event.type is StreamEventType.CONTENT_DELTA for event in events))
        final_events = [event for event in events if event.type is StreamEventType.FINAL_ANSWER]
        self.assertEqual(1, len(final_events))
        self.assertEqual(len(result.answer.text), final_events[0].payload["characters"])
        self.assertEqual(StreamEventType.COMPLETED, events[-1].type)
        self.assertFalse(any(event.type is StreamEventType.CONTENT_DELTA
                             and event.payload.get("contract_validated") is True
                             for event in events))

    def test_interrupted_deepseek_stream_uses_one_buffered_recovery_and_keeps_complete_answer(self):
        from types import SimpleNamespace

        from agentforge.dialogue_router import DialogueMode, DialoguePlan
        from agentforge.errors import ProviderError

        class InterruptedProvider:
            provider_id = "deepseek"
            model = "deepseek-flash"
            stream = True
            descriptor = SimpleNamespace(supports_streaming=True)

            def __init__(self):
                self.calls = []

            def answer_general(self, context, *, on_content=None, stream=None, **_kwargs):
                self.calls.append(stream)
                if stream is not False:
                    on_content("partial draft")
                    raise ProviderError("stream disconnected")
                self.assert_no_stream_callback(on_content)
                return Answer("Complete buffered answer.", (), False)

            @staticmethod
            def assert_no_stream_callback(callback):
                if callback is not None:
                    raise AssertionError("buffered recovery must not publish as a stream")

            def request_stats(self):
                return {"last_error_code": "incomplete_stream",
                        "last_failure_phase": "response_body",
                        "last_usage": {"completion_tokens": 5}}

        provider = InterruptedProvider()
        orchestrator = ConversationOrchestrator(ConversationDependencies(
            provider_factory=Mock(),
            model_budget_resolver=lambda *args, **kwargs: {
                "input_budget": 1000, "output_reserve": 256,
                "safety_margin": 128, "context_window": 4096,
                "estimate_kind": "estimated",
            },
        ))
        plan = DialoguePlan(
            mode=DialogueMode.GENERAL, use_repository=False,
            use_general_knowledge=True, reason="synthetic general answer",
            planner_source="explicit",
        )
        displayed, events = [], []
        result = orchestrator.run(
            ConversationRequest("Explain TCP and UDP.", "", "deepseek",
                                model_id="deepseek-flash", mode="auto", stream=True),
            session=ConversationSession(provider=provider), planned_plan=plan,
            on_content=displayed.append, on_event=events.append,
        )

        self.assertEqual([None, False], provider.calls)
        self.assertEqual("Complete buffered answer.", result.answer.text)
        self.assertEqual("degraded_to_buffered", result.retrieval["streaming_status"])
        self.assertEqual([], displayed, "an interrupted no-evidence draft is never exposed")
        self.assertFalse(any(event.type is StreamEventType.CONTENT_DELTA for event in events))
        fallback = [event for event in events if event.type is StreamEventType.STATE_CHANGED]
        self.assertEqual("degraded_to_buffered", fallback[-1].payload["streaming_status"])
        self.assertEqual(StreamEventType.COMPLETED, events[-1].type)

    def test_unknown_native_streaming_capability_keeps_deepseek_buffered_answer_available(self):
        from types import SimpleNamespace

        from agentforge.dialogue_router import DialogueMode, DialoguePlan

        class BufferedProvider:
            provider_id = "deepseek"
            descriptor = SimpleNamespace(supports_streaming=None)
            stream = False

            def __init__(self):
                self.callback_values = []

            def answer_general(self, context, *, on_content=None, **_kwargs):
                self.callback_values.append(on_content)
                return Answer("Complete non-streaming answer.", (), False)

            def request_stats(self):
                return {"last_usage": {"completion_tokens": 4}}

        provider = BufferedProvider()
        orchestrator = ConversationOrchestrator(ConversationDependencies(
            provider_factory=Mock(),
            model_budget_resolver=lambda *args, **kwargs: {
                "input_budget": 1000, "output_reserve": 256,
                "safety_margin": 128, "context_window": 4096,
                "estimate_kind": "estimated",
            },
        ))
        plan = DialoguePlan(
            mode=DialogueMode.GENERAL, use_repository=False,
            use_general_knowledge=True, reason="synthetic general answer",
            planner_source="explicit",
        )
        displayed, events = [], []
        result = orchestrator.run(
            ConversationRequest("Explain TCP and UDP.", "", "deepseek",
                                model_id="deepseek-flash", mode="auto", stream=True),
            session=ConversationSession(provider=provider), planned_plan=plan,
            on_content=displayed.append, on_event=events.append,
        )

        self.assertEqual("Complete non-streaming answer.", result.answer.text)
        self.assertEqual([None], provider.callback_values)
        self.assertEqual([], displayed)
        self.assertEqual("not_measured", result.retrieval["streaming_status"])
        self.assertFalse(any(event.type is StreamEventType.CONTENT_DELTA for event in events))
        self.assertEqual(StreamEventType.COMPLETED, events[-1].type)

    def test_streaming_off_keeps_buffered_final_answer_path(self):
        displayed, events = [], []
        orchestrator = self._orchestrator(Answer("Buffered answer.", (), False))
        result = orchestrator.run(
            ConversationRequest("Explain attention.", "", "test", mode="general", stream=False),
            session=ConversationSession(), on_content=displayed.append, on_event=events.append,
        )
        self.assertEqual("Buffered answer.", result.answer.text)
        self.assertEqual([], displayed)
        self.assertEqual(StreamEventType.COMPLETED, events[-1].type)

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


class NoEvidenceAnswerGuaranteeTests(unittest.TestCase):
    class Provider:
        provider_id = "deepseek"
        model = "deepseek-flash"

        def __init__(self):
            self.calls = []

        def answer_general(self, context, *, external_data_unavailable=False,
                           repository_evidence_unavailable=False, **_kwargs):
            self.calls.append({
                "context": context,
                "external_data_unavailable": external_data_unavailable,
                "repository_evidence_unavailable": repository_evidence_unavailable,
            })
            answer = Answer("TCP and UDP are transport protocols with different delivery guarantees.", (), False)
            on_content = _kwargs.get("on_content")
            if callable(on_content):
                midpoint = len(answer.text) // 2
                on_content(answer.text[:midpoint])
                on_content(answer.text[midpoint:])
            return answer

        def request_stats(self):
            return {"last_usage": {"prompt_tokens": 10, "completion_tokens": 8}}

    @staticmethod
    def _budget(*_args, **_kwargs):
        return {"input_budget": 4096, "output_reserve": 512,
                "safety_margin": 128, "context_window": 8192,
                "estimate_kind": "estimated"}

    def test_bound_repository_without_evidence_still_generates_and_persists_when_planner_requests_clarification(self):
        from types import SimpleNamespace

        from agentforge.dialogue_router import DialogueMode, DialoguePlan
        from agentforge.product_retrieval import (
            PRODUCT_ROUTE_VERSION, ProductIntentRouter, ProductRetrievalOutcome,
        )

        query = "What is the difference between TCP and UDP?"
        provider = self.Provider()
        decision = ProductIntentRouter().route(query)
        outcome = ProductRetrievalOutcome(
            query, PRODUCT_ROUTE_VERSION, decision, (), (), (), None,
        )
        retriever = SimpleNamespace(search=Mock(return_value=outcome))
        session_service = Mock()
        session_service.restore_context.return_value = SimpleNamespace(
            history=(), rows=(), summary=None, covered_through=None, fact_ledger=(),
        )
        agent_executor = Mock()
        plan = DialoguePlan(
            mode=DialogueMode.REPOSITORY, use_repository=True,
            use_general_knowledge=False, reason="synthetic no-evidence plan",
            requires_agent_execution=True, clarification_required=True,
            planner_source="llm",
        )
        dependencies = ConversationDependencies(
            provider_factory=Mock(), model_budget_resolver=self._budget,
            retriever_factory=Mock(return_value=retriever),
            candidate_selector=lambda *_args, **_kwargs: (),
            retrieval_refiner=lambda item, *_args, **_kwargs: (item, None),
            session_service=session_service, agent_executor=agent_executor,
            repository_preparer=lambda *_args: {"scan_report": None, "vector_retriever": None},
        )
        orchestrator = ConversationOrchestrator(dependencies)
        session = ConversationSession(
            provider=provider, store=object(), repository_id="synthetic-repository",
            conversation={"repository_id": "synthetic-repository"},
        )
        displayed, events = [], []

        result = orchestrator.run(
            ConversationRequest(
                query, r"D:\synthetic\repo", "deepseek", model_id="deepseek-flash",
                conversation_id="synthetic-session", mode="auto", stream=True,
            ),
            index_store=object(), session=session, planned_plan=plan,
            on_content=displayed.append, on_event=events.append,
        )

        retriever.search.assert_called_once_with(query, top_k=5)
        self.assertEqual(1, len(provider.calls))
        self.assertEqual((), provider.calls[0]["context"].evidence)
        self.assertTrue(provider.calls[0]["repository_evidence_unavailable"])
        self.assertEqual("limited", result.contract.status)
        self.assertEqual("TCP and UDP are transport protocols with different delivery guarantees.",
                         result.answer.text)
        self.assertEqual((), result.contract.evidence)
        session_service.commit_completed_turn.assert_called_once()
        self.assertEqual(result.answer, session_service.commit_completed_turn.call_args.args[4])
        agent_executor.execute.assert_not_called()
        self.assertEqual([], displayed, "no-evidence drafts stay buffered until contract validation")
        self.assertFalse(any(event.type is StreamEventType.CONTENT_DELTA for event in events))
        self.assertEqual("buffered_for_contract", result.retrieval["streaming_status"])
        self.assertEqual({"completion_tokens": 8, "prompt_tokens": 10},
                         events[-2].payload["usage"])
        self.assertEqual(StreamEventType.COMPLETED, events[-1].type)

    def test_unknown_source_marker_is_removed_before_answer_persistence_and_display(self):
        from types import SimpleNamespace

        from agentforge.dialogue_router import DialogueMode, DialoguePlan

        class CitationProvider(self.Provider):
            def answer_general(self, context, **kwargs):
                self.calls.append({"context": context})
                answer = Answer("A greeting is a casual way to start a conversation. [E1]", (), False)
                callback = kwargs.get("on_content")
                if callable(callback):
                    callback(answer.text)
                return answer

        provider = CitationProvider()
        store = object()
        session_service = Mock()
        session_service.restore_context.return_value = SimpleNamespace(
            history=(), rows=(), summary=None, covered_through=None, fact_ledger=(),
        )
        orchestrator = ConversationOrchestrator(ConversationDependencies(
            provider_factory=Mock(), model_budget_resolver=self._budget,
            session_service=session_service,
        ))
        plan = DialoguePlan(
            mode=DialogueMode.GENERAL, use_repository=False, use_general_knowledge=True,
            reason="synthetic no-evidence general answer", planner_source="explicit",
        )
        displayed, events = [], []
        result = orchestrator.run(
            ConversationRequest("how does it going?", "", "deepseek", model_id="deepseek-flash",
                                conversation_id="synthetic-session", mode="auto", stream=True),
            session=ConversationSession(
                provider=provider, store=store,
                conversation={"conversation_id": "synthetic-session"},
            ),
            planned_plan=plan, on_content=displayed.append, on_event=events.append,
        )

        expected = "A greeting is a casual way to start a conversation."
        self.assertEqual(expected, result.answer.text)
        self.assertEqual(expected, result.contract.content)
        self.assertEqual("limited", result.contract.status)
        self.assertEqual(1, result.retrieval["invalid_source_markers_removed"])
        self.assertEqual(["E1"], result.retrieval["invalid_source_marker_ids_removed"])
        self.assertEqual(expected, result.to_payload()["answer"])
        self.assertEqual(expected, session_service.commit_completed_turn.call_args.args[4].text)
        self.assertEqual([], displayed)
        self.assertFalse(any(event.type is StreamEventType.CONTENT_DELTA for event in events))
        self.assertEqual(StreamEventType.COMPLETED, events[-1].type)

    def test_empty_after_citation_cleanup_uses_one_same_provider_correction(self):
        from types import SimpleNamespace

        from agentforge.dialogue_router import DialogueMode, DialoguePlan

        class EmptyThenCorrectProvider(self.Provider):
            def answer_general(self, context, **kwargs):
                self.calls.append(context)
                answer = Answer("[E1]", (), False) if len(self.calls) == 1 else Answer(
                    "A greeting is an informal way to begin a conversation.", (), False,
                )
                callback = kwargs.get("on_content")
                if callable(callback):
                    callback(answer.text)
                return answer

        provider = EmptyThenCorrectProvider()
        session_service = Mock()
        session_service.restore_context.return_value = SimpleNamespace(
            history=(), rows=(), summary=None, covered_through=None, fact_ledger=(),
        )
        orchestrator = ConversationOrchestrator(ConversationDependencies(
            provider_factory=Mock(), model_budget_resolver=self._budget,
            session_service=session_service,
        ))
        plan = DialoguePlan(
            mode=DialogueMode.GENERAL, use_repository=False, use_general_knowledge=True,
            reason="synthetic empty-response correction", planner_source="explicit",
        )
        result = orchestrator.run(
            ConversationRequest("how does it going?", "", "deepseek", model_id="deepseek-flash",
                                conversation_id="synthetic-session", mode="auto", stream=True),
            session=ConversationSession(
                provider=provider, store=object(),
                conversation={"conversation_id": "synthetic-session"},
            ),
            planned_plan=plan,
        )

        self.assertEqual(2, len(provider.calls))
        self.assertIn("how does it going?", provider.calls[1].question)
        self.assertIn("empty_after_invalid_source_marker_cleanup", provider.calls[1].question)
        self.assertEqual("A greeting is an informal way to begin a conversation.", result.answer.text)
        self.assertEqual(1, result.retrieval["invalid_source_markers_removed"])
        self.assertEqual(["E1"], result.retrieval["invalid_source_marker_ids_removed"])

    def test_second_empty_citation_only_answer_stops_after_one_correction(self):
        from agentforge.dialogue_router import DialogueMode, DialoguePlan
        from agentforge.errors import ProviderError

        class CitationOnlyProvider(self.Provider):
            def answer_general(self, context, **_kwargs):
                self.calls.append(context)
                return Answer("[E1]", (), False)

        provider = CitationOnlyProvider()
        session_service = Mock()
        orchestrator = ConversationOrchestrator(ConversationDependencies(
            provider_factory=Mock(), model_budget_resolver=self._budget,
            session_service=session_service,
        ))
        plan = DialoguePlan(
            mode=DialogueMode.GENERAL, use_repository=False, use_general_knowledge=True,
            reason="synthetic bounded correction", planner_source="explicit",
        )
        with self.assertRaises(ProviderError) as caught:
            orchestrator.run(
                ConversationRequest("Explain a greeting.", "", "deepseek",
                                    model_id="deepseek-flash", mode="auto"),
                session=ConversationSession(provider=provider), planned_plan=plan,
            )
        self.assertEqual("empty_general_answer_after_correction", caught.exception.code)
        self.assertTrue(caught.exception.diagnostic["correction_attempted"])
        self.assertEqual(2, len(provider.calls))
        session_service.commit_completed_turn.assert_not_called()

    def test_repository_retrieval_failure_degrades_but_still_generates_answer(self):
        from agentforge.dialogue_router import DialogueMode, DialoguePlan
        from agentforge.errors import StoreError

        query = "What is the difference between TCP and UDP?"
        provider = self.Provider()
        plan = DialoguePlan(
            mode=DialogueMode.REPOSITORY, use_repository=True,
            use_general_knowledge=False, reason="synthetic repository question",
            planner_source="llm",
        )
        retriever_factory = Mock()

        def fail_preparation(*_args):
            raise StoreError("synthetic SQLite index is unavailable")

        orchestrator = ConversationOrchestrator(ConversationDependencies(
            provider_factory=Mock(), model_budget_resolver=self._budget,
            retriever_factory=retriever_factory,
            repository_preparer=fail_preparation,
        ))
        result = orchestrator.run(
            ConversationRequest(query, r"D:\synthetic\repo", "deepseek",
                                model_id="deepseek-flash", mode="auto"),
            index_store=object(),
            session=ConversationSession(
                provider=provider, repository_id="synthetic-repository",
                conversation={"repository_id": "synthetic-repository"},
            ),
            planned_plan=plan,
        )

        self.assertEqual(1, len(provider.calls))
        self.assertTrue(provider.calls[0]["repository_evidence_unavailable"])
        self.assertEqual((), provider.calls[0]["context"].evidence)
        self.assertEqual("unavailable", result.retrieval["retrieval_status"])
        self.assertEqual("sqlite_index_unavailable", result.retrieval["retrieval_failure_code"])
        self.assertEqual("limited", result.contract.status)
        self.assertTrue(result.answer.text)
        retriever_factory.assert_not_called()

    def test_external_data_plan_uses_provider_safe_limit_instead_of_silent_local_refusal(self):
        from agentforge.dialogue_router import DialogueMode, DialoguePlan

        provider = self.Provider()
        plan = DialoguePlan(
            mode=DialogueMode.GENERAL, use_repository=False,
            use_general_knowledge=False, reason="synthetic external-data request",
            requires_external_data=True, planner_source="llm",
        )
        orchestrator = ConversationOrchestrator(ConversationDependencies(
            provider_factory=Mock(), model_budget_resolver=self._budget,
        ))

        result = orchestrator.run(
            ConversationRequest("What is the current weather?", "", "deepseek",
                                model_id="deepseek-flash", mode="auto"),
            session=ConversationSession(provider=provider), planned_plan=plan,
        )

        self.assertEqual(1, len(provider.calls))
        self.assertTrue(provider.calls[0]["external_data_unavailable"])
        self.assertEqual("limited", result.contract.status)
        self.assertTrue(result.answer.text)

    def test_configured_provider_without_general_answer_interface_never_falls_back_to_offline(self):
        from agentforge.dialogue_router import DialogueMode, DialoguePlan
        from agentforge.errors import ProviderError

        class IncompleteProvider:
            provider_id = "custom"

        plan = DialoguePlan(
            mode=DialogueMode.GENERAL, use_repository=False,
            use_general_knowledge=True, reason="synthetic general plan",
        )
        orchestrator = ConversationOrchestrator(ConversationDependencies(
            provider_factory=Mock(), model_budget_resolver=self._budget,
        ))
        with self.assertRaisesRegex(ProviderError, "cannot generate a general answer"):
            orchestrator.run(
                ConversationRequest("What is TCP?", "", "custom", mode="auto"),
                session=ConversationSession(provider=IncompleteProvider()), planned_plan=plan,
            )

if __name__ == "__main__":
    unittest.main()
