"""Model-independent evidence contract and fail-closed answer verification."""

from __future__ import annotations

from dataclasses import dataclass

from .context import validate_model_citations
from .errors import ProviderError
from .models import Answer, BuiltContext


ANSWER_CONTRACT_VERSION = "af011-answer-contract-v1"
_STATUSES = {"answered", "limited", "refused"}
_CLAIM_KINDS = {"confirmed", "inference", "unknown"}
_RESOLUTION = {"resolved", "ambiguous", "unresolved", "uncertain", "not_applicable"}


@dataclass(frozen=True)
class ContractEvidence:
    source_id: str
    relative_path: str
    start_line: int
    end_line: int
    excerpt: str
    channels: tuple[str, ...]
    resolution_status: str

    @property
    def citation(self) -> str:
        return f"{self.relative_path}:{self.start_line}-{self.end_line}"


@dataclass(frozen=True)
class AnswerClaim:
    text: str
    kind: str
    citations: tuple[str, ...]


@dataclass(frozen=True)
class VerifiedAnswerContract:
    version: str
    status: str
    content: str
    reason: str
    claims: tuple[AnswerClaim, ...]
    evidence: tuple[ContractEvidence, ...]
    route: str
    route_confidence: str


def _provenance(candidate_by_citation, citation: str):
    candidate = candidate_by_citation.get(citation) if candidate_by_citation else None
    channels = tuple(getattr(candidate, "channels", ("lexical",)))
    resolution = getattr(candidate, "resolution_status", "not_applicable")
    return channels, resolution


def _contract_evidence(context: BuiltContext, candidate_by_citation=None) -> tuple[ContractEvidence, ...]:
    values = []
    for source in context.evidence:
        channels, resolution = _provenance(candidate_by_citation, source.citation)
        values.append(ContractEvidence(
            source.source_id, source.result.relative_path,
            source.result.start_line, source.result.end_line,
            source.result.text, channels, resolution,
        ))
    return tuple(values)


def validate_answer_contract(contract: VerifiedAnswerContract, context: BuiltContext) -> None:
    if not isinstance(contract, VerifiedAnswerContract):
        raise ProviderError("Answer contract has an invalid type.")
    if contract.version != ANSWER_CONTRACT_VERSION or contract.status not in _STATUSES:
        raise ProviderError("Answer contract version or status is invalid.")
    context_by_citation = {item.citation: item for item in context.evidence}
    seen = set()
    for evidence in contract.evidence:
        source = context_by_citation.get(evidence.citation)
        if source is None or source.source_id != evidence.source_id:
            raise ProviderError("Answer evidence is absent from this request context.")
        if (evidence.excerpt not in source.result.text
                or evidence.excerpt != source.result.text
                or not evidence.channels
                or evidence.resolution_status not in _RESOLUTION):
            raise ProviderError("Answer evidence text or provenance is invalid.")
        if evidence.citation in seen:
            raise ProviderError("Answer evidence contains a duplicate citation.")
        seen.add(evidence.citation)
    if context.evidence and not contract.evidence and contract.status != "refused":
        raise ProviderError("A non-refused answer must retain request evidence.")
    if not context.evidence and contract.status != "refused":
        raise ProviderError("An answer without evidence must be refused.")
    for claim in contract.claims:
        if claim.kind not in _CLAIM_KINDS or not claim.text.strip():
            raise ProviderError("Answer claim type or text is invalid.")
        if any(citation not in seen for citation in claim.citations):
            raise ProviderError("Answer claim cites evidence outside its contract.")
        if claim.kind in {"confirmed", "inference"} and not claim.citations:
            raise ProviderError("A factual claim must cite request-local evidence.")
        if claim.kind in {"confirmed", "inference"} and claim.text not in contract.content:
            raise ProviderError("A factual claim must appear in the answer content.")
        if claim.kind == "confirmed" and not any(
                claim.text in item.excerpt
                for item in contract.evidence if item.citation in claim.citations):
            raise ProviderError("A confirmed claim must be an exact excerpt from cited evidence.")
    if contract.status == "answered" and not any(
            claim.kind == "confirmed" for claim in contract.claims):
        raise ProviderError("An answered contract requires a confirmed cited claim.")
    if contract.status == "refused" and any(
            claim.kind == "confirmed" for claim in contract.claims):
        raise ProviderError("A refused answer cannot contain confirmed claims.")


def build_offline_contract(answer: Answer, context: BuiltContext, *, route: str,
                           route_confidence: str, candidate_by_citation=None,
                           uncertainty: str | None = None) -> VerifiedAnswerContract:
    evidence = _contract_evidence(context, candidate_by_citation)
    if not evidence or (route_confidence == "low" and uncertainty):
        reason = uncertainty or "No request-local evidence supports an answer."
        contract = VerifiedAnswerContract(
            ANSWER_CONTRACT_VERSION, "refused", answer.text, reason,
            (AnswerClaim("当前证据不足，无法确认代码结论。", "unknown", ()),),
            evidence, route, route_confidence,
        )
    else:
        reason = uncertainty or (
            "Offline mode returns verified evidence excerpts but does not infer a code conclusion."
        )
        contract = VerifiedAnswerContract(
            ANSWER_CONTRACT_VERSION, "limited", answer.text, reason,
            (AnswerClaim("仅提供可追溯证据摘录，未将检索候选断言为事实答案。", "unknown", ()),),
            evidence, route, route_confidence,
        )
    validate_answer_contract(contract, context)
    return contract


def verify_generated_answer(answer: Answer, context: BuiltContext, *, route: str,
                            route_confidence: str, candidate_by_citation=None,
                            uncertainty: str | None = None) -> VerifiedAnswerContract:
    evidence = _contract_evidence(context, candidate_by_citation)
    citations = validate_model_citations(answer.text, context)
    if not evidence or not citations:
        contract = VerifiedAnswerContract(
            ANSWER_CONTRACT_VERSION, "refused", answer.text,
            "Generated text did not contain a request-local verifiable citation.",
            (AnswerClaim("模型回答缺少可验证引用，不能作为有证据答案。", "unknown", ()),),
            evidence, route, route_confidence,
        )
    else:
        # Citation membership proves provenance, not semantic entailment. Keep
        # generated content limited until a separate entailment check exists.
        kind = "inference"
        status = "limited"
        contract = VerifiedAnswerContract(
            ANSWER_CONTRACT_VERSION, status, answer.text,
            uncertainty or (
                "Citations are request-local; semantic entailment has not been verified."
            ),
            (AnswerClaim(answer.text, kind, citations),),
            evidence, route, route_confidence,
        )
    validate_answer_contract(contract, context)
    return contract


__all__ = [
    "ANSWER_CONTRACT_VERSION", "AnswerClaim", "ContractEvidence",
    "VerifiedAnswerContract", "build_offline_contract",
    "validate_answer_contract", "verify_generated_answer",
]
