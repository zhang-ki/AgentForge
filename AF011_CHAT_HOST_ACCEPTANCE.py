"""One-command, bounded AF-011 real-chat acceptance runner.

Run manually from a normal Windows PowerShell host:

    python -X utf8 -B AF011_CHAT_HOST_ACCEPTANCE.py

The product CLI never manages llama-server.  This separate acceptance program
owns one temporary process, one temporary runtime copy, and at most one real
chat-completions request per invocation.  It preserves every previous attempt
in AF011_CHAT_ACCEPTANCE_RESULTS.json.
"""

from __future__ import annotations

import argparse
from contextlib import redirect_stderr, redirect_stdout
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid

import AF009GR3_HOST_RUNNER as host


ROOT = Path(__file__).resolve().parent
RESULT_PATH = ROOT / "AF011_CHAT_ACCEPTANCE_RESULTS.json"
REPORT_PATH = ROOT / "AF011_CHAT_ACCEPTANCE_REPORT.md"
TEMP_ROOT = ROOT / "tests" / ".tmp"
SOURCE_FIXTURE = ROOT / "tests" / "fixtures" / "synthetic_repo"
MODEL_ROOT = Path(os.environ.get("AGENTFORGE_MODEL_ROOT", ROOT.parent / "models")).resolve()
CHAT_MODEL_ROOT = MODEL_ROOT / "qwen3.5-4b"
MODEL_PATH = CHAT_MODEL_ROOT / "Qwen3.5-4B-Q4_K_M.gguf"
RUNTIME_PATH = CHAT_MODEL_ROOT / ".runtime" / "llama-b11424"
EXPECTED_HASHES = {
    MODEL_PATH: "00FE7986FF5F6B463E62455821146049DB6F9313603938A70800D1FB69EF11A4",
    RUNTIME_PATH / "llama-server.exe": "F8DF891A5E4D10FA8CFC0F86C8D99617A06EDD18D046E18B59EC3354F17FC12D",
    RUNTIME_PATH / "llama-server-impl.dll": "F020D94DF5E91840F2C9626B86F9AFE414C16D17ED5E1F41FE9CB614D3CDCDCB",
}
API_MODEL = "qwen3.5-4b-q4_k_m-af011-chat"
MODEL_READY_TIMEOUT = 180.0
CHAT_WORKER_TIMEOUT = 180.0
MAX_REAL_CHAT_REQUESTS = 1
RESULT_SCHEMA = "af011-chat-acceptance-v1"
EXPECTED_CITATION = "src/cache.py:4-6"
EXPECTED_EXCERPT = (
    "def refresh_access_token(session_id):\n"
    "    \"\"\"Return a deterministic placeholder for a session.\"\"\"\n"
    "    return f\"offline-{session_id}\""
)
PROXY_VARIABLES = (
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "all_proxy", "no_proxy",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def _record(path: Path) -> dict:
    return {"bytes": path.stat().st_size, "sha256": _sha256(path)}


def _tree_snapshot(root: Path) -> list[dict]:
    records = []
    for path in sorted(
        (item for item in root.rglob("*") if item.is_file()),
        key=lambda item: item.as_posix().casefold(),
    ):
        records.append({
            "path": path.relative_to(root).as_posix(),
            **_record(path),
        })
    return records


def _historical_expected() -> dict:
    path = ROOT / "AF010S2_RESULTS.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    expected = payload.get("protected_after")
    if not isinstance(expected, dict) or not expected:
        raise RuntimeError("AF010S2 protected baseline is missing or invalid.")
    return expected


def _verify_expected(expected: dict) -> dict:
    missing = []
    changed = []
    current = {}
    for relative, baseline in sorted(expected.items()):
        path = ROOT / relative
        if not path.is_file():
            missing.append(relative)
            continue
        record = _record(path)
        current[relative] = record
        if record != baseline:
            changed.append({"path": relative, "expected": baseline, "actual": record})
    return {
        "count": len(expected), "missing": missing, "changed": changed,
        "passed": not missing and not changed, "current": current,
    }


def _existing_llama_servers() -> list[str]:
    completed = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq llama-server.exe", "/FO", "CSV", "/NH"],
        check=False, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=10.0,
    )
    if completed.returncode != 0:
        raise RuntimeError("Could not confirm that no llama-server process is already running.")
    values = []
    for row in csv.reader(io.StringIO(completed.stdout)):
        if row and row[0].casefold() == "llama-server.exe":
            values.append(row[1] if len(row) > 1 else "pid-unavailable")
    return values


def _owned_listeners(pid: int) -> list[dict]:
    command = (
        "$ErrorActionPreference='Stop'; "
        f"$rows=Get-NetTCPConnection -OwningProcess {int(pid)} -State Listen | "
        "Select-Object LocalAddress,LocalPort,OwningProcess; "
        "if ($null -eq $rows) { '[]' } else { $rows | ConvertTo-Json -Compress }"
    )
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
        check=False, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=10.0,
    )
    if completed.returncode != 0:
        raise RuntimeError("Could not verify the owned process listener addresses.")
    value = json.loads(completed.stdout.strip() or "[]")
    return value if isinstance(value, list) else [value]


