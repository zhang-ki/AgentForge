"""Repository retrieval candidate selection for the conversation use case."""

import ast
from dataclasses import replace
import re

from .errors import AgentForgeError


def _conversation_retrieval(outcome, retriever, conversation, arguments, store, rows):
    symbol = None
    missing_named_symbol = False
    if conversation is not None and outcome.decision.route == "exact_symbol" and _is_followup(arguments.query):
        named = retriever._symbol_name(arguments.query)
        missing_named_symbol = bool(named and not store.search_symbols(name=named, limit=1))
    if conversation is not None and (outcome.decision.confidence == "low" or missing_named_symbol):
        symbol = _unique_mentioned_symbol(arguments.query, store) or _followup_symbol(arguments.query, rows, store)
        if symbol:
            outcome = retriever.search(symbol, top_k=arguments.top_k)
    return outcome, symbol

def _focus_large_class_symbol(question, symbol, store, retriever):
    """按行为查询过大的类时，使用精确的类头和成员源码范围。"""
    if len(symbol.text.encode("utf-8")) <= 1_400 or not symbol.symbol_ids:
        return None
    try:
        tree = ast.parse(symbol.text)
    except (SyntaxError, ValueError):
        return None
    classes = [node for node in tree.body if isinstance(node, ast.ClassDef)]
    if len(classes) != 1:
        return None

    parameter_intent = bool(re.search(
        r"\b(?:parameter|parameters|argument|arguments|initialize|initialization)\b|参数|初始化",
        question, re.IGNORECASE,
    ))
    behavior_intent = bool(re.search(
        r"\b(?:what|how)\b.*\b(?:do|does|work|perform|function)\b|作用|做什么|功能",
        question, re.IGNORECASE,
    ))
    if not parameter_intent and not behavior_intent:
        return None

    try:
        methods = [row for row in store.search_symbols(
            kind="method", relative_path=symbol.relative_path, limit=500,
        ) if row.get("parent_symbol_id") == symbol.symbol_ids[0]]
    except (ValueError, AgentForgeError):
        return None
    if not methods:
        return None

    by_name = {str(row.get("name", "")).casefold(): row for row in methods}
    priorities = (("__init__",) if parameter_intent else
                  ("forward", "__call__", "run", "execute", "compute", "apply", "transform", "predict"))
    selected = next((by_name[name] for name in priorities if name in by_name), None)
    if selected is None:
        public_methods = [row for row in methods
                          if isinstance(row.get("name"), str)
                          and not row["name"].startswith("_")]
        if len(public_methods) == 1:
            selected = public_methods[0]
    if selected is None or not retriever._indexed_file_is_current(symbol.relative_path):
        return None

    class_start = symbol.start_line + classes[0].lineno - 1
    method_start, method_end = selected.get("start_line"), selected.get("end_line")
    if (isinstance(method_start, bool) or not isinstance(method_start, int)
            or isinstance(method_end, bool) or not isinstance(method_end, int)
            or method_start < 1 or method_end < method_start):
        return None
    try:
        class_text = retriever._excerpt(symbol.relative_path, class_start, class_start)
        method_text = retriever._excerpt(symbol.relative_path, method_start, method_end)
    except ValueError:
        return None

    from .product_retrieval import ProductCandidate, _content_hash
    class_header = replace(
        symbol, start_line=class_start, end_line=class_start, text=class_text,
        content_hash=_content_hash(symbol.relative_path, class_start, class_start, class_text),
        evidence_roles=("definition",),
    )
    member = ProductCandidate(
        symbol.repository, symbol.relative_path, method_start, method_end, method_text,
        _content_hash(symbol.relative_path, method_start, method_end, method_text),
        symbol.score, symbol.channels, symbol.channel_scores,
        symbol_ids=(selected["symbol_id"],), resolution_status="resolved",
        evidence_roles=("definition",),
    )
    return class_header, member

