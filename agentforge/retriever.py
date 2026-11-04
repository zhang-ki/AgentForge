"""可解释的词法评分；不使用 Embedding 或外部服务。"""

from collections import Counter
import re

from .config import MAX_QUERY_LENGTH, MAX_TOP_K
from .models import SearchResult

_WORDS = re.compile(r"[^\W_]+(?:_[^\W_]+)*", re.UNICODE)
_STOP = {"the", "a", "an", "and", "or", "is", "are", "to", "of", "in", "for", "how", "does"}


def terms(text: str) -> list[str]:
    result = []
    for word in _WORDS.findall(text):
        result.append(word.casefold())
        pieces = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", word).replace("_", " ").split()
        if len(pieces) > 1:
            result.extend(piece.casefold() for piece in pieces)
    return result


def search(store, query: str, top_k: int = 5) -> list[SearchResult]:
    if not isinstance(query, str) or not query.strip():
        raise ValueError("Query must not be empty.")
    if len(query) > MAX_QUERY_LENGTH:
        raise ValueError(f"Query cannot exceed {MAX_QUERY_LENGTH} characters.")
    if not 1 <= top_k <= MAX_TOP_K:
        raise ValueError(f"top_k must be between 1 and {MAX_TOP_K}.")
    query_terms = sorted(set(terms(query)) - _STOP)
    if not query_terms:
        raise ValueError("Query has no searchable terms.")
    raw_terms = sorted({word.casefold() for word in _WORDS.findall(query)} - _STOP)
    repository = store.metadata().get("repository", "")
    results = []
    for row in store.iter_chunks():
        content_terms = Counter(terms(row["text"]))
        path_terms = set(terms(row["relative_path"]))
        # 每个词项按有限词频与路径命中计分；完整查询词项另有
        # 会额外获得精确标识符匹配加分。
        score = sum(min(content_terms[term], 5) + (3 if term in path_terms else 0)
                    for term in query_terms)
        score += sum(4 for term in raw_terms if re.search(
            r"(?<!\w)" + re.escape(term) + r"(?!\w)", row["text"], re.IGNORECASE
        ))
        score += sum(8 for term in raw_terms if re.search(
            r"(?m)^\s*(?:async\s+)?(?:def|class|function)\s+" + re.escape(term) + r"\b",
            row["text"], re.IGNORECASE,
        ))
        # 支持中文关键词子串匹配，但不声称完成了中文分词。
        score += sum(2 * min(row["text"].casefold().count(term), 5)
                     for term in query_terms if re.search(r"[\u3400-\u9fff]", term))
        if query.strip().casefold() in row["relative_path"].casefold():
            score += 8
        if score:
            results.append(SearchResult(
                repository=repository,
                relative_path=row["relative_path"],
                start_line=row["start_line"],
                end_line=row["end_line"],
                text=row["text"],
                content_hash=row["content_hash"],
                score=float(score),
            ))
    results.sort(key=lambda item: (-item.score, item.relative_path, item.start_line, item.end_line))
    unique = []
    seen = set()
    for result in results:
        if result.content_hash in seen:
            continue
        seen.add(result.content_hash)
        unique.append(result)
        if len(unique) == top_k:
            break
    return unique

