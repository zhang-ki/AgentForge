"""Static Python package import/export relation research prototype.

The graph is deliberately separate from the production SQLite schema and default
retriever.  It consumes only files approved and read by the repository scanner.
"""

from __future__ import annotations

import ast
from collections import Counter, defaultdict
from dataclasses import dataclass
import hashlib
from pathlib import PurePosixPath
from time import perf_counter

from .config import MAX_QUERY_LENGTH, MAX_TOP_K
from .scanner import ScanLimits, ScanReadError, read_candidate, scan_repository
from .retriever import terms


RELATION_TYPES = {
    "imports", "imports_symbol", "exports_symbol", "reexports_symbol",
    "import_alias", "resolves_to",
}
RESOLUTION_STATES = {"resolved", "ambiguous", "unresolved", "uncertain"}


def _id(prefix: str, *parts: object) -> str:
    value = "\0".join(str(part) for part in parts)
    return prefix + hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


def _module_for_path(path: str) -> tuple[str, str, bool]:
    parts = path.replace("\\", "/").split("/")
    is_init = parts[-1] == "__init__.py"
    module_parts = parts[:-1] if is_init else parts[:-1] + [parts[-1][:-3]]
    module = ".".join(module_parts)
    package = module if is_init else ".".join(module_parts[:-1])
    return module, package, is_init


def _absolute_module(package: str, module: str, level: int) -> str | None:
    if level == 0:
        return module or None
    if not package:
        return None
    parts = package.split(".")
    remove = level - 1
    if remove >= len(parts):
        return None
    base = parts[:len(parts) - remove] if remove else parts
    if module:
        base.extend(module.split("."))
    return ".".join(base)


@dataclass(frozen=True)
class PackageRelation:
    relation_id: str
    relation_type: str
    source_path: str
    source_module: str
    source_symbol_id: str
    start_line: int
    end_line: int
    target_module: str | None
    target_name: str | None
    local_name: str | None
    target_path: str | None
    target_symbol_id: str | None
    candidate_symbol_ids: tuple[str, ...]
    resolution_status: str
    cycle_detected: bool = False
    note: str = ""


@dataclass(frozen=True)
class PackageRelationResult:
    relation_id: str
    relative_path: str
    start_line: int
    end_line: int
    text: str
    score: float
    relation_type: str
    resolution_status: str
    target_path: str | None
    target_symbol_id: str | None


@dataclass(frozen=True)
class PackageRelationGraph:
    repository: str
    relations: tuple[PackageRelation, ...]
    parse_failures: tuple[tuple[str, str], ...]


@dataclass
class _Module:
    path: str
    name: str
    package: str
    is_init: bool
    tree: ast.Module
    module_symbol_id: str
    definitions: dict[str, tuple[str, int, int]]
    imports: list[dict]
    all_names: tuple[str, ...] | None
    all_line: int | None
    all_end_line: int | None
    all_dynamic: bool


_ParsedCache = dict[str, tuple[str, _Module | None, str | None]]
PACKAGE_RELATION_CACHE_VERSION = 1


class PackageSnapshotReadError(ValueError):
    """The graph snapshot is incomplete and must not replace a prior snapshot."""

    def __init__(self, relative_path: str, reason: str):
        self.relative_path = relative_path
        self.reason = reason
        super().__init__(f"Could not read scanned Python file {relative_path}: {reason}")


def _snapshot_repository(root, limits: ScanLimits | None = None):
    """Return a complete Python-file content snapshot and scanner candidates."""
    report = scan_repository(root, limits=limits)
    if not report.complete:
        raise ValueError("Repository scan was incomplete; package graph was not built.")
    candidates = {}
    manifest = {}
    for candidate in report.candidates:
        path = candidate.relative_path
        if not path.casefold().endswith(".py"):
            continue
        candidates[path] = candidate
        try:
            text = read_candidate(candidate, report.root)
        except ScanReadError as exc:
            manifest[path] = (
                f"!read-error:{type(exc).__name__}:{candidate.size_bytes}:{candidate.mtime_ns}"
            )
        else:
            manifest[path] = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return report, manifest, candidates