def _narrow_explicit_symbol_candidates(query, symbols, store):
    """按精确拼写解析；必要时优先选择唯一的包级重新导出项。"""
    from .product_retrieval import _mentioned_code_symbol

    mentioned = _mentioned_code_symbol(query)
    if not mentioned:
        return tuple(symbols)

    rows_by_candidate = {}
    for candidate in symbols:
        try:
            rows = store.search_symbols_by_ids(candidate.symbol_ids, limit=100)
        except (ValueError, AgentForgeError):
            continue
        rows_by_candidate[id(candidate)] = rows

    if "." in mentioned:
        exact = [candidate for candidate in symbols
                 if any(row.get("qualified_name") == mentioned
                        for row in rows_by_candidate.get(id(candidate), ()))]
    else:
            # Python 标识符区分大小写；交互式搜索的存储查找
            # 不区分大小写，因此构建证据前要先移除
        # 仅在大小写折叠后才匹配的近似结果，再构建证据。
        exact = [candidate for candidate in symbols
                 if any(row.get("name") == mentioned
                        for row in rows_by_candidate.get(id(candidate), ()))]
    if not exact:
        return ()
    if len(exact) == 1 or "." in mentioned:
        return tuple(exact)

        # 若精确名称对应多个定义，则只有在包的 __init__.py 中存在唯一
        # 重新导出项时才优先选择它。否则，如果仓库无法
        # 确认唯一的公开定义，就保留全部候选，并由现有
    # 预算与证据门禁安全拒绝，而不是猜测。
    public = []
    for candidate in exact:
        for row in rows_by_candidate.get(id(candidate), ()):
            if row.get("name") != mentioned:
                continue
            try:
                references = store.search_references(
                    target=row.get("qualified_name"),
                    reference_type="from_import", status="resolved", limit=500,
                )
            except (ValueError, AgentForgeError):
                continue
            if any(
                str(reference.get("relative_path", "")).replace("\\", "/").rsplit("/", 1)[-1]
                == "__init__.py"
                and str(reference.get("target_path", "")).casefold()
                == str(row.get("relative_path", "")).casefold()
                and reference.get("target_qualified_name") == row.get("qualified_name")
                for reference in references
            ):
                public.append(candidate)
                break
    return tuple(public) if len(public) == 1 else tuple(exact)

