"""Python 解析器仅依赖标准库 AST 模块。"""

import ast
from collections import Counter
import hashlib
from pathlib import PurePosixPath

from ..chunker import split_source_lines
from ..config import (
    MAX_AST_NODES_PER_FILE,
    MAX_REFERENCES_PER_FILE,
    MAX_SYMBOLS_PER_FILE,
)
from .base import ParsedFile, ParsedReference, ParsedSymbol


def _digest(prefix: str, value: str, size: int = 24) -> str:
    return prefix + hashlib.sha256(value.encode("utf-8")).hexdigest()[:size]


def _normalise_relative_path(relative_path: str) -> str:
    value = relative_path.replace("\\", "/")
    parts = PurePosixPath(value).parts
    if (not value or value.startswith("/") or ":" in value
            or any(part in ("", ".", "..") for part in parts)):
        raise ValueError("Parser path must be repository-relative and must not traverse.")
    return "/".join(parts)


def _module_parts(relative_path: str) -> tuple[str | None, str | None, bool]:
    parts = relative_path.split("/")
    is_init = parts[-1] in ("__init__.py", "__init__.pyi")
    if is_init:
        module_parts = parts[:-1]
        package_parts = module_parts
    else:
        module_parts = parts[:-1] + [parts[-1].rsplit(".", 1)[0]]
        package_parts = parts[:-1]
    valid_module = bool(module_parts) and all(part.isidentifier() for part in module_parts)
    valid_package = bool(package_parts) and all(part.isidentifier() for part in package_parts)
    module_name = ".".join(module_parts) if valid_module else None
    package_name = ".".join(package_parts) if valid_package else None
    return module_name, package_name, is_init


def _source_id(relative_path: str) -> str:
    return _digest("F", relative_path, 20)


def _signature(node: ast.AST) -> str:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        prefix = "async " if isinstance(node, ast.AsyncFunctionDef) else ""
        args = ast.unparse(node.args)
        result = f"{prefix}def {node.name}({args})"
        if node.returns is not None:
            result += f" -> {ast.unparse(node.returns)}"
        return result
    if isinstance(node, ast.ClassDef):
        arguments = [ast.unparse(item) for item in node.bases]
        arguments.extend(
            f"{item.arg}={ast.unparse(item.value)}" if item.arg else f"**{ast.unparse(item.value)}"
            for item in node.keywords
        )
        return f"class {node.name}({', '.join(arguments)})"
    raise TypeError("Unsupported symbol node.")


class _ParseLimitExceeded(Exception):
    pass


def _ast_node_limit_exceeded(tree: ast.AST) -> bool:
    """在递归遍历收集器和创建 SQL 记录前限制 AST 遍历规模。"""
    pending = [tree]
    visited = 0
    while pending:
        node = pending.pop()
        visited += 1
        if visited > MAX_AST_NODES_PER_FILE:
            return True
        for child in ast.iter_child_nodes(node):
            pending.append(child)
            # visited 与 pending 之和是可达节点数量的低成本上界。
            if visited + len(pending) > MAX_AST_NODES_PER_FILE:
                return True
    return False


def _definition_structure(node: ast.AST, signature: str) -> str:
    """可哈希的函数头摘要，避免反复序列化嵌套函数体。"""
    decorators = getattr(node, "decorator_list", ())
    return "\0".join((type(node).__name__, signature, *(ast.dump(item, include_attributes=False)
                                                       for item in decorators)))


class _DefinitionCollector(ast.NodeVisitor):
    def __init__(self, relative_path: str, module_name: str, module_symbol: ParsedSymbol):
        self.relative_path = relative_path
        self.module_name = module_name
        self.symbols = [module_symbol]
        self.stack = [module_symbol]
        self.node_symbols: dict[int, ParsedSymbol] = {}
        self.symbol_nodes: dict[int, ast.AST] = {}
        self.occurrences: Counter[tuple[str, str, str]] = Counter()

    def _add_definition(self, node: ast.AST, name: str, kind: str) -> None:
        if len(self.symbols) >= MAX_SYMBOLS_PER_FILE:
            raise _ParseLimitExceeded("symbol limit exceeded for this file")
        parent = self.stack[-1]
        qualified_name = f"{parent.qualified_name}.{name}"
        signature = _signature(node)
        key = (kind, qualified_name, signature)
        ordinal = self.occurrences[key]
        self.occurrences[key] += 1
        identity = "\0".join((self.relative_path, kind, qualified_name, signature, str(ordinal)))
        symbol_id = _digest("S", identity)
        decorators = getattr(node, "decorator_list", ())
        start_line = min([node.lineno, *(item.lineno for item in decorators)])
        end_line = max(start_line, getattr(node, "end_lineno", node.lineno) or node.lineno)
        structure = _definition_structure(node, signature)
        symbol = ParsedSymbol(
            symbol_id=symbol_id,
            source_id=self.symbols[0].source_id,
            kind=kind,
            name=name,
            qualified_name=qualified_name,
            start_line=start_line,
            end_line=end_line,
            parent_symbol_id=parent.symbol_id,
            signature=signature,
            docstring=ast.get_docstring(node, clean=False),
            structure_hash=hashlib.sha256(structure.encode("utf-8")).hexdigest(),
        )
        self.symbols.append(symbol)
        self.node_symbols[id(node)] = symbol
        self.symbol_nodes[id(node)] = node
        self.stack.append(symbol)
        for statement in getattr(node, "body", ()):
            self.visit(statement)
        self.stack.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._add_definition(node, node.name, "class")

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        parent_kind = self.stack[-1].kind
        self._add_definition(node, node.name, "method" if parent_kind == "class" else "function")

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        parent_kind = self.stack[-1].kind
        self._add_definition(node, node.name, "method" if parent_kind == "class" else "function")