def _parse_snapshot(report, manifest, candidates, previous_cache=None):
    """Reuse unchanged ASTs and parse changed Python files only."""
    previous_cache = previous_cache or {}
    cache = {}
    modules = {}
    failures = []
    parsed_count = 0
    for path in sorted(candidates, key=str.casefold):
        digest = manifest[path]
        cached = previous_cache.get(path)
        if cached is not None and cached[0] == digest:
            entry = cached
        elif digest.startswith("!read-error:"):
            entry = (digest, None, "ScanReadError")
        else:
            try:
                text = read_candidate(candidates[path], report.root)
                actual = hashlib.sha256(text.encode("utf-8")).hexdigest()
                if actual != digest:
                    raise RuntimeError("Repository changed while the package snapshot was read.")
                module = _parse_module(path, text)
                entry = (digest, module, None)
                parsed_count += 1
            except (SyntaxError, ValueError, RecursionError) as exc:
                entry = (digest, None, type(exc).__name__)
                parsed_count += 1
        cache[path] = entry
        _entry_digest, module, failure = entry
        if module is not None:
            existing = modules.get(module.name)
            if existing is not None and existing.path != module.path:
                raise ValueError("Two Python files resolve to the same module name.")
            modules[module.name] = module
        elif failure:
            failures.append((path, failure))
    return modules, failures, cache, parsed_count


def _cache_entry_matches_manifest(path, entry, manifest):
    """Reject incomplete or malformed in-memory parse cache entries."""
    if not isinstance(entry, tuple) or len(entry) != 3:
        return False
    digest, module, failure = entry
    if digest != manifest.get(path):
        return False
    if module is not None:
        return isinstance(module, _Module) and module.path == path and failure is None
    return isinstance(failure, str) and bool(failure)


def _snapshot_and_parse(root, limits=None, previous_cache=None, previous_root=None):
    """Discover, read, hash, and parse each approved Python file at most once.

    Hashing remains mandatory on each refresh: size and timestamps are useful
    scanner metadata but are not proof that file contents stayed unchanged.
    The returned AST cache may be reused only when its content digest matches.
    """
    previous_cache = previous_cache or {}
    discovery_started = perf_counter()
    report = scan_repository(root, limits=limits)
    discovery_seconds = perf_counter() - discovery_started
    if not report.complete:
        raise ValueError("Repository scan was incomplete; package graph was not built.")
    if previous_root != str(report.root):
        previous_cache = {}

    candidates = {}
    manifest = {}
    cache = {}
    modules = {}
    failures = []
    read_count = 0
    hash_count = 0
    cache_hit_count = 0
    parsed_count = 0
    read_hash_seconds = 0.0
    parse_seconds = 0.0

    for candidate in report.candidates:
        path = candidate.relative_path
        if not path.casefold().endswith(".py"):
            continue
        candidates[path] = candidate
        io_started = perf_counter()
        read_count += 1
        try:
            text = read_candidate(candidate, report.root)
        except ScanReadError as exc:
            read_hash_seconds += perf_counter() - io_started
            raise PackageSnapshotReadError(path, exc.reason) from None
        else:
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            hash_count += 1
            manifest[path] = digest
            read_hash_seconds += perf_counter() - io_started
            cached = previous_cache.get(path)
            if cached is not None and _cache_entry_matches_manifest(path, cached, {path: digest}):
                entry = cached
                cache_hit_count += 1
            else:
                parse_started = perf_counter()
                try:
                    module = _parse_module(path, text)
                    entry = (digest, module, None)
                    parsed_count += 1
                except (SyntaxError, ValueError, RecursionError) as exc:
                    entry = (digest, None, type(exc).__name__)
                    parsed_count += 1
                parse_seconds += perf_counter() - parse_started

        _entry_digest, module, failure = entry
        cache[path] = entry
        if module is not None:
            existing = modules.get(module.name)
            if existing is not None and existing.path != module.path:
                raise ValueError("Two Python files resolve to the same module name.")
            modules[module.name] = module
        elif failure and (path, failure) not in failures:
            failures.append((path, failure))

    return {
        "report": report,
        "manifest": manifest,
        "candidates": candidates,
        "modules": modules,
        "failures": failures,
        "cache": cache,
        "parsed_count": parsed_count,
        "read_count": read_count,
        "hash_count": hash_count,
        "cache_hit_count": cache_hit_count,
        "discovery_seconds": discovery_seconds,
        "read_hash_seconds": read_hash_seconds,
        "parse_seconds": parse_seconds,
    }


def _static_all(tree: ast.Module) -> tuple[tuple[str, ...] | None, int | None, int | None, bool]:
    found = None
    line = None
    end_line = None
    dynamic = False
    for node in tree.body:
        value = None
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "__all__" for target in node.targets
        ):
            value = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) \
                and node.target.id == "__all__":
            value = node.value
        elif isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name) \
                and node.target.id == "__all__":
            dynamic = True
            line = node.lineno
            end_line = getattr(node, "end_lineno", node.lineno) or node.lineno
            continue
        else:
            continue
        line = node.lineno
        end_line = getattr(node, "end_lineno", node.lineno) or node.lineno
        if isinstance(value, (ast.List, ast.Tuple, ast.Set)) and all(
            isinstance(item, ast.Constant) and isinstance(item.value, str)
            for item in value.elts
        ):
            found = tuple(item.value for item in value.elts)
        else:
            found = None
            dynamic = True
    return found, line, end_line, dynamic