def _sanitized_text(value: str, limit: int = 4000) -> str:
    value = re.sub(
        r"(?i)(api[_-]?key|authorization|password|secret|token)(\s*[:=]\s*)([^\s,;]+)",
        r"\1\2<redacted>", value,
    )
    return value[:limit]


def _load_history() -> dict:
    if not RESULT_PATH.exists():
        return {"schema": RESULT_SCHEMA, "latest_status": "not_run", "attempts": []}
    value = json.loads(RESULT_PATH.read_text(encoding="utf-8"))
    if (not isinstance(value, dict) or value.get("schema") != RESULT_SCHEMA
            or not isinstance(value.get("attempts"), list)):
        raise RuntimeError("Existing AF011 chat result has an incompatible schema.")
    return value


def _write_history(history: dict) -> None:
    temporary = RESULT_PATH.with_suffix(".json.tmp")
    try:
        temporary.write_text(
            json.dumps(history, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(RESULT_PATH)
    finally:
        if temporary.exists():
            temporary.unlink()


def _write_report(history: dict) -> None:
    latest = history["attempts"][-1] if history.get("attempts") else {}
    lines = [
        "# AF-011 真实聊天验收报告",
        "",
        f"当前状态：`{latest.get('status', 'NOT_RUN')}`",
        "",
        "本报告由有界 Host Runner 更新；历史尝试保存在机器结果的 `attempts` 数组中。",
        "API key、Authorization 头和完整环境变量不会写入结果。",
        "",
        "## 最近一次尝试",
        "",
        f"- attempt_id：`{latest.get('attempt_id', 'none')}`",
        f"- 执行环境：`{latest.get('execution_environment', {}).get('classification', 'unknown')}`",
        f"- 模型服务启动：`{latest.get('service', {}).get('started', False)}`",
        f"- 模型就绪：`{latest.get('service', {}).get('model_ready', False)}`",
        f"- 请求计数 attempted/sent/succeeded/failed/retries：`"
        f"{latest.get('request_counts', {}).get('attempted', 0)}/"
        f"{latest.get('request_counts', {}).get('sent', 0)}/"
        f"{latest.get('request_counts', {}).get('succeeded', 0)}/"
        f"{latest.get('request_counts', {}).get('failed', 0)}/"
        f"{latest.get('request_counts', {}).get('retries', 0)}`",
        f"- 服务退出码：`{latest.get('service', {}).get('exit_code', 'not_measured')}`",
        f"- 端口释放：`{latest.get('service', {}).get('port_released', 'not_measured')}`",
        f"- 结论：{latest.get('summary', latest.get('blocked_reason', '尚无结果'))}",
        "",
    ]
    cases = latest.get("cases") or {}
    if cases:
        lines.extend(["## 应用层案例", ""])
        for name in ("evidence_question", "no_evidence_question"):
            case = cases.get(name) or {}
            lines.extend([
                f"### {name}", "",
                f"- 状态：`{case.get('status', 'not_measured')}`",
                f"- Provider：`{case.get('provider', 'unknown')}`",
                f"- 引用/回答合同：`{case.get('contract_status', 'not_measured')}`",
                f"- 语义检查：`{case.get('semantic_status', 'not_measured')}`",
            ])
            if case.get("answer"):
                lines.extend(["", "实际回答：", "", case["answer"]])
            lines.append("")
    else:
        lines.extend([
            "## 应用层案例", "",
            "- 有证据真实回答：`NOT_MEASURED`",
            "- 引用与语义断言：`NOT_MEASURED`",
            "- 无证据拒答：`NOT_MEASURED`（仅离线回归已覆盖）",
            "",
        ])
    worker_cases = ((latest.get("ask_worker") or {}).get("payload") or {}).get("cases") or {}
    failed_cases = [
        (name, case) for name, case in worker_cases.items()
        if isinstance(case, dict) and case.get("exit_code") not in (None, 0)
    ]
    if failed_cases:
        lines.extend(["## 失败阶段证据", ""])
        for name, case in failed_cases:
            failure = case.get("failure") or {}
            stats = case.get("provider_request_stats") or {}
            lines.extend([
                f"- `{name}`：阶段 `{failure.get('stage', 'legacy_unclassified')}`；"
                f"代码 `{failure.get('code', 'legacy_unclassified')}`；"
                f"请求 attempted/sent/succeeded/failed = "
                f"`{stats.get('attempted', 'not_recorded')}/{stats.get('sent', 'not_recorded')}/"
                f"{stats.get('succeeded', 'not_recorded')}/{stats.get('failed', 'not_recorded')}`。",
            ])
        lines.append("")
    repairs = history.get("repair_audits") or []
    if repairs:
        repair = repairs[-1]
        lines.extend([
            "## 最近一次离线故障修复", "",
            f"- 对应真实尝试：`{repair.get('attempt_id', 'unknown')}`（仍为 `FAIL`）。",
            f"- 已确认根因：{repair.get('confirmed_root_cause', 'not_recorded')}",
            f"- 响应子类型：`{repair.get('response_subtype', 'not_measured')}`。",
            f"- 离线修复状态：`{repair.get('status', 'not_recorded')}`；下一次真实验收："
            f"`{repair.get('next_real_acceptance', 'not_recorded')}`。",
            "",
        ])
    integrity = latest.get("integrity") or {}
    lines.extend([
        "## 完整性", "",
        f"- 正式索引：`{json.dumps(integrity.get('formal_index', 'not_measured'), ensure_ascii=False)}`",
        f"- 只读证据：`{json.dumps(integrity.get('read_only_evidence', 'not_measured'), ensure_ascii=False)}`",
        f"- Agentexpr 访问：`{integrity.get('agentexpr_accessed', False)}`",
        "",
    ])
    regression = history.get("offline_regression") or {}
    if regression:
        lines.extend([
            "## 离线回归", "",
            f"- 命令：`{regression.get('command', 'not_recorded')}`",
            f"- 运行/通过/失败/错误/跳过：`{regression.get('run', 0)}/"
            f"{regression.get('passed', 0)}/{regression.get('failures', 0)}/"
            f"{regression.get('errors', 0)}/{regression.get('skipped', 0)}`",
            f"- 退出码：`{regression.get('exit_code', 'not_recorded')}`",
            "- 跳过原因：Windows 当前权限无法创建符号链接、junction 或硬链接；跳过未计入通过。",
            "",
        ])
    if latest.get("status") == "BLOCKED":
        lines.extend([
            "## 一键宿主执行", "",
            "在平台允许 loopback 的普通 Windows PowerShell 中执行：", "",
            "```powershell",
            "cd <AgentForge project directory>",
            "python -X utf8 -B AF011_CHAT_HOST_ACCEPTANCE.py",
            "```",
            "",
            "该命令会自行核验资产、启动和清理唯一临时服务，不要求手工输入 API key。",
        ])
    REPORT_PATH.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _append_attempt(attempt: dict) -> None:
    history = _load_history()
    history["attempts"].append(attempt)
    history["latest_status"] = attempt["status"]
    history["latest_attempt_id"] = attempt["attempt_id"]
    _write_history(history)
    _write_report(history)


class _BorrowedStore:
    def __init__(self, store):
        self.store = store

    def __enter__(self):
        return self.store

    def __exit__(self, exc_type, exc, traceback):
        return False


def _invoke_cli(cli_module, store, arguments: list[str]) -> dict:
    output = io.StringIO()
    errors = io.StringIO()
    original_open_index = cli_module._open_current_index
    original_client = cli_module.LlamaServerClient
    clients = []

    def capture_client(*args, **kwargs):
        client = original_client(*args, **kwargs)
        clients.append(client)
        return client

    cli_module._open_current_index = lambda readonly=False: _BorrowedStore(store)
    cli_module.LlamaServerClient = capture_client
    try:
        with redirect_stdout(output), redirect_stderr(errors):
            code = cli_module.main(arguments)
    finally:
        cli_module._open_current_index = original_open_index
        cli_module.LlamaServerClient = original_client
    text = output.getvalue()
    payload = None
    if text.strip():
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = None
    request_stats = None
    if clients:
        getter = getattr(clients[-1], "request_stats", None)
        if callable(getter):
            value = getter()
            request_stats = value if isinstance(value, dict) else None
    error_text = _sanitized_text(errors.getvalue())
    failure = None
    if code != 0:
        if request_stats is None:
            stage = "PROVIDER_CONFIGURATION_OR_CLI_PREFLIGHT"
            failure_code = "cli_failed_before_client_creation"
        elif request_stats.get("attempted", 0) == 0:
            stage = "BEFORE_PROVIDER_REQUEST"
            failure_code = "cli_failed_before_provider_request"
        elif request_stats.get("last_application_failure_phase"):
            phase = request_stats["last_application_failure_phase"]
            stage = {
                "citation_validation": "CITATION_VALIDATION",
                "answer_contract": "ANSWER_CONTRACT",
            }.get(phase, "APPLICATION_VALIDATION")
            failure_code = (
                request_stats.get("last_application_error_code")
                or "application_validation_failed"
            )
        elif request_stats.get("sent", 0) == 0:
            stage = "PROVIDER_CONNECTION_OR_REQUEST_WRITE"
            failure_code = request_stats.get("last_error_code") or "provider_send_failed"
        else:
            stage = "PROVIDER_RESPONSE"
            failure_code = request_stats.get("last_error_code") or "provider_response_failed"
        failure = {
            "stage": stage,
            "code": failure_code,
            "type": "CliExecutionError",
            "message": error_text.strip() or f"AgentForge CLI exited with code {code}.",
        }
    return {
        "exit_code": code,
        "stdout_readable": bool(text),
        "stderr": error_text,
        "payload": payload,
        "provider_request_stats": request_stats,
        "failure": failure,
    }


def _ask_worker(repository: Path, endpoint: str, model: str) -> int:
    """Index in memory, then exercise the actual CLI ask path twice."""
    from agentforge import cli
    from agentforge.indexer import index_repository
    from agentforge.store import IndexStore

    if not os.environ.get("AGENTFORGE_LLAMA_API_KEY"):
        print(json.dumps({"error": "worker API key is missing"}))
        return 2
    started = time.perf_counter()
    with IndexStore(":memory:") as store:
        index_result = index_repository(repository, store)
        common = [
            "--provider", "llama-server",
            "--base-url", endpoint,
            "--model", model,
            "--connect-timeout", "2",
            "--read-timeout", "120",
            "--total-timeout", "150",
            "--max-output-tokens", "512",
            "--top-k", "3",
            "--context-budget", "4000",
            "--mode", "repository",
        ]
        evidence = _invoke_cli(cli, store, [
            "ask",
            "What does function refresh_access_token return for session_id? Cite the exact source.",
            *common,
        ])
        # This second product-path case must be blocked before a model request.
        no_evidence = _invoke_cli(cli, store, [
            "ask", "What does function quantum_flux_override_zz9 return?", *common,
        ])
    result = {
        "worker_status": "completed" if (
            evidence["exit_code"] == 0 and no_evidence["exit_code"] == 0
        ) else "failed",
        "index": index_result,
        "cases": {
            "evidence_question": evidence,
            "no_evidence_question": no_evidence,
        },
        "elapsed_seconds": round(time.perf_counter() - started, 6),
    }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["worker_status"] == "completed" else 1


def _run_worker(repository: Path, endpoint: str, model: str, api_key: str) -> dict:
    environment = os.environ.copy()
    for key in PROXY_VARIABLES:
        environment.pop(key, None)
    environment["AGENTFORGE_LLAMA_API_KEY"] = api_key
    command = [
        sys.executable, "-X", "utf8", "-B", str(Path(__file__).resolve()),
        "--ask-worker", str(repository), endpoint, model,
    ]
    started = time.perf_counter()
    process = subprocess.Popen(
        command, cwd=ROOT, env=environment, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        encoding="utf-8", errors="replace", shell=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    timed_out = False
    termination = "exited"
    try:
        stdout, stderr = process.communicate(timeout=CHAT_WORKER_TIMEOUT)
    except subprocess.TimeoutExpired:
        timed_out = True
        process.terminate()
        try:
            stdout, stderr = process.communicate(timeout=10.0)
            termination = "terminated_after_total_timeout"
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate(timeout=10.0)
            termination = "killed_after_total_timeout"
    payload = None
    if stdout.strip():
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError:
            payload = None
    return {
        "created": True,
        "exit_code": process.returncode,
        "timed_out": timed_out,
        "termination": termination,
        "elapsed_seconds": round(time.perf_counter() - started, 6),
        "stdout_readable": bool(stdout),
        "stderr": _sanitized_text(stderr),
        "payload": payload,
    }


def _sum_request_counts(worker: dict) -> dict:
    total = {"attempted": 0, "sent": 0, "succeeded": 0, "failed": 0, "retries": 0}
    payload = worker.get("payload") or {}
    for case in (payload.get("cases") or {}).values():
        stats = case.get("provider_request_stats")
        if not isinstance(stats, dict):
            stats = ((case.get("payload") or {}).get("provider_request_stats") or {})
        for key in total:
            value = stats.get(key, 0)
            if isinstance(value, int) and not isinstance(value, bool):
                total[key] += value
    return total


def _worker_case_audit(worker: dict) -> dict:
    """Retain failure-stage evidence even when the CLI produced no JSON answer."""
    payload = worker.get("payload") or {}
    audited = {}
    for name, case in (payload.get("cases") or {}).items():
        audited[name] = {
            "exit_code": case.get("exit_code"),
            "failure": case.get("failure"),
            "provider_request_stats": case.get("provider_request_stats"),
            "stdout_readable": case.get("stdout_readable", False),
            "stderr": case.get("stderr", ""),
        }
    return audited


def _evaluate_cases(worker: dict) -> dict:
    payload = worker.get("payload")
    if worker.get("timed_out") or not isinstance(payload, dict):
        case_audit = _worker_case_audit(worker)
        failures = [
            value.get("failure") for value in case_audit.values()
            if isinstance(value.get("failure"), dict)
        ]
        if failures:
            failure = failures[0]
            raise RuntimeError(
                "AgentForge ask worker failed at "
                f"{failure.get('stage', 'UNKNOWN')} ({failure.get('code', 'unknown')})."
            )
        raise RuntimeError("AgentForge ask worker did not return a complete structured result.")
    raw_cases = payload.get("cases") or {}
    evidence_raw = raw_cases.get("evidence_question") or {}
    no_evidence_raw = raw_cases.get("no_evidence_question") or {}
    evidence_payload = evidence_raw.get("payload") or {}
    no_evidence_payload = no_evidence_raw.get("payload") or {}

    if evidence_raw.get("exit_code") not in (None, 0) or not evidence_payload:
        evidence_stats = evidence_raw.get("provider_request_stats") or {}
        evidence_result = {
            "status": "FAIL",
            "provider": "unknown",
            "answer": None,
            "contract_status": "not_measured",
            "semantic_status": "not_measured",
            "semantic_checks": {},
            "evidence": [],
            "retrieval": None,
            "context_budget": None,
            "request_stats": evidence_stats,
            "failure": evidence_raw.get("failure"),
        }
        evidence_passed = False
    else:
        answer = evidence_payload.get("answer")
        contract = evidence_payload.get("answer_contract") or {}
        evidence_rows = contract.get("evidence") or []
        claims = contract.get("claims") or []
        evidence_stats = evidence_payload.get("provider_request_stats") or {}
        citations = {
            citation
            for claim in claims if isinstance(claim, dict)
            for citation in claim.get("citations", [])
        }
        exact_evidence = any(
            row.get("relative_path") == "src/cache.py"
            and row.get("start_line") == 4 and row.get("end_line") == 6
            and row.get("excerpt") == EXPECTED_EXCERPT
            for row in evidence_rows if isinstance(row, dict)
        )
        semantic_checks = {
            "answer_is_nonempty": isinstance(answer, str) and bool(answer.strip()),
            "mentions_offline_prefix": isinstance(answer, str) and "offline-" in answer,
            "mentions_session_id": isinstance(answer, str) and "session_id" in answer,
            "expected_citation_in_claim": EXPECTED_CITATION in citations,
            "exact_source_excerpt_retained": exact_evidence,
            "provider_is_real_llama": evidence_payload.get("provider") == "llama-server",
            "contract_is_limited_or_answered": contract.get("status") in {"limited", "answered"},
            "response_was_not_token_truncated": evidence_stats.get("last_finish_reason") not in {
                "length", "max_tokens",
            },
        }
        evidence_passed = all(semantic_checks.values())
        evidence_result = {
            "status": "PASS" if evidence_passed else "FAIL",
            "provider": evidence_payload.get("provider"),
            "answer": answer,
            "contract_status": contract.get("status", "not_measured"),
            "semantic_status": "PASS" if evidence_passed else "FAIL",
            "semantic_checks": semantic_checks,
            "evidence": evidence_rows,
            "retrieval": evidence_payload.get("retrieval"),
            "context_budget": evidence_payload.get("context_budget"),
            "request_stats": evidence_stats,
            "failure": None,
        }

    if no_evidence_raw.get("exit_code") not in (None, 0) or not no_evidence_payload:
        no_evidence_result = {
            "status": "FAIL", "provider": "unknown", "answer": None,
            "refused_output": None, "contract_status": "not_measured",
            "semantic_status": "not_measured", "checks": {}, "retrieval": None,
            "request_stats": no_evidence_raw.get("provider_request_stats") or {},
            "failure": no_evidence_raw.get("failure"),
        }
        no_evidence_passed = False
    else:
        absent_contract = no_evidence_payload.get("answer_contract") or {}
        absent_stats = no_evidence_payload.get("provider_request_stats") or {}
        no_evidence_checks = {
            "provider_gate_refused": no_evidence_payload.get("provider") == "evidence_gate_refusal",
            "answer_not_exposed": no_evidence_payload.get("answer") is None,
            "contract_refused": absent_contract.get("status") == "refused",
            "no_chat_attempt": absent_stats.get("attempted") == 0,
            "no_chat_sent": absent_stats.get("sent") == 0,
        }
        no_evidence_passed = all(no_evidence_checks.values())
        no_evidence_result = {
            "status": "PASS" if no_evidence_passed else "FAIL",
            "provider": no_evidence_payload.get("provider"),
            "answer": no_evidence_payload.get("answer"),
            "refused_output": no_evidence_payload.get("refused_output"),
            "contract_status": absent_contract.get("status", "not_measured"),
            "semantic_status": "PASS" if no_evidence_passed else "FAIL",
            "checks": no_evidence_checks,
            "retrieval": no_evidence_payload.get("retrieval"),
            "request_stats": absent_stats,
            "failure": None,
        }
    return {
        "evidence_question": evidence_result,
        "no_evidence_question": no_evidence_result,
        "passed": evidence_passed and no_evidence_passed,
    }


def _blocked_attempt() -> dict:
    return {
        "attempt_id": uuid.uuid4().hex,
        "status": "BLOCKED",
        "summary": (
            "Current process has CODEX_SANDBOX_NETWORK_DISABLED=1; no loopback test, "
            "asset access, service start, Health request, or chat request was attempted."
        ),
        "blocked_reason": "platform_network_disabled",
        "execution_environment": {
            "classification": "network_restricted_codex_process",
            "platform": sys.platform,
            "python": sys.version,
            "network_disabled_marker": "1",
            "marker_was_not_modified": True,
        },
        "service": {"started": False, "model_ready": False},
        "request_counts": {
            "attempted": 0, "sent": 0, "succeeded": 0, "failed": 0, "retries": 0,
        },
        "cases": {},
        "integrity": {
            "formal_index": _record(ROOT / ".agentforge/data/index.sqlite3"),
            "read_only_evidence": _record(ROOT / ".agentforge/data/read_only_evidence.json"),
            "agentexpr_accessed": False,
        },
    }


def _host_attempt() -> tuple[dict, int]:
    attempt_started = time.perf_counter()
    attempt = {
        "attempt_id": uuid.uuid4().hex,
        "status": "FAIL",
        "summary": "Host acceptance did not complete.",
        "execution_environment": {
            "classification": "windows_host_candidate",
            "platform": sys.platform,
            "python": sys.version,
            "network_disabled_marker": os.environ.get("CODEX_SANDBOX_NETWORK_DISABLED"),
            "marker_was_not_modified": True,
        },
        "state": None,
        "state_history": [],
        "service": {"started": False, "model_ready": False},
        "request_counts": {
            "attempted": 0, "sent": 0, "succeeded": 0, "failed": 0, "retries": 0,
        },
        "cases": {},
        "integrity": {"agentexpr_accessed": False},
    }
    process = None
    reader_threads = []
    stdout_buffer = bytearray()
    stderr_buffer = bytearray()
    output_counts = {}
    temp_root = None
    temp_runtime = None
    temp_repository = None
    source_manifest = []
    api_key = None

    def transition(state: str) -> None:
        attempt["state"] = state
        attempt["state_history"].append({
            "state": state, "monotonic_seconds": round(time.monotonic(), 6),
        })

    try:
        if sys.platform != "win32":
            raise RuntimeError("The pinned llama-server runtime requires Windows.")
        expected = _historical_expected()
        historical_before = _verify_expected(expected)
        attempt["integrity"]["historical_before"] = {
            key: value for key, value in historical_before.items() if key != "current"
        }
        if not historical_before["passed"]:
            raise RuntimeError("A protected historical or formal file differs from its frozen baseline.")
        source_before = _tree_snapshot(SOURCE_FIXTURE)
        attempt["integrity"]["source_fixture_before"] = source_before

        existing = _existing_llama_servers()
        attempt["preexisting_llama_server_pids"] = existing
        if existing:
            raise RuntimeError("An existing llama-server was found; this runner will not touch it.")

        attempt["assets_before"] = {}
        for path, expected_hash in EXPECTED_HASHES.items():
            if not path.is_file():
                raise FileNotFoundError(f"Required asset is missing: {path}")
            record = _record(path)
            record.update({"expected_sha256": expected_hash, "matches": record["sha256"] == expected_hash})
            attempt["assets_before"][str(path)] = record
            if not record["matches"]:
                raise RuntimeError(f"Required asset hash does not match: {path.name}")

        source_manifest = host._runtime_manifest(RUNTIME_PATH)
        if len(source_manifest) != 51:
            raise RuntimeError("Pinned llama-b11424 runtime no longer contains 51 files.")
        attempt["runtime_copy"] = {
            "source_file_count": len(source_manifest),
            "source_manifest_sha256": hashlib.sha256(
                json.dumps(source_manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest().upper(),
        }

        attempt["loopback_preflight"] = host._loopback_preflight()
        if attempt["loopback_preflight"].get("status") != "passed":
            raise RuntimeError("Loopback preflight failed; model startup is prohibited.")

        TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        temp_root = TEMP_ROOT / f"af011-chat-{uuid.uuid4().hex}"
        temp_runtime = temp_root / "runtime"
        temp_repository = temp_root / "repository"
        temp_root.mkdir()
        shutil.copytree(RUNTIME_PATH, temp_runtime, copy_function=shutil.copy2)
        shutil.copytree(SOURCE_FIXTURE, temp_repository, copy_function=shutil.copy2)
        copied_manifest = host._runtime_manifest(temp_runtime)
        attempt["runtime_copy"].update({
            "path": str(temp_runtime), "copied_file_count": len(copied_manifest),
            "copy_matches_source": copied_manifest == source_manifest,
        })
        if copied_manifest != source_manifest:
            raise RuntimeError("Temporary runtime copy does not match the pinned source manifest.")

        port = host._reserve_ephemeral_port()
        executable = temp_runtime / "llama-server.exe"
        arguments = [
            str(executable), "--model", str(MODEL_PATH), "--alias", API_MODEL,
            "--host", "127.0.0.1", "--port", str(port),
            "--ctx-size", "4096", "--n-gpu-layers", "0", "--no-webui",
            "--cors-origins", f"http://127.0.0.1:{port}", "--no-cors-credentials",
        ]
        api_key = secrets.token_urlsafe(32)
        child_environment = os.environ.copy()
        for key in PROXY_VARIABLES:
            child_environment.pop(key, None)
        child_environment.update({
            "LLAMA_API_KEY": api_key,
            "TEMP": str(temp_runtime),
            "TMP": str(temp_runtime),
        })
        process = subprocess.Popen(
            arguments, cwd=temp_runtime, env=child_environment,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            shell=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        attempt["service"].update({
            "started": True, "pid": process.pid, "model_ready": False,
            "host": "127.0.0.1", "port": port,
            "executable": str(executable.resolve()),
            "working_directory": str(temp_runtime),
            "arguments": arguments[1:],
            "api_key_transport": "child_environment_only",
        })
        transition("PROCESS_STARTED")
        for name, pipe, target in (
            ("stdout_bytes", process.stdout, stdout_buffer),
            ("stderr_bytes", process.stderr, stderr_buffer),
        ):
            thread = threading.Thread(
                target=host._drain, args=(pipe, target, output_counts, name),
                name=f"af011-chat-{name}", daemon=True,
            )
            thread.start()
            reader_threads.append(thread)

        attempt["service"]["tcp_readiness"] = host._wait_for_tcp(process, port, timeout=60.0)
        transition("TCP_LISTENING")
        listeners = _owned_listeners(process.pid)
        attempt["service"]["listeners"] = listeners
        if not listeners or any(
            str(item.get("LocalAddress", "")).strip() != "127.0.0.1"
            or int(item.get("LocalPort", -1)) != port
            for item in listeners
        ):
            raise RuntimeError("Owned llama-server listener is absent or not loopback-only.")

        transition("MODEL_LOADING")
        readiness = host._wait_for_model_ready(
            process, "127.0.0.1", port,
            total_timeout=MODEL_READY_TIMEOUT,
            request_timeout=2.0, poll_interval=1.0,
        )
        attempt["service"]["model_readiness"] = readiness
        attempt["service"]["model_ready"] = True
        transition("MODEL_READY")

        transition("ASKING")
        worker = _run_worker(
            temp_repository,
            f"http://127.0.0.1:{port}/v1/chat/completions",
            API_MODEL,
            api_key,
        )
        attempt["ask_worker"] = worker
        attempt["request_counts"] = _sum_request_counts(worker)
        attempt["cases"] = _worker_case_audit(worker)
        if attempt["request_counts"]["attempted"] > MAX_REAL_CHAT_REQUESTS:
            raise RuntimeError("Real chat request limit was exceeded.")
        cases = _evaluate_cases(worker)
        attempt["cases"] = {
            "evidence_question": cases["evidence_question"],
            "no_evidence_question": cases["no_evidence_question"],
        }
        if not cases["passed"]:
            failed = next(
                value for value in attempt["cases"].values()
                if value.get("status") != "PASS"
            )
            failure = failed.get("failure") or {}
            detail = (
                f" at {failure.get('stage')} ({failure.get('code')})"
                if failure else ""
            )
            raise RuntimeError(f"One or more application-layer acceptance cases failed{detail}.")
        if attempt["request_counts"] != {
            "attempted": 1, "sent": 1, "succeeded": 1, "failed": 0, "retries": 0,
        }:
            raise RuntimeError("Real chat request counters do not match the one-request contract.")
        attempt["service"]["resource_sample"] = host._process_memory_record(process)
        attempt["status"] = "PASS"
        attempt["summary"] = (
            "One real Qwen chat request completed through AgentForge ask; the cited source, "
            "deterministic semantic assertion, and no-evidence refusal all passed."
        )
        transition("COMPLETED")
    except BaseException as exc:
        attempt["status"] = "FAIL"
        attempt["summary"] = f"Acceptance failed at state {attempt.get('state') or 'PRECHECK'}."
        attempt["error"] = host._error_record(exc)
        if isinstance(exc, host.ModelReadinessError):
            attempt["error"].update({
                "code": exc.code, "observations": exc.observations,
                "process_exit_code": exc.exit_code,
            })
        transition("FAILED")
    finally:
        api_key = None
        if process is not None:
            attempt["service"].setdefault("resource_sample", host._process_memory_record(process))
            running_before_cleanup = process.poll() is None
            attempt["service"]["running_before_cleanup"] = running_before_cleanup
            cleanup = host._stop_owned_process(process)
            attempt["service"].update(cleanup)
            for thread in reader_threads:
                thread.join(timeout=5.0)
            attempt["service"]["output_bytes"] = output_counts
            attempt["service"]["stdout_summary"] = host._safe_log_summary(bytes(stdout_buffer))
            attempt["service"]["stderr_summary"] = host._safe_log_summary(bytes(stderr_buffer))
            attempt["service"]["port_released"] = host._port_released(
                attempt["service"].get("port", -1)
            )
            attempt["service"]["exit_semantics"] = (
                "runner_initiated_termination"
                if cleanup.get("termination") in {"terminated", "killed_after_terminate_timeout"}
                else "process_exited_without_cleanup_signal"
            )
            if (attempt["status"] == "PASS" and (
                    not running_before_cleanup
                    or not cleanup.get("owned_process_stopped")
                    or not attempt["service"]["port_released"])):
                attempt["status"] = "FAIL"
                attempt["summary"] = "Application checks passed but service cleanup was not verified."
                transition("FAILED")

        if temp_runtime is not None and temp_runtime.exists():
            post_manifest = host._runtime_manifest(temp_runtime)
            attempt.setdefault("runtime_copy", {}).update({
                "post_run_file_count": len(post_manifest),
                "post_run_matches_source": post_manifest == source_manifest,
                "extra_files": [
                    row["path"] for row in post_manifest
                    if row["path"] not in {item["path"] for item in source_manifest}
                ],
            })
        if temp_root is not None and temp_root.exists():
            try:
                shutil.rmtree(temp_root)
            except OSError as exc:
                attempt.setdefault("cleanup_errors", []).append(host._error_record(exc))
        attempt.setdefault("runtime_copy", {})["removed"] = bool(
            temp_root is None or not temp_root.exists()
        )

        attempt["assets_after"] = {
            str(path): _record(path) for path in EXPECTED_HASHES if path.is_file()
        }
        attempt["assets_unchanged"] = all(
            attempt["assets_after"].get(path) == {
                "bytes": before.get("bytes"), "sha256": before.get("sha256"),
            }
            for path, before in attempt.get("assets_before", {}).items()
        ) and len(attempt.get("assets_before", {})) == len(attempt["assets_after"])
        try:
            expected = _historical_expected()
            historical_after = _verify_expected(expected)
            attempt["integrity"]["historical_after"] = {
                key: value for key, value in historical_after.items() if key != "current"
            }
            attempt["integrity"]["source_fixture_after"] = _tree_snapshot(SOURCE_FIXTURE)
            attempt["integrity"]["source_fixture_unchanged"] = (
                attempt["integrity"].get("source_fixture_before")
                == attempt["integrity"]["source_fixture_after"]
            )
            attempt["integrity"]["formal_index"] = _record(
                ROOT / ".agentforge/data/index.sqlite3"
            )
            attempt["integrity"]["read_only_evidence"] = _record(
                ROOT / ".agentforge/data/read_only_evidence.json"
            )
            integrity_passed = (
                historical_after["passed"]
                and attempt["integrity"].get("source_fixture_unchanged")
                and attempt.get("assets_unchanged")
                and attempt.get("runtime_copy", {}).get("post_run_matches_source")
                and attempt.get("runtime_copy", {}).get("removed")
            )
            attempt["integrity"]["passed"] = bool(integrity_passed)
            if attempt["status"] == "PASS" and not integrity_passed:
                attempt["status"] = "FAIL"
                attempt["summary"] = "Application checks passed but integrity cleanup failed."
                transition("FAILED")
        except BaseException as exc:
            attempt["integrity"]["verification_error"] = host._error_record(exc)
            attempt["integrity"]["passed"] = False
            if attempt["status"] == "PASS":
                attempt["status"] = "FAIL"
                attempt["summary"] = "Application checks passed but final integrity was not verified."
                transition("FAILED")

        attempt["total_elapsed_seconds"] = round(
            time.perf_counter() - attempt_started, 6
        )

    return attempt, 0 if attempt["status"] == "PASS" else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Run one bounded real Qwen chat acceptance through AgentForge ask."
    )
    parser.add_argument("--ask-worker", nargs=3, metavar=("REPOSITORY", "ENDPOINT", "MODEL"),
                        help=argparse.SUPPRESS)
    arguments = parser.parse_args(argv)
    if arguments.ask_worker:
        repository, endpoint, model = arguments.ask_worker
        return _ask_worker(Path(repository), endpoint, model)

    history = _load_history()
    if any(item.get("status") == "PASS" for item in history.get("attempts", [])):
        print("AF-011 real-chat acceptance already passed; refusing another real request.")
        return 2
    if os.environ.get("CODEX_SANDBOX_NETWORK_DISABLED") == "1":
        attempt = _blocked_attempt()
        _append_attempt(attempt)
        print(json.dumps({
            "status": "BLOCKED",
            "reason": attempt["blocked_reason"],
            "result_path": str(RESULT_PATH),
            "report_path": str(REPORT_PATH),
            "model_started": False,
            "chat_requests_sent": 0,
            "next_command": "python -X utf8 -B AF011_CHAT_HOST_ACCEPTANCE.py",
        }, ensure_ascii=False, indent=2))
        return 3

    attempt, exit_code = _host_attempt()
    _append_attempt(attempt)
    print(json.dumps({
        "status": attempt["status"],
        "state": attempt.get("state"),
        "result_path": str(RESULT_PATH),
        "report_path": str(REPORT_PATH),
        "model_started": attempt.get("service", {}).get("started", False),
        "model_ready": attempt.get("service", {}).get("model_ready", False),
        "chat_requests": attempt.get("request_counts"),
        "service_exit_code": attempt.get("service", {}).get("exit_code"),
        "port_released": attempt.get("service", {}).get("port_released"),
        "integrity_passed": attempt.get("integrity", {}).get("passed"),
    }, ensure_ascii=False, indent=2))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
