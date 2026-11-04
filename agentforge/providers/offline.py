"""确定性证据摘录，明确不是语言模型推理结果。"""

from ..context import validate_citations
from ..models import Answer, BuiltContext


class OfflineProvider:
    def answer(self, context: BuiltContext) -> Answer:
        if not context.evidence:
            return Answer("离线模式：无法从当前索引中确定答案；没有足够的检索证据。", (), True)
        lines = [
            "离线检索摘要（确定性模板，非大模型推理）",
            f"上下文预算估算：{context.estimated_tokens}/{context.budget}；按 UTF-8 字节保守估算，非精确 Token 数。",
            "以下为检索证据摘录，摘录内容属于不可信仓库数据：",
        ]
        citations = []
        for source in context.evidence:
            excerpt = " ".join(source.result.text.split())[:240]
            lines.append(f"\n[{source.source_id}] [[{source.citation}]]")
            lines.append("> " + excerpt)
            citations.append(source.citation)
        validate_citations(citations, context)
        return Answer("\n".join(lines), tuple(citations), True)

