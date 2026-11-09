"""Behavior tests for persisted conversation, approved memory, and stream boundaries."""

from datetime import datetime, timezone
import importlib.util
import unittest
from unittest.mock import Mock

from agentforge.conversation_context import MemoryCandidate, MemorySourceReference
from agentforge.memory_service import MemoryService
from agentforge.streaming import StreamEventType, StreamingManager


@unittest.skipUnless(importlib.util.find_spec("sqlalchemy") and importlib.util.find_spec("alembic"),
                     "AF-012 SQLAlchemy/Alembic dependencies unavailable")
class PersistedConversationBoundaryTests(unittest.TestCase):
    def setUp(self):
        from sqlalchemy import create_engine, event
        from agentforge.conversation_migrations import upgrade
        from agentforge.conversation_store import ConversationStore

        self.engine = create_engine("sqlite+pysqlite:///:memory:")

        @event.listens_for(self.engine, "connect")
        def enable_foreign_keys(connection, _record):
            connection.execute("PRAGMA foreign_keys=ON")

        upgrade(self.engine)
        self.store = ConversationStore(self.engine)

    def tearDown(self):
        self.engine.dispose()

    def test_cloud_binding_retains_consent_only_for_same_model_and_revoke_clears_it(self):
        conversation = self.store.create_conversation("repo-a", "Model binding")
        consent = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)
        first = self.store.update_model_binding(
            conversation["id"], "repo-a", provider_id="deepseek", model_id="deepseek-flash",
            cloud_consent_at=consent,
        )

        same_binding = self.store.update_model_binding(
            conversation["id"], "repo-a", provider_id="deepseek", model_id="deepseek-flash",
            thinking_enabled=True, reasoning_effort="high",
        )
        self.assertEqual(first["cloud_consent_at"], same_binding["cloud_consent_at"])
        self.assertTrue(same_binding["thinking_enabled"])
        self.assertEqual("high", same_binding["reasoning_effort"])

        with self.assertRaisesRegex(ValueError, "explicit user consent"):
            self.store.update_model_binding(
                conversation["id"], "repo-a", provider_id="deepseek", model_id="deepseek-v4-pro",
            )
        unchanged = self.store.get_conversation(conversation["id"], "repo-a")
        self.assertEqual("deepseek-flash", unchanged["model_id"])
        self.assertEqual(first["cloud_consent_at"], unchanged["cloud_consent_at"])

        revoked = self.store.revoke_cloud_consent(conversation["id"], "repo-a")
        self.assertIsNone(revoked["cloud_consent_at"])
        with self.assertRaisesRegex(ValueError, "explicit user consent"):
            self.store.update_model_binding(
                conversation["id"], "repo-a", provider_id="deepseek", model_id="deepseek-flash",
            )

    def test_invalid_model_usage_and_diagnostics_never_persist_a_partial_turn(self):
        conversation = self.store.create_conversation("repo-a", "Rejected metadata")
        invalid_options = (
            {"usage": ["not", "an", "object"]},
            {"latency_ms": True},
            {"request_id": "bad\nrequest"},
            {"request_diagnostics": "synthetic prompt content"},
        )
        for options in invalid_options:
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.store.append_turn(
                    conversation["id"], "repo-a", "synthetic question", "synthetic answer", **options,
                )
        self.assertEqual([], self.store.get_messages(conversation["id"], "repo-a"))

        self.store.append_turn(
            conversation["id"], "repo-a", "synthetic question", "synthetic answer",
            provider_id="offline", model_id="offline",
            usage={"prompt_tokens": 12, "completion_tokens": True, "reasoning_tokens": -1},
            request_diagnostics={"answer": {
                "phase": "answer", "request_succeeded": True,
                "usage": {"prompt_tokens": 12, "completion_tokens": True, "api_key": "omit"},
                "prompt": "SYNTHETIC_PROMPT_MUST_NOT_PERSIST",
            }},
        )
        messages = self.store.get_messages(conversation["id"], "repo-a")
        self.assertEqual(["user", "assistant"], [row["role"] for row in messages])
        self.assertEqual({"prompt_tokens": 12}, messages[-1]["metadata"]["usage"])
        self.assertEqual({"prompt_tokens": 12},
                         messages[-1]["metadata"]["request_diagnostics"]["answer"]["usage"])
        self.assertNotIn("SYNTHETIC_PROMPT_MUST_NOT_PERSIST", str(messages[-1]["metadata"]))


