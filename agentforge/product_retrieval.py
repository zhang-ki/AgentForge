"""Intent-driven, evidence-preserving retrieval for the bounded product agent.

This module deliberately does not use RRF and does not make Vector mandatory.  A
route selects ordered evidence channels; unavailable optional channels are exposed
instead of silently being treated as successful.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
from pathlib import Path
import re

from .chunker import split_source_lines
from .errors import StoreError
from .evidence_roles import required_evidence_roles
from .models import SearchResult
from .package_relations import (
    PackageRelationRetriever, PackageSnapshotReadError, build_package_relation_graph,
)
from .query_routing import ExplainableQueryRouter, RouteDecision
from .retriever import search as lexical_search
from .scanner import ScanLimits, ScanReport, read_candidate, scan_repository


PRODUCT_ROUTE_VERSION = "af011-product-route-v1"
_SYMBOL_SHAPE = re.compile(
    r"(?:\b(?:class|function|method|module|symbol)\s+|(?:函数|方法|类|模块|符号)\s*)[`'\"]?([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)",
    re.IGNORECASE,
)
_CALL_WORDS = {"caller", "callers", "callee", "callees", "call", "calls",
               "called", "invoke", "invokes", "invoked"}
_DOCUMENT_SUFFIXES = (".md", ".rst", ".txt")
_BARE_IDENTIFIER = re.compile(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*$")
_RELATION_STOPWORDS = {
    "which", "what", "where", "package", "module", "public", "api", "reexport",
    "reexports", "export", "exports", "import", "imports", "caller", "callers",
    "callee", "callees", "call", "calls", "called", "function", "method", "class",
    "symbol", "as", "a", "an", "the", "is", "does", "from", "to",
}


@dataclass(frozen=True)
class ProductCandidate:
    repository: str
    relative_path: str
    start_line: int
    end_line: int
    text: str
    content_hash: str
    score: float
    channels: tuple[str, ...]
    channel_scores: tuple[tuple[str, float], ...]
    symbol_ids: tuple[str, ...] = ()
    relation_ids: tuple[str, ...] = ()
    resolution_status: str = "not_applicable"
    evidence_roles: tuple[str, ...] = ()

    @property
    def citation(self) -> str:
        return f"{self.relative_path}:{self.start_line}-{self.end_line}"

    def as_search_result(self) -> SearchResult:
        return SearchResult(
            self.repository, self.relative_path, self.start_line, self.end_line,
            self.text, self.content_hash, self.score, self.evidence_roles,
        )


@dataclass(frozen=True)
class ProductRetrievalOutcome:
    query: str
    route_version: str
    decision: RouteDecision
    candidates: tuple[ProductCandidate, ...]
    unavailable_channels: tuple[str, ...]
    parse_failures: tuple[tuple[str, str], ...]
    uncertainty: str | None


class ProductIntentRouter:
    """Small product wrapper that leaves the frozen research router unchanged."""

    version = PRODUCT_ROUTE_VERSION

    def __init__(self):
        self._base = ExplainableQueryRouter()

    def route(self, query: str) -> RouteDecision:
        stripped = query.strip() if isinstance(query, str) else query
        if _is_explicit_symbol_reference(stripped):
            return RouteDecision(
                "exact_symbol", ("symbols", "lexical"), "explicit_symbol_reference",
                "An explicit Python symbol reference is treated as an exact lookup.", "high",
            )
        return self._base.route(query)


def _content_hash(path: str, start: int, end: int, text: str) -> str:
    value = f"{path}\0{start}\0{end}\0{text}".encode("utf-8")
    return hashlib.sha256(value).hexdigest()


class ProductRetriever:
    """Use deterministic intent routing while retaining per-channel provenance."""

    def __init__(self, store, repository, *, vector_retriever=None,
                 limits: ScanLimits | None = None, scan_report: ScanReport | None = None):
        self.store = store
        self.limits = limits or ScanLimits()
        report = scan_report or scan_repository(repository, self.limits)
        if not report.complete:
            raise ValueError("Repository scan is incomplete; product retrieval is refused.")
        requested_root = str(Path(repository).resolve(strict=True))
        actual_root = str(report.root.resolve(strict=True))
        if actual_root.casefold() != requested_root.casefold():
            raise ValueError("Scanner report does not belong to the requested repository.")
        self.repository = report.root
        self._candidates = {item.relative_path: item for item in report.candidates}
        self.vector_retriever = vector_retriever
        self.router = ProductIntentRouter()
        self._relation_graph = None
        self._relation_failures = ()
        self._relation_attempted = False
        self._file_text_cache = {}
        self._index_stale = False
        self._channel_failures = []

    def _ensure_index_matches(self) -> None:
        indexed_root = self.store.metadata().get("repository")
        if not indexed_root:
            raise ValueError("No repository index is available; index the authorized repository first.")
        try:
            normalized = str(Path(indexed_root).resolve(strict=True))
        except (OSError, RuntimeError, ValueError):
            raise ValueError("The current index repository is unavailable or cannot be verified.") from None
        if normalized.casefold() != str(self.repository).casefold():
            raise ValueError("The index belongs to a different repository; refusing cross-repository evidence.")

    def _current_file(self, relative_path: str) -> tuple[str, str] | None:
        if relative_path in self._file_text_cache:
            return self._file_text_cache[relative_path]
        candidate = self._candidates.get(relative_path)
        if candidate is None:
            return None
        try:
            text = read_candidate(candidate, self.repository, self.limits.max_file_bytes)
        except (OSError, ValueError):
            self._index_stale = True
            return None
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        value = (text, digest)
        self._file_text_cache[relative_path] = value
        return value

    def _indexed_file_is_current(self, relative_path: str) -> bool:
        current = self._current_file(relative_path)
        if current is None:
            self._index_stale = True
            return False
        if self.store.indexed_file_hash(relative_path) != current[1]:
            self._index_stale = True
            return False
        return True

    def _excerpt(self, relative_path: str, start_line: int, end_line: int) -> str:
        current = self._current_file(relative_path)
        if current is None:
            raise ValueError("Structured result points outside the scanner-approved snapshot.")
        lines = split_source_lines(current[0])
        if not lines or start_line < 1 or end_line < start_line or end_line > len(lines):
            raise ValueError("Structured result contains an invalid source line range.")
        return "\n".join(lines[start_line - 1:end_line])

    @staticmethod
    def _symbol_name(query: str) -> str | None:
        stripped = query.strip()
        if _BARE_IDENTIFIER.fullmatch(stripped):
            return stripped.split(".")[-1]
        match = _SYMBOL_SHAPE.search(query)
        if match:
            return match.group(1).split(".")[-1]
        mentioned = _mentioned_code_symbol(query)
        return mentioned.split(".")[-1] if mentioned else None

    @staticmethod
    def _qualified_symbol_name(query: str) -> str | None:
        stripped = query.strip()
        if _BARE_IDENTIFIER.fullmatch(stripped) and "." in stripped:
            return stripped
        match = _SYMBOL_SHAPE.search(query)
        value = match.group(1) if match else _mentioned_code_symbol(query)
        return value if value and "." in value else None

    @staticmethod
    def _relation_target(query: str) -> str | None:
        quoted = re.findall(r"[`'\"]([A-Za-z_]\w*)[`'\"]", query)
        if quoted:
            return quoted[-1]
        identifiers = re.findall(r"\b[A-Za-z_]\w*\b", query)
        candidates = [value for value in identifiers
                      if value.casefold() not in _RELATION_STOPWORDS]
        code_shaped = [value for value in candidates
                       if "_" in value or any(char.isupper() for char in value[1:])]
        return code_shaped[-1] if code_shaped else None

    def _lexical(self, query: str, top_k: int) -> list[ProductCandidate]:
        results = []
        for item in lexical_search(self.store, query, top_k=top_k):
            if not self._indexed_file_is_current(item.relative_path):
                continue
            try:
                excerpt = self._excerpt(item.relative_path, item.start_line, item.end_line)
            except ValueError:
                self._index_stale = True
                continue
            if excerpt != item.text:
                self._index_stale = True
                continue
            results.append(ProductCandidate(
                item.repository, item.relative_path, item.start_line, item.end_line,
                excerpt, item.content_hash, item.score,
                ("lexical",), (("lexical", item.score),),
            ))
        return results

    def _symbols(self, query: str, top_k: int) -> list[ProductCandidate]:
        name = self._symbol_name(query)
        if name is None:
            return []
        try:
            qualified_name = self._qualified_symbol_name(query)
            rows = self.store.search_symbols(
                qualified_name=qualified_name,
                name=None if qualified_name else name,
                limit=min(500, top_k * 4),
            )
            # Comparison questions need both definitions, including a plain
            # identifier without an underscore. Only exact indexed names are
            # accepted; unrelated words produce no candidates.
            if re.search(r"(?:与|和|\bversus\b|\bvs\.?\b)", query, re.IGNORECASE):
                seen = {row["symbol_id"] for row in rows}
                for other in re.findall(r"[A-Za-z_]\w*", query):
                    if other == name:
                        continue
                    for row in self.store.search_symbols(name=other, limit=min(500, top_k * 4)):
                        if row["symbol_id"] not in seen:
                            rows.append(row)
                            seen.add(row["symbol_id"])
        except StoreError:
            self._channel_failures.append(("symbols", "structured_symbol_index_unavailable"))
            return []
        results = []
        name_counts = {}
        for row in rows:
            name_counts[row["name"]] = name_counts.get(row["name"], 0) + 1
        for rank, row in enumerate(rows):
            if not self._indexed_file_is_current(row["relative_path"]):
                continue
            text = self._excerpt(row["relative_path"], row["start_line"], row["end_line"])
            score = float(100 - rank)
            results.append(ProductCandidate(
                str(self.repository), row["relative_path"], row["start_line"], row["end_line"],
                text, _content_hash(row["relative_path"], row["start_line"], row["end_line"], text),
                score, ("symbols",), (("symbols", score),),
                symbol_ids=(row["symbol_id"],),
                resolution_status="ambiguous" if name_counts[row["name"]] > 1 and qualified_name is None else "resolved",
                evidence_roles=("definition",),
            ))
        return results

    def _package_relations(self, query: str, top_k: int) -> list[ProductCandidate]:
        if self._relation_graph is None and not self._relation_attempted:
            self._relation_attempted = True
            try:
                self._relation_graph = build_package_relation_graph(
                    self.repository, scan_limits=self.limits,
                )
            except PackageSnapshotReadError as exc:
                self._relation_failures = ((exc.relative_path, exc.reason),)
                return []
            except (OSError, ValueError) as exc:
                self._relation_failures = (("<repository>", type(exc).__name__),)
                return []
        if self._relation_graph is None:
            return []
        by_id = {item.relation_id: item for item in self._relation_graph.relations}
        relation_target = self._relation_target(query)
        query_words = {word.casefold() for word in re.findall(r"[A-Za-z_]+", query)}
        call_query = bool(query_words & _CALL_WORDS) or "callsite" in required_evidence_roles(query)
        raw = PackageRelationRetriever(self._relation_graph).search(
            query, top_k=top_k,
            symbol_name=relation_target if relation_target else None,
        )
        results = []
        caller_evidence = []
        callsite_evidence = []
        definition_evidence = []
        for item in raw:
            relation = by_id[item.relation_id]
            # A relation is evidenced at its source statement.  A resolves_to target
            # path cannot reuse the source import line number.
            text = self._excerpt(
                relation.source_path, relation.start_line, relation.end_line,
            )
            results.append(ProductCandidate(
                str(self.repository), relation.source_path,
                relation.start_line, relation.end_line, text,
                _content_hash(relation.source_path, relation.start_line, relation.end_line, text),
                item.score, ("package_relations",), (("package_relations", item.score),),
                # PackageRelation currently assigns graph-local symbol IDs;
                # retain its own relation ID, but do not expose those IDs as
                # identities from the SQLite symbol table.
                symbol_ids=(),
                relation_ids=(relation.relation_id,),
                resolution_status=relation.resolution_status,
                evidence_roles=({
                    "imports": ("import",),
                    "imports_symbol": ("import",),
                    "import_alias": ("import",),
                    "exports_symbol": ("reexport",),
                    "reexports_symbol": ("reexport",),
                    "resolves_to": ("static_resolution",),
                }.get(relation.relation_type, ("static_relation",))),
            ))
            # Add the actual definition only when the static graph resolved a
            # unique import/re-export target. A relation's import line is never
            # reused as the target definition's line range.
            if (relation.relation_type in {"resolves_to", "reexports_symbol"}
                    and relation.resolution_status == "resolved"
                    and relation.target_symbol_id and relation.target_path):
                try:
                    definitions = self.store.search_symbols(
                        name=relation.target_definition_name,
                        relative_path=relation.target_path, limit=100,
                    ) if relation.target_definition_name else []
                except StoreError:
                    definitions = []
                    self._channel_failures.append((
                        "symbols", "structured_symbol_index_unavailable",
                    ))
                matches = [row for row in definitions
                           if row["relative_path"].casefold() == relation.target_path.casefold()
                           and row["kind"] in {"class", "function", "method"}
                           and row["end_line"] == relation.target_definition_end_line
                           and relation.target_definition_start_line is not None
                           and row["start_line"] <= relation.target_definition_start_line]
                # The graph and symbol table have independent identity formulas.
                # Join only when path, name and source range identify one exact
                # indexed definition; otherwise omit the supplemental excerpt.
                definition = matches[0] if len(matches) == 1 else None
                if definition is not None and self._indexed_file_is_current(definition["relative_path"]):
                    definition_text = self._excerpt(
                        definition["relative_path"], definition["start_line"], definition["end_line"],
                    )
                    definition_candidate = ProductCandidate(
                        str(self.repository), definition["relative_path"],
                        definition["start_line"], definition["end_line"], definition_text,
                        _content_hash(definition["relative_path"], definition["start_line"],
                                      definition["end_line"], definition_text),
                        950.0 if call_query else max(0.01, item.score - 0.01), ("package_relations",),
                        (("package_relations", 950.0 if call_query else max(0.01, item.score - 0.01)),),
                        symbol_ids=(definition["symbol_id"],),
                        relation_ids=(relation.relation_id,),
                        resolution_status="resolved",
                        evidence_roles=("target_definition", "definition"),
                    )
                    (definition_evidence if call_query else results).append(definition_candidate)
        # The relation graph describes import and re-export statements.  Actual
        # callsites live in the AST reference index and must be retrieved by the
        # code-shaped target, never by the last prose word in the question.
        if relation_target is not None and call_query:
            try:
                references = self.store.search_references(
                    target=relation_target, limit=min(500, top_k * 20),
                )
            except StoreError:
                references = []
                self._channel_failures.append(("references", "structured_reference_index_unavailable"))
            for rank, row in enumerate(references):
                if not self._indexed_file_is_current(row["relative_path"]):
                    continue
                if row["reference_type"] == "call" and row["source_symbol_id"]:
                    try:
                        callers = self.store.search_symbols_by_ids(
                            (row["source_symbol_id"],), limit=1,
                        )
                    except StoreError:
                        callers = []
                        self._channel_failures.append((
                            "symbols", "structured_symbol_index_unavailable",
                        ))
                    caller = next((item for item in callers
                                   if item["symbol_id"] == row["source_symbol_id"]
                                   and item["relative_path"].casefold() == row["relative_path"].casefold()
                                   and item["kind"] in {"class", "function", "method"}), None)
                    if caller is not None and self._indexed_file_is_current(caller["relative_path"]):
                        caller_text = self._excerpt(
                            caller["relative_path"], caller["start_line"], caller["end_line"],
                        )
                        caller_score = float(91 - rank)
                        caller_evidence.append(ProductCandidate(
                            str(self.repository), caller["relative_path"], caller["start_line"],
                            caller["end_line"], caller_text,
                            _content_hash(caller["relative_path"], caller["start_line"],
                                          caller["end_line"], caller_text),
                            caller_score, ("references",), (("references", caller_score),),
                            symbol_ids=(caller["symbol_id"],),
                            relation_ids=(row["reference_id"],),
                            resolution_status="resolved",
                            evidence_roles=("caller_definition", "definition"),
                        ))
                    # Preserve the exact call expression as a separate source
                    # range.  An import or re-export on the same file is not a
                    # substitute for the caller's executed syntax.
                    call_text = self._excerpt(row["relative_path"], row["line"], row["line"])
                    callsite_evidence.append(ProductCandidate(
                        str(self.repository), row["relative_path"], row["line"], row["line"],
                        call_text, _content_hash(row["relative_path"], row["line"],
                                                 row["line"], call_text),
                        990.0 - rank, ("references",), (("references", 990.0 - rank),),
                        symbol_ids=tuple(filter(None, (row["source_symbol_id"],
                                                        row["target_symbol_id"]))),
                        relation_ids=(row["reference_id"],),
                        resolution_status=row["resolution_status"],
                        evidence_roles=("callsite",),
                    ))
                text = self._excerpt(row["relative_path"], row["line"], row["line"])
                score = float(90 - rank)
                results.append(ProductCandidate(
                    str(self.repository), row["relative_path"], row["line"], row["line"],
                    text, _content_hash(row["relative_path"], row["line"], row["line"], text),
                    score, ("references",), (("references", score),),
                    symbol_ids=tuple(filter(None, (row["source_symbol_id"],
                                                    row["target_symbol_id"]))),
                    relation_ids=(row["reference_id"],),
                    resolution_status=row["resolution_status"],
                    evidence_roles=("import",) if row["reference_type"] in {
                        "import", "from_import", "import_alias",
                    } else ("callsite",) if row["reference_type"] == "call" else ("reference",),
                ))
        results.sort(key=lambda item: (-item.score, item.relative_path.casefold(),
                                       item.start_line, item.end_line, item.relation_ids))
        if call_query:
            # A call answer needs the caller and callee first; static import
            # edges follow as supporting route evidence.  Keep all ranges so
            # same-file imports cannot hide the actual call statement.
            return [*caller_evidence, *callsite_evidence, *definition_evidence, *results]
        return results

    def _vector(self, query: str, top_k: int) -> list[ProductCandidate]:
        if self.vector_retriever is None:
            return []
        results = []
        for item in self.vector_retriever.search(query, top_k=top_k):
            if not self._indexed_file_is_current(item.relative_path):
                continue
            try:
                excerpt = self._excerpt(item.relative_path, item.start_line, item.end_line)
            except ValueError:
                self._index_stale = True
                continue
            if excerpt != item.text:
                self._index_stale = True
                continue
            results.append(ProductCandidate(
                str(self.repository), item.relative_path, item.start_line, item.end_line,
                excerpt, item.content_hash, item.score,
                ("vector",), (("vector", item.score),),
                symbol_ids=tuple(filter(None, (item.symbol_id,))),
            ))
        return results

    @staticmethod
    def _merge(existing: ProductCandidate, incoming: ProductCandidate) -> ProductCandidate:
        channels = tuple(dict.fromkeys(existing.channels + incoming.channels))
        scores = dict(existing.channel_scores)
        scores.update(dict(incoming.channel_scores))
        statuses = {existing.resolution_status, incoming.resolution_status}
        status = next((item for item in ("ambiguous", "unresolved", "uncertain")
                       if item in statuses), existing.resolution_status)
        return replace(
            existing,
            channels=channels,
            channel_scores=tuple((name, scores[name]) for name in channels),
            symbol_ids=tuple(dict.fromkeys(existing.symbol_ids + incoming.symbol_ids)),
            relation_ids=tuple(dict.fromkeys(existing.relation_ids + incoming.relation_ids)),
            resolution_status=status,
            evidence_roles=tuple(dict.fromkeys(existing.evidence_roles + incoming.evidence_roles)),
        )

    def search(self, query: str, *, top_k: int = 5) -> ProductRetrievalOutcome:
        if isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= 100:
            raise ValueError("top_k must be an integer between 1 and 100.")
        self._ensure_index_matches()
        self._index_stale = False
        self._channel_failures = []
        self._file_text_cache.clear()
        self._relation_graph = None
        self._relation_failures = ()
        self._relation_attempted = False
        decision = self.router.route(query)
        required_roles = required_evidence_roles(query)
        effective_top_k = max(top_k, len(required_roles))
        channel_results = {}
        unavailable = []
        for channel in decision.channels:
            if channel == "lexical":
                lexical_limit = 100 if decision.route == "documentation" else max(top_k, 10)
                values = self._lexical(query, lexical_limit)
            elif channel == "symbols":
                values = self._symbols(query, max(top_k, 10))
            elif channel == "package_relations":
                values = self._package_relations(query, max(top_k, 10))
            elif channel == "vector":
                if self.vector_retriever is None:
                    unavailable.append("vector")
                    values = []
                else:
                    values = self._vector(query, max(top_k, 10))
            else:
                unavailable.append(channel)
                values = []
            if required_roles:
                values = [item for _position, item in sorted(
                    enumerate(values),
                    key=lambda pair: (0 if set(pair[1].evidence_roles) & set(required_roles) else 1,
                                      pair[0]),
                )]
            channel_results[channel] = values

        if (decision.route == "exact_symbol" and not channel_results.get("symbols")
                and not any(name == "symbols" for name, _reason in self._channel_failures)
                and self.store.status().get("symbol_index_available")):
            decision = replace(
                decision, confidence="low",
                reason=(decision.reason + " No exact symbol is present in the current index; "
                        "lexical matches remain unverified candidates."),
            )
        relation_target = self._relation_target(query)
        if (decision.route in {"package_api", "dependency_relation"}
                and relation_target and not channel_results.get("package_relations")
                and not self._channel_failures):
            decision = replace(
                decision, confidence="low",
                reason=(decision.reason + f" No static relationship for {relation_target!r} "
                        "was found; textual matches remain unverified candidates."),
            )

        ordered = []
        positions = {}
        for channel in decision.channels:
            values = channel_results.get(channel, [])
            if decision.route == "documentation" and channel == "lexical":
                values = sorted(values, key=lambda item: (
                    0 if item.relative_path.casefold().endswith(_DOCUMENT_SUFFIXES) else 1,
                    -item.score, item.relative_path.casefold(), item.start_line,
                ))
            for item in values:
                # Merge duplicate channel hits for the same source range.  A
                # different line range in the same file is complementary
                # evidence (for example an import and a real callsite).
                key = (item.relative_path.casefold(), item.start_line, item.end_line)
                if key in positions:
                    index = positions[key]
                    ordered[index] = self._merge(ordered[index], item)
                    continue
                positions[key] = len(ordered)
                ordered.append(item)
                if len(ordered) >= effective_top_k:
                    break
            if len(ordered) >= effective_top_k:
                break

        parse_failures = (
            tuple(self._relation_graph.parse_failures)
            if self._relation_graph is not None else self._relation_failures
        )
        unavailable.extend(item[0] for item in self._channel_failures)
        uncertain_statuses = {"ambiguous", "unresolved", "uncertain"}
        uncertainty = None
        if decision.confidence == "low":
            uncertainty = decision.reason
        elif parse_failures:
            uncertainty = "Some Python files could not be parsed; static relations may be incomplete."
        elif any(item.resolution_status in uncertain_statuses for item in ordered):
            uncertainty = "At least one selected static relationship is not uniquely resolved."
        if self._index_stale:
            uncertainty = "The repository changed since indexing; stale indexed evidence was excluded."
        if not ordered:
            uncertainty = uncertainty or "No scanner-approved evidence matched this query."
        return ProductRetrievalOutcome(
            query, PRODUCT_ROUTE_VERSION, decision, tuple(ordered),
            tuple(unavailable), parse_failures, uncertainty,
        )


__all__ = [
    "PRODUCT_ROUTE_VERSION", "ProductCandidate", "ProductRetrievalOutcome",
    "ProductIntentRouter", "ProductRetriever",
]


_ROUTE_STOPWORDS = {
    "what", "which", "where", "when", "why", "how", "does", "do", "is", "are",
    "the", "a", "an", "function", "method", "class", "module", "symbol", "return",
    "returns", "parameter", "parameters", "cite", "source", "evidence", "please",
}
_RELATION_WORDS = _CALL_WORDS | {
    "import", "imports", "imported", "export", "exports", "reexport", "reexports",
}


def _mentioned_code_symbol(query: str) -> str | None:
    """Recognize one code-shaped identifier embedded in multilingual prose."""
    if not isinstance(query, str):
        return None
    names = []
    for match in re.finditer(r"\b[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*\b", query):
        value = match.group(0)
        if value.casefold() in _ROUTE_STOPWORDS:
            continue
        if ("_" in value or any(char.isdigit() for char in value)
                or any(char.isupper() for char in value[1:])):
            names.append(value)
    unique = tuple(dict.fromkeys(names))
    return unique[0] if len(unique) == 1 else None


def _is_explicit_symbol_reference(query: object) -> bool:
    if not isinstance(query, str):
        return False
    relation_intent = bool(set(re.findall(r"[A-Za-z_]+", query.casefold())) & _RELATION_WORDS)
    return not relation_intent and (
        bool(_BARE_IDENTIFIER.fullmatch(query))
        or bool(_SYMBOL_SHAPE.search(query))
        or bool(_mentioned_code_symbol(query))
    )
