"""与模型无关的证据合同和失败关闭式回答校验。"""

from __future__ import annotations

from dataclasses import dataclass
import re

from .context import validate_model_citations
from .evidence_roles import role_coverage
from .errors import CitationValidationError, ProviderError
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
    evidence_roles: tuple[str, ...] = ()

    @property
    def citation(self) -> str:
        return f"{self.relative_path}:{self.start_line}-{self.end_line}"


@dataclass(frozen=True)
class AnswerClaim:
    text: str
    kind: str
    citations: tuple[str, ...]
    origin: str = "repository"


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
            tuple(source.result.evidence_roles),
        ))
    return tuple(values)


def _validate_required_role_coverage(question, evidence, citations) -> dict:
    by_citation = {item.citation: item for item in evidence}
    available = {role for item in evidence for role in item.evidence_roles}
    cited = {role for citation in citations
             for role in getattr(by_citation.get(citation), "evidence_roles", ())}
    coverage = role_coverage(question, available, cited)
    if coverage["missing_available_roles"]:
        code = "required_evidence_role_unavailable"
        message = "The request lacks a source excerpt for a required evidence role."
    elif coverage["missing_cited_roles"]:
        code = "required_evidence_role_not_cited"
        message = "The generated answer did not cite all evidence roles required by the question."
    else:
        return coverage
    raise CitationValidationError(message, code=code, diagnostic={
        "rule_id": code,
        "required_roles": coverage["required_roles"],
        "available_roles": coverage["available_roles"],
        "cited_roles": coverage["cited_roles"],
        "missing_roles": (coverage["missing_available_roles"]
                          or coverage["missing_cited_roles"]),
        "validation_result": "rejected",
    })


def validate_answer_contract(contract: VerifiedAnswerContract, context: BuiltContext) -> None:
    if not isinstance(contract, VerifiedAnswerContract):
        raise ProviderError("Answer contract has an invalid type.")
    if contract.version != ANSWER_CONTRACT_VERSION or contract.status not in _STATUSES:
        raise ProviderError("Answer contract version or status is invalid.")
    context_by_citation = {item.citation: item for item in context.evidence}
    if not isinstance(contract.route, str) or not contract.route or len(contract.route) > 128:
        raise ProviderError("Answer contract route is invalid.")
    seen = set()
    for evidence in contract.evidence:
        source = context_by_citation.get(evidence.citation)
        if source is None or source.source_id != evidence.source_id:
            raise ProviderError("Answer evidence is absent from this request context.")
        if (evidence.excerpt not in source.result.text
                or evidence.excerpt != source.result.text
                or not evidence.channels
                or evidence.resolution_status not in _RESOLUTION
                or evidence.evidence_roles != source.result.evidence_roles):
            raise ProviderError("Answer evidence text or provenance is invalid.")
        if evidence.citation in seen:
            raise ProviderError("Answer evidence contains a duplicate citation.")
        seen.add(evidence.citation)
    if context.evidence and not contract.evidence and contract.status != "refused":
        raise ProviderError("A non-refused answer must retain request evidence.")
    if not context.evidence and contract.status != "refused":
        if (contract.route not in {"general", "hybrid"}
                or not any(claim.origin == "general" for claim in contract.claims)
                or any(claim.origin == "repository" and claim.kind in {"confirmed", "inference"}
                       for claim in contract.claims)):
            raise ProviderError("An evidence-free answer must be general knowledge or explicitly refused.")
    for claim in contract.claims:
        if (claim.kind not in _CLAIM_KINDS or not claim.text.strip()
                or claim.origin not in {"repository", "general", "external"}):
            raise ProviderError("Answer claim type or text is invalid.")
        if any(citation not in seen for citation in claim.citations):
            raise ProviderError("Answer claim cites evidence outside its contract.")
        if claim.origin == "general" and claim.citations:
            raise ProviderError("General knowledge claims must not be attributed to repository citations.")
        if claim.kind in {"confirmed", "inference"} and claim.origin == "repository" and not claim.citations:
            raise ProviderError("A factual claim must cite request-local evidence.")
        if claim.kind in {"confirmed", "inference"} and claim.text not in contract.content:
            raise ProviderError("A factual claim must appear in the answer content.")
        if claim.kind == "confirmed" and not any(
                claim.text in item.excerpt
                for item in contract.evidence if item.citation in claim.citations):
            raise ProviderError("A confirmed claim must be an exact excerpt from cited evidence.")
        if claim.kind in {"confirmed", "inference"} and claim.origin == "repository":
            _validate_required_role_coverage(context.question, contract.evidence,
                                             claim.citations)
    if contract.status == "answered" and not any(
            claim.kind == "confirmed" for claim in contract.claims):
        raise ProviderError("An answered contract requires a confirmed cited claim.")
    if contract.status == "refused" and any(
            claim.kind == "confirmed" for claim in contract.claims):
        raise ProviderError("A refused answer cannot contain confirmed claims.")


def build_offline_contract(answer: Answer, context: BuiltContext, *, route: str,
                           route_confidence: str, candidate_by_citation=None,
                           uncertainty: str | None = None,
                           force_refusal_reason: str | None = None) -> VerifiedAnswerContract:
    evidence = _contract_evidence(context, candidate_by_citation)
    if not evidence or (route_confidence == "low" and uncertainty) or force_refusal_reason:
        reason = force_refusal_reason or uncertainty or "No request-local evidence supports an answer."
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
        _validate_required_role_coverage(context.question, evidence, citations)
    # 引用属于证据集合只能证明来源，不代表语义蕴含成立。
    # 在具备独立蕴含检查前，生成内容只能标记为有限结论。
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