def _parse_module(path: str, text: str) -> _Module:
    module, package, is_init = _module_for_path(path)
    tree = ast.parse(text.replace("\r\n", "\n").replace("\r", "\n"), filename=path)
    definitions = {}
    imports = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            definitions[node.name] = (
                _id("S", path, type(node).__name__, f"{module}.{node.name}"),
                node.lineno, getattr(node, "end_lineno", node.lineno) or node.lineno,
            )
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imports.append({
                    "kind": "import", "module": alias.name, "name": None,
                    "local": alias.asname or alias.name.split(".", 1)[0],
                    "alias": alias.asname is not None, "line": node.lineno,
                    "end": getattr(node, "end_lineno", node.lineno) or node.lineno,
                })
        elif isinstance(node, ast.ImportFrom):
            absolute = _absolute_module(package, node.module or "", node.level)
            for alias in node.names:
                imports.append({
                    "kind": "from", "module": absolute, "name": alias.name,
                    "local": alias.asname or alias.name,
                    "alias": alias.asname is not None, "line": node.lineno,
                    "end": getattr(node, "end_lineno", node.lineno) or node.lineno,
                })
        # Dynamic imports are evidence of uncertainty, never guessed targets.
        for child in ast.walk(node):
            if not isinstance(child, ast.Call):
                continue
            func = child.func
            dotted = (
                func.id if isinstance(func, ast.Name) else
                f"{func.value.id}.{func.attr}" if isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name) else None
            )
            if dotted not in {"__import__", "importlib.import_module"}:
                continue
            literal = child.args[0].value if child.args and isinstance(child.args[0], ast.Constant) \
                and isinstance(child.args[0].value, str) else None
            imports.append({
                "kind": "dynamic", "module": literal, "name": None,
                "local": None, "alias": False, "line": child.lineno,
                "end": getattr(child, "end_lineno", child.lineno) or child.lineno,
            })
    all_names, all_line, all_end_line, all_dynamic = _static_all(tree)
    return _Module(
        path, module, package, is_init, tree, _id("S", path, "module", module),
        definitions, imports, all_names, all_line, all_end_line, all_dynamic,
    )


def build_package_relation_graph(root, *, scan_limits: ScanLimits | None = None) -> PackageRelationGraph:
    """Build a bounded static graph from scanner-approved Python files only."""
    snapshot = _snapshot_and_parse(root, limits=scan_limits)
    return _build_package_relation_graph(
        snapshot["report"].root, snapshot["modules"], snapshot["failures"]
    )


