"""Bounded 1.1 host acceptance using real providers and isolated stores.

Desktop process and PostgreSQL service restarts remain explicit unmeasured gates.
The runner never substitutes a fake model response and never retries a request.
"""
from contextlib import contextmanager, redirect_stdout, redirect_stderr
import ast
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from unittest.mock import patch

import AF011_1_JOINT_HOST as joint

CHAT_LIMIT = 60
EMBED_LIMIT = 300
TOTAL_SECONDS = 2700


def _file_snapshot(root, *, deadline, verify_expected=True):
    from AF012_MVP_HOST_ACCEPTANCE import EXPECTED_ASSETS, MODEL_ROOT
    required = [root / ".agentforge" / "data" / "index.sqlite3",
                root / ".agentforge" / "data" / "read_only_evidence.json"]
    required.extend(Path(path) for path in EXPECTED_ASSETS)
    for path in required:
        if not path.is_file() or path.is_symlink():
            raise joint.HostBlocked("PROTECTED_FILE_OR_MODEL_ASSET_MISSING")
    paths = set(required)
    for directory in (root / "tests" / "fixtures", root / ".agentforge" / "data" / "chroma"):
        if directory.exists():
            paths.update(path for path in directory.rglob("*") if path.is_file() and not path.is_symlink())
    paths.update(path for path in root.iterdir() if path.is_file() and
                 path.name.startswith(("AF009", "AF010", "AF011", "AF012")) and
                 path.suffix.lower() in (".json", ".md") and
                 not path.name.startswith("AF011_1_FULL_HOST_RESULT_"))
    runtime = MODEL_ROOT / "qwen3.5-4b" / ".runtime" / "llama-b11424"
    if runtime.exists():
        paths.update(path for path in runtime.rglob("*") if path.is_file() and
                     not path.is_symlink() and path.suffix.lower() in (".exe", ".dll"))
    items = {}
    for path in sorted(paths, key=lambda item: str(item).casefold()):
        if time.monotonic() > deadline:
            raise joint.HostBlocked("INTEGRITY_TIME_BUDGET_EXCEEDED")
        try:
            relative = "model_assets/" + path.relative_to(MODEL_ROOT).as_posix()
        except ValueError:
            relative = path.relative_to(root).as_posix()
        try:
            before = path.stat()
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
                    if time.monotonic() > deadline:
                        raise joint.HostBlocked("INTEGRITY_TIME_BUDGET_EXCEEDED")
                    digest.update(block)
            after = path.stat()
            if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
                raise joint.HostBlocked("PROTECTED_FILE_CHANGED_DURING_HASH")
            items[relative] = {"bytes": after.st_size, "sha256": digest.hexdigest()}
        except OSError:
            raise joint.HostBlocked("PROTECTED_FILE_UNREADABLE") from None
    for filename, expected in EXPECTED_ASSETS.items():
        key = "model_assets/" + Path(filename).relative_to(MODEL_ROOT).as_posix()
        if verify_expected and items[key]["sha256"].casefold() != expected.casefold():
            raise joint.HostBlocked("MODEL_ASSET_HASH_MISMATCH")
    return items

# Fixed before retrieval. Each source string must appear in the named file.
CASES = (
    {"id": "function", "question": "What does function shared return for value and amount?", "sources": {"shared.py": "return value + amount"}, "any": ("add", "sum", "+", "plus"), "forbidden": ("subtract", "minus", "减去")},
    {"id": "class_method", "question": "What does method size in class Packet return?", "sources": {"shared.py": "return len(self.value)"}, "any": ("length", "len(", "number of", "size")},
    {"id": "parameters", "question": "What parameters does function shared take?", "sources": {"shared.py": "def shared(value, amount):"}, "all": ("value", "amount")},
    {"id": "return_value", "question": "What does function start_job return?", "sources": {"pkg/impl.py": "return 'started:' + item"}, "all": ("started", "item")},
    {"id": "cross_file_call", "question": "Which caller calls launch_job and where is its implementation?", "sources": {"app.py": "return launch_job(item)", "pkg/impl.py": "def start_job(item):"}, "all": ("run_queue", "start_job")},
    {"id": "configuration", "question": "Where does function max_attempts get RETRY_LIMIT and what is its value?", "sources": {"worker.py": "return RETRY_LIMIT", "settings.py": "RETRY_LIMIT = 3"}, "additional_sources": (("worker.py", "from settings import RETRY_LIMIT"),), "all": ("3", "retry")},
    {"id": "chinese", "question": "函数 start_job 返回什么？", "sources": {"pkg/impl.py": "return 'started:' + item"}, "all": ("started", "item")},
    {"id": "english", "question": "What does function shared_backup return?", "sources": {"shared.py": "return value - amount"}, "any": ("subtract", "difference", "minus", "-"), "forbidden": ("addition", "adds", "sum of")},
    {"id": "mixed_similar", "question": "函数 shared_backup 与 shared 不同在哪里？", "sources": {"shared.py": "def shared_backup(value, amount):"}, "any": ("subtract", "减", "minus", "-")},
    {"id": "missing", "question": "What does function nonexistent_zephyr_941 return?", "sources": {}, "refuse": True, "absent_symbol": "nonexistent_zephyr_941"},
    {"id": "history_conflict", "question": "Earlier I claimed shared subtracts. What does function shared return in the current code?", "sources": {"shared.py": "return value + amount"}, "any": ("add", "sum", "+", "plus")},
)


