"""One explicit host-side PostgreSQL verification; no model or HTTP requests."""
import json
import os
from pathlib import Path
from uuid import uuid4


def main():
    if os.environ.get("CODEX_SANDBOX_NETWORK_DISABLED") == "1":
        print(json.dumps({"status": "blocked_by_environment", "database_requests": 0}))
        return 2
    from sqlalchemy import inspect, text
    from agentforge.af012_connection import create_local_engine
    from agentforge.conversation_migrations import upgrade
    from agentforge.conversation_store import ConversationStore

    result = {"status": "failed", "checks": {}, "temporary_rows_cleaned": False,
              "model_requests": 0, "service_restart": "not_measured"}
    engine = None
    created = []
    repo = "af012-b2-test-" + uuid4().hex
    try:
        engine = create_local_engine(os.environ.get("AGENTFORGE_DB_PASSWORD"))
        with engine.connect() as conn:
            row = conn.execute(text("SELECT current_user, current_database(), "
                                    "inet_server_addr()::text, inet_server_port(), "
                                    "current_setting('listen_addresses')")).one()
            if tuple(row) != ("agentforge_app", "agentforge", "127.0.0.1/32", 5432, "127.0.0.1"):
                # PostgreSQL versions may render a host inet without /32.
                if tuple(row) != ("agentforge_app", "agentforge", "127.0.0.1", 5432, "127.0.0.1"):
                    raise RuntimeError("Unexpected database, role or listening configuration.")
            existing = set(inspect(conn).get_table_names())
            owned = {"conversations", "messages", "conversation_summaries", "conversation_selections"}
            if existing & owned and "alembic_version" not in existing:
                raise RuntimeError("Existing conversation tables have no migration history.")
        result["checks"]["local_identity"] = "passed"
        upgrade(engine)
        result["checks"]["migration"] = "passed"
        store = ConversationStore(engine)
        first = store.create_conversation(repo, "temporary integration test")
        created.append(first["id"])
        second = store.create_conversation(repo, "temporary isolated session")
        created.append(second["id"])
        store.switch_conversation(first["id"], repo)
        store.append_turn(first["id"], repo, "test question", "test answer")
        rows = store.get_messages(first["id"], repo)
        if [row["role"] for row in rows] != ["user", "assistant"]:
            raise RuntimeError("Message order mismatch.")
        if store.get_messages(second["id"], repo):
            raise RuntimeError("Conversations are not isolated.")
        try:
            store.get_conversation(first["id"], repo + "-other")
        except ValueError:
            pass
        else:
            raise RuntimeError("Repository boundary did not reject a cross-scope request.")
        store.save_summary(first["id"], repo, rows[-1]["id"], "temporary summary")
        store.rename_conversation(first["id"], repo, "temporary renamed session")
        result["checks"]["crud_order_scope_summary"] = "passed"
        try:
            with engine.begin() as conn:
                conn.execute(text("UPDATE conversations SET title = :title WHERE id = :id"),
                             {"title": "rollback probe", "id": first["id"]})
                raise RuntimeError("intentional rollback")
        except RuntimeError:
            pass
        if store.get_conversation(first["id"], repo)["title"] != "temporary renamed session":
            raise RuntimeError("Transaction rollback did not preserve the committed title.")
        result["checks"]["transaction_rollback"] = "passed"
        engine.dispose()
        engine = create_local_engine(os.environ.get("AGENTFORGE_DB_PASSWORD"))
        store = ConversationStore(engine)
        if (store.current_conversation(repo)["id"] != first["id"]
                or len(store.get_messages(first["id"], repo)) != 2
                or not store.latest_summary(first["id"], repo)):
            raise RuntimeError("Reconnection did not restore the persisted state.")
        result["checks"]["new_engine_reconnect"] = "passed"
        result["status"] = "passed"
    except Exception as exc:
        result["error_type"] = type(exc).__name__
        result["error_stage"] = next((stage for stage in ("local_identity", "migration", "crud_order_scope_summary", "transaction_rollback", "new_engine_reconnect")
                                      if stage not in result["checks"]), "unknown")
        # Do not record database-driver error messages, which can contain secrets.
    finally:
        if engine is not None:
            try:
                store = ConversationStore(engine)
                for item in reversed(created):
                    store.delete_conversation(item, repo)
                result["temporary_rows_cleaned"] = not bool(store.list_conversations(repo)) if created else True
            except Exception as exc:
                result["status"] = "failed"
                result["cleanup_error_type"] = type(exc).__name__
            engine.dispose()
    target = Path(__file__).resolve().parent / ("AF012_B2_DB_VERIFY_" + uuid4().hex + ".json")
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"result_file": str(target), **result}, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "passed" and result["temporary_rows_cleaned"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
