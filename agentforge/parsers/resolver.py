"""保守地解析仓库内的导入与调用关系。"""

from collections import defaultdict
from dataclasses import replace

from .base import ParsedFile, ParsedReference, Parser
from .python_ast import PythonAstParser


_PARSERS: tuple[tuple[tuple[str, ...], Parser], ...] = (
    ((".py", ".pyi"), PythonAstParser()),
)


def parser_for_path(relative_path: str) -> Parser | None:
    lowered = relative_path.replace("\\", "/").casefold()
    for suffixes, parser in _PARSERS:
        if lowered.endswith(suffixes):
            return parser
    return None


def _relative_module(parsed: ParsedFile, reference: ParsedReference) -> str | None:
    if reference.import_level == 0:
        return reference.module_name or None
    package = parsed.package_name
    if not package:
        return None
    parts = package.split(".")
    remove = reference.import_level - 1
    if remove >= len(parts):
        return None
    base = parts[:len(parts) - remove] if remove else parts
    if reference.module_name:
        base.extend(reference.module_name.split("."))
    return ".".join(base)


def _resolve_one(candidates_by_qname, qname: str | None, reference: ParsedReference) -> ParsedReference:
    candidates = tuple(sorted({
        item.symbol_id for item in candidates_by_qname.get(qname or "", ())
    }))
    if len(candidates) == 1:
        return replace(
            reference, target_qualified_name=qname, resolution_status="resolved",
            target_symbol_id=candidates[0], candidate_symbol_ids=candidates,
        )
    if len(candidates) > 1:
        return replace(
            reference, target_qualified_name=qname, resolution_status="ambiguous",
            target_symbol_id=None, candidate_symbol_ids=candidates,
        )
    return replace(
        reference, target_qualified_name=qname, resolution_status="unresolved",
        target_symbol_id=None, candidate_symbol_ids=(),
    )


def _resolve_call_candidates(candidates_by_qname, qnames, reference: ParsedReference) -> ParsedReference:
    ordered = tuple(dict.fromkeys(qname for qname in qnames if qname))
    candidates = tuple(sorted({
        symbol.symbol_id
        for qname in ordered
        for symbol in candidates_by_qname.get(qname, ())
        if symbol.kind in ("class", "function", "method")
    }))
    target_name = ordered[0] if len(ordered) == 1 else None
    if len(candidates) == 1:
        return replace(
            reference, target_qualified_name=target_name, resolution_status="resolved",
            target_symbol_id=candidates[0], candidate_symbol_ids=candidates,
        )
    if len(candidates) > 1:
        return replace(
            reference, target_qualified_name=target_name, resolution_status="ambiguous",
            target_symbol_id=None, candidate_symbol_ids=candidates,
        )
    return replace(
        reference, target_qualified_name=target_name, resolution_status="unresolved",
        target_symbol_id=None, candidate_symbol_ids=(),
    )


def _nearest_class(symbol_by_id, symbol_id: str):
    current = symbol_by_id.get(symbol_id)
    while current is not None:
        if current.kind == "class":
            return current
        current = symbol_by_id.get(current.parent_symbol_id)
    return None


def _scope_chain(symbol_by_id, symbol_id: str):
    current = symbol_by_id.get(symbol_id)
    while current is not None:
        yield current
        parent = symbol_by_id.get(current.parent_symbol_id)
        # 仅凭方法名查找时，不会自动搜索其所属类的命名空间。
        if current.kind == "method" and parent is not None and parent.kind == "class":
            current = symbol_by_id.get(parent.parent_symbol_id)
        else:
            current = parent