def _write_fixture(repo, *, beta=False):
    (repo / "pkg").mkdir(exist_ok=True)
    operation = "-" if beta else "+"
    (repo / "shared.py").write_text(
        f"def shared(value, amount):\n    return value {operation} amount  # {'beta' if beta else 'alpha'}\n\n"
        "def shared_backup(value, amount):\n    return value - amount\n\n"
        "class Packet:\n    def __init__(self, value):\n        self.value = value\n"
        "    def size(self):\n        return len(self.value)\n", encoding="utf-8")
    (repo / "pkg" / "__init__.py").write_text("from .impl import start_job as launch_job\n__all__ = ['launch_job']\n", encoding="utf-8")
    (repo / "pkg" / "impl.py").write_text("def start_job(item):\n    return 'started:' + item\n", encoding="utf-8")
    (repo / "app.py").write_text("from pkg import launch_job\ndef run_queue(item):\n    return launch_job(item)\n", encoding="utf-8")
    (repo / "settings.py").write_text("RETRY_LIMIT = 3\n", encoding="utf-8")
    (repo / "worker.py").write_text("from settings import RETRY_LIMIT\ndef max_attempts():\n    return RETRY_LIMIT\n", encoding="utf-8")


def _verify_fixture(repo):
    manifest = {}
    declared = set()
    for source in repo.rglob("*.py"):
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        declared.update(node.name for node in ast.walk(tree)
                        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)))
    for case in CASES:
        manifest[case["id"]] = []
        if case.get("absent_symbol") in declared:
            raise joint.CheckFailure("fixture_negative_symbol_exists")
        for relative, expected in (*case["sources"].items(), *case.get("additional_sources", ())):
            path = (repo / relative).resolve(strict=True)
            if not path.is_relative_to(repo.resolve()):
                raise joint.CheckFailure("fixture_path_escape")
            lines = path.read_text(encoding="utf-8").splitlines()
            matches = [i for i, line in enumerate(lines, 1) if expected in line]
            if len(matches) != 1:
                raise joint.CheckFailure("fixture_evidence_mismatch")
            manifest[case["id"]].append({"path": relative, "line": matches[0], "expected": expected})
    return manifest


def _audit_chat_prompt(messages):
    """Return bounded IDs and marker booleans, never the full prompt text."""
    content = "\n".join(str(item.get("content", "")) for item in messages
                        if isinstance(item, dict))
    fact_ids = set()
    fact_message_ids = set()
    fact_header = "Untrusted source-linked conversation facts; context only, not source evidence:"
    for item in messages:
        if not isinstance(item, dict):
            continue
        body = str(item.get("content", ""))
        if fact_header not in body:
            continue
        fact_ids.update(re.findall(r"\[fact_id=([0-9a-f]{24});", body))
        for match in re.finditer(r"\[fact_id=[0-9a-f]{24};([^\]\r\n]+)\]", body):
            fact_message_ids.update(re.findall(
                r"\bmessage_id=([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})\b",
                match.group(1), re.IGNORECASE,
            ))
    evidence_text = "\n".join(
        str(item.get("content", "")) for item in messages if isinstance(item, dict)
        and ("Untrusted current evidence:" in str(item.get("content", ""))
             or "Untrusted evidence:" in str(item.get("content", "")))
    )
    return {
        "synthetic_markers": {token: token in content
                              for token in ("ALPHA731", "BETA842", "GAMMA953")},
        "context_fact_ids_in_prompt": sorted(fact_ids),
        "context_fact_source_message_ids_in_prompt": sorted(fact_message_ids),
        "evidence_source_ids_in_prompt": sorted(set(re.findall(r"\[(E[1-9]\d*)\]", evidence_text))),
        "message_count": len(messages),
        "prompt_utf8_bytes": len(content.encode("utf-8")),
    }


