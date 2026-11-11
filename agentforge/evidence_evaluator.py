"""从检索候选计算来源可用性；不声称证明自然语言语义蕴含。"""

from dataclasses import dataclass
import re

from .evidence_roles import role_coverage


@dataclass(frozen=True)
class EvidenceAssessment:
    candidate_count: int
    relevant_candidates: tuple[object, ...]
    required_roles: tuple[str, ...]
    available_roles: tuple[str, ...]
    missing_roles: tuple[str, ...]
    relevance: str
    sufficiency: str
    reason: str
    candidate_results: tuple[dict, ...] = ()


_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "can", "do", "does",
    "for", "from", "function", "how", "in", "is", "it", "its", "method", "of",
    "on", "or", "return", "returns", "the", "this", "to", "what", "where", "which",
    "with", "class", "代码", "函数", "方法", "项目", "如何", "怎么", "什么", "哪里",
}


def _relevance_signal(question, candidate):
    path = getattr(candidate, "relative_path", "")
    text = getattr(candidate, "text", "")
    if not isinstance(path, str) or not isinstance(text, str):
        return (), "candidate lacks verifiable text or path metadata"
    source = f"{path}\n{text}"
    query_identifiers = {
        token.casefold() for token in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", question)
        if token.casefold() not in _STOPWORDS and len(token) >= 3
    }
    source_identifiers = {
        token.casefold() for token in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", source)
    }
    exact = query_identifiers & source_identifiers
    if exact:
        return tuple(sorted(exact)), "exact source identifier overlap"

    query_words = {word for word in query_identifiers if len(word) >= 3}
    source_words = source_identifiers
    overlap = query_words & source_words
    distinctive = {word for word in overlap if len(word) >= 7 or "_" in word}
    if len(overlap) >= 2 or distinctive:
        return tuple(sorted(overlap)), "multiple or distinctive source terms overlap"

    query_cjk = "".join(re.findall(r"[\u3400-\u9fff]", question))
    source_cjk = "".join(re.findall(r"[\u3400-\u9fff]", source))
    query_pairs = {query_cjk[index:index + 2] for index in range(max(0, len(query_cjk) - 1))}
    source_pairs = {source_cjk[index:index + 2] for index in range(max(0, len(source_cjk) - 1))}
    cjk_overlap = query_pairs & source_pairs
    if len(cjk_overlap) >= 2:
        return tuple(sorted(cjk_overlap)), "multiple Chinese character pairs overlap"
    return (), "no deterministic relevance signal; candidate remains unverified"


def assess_evidence(question: str, decision, candidates) -> EvidenceAssessment:
    """Apply conservative lexical relevance checks, then independently assess role sufficiency.

    This is a relevance signal, not an LLM semantic judge or proof of entailment.
    Candidates with no deterministic match remain unverified and are not sent as
    repository evidence.
    """
    candidates = tuple(candidates)
    confidence = getattr(decision, "confidence", "low")
    results = []
    relevant = []
    for candidate in candidates:
        score = getattr(candidate, "score", None)
        valid_score = (isinstance(score, (int, float)) and not isinstance(score, bool)
                       and score > 0)
        matched, reason = _relevance_signal(question, candidate)
        reason_code = "RELEVANCE_SIGNAL"
        status = "relevant" if valid_score and matched and confidence != "low" else "unverified"
        if not valid_score:
            reason = "candidate has no valid positive retrieval score"
            reason_code = "INVALID_RETRIEVAL_SCORE"
            status = "unverified"
        elif reason == "candidate lacks verifiable text or path metadata":
            reason_code = "CANDIDATE_METADATA_MISSING"
            status = "unverified"
        elif confidence == "low":
            reason = "low-confidence retrieval route; lexical overlap is not promoted"
            reason_code = "LOW_CONFIDENCE_ROUTE"
            status = "unverified"
        elif not matched:
            reason_code = "NO_DETERMINISTIC_RELEVANCE"
        results.append({"candidate": candidate, "status": status,
                        "matched_terms": matched, "reason": reason,
                        "reason_code": reason_code})
        if status == "relevant":
            relevant.append(candidate)

    required = role_coverage(
        question,
        (role for candidate in relevant for role in candidate.evidence_roles),
    )
    if not relevant:
        return EvidenceAssessment(
            len(candidates), (), tuple(required["required_roles"]),
            tuple(required["available_roles"]), tuple(required["missing_available_roles"]),
            "unverified", "insufficient",
            "No candidate passed the conservative relevance check; semantic relevance is unverified.",
            tuple(results),
        )
    missing = tuple(required["missing_available_roles"])
    if missing:
        return EvidenceAssessment(
            len(candidates), tuple(relevant), tuple(required["required_roles"]),
            tuple(required["available_roles"]), missing, "route_supported", "partial",
            "Relevant candidates exist, but required source roles are missing.",
            tuple(results),
        )
    return EvidenceAssessment(
        len(candidates), tuple(relevant), tuple(required["required_roles"]),
        tuple(required["available_roles"]), (), "route_supported", "sufficient",
        "Required source roles are present; semantic entailment is not established by this check.",
        tuple(results),
    )


__all__ = ["EvidenceAssessment", "assess_evidence"]