def _build_package_relation_graph(repository, modules, failures,
                                  source_paths: set[str] | None = None) -> PackageRelationGraph:
    """Resolve all module bindings but materialize relations for selected sources."""
    def include(source: _Module) -> bool:
        return source_paths is None or source.path in source_paths

    relations = []
    occurrence = Counter()

    def add(kind, source, line, end_line=None, target_module=None, target_name=None, local=None,
            target_path=None, target_symbol_id=None, candidates=(), status="unresolved",
            cycle=False, note=""):
        if kind not in RELATION_TYPES or status not in RESOLUTION_STATES:
            raise ValueError("Invalid package relation.")
        semantic = (source.path, kind, target_module, target_name, local)
        ordinal = occurrence[semantic]
        occurrence[semantic] += 1
        relations.append(PackageRelation(
            relation_id=_id("PR", *semantic, ordinal), relation_type=kind,
            source_path=source.path, source_module=source.name,
            source_symbol_id=source.module_symbol_id,
            start_line=max(1, line), end_line=max(1, end_line or line),
            target_module=target_module, target_name=target_name, local_name=local,
            target_path=target_path, target_symbol_id=target_symbol_id,
            candidate_symbol_ids=tuple(sorted(set(candidates))), resolution_status=status,
            cycle_detected=cycle, note=note,
        ))

    bindings = defaultdict(lambda: defaultdict(list))
    for source in sorted(modules.values(), key=lambda item: item.path.casefold()):
        for item in source.imports:
            target_module = item["module"]
            if item["kind"] == "dynamic":
                if include(source):
                    add("imports", source, item["line"], item["end"], target_module=target_module,
                        status="uncertain", note="dynamic import is not statically resolvable")
                continue
            target = modules.get(target_module or "")
            if item["kind"] == "import":
                status = "resolved" if target else "unresolved"
                if include(source):
                    add("imports", source, item["line"], item["end"], target_module=target_module,
                        local=item["local"], target_path=target.path if target else None,
                        target_symbol_id=target.module_symbol_id if target else None,
                        candidates=(target.module_symbol_id,) if target else (), status=status)
                bindings[source.name][item["local"]].append((target_module, None, item["line"]))
            else:
                name = item["name"]
                if name == "*":
                    if include(source):
                        add("imports_symbol", source, item["line"], item["end"], target_module=target_module,
                            target_name="*", local="*", target_path=target.path if target else None,
                            status="uncertain", note="star import membership is not inferred")
                    continue
                bindings[source.name][item["local"]].append((target_module, name, item["line"]))
                symbol = target.definitions.get(name) if target else None
                submodule = modules.get(f"{target_module}.{name}") if target_module else None
                candidates = []
                if symbol:
                    candidates.append(symbol[0])
                if submodule:
                    candidates.append(submodule.module_symbol_id)
                status = "resolved" if len(candidates) == 1 else "ambiguous" if candidates else "unresolved"
                chosen_path = target.path if symbol else submodule.path if submodule else None
                chosen_id = candidates[0] if len(candidates) == 1 else None
                if include(source):
                    add("imports_symbol", source, item["line"], item["end"], target_module=target_module,
                        target_name=name, local=item["local"], target_path=chosen_path,
                        target_symbol_id=chosen_id, candidates=candidates, status=status)
            if item["alias"] and include(source):
                add("import_alias", source, item["line"], item["end"], target_module=target_module,
                    target_name=item["name"], local=item["local"],
                    target_path=target.path if target else None,
                    target_symbol_id=target.module_symbol_id if target and item["kind"] == "import" else None,
                    status="resolved" if target else "unresolved")

    def resolve_binding(module_name, local_name, trail=()):
        key = (module_name, local_name)
        if key in trail:
            return [], True
        module = modules.get(module_name)
        if not module:
            return [], False
        if local_name in module.definitions:
            symbol_id, start, end = module.definitions[local_name]
            return [(module.path, symbol_id, start, end)], False
        results = []
        cycle = False
        for target_module, target_name, _line in bindings[module_name].get(local_name, ()):
            if target_name is None:
                target = modules.get(target_module or "")
                if target:
                    results.append((target.path, target.module_symbol_id, 1, 1))
            else:
                found, hit_cycle = resolve_binding(target_module, target_name, trail + (key,))
                results.extend(found)
                cycle = cycle or hit_cycle
        unique = list(dict.fromkeys(results))
        return unique, cycle

    # Materialise import-binding resolution separately from the raw import
    # relation.  This lets a consumer alias resolve through one or more package
    # reexports while preserving ambiguous/cyclic outcomes rather than guessing.
    for source in sorted(modules.values(), key=lambda item: item.path.casefold()):
        if not include(source):
            continue
        for local_name, bound in sorted(bindings[source.name].items()):
            found = []
            cycle = False
            for target_module, target_name, line in bound:
                if target_name is None:
                    target = modules.get(target_module or "")
                    if target:
                        found.append((target.path, target.module_symbol_id, 1, 1))
                    continue
                nested, hit_cycle = resolve_binding(target_module, target_name)
                found.extend(nested)
                cycle = cycle or hit_cycle
            found = list(dict.fromkeys(found))
            ids = tuple(item[1] for item in found)
            status = "resolved" if len(found) == 1 and not cycle else \
                "ambiguous" if len(found) > 1 else "unresolved"
            target_module = bound[0][0] if len(bound) == 1 else None
            target_path = found[0][0] if len(found) == 1 and not cycle else None
            target_id = found[0][1] if len(found) == 1 and not cycle else None
            add("resolves_to", source, bound[0][2], target_module=target_module,
                target_name=local_name, local=local_name, target_path=target_path,
                target_symbol_id=target_id, candidates=ids, status=status, cycle=cycle,
                note="import binding cycle detected" if cycle else "import binding resolution")

    for source in sorted(modules.values(), key=lambda item: item.path.casefold()):
        if not include(source) or not source.is_init:
            continue
        if source.all_dynamic:
            add("exports_symbol", source, source.all_line or 1, source.all_end_line,
                target_name="<dynamic __all__>", status="uncertain",
                note="__all__ is not a static string collection")
            continue
        if source.all_names is None:
            continue
        for name in source.all_names:
            direct = source.definitions.get(name)
            if direct:
                add("exports_symbol", source, source.all_line or direct[1], source.all_end_line,
                    target_module=source.name,
                    target_name=name, local=name, target_path=source.path,
                    target_symbol_id=direct[0], candidates=(direct[0],), status="resolved")
                add("resolves_to", source, source.all_line or direct[1], source.all_end_line,
                    target_module=source.name,
                    target_name=name, local=name, target_path=source.path,
                    target_symbol_id=direct[0], candidates=(direct[0],), status="resolved")
                continue
            found, cycle = resolve_binding(source.name, name)
            ids = tuple(item[1] for item in found)
            status = "resolved" if len(found) == 1 and not cycle else \
                "ambiguous" if len(found) > 1 else "unresolved"
            target_path = found[0][0] if len(found) == 1 and not cycle else None
            target_id = found[0][1] if len(found) == 1 and not cycle else None
            imported = bindings[source.name].get(name, ())
            target_module = imported[0][0] if len(imported) == 1 else None
            add("reexports_symbol", source, source.all_line or 1, source.all_end_line,
                target_module=target_module,
                target_name=name, local=name, target_path=target_path,
                target_symbol_id=target_id, candidates=ids, status=status, cycle=cycle,
                note="cycle detected" if cycle else "")
            if target_id:
                add("resolves_to", source, source.all_line or 1, source.all_end_line,
                    target_module=target_module,
                    target_name=name, local=name, target_path=target_path,
                    target_symbol_id=target_id, candidates=(target_id,), status="resolved")

    return PackageRelationGraph(
        repository=str(repository),
        relations=tuple(sorted(relations, key=lambda item: (
            item.source_path.casefold(), item.start_line, item.relation_type, item.relation_id
        ))),
        parse_failures=tuple(sorted(failures)),
    )


