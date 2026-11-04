"""有界证据组装与精确引用成员校验。"""

from pathlib import PurePosixPath
import re

from .models import BuiltContext, Evidence, SearchResult
from .errors import CitationValidationError, ProviderError


def build_context(
    question: str,
    results: list[SearchResult],
    *,
    max_candidates: int = 5,
    budget: int = 6000,
) -> BuiltContext:
    if not 1 <= max_candidates <= 100 or not 1 <= budget <= 1_000_000:
        raise ValueError("Context candidate count or estimated budget is out of range.")
    evidence = []
    blocks = []
    seen = set()
    used = 0
    for result in results[:max_candidates]:
        path = PurePosixPath(result.relative_path)
        if (path.is_absolute() or ".." in path.parts or "\\" in result.relative_path
                or ":" in result.relative_path or any(c in result.relative_path for c in "\r\n[]")
                or result.start_line < 1 or result.end_line < result.start_line):
            raise ValueError("Invalid evidence path or line range.")
        if result.content_hash in seen:
            continue
        seen.add(result.content_hash)
        source = Evidence(f"E{len(evidence) + 1}", result)
        block = f"[{source.source_id}] [[{source.citation}]]\n{result.text}\n"
        cost = len(block.encode("utf-8"))
        if used + cost > budget:
            continue
        used += cost
        evidence.append(source)
        blocks.append(block)
    return BuiltContext(question, tuple(evidence), "".join(blocks), used, budget)


def validate_citations(citations, context: BuiltContext) -> None:
    allowed = {source.citation for source in context.evidence}
    if any(citation not in allowed for citation in citations):
        raise ProviderError("Answer contains a citation absent from the retrieved evidence.")


def validate_model_citations(text: str, context: BuiltContext) -> tuple[str, ...]:
    """将请求内的 ``[E#]`` 标记映射到规范化证据引用。

    模型不能决定路径或行范围；这些身份只能来自
    当前请求的 :class:`BuiltContext`。明显的路径/行号引用尝试会被
    拒绝；普通文字（如 ``line 12``）或类型注解则不会被
    当作引用处理。
    """
    if not isinstance(text, str):
        raise ProviderError("Model answer must be text.")

    by_id = {source.source_id: source.citation for source in context.evidence}
    citations: list[str] = []
    matched_source_ids: list[str] = []
    validated_source_ids: list[str] = []

    # 提示词会展示 [E1] 这类来源标签。必须验证每个以 E 开头的标记，
    # 也要检查宽松正则表达式可能悄悄漏掉的格式错误标记。
    cursor = 0
    while True:
        upper_start = text.find("[E", cursor)
        lower_start = text.find("[e", cursor)
        if upper_start < 0:
            start = lower_start
        elif lower_start < 0:
            start = upper_start
        else:
            start = min(upper_start, lower_start)
        if start < 0:
            break
        end = text.find("]", start + 2)
        if end < 0 or "\n" in text[start:end] or "\r" in text[start:end]:
            _citation_failure(
                text, code="malformed_source_id",
                message="Model output contains a malformed source ID.",
                position=start, matched_source_ids=matched_source_ids,
                validated_source_ids=validated_source_ids,
            )
        source_id = text[start + 1:end]
        recorded_source_id = source_id if len(source_id) <= 32 else source_id[:32] + "..."
        matched_source_ids.append(recorded_source_id)
        if not re.fullmatch(r"E[1-9]\d*", source_id) or source_id not in by_id:
            _citation_failure(
                text, code="unknown_source_id",
                message="Model output contains a source ID absent from this request.",
                position=start, matched_source_ids=matched_source_ids,
                validated_source_ids=validated_source_ids,
            )
        citations.append(by_id[source_id])
        validated_source_ids.append(source_id)
        cursor = end + 1

    # 绝不接受模型提供的旧式路径标记；仓库路径和行号范围
    # 由应用程序提供，而不是由模型生成。
    legacy_positions = [position for position in (text.find("[["), text.find("]]")) if position >= 0]
    if legacy_positions:
        position = min(legacy_positions)
        code = "legacy_path_citation_forbidden"
        marker = re.search(r"\[\[([^\r\n\[\]]+)\]\]", text[position:])
        if marker:
            parsed = re.fullmatch(r"([^:]+):([1-9]\d*)-([1-9]\d*)", marker.group(1))
            if parsed:
                if len(parsed.group(2)) > 10 or len(parsed.group(3)) > 10:
                    code = "out_of_range_line_number"
                elif int(parsed.group(3)) < int(parsed.group(2)):
                    code = "invalid_line_range"
        _citation_failure(
            text, code=code,
            message="Model output must cite request evidence by source ID only.",
            position=position, matched_source_ids=matched_source_ids,
            validated_source_ids=validated_source_ids,
        )

    # 只拒绝明确的“路径加位置”引用。
    # “line 12”、时间、类型注解、文件名和 Markdown 都仍是
    # 普通技术文本，不能被误判为引用。
    path_location = _BARE_PATH_LOCATION.search(text)
    if path_location:
        starts = [path_location.group(name) for name in (
            "colon_start", "hash_start", "word_start",
        ) if path_location.group(name)]
        ends = [path_location.group(name) for name in (
            "colon_end", "hash_end", "word_end",
        ) if path_location.group(name)]
        code = "model_supplied_path_location_forbidden"
        if starts and any(len(value) > 10 for value in starts + ends):
            code = "out_of_range_line_number"
        elif starts and ends and int(ends[0]) < int(starts[0]):
            code = "invalid_line_range"
        _citation_failure(
            text, code=code,
            message="Model output must not supply repository paths or line locations.",
            position=path_location.start(), matched_source_ids=matched_source_ids,
            validated_source_ids=validated_source_ids,
        )

    return tuple(dict.fromkeys(citations))


_BARE_PATH_LOCATION = re.compile(
    r"(?<![\w/.-])"
    r"(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+\."
    r"(?:py|md|rst|txt|toml|json|ya?ml|ini|cfg|tsx?|jsx?|java|go|rs|"
    r"c|cc|cpp|h|hpp|cs|rb|php|swift|kt|kts|scala|sh|ps1)"
    r"(?::(?P<colon_start>[1-9]\d*)(?:-(?P<colon_end>[1-9]\d*))?"
    r"|#L(?P<hash_start>[1-9]\d*)(?:-L?(?P<hash_end>[1-9]\d*))?"
    r"|\s+(?:lines?|行)\s*(?P<word_start>[1-9]\d*)"
    r"(?:\s*[-–]\s*(?P<word_end>[1-9]\d*))?)"
    r"(?![\w])",
    re.IGNORECASE,
)


def _citation_failure(
    text: str, *, code: str, message: str, position: int,
    matched_source_ids: list[str], validated_source_ids: list[str],
) -> None:
    start = max(0, position - 32)
    end = min(len(text), position + 64)
    snippet = text[start:end].replace("\r", "\\r").replace("\n", "\\n")
    snippet = re.sub(
        r"(?i)(api[_-]?key|authorization|password|secret|token)(\s*[:=]\s*)([^\s,;]+)",
        r"\1\2<redacted>", snippet,
    )
    raise CitationValidationError(
        message,
        code=code,
        diagnostic={
            "rule_id": code,
            "position": position,
            "snippet": snippet,
            "snippet_truncated": start > 0 or end < len(text),
            "matched_source_ids": tuple(dict.fromkeys(matched_source_ids)),
            "validated_source_ids": tuple(dict.fromkeys(validated_source_ids)),
            "validation_result": "rejected",
        },
    )

