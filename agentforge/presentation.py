"""为现有结构化 ask 命令提供便于阅读的展示格式。"""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import re
import sys

from . import cli
from .observability import RequestLifecycleTracker, bind_lifecycle, default_log_path


def render_ask_result(result: dict) -> str:
    """展示合同状态和请求证据，但不改变已有判定。"""
    contract = result["answer_contract"]
    status = contract["status"]
    evidence = contract["evidence"]
    provider = result.get("provider")
    if status == "refused":
        if provider in {"llama-server", "deepseek"}:
            lines = ["未能生成通过证据校验的回答。", "模型原文（未经验证，不作为已确认答案）：",
                     result.get("refused_output") or "（无回答）"]
        else:
            lines = ["证据不足，未能回答。", "本次没有使用模型生成答案。"]
    elif status in ("answered", "limited"):
        lines = ["回答：", result.get("answer") or "（无回答）"]
        if status == "limited":
            lines.append("提示：这是有限结论；引用有效不代表每句话都已证明，请结合源码核对。")
    else:
        raise ValueError("Unknown answer contract status")

    retrieval = result.get("retrieval")
    lifecycle = retrieval.get("index_lifecycle") if isinstance(retrieval, dict) else None
    if isinstance(lifecycle, dict):
        state = lifecycle.get("state")
        reason = lifecycle.get("reason_code")
        if state == "DEGRADED_LEXICAL_ONLY":
            lines.append("\n检索状态：向量检索暂不可用，当前使用 SQLite 词法检索。"
                         + (f"（{reason}）" if isinstance(reason, str) else ""))
        elif state == "STALE":
            lines.append("\n检索状态：索引已变化或向量索引不完整；仅使用经过 SQLite 来源与哈希校验的结果。")
        elif state == "VECTOR_HEALTH_UNKNOWN":
            lines.append("\n检索状态：向量索引完整性暂未验证。")
        elif state == "LEXICAL_ONLY" and reason == "vector_not_requested":
            lines.append("\n检索状态：本次请求使用 SQLite 词法检索。")
        elif state == "READY":
            lines.append("\n检索状态：SQLite 与 Chroma 向量索引一致。")

    cited = {citation for claim in contract.get("claims", [])
             for citation in claim.get("citations", [])}
    if evidence:
        lines.append("\n本次找到的源码证据：")
        for item in evidence:
            citation = f"{item['relative_path']}:{item['start_line']}-{item['end_line']}"
            label = "回答已引用" if citation in cited else "找到的证据，回答未引用"
            lines.append(f"[{item['source_id']}] {citation}（{label}）")
            if item.get("resolution_status") in ("ambiguous", "unresolved", "uncertain"):
                lines.append("  注意：静态关系存在不确定性，请查看源码。")
    else:
        lines.append("\n本次未找到可用的源码证据。")
    stats = result.get("provider_request_stats") or {}
    usage = stats.get("last_usage") if isinstance(stats, dict) else None
    if isinstance(usage, dict) and usage:
        fields = ("prompt_tokens", "completion_tokens", "total_tokens",
                  "prompt_cache_hit_tokens", "prompt_cache_miss_tokens", "reasoning_tokens")
        values = [f"{name}={usage[name]}" for name in fields
                  if isinstance(usage.get(name), int) and not isinstance(usage.get(name), bool)]
        if values:
            lines.append("\n模型实际 Token 用量：" + ", ".join(values))
    conversation_context = result.get("conversation_context")
    estimate = conversation_context.get("token_estimate") if isinstance(conversation_context, dict) else None
    if isinstance(estimate, dict):
        lines.append("上下文估算：" + str(estimate.get("input_tokens")) + " tokens（"
                     + str(estimate.get("method", "estimated")) + "；非 API 实际用量）")
    return "\n".join(lines)


