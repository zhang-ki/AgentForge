"""为现有结构化 ask 命令提供便于阅读的展示格式。"""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import re
import sys

from . import cli


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
    if "--stream" in arguments:
        banner_written = False
        def stream_callback(piece):
            nonlocal banner_written
            if not banner_written:
                original_stdout.write("DeepSeek 草稿（尚未通过引用验证）：\n")
                banner_written = True
            original_stdout.write(piece)
            original_stdout.flush()
    with redirect_stdout(output), redirect_stderr(errors):
        try:
            code = (cli.main(arguments, stream_callback=stream_callback)
                    if stream_callback is not None else cli.main(arguments))
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
    if code:
        error_text = errors.getvalue().strip()
        if error_text:
            print(error_text, file=sys.stderr)
        else:
            try:
                failure = render_execution_failure(json.loads(output.getvalue()))
            except (ValueError, TypeError):
                failure = None
            print(failure or "AgentForge 请求失败。", file=sys.stderr)
        return code
    try:
        result = json.loads(output.getvalue())
        rendered = render_ask_result(result)
    except (ValueError, TypeError, KeyError, AttributeError):
        print("无法读取 AgentForge 的结构化结果，未显示为成功。", file=sys.stderr)
        return 1
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