def _evaluate_case(case, response, repo, manifest):
    from agentforge.evidence_roles import required_evidence_roles
    contract = response.get("answer_contract") or {}
    answer = response.get("answer") or ""
    evidence = contract.get("evidence") or []
    checks = {"source_identity": True, "cited_expected_paths": False,
              "fact_assertions": False, "refusal": False,
              "required_evidence_role_coverage": False}
    cited = set(re.findall(r"\[E[1-9][0-9]*\]", answer))
    cited_ranges = []
    available_roles = set()
    cited_roles = set()
    for item in evidence:
        try:
            path = (repo / item["relative_path"]).resolve(strict=True)
            if not path.is_relative_to(repo.resolve()) or not path.is_file():
                raise ValueError("evidence_outside_fixture")
            lines = path.read_text(encoding="utf-8").splitlines()
            start, end = item["start_line"], item["end_line"]
            if (isinstance(start, bool) or not isinstance(start, int) or isinstance(end, bool)
                    or not isinstance(end, int) or start < 1 or end < start
                    or end > len(lines) or "\n".join(lines[start - 1:end]) != item["excerpt"].rstrip("\n")):
                checks["source_identity"] = False
            if "[" + item["source_id"] + "]" in cited:
                cited_ranges.append((item["relative_path"], start, end))
                cited_roles.update(item.get("evidence_roles") or ())
            available_roles.update(item.get("evidence_roles") or ())
        except (OSError, ValueError, KeyError, TypeError):
            checks["source_identity"] = False
    if case.get("refuse"):
        checks["refusal"] = contract.get("status") == "refused" and not answer
        checks["required_evidence_role_coverage"] = True
        status = "passed" if checks["refusal"] else "failed"
    else:
        required_roles = required_evidence_roles(case["question"])
        checks["required_evidence_role_coverage"] = all(
            role in available_roles and role in cited_roles for role in required_roles
        )
        checks["cited_expected_paths"] = all(any(path == expected["path"] and start <= expected["line"] <= end
            for path, start, end in cited_ranges) for expected in manifest[case["id"]])
        folded = answer.casefold()
        checks["fact_assertions"] = (bool(answer) and
            all(word.casefold() in folded for word in case.get("all", ())) and
            (not case.get("any") or any(word.casefold() in folded for word in case["any"])) and
            not any(word.casefold() in folded for word in case.get("forbidden", ())))
        status = "passed" if all((checks["source_identity"], checks["cited_expected_paths"],
                                  checks["fact_assertions"], checks["required_evidence_role_coverage"],
                                  response.get("provider") == "llama-server")) else "failed"
    return {"id": case["id"], "status": status, "checks": checks,
            "expected_evidence": manifest[case["id"]], "response": response,
            "evidence_role_audit": {
                "required_roles": required_evidence_roles(case["question"]),
                "available_roles": tuple(sorted(available_roles)),
                "cited_roles": tuple(sorted(cited_roles)),
            },
            "semantic_scope": "fixed assertions only; arbitrary extra claims require independent review"}


_CLI_DIAGNOSTIC_LIMIT = 1_200


def _sanitize_cli_diagnostic(value):
    """Keep a bounded CLI error detail while removing common credential forms."""
    text = str(value or "")
    text = re.sub(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,;]+",
                  r"\1[REDACTED]", text)
    text = re.sub(r"(?i)((?:api[_ -]?key|password|passwd|secret|token)\s*[:=]\s*)"
                  r"(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)",
                  r"\1[REDACTED]", text)
    text = re.sub(r"(?i)(://[^:/\s]+:)[^@/\s]+(@)", r"\1[REDACTED]\2", text)
    text = re.sub(r"(?im)(-----BEGIN [^-]*PRIVATE KEY-----).*?(-----END [^-]*PRIVATE KEY-----)",
                  r"\1[REDACTED]\2", text, flags=re.DOTALL)
    text = "".join(char for char in text if char in "\n\t" or ord(char) >= 32)
    return text[:_CLI_DIAGNOSTIC_LIMIT], len(text) > _CLI_DIAGNOSTIC_LIMIT


def _record_cli_failure(result, *, stage, operation, exit_code, stderr, stdout,
                        failure_kind):
    if result is None:
        return
    diagnostic, truncated = _sanitize_cli_diagnostic(stderr)
    result["cli_failure_diagnostic"] = {
        "stage": stage,
        "operation": operation,
        "failure_kind": failure_kind,
        "exit_code": exit_code,
        "stdout_chars": len(stdout or ""),
        "stderr_chars": len(stderr or ""),
        "stderr_truncated": truncated,
        "stderr_redacted": diagnostic,
    }
    marker = "AF_PROVIDER_DIAGNOSTIC="
    for line in (stderr or "").splitlines():
        if not line.startswith(marker):
            continue
        try:
            parsed = json.loads(line[len(marker):])
        except (ValueError, TypeError):
            break
        if isinstance(parsed, dict):
            allowed = {
                "attempted", "sent", "succeeded", "failed", "retries",
                "last_http_status", "last_failure_phase", "last_error_code",
                "last_application_failure_phase", "last_application_error_code",
                "citation", "response",
            }
            result["cli_failure_diagnostic"]["provider_diagnostic"] = {
                key: parsed[key] for key in allowed if key in parsed
            }
        break


def _invoke(cli, args, *, result=None, operation="ask"):
    out, err = io.StringIO(), io.StringIO()
    try:
        with redirect_stdout(out), redirect_stderr(err):
            try:
                code = cli.main(args)
            except SystemExit as exc:
                _record_cli_failure(result, stage=(result or {}).get("stage"),
                    operation=operation, exit_code=exc.code, stderr=err.getvalue(),
                    stdout=out.getvalue(), failure_kind="argument_parser_exit")
                raise joint.CheckFailure("ask_argument_exit_" + str(exc.code)) from None
            except Exception as exc:
                safe_message, truncated = _sanitize_cli_diagnostic(str(exc))
                if result is not None:
                    result["cli_failure_diagnostic"] = {
                        "stage": result.get("stage"), "operation": operation,
                        "failure_kind": "uncaught_cli_exception",
                        "exception_type": type(exc).__name__,
                        "exception_message_redacted": safe_message,
                        "message_truncated": truncated,
                        "stdout_chars": len(out.getvalue()),
                        "stderr_chars": len(err.getvalue()),
                    }
                raise
    except joint.CheckFailure:
        raise
    if code:
        _record_cli_failure(result, stage=(result or {}).get("stage"),
            operation=operation, exit_code=code, stderr=err.getvalue(),
            stdout=out.getvalue(),
            failure_kind=("cli_value_error" if code == 2 else "cli_nonzero_exit"))
        raise joint.CheckFailure("ask_exit_" + str(code))
    try:
        return json.loads(out.getvalue())
    except json.JSONDecodeError:
        _record_cli_failure(result, stage=(result or {}).get("stage"),
            operation=operation, exit_code=code, stderr=err.getvalue(),
            stdout=out.getvalue(), failure_kind="invalid_cli_json")
        raise joint.CheckFailure("ask_output_invalid_json") from None


