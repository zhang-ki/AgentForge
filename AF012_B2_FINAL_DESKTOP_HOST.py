"""Run the real Windows launcher twice with only a disposable conversation."""
import hashlib
import http.client
import ast
import json
import os
from pathlib import Path
import re
import subprocess
import textwrap
from uuid import uuid4


def main():
    if os.environ.get("CODEX_SANDBOX_NETWORK_DISABLED") == "1":
        print(json.dumps({"status": "blocked_by_environment"}))
        return 2
    from agentforge import cli
    from agentforge.af012_connection import create_local_engine
    from agentforge.conversation_store import ConversationStore
    from agentforge.scanner import scan_repository, read_candidate, ScanLimits
    from agentforge.store import IndexStore
    root = Path(__file__).resolve().parent
    protected = [root / ".agentforge/data/index.sqlite3", root / ".agentforge/data/read_only_evidence.json",
                 root / "AF011_CHAT_ACCEPTANCE_RESULTS.json"]
    def hashes():
        return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in protected}
    result = {"status": "failed", "stage": "prepare", "checks": {}, "temporary_rows_cleaned": False}
    before = hashes()
    engine = create_local_engine(os.environ.get("AGENTFORGE_DB_PASSWORD"))
    store = ConversationStore(engine)
    repo_id = title = cid = None
    previous = None
    try:
        connection = http.client.HTTPConnection("127.0.0.1", 56572, timeout=2)
        try:
            connection.request("GET", "/health")
            response = connection.getresponse()
            if response.status != 200 or json.loads(response.read(8192)).get("status") != "ok":
                raise RuntimeError("Local model service is not ready.")
        finally:
            connection.close()
        with IndexStore(readonly=True) as index:
            repository = index.metadata().get("repository")
            repo_id = cli._repository_id(repository)
            candidates = index.search_symbols(kind="function", limit=100)
            selected = next((row for row in candidates
                           if re.fullmatch(r"[A-Za-z_]\w{2,}", row["name"])
                           and row["end_line"] - row["start_line"] < 25
                           and len(index.search_symbols(name=row["name"], limit=2)) == 1), None)
            if selected is None:
                raise RuntimeError("No uniquely indexed short function is available.")
            symbol = selected["name"]
            scan = scan_repository(repository, ScanLimits())
            candidate = next((c for c in scan.candidates if c.relative_path == selected["relative_path"]), None)
            if not scan.complete or candidate is None:
                raise RuntimeError("Selected source cannot be safely inspected.")
            source = read_candidate(candidate, scan.root, ScanLimits().max_file_bytes).splitlines()
            tree = ast.parse(textwrap.dedent("\n".join(source[selected["start_line"] - 1:selected["end_line"]])))
            no_explicit_return = not any(isinstance(node, ast.Return) for node in ast.walk(tree))
        previous = store.current_conversation(repo_id)
        title = "AF012-DESKTOP-" + uuid4().hex
        first_question = f"What does function {symbol} do? Cite evidence."
        followup = "What does it return? Identify the function and cite evidence."
        resumed = "Which function were we discussing, and what does it return? Cite evidence."
        launcher = str(root / "Start-AgentForge.cmd")

        def run_desktop(lines):
            process = subprocess.Popen(["cmd.exe", "/d", "/c", launcher], cwd=root,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
            try:
                out, _ = process.communicate(("\r\n".join(lines) + "\r\n\r\n").encode("utf-8"), timeout=480)
            except subprocess.TimeoutExpired:
                if process.poll() is None:
                    subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                   capture_output=True, timeout=15, check=False)
                process.communicate(timeout=15)
                raise RuntimeError("Owned desktop process exceeded its time limit.")
            return process.returncode, out.decode("utf-8", errors="replace")

        result["stage"] = "first_launch"
        code, first_output = run_desktop(["/new " + title, first_question, followup, "/exit"])
        matches = [row for row in store.list_conversations(repo_id) if row["title"] == title]
        if len(matches) != 1:
            raise RuntimeError("The first launch did not produce exactly one test conversation.")
        cid = matches[0]["id"]
        before_rows = store.get_messages(cid, repo_id)
        result["checks"]["first_process_exited"] = code == 0
        result["checks"]["first_two_real_turns"] = (len(before_rows) == 4
            and [r["role"] for r in before_rows] == ["user", "assistant", "user", "assistant"]
            and all(r["model_name"] == "qwen3.5-4b" for r in before_rows if r["role"] == "assistant")
            and symbol.casefold() in before_rows[-1]["content"].casefold())
        result["checks"]["no_unrelated_citation_in_first_answer"] = (
            len(before_rows) >= 2 and not re.search(r"\[E(?:[2-9]|[1-9]\d+)\]", before_rows[1]["content"]))
        if no_explicit_return:
            result["checks"]["no_invented_tensor_return"] = (
                len(before_rows) >= 2 and not re.search(r"\breturns?\s+(?:a\s+)?tensor\b", before_rows[1]["content"], re.I))
        result["stage"] = "second_launch"
        code2, second_output = run_desktop(["/list", "/history", resumed, "/exit"])
        after_rows = store.get_messages(cid, repo_id)
        selected = store.current_conversation(repo_id)
        result["checks"].update({"second_process_exited": code2 == 0,
            "session_identity_preserved": bool(selected and selected["id"] == cid),
            "title_listed": title in second_output,
            "history_displayed": first_question in second_output,
            "message_order_preserved": [r["id"] for r in after_rows[:4]] == [r["id"] for r in before_rows],
            "no_duplicate_session": len([row for row in store.list_conversations(repo_id) if row["title"] == title]) == 1,
            "resumed_answer": len(after_rows) == 6 and symbol.casefold() in after_rows[-1]["content"].casefold()})
        result["messages"] = [{"role": row["role"], "content": row["content"], "sequence": row["sequence"]} for row in after_rows]
        result["symbol"] = symbol
        result["process_exit_codes"] = [code, code2]
        result["stage"] = "done"
        result["status"] = "passed" if all(result["checks"].values()) else "failed"
    except Exception as exc:
        result["error_type"] = type(exc).__name__
        if isinstance(exc, RuntimeError):
            result["error"] = str(exc)
    finally:
        if repo_id is not None and title:
            try:
                matches = [row for row in store.list_conversations(repo_id) if row["title"] == title]
                current = store.current_conversation(repo_id)
                if current and any(current["id"] == item["id"] for item in matches) and previous:
                    store.switch_conversation(previous["id"], repo_id)
                for item in matches:
                    store.delete_conversation(item["id"], repo_id)
                result["temporary_rows_cleaned"] = not any(row["title"] == title for row in store.list_conversations(repo_id))
            except Exception as exc:
                result["status"] = "failed"
                result["cleanup_error_type"] = type(exc).__name__
        engine.dispose()
        result["protected_unchanged"] = before == hashes()
        if not result["protected_unchanged"]:
            result["status"] = "failed"
    path = root / ("AF012_B2_FINAL_DESKTOP_RESULT_" + uuid4().hex + ".json")
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"result_file": str(path), "status": result["status"], "stage": result["stage"],
                      "checks": result["checks"], "error_type": result.get("error_type"),
                      "temporary_rows_cleaned": result["temporary_rows_cleaned"],
                      "protected_unchanged": result["protected_unchanged"]}, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