def _context_candidates(outcome, conversation, *, store=None, retriever=None):
    """将精确的定义及其静态关联证据一起保留。"""
    query = getattr(outcome, "query", "")
    if outcome.decision.route == "exact_symbol":
        symbols = tuple(item for item in outcome.candidates if item.symbol_ids)
        if symbols:
            if store is None or retriever is None:
                return symbols
            symbols = _narrow_explicit_symbol_candidates(query, symbols, store)
            if not symbols:
                return ()
            focused = []
            for symbol_item in symbols:
                replacements = _focus_large_class_symbol(query, symbol_item, store, retriever)
                focused.extend(replacements if replacements is not None else (symbol_item,))
            symbols = tuple(focused)
            explicit = set(re.findall(r"\b[A-Z][A-Z0-9_]{2,}\b", query))
            targets = []
            for item in symbols:
                try:
                    tree = ast.parse(item.text)
                except (SyntaxError, ValueError):
                    continue
                referenced = {node.id for node in ast.walk(tree)
                              if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
                              and node.id in explicit}
                if not referenced:
                    continue
                try:
                    refs = store.search_references(relative_path=item.relative_path,
                                                   reference_type="from_import", limit=500)
                except (ValueError, AgentForgeError):
                    continue
                for ref in refs:
                    qualified = ref.get("target_qualified_name") or ""
                    if ref.get("resolution_status") == "ambiguous" or not re.fullmatch(
                            r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+", qualified):
                        continue
                    module, name = qualified.rsplit(".", 1)
                    if name in referenced:
                        targets.append((ref, name, {module.replace(".", "/") + ".py",
                                                   module.replace(".", "/") + "/__init__.py"}))
            additions = []
            imports = []
            configuration_names = set()
            for ref, name, possible_paths in targets:
                configuration_names.add(name)
                # 被省略的检索候选不能掩盖模块或包名存在歧义的事实。
                repository_paths = {path.casefold() for path in retriever._candidates}
                if sum(path.casefold() in repository_paths for path in possible_paths) != 1:
                    continue
                matching = [item for item in outcome.candidates if not item.symbol_ids and
                            item.relative_path.casefold() in {path.casefold() for path in possible_paths}]
                if len(matching) != 1:
                    continue
                item = matching[0]
                try:
                    tree = ast.parse(item.text)
                except (SyntaxError, ValueError):
                    continue
                declared = set()
                for node in tree.body:
                    targets = node.targets if isinstance(node, ast.Assign) else (
                        [node.target] if isinstance(node, ast.AnnAssign) else [])
                    declared.update(target.id for target in targets if isinstance(target, ast.Name))
                if name not in declared or not retriever._indexed_file_is_current(ref["relative_path"]):
                    continue
                try:
                    import_text = retriever._excerpt(ref["relative_path"], ref["line"], ref["line"])
                except ValueError:
                    continue
                from .product_retrieval import ProductCandidate, _content_hash
                imports.append(ProductCandidate(str(retriever.repository), ref["relative_path"],
                    ref["line"], ref["line"], import_text,
                    _content_hash(ref["relative_path"], ref["line"], ref["line"], import_text),
                    item.score, ("references",), (("references", item.score),),
                    relation_ids=(ref["reference_id"],), resolution_status="unresolved",
                    evidence_roles=("import",)))
                additions.append(replace(
                    item, resolution_status="uncertain",
                    evidence_roles=tuple(dict.fromkeys(
                        item.evidence_roles + ("configuration_definition", "definition")
                    )),
                ))
            symbols = tuple(replace(
                symbol_item,
                evidence_roles=tuple(dict.fromkeys(
                    symbol_item.evidence_roles + ("configuration_use",)
                )),
            ) if any(name in symbol_item.text for name in configuration_names) else symbol_item
                for symbol_item in symbols)
            candidates = (*symbols, *imports, *additions)
            return _prioritize_required_evidence(query, candidates)
    return _prioritize_required_evidence(query, outcome.candidates)

def _prioritize_required_evidence(question, candidates):
    from .evidence_roles import required_evidence_roles
    required = required_evidence_roles(question)
    if not required:
        return tuple(candidates)
    return tuple(pair[1] for pair in sorted(enumerate(candidates), key=lambda pair: (
        0 if set(pair[1].evidence_roles) & set(required) else 1,
        pair[0],
    )))

def _followup_symbol(query, rows, store):
    """只解析前一个问题提到、且索引中唯一对应的符号。"""
    if not _is_followup(query):
        return None
    if len(rows) < 2 or rows[-2]["role"] != "user" or rows[-1]["role"] != "assistant":
        return None
    mentioned = {_unique_mentioned_symbol(row["content"], store)
                 for row in rows[-12:] if row["role"] == "user"}
    mentioned.discard(None)
    return next(iter(mentioned)) if len(mentioned) == 1 else None

def _is_followup(query):
    return bool(re.search(r"(?:它|其|这个|该)|\b(?:it|its|that)\b|\bwere we discussing\b",
                          query, re.IGNORECASE))

def _unique_mentioned_symbol(query, store):
            # 模块和其中的函数可以合法地同名。若问题已明确
            # 指出符号种类，就按该种类解析；绝不在多个定义间猜选。
    explicit = re.findall(r"\b(function|class|method)\s+`?([A-Za-z_][A-Za-z0-9_]*)`?",
                          query, re.IGNORECASE)
    if explicit:
        resolved = set()
        for kind, name in explicit:
            found = store.search_symbols(name=name, kind=kind.casefold(), limit=2)
            if len(found) != 1:
                return None
            resolved.add(name)
        return next(iter(resolved)) if len(resolved) == 1 else None
    words = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", query))
    matches = []
    generic = {"what", "which", "does", "function", "class", "method", "module", "symbol",
               "return", "returns", "cite", "evidence", "identify", "the", "and", "for"}
    for word in sorted(words):
        if len(word) < 3 or word.casefold() in generic:
            continue
        found = store.search_symbols(name=word, limit=2)
        if len(found) == 1:
            matches.append(word)
    return matches[0] if len(matches) == 1 else None
