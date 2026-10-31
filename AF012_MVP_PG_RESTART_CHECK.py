"""Verify AF-012-MVP session, memory and outbox durability around PG restart."""
import json
import os
from pathlib import Path
import re
import sys
from uuid import UUID, uuid4


ROOT = Path(__file__).resolve().parent
REVISION = "af012mvp_002"


def _checkpoint_path(value):
    path = Path(value).resolve()
    data = (ROOT / ".agentforge" / "data").resolve()
    if (path.parent != data
            or re.fullmatch(r"af012-mvp-restart-[0-9a-f]{32}\.json", path.name) is None):
        raise ValueError("Checkpoint path is outside the dedicated AgentForge data directory.")
    return path


def _restart_observation_fields(restarted):
    observed = bool(restarted)
    return {
        "service_restart_observed": observed,
        "server_restart_observed": observed,
    }


def _cleanup(engine, repository_id, conversation_id, memory_id):
    from sqlalchemy import delete
    from agentforge.af012_schema import (
        Conversation, ConversationSelection, ConversationSummary, Memory,
        MemoryApproval, MemoryEvent, MemorySource, Message, RepositoryMetadata,
        VectorSyncOutbox,
    )
    with engine.begin() as connection:
        if repository_id:
            connection.execute(delete(VectorSyncOutbox).where(
                VectorSyncOutbox.repository_id == repository_id))
        if memory_id:
            identity = UUID(memory_id)
            connection.execute(delete(MemorySource).where(MemorySource.memory_id == identity))
            connection.execute(delete(MemoryApproval).where(MemoryApproval.memory_id == identity))
            connection.execute(delete(MemoryEvent).where(MemoryEvent.memory_id == identity))
            connection.execute(delete(Memory).where(Memory.id == identity))
        if repository_id:
            connection.execute(delete(ConversationSelection).where(
                ConversationSelection.repository_id == repository_id))
        if conversation_id:
            identity = UUID(conversation_id)
            connection.execute(delete(ConversationSummary).where(
                ConversationSummary.conversation_id == identity))
            connection.execute(delete(Message).where(Message.conversation_id == identity))
            connection.execute(delete(Conversation).where(Conversation.id == identity))
        if repository_id:
            connection.execute(delete(RepositoryMetadata).where(
                RepositoryMetadata.repository_id == repository_id))