def _dotted_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _dotted_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else None
    if isinstance(node, ast.Call):
        inner = _dotted_name(node.func)
        return f"{inner}()" if inner else None
    return None


class _ReferenceCollector(ast.NodeVisitor):
    def __init__(self, source_id: str, module_symbol: ParsedSymbol,
                 node_symbols: dict[int, ParsedSymbol], relative_path: str):
        self.source_id = source_id
        self.relative_path = relative_path
        self.node_symbols = node_symbols
        self.scope = module_symbol
        self.references: list[ParsedReference] = []
        self.occurrences: Counter[tuple[int, str, str]] = Counter()

    def _add(self, line: int, reference_type: str, target_name: str, *,
             module_name: str | None = None, imported_name: str | None = None,
             local_name: str | None = None, import_level: int = 0) -> None:
        if len(self.references) >= MAX_REFERENCES_PER_FILE:
            raise _ParseLimitExceeded("reference limit exceeded for this file")
        key = (line, reference_type, target_name)
        ordinal = self.occurrences[key]
        self.occurrences[key] += 1
        identity = "\0".join((
            self.relative_path, self.scope.symbol_id, str(line),
            reference_type, target_name, str(ordinal),
        ))
        self.references.append(ParsedReference(
            reference_id=_digest("R", identity),
            source_id=self.source_id,
            source_symbol_id=self.scope.symbol_id,
            line=line,
            reference_type=reference_type,
            target_name=target_name,
            module_name=module_name,
            imported_name=imported_name,
            local_name=local_name,
            import_level=import_level,
        ))

    def _visit_definition_expressions(self, node: ast.AST) -> None:
        for item in getattr(node, "decorator_list", ()):
            self.visit(item)
        for item in getattr(node, "bases", ()):
            self.visit(item)
        for item in getattr(node, "keywords", ()):
            self.visit(item)
        args = getattr(node, "args", None)
        if args is not None:
            for item in (*args.defaults, *(value for value in args.kw_defaults if value is not None)):
                self.visit(item)
            for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs):
                if arg.annotation:
                    self.visit(arg.annotation)
            if args.vararg and args.vararg.annotation:
                self.visit(args.vararg.annotation)
            if args.kwarg and args.kwarg.annotation:
                self.visit(args.kwarg.annotation)
        returns = getattr(node, "returns", None)
        if returns is not None:
            self.visit(returns)

    def _visit_scope_body(self, node: ast.AST) -> None:
        previous = self.scope
        self.scope = self.node_symbols[id(node)]
        for statement in getattr(node, "body", ()):
            self.visit(statement)
        self.scope = previous

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._visit_definition_expressions(node)
        self._visit_scope_body(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_definition_expressions(node)
        self._visit_scope_body(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_definition_expressions(node)
        self._visit_scope_body(node)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self._add(
                node.lineno, "import", alias.name, module_name=alias.name,
                local_name=alias.asname or alias.name.split(".", 1)[0],
            )

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        for alias in node.names:
            name = "*" if alias.name == "*" else alias.name
            self._add(
                node.lineno, "from_import", f"{module}:{name}",
                module_name=module, imported_name=name,
                local_name=alias.asname or alias.name, import_level=node.level,
            )

    def visit_Call(self, node: ast.Call) -> None:
        name = _dotted_name(node.func)
        if name:
            if name in {"__import__", "importlib.import_module"}:
                imported = None
                if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                    imported = node.args[0].value
                self._add(node.lineno, "dynamic_import", imported or "<dynamic>")
            else:
                self._add(node.lineno, "call", name)
        self.generic_visit(node)


class _LocalBindingCollector(ast.NodeVisitor):
    """查找可能遮蔽导入名称的赋值，但不进入子作用域。"""

    def __init__(self):
        self.names: set[str] = set()

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            self.names.add(node.id)

    def visit_Import(self, node: ast.Import) -> None:
        return

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        return

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        # 定义记录在符号表中；解析器先检查本地候选项，
        # 再解析导入别名。
        return

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        return

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        return

    def visit_Lambda(self, node: ast.Lambda) -> None:
        # 将 Lambda 函数体归属到所在符号；对于 Lambda，
        # 把参数视为名称遮蔽，不通过导入关系解析。
        args = node.args
        self.names.update(arg.arg for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs))
        if args.vararg:
            self.names.add(args.vararg.arg)
        if args.kwarg:
            self.names.add(args.kwarg.arg)
        return

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.name:
            self.names.add(node.name)
        self.generic_visit(node)


def _scope_shadowed_names(
    tree: ast.Module,
    module_symbol: ParsedSymbol,
    node_symbols: dict[int, ParsedSymbol],
    symbol_nodes: dict[int, ast.AST],
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    result: dict[str, tuple[str, ...]] = {}
    module_bindings = _LocalBindingCollector()
    for statement in tree.body:
        module_bindings.visit(statement)
    result[module_symbol.symbol_id] = tuple(sorted(module_bindings.names))
    for node_id, symbol in node_symbols.items():
        node = symbol_nodes.get(node_id)
        if node is None:
            continue
        bindings = _LocalBindingCollector()
        args = getattr(node, "args", None)
        if args is not None:
            positional = [*args.posonlyargs, *args.args]
            for index, arg in enumerate(positional):
                if not (
                    symbol.kind == "method" and index == 0 and arg.arg in ("self", "cls")
                ):
                    bindings.names.add(arg.arg)
            bindings.names.update(arg.arg for arg in args.kwonlyargs)
            if args.vararg:
                bindings.names.add(args.vararg.arg)
            if args.kwarg:
                bindings.names.add(args.kwarg.arg)
        for statement in getattr(node, "body", ()):
            bindings.visit(statement)
        result[symbol.symbol_id] = tuple(sorted(bindings.names))
    return tuple(sorted(result.items()))


class PythonAstParser:
    parser_name = "python-ast-v1"

    def parse(self, relative_path: str, source_text: str) -> ParsedFile:
        relative = _normalise_relative_path(relative_path)
        source_id = _source_id(relative)
        module_name, package_name, is_init = _module_parts(relative)
        fallback_name = PurePosixPath(relative).with_suffix("").as_posix().replace("/", ".")
        module_qualified_name = module_name or fallback_name
        module_name_for_symbol = module_qualified_name.rsplit(".", 1)[-1]
        try:
            # Python AST 与索引器都只把 CR/LF 视为源码行边界。
            normalised_source = source_text.replace("\r\n", "\n").replace("\r", "\n")
            tree = ast.parse(normalised_source, filename=relative, type_comments=True)
            if _ast_node_limit_exceeded(tree):
                raise _ParseLimitExceeded("AST node limit exceeded for this file")
            line_count = max(1, len(split_source_lines(source_text)))
            module_dump = ast.dump(tree, annotate_fields=True, include_attributes=False)
            module_symbol = ParsedSymbol(
                symbol_id=_digest("S", "\0".join((relative, "module", module_qualified_name))),
                source_id=source_id,
                kind="module",
                name=module_name_for_symbol,
                qualified_name=module_qualified_name,
                start_line=1,
                end_line=line_count,
                parent_symbol_id=None,
                signature=f"module {module_qualified_name}",
                docstring=ast.get_docstring(tree, clean=False),
                structure_hash=hashlib.sha256(module_dump.encode("utf-8")).hexdigest(),
            )
            definitions = _DefinitionCollector(relative, module_qualified_name, module_symbol)
            definitions.visit(tree)
            references = _ReferenceCollector(
                source_id, module_symbol, definitions.node_symbols, relative
            )
            references.visit(tree)
            shadowed_names = _scope_shadowed_names(
                tree, module_symbol, definitions.node_symbols, definitions.symbol_nodes
            )
            return ParsedFile(
                relative_path=relative,
                source_id=source_id,
                parser_name=self.parser_name,
                status="parsed",
                error=None,
                module_name=module_name,
                package_name=package_name,
                is_package_init=is_init,
                symbols=tuple(definitions.symbols),
                references=tuple(references.references),
                scope_shadowed_names=shadowed_names,
            )
        except SyntaxError as exc:
            error = f"SyntaxError: {exc.msg} at line {exc.lineno or 1}"
        except _ParseLimitExceeded as exc:
            error = f"ResourceLimitExceeded: {exc}"
        except (ValueError, RecursionError, MemoryError) as exc:
            error = f"{type(exc).__name__}: Python source could not be parsed safely"
        return ParsedFile(
            relative_path=relative,
            source_id=source_id,
            parser_name=self.parser_name,
            status="failed",
            error=error,
            module_name=module_name,
            package_name=package_name,
            is_package_init=is_init,
            symbols=(),
            references=(),
        )