class PackageRelationRetriever:
    """Explicit research retriever over a frozen in-memory package graph."""

    def __init__(self, graph: PackageRelationGraph):
        self.graph = graph

    def search(self, query: str, *, top_k: int = 10, relation_type: str | None = None,
               source_module: str | None = None, target_module: str | None = None,
               symbol_name: str | None = None, resolution_status: str | None = None):
        if not isinstance(query, str) or not query.strip() or len(query) > MAX_QUERY_LENGTH:
            raise ValueError("Package relation query must be non-empty and within the query limit.")
        if isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= MAX_TOP_K:
            raise ValueError(f"top_k must be between 1 and {MAX_TOP_K}.")
        if relation_type is not None and relation_type not in RELATION_TYPES:
            raise ValueError("Unknown package relation type.")
        if resolution_status is not None and resolution_status not in RESOLUTION_STATES:
            raise ValueError("Unknown package relation resolution status.")
        tokens = set(terms(query))
        query_folded = query.casefold()
        intent = set()
        if any(word in tokens for word in ("reexport", "reexports")) or "re-export" in query_folded:
            intent.add("reexports_symbol")
        if any(word in tokens for word in ("export", "exports", "public", "all")):
            intent.update(("exports_symbol", "reexports_symbol"))
        if any(word in tokens for word in ("import", "imports", "imported", "depends", "dependency")):
            intent.update(("imports", "imports_symbol"))
        if any(word in tokens for word in ("alias", "aliased", "as")):
            intent.add("import_alias")
        if any(word in tokens for word in ("resolve", "resolves", "resolved", "definition", "defined")):
            intent.add("resolves_to")
        results = []
        for relation in self.graph.relations:
            if relation_type and relation.relation_type != relation_type:
                continue
            if source_module and relation.source_module != source_module:
                continue
            if target_module and relation.target_module != target_module:
                continue
            if symbol_name and symbol_name not in {relation.target_name, relation.local_name}:
                continue
            if resolution_status and relation.resolution_status != resolution_status:
                continue
            haystack = " ".join(filter(None, (
                relation.source_module, relation.target_module, relation.target_name,
                relation.local_name, relation.relation_type.replace("_", " "),
            ))).replace(".", " ").replace("_", " ").casefold()
            relation_terms = set(terms(haystack))
            score = 10.0 * sum(value is not None for value in (
                relation_type, source_module, target_module, symbol_name, resolution_status
            )) + 2.0 * len(tokens & relation_terms)
            if relation.relation_type in intent:
                score += 6.0
            for exact in (relation.source_module, relation.target_module,
                          relation.target_name, relation.local_name):
                if exact and exact.casefold() in query_folded:
                    score += 8.0
            if relation.cycle_detected and "cycle" in tokens:
                score += 5.0
            if score <= 0:
                continue
            text = (
                f"{relation.source_module} {relation.relation_type} "
                f"{relation.target_module or ''}.{relation.target_name or ''}"
            ).strip(".")
            result_path = (
                relation.target_path if relation.relation_type == "resolves_to"
                and relation.target_path else relation.source_path
            )
            results.append(PackageRelationResult(
                relation.relation_id, result_path, relation.start_line,
                relation.end_line, text, score, relation.relation_type,
                relation.resolution_status, relation.target_path, relation.target_symbol_id,
            ))
        results.sort(key=lambda item: (-item.score, item.relative_path.casefold(),
                                       item.start_line, item.relation_id))
        unique = []
        seen = set()
        for result in results:
            if result.relative_path in seen:
                continue
            seen.add(result.relative_path)
            unique.append(result)
            if len(unique) >= top_k:
                break
        return unique