def build_general_contract(answer: Answer) -> VerifiedAnswerContract:
    """保留无仓库来源的模型回答为有限通用结论，不伪装成源码事实。"""
    if not isinstance(answer, Answer) or not isinstance(answer.text, str) or not answer.text.strip():
        raise ProviderError("General provider returned no answer text.")
    # General 路径没有仓库证据，仍须拒绝模型自行提供的来源 ID 或路径位置。
    # 这是来源合同校验，不表示通用知识已被独立证明。
    empty_context = BuiltContext("general", (), "", 0, 1)
    validate_model_citations(answer.text, empty_context)
    if answer.offline:
        contract = VerifiedAnswerContract(
            ANSWER_CONTRACT_VERSION, "refused", answer.text,
            "No generative provider is configured for General mode.",
            (AnswerClaim("当前没有可用的通用问答模型。", "unknown", (), "general"),),
            (), "general", "not_applicable",
        )
    else:
        contract = VerifiedAnswerContract(
            ANSWER_CONTRACT_VERSION, "limited", answer.text,
            "General knowledge is not grounded in repository evidence and has not been independently verified.",
            (AnswerClaim(answer.text, "inference", (), "general"),),
            (), "general", "not_applicable",
        )
    validate_answer_contract(contract, empty_context)
    return contract


def build_planner_stop_contract(answer: Answer, *, route: str, reason: str,
                                origin: str = "external") -> VerifiedAnswerContract:
    """Represent unavailable capabilities or clarification as a safe refusal."""
    if origin not in {"general", "external"} or not isinstance(reason, str) or not reason.strip():
        raise ProviderError("Planner stop contract metadata is invalid.")
    if not isinstance(answer, Answer) or not isinstance(answer.text, str) or not answer.text.strip():
        raise ProviderError("Planner stop response is empty.")
    contract = VerifiedAnswerContract(
        ANSWER_CONTRACT_VERSION, "refused", answer.text, reason.strip(),
        (AnswerClaim(answer.text, "unknown", (), origin),), (), route, "not_applicable",
    )
    validate_answer_contract(contract, BuiltContext("planner", (), "", 0, 1))
    return contract


def verify_hybrid_answer(answer: Answer, context: BuiltContext, *,
                         candidate_by_citation=None,
                         route_confidence="medium") -> VerifiedAnswerContract:
    """将 Hybrid 输出分区；通用结论不取源码引用，仓库结论仍逐条验 ID。"""
    if not isinstance(answer, Answer) or not isinstance(answer.text, str):
        raise ProviderError("Hybrid provider returned an invalid answer.")
    lines = answer.text.splitlines()
    headings = [(index, line.strip().casefold()) for index, line in enumerate(lines)
                if line.strip().casefold() in {"general knowledge:", "repository evidence:"}]
    if (len(headings) != 2 or headings[0][1] != "general knowledge:"
            or headings[1][1] != "repository evidence:"):
        raise ProviderError("Hybrid response did not follow the required general/repository section protocol.")
    general_body = "\n".join(lines[headings[0][0] + 1:headings[1][0]]).strip()
    repository_body = "\n".join(lines[headings[1][0] + 1:]).strip()
    if not general_body or not repository_body:
        raise ProviderError("Hybrid response must include both non-empty answer sections.")
    # A source ID in the General section would blur provenance. Fail closed.
    if re.search(r"\[[Ee]\d+\]", general_body):
        raise CitationValidationError(
            "General knowledge section must not cite repository evidence.",
            code="repository_citation_in_general_section",
            diagnostic={"rule_id": "repository_citation_in_general_section",
                        "validation_result": "rejected"},
        )
    citations = validate_model_citations(repository_body, context)
    evidence = _contract_evidence(context, candidate_by_citation)
    if citations:
        repo_claim = AnswerClaim(repository_body, "inference", citations, "repository")
    else:
        unavailable = re.search(
            r"cannot (?:confirm|determine|find)|insufficient (?:repository )?evidence|"
            r"not (?:present|available|found|confirmed)|无法(?:从|在).{0,16}(?:确认|找到|判断)|"
            r"证据不足|未能(?:找到|确认)|无法确认",
            repository_body, re.IGNORECASE,
        )
        if not unavailable:
            raise ProviderError(
                "Hybrid repository section made an uncited claim instead of stating the evidence limit."
            )
        repo_claim = AnswerClaim(repository_body, "unknown", (), "repository")
    contract = VerifiedAnswerContract(
        ANSWER_CONTRACT_VERSION, "limited", answer.text,
        "General and repository claims are separated; citation identity is validated, semantic entailment is not.",
        (AnswerClaim(general_body, "inference", (), "general"), repo_claim),
        evidence, "hybrid", route_confidence,
    )
    validate_answer_contract(contract, context)
    return contract


__all__ = [
    "ANSWER_CONTRACT_VERSION", "AnswerClaim", "ContractEvidence",
    "VerifiedAnswerContract", "build_offline_contract",
    "build_general_contract", "build_planner_stop_contract", "validate_answer_contract", "verify_generated_answer",
    "verify_hybrid_answer",
]