def _resolve_call(
    reference, parsed, imports_by_scope, invalid_imports, shadowed,
    symbols_by_id, symbols_by_qname,
):
    raw = reference.target_name
    if "." in raw:
        first, rest = raw.split(".", 1)
        for scope in _scope_chain(symbols_by_id, reference.source_symbol_id):
            if first in shadowed.get(scope.symbol_id, ()):
                return replace(reference, resolution_status="unresolved")
            if first in invalid_imports.get(scope.symbol_id, ()):
                return replace(reference, resolution_status="unresolved")
            local_prefix = _resolve_call_candidates(
                symbols_by_qname, (scope.qualified_name + "." + first,), reference
            )
            if local_prefix.resolution_status != "unresolved":
                # 本地函数或类的绑定不是导入的模块别名；
                # 属性解析不在这个保守解析器的处理范围内。
                return replace(reference, resolution_status="unresolved")
            targets = imports_by_scope.get(scope.symbol_id, {}).get(first)
            if targets:
                return _resolve_call_candidates(
                    symbols_by_qname,
                    (target + "." + rest for target in sorted(targets)),
                    reference,
                )
        if first in ("self", "cls"):
            if any(first in shadowed.get(scope.symbol_id, ()) for scope in
                   _scope_chain(symbols_by_id, reference.source_symbol_id)):
                return replace(reference, resolution_status="unresolved")
            owner = _nearest_class(symbols_by_id, reference.source_symbol_id)
            if owner:
                return _resolve_call_candidates(
                    symbols_by_qname, (owner.qualified_name + "." + rest,), reference
                )
        return replace(reference, resolution_status="unresolved")

    for scope in _scope_chain(symbols_by_id, reference.source_symbol_id):
        if raw in shadowed.get(scope.symbol_id, ()):
            return replace(reference, resolution_status="unresolved")
        if raw in invalid_imports.get(scope.symbol_id, ()):
            return replace(reference, resolution_status="unresolved")
        local = _resolve_call_candidates(
            symbols_by_qname, (scope.qualified_name + "." + raw,), reference
        )
        if local.resolution_status != "unresolved":
            return local
        qnames = set(imports_by_scope.get(scope.symbol_id, {}).get(raw, ()))
        resolved = _resolve_call_candidates(symbols_by_qname, qnames, reference)
        if resolved.resolution_status != "unresolved":
            return resolved
    return replace(reference, resolution_status="unresolved")


def resolve_references(parsed_files: list[ParsedFile]) -> list[ParsedFile]:
    """只解析唯一的本地候选项；保留未解析或存在歧义的证据。"""
    successful = [item for item in parsed_files if item.status == "parsed"]
    symbols_by_qname = defaultdict(list)
    symbols_by_id = {}
    for parsed in successful:
        for symbol in parsed.symbols:
            symbols_by_qname[symbol.qualified_name].append(symbol)
            symbols_by_id[symbol.symbol_id] = symbol

    resolved_imports: dict[str, list[ParsedReference]] = {}
    for parsed in successful:
        imports = []
        for reference in parsed.references:
            if reference.reference_type not in ("import", "from_import"):
                continue
            module_name = _relative_module(parsed, reference)
            if reference.reference_type == "import":
                target_qname = module_name
            elif reference.imported_name == "*":
                target_qname = module_name
            elif module_name:
                target_qname = f"{module_name}.{reference.imported_name}" if reference.imported_name else module_name
            else:
                target_qname = None
            imports.append(_resolve_one(symbols_by_qname, target_qname, reference))
        resolved_imports[parsed.relative_path] = imports

    output = []
    for parsed in parsed_files:
        if parsed.status != "parsed":
            output.append(parsed)
            continue
        imports_by_scope = defaultdict(lambda: defaultdict(set))
        invalid_imports = defaultdict(set)
        for reference in resolved_imports.get(parsed.relative_path, ()):
            if not reference.local_name or reference.local_name == "*":
                continue
            if reference.resolution_status != "resolved" or not reference.target_qualified_name:
                invalid_imports[reference.source_symbol_id].add(reference.local_name)
                continue
            if reference.reference_type == "import":
                module = _relative_module(parsed, reference)
                first_part = (module or "").split(".", 1)[0]
                target = (
                    first_part if reference.local_name == first_part
                    else reference.target_qualified_name
                )
            elif reference.imported_name != "*":
                target = reference.target_qualified_name
            else:
                continue
            imports_by_scope[reference.source_symbol_id][reference.local_name].add(target)

        shadowed = dict(parsed.scope_shadowed_names)
        imports = iter(resolved_imports.get(parsed.relative_path, ()))
        references = []
        for reference in parsed.references:
            if reference.reference_type in ("import", "from_import"):
                references.append(next(imports))
            elif reference.reference_type == "dynamic_import":
                references.append(replace(reference, resolution_status="unresolved"))
            else:
                references.append(_resolve_call(
                    reference, parsed, imports_by_scope, invalid_imports, shadowed,
                    symbols_by_id, symbols_by_qname,
                ))
        output.append(replace(parsed, references=tuple(references)))
    return output
