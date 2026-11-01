"""Application-owned semantic roles for source evidence.

Roles describe source syntax selected by static analysis. They do not prove
that an import binds or that a call executes at runtime.
"""

from __future__ import annotations

import re


_CALL = re.compile(
    r"(?i)\b(?:caller|callers|callee|callees|call|calls|called|invoke|invokes|invoked)\b|"
    r"(?:调用方|调用者|被调用方|调用|执行调用)"
)
_CALLER_IDENTITY = re.compile(r"(?i)\b(?:which\s+)?callers?\b|(?:哪个调用方|调用方名称)")
_DEFINITION = re.compile(
    r"(?i)\b(?:implementation|definition|defined|implement(?:ed|ation)?)\b|"
    r"(?:实现|定义|具体代码)"
)
_CONFIG_VALUE = re.compile(
    r"(?i)\b(?:configuration|config|constant|value|setting|settings)\b|"
    r"(?:配置|常量|取值|数值)"
)
_CONFIG_USE = re.compile(
    r"(?i)\b(?:where.*\bget\b|where.*\bread\b|use|uses|used)\b|"
    r"(?:从哪里|在哪里.*(?:取|读|用))"
)


def required_evidence_roles(question: str) -> tuple[str, ...]:
    """Return evidence roles required to answer the question's explicit parts."""
    text = question if isinstance(question, str) else ""
    if _CALL.search(text) and _DEFINITION.search(text):
        roles = ["callsite", "target_definition"]
        if _CALLER_IDENTITY.search(text):
            roles.insert(0, "caller_definition")
        return tuple(roles)
    if (_CONFIG_USE.search(text) and _CONFIG_VALUE.search(text)
            and re.search(r"\b[A-Z][A-Z0-9_]{2,}\b", text)):
        return ("configuration_use", "configuration_definition")
    return ()


def role_coverage(question: str, available_roles, cited_roles=None) -> dict:
    """Describe required, available and cited roles without retaining source text."""
    required = required_evidence_roles(question)
    available = set(available_roles or ())
    cited = available if cited_roles is None else set(cited_roles or ())
    return {
        "required_roles": required,
        "available_roles": tuple(sorted(available)),
        "cited_roles": tuple(sorted(cited)),
        "missing_available_roles": tuple(role for role in required if role not in available),
        "missing_cited_roles": tuple(role for role in required if role not in cited),
    }


__all__ = ["required_evidence_roles", "role_coverage"]
