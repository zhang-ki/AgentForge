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
        if provider == "llama-server":
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
    return "\n".join(lines)


def main(arguments=None) -> int:
    arguments = sys.argv[1:] if arguments is None else arguments
    if not arguments or arguments[0] != "ask":
        print("仅支持展示 ask 命令结果。", file=sys.stderr)
        return 2
    output, errors = io.StringIO(), io.StringIO()
    with redirect_stdout(output), redirect_stderr(errors):
        try:
            code = cli.main(arguments)
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
