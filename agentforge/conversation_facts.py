"""保守提取持久化对话陈述，并保留来源关联。

这些记录属于不可信对话上下文，绝不是仓库源码证据。
提取器会优先避免误报，不会把问题或
助手的说法作为持久用户事实。
"""
from __future__ import annotations

import hashlib
import re


_CUE = re.compile(
    r"(?i)(?:\b(?:decision|decided|constraint|must|must not|should not|do not|don't|"
    r"never|requirement|open task|todo|correction|corrected|prefer|preference|"
    r"project fact|keep|preserve|chosen|will use)\b|"
    r"(?:必须|不得|不要|不能|约束|决策|决定|选择|保留|保持|改为|纠正|偏好|"
    r"未完成|待办|任务|要求|项目事实))"
)
_STATEMENT = re.compile(
    r"(?i)(?:\b(?:is|are|was|were|equals?|returns?|stores?|uses?|means?|remains?|"
    r"changed to|set to|requires?|configured as)\b|"
    r"(?:是|为|等于|返回|保存|使用|表示|保持|改成|设置为|需要|配置为))"
)
_DISTINCTIVE = re.compile(
    r"\b(?:[A-Z][A-Z0-9_]{2,}|[A-Za-z_][A-Za-z0-9_]*\d[A-Za-z0-9_]*|"
    r"[a-z]+(?:[A-Z][A-Za-z0-9]*)+)\b"
)
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?。！？])\s+|\r?\n+")
_FACT_SECTION = "Source-linked protected statements (untrusted user conversation context; not code evidence):"


def _fact_sentences(text: str):
    for part in _SENTENCE_SPLIT.split(text):
        candidate = " ".join(part.split()).strip(" \t\r\n-*•")
        if candidate:
            yield candidate


def extract_message_facts(rows):
    """只从已完成的用户消息中提取看似稳定的陈述。"""
    output = []
    for row in rows:
        if row.get("role") != "user" or row.get("status", "completed") != "completed":
            continue
        text = row.get("content")
        if not isinstance(text, str) or not text.strip():
            continue
        source_id = str(row.get("id") or "")
        sequence = row.get("sequence")
        if not source_id or isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
            continue
        source_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        for sentence in _fact_sentences(text):
            if len(sentence) > 512:
                # 不要保存被静默截断的陈述。
                if _CUE.search(sentence) or (_STATEMENT.search(sentence) and _DISTINCTIVE.search(sentence)):
                    raise ValueError("Important conversation statement exceeds the safe fact size.")
                continue
            explicit = bool(_CUE.search(sentence))
            declarative_detail = bool(_STATEMENT.search(sentence) and _DISTINCTIVE.search(sentence))
            uppercase_fact = bool(re.search(r"\b[A-Z][A-Z0-9_]{2,}\b", sentence))
            if not (explicit or declarative_detail or uppercase_fact):
                continue
            if sentence.endswith("?") and not explicit:
                continue
            normalized = " ".join(sentence.casefold().split())
            fact_id = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]
            output.append({
                "fact_id": fact_id,
                "text": sentence,
                "kind": _kind(sentence),
                "sources": [{"message_id": source_id, "sequence": sequence,
                             "source_sha256": source_hash}],
            })
    return output


def _kind(text: str) -> str:
    if re.search(r"(?i)\b(?:decision|decided|chosen)\b|(?:决策|决定|选择)", text):
        return "decision"
    if re.search(r"(?i)\b(?:must|must not|should not|do not|don't|never|constraint|requirement)\b|(?:必须|不得|不要|不能|约束|要求)", text):
        return "constraint"
    if re.search(r"(?i)\b(?:open task|todo|unfinished)\b|(?:未完成|待办|任务)", text):
        return "open_task"
    if re.search(r"(?i)\b(?:correction|corrected)\b|(?:纠正|改为)", text):
        return "correction"
    return "user_fact"


def merge_fact_ledgers(*ledgers):
    """按规范化事实身份合并，同时保留全部来源 ID。"""
    merged = {}
    for ledger in ledgers:
        for fact in ledger or ():
            if not isinstance(fact, dict):
                raise ValueError("Conversation fact ledger is malformed.")
            fact_id, text = fact.get("fact_id"), fact.get("text")
            if not isinstance(fact_id, str) or not isinstance(text, str) or not text.strip():
                raise ValueError("Conversation fact ledger is malformed.")
            existing = merged.get(fact_id)
            if existing is None:
                existing = {"fact_id": fact_id, "text": text, "kind": fact.get("kind", "user_fact"), "sources": []}
                merged[fact_id] = existing
            elif existing["text"].casefold() != text.casefold():
                raise ValueError("Conversation fact identity collision; summary was retained.")
            for source in fact.get("sources", ()):
                if not isinstance(source, dict):
                    raise ValueError("Conversation fact provenance is malformed.")
                if source not in existing["sources"]:
                    existing["sources"].append(dict(source))
    result = list(merged.values())
    for fact in result:
        fact["sources"].sort(key=lambda source: (source.get("sequence", 0), source.get("message_id", "")))
    result.sort(key=lambda fact: (fact["sources"][0].get("sequence", 0) if fact["sources"] else 0,
                                  fact["fact_id"]))
    return result


def validate_fact_ledger(ledger):
    if not isinstance(ledger, list):
        raise ValueError("Conversation fact ledger must be a list.")
    normalized = merge_fact_ledgers(ledger)
    for fact in normalized:
        if re.fullmatch(r"[0-9a-f]{24}", fact["fact_id"]) is None:
            raise ValueError("Conversation fact identity is invalid.")
        expected_id = hashlib.sha256(
            " ".join(fact["text"].casefold().split()).encode("utf-8")
        ).hexdigest()[:24]
        if fact["fact_id"] != expected_id:
            raise ValueError("Conversation fact identity does not match its source text.")
        if fact["kind"] not in {"decision", "constraint", "open_task", "correction", "user_fact"}:
            raise ValueError("Conversation fact kind is invalid.")
        if len(fact["text"]) > 512 or not fact["sources"]:
            raise ValueError("Conversation fact source is invalid.")
        for source in fact["sources"]:
            if (not isinstance(source.get("message_id"), str) or not source["message_id"]
                    or isinstance(source.get("sequence"), bool)
                    or not isinstance(source.get("sequence"), int) or source["sequence"] < 1
                    or re.fullmatch(r"[0-9a-f]{64}", str(source.get("source_sha256", ""))) is None):
                raise ValueError("Conversation fact provenance is invalid.")
    return normalized


def render_fact_section(summary: str, ledger):
    """将原始用户陈述及其来源 ID 附加到有界摘要中。"""
    body = summary.split(_FACT_SECTION, 1)[0].rstrip()
    facts = validate_fact_ledger(ledger)
    if not facts:
        return body
    lines = [body, "", _FACT_SECTION]
    for fact in facts:
        source = fact["sources"][0]
        lines.append(f"- [seq {source['sequence']}; message_id {source['message_id']}] {fact['text']}")
    return "\n".join(lines)


def missing_fact_ids(summary: str, ledger):
    """检查合成后的摘要是否保留了准确的原始陈述。"""
    return [fact["fact_id"] for fact in validate_fact_ledger(ledger)
            if fact["text"] not in summary]