def render_execution_failure(result: dict) -> str | None:
    """Show safe execution metadata when ask returned a non-completed JSON result.

    Tool outputs, arguments, prompts, and arbitrary exception text are deliberately
    excluded: this view is for locating the failed phase without exposing content.
    """
    status = result.get("execution_status")
    if not isinstance(status, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", status):
        return None

    execution = result.get("agent_execution")
    if not isinstance(execution, dict):
        return f"AgentForge 执行未完成（状态：{status}）。"

    execution_status = execution.get("status")
    execution_state = execution.get("state")
    error_code = execution.get("error_code")
    failure_phase = execution.get("failure_phase")
    lines = [f"AgentForge 执行未完成（状态：{status}）。"]
    if isinstance(execution_status, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", execution_status):
        lines.append(f"执行结果：{execution_status}")
    if isinstance(execution_state, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", execution_state):
        lines.append(f"执行阶段状态：{execution_state}")
    if isinstance(failure_phase, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", failure_phase):
        lines.append(f"失败阶段：{failure_phase}")
    if isinstance(error_code, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", error_code):
        lines.append(f"错误代码：{error_code}")

    failed_tools = []
    results = execution.get("results")
    if isinstance(results, list):
        for item in results:
            if not isinstance(item, dict) or item.get("status") in {"completed", "succeeded"}:
                continue
            tool_id = item.get("tool_id")
            tool_status = item.get("status")
            tool_error = item.get("error_code")
            failure_phase = item.get("failure_phase")
            if not isinstance(tool_id, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", tool_id):
                tool_id = "unknown"
            details = [tool_id]
            if failure_phase == "plan_validation":
                details.append("计划校验拒绝")
            elif isinstance(tool_status, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", tool_status):
                details.append(tool_status)
            if isinstance(tool_error, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", tool_error):
                details.append(tool_error)
            failed_tools.append(" / ".join(details))
    if failed_tools:
        lines.append("工具结果：" + "; ".join(failed_tools[:8]))
    if status == "waiting_approval":
        lines.append("执行已暂停，等待审批。")
    elif status == "waiting_user_input":
        lines.append("执行已暂停，等待补充信息。")
    elif status == "cancelled":
        lines.append("执行已取消。")
    return "\n".join(lines)


def main(arguments=None) -> int:
    arguments = sys.argv[1:] if arguments is None else arguments
    if not arguments or arguments[0] != "ask":
        print("仅支持展示 ask 命令结果。", file=sys.stderr)
        return 2
    output, errors = io.StringIO(), io.StringIO()
    stream_callback = None
    original_stdout = sys.stdout
    original_stderr = sys.stderr
    lifecycle = None
    debug_enabled = "--debug" in arguments
    if "--stream" in arguments:
        banner_written = False
        def stream_callback(piece):
            nonlocal banner_written
            if not banner_written:
                original_stdout.write("DeepSeek 草稿（尚未通过引用验证）：\n")
                banner_written = True
            original_stdout.write(piece)
            original_stdout.flush()
    labels = _request_labels(arguments)
    lifecycle = RequestLifecycleTracker(
        console_stream=original_stderr if debug_enabled else None,
        log_path=default_log_path(),
    )
    lifecycle.start_request(**labels)
    if not debug_enabled:
        print("处理中…", file=original_stdout, flush=True)
    result = None
    code = 1
    try:
        with bind_lifecycle(lifecycle):
            with redirect_stdout(output), redirect_stderr(errors):
                try:
                    code = (cli.main(arguments, stream_callback=stream_callback)
                            if stream_callback is not None else cli.main(arguments))
                except SystemExit as exc:
                    code = exc.code if isinstance(exc.code, int) else 1
        raw_output = output.getvalue()
        try:
            result = json.loads(raw_output)
        except (ValueError, TypeError):
            result = None
        if code:
            error_text = errors.getvalue().strip()
            if error_text:
                print(error_text, file=original_stderr, flush=True)
            else:
                failure = render_execution_failure(result) if isinstance(result, dict) else None
                print(failure or "AgentForge 请求失败。", file=original_stderr, flush=True)
            status, error_code, metadata = _terminal_state(code, result, errors.getvalue())
            lifecycle.finish(status, error_code=error_code, metadata=metadata)
            return code
        try:
            if not isinstance(result, dict):
                raise ValueError("The ask command did not return a structured result.")
            rendered = render_ask_result(result)
        except (ValueError, TypeError, KeyError, AttributeError):
            print("无法读取 AgentForge 的结构化结果，未显示为成功。", file=original_stderr, flush=True)
            lifecycle.finish("FAILED", error_code="invalid_cli_result")
            return 1
        print(rendered, file=original_stdout, flush=True)
        contract = result.get("answer_contract")
        contract_status = contract.get("status") if isinstance(contract, dict) else None
        lifecycle.finish("FAILED" if contract_status == "refused" else "COMPLETED",
                         error_code="answer_contract_refused" if contract_status == "refused" else None,
                         metadata={
            "answer_contract_status": contract_status,
            "answer_produced": bool(result.get("answer")),
            "execution_status": result.get("execution_status"),
            "failure_stage": "answer_contract" if contract_status == "refused" else None,
        })
        return 0
    except BaseException as exc:
        lifecycle.finish("TIMEOUT" if isinstance(exc, TimeoutError) else "FAILED",
                         error_code=_exception_code(exc))
        raise
    finally:
        if lifecycle is not None:
            lifecycle.finish("FAILED", error_code="presentation_incomplete")


def _request_labels(arguments):
    """Extract only model/session labels from the ask arguments; never retain the query."""
    values = {"provider": "offline", "model": None, "session_id": None,
              "streaming_enabled": "--stream" in arguments}
    for option, key in (("--provider", "provider"), ("--model", "model"),
                        ("--conversation-id", "session_id"), ("--mode", "mode")):
        try:
            position = arguments.index(option)
            candidate = arguments[position + 1]
        except (ValueError, IndexError):
            continue
        if isinstance(candidate, str) and len(candidate) <= 256:
            values[key] = candidate
    return values


def _terminal_state(code, result, error_output):
    if isinstance(result, dict):
        execution_status = result.get("execution_status")
        if execution_status in {"waiting_approval", "waiting_user_input"}:
            return "PENDING", None, {"execution_status": execution_status}
        if execution_status == "cancelled":
            return "CANCELLED", None, {"execution_status": execution_status}
    if code == 0:
        return "COMPLETED", None, {}
    error_code = _diagnostic_error_code(error_output)
    if "timeout" in error_code.casefold():
        return "TIMEOUT", error_code, {}
    if "cancel" in error_code.casefold():
        return "CANCELLED", error_code, {}
    return "FAILED", error_code, {}


def _diagnostic_error_code(text):
    """Extract only a bounded machine error code from a structured CLI diagnostic."""
    if not isinstance(text, str):
        return "request_failed"
    for prefix in ("AF_PROVIDER_DIAGNOSTIC=", "AF_PLANNER_DIAGNOSTIC=",
                   "AF_CONTEXT_BUDGET_DIAGNOSTIC="):
        for line in text.splitlines():
            if not line.startswith(prefix):
                continue
            try:
                payload = json.loads(line[len(prefix):])
            except (ValueError, TypeError):
                continue
            if not isinstance(payload, dict):
                continue
            for key in ("error_code", "provider_failure_code", "planning_failure_code",
                        "failure_code"):
                value = payload.get(key)
                if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", value):
                    return value
    return "request_failed"


def _exception_code(exc):
    value = getattr(exc, "code", None) or getattr(exc, "diagnostic_code", None)
    return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", value) else type(exc).__name__


if __name__ == "__main__":
    raise SystemExit(main())
