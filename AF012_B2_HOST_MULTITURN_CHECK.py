"""Explicit host-only multi-turn acceptance on a disposable synthetic repository."""
from contextlib import contextmanager, redirect_stderr, redirect_stdout
import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from unittest.mock import patch
from uuid import uuid4


def main():
    if os.environ.get("CODEX_SANDBOX_NETWORK_DISABLED") == "1":
        print(json.dumps({"status": "blocked_by_environment", "chat_requests": 0}))
        return 2
    from agentforge import cli
    from agentforge.af012_connection import create_local_engine
    from agentforge.conversation_store import ConversationStore
    from agentforge.indexer import index_repository
    from agentforge.providers.llama_server import LlamaServerClient
    from agentforge.store import IndexStore

    result = {"status": "failed", "checks": {}, "cases": {}, "chat_requests": 0,
              "temporary_rows_cleaned": False, "temporary_repository_cleaned": False,
              "service_restart": "not_measured"}
    root = Path(__file__).resolve().parent
    temp_parent = root / "tests" / ".tmp"
    temp_parent.mkdir(exist_ok=True)
    temporary = None
    engine = None
    index = None
    ids = []
    repo_id = None
    stage = "health"
    recorded_calls = []
    try:
        conn = http.client.HTTPConnection("127.0.0.1", 56572, timeout=2)
        try:
            conn.request("GET", "/health")
            response = conn.getresponse()
            body = response.read(8192)
            if response.status != 200 or json.loads(body).get("status") != "ok":
                raise RuntimeError("Local model health did not report ready.")
        finally:
            conn.close()
        result["checks"]["health_ready"] = "passed"
        stage = "prepare"
        temporary = tempfile.TemporaryDirectory(prefix="af012-b2-multiturn-", dir=temp_parent)
        repository = Path(temporary.name)
        (repository / "example.py").write_text(
            'class Conv2d:\n'
            '    """Store a 2D convolution configuration."""\n'
            '    def __init__(self, in_channels, kernel_size):\n'
            '        self.in_channels = in_channels\n'
            '        self.kernel_size = kernel_size\n\n'
            'def add_numbers(left, right):\n'
            '    """Return the sum of two numbers."""\n'
            '    return left + right\n', encoding="utf-8")
        repo_id = cli._repository_id(str(repository))
        index = IndexStore(":memory:")
        index_repository(repository, index)
        engine = create_local_engine(os.environ.get("AGENTFORGE_DB_PASSWORD"))
        store = ConversationStore(engine)
        first = store.create_conversation(repo_id, "AF012 disposable first")
        ids.append(first["id"])
        second = store.create_conversation(repo_id, "AF012 disposable second")
        ids.append(second["id"])
        store.switch_conversation(first["id"], repo_id)
        result["checks"]["created_two_sessions"] = "passed"

        @contextmanager
        def isolated_index(*_args, **_kwargs):
            yield index

        original_chat = LlamaServerClient.chat
        def record_chat(client, messages, *, model):
            recorded_calls.append(messages)
            result["chat_requests"] += 1
            try:
                return original_chat(client, messages, model=model)
            finally:
                result.setdefault("request_stats", []).append(client.request_stats())

        def ask(question, cid):
            output, errors = io.StringIO(), io.StringIO()
            with redirect_stdout(output), redirect_stderr(errors):
                code = cli.main(["ask", question, "--conversation-id", cid,
                    "--provider", "llama-server", "--base-url", "http://127.0.0.1:56572/v1/chat/completions",
                    "--model", "qwen3.5-4b", "--read-timeout", "120"])
            if code != 0:
                raise RuntimeError("Ask failed: " + errors.getvalue()[:300])
            answer = json.loads(output.getvalue())
            result["cases"][stage] = answer
            if answer.get("provider") != "llama-server" or not answer.get("answer"):
                raise RuntimeError("Ask did not return a real model answer.")
            if answer["answer_contract"]["status"] == "refused" or not answer["answer_contract"]["claims"]:
                raise RuntimeError("Answer contract refused the result.")
            return answer

        with patch("agentforge.cli.IndexStore", side_effect=isolated_index), patch.object(LlamaServerClient, "chat", record_chat):
            stage = "A_first_question"
            first_answer = ask("Conv2d 的作用是什么？", first["id"])
            result["checks"]["A_real_answer_and_persistence"] = (
                "passed" if len(store.get_messages(first["id"], repo_id)) == 2 else "failed")
            result["checks"]["A_configuration_semantics"] = (
                "passed" if any(word in first_answer["answer"].lower() for word in ("配置", "参数", "config", "parameter"))
                and "[E1]" in first_answer["answer"] else "failed")
            stage = "B_pronoun_followup"
            second_answer = ask("它的第二个构造参数（不计 self）是什么？", first["id"])
            previous_texts = [m["content"] for m in recorded_calls[-1][:-1]]
            result["checks"]["B_history_supplied"] = (
                "passed" if "Conv2d 的作用是什么？" in previous_texts else "failed")
            result["checks"]["B_semantic_kernel_size"] = (
                "passed" if "kernel_size" in second_answer["answer"]
                and "[E1]" in second_answer["answer"] else "failed")
            stage = "C_other_conversation"
            store.switch_conversation(second["id"], repo_id)
            ask("add_numbers", second["id"])
            other_text = "\n".join(m["content"] for m in recorded_calls[-1])
            result["checks"]["C_session_isolation"] = (
                "passed" if "它的第二个构造参数" not in other_text else "failed")
            stage = "D_switch_back"
            store.switch_conversation(first["id"], repo_id)
            ask("它第二个构造参数叫什么？", first["id"])
            history_text = "\n".join(m["content"] for m in recorded_calls[-1][:-1])
            result["checks"]["D_switch_history"] = (
                "passed" if "它的第二个构造参数" in history_text else "failed")
        stage = "E_process_restart"
        child_code = ("import os,sys; from agentforge.af012_connection import create_local_engine; "
                      "from agentforge.conversation_store import ConversationStore; "
                      "e=create_local_engine(os.environ.get('AGENTFORGE_DB_PASSWORD')); "
                      "s=ConversationStore(e); c=s.current_conversation(sys.argv[1]); "
                      "ok=c and c['id']==sys.argv[2] and len(s.get_messages(sys.argv[2],sys.argv[1]))==6; "
                      "e.dispose(); sys.exit(0 if ok else 1)")
        child = subprocess.run([sys.executable, "-X", "utf8", "-B", "-c", child_code, repo_id, first["id"]],
                               cwd=root, capture_output=True, timeout=20, check=False)
        result["checks"]["E_process_restart"] = "passed" if child.returncode == 0 else "failed"
        stage = "F_history_budget"
        for i in range(7):
            store.append_turn(first["id"], repo_id, f"synthetic question {i}", f"synthetic answer {i}")
        remaining, summary = store.prepare_history(first["id"], repo_id)
        result["checks"]["F_summary_window"] = "passed" if summary and len(remaining) == 6 else "failed"
        result["status"] = "passed" if all(x == "passed" for x in result["checks"].values()) else "failed"
    except Exception as exc:
        result["error_stage"] = stage
        result["error_type"] = type(exc).__name__
        if isinstance(exc, RuntimeError):
            result["error"] = str(exc)[:300]
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
            name = Path(temporary.name)
            temporary.cleanup()
            result["temporary_repository_cleaned"] = not name.exists()
        try:
            temp_parent.rmdir()
        except OSError:
            pass
    target = root / ("AF012_B2_MULTITURN_VERIFY_" + uuid4().hex + ".json")
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"result_file": str(target), "status": result["status"], "checks": result["checks"],
                      "chat_requests": result["chat_requests"], "error_stage": result.get("error_stage"),
                      "error_type": result.get("error_type"), "temporary_rows_cleaned": result["temporary_rows_cleaned"],
                      "temporary_repository_cleaned": result["temporary_repository_cleaned"]}, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
