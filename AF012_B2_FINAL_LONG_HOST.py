"""Host-only real long-history experiment; does not start services or touch the RAG index."""
from contextlib import contextmanager, redirect_stdout, redirect_stderr
import http.client
import io
import json
import os
from pathlib import Path
import tempfile
from unittest.mock import patch
from uuid import uuid4


def main():
    if os.environ.get("CODEX_SANDBOX_NETWORK_DISABLED") == "1":
        print(json.dumps({"status": "blocked_by_environment", "model_requests": 0}))
        return 2
    from agentforge import cli
    from agentforge.af012_connection import create_local_engine
    from agentforge.conversation_store import ConversationStore
    from agentforge.indexer import index_repository
    from agentforge.providers.llama_server import LlamaServerClient
    from agentforge.store import IndexStore

    result = {"status": "failed", "stage": "health", "cases": {}, "model_requests": 0,
              "temporary_rows_cleaned": False, "temporary_repository_cleaned": False}
    root = Path(__file__).resolve().parent
    parent = root / "tests" / ".tmp"
    parent.mkdir(exist_ok=True)
    engine = index = temporary = None
    ids, repo_id, calls = [], None, []
    try:
        connection = http.client.HTTPConnection("127.0.0.1", 56572, timeout=2)
        try:
            connection.request("GET", "/health")
            response = connection.getresponse()
            body = response.read(8192)
            if response.status != 200 or json.loads(body).get("status") != "ok":
                raise RuntimeError("model_not_ready")
        finally:
            connection.close()
        result["stage"] = "prepare"
        temporary = tempfile.TemporaryDirectory(prefix="af012-final-long-", dir=parent)
        repository = Path(temporary.name)
        (repository / "example.py").write_text('def memory_anchor():\n    """Return a local marker."""\n    return "local"\n', encoding="utf-8")
        repo_id = cli._repository_id(str(repository))
        index = IndexStore(":memory:")
        index_repository(repository, index)
        engine = create_local_engine(os.environ.get("AGENTFORGE_DB_PASSWORD"))
        store = ConversationStore(engine)
        first = store.create_conversation(repo_id, "AF012 synthetic long history")
        ids.append(first["id"])
        other = store.create_conversation(repo_id, "AF012 synthetic isolated history")
        ids.append(other["id"])
        for i in range(20):
            fact = {0: "earlier project code ALPHA731", 10: "middle project code BETA842", 19: "recent project code GAMMA953"}.get(i, f"discussion step {i}")
            store.append_turn(first["id"], repo_id, f"For this conversation remember: {fact}.",
                              f"You said: {fact}.")
        remaining, summary = store.prepare_history(first["id"], repo_id)
        result["summary"] = summary
        result["summary_checks"] = {"persisted": bool(summary), "earlier_present": "ALPHA731" in summary["content"],
                                    "middle_present": "BETA842" in summary["content"],
                                    "recent_in_window": any("GAMMA953" in r["content"] for r in remaining)}

        @contextmanager
        def isolated_index(*_args, **_kwargs):
            yield index
        original_chat = LlamaServerClient.chat
        def record_chat(client, messages, *, model):
            result["model_requests"] += 1
            try:
                return original_chat(client, messages, model=model)
            finally:
                calls.append({"messages": messages, "stats": client.request_stats()})

        cases = [("early", first["id"], "function memory_anchor: describe the code with [E1], and state the earlier project code I provided in this conversation." , "ALPHA731"),
                 ("middle", first["id"], "function memory_anchor: describe the code with [E1], and state the middle project code I provided in this conversation.", "BETA842"),
                 ("recent", first["id"], "function memory_anchor: describe the code with [E1], and state the most recent project code I provided in this conversation.", "GAMMA953"),
                 ("reference", first["id"], "function memory_anchor: describe the code with [E1]; what was it that I first mentioned as the earlier project code?", "ALPHA731"),
                 ("isolation", other["id"], "function memory_anchor: describe the code with [E1]. Did I give a project code in this conversation?", None)]
        with patch("agentforge.cli.IndexStore", side_effect=isolated_index), patch.object(LlamaServerClient, "chat", record_chat):
            for name, cid, question, expected in cases:
                result["stage"] = name
                output, errors = io.StringIO(), io.StringIO()
                with redirect_stdout(output), redirect_stderr(errors):
                    code = cli.main(["ask", question, "--conversation-id", cid,
                        "--provider", "llama-server", "--base-url", "http://127.0.0.1:56572/v1/chat/completions",
                        "--model", "qwen3.5-4b", "--read-timeout", "120"])
                if code:
                    result["cases"][name] = {"status": "failed", "error": errors.getvalue()[:300]}
                    break
                data = json.loads(output.getvalue())
                text = data.get("answer") or data.get("refused_output") or ""
                passed = (expected in text) if expected else not any(x in text for x in ("ALPHA731", "BETA842", "GAMMA953"))
                result["cases"][name] = {"status": "passed" if passed and data.get("provider") == "llama-server" else "failed",
                                          "response": data, "model_input": calls[-1] if calls else None}
            if len(result["cases"]) == len(cases) and all(c["status"] == "passed" for c in result["cases"].values()):
                previous_covered = store.latest_summary(first["id"], repo_id)["covered_through"]
                for i in range(7):
                    store.append_turn(first["id"], repo_id, f"followup topic {i}", f"acknowledged {i}")
                _, updated = store.prepare_history(first["id"], repo_id)
                result["summary_checks"]["updated_persistently"] = updated["covered_through"] > previous_covered
                result["stage"] = "after_summary_update"
                output, errors = io.StringIO(), io.StringIO()
                with redirect_stdout(output), redirect_stderr(errors):
                    code = cli.main(["ask", "function memory_anchor: describe it with [E1] and recall the first project code I provided.",
                        "--conversation-id", first["id"], "--provider", "llama-server",
                        "--base-url", "http://127.0.0.1:56572/v1/chat/completions", "--model", "qwen3.5-4b", "--read-timeout", "120"])
                if code:
                    result["cases"]["after_summary_update"] = {"status": "failed", "error": errors.getvalue()[:300]}
                else:
                    data = json.loads(output.getvalue())
                    answer = data.get("answer") or ""
                    result["cases"]["after_summary_update"] = {
                        "status": "passed" if data.get("provider") == "llama-server" and "ALPHA731" in answer else "failed",
                        "response": data, "model_input": calls[-1] if calls else None}
        result["stage"] = "done"
        # Semantic provenance is not independently validated for prior user statements.
        result["history_claim_provenance"] = "NOT_MEASURED"
        passed_cases = len(result["cases"]) == len(cases) + 1 and all(
            case["status"] == "passed" for case in result["cases"].values())
        result["status"] = "partial" if all(result["summary_checks"].values()) and passed_cases else "failed"
    except Exception as exc:
        result["error_type"] = type(exc).__name__
    finally:
        if engine is not None and repo_id is not None:
            try:
                store = ConversationStore(engine)
                for cid in reversed(ids):
                    store.delete_conversation(cid, repo_id)
                result["temporary_rows_cleaned"] = not store.list_conversations(repo_id)
            except Exception as exc:
                result["cleanup_error_type"] = type(exc).__name__
                result["status"] = "failed"
            engine.dispose()
        if index is not None:
            index.close()
        if temporary is not None:
            path = Path(temporary.name)
            temporary.cleanup()
            result["temporary_repository_cleaned"] = not path.exists()
        try:
            parent.rmdir()
        except OSError:
            pass
    path = root / ("AF012_B2_FINAL_LONG_RESULT_" + uuid4().hex + ".json")
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"result_file": str(path), "status": result["status"], "stage": result["stage"],
                      "cases": {key: value["status"] for key, value in result["cases"].items()},
                      "model_requests": result["model_requests"], "temporary_rows_cleaned": result["temporary_rows_cleaned"],
                      "temporary_repository_cleaned": result["temporary_repository_cleaned"]}, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "partial" else 1


if __name__ == "__main__":
    raise SystemExit(main())
