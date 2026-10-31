"""Create/verify only one disposable PostgreSQL record around a service restart."""
import json
import os
from pathlib import Path
import sys
from uuid import uuid4


def main():
    if os.environ.get("CODEX_SANDBOX_NETWORK_DISABLED") == "1":
        print(json.dumps({"status": "blocked_by_environment"}))
        return 2
    if len(sys.argv) != 3 or sys.argv[1] not in ("prepare", "verify"):
        print("Expected prepare/verify and a dedicated checkpoint path.", file=sys.stderr)
        return 2
    root = Path(__file__).resolve().parent
    path = Path(sys.argv[2]).resolve()
    data_dir = (root / ".agentforge" / "data").resolve()
    if path.parent != data_dir or not path.name.startswith("af012-restart-") or path.suffix != ".json":
        print("Invalid checkpoint path.", file=sys.stderr)
        return 2
    from sqlalchemy import text
    from agentforge.af012_connection import create_local_engine
    from agentforge.conversation_store import ConversationStore
    engine = create_local_engine(os.environ.get("AGENTFORGE_DB_PASSWORD"))
    result = {"status": "failed", "phase": sys.argv[1], "temporary_rows_cleaned": False}
    repo = cid = None
    try:
        with engine.connect() as connection:
            started = connection.scalar(text("SELECT pg_postmaster_start_time()"))
        store = ConversationStore(engine)
        if sys.argv[1] == "prepare":
            if path.exists():
                raise ValueError("Checkpoint already exists.")
            repo = "af012-b2-restart-" + uuid4().hex
            cid = store.create_conversation(repo, "temporary restart verification")["id"]
            store.append_turn(cid, repo, "temporary question", "temporary answer")
            store.switch_conversation(cid, repo)
            checkpoint = {"repository_id": repo, "conversation_id": cid,
                          "server_started_at": started.isoformat()}
            with path.open("x", encoding="utf-8") as output:
                json.dump(checkpoint, output)
            result.update(status="prepared", checkpoint=str(path))
        else:
            checkpoint = json.loads(path.read_text(encoding="utf-8"))
            repo, cid = checkpoint["repository_id"], checkpoint["conversation_id"]
            if not repo.startswith("af012-b2-restart-"):
                raise ValueError("Invalid checkpoint scope.")
            restarted = checkpoint["server_started_at"] != started.isoformat()
            current = store.current_conversation(repo)
            rows = store.get_messages(cid, repo)
            restored = current is not None and current["id"] == cid and [m["content"] for m in rows] == ["temporary question", "temporary answer"]
            result.update(status="passed" if restarted and restored else "failed",
                          server_restart_observed=restarted, restored=restored,
                          before=checkpoint["server_started_at"], after=started.isoformat())
    except Exception as exc:
        result["error_type"] = type(exc).__name__
    finally:
        if repo and cid and (sys.argv[1] == "verify" or result["status"] != "prepared"):
            try:
                ConversationStore(engine).delete_conversation(cid, repo)
                result["temporary_rows_cleaned"] = True
            except Exception as exc:
                result["cleanup_error_type"] = type(exc).__name__
                result["status"] = "failed"
        engine.dispose()
    if sys.argv[1] == "verify":
        target = root / ("AF012_B2_RESTART_VERIFY_" + uuid4().hex + ".json")
        target.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if result["temporary_rows_cleaned"]:
            try:
                path.unlink()
                result["checkpoint_cleaned"] = True
            except OSError:
                result["checkpoint_cleaned"] = False
                result["status"] = "failed"
        target.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        result["result_file"] = str(target)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] in ("prepared", "passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
