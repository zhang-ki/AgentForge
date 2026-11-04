"""可解释的离线路由研究原型。"""

from dataclasses import dataclass
import re

from .config import MAX_QUERY_LENGTH
from .retriever import terms


ROUTES = {
    "dependency_relation", "package_api", "exact_symbol",
    "business_implementation", "documentation", "uncertain",
}


@dataclass(frozen=True)
class RouteDecision:
    route: str
    channels: tuple[str, ...]
    rule: str
    reason: str
    confidence: str


class ExplainableQueryRouter:
    """冻结规则只使用查询文本；检索结果不会反过来选择路由。"""

    version = "package-router-calibration-v1"

    def route(self, query: str) -> RouteDecision:
        if not isinstance(query, str) or not query.strip() or len(query) > MAX_QUERY_LENGTH:
            raise ValueError("Routing query must be non-empty and within the query limit.")
        folded = query.casefold()
        words = set(terms(query))
        if any(phrase in folded for phrase in (
            "is there evidence", "cannot determine", "unknown relationship", "unclassified request",
        )):
            return RouteDecision(
                "uncertain", ("lexical",), "uncertainty_phrase",
                "The wording asks about evidence sufficiency or lacks a classifiable target.", "low",
            )
        structural_terms = {
            "import", "imports", "alias", "caller", "callee", "call", "calls",
            "export", "exports", "reexport", "reexports", "resolve", "resolved",
            "relationship", "relationships", "relation", "relations",
        }
        if words & {"dynamic", "runtime", "reflective", "reflection"} and words & structural_terms:
            return RouteDecision(
                "uncertain", ("lexical",), "dynamic_relation_uncertainty",
                "Dynamic or reflective behavior cannot be established by the static relation graph; "
                "retain ambiguity and fall back to lexical evidence.", "low",
            )
        if any(word in words for word in (
            "import", "imports", "imported", "alias", "aliased", "caller", "callers",
            "callee", "callees", "call", "calls", "called", "invoke", "invokes",
            "invoked", "invoking", "dependency", "dependencies", "depends",
        )):
            return RouteDecision(
                "dependency_relation", ("package_relations", "symbols", "lexical"),
                "dependency_terms", "Import, alias, dependency or caller terms request a static relationship.",
                "high",
            )
        if ("__all__" in query or "re-export" in folded
                or any(word in words for word in ("reexport", "reexports", "export", "exports", "public"))):
            return RouteDecision(
                "package_api", ("package_relations", "lexical"), "public_api_terms",
                "Public API, export or __all__ terms request a package-surface relationship.", "high",
            )
        if (re.search(r"\b(?:where|which file)\b.*\b(?:class|function|module|defined|definition)\b", folded)
                or "qualified name" in folded):
            return RouteDecision(
                "exact_symbol", ("symbols", "lexical"), "exact_symbol_shape",
                "The query explicitly asks for a class/function/module definition.", "high",
            )
        if any(word in words for word in ("document", "documentation", "policy", "rule", "guide")):
            return RouteDecision(
                "documentation", ("lexical", "vector"), "documentation_terms",
                "Documentation or policy terms request textual evidence.", "high",
            )
        if any(word in words for word in ("implement", "implementation", "behavior", "workflow",
                                          "business", "handles", "processes", "how")):
            return RouteDecision(
                "business_implementation", ("lexical", "vector"), "implementation_terms",
                "The query asks for implementation or business behavior rather than a named relation.",
                "medium",
            )
        return RouteDecision(
            "uncertain", ("lexical",), "no_specific_rule",
            "No high-confidence structural or document intent rule matched; use lexical fallback only.",
            "low",
        )