def main():
    if os.environ.get("CODEX_SANDBOX_NETWORK_DISABLED") == "1":
        print(json.dumps({"status": "blocked_by_environment", "phase": "environment_gate"}))
        return 2
    if len(sys.argv) != 3 or sys.argv[1] not in ("prepare", "verify"):
        print("Expected prepare/verify and a dedicated checkpoint path.", file=sys.stderr)
        return 2
    phase = sys.argv[1]
    path = _checkpoint_path(sys.argv[2])
    if not os.environ.get("AGENTFORGE_DB_PASSWORD"):
        print(json.dumps({"status": "blocked_by_configuration", "phase": "configuration",
                          "missing": "AGENTFORGE_DB_PASSWORD"}))
        return 2

    from sqlalchemy import text
    from sqlalchemy.exc import SQLAlchemyError
    from agentforge.af012_connection import create_local_engine
    from agentforge.af012_schema import Memory
    from agentforge.conversation_store import ConversationStore
    from agentforge.memory_store import MemoryStore

    engine = create_local_engine(os.environ["AGENTFORGE_DB_PASSWORD"])
    result = {"status": "failed", "phase": phase, "temporary_rows_cleaned": False,
              "service_restart_observed": False, "migration_revision": None}
    repository_id = conversation_id = memory_id = None
    keep_rows = False
    checkpoint = None
    try:
        if phase == "verify":
            checkpoint = json.loads(path.read_text(encoding="utf-8"))
            repository_id, conversation_id, memory_id = (
                checkpoint["repository_id"], checkpoint["conversation_id"], checkpoint["memory_id"])
            if (not repository_id.startswith("af012-mvp-restart-")
                    or checkpoint.get("migration_revision") != REVISION):
                raise ValueError("Checkpoint scope or migration marker is invalid.")
        with engine.connect() as connection:
            start_time = connection.scalar(text("SELECT pg_postmaster_start_time()"))
            revision = connection.execute(text(
                "SELECT version_num FROM alembic_version")).scalar_one()
        result["migration_revision"] = revision
        if revision != REVISION:
            raise ValueError("AF-012-MVP migration head does not match the expected revision.")
        if phase == "prepare":
            if path.exists():
                raise ValueError("Checkpoint already exists.")
            repository_id = "af012-mvp-restart-" + uuid4().hex
            conversations = ConversationStore(engine)
            memories = MemoryStore(engine)
            memories.register_repository(repository_id, str(ROOT.resolve()), "Disposable restart check")
            conversation = conversations.create_conversation(repository_id, "AF012 MVP restart check")
            conversation_id = conversation["id"]
            conversations.switch_conversation(conversation_id, repository_id)
            conversations.append_turn(conversation_id, repository_id,
                "Please retain this disposable fact: AF012 MVP restart verification.",
                "Disposable restart test turn recorded.")
            user_message = conversations.get_messages(conversation_id, repository_id)[0]
            memory = memories.propose(repository_id, conversation_id,
                "Disposable approved memory for PostgreSQL restart verification.",
                [user_message["id"]], memory_type="project_fact")
            memory_id = memory["id"]
            memories.approve(memory_id, repository_id)
            checkpoint = {"repository_id": repository_id, "conversation_id": conversation_id,
                "memory_id": memory_id, "server_started_at": start_time.isoformat(),
                "messages": ["Please retain this disposable fact: AF012 MVP restart verification.",
                             "Disposable restart test turn recorded."],
                "memory_content": "Disposable approved memory for PostgreSQL restart verification.",
                "migration_revision": revision}
            with path.open("x", encoding="utf-8") as output:
                json.dump(checkpoint, output, ensure_ascii=False, indent=2)
            keep_rows = True
            result.update(status="prepared", checkpoint=str(path),
                repository_id=repository_id, conversation_id=conversation_id,
                memory_id=memory_id, outbox_status=memories.outbox_status(repository_id))
        else:
            restarted = checkpoint["server_started_at"] != start_time.isoformat()
            conversations = ConversationStore(engine)
            memories = MemoryStore(engine)
            conversation = conversations.get_conversation(conversation_id, repository_id)
            messages = conversations.get_messages(conversation_id, repository_id)
            memory = memories.approved_by_id(memory_id, repository_id)
            sources = memories.source_records(memory_id, repository_id)
            outbox = memories.outbox_status(repository_id)
            current = conversations.current_conversation(repository_id)
            restored = (
                conversation["id"] == conversation_id
                and [item["content"] for item in messages] == checkpoint["messages"]
                and current is not None and current["id"] == conversation_id
                and memory is not None and memory["content"] == checkpoint["memory_content"]
                and len(sources) == 1 and sources[0]["source_role"] == "user"
                and outbox.get("pending") == 1
            )
            result.update(status="passed" if restarted and restored else "failed",
                **_restart_observation_fields(restarted), restored=restored,
                before=checkpoint["server_started_at"], after=start_time.isoformat(),
                conversation_id=conversation_id, memory_id=memory_id,
                message_count=len(messages), memory_source_count=len(sources),
                outbox_status=outbox)
    except Exception as exc:
        result["error_type"] = type(exc).__name__
        if isinstance(exc, SQLAlchemyError):
            result["error_category"] = "database"
    finally:
        if phase == "verify" or (phase == "prepare" and not keep_rows):
            try:
                if repository_id or conversation_id or memory_id:
                    _cleanup(engine, repository_id, conversation_id, memory_id)
                result["temporary_rows_cleaned"] = True
            except Exception as exc:
                result["temporary_rows_cleaned"] = False
                result["cleanup_error_type"] = type(exc).__name__
                result["status"] = "failed"
        engine.dispose()

    if phase == "prepare" and not keep_rows and path.exists():
        try:
            path.unlink()
            result["checkpoint_cleaned"] = True
        except OSError:
            result["checkpoint_cleaned"] = False
            result["status"] = "failed"

    result_file = ROOT / ("AF012_MVP_PG_RESTART_RESULT_" + uuid4().hex + ".json")
    result["result_file"] = str(result_file)
    if phase == "verify" and result["temporary_rows_cleaned"]:
        try:
            path.unlink()
            result["checkpoint_cleaned"] = True
        except OSError:
            result["checkpoint_cleaned"] = False
            result["status"] = "failed"
    result_file.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] in ("prepared", "passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