def _dependency_map(modules: dict[str, _Module]):
    dependencies = {}
    for module in modules.values():
        targets = set()
        if module.all_dynamic:
            return None, "dynamic_all"
        for item in module.imports:
            target = item["module"]
            if item["kind"] == "dynamic":
                return None, "dynamic_import"
            if target is None:
                return None, "unresolved_relative_import"
            targets.add(target)
            if item["kind"] == "from":
                if item["name"] == "*":
                    return None, "star_import"
                targets.add(f"{target}.{item['name']}")
        dependencies[module.name] = targets
    return dependencies, None


def _affected_module_names(changed_names, old_modules, current_modules):
    old_dependencies, old_reason = _dependency_map(old_modules)
    current_dependencies, current_reason = _dependency_map(current_modules)
    if old_reason or current_reason:
        return None, old_reason or current_reason
    dependencies = defaultdict(set)
    for table in (old_dependencies, current_dependencies):
        for source, targets in table.items():
            dependencies[source].update(targets)
    affected = set(changed_names)
    while True:
        target_keys = set(affected)
        dependents = {
            source for source, targets in dependencies.items()
            if targets & target_keys and source not in affected
        }
        if not dependents:
            break
        affected.update(dependents)
    return affected, None


def _graph_from_modules(repository, modules, failures, source_paths=None):
    if source_paths is None:
        selected_failures = failures
    else:
        selected_failures = [item for item in failures if item[0] in source_paths]
    return _build_package_relation_graph(
        repository, modules, selected_failures, source_paths=source_paths
    )


def _update_relation_graph_delta(repository, previous_graph, old_modules, modules,
                                 failures, changed_paths):
    """Build a graph update, using a conservative full-graph fallback as needed."""
    changed_names = {_module_for_path(path)[0] for path in changed_paths}
    if failures or previous_graph.parse_failures:
        affected_names = None
        fallback_reason = "parse_failure_or_unreadable_file"
    else:
        affected_names, fallback_reason = _affected_module_names(
            changed_names, old_modules, modules
        )
    if affected_names is None:
        affected_paths = set(modules[module].path for module in modules)
        affected_paths.update(changed_paths)
        candidate_graph = _graph_from_modules(repository, modules, failures)
        full_rebuild = True
    else:
        old_paths_by_name = {module.name: module.path for module in old_modules.values()}
        new_paths_by_name = {module.name: module.path for module in modules.values()}
        affected_paths = set(changed_paths)
        for name in affected_names:
            if name in old_paths_by_name:
                affected_paths.add(old_paths_by_name[name])
            if name in new_paths_by_name:
                affected_paths.add(new_paths_by_name[name])
        delta = _graph_from_modules(
            repository, modules, failures, source_paths=affected_paths
        )
        retained_relations = [
            relation for relation in previous_graph.relations
            if relation.source_path not in affected_paths
        ]
        combined = retained_relations + list(delta.relations)
        ids = [relation.relation_id for relation in combined]
        if len(ids) != len(set(ids)):
            raise ValueError("Incremental merge produced duplicate relation IDs.")
        relation_order = lambda relation: (
            relation.source_path.casefold(), relation.start_line,
            relation.relation_type, relation.relation_id,
        )
        failures_by_path = {
            path: error for path, error in previous_graph.parse_failures
            if path not in affected_paths
        }
        failures_by_path.update(dict(delta.parse_failures))
        candidate_graph = PackageRelationGraph(
            str(repository), tuple(sorted(combined, key=relation_order)),
            tuple(sorted(failures_by_path.items())),
        )
        full_rebuild = False
    old_by_id = {relation.relation_id: relation for relation in previous_graph.relations}
    new_by_id = {relation.relation_id: relation for relation in candidate_graph.relations}
    stats = {
        "full_rebuild": full_rebuild,
        "fallback_reason": fallback_reason,
        "affected_paths": affected_paths,
        "added_relation_count": len(set(new_by_id) - set(old_by_id)),
        "removed_relation_count": len(set(old_by_id) - set(new_by_id)),
        "modified_relation_count": sum(
            old_by_id[item] != new_by_id[item]
            for item in set(old_by_id) & set(new_by_id)
        ),
    }
    return candidate_graph, stats


