"""Application service for restoring and committing conversation turns.

The service depends on a repository port implemented by the persistence
adapter. It owns conversation-history mapping and keeps orchestration
independent from ORM models and SQLAlchemy exceptions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class SessionRepository(Protocol):
    def create_conversation(self, repository_id, title): ...
    def list_conversations(self, repository_id): ...
    def current_conversation(self, repository_id): ...
    def get_conversation(self, conversation_id, repository_id): ...
    def switch_conversation(self, conversation_id, repository_id): ...
    def rename_conversation(self, conversation_id, repository_id, title): ...
    def delete_conversation(self, conversation_id, repository_id): ...
    def get_messages(self, conversation_id, repository_id): ...
    def latest_summary(self, conversation_id, repository_id): ...
    def update_model_binding(self, conversation_id, repository_id, **options): ...
    def revoke_cloud_consent(self, conversation_id, repository_id): ...
    def prepare_history(self, conversation_id, repository_id, *,
                        summarizer=None, summary_model=None): ...

    def append_turn(self, conversation_id, repository_id, question, answer, **metadata): ...


@dataclass(frozen=True)
class RestoredConversation:
    rows: tuple[dict, ...]
    summary: dict | None
    history: tuple[object, ...]
    fact_ledger: tuple[dict, ...]
    covered_through: int


class SessionService:
    """Load model context and persist only completed answer turns."""

    def create(self, repository: SessionRepository, repository_id: str, title: str):
        return repository.create_conversation(repository_id, title)

    def list(self, repository: SessionRepository, repository_id: str):
        return repository.list_conversations(repository_id)

    def current(self, repository: SessionRepository, repository_id: str):
        return repository.current_conversation(repository_id)

    def get(self, repository: SessionRepository, conversation_id: str, repository_id: str):
        return repository.get_conversation(conversation_id, repository_id)

    def messages(self, repository: SessionRepository, conversation_id: str, repository_id: str):
        return repository.get_messages(conversation_id, repository_id)

    def switch(self, repository: SessionRepository, conversation_id: str, repository_id: str):
        return repository.switch_conversation(conversation_id, repository_id)

    def rename(self, repository: SessionRepository, conversation_id: str,
               repository_id: str, title: str):
        return repository.rename_conversation(conversation_id, repository_id, title)

    def delete(self, repository: SessionRepository, conversation_id: str, repository_id: str):
        return repository.delete_conversation(conversation_id, repository_id)

    def bind_model(self, repository: SessionRepository, conversation_id: str,
                   repository_id: str, **options):
        return repository.update_model_binding(conversation_id, repository_id, **options)

    def revoke_cloud_consent(self, repository: SessionRepository,
                             conversation_id: str, repository_id: str):
        return repository.revoke_cloud_consent(conversation_id, repository_id)

    def restore_context(self, repository: SessionRepository, conversation_id: str,
                        repository_id: str, *, summarizer=None,
                        summary_model: str | None = None) -> RestoredConversation:
        if not callable(getattr(repository, "prepare_history", None)):
            raise ValueError("Session repository does not support history restoration.")
        rows, summary = repository.prepare_history(
            conversation_id, repository_id, summarizer=summarizer,
            summary_model=summary_model,
        )
        return _map_restored_context(conversation_id, repository_id, rows, summary)

    def load_context(self, repository: SessionRepository, conversation_id: str,
                     repository_id: str) -> RestoredConversation:
        """Read the current history snapshot without triggering summary compaction."""
        if (not callable(getattr(repository, "get_messages", None))
                or not callable(getattr(repository, "latest_summary", None))):
            raise ValueError("Session repository does not support read-only context loading.")
        rows = repository.get_messages(conversation_id, repository_id)
        summary = repository.latest_summary(conversation_id, repository_id)
        return _map_restored_context(conversation_id, repository_id, rows, summary)

    def commit_completed_turn(self, repository: SessionRepository, conversation_id: str,
                              repository_id: str | None, question: str, answer,
                              provider=None, *, request_diagnostics=None) -> None:
        if not callable(getattr(repository, "append_turn", None)):
            raise ValueError("Session repository does not support completed-turn persistence.")
        generated = not answer.offline and provider is not None
        stats = provider.request_stats() if generated and callable(
            getattr(provider, "request_stats", None)) else {}
        usage = stats.get("last_usage") if isinstance(stats, dict) else None
        elapsed = stats.get("last_elapsed_seconds") if isinstance(stats, dict) else None
        latency_ms = (round(elapsed * 1000) if isinstance(elapsed, (int, float))
                      and not isinstance(elapsed, bool) else None)
        model_id = getattr(provider, "model", None) if generated else None
        provider_id = getattr(provider, "provider_id", None) if generated else None
        metadata = {
            "model_name": model_id, "provider_id": provider_id, "model_id": model_id,
            "usage": usage, "latency_ms": latency_ms,
            "request_id": stats.get("last_request_id") if isinstance(stats, dict) else None,
        }
        if request_diagnostics is not None:
            metadata["request_diagnostics"] = request_diagnostics
        repository.append_turn(conversation_id, repository_id, question, answer.text, **metadata)

    def commit_execution_turn(self, repository: SessionRepository, conversation_id: str,
                              repository_id: str, execution_id: str, question: str,
                              answer, provider=None, *, request_diagnostics=None) -> str:
        """Persist one completed Agent execution exactly once in conversation history."""
        append = getattr(repository, "append_execution_turn", None)
        if not callable(append):
            raise ValueError("Session repository does not support idempotent Agent turn persistence.")
        generated = not answer.offline and provider is not None
        stats = provider.request_stats() if generated and callable(
            getattr(provider, "request_stats", None)) else {}
        usage = stats.get("last_usage") if isinstance(stats, dict) else None
        elapsed = stats.get("last_elapsed_seconds") if isinstance(stats, dict) else None
        latency_ms = (round(elapsed * 1000) if isinstance(elapsed, (int, float))
                      and not isinstance(elapsed, bool) else None)
        model_id = getattr(provider, "model", None) if generated else None
        provider_id = getattr(provider, "provider_id", None) if generated else None
        metadata = {
            "model_name": model_id, "provider_id": provider_id, "model_id": model_id,
            "usage": usage, "latency_ms": latency_ms,
            "request_id": stats.get("last_request_id") if isinstance(stats, dict) else None,
        }
        if request_diagnostics is not None:
            metadata["request_diagnostics"] = request_diagnostics
        return append(conversation_id, repository_id, execution_id, question,
                      answer.text, **metadata)


__all__ = ["RestoredConversation", "SessionRepository", "SessionService"]


def _map_restored_context(conversation_id, repository_id, rows, summary):
    rows = tuple(rows)
    covered = summary["covered_through"] if summary else 0
    from .conversation_context import ConversationTurn
    from .conversation_facts import extract_message_facts, merge_fact_ledgers

    history = tuple(ConversationTurn(
        conversation_id, repository_id, row["sequence"], row["role"],
        row["content"], row["status"],
    ) for row in rows if row["sequence"] > covered)
    ledger = tuple(merge_fact_ledgers(
        summary.get("fact_ledger", []) if summary else [],
        extract_message_facts(rows),
    ))
    return RestoredConversation(rows, summary, history, ledger, covered)