def _request_purpose(messages):
    """Classify request purpose without recording prompt contents."""
    if any(isinstance(item, dict) and
           "Summarize prior conversation messages for continuity" in str(item.get("content", ""))
           for item in messages):
        return "semantic_summary"
    return "answer_generation"


def _build_summary_audit(summary, persisted_messages):
    """Build a summary audit from the loaded conversation rows only.

    This is intentionally independent of variables used later by the Chroma
    lifecycle checks, so the audit can run immediately after history loading.
    """
    content = summary.get("content", "") if summary else ""
    return {
        "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "content_bytes": len(content.encode("utf-8")),
        "covered_message_id": summary.get("covered_message_id") if summary else None,
        "persisted_message_count": len(persisted_messages),
        "covered_through": summary.get("covered_through") if summary else 0,
        "fact_sources": [
            {"fact_id": fact["fact_id"], "kind": fact["kind"],
             "sources": [{"message_id": source["message_id"],
                          "sequence": source["sequence"],
                          "source_sha256": source["source_sha256"]}
                         for source in fact["sources"]]}
            for fact in (summary.get("fact_ledger", []) if summary else [])
        ],
        "history_origin": "synthetic_seeded_turns_not_real_model_turns",
    }


def run_extended(resources, result):
    from agentforge import cli
    from agentforge.indexer import index_repository
    from agentforge.embeddings.chroma_index import ChromaCodeIndex
    from agentforge.conversation_store import ConversationStore
    engine, indexes, repos = resources["engine"], resources["indexes"], resources["repos"]
    provider, collection, client = resources["provider"], resources["collection"], resources["client"]
    result.setdefault("stage_results", {})
    _start_stage(result, "fixture_and_incremental_index")
    for repo, beta, index in zip(repos, (False, True), indexes):
        _write_fixture(repo, beta=beta)
        index_repository(repo, index)
        ChromaCodeIndex(index, collection, provider).sync()
    manifest = _verify_fixture(repos[0])
    result["fixture_manifest"] = manifest
    for index, expected in zip(indexes, ("value + amount", "value - amount")):
        found = ChromaCodeIndex(index, collection, provider).search("shared value amount", top_k=20)
        matches = [item for item in found if item.relative_path == "shared.py"]
        if not matches or expected not in matches[0].text:
            raise joint.CheckFailure("incremental_repository_vector_mismatch")
    result["checks"]["real_embedding_incremental_two_repositories"] = "passed"
    _start_stage(result, "thirty_real_chat_turns")
    conversation = resources["conversations"].create_conversation(resources["ids"][0], "1.1 actual chat history")
    turns = []
    last_turn_response = {}
    intermediate_summary = None
    result["real_turns"] = turns
    @contextmanager
    def opened(**_kwargs):
        yield indexes[0]
    args = ["--conversation-id", conversation["id"], "--provider", "llama-server",
            "--base-url", "http://127.0.0.1:56572/v1/chat/completions", "--model", "qwen3.5-4b",
            "--read-timeout", "120", "--total-timeout", "180", "--max-output-tokens", "256", "--use-chroma"]
    with (patch("agentforge.cli._conversation_engine", return_value=engine),
          patch("agentforge.cli._open_current_index", side_effect=opened),
          patch("agentforge.af012_runtime.create_persistent_chroma", return_value=client),
          patch("agentforge.af012_runtime.collection_for_model", return_value=collection),
          patch("agentforge.af012_runtime.embedding_provider_from_environment", return_value=provider)):
        for number in range(30):
            fact = {0: "Project fact ALPHA731.", 3: "Original project color RED.",
                    12: "Decision BETA842: use local storage.",
                    20: "Correction: the earlier color RED is now BLUE.",
                    28: "Open task GAMMA953."}.get(number, "Progress step %d." % number)
            question = fact + " What does function shared return? Cite its evidence."
            response = _invoke(cli, ["ask", question, *args], result=result,
                               operation="real_chat_turn_%02d" % (number + 1))
            last_turn_response = response
            answer_text = response.get("answer") or ""
            turns.append({
                "number": number + 1,
                "provider": response.get("provider"),
                "answer_chars": len(answer_text),
                "answer_sha256": hashlib.sha256(answer_text.encode("utf-8")).hexdigest(),
                "answer_contract_status": (response.get("answer_contract") or {}).get("status"),
                "citation_count": len(re.findall(r"\[E[1-9][0-9]*\]", answer_text)),
            })
            if response.get("provider") != "llama-server" or not response.get("answer"):
                raise joint.CheckFailure("real_chat_turn_not_completed")
            if number == 14:
                intermediate_summary = ConversationStore(engine).latest_summary(conversation["id"], resources["ids"][0])
            print("Completed real chat turn %d/30" % (number + 1), flush=True)
        persisted = ConversationStore(engine).get_messages(conversation["id"], resources["ids"][0])
        summary = ConversationStore(engine).latest_summary(conversation["id"], resources["ids"][0])
        from agentforge.conversation_facts import extract_message_facts
        recent_marker_row = next((row for row in persisted
                                  if row["role"] == "user" and "GAMMA953" in row["content"]), None)
        recent_marker_fact = next((fact for fact in extract_message_facts(
            [recent_marker_row] if recent_marker_row else [])
            if "GAMMA953" in fact["text"]), None)
        context_audit = last_turn_response.get("conversation_context") or {}
        last_request_audit = (resources["observations"]["chat"][-1]
                              if resources["observations"]["chat"] else {})
        expected_fact_ids = set(context_audit.get("protected_fact_ids", ()))
        expected_fact_sources = set(context_audit.get("protected_fact_source_message_ids", ()))
        observed_fact_ids = set(last_request_audit.get("context_fact_ids_in_prompt", ()))
        observed_fact_sources = set(last_request_audit.get(
            "context_fact_source_message_ids_in_prompt", ()))
        marker_fact_id = recent_marker_fact["fact_id"] if recent_marker_fact else None
        marker_message_id = recent_marker_row["id"] if recent_marker_row else None
        marker_in_plan = bool(marker_fact_id and marker_fact_id in expected_fact_ids
                              and marker_message_id in expected_fact_sources)
        marker_in_request = bool(marker_fact_id and marker_fact_id in observed_fact_ids
                                 and marker_message_id in observed_fact_sources)
        audit_matches_request = (expected_fact_ids == observed_fact_ids
                                 and expected_fact_sources == observed_fact_sources)
        summary_content = summary["content"] if summary else ""
        summary_audit = _build_summary_audit(summary, persisted)
        result["summary"] = summary_audit
        result["history_evidence"] = {"message_count": len(persisted), "summary": summary_audit,
            "semantic_model": bool(summary and summary.get("model_name") == "qwen3.5-4b"),
            "early_marker": bool(summary and "ALPHA731" in summary_content),
            "middle_marker": bool(summary and "BETA842" in summary_content),
            "correction_markers": bool(summary and "RED" in summary_content and "BLUE" in summary_content),
            "summary_updated": bool(summary and intermediate_summary and
                                    summary["covered_through"] > intermediate_summary["covered_through"]),
            "recent_marker_in_model_request": marker_in_plan and marker_in_request and audit_matches_request,
            "recent_fact_trace": {
                "message_id": marker_message_id,
                "fact_id": marker_fact_id,
                "summary_covers_source": bool(summary and any(
                    source.get("message_id") == marker_message_id
                    for fact in summary.get("fact_ledger", [])
                    for source in fact.get("sources", []))),
                "context_plan_contains_source": marker_in_plan,
                "provider_request_contains_source": marker_in_request,
                "context_audit_matches_provider_request": audit_matches_request,
                "summary_covered_through": summary.get("covered_through") if summary else 0,
                "context_plan": context_audit,
                "provider_request": {
                    key: last_request_audit.get(key) for key in (
                        "context_fact_ids_in_prompt",
                        "context_fact_source_message_ids_in_prompt",
                        "message_count", "prompt_utf8_bytes",
                    )
                },
            }}
        if len(persisted) != 60:
            raise joint.CheckFailure("real_chat_persistence_count_mismatch")
        result["checks"]["thirty_real_qwen_turns_persisted"] = "passed"
        _finish_stage(result, "thirty_real_chat_turns", "completed",
                      turn_count=30, persisted_message_count=len(persisted),
                      final_summary_covered_through=summary.get("covered_through") if summary else 0)
        _start_stage(result, "context_assembly")
        result["checks"]["summary_preserves_fixed_markers"] = (
            "passed" if all(result["history_evidence"][name] for name in
                            ("semantic_model", "early_marker", "middle_marker", "correction_markers", "summary_updated",
                             "recent_marker_in_model_request")) else "failed")
        context_status = "completed" if result["checks"]["summary_preserves_fixed_markers"] == "passed" else "failed"
        _finish_stage(result, "context_assembly", context_status,
                      recent_fact_in_plan=marker_in_plan,
                      recent_fact_in_provider_request=marker_in_request,
                      audit_matches_provider_request=audit_matches_request,
                      source_message_id=marker_message_id, fact_id=marker_fact_id)
        if context_status != "completed":
            raise joint.CheckFailure("recent_protected_fact_missing_from_model_context")
        _start_stage(result, "pronoun_followup")
        followup = _invoke(cli, ["ask", "What does its second parameter represent?", *args],
                           result=result, operation="pronoun_followup")
        result["pronoun_followup"] = {"response": followup,
            "fixed_assertion": bool(followup.get("provider") == "llama-server" and
                                    "amount" in (followup.get("answer") or "").casefold() and
                                    followup.get("retrieval", {}).get("conversation_lookup_symbol") == "shared")}
        result["checks"]["pronoun_followup"] = "passed" if result["pronoun_followup"]["fixed_assertion"] else "failed"
        _finish_stage(result, "pronoun_followup",
                      "completed" if result["checks"]["pronoun_followup"] == "passed" else "failed")
        if result["checks"]["pronoun_followup"] != "passed":
            raise joint.CheckFailure("pronoun_followup_assertion_failed")
        _start_stage(result, "fixed_rag_cases")
        result["rag_cases"] = []
        for case in CASES:
            result["current_case"] = case["id"]
            result.pop("cli_failure_diagnostic", None)
            before_requests = len(resources["observations"]["chat"])
            try:
                response = _invoke(cli, ["ask", case["question"], *args], result=result,
                                   operation="rag_case_" + case["id"])
                outcome = _evaluate_case(case, response, repos[0], manifest)
            except joint.CheckFailure as exc:
                outcome = {
                    "id": case["id"], "status": "failed",
                    "checks": {"request_completed": False},
                    "failure_code": str(exc),
                    "diagnostic": result.pop("cli_failure_diagnostic", None),
                    "response": None,
                    "semantic_scope": "Provider or application rejection; no answer was accepted.",
                }
            outcome["chat_requests"] = len(resources["observations"]["chat"]) - before_requests
            if case.get("refuse") and outcome["chat_requests"]:
                outcome["status"] = "failed"
            result["rag_cases"].append(outcome)
            print("RAG case %s: %s" % (case["id"], outcome["status"]), flush=True)
        passed = sum(item["status"] == "passed" for item in result["rag_cases"])
        result["checks"]["fixed_rag_assertions"] = "%d/%d" % (passed, len(CASES))
        result["rag_semantics"] = "limited_fixed_assertions_not_full_semantic_entailment"
        fixed_assertions_failed = passed != len(CASES)
        _finish_stage(result, "fixed_rag_cases",
                      "failed" if fixed_assertions_failed else "completed",
                      passed=passed, total=len(CASES),
                      failed_cases=[item["id"] for item in result["rag_cases"]
                                    if item["status"] != "passed"])
    _start_stage(result, "cross_repository_memory_context")
    @contextmanager
    def opened_beta(**_kwargs):
        yield indexes[1]
    beta_args = ["--conversation-id", resources["sessions"][1]["id"], "--provider", "llama-server",
                 "--base-url", "http://127.0.0.1:56572/v1/chat/completions", "--model", "qwen3.5-4b",
                 "--read-timeout", "120", "--total-timeout", "180", "--max-output-tokens", "256", "--use-chroma"]
    with (patch("agentforge.cli._conversation_engine", return_value=engine),
          patch("agentforge.cli._open_current_index", side_effect=opened_beta),
          patch("agentforge.af012_runtime.create_persistent_chroma", return_value=client),
          patch("agentforge.af012_runtime.collection_for_model", return_value=collection),
          patch("agentforge.af012_runtime.embedding_provider_from_environment", return_value=provider)):
        beta_response = _invoke(cli,
            ["ask", "What does function shared return? Use my concise preference.", *beta_args],
            result=result, operation="cross_repository_memory_context")
    injected = beta_response.get("persistent_chroma", {}).get("memory_context", [])
    injected_ids = {item.get("memory_id") for item in injected}
    prompt_ids = set(resources["observations"]["chat"][-1].get("memory_ids", ()))
    result["cross_repository_memory"] = {"response": beta_response,
        "global_injected": resources["shared_memory"]["id"] in injected_ids and resources["shared_memory"]["id"] in prompt_ids,
        "private_leaked": resources["private_memory"]["id"] in injected_ids or resources["private_memory"]["id"] in prompt_ids,
        "causal_model_use": "not_measured"}
    if not result["cross_repository_memory"]["global_injected"] or result["cross_repository_memory"]["private_leaked"]:
        raise joint.CheckFailure("cross_repository_memory_injection_failed")
    result["checks"]["global_memory_in_actual_beta_prompt"] = "passed"
    _start_stage(result, "chroma_incremental_lifecycle")
    a, index = repos[0], indexes[0]
    code_index = ChromaCodeIndex(index, collection, provider)
    delta = a / "delta.py"
    delta.write_text("def delta_probe():\n    return 'first-version'\n", encoding="utf-8")
    index_repository(a, index)
    added = code_index.sync()
    delta.write_text("def delta_probe():\n    return 'second-version'\n", encoding="utf-8")
    index_repository(a, index)
    updated = code_index.sync()
    rows = code_index.search("delta_probe second-version", top_k=30)
    if not any(item.relative_path == "delta.py" and "second-version" in item.text for item in rows):
        raise joint.CheckFailure("updated_vector_not_retrieved")
    renamed = a / "renamed_probe.py"
    delta.rename(renamed)
    index_repository(a, index)
    renamed_stats = code_index.sync()
    matches = collection.get(where={"$and": [{"repository_id": resources["ids"][0]},
                                               {"model_id": provider.model_id}]}, include=["metadatas"])
    paths = {item.get("file_path") for item in matches.get("metadatas", [])}
    if "delta.py" in paths or "renamed_probe.py" not in paths:
        raise joint.CheckFailure("renamed_vector_mismatch")
    renamed.unlink()
    index_repository(a, index)
    deleted = code_index.sync()
    matches = collection.get(where={"$and": [{"repository_id": resources["ids"][0]},
                                               {"model_id": provider.model_id}]}, include=["metadatas"])
    if any(item.get("file_path") == "renamed_probe.py" for item in matches.get("metadatas", [])):
        raise joint.CheckFailure("deleted_vector_remains")
    index_repository(a, index, force_rebuild=True)
    rebuilt = code_index.sync(force=True)
    from dataclasses import asdict
    result["chroma_lifecycle"] = {"added": asdict(added), "updated": asdict(updated),
        "renamed": asdict(renamed_stats), "deleted": asdict(deleted), "rebuilt": asdict(rebuilt)}
    result["checks"]["real_embedding_update_rename_delete_rebuild"] = "passed"
    _start_stage(result, "chroma_reopen_process")
    child = ("import sys,json,chromadb; from chromadb.config import Settings; "
             "c=chromadb.PersistentClient(path=sys.argv[1],settings=Settings(anonymized_telemetry=False)); "
             "x=c.get_collection(sys.argv[2],embedding_function=None); "
             "print(json.dumps({'count':x.count()}))")
    env = {key: value for key, value in os.environ.items() if key not in
           {"AGENTFORGE_DB_PASSWORD", "AGENTFORGE_LLAMA_API_KEY", "PGPASSWORD"}}
    env["ANONYMIZED_TELEMETRY"] = "False"
    reopened = subprocess.run([sys.executable, "-X", "utf8", "-B", "-c", child,
        str(resources["base"] / "chroma"), collection.name], capture_output=True, text=True,
        timeout=60, env=env)
    if reopened.returncode != 0:
        result["chroma_reopen_error"] = {"exit_code": reopened.returncode,
                                          "stderr_chars": len(reopened.stderr)}
        raise joint.CheckFailure("chroma_reopen_failed")
    try:
        reopened_count = json.loads(reopened.stdout.strip().splitlines()[-1])["count"]
    except (ValueError, IndexError, KeyError):
        raise joint.CheckFailure("chroma_reopen_invalid_output") from None
    if reopened_count != collection.count():
        raise joint.CheckFailure("chroma_reopen_count_mismatch")
    result["checks"]["chroma_new_process_reopen"] = "passed"
    _start_stage(result, "postgres_transaction_rollback")
    from sqlalchemy import text
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE af0111_rollback_probe (id INTEGER PRIMARY KEY)"))
    with engine.connect() as connection:
        transaction = connection.begin()
        connection.execute(text("INSERT INTO af0111_rollback_probe (id) VALUES (1)"))
        transaction.rollback()
        if connection.scalar(text("SELECT COUNT(*) FROM af0111_rollback_probe")) != 0:
            raise joint.CheckFailure("postgres_rollback_failed")
    result["checks"]["postgres_transaction_rollback"] = "passed"
    _start_stage(result, "postgres_reconnect")
    engine.dispose()
    restored = ConversationStore(engine).get_messages(conversation["id"], resources["ids"][0])
    if len(restored) < 60:
        raise joint.CheckFailure("postgres_reconnect_lost_history")
    result["checks"]["postgres_new_connection_history"] = "passed"
    _finish_stage(result, "postgres_reconnect", "completed",
                  restored_message_count=len(restored))
    result["unmeasured"] = ["desktop_process_restart", "postgres_service_restart",
        "exact_model_token_budget", "arbitrary_semantic_entailment", "causal_memory_use_by_model",
        "semantic_conflict_resolution", "other_session_history_isolation",
        "repository_registration_delete_rebuild", "failure_recovery_live"]
    _raise_fixed_assertions_if_needed(result, fixed_assertions_failed)