class IncrementalPackageRelationIndex:
    """Atomic in-memory relation index with dependency-aware source updates.

    Every Python file is content-hashed to detect changes. Unchanged ASTs are
    reused. For statically knowable import graphs, only changed module sources
    and their transitive reverse dependents have relations rematerialized; all
    cached modules remain available to resolution. Dynamic imports, dynamic
    ``__all__``, star imports, parse failures and other incomplete dependency
    maps conservatively trigger a complete relation rebuild. There is no
    persistent schema or filesystem write.
    """

    def __init__(self, *, builder=build_package_relation_graph,
                 scan_limits: ScanLimits | None = None):
        self._builder = builder
        self._scan_limits = scan_limits
        self.graph: PackageRelationGraph | None = None
        self._manifest: dict[str, str] = {}
        self._parsed_cache: _ParsedCache = {}
        self._cache_root: str | None = None
        self._cache_version = PACKAGE_RELATION_CACHE_VERSION

    @staticmethod
    def _snapshot(root) -> dict[str, str]:
        return _snapshot_repository(root)[1]

    def _module_maps(self, cache):
        modules = {}
        for _path, (_digest, module, _failure) in cache.items():
            if module is not None:
                modules[module.name] = module
        return modules

    def refresh(self, root) -> dict:
        started = perf_counter()
        discovery_seconds = 0.0
        read_hash_seconds = 0.0
        parse_seconds = 0.0
        graph_seconds = 0.0
        snapshot = None
        previous_graph = self.graph
        previous_manifest = dict(self._manifest)
        previous_cache = dict(self._parsed_cache)
        previous_cache_root = self._cache_root
        previous_cache_version = self._cache_version
        try:
            cache_entries_valid = (
                set(previous_cache) == set(previous_manifest)
                and all(_cache_entry_matches_manifest(path, entry, previous_manifest)
                        for path, entry in previous_cache.items())
            )
            cache_header_valid = (
                previous_graph is not None
                and previous_cache_version == PACKAGE_RELATION_CACHE_VERSION
                and previous_cache_root == previous_graph.repository
                and cache_entries_valid
            )
            snapshot = _snapshot_and_parse(
                root,
                limits=self._scan_limits,
                previous_cache=previous_cache if cache_header_valid else {},
                previous_root=previous_cache_root if cache_header_valid else None,
            )
            report = snapshot["report"]
            current = snapshot["manifest"]
            candidates = snapshot["candidates"]
            modules = snapshot["modules"]
            failures = snapshot["failures"]
            current_cache = snapshot["cache"]
            discovery_seconds = snapshot["discovery_seconds"]
            read_hash_seconds = snapshot["read_hash_seconds"]
            parse_seconds = snapshot["parse_seconds"]
            current_root = str(report.root)
            added = sorted(set(current) - set(previous_manifest), key=str.casefold)
            deleted = sorted(set(previous_manifest) - set(current), key=str.casefold)
            modified = sorted(
                (path for path in set(current) & set(previous_manifest)
                 if current[path] != previous_manifest[path]),
                key=str.casefold,
            )
            changed = sorted(set(added) | set(deleted) | set(modified), key=str.casefold)
            root_changed = previous_graph is None or previous_graph.repository != current_root
            cache_invalidated = not cache_header_valid or root_changed
            if previous_graph is not None and not changed \
                    and previous_graph.repository == current_root and not cache_invalidated:
                self._parsed_cache = current_cache
                self._cache_root = current_root
                self._cache_version = PACKAGE_RELATION_CACHE_VERSION
                return {
                    "status": "unchanged", "added": [], "modified": [], "deleted": [],
                    "elapsed_seconds": round(perf_counter() - started, 6),
                    "discovery_seconds": round(discovery_seconds, 6),
                    "read_hash_seconds": round(read_hash_seconds, 6),
                    "scan_hash_seconds": round(discovery_seconds + read_hash_seconds, 6),
                    "parse_seconds": round(parse_seconds, 6),
                    "relation_update_seconds": 0.0,
                    "scan_candidate_count": len(candidates),
                    "scanner_candidate_file_count": len(report.candidates),
                    "python_file_count": len(candidates),
                    "scan_entries_visited": report.entries_visited,
                    "scan_issue_count": len(report.issues),
                    "relation_count": len(previous_graph.relations), "full_rebuild": False,
                    "reparsed_file_count": 0, "affected_source_count": 0,
                    "content_read_count": snapshot["read_count"],
                    "content_hash_count": snapshot["hash_count"],
                    "ast_cache_hit_count": snapshot["cache_hit_count"],
                    "parse_failure_count": len(failures),
                    "cache_invalidated": False,
                    "added_relation_count": 0, "removed_relation_count": 0,
                    "modified_relation_count": 0,
                }

            reparsed_count = snapshot["parsed_count"]
            graph_started = perf_counter()
            if self._builder is not build_package_relation_graph:
                candidate_graph = self._builder(root)
                affected_paths = set(current)
                update_stats = {
                    "full_rebuild": True, "fallback_reason": "custom_builder",
                    "affected_paths": affected_paths,
                    "added_relation_count": 0, "removed_relation_count": 0,
                    "modified_relation_count": 0,
                }
            elif cache_invalidated:
                candidate_graph = _graph_from_modules(str(report.root), modules, failures)
                affected_paths = set(current)
                fallback_reason = (
                    "initial_build_or_repository_changed"
                    if root_changed else "cache_missing_corrupt_or_version_mismatch"
                )
                old_ids = set() if previous_graph is None else {
                    relation.relation_id for relation in previous_graph.relations
                }
                new_ids = {relation.relation_id for relation in candidate_graph.relations}
                update_stats = {
                    "full_rebuild": True,
                    "fallback_reason": fallback_reason,
                    "affected_paths": affected_paths,
                    "added_relation_count": len(new_ids - old_ids),
                    "removed_relation_count": len(old_ids - new_ids),
                    "modified_relation_count": 0,
                }
            else:
                old_modules = self._module_maps(previous_cache)
                candidate_graph, update_stats = _update_relation_graph_delta(
                    str(report.root), previous_graph, old_modules, modules, failures, changed
                )
            graph_seconds = perf_counter() - graph_started
            affected_paths = update_stats["affected_paths"]
            affected_source_count = len(set(current) & affected_paths)
            self.graph = candidate_graph
            self._manifest = current
            self._parsed_cache = current_cache
            self._cache_root = current_root
            self._cache_version = PACKAGE_RELATION_CACHE_VERSION
            return {
                "status": "updated", "added": added, "modified": modified, "deleted": deleted,
                "elapsed_seconds": round(perf_counter() - started, 6),
                "discovery_seconds": round(discovery_seconds, 6),
                "read_hash_seconds": round(read_hash_seconds, 6),
                "scan_hash_seconds": round(discovery_seconds + read_hash_seconds, 6),
                "parse_seconds": round(parse_seconds, 6),
                "relation_update_seconds": round(graph_seconds, 6),
                "scan_candidate_count": len(candidates),
                "scanner_candidate_file_count": len(report.candidates),
                "python_file_count": len(candidates),
                "scan_entries_visited": report.entries_visited,
                "scan_issue_count": len(report.issues),
                "relation_count": len(candidate_graph.relations),
                "full_rebuild": update_stats["full_rebuild"],
                "fallback_reason": update_stats["fallback_reason"],
                "reparsed_file_count": reparsed_count,
                "affected_source_count": affected_source_count,
                "content_read_count": snapshot["read_count"],
                "content_hash_count": snapshot["hash_count"],
                "ast_cache_hit_count": snapshot["cache_hit_count"],
                "parse_failure_count": len(candidate_graph.parse_failures),
                "cache_invalidated": cache_invalidated,
                "added_relation_count": update_stats["added_relation_count"],
                "removed_relation_count": update_stats["removed_relation_count"],
                "modified_relation_count": update_stats["modified_relation_count"],
                "parse_failures": [list(value) for value in candidate_graph.parse_failures],
            }
        except Exception as exc:
            self.graph = previous_graph
            self._manifest = previous_manifest
            self._parsed_cache = previous_cache
            self._cache_root = previous_cache_root
            self._cache_version = previous_cache_version
            report = None if snapshot is None else snapshot["report"]
            return {
                "status": "failed_preserved", "error_type": type(exc).__name__,
                "elapsed_seconds": round(perf_counter() - started, 6),
                "discovery_seconds": round(discovery_seconds, 6),
                "read_hash_seconds": round(read_hash_seconds, 6),
                "scan_hash_seconds": round(discovery_seconds + read_hash_seconds, 6),
                "parse_seconds": round(parse_seconds, 6),
                "relation_update_seconds": round(graph_seconds, 6),
                "scan_candidate_count": 0 if report is None else len(snapshot["candidates"]),
                "scanner_candidate_file_count": 0 if report is None else len(report.candidates),
                "python_file_count": 0 if snapshot is None else len(snapshot["candidates"]),
                "scan_entries_visited": 0 if report is None else report.entries_visited,
                "scan_issue_count": 0 if report is None else len(report.issues),
                "relation_count": 0 if previous_graph is None else len(previous_graph.relations),
                "full_rebuild": False,
                "reparsed_file_count": 0, "affected_source_count": 0,
                "content_read_count": 0 if snapshot is None else snapshot["read_count"],
                "content_hash_count": 0 if snapshot is None else snapshot["hash_count"],
                "ast_cache_hit_count": 0 if snapshot is None else snapshot["cache_hit_count"],
                "parse_failure_count": 0 if previous_graph is None else len(previous_graph.parse_failures),
                "cache_invalidated": False,
                "added_relation_count": 0, "removed_relation_count": 0,
                "modified_relation_count": 0,
            }