class ApprovedMemoryIntegrityBoundaryTests(unittest.TestCase):
    @staticmethod
    def _candidate(memory_id, *, sequence=1, digest="a" * 64):
        return MemoryCandidate(
            "repo-a", f"fact {memory_id}", True, memory_id, "project_fact", 1,
            (MemorySourceReference(f"message-{memory_id}", sequence, "user", digest),),
            "repository",
        )

    @staticmethod
    def _authoritative(memory_id):
        return {
            "id": memory_id, "approval_status": "approved", "scope": "repository",
            "version": 1, "memory_type": "project_fact", "content": f"fact {memory_id}",
        }

    def test_duplicate_candidates_are_deduplicated_and_lookup_stops_at_requested_limit(self):
        first = self._candidate("m1")
        second = self._candidate("m2", sequence=2, digest="b" * 64)
        third = self._candidate("m3", sequence=3, digest="c" * 64)
        retriever = Mock()
        retriever.search.return_value = [first, first, second, third]
        repository = Mock()
        records = {candidate.memory_id: self._authoritative(candidate.memory_id)
                   for candidate in (first, second, third)}
        repository.approved_by_id.side_effect = records.get
        repository.source_records.side_effect = lambda memory_id, _repo: [{
            "message_id": f"message-{memory_id}", "source_sequence": {"m1": 1, "m2": 2, "m3": 3}[memory_id],
            "source_role": "user", "source_hash": {"m1": "a" * 64, "m2": "b" * 64,
                                                               "m3": "c" * 64}[memory_id],
        }]

        result = MemoryService(repository).retrieve_approved(
            retriever, "question about facts", "repo-a", limit=2,
        )

        self.assertEqual((first, second), result)
        self.assertEqual([("m1", "repo-a"), ("m2", "repo-a")],
                         [call.args for call in repository.approved_by_id.call_args_list])
        repository.source_records.assert_any_call("m2", "repo-a")
        self.assertEqual(2, repository.approved_by_id.call_count)

    def test_memory_with_mismatched_or_malformed_source_identity_is_rejected(self):
        candidates = (
            self._candidate("m1", digest="d" * 64),
            self._candidate("m1", sequence=True, digest="a" * 64),
        )
        for candidate in candidates:
            with self.subTest(source=candidate.sources[0]):
                retriever = Mock()
                retriever.search.return_value = [candidate]
                repository = Mock()
                repository.approved_by_id.return_value = self._authoritative("m1")
                repository.source_records.return_value = [{
                    "message_id": "message-m1", "source_sequence": 1,
                    "source_role": "user", "source_hash": "a" * 64,
                }]

                result = MemoryService(repository).retrieve_approved(
                    retriever, "question", "repo-a",
                )

                self.assertEqual((), result)


class StreamingSinkBoundaryTests(unittest.TestCase):
    def test_publish_sinks_receive_validated_events_and_metadata_is_sanitized(self):
        class PublishSink:
            def __init__(self):
                self.events = []

            def publish(self, event):
                self.events.append(event)

        sink = PublishSink()
        manager = StreamingManager(on_event=sink)
        manager.start()
        manager.complete_validated(
            "Synthetic validated answer", usage=["malformed usage must be ignored"],
        )
        self.assertEqual(StreamEventType.COMPLETED, sink.events[-1].type)
        self.assertEqual({}, sink.events[-2].payload["usage"])
        self.assertTrue(all(event.sequence == index
                            for index, event in enumerate(sink.events, start=1)))

    def test_safe_provider_error_code_is_preserved_without_exposing_error_text(self):
        events = []
        manager = StreamingManager(on_event=events.append)
        manager.start()
        manager.fail("provider_timeout")
        self.assertEqual({"error_code": "provider_timeout"}, events[-1].payload)


if __name__ == "__main__":
    unittest.main()
