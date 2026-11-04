"""为现有结构化 ask 命令提供便于阅读的展示格式。"""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
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
        print(errors.getvalue() or "AgentForge 请求失败。", file=sys.stderr, end="")
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