def _start_stage(result, name):
    previous = result.get("stage")
    phases = result.setdefault("stage_results", {})
    if previous and previous != name and phases.get(previous, {}).get("status") == "running":
        phases[previous]["status"] = "completed"
    result["stage"] = name
    phases[name] = {"status": "running"}


def _finish_stage(result, name, status, **details):
    result.setdefault("stage_results", {})[name] = {"status": status, **details}


def _raise_fixed_assertions_if_needed(result, failed):
    if not failed:
        return
    result["stage"] = "fixed_rag_cases"
    result.setdefault("stage_results", {}).setdefault(
        "fixed_rag_cases", {"status": "failed"},
    )["status"] = "failed"
    raise joint.CheckFailure("fixed_acceptance_assertions_failed")


def main():
    observations = {"chat": [], "embedding": [], "embedding_health": [],
                    "limits": {"chat": CHAT_LIMIT, "embedding": EMBED_LIMIT, "seconds": TOTAL_SECONDS}}
    started = time.monotonic()
    baseline = {}
    def preflight(result):
        baseline.update(_file_snapshot(joint.ROOT, deadline=started + TOTAL_SECONDS))
        result["integrity"] = {"before": dict(baseline), "after": "not_measured",
                               "postgresql_logical_hash": "not_measured"}
        result["checks"]["asset_hash_preflight"] = "passed"
    def finalizer(result):
        if not baseline:
            result["integrity"] = {"status": "not_measured", "reason": "preflight_not_completed",
                                   "postgresql_logical_hash": "not_measured"}
            return
        after = _file_snapshot(joint.ROOT, deadline=started + TOTAL_SECONDS, verify_expected=False)
        changed = sorted(name for name in set(baseline) | set(after) if baseline.get(name) != after.get(name))
        result["integrity"] = {"before": baseline, "after": after, "changed": changed,
                               "status": "passed" if not changed else "changed",
                               "postgresql_logical_hash": "not_measured"}
        if changed and result["status"] not in ("FAIL", "BLOCKED"):
            result["status"] = "PARTIAL"
    if os.environ.get("CODEX_SANDBOX_NETWORK_DISABLED") == "1":
        return joint.main(extension=run_extended, observations=observations, preflight=preflight, finalizer=finalizer,
                          result_prefix="AF011_1_FULL_HOST_RESULT_")
    from agentforge.providers.llama_server import LlamaServerClient
    from agentforge.embeddings.llama_server import LlamaServerEmbeddingProvider
    original_chat = LlamaServerClient.chat
    original_embed = LlamaServerEmbeddingProvider._embed
    original_health = LlamaServerEmbeddingProvider.health
    def check_budget(kind):
        if time.monotonic() - started > TOTAL_SECONDS or len(observations[kind]) >= (CHAT_LIMIT if kind == "chat" else EMBED_LIMIT):
            raise joint.CheckFailure("request_budget_exceeded")
    def record_chat(self, messages, *, model, max_output_tokens=None):
        check_budget("chat")
        before = self.request_stats()
        requested_output_tokens = (self.max_output_tokens if max_output_tokens is None
                                   else max_output_tokens)
        content = "\n".join(str(item.get("content", "")) for item in messages if isinstance(item, dict))
        record = {"model": model, **_audit_chat_prompt(messages),
                  "request_purpose": _request_purpose(messages),
                  "memory_ids": re.findall(r"Memory ID: ([0-9a-f-]+)", content),
                  "requested_output_tokens": requested_output_tokens}
        if record["request_purpose"] == "semantic_summary":
            marker = "Summarize this JSON conversation history:\n"
            payload_text = next((str(item.get("content", "")).split(marker, 1)[1]
                                 for item in messages if isinstance(item, dict)
                                 and marker in str(item.get("content", ""))), "")
            try:
                summary_input = json.loads(payload_text)
                sequences = [row["sequence"] for row in summary_input
                             if isinstance(row, dict) and isinstance(row.get("sequence"), int)
                             and row.get("role") != "summary"]
                record["input_sequence_start"] = min(sequences) if sequences else None
                record["input_sequence_end"] = max(sequences) if sequences else None
                record["input_json_bytes"] = len(payload_text.encode("utf-8"))
            except (ValueError, TypeError, KeyError):
                record["input_sequence_start"] = None
                record["input_sequence_end"] = None
                record["input_json_bytes"] = None
        try:
            chat_kwargs = {} if max_output_tokens is None else {
                "max_output_tokens": max_output_tokens}
            response = original_chat(self, messages, model=model, **chat_kwargs)
            record["output_utf8_bytes"] = len(response.encode("utf-8")) if isinstance(response, str) else None
            return response
        except Exception as exc:
            record["exception_type"] = type(exc).__name__
            raise
        finally:
            after = self.request_stats()
            record.update({key: after.get(key, 0) - before.get(key, 0) for key in ("attempted", "sent", "succeeded", "failed", "retries")})
            record["http_status"] = after.get("last_http_status")
            record["failure_phase"] = after.get("last_failure_phase")
            record["finish_reason"] = after.get("last_finish_reason")
            record["error_code"] = after.get("last_error_code")
            record["response_diagnostic"] = after.get("last_response_diagnostic")
            observations["chat"].append(record)
    def record_embed(self, text, *, role):
        check_budget("embedding")
        before = self.request_counts
        record = {"role": role}
        try:
            return original_embed(self, text, role=role)
        except Exception as exc:
            record["exception_type"] = type(exc).__name__
            raise
        finally:
            after = self.request_counts
            record.update({key: after[key] - before[key] for key in ("attempts", "sent", "successes", "failures")})
            record["retries"] = 0
            observations["embedding"].append(record)
    def record_health(self, *args, **kwargs):
        if time.monotonic() - started > TOTAL_SECONDS:
            raise joint.CheckFailure("total_time_budget_exceeded")
        try:
            value = original_health(self, *args, **kwargs)
            observations["embedding_health"].append({"status": value})
            return value
        except Exception as exc:
            observations["embedding_health"].append({"error_type": type(exc).__name__})
            raise
    with (patch.object(LlamaServerClient, "chat", record_chat),
          patch.object(LlamaServerEmbeddingProvider, "_embed", record_embed),
          patch.object(LlamaServerEmbeddingProvider, "health", record_health)):
        return joint.main(extension=run_extended, observations=observations, preflight=preflight, finalizer=finalizer,
                          result_prefix="AF011_1_FULL_HOST_RESULT_")


if __name__ == "__main__":
    raise SystemExit(main())
