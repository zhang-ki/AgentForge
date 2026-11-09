import unittest
from unittest.mock import Mock

from agentforge.models import Answer
from agentforge.session_service import SessionService


class SessionServiceTests(unittest.TestCase):
    def test_session_lifecycle_uses_repository_port_through_application_service(self):
        repository = Mock()
        repository.create_conversation.return_value = {"id": "conversation-1"}
        repository.get_conversation.return_value = {"id": "conversation-1"}
        service = SessionService()
        self.assertEqual({"id": "conversation-1"}, service.create(repository, "repo-1", "title"))
        self.assertEqual({"id": "conversation-1"}, service.get(repository, "conversation-1", "repo-1"))
        service.switch(repository, "conversation-1", "repo-1")
        service.rename(repository, "conversation-1", "repo-1", "renamed")
        service.bind_model(repository, "conversation-1", "repo-1", provider_id="offline")
        service.revoke_cloud_consent(repository, "conversation-1", "repo-1")
        service.delete(repository, "conversation-1", "repo-1")
        repository.create_conversation.assert_called_once_with("repo-1", "title")
        repository.switch_conversation.assert_called_once_with("conversation-1", "repo-1")
        repository.rename_conversation.assert_called_once_with("conversation-1", "repo-1", "renamed")
        repository.update_model_binding.assert_called_once_with(
            "conversation-1", "repo-1", provider_id="offline")
        repository.revoke_cloud_consent.assert_called_once_with("conversation-1", "repo-1")
        repository.delete_conversation.assert_called_once_with("conversation-1", "repo-1")

    def test_restore_maps_only_uncovered_rows_into_model_history(self):
        repository = Mock()
        repository.prepare_history.return_value = (
            ({"sequence": 1, "role": "user", "content": "early fact", "status": "completed"},
             {"sequence": 2, "role": "assistant", "content": "ack", "status": "completed"},
             {"sequence": 3, "role": "user", "content": "recent question", "status": "completed"},
             {"sequence": 4, "role": "assistant", "content": "recent answer", "status": "completed"}),
            {"covered_through": 2, "content": "Summary of early fact", "fact_ledger": []},
        )
        restored = SessionService().restore_context(
            repository, "conversation-1", "repo-1")
        self.assertEqual((3, 4), tuple(turn.sequence for turn in restored.history))
        self.assertEqual("Summary of early fact", restored.summary["content"])
        self.assertEqual(2, restored.covered_through)

    def test_read_only_context_load_does_not_request_history_compaction(self):
        repository = Mock()
        repository.get_messages.return_value = (
            {"sequence": 1, "role": "user", "content": "question", "status": "completed"},
            {"sequence": 2, "role": "assistant", "content": "answer", "status": "completed"},
        )
        repository.latest_summary.return_value = None

        restored = SessionService().load_context(repository, "conversation-1", "repo-1")

        self.assertEqual(2, len(restored.rows))
        self.assertEqual((1, 2), tuple(turn.sequence for turn in restored.history))
        repository.get_messages.assert_called_once_with("conversation-1", "repo-1")
        repository.latest_summary.assert_called_once_with("conversation-1", "repo-1")
        repository.prepare_history.assert_not_called()

    def test_commit_records_answer_and_provider_usage_without_database_coupling(self):
        repository = Mock()
        provider = Mock()
        provider.provider_id = "llama-server"
        provider.model = "qwen3.5-4b"
        provider.request_stats.return_value = {
            "last_usage": {"prompt_tokens": 10, "completion_tokens": 4},
            "last_elapsed_seconds": 1.25, "last_request_id": "request-1",
        }
        SessionService().commit_completed_turn(
            repository, "conversation-1", "repo-1", "question", Answer("answer", (), False), provider)
        repository.append_turn.assert_called_once_with(
            "conversation-1", "repo-1", "question", "answer",
            model_name="qwen3.5-4b", provider_id="llama-server", model_id="qwen3.5-4b",
            usage={"prompt_tokens": 10, "completion_tokens": 4}, latency_ms=1250,
            request_id="request-1",
        )

    def test_commit_forwards_content_free_request_diagnostics_only_when_provided(self):
        repository = Mock()
        provider = Mock()
        provider.provider_id = "deepseek"
        provider.model = "deepseek-chat"
        provider.request_stats.return_value = {
            "last_usage": {"prompt_tokens": 200, "completion_tokens": 30},
            "last_elapsed_seconds": 2.0,
        }
        phases = {"planner": {"input_budget_tokens": 500_000},
                  "answer": {"estimated_input_tokens": 2_000}}
        SessionService().commit_completed_turn(
            repository, "conversation-1", "repo-1", "question", Answer("answer", (), False),
            provider, request_diagnostics={"request_phases": phases},
        )
        metadata = repository.append_turn.call_args.kwargs
        self.assertEqual(phases, metadata["request_diagnostics"]["request_phases"])
        self.assertEqual(200, metadata["usage"]["prompt_tokens"])

    def test_commit_execution_turn_uses_idempotent_repository_port(self):
        repository = Mock()
        repository.append_execution_turn.return_value = "turn-1"
        provider = Mock()
        provider.provider_id = "llama-server"
        provider.model = "qwen3.5-4b"
        provider.request_stats.return_value = {
            "last_usage": {"prompt_tokens": 20, "completion_tokens": 7},
            "last_elapsed_seconds": 0.5, "last_request_id": "request-1",
        }
        result = SessionService().commit_execution_turn(
            repository, "conversation-1", "repo-1", "execution-1", "objective",
            Answer("verified answer", ("src/example.py:1-2",), False), provider,
            request_diagnostics={"agent_planner_1": {"input_budget_tokens": 3000}},
        )
        self.assertEqual("turn-1", result)
        repository.append_execution_turn.assert_called_once_with(
            "conversation-1", "repo-1", "execution-1", "objective", "verified answer",
            model_name="qwen3.5-4b", provider_id="llama-server", model_id="qwen3.5-4b",
            usage={"prompt_tokens": 20, "completion_tokens": 7}, latency_ms=500,
            request_id="request-1",
            request_diagnostics={"agent_planner_1": {"input_budget_tokens": 3000}},
        )

    def test_missing_repository_capability_fails_explicitly(self):
        with self.assertRaisesRegex(ValueError, "history restoration"):
            SessionService().restore_context(object(), "conversation-1", "repo-1")
        with self.assertRaisesRegex(ValueError, "completed-turn persistence"):
            SessionService().commit_completed_turn(
                object(), "conversation-1", "repo-1", "question", Answer("answer", (), True))

    def test_read_only_load_and_execution_commit_require_their_repository_ports(self):
        service = SessionService()
        with self.subTest("read-only load requires both readers"):
            with self.assertRaisesRegex(ValueError, "read-only context loading"):
                service.load_context(object(), "conversation-1", "repo-1")
        with self.subTest("execution commit requires idempotent persistence"):
            with self.assertRaisesRegex(ValueError, "idempotent Agent turn persistence"):
                service.commit_execution_turn(
                    object(), "conversation-1", "repo-1", "execution-1", "question",
                    Answer("answer", (), False),
                )


if __name__ == "__main__":
    unittest.main()
