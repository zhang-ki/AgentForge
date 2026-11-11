"""Coordinate safe scans and incremental index updates."""

from collections import Counter, OrderedDict
from dataclasses import replace
import hashlib
import os
from pathlib import Path
import threading

from .chunker import chunk_signature, chunk_text
from .config import (
    DEFAULT_OVERLAP_LINES,
    DEFAULT_WINDOW_LINES,
    MAX_TOTAL_CHUNKS,
    MAX_TOTAL_REFERENCES,
    MAX_TOTAL_SYMBOLS,
)
from .errors import ScanError
from .observability import lifecycle_stage
from .parsers import parser_for_path, resolve_references
from .scanner import ScanLimits, ScanReadError, read_candidate, scan_repository
from .store import IndexStore


_INDEX_CACHE_LIMIT = 4
_INDEX_CACHE: OrderedDict[tuple, dict] = OrderedDict()
_INDEX_CACHE_LOCK = threading.RLock()
_PARSER_VERSION = "python-ast-v1"


def _cache_key(store: IndexStore, root: Path, signature: str) -> tuple:
    database = getattr(store, "database", None)
    if isinstance(database, str) and database == ":memory:":
        database_identity = ("memory", id(store))
    else:
        try:
            database_identity = str(Path(database).resolve())
        except (TypeError, OSError, RuntimeError, ValueError):
            database_identity = str(database)
    return database_identity, os.path.normcase(str(root)), signature, _PARSER_VERSION


def _candidate_fingerprints(candidates) -> dict[str, tuple]:
    return {
        item.relative_path: (
            item.size_bytes, item.mtime_ns, item.ctime_ns, item.device, item.inode,
        )
        for item in candidates
    }


def _database_snapshot(store: IndexStore) -> tuple:
    return tuple(
        (item["relative_path"], item["content_hash"], item["size_bytes"],
         item["mtime_ns"], item.get("chunk_signature"))
        for item in store.file_snapshot()
    )


def _cache_get(key):
    with _INDEX_CACHE_LOCK:
        value = _INDEX_CACHE.get(key)
        if value is not None:
            _INDEX_CACHE.move_to_end(key)
        return value


def _cache_set(key, value):
    with _INDEX_CACHE_LOCK:
        _INDEX_CACHE[key] = value
        _INDEX_CACHE.move_to_end(key)
        while len(_INDEX_CACHE) > _INDEX_CACHE_LIMIT:
            _INDEX_CACHE.popitem(last=False)


def _metadata_allows_cache(metadata, root, signature):
    try:
        indexed_root = str(Path(metadata.get("repository", "")).resolve(strict=True))
    except (OSError, RuntimeError, ValueError):
        return False
    return (
        os.path.normcase(indexed_root) == os.path.normcase(str(root))
        and metadata.get("chunk_signature") == signature
        and metadata.get("parser_version") == _PARSER_VERSION
    )


def index_repository(
    root,
    store: IndexStore,
    *,
    window_lines: int = DEFAULT_WINDOW_LINES,
    overlap_lines: int = DEFAULT_OVERLAP_LINES,
    limits: ScanLimits | None = None,
    replace_repository: bool = False,
    force_rebuild: bool = False,
    scan_report=None,
) -> dict:
    signature = chunk_signature(window_lines, overlap_lines)
    limits = limits or ScanLimits()
    report = scan_report or scan_repository(root, limits)
    try:
        requested_root = str(Path(root).resolve(strict=True))
        scanned_root = str(report.root.resolve(strict=True))
    except (AttributeError, OSError, RuntimeError, ValueError):
        raise ScanError("Precomputed scan report cannot be verified for this repository.") from None
    if requested_root.casefold() != scanned_root.casefold():
        raise ScanError("Precomputed scan report belongs to a different repository.")
    store.check_repository(report.root, replace_repository)
    if not report.complete:
        reasons = ", ".join(sorted({issue.reason for issue in report.issues}))
        raise ScanError(f"Scan was incomplete ({reasons}); existing index was not changed.")

    fingerprints = _candidate_fingerprints(report.candidates)
    key = _cache_key(store, report.root, signature)
    cache = None if (force_rebuild or replace_repository) else _cache_get(key)
    metadata = store.metadata()
    indexed_snapshot = store.file_snapshot()
    indexed_by_path = {item["relative_path"]: item for item in indexed_snapshot}
    cache_valid = bool(
        cache is not None
        and _metadata_allows_cache(metadata, report.root, signature)
        and cache.get("parser_version") == _PARSER_VERSION
    )

    # A full, source-verified snapshot is reused only inside this process and
    # only while the scanner's file identity metadata and SQLite snapshot agree.
    # Source/hash validation still reads and hashes every candidate actually used.
    if (cache_valid and cache.get("fingerprints") == fingerprints
            and cache.get("database_snapshot") == _database_snapshot(store)):
        status = store.status()
        return {
            **status, "added": 0, "changed": 0,
            "unchanged": len(report.candidates), "removed": 0,
            "index_unchanged": True, "files_read": 0,
            "files_reused": len(report.candidates),
            "chunks_reused": status.get("chunks", 0),
            "skipped": len(report.issues),
            "skip_reasons": dict(sorted(Counter(issue.reason for issue in report.issues).items())),
        }

    parsed_cache = cache.get("parsed_files", {}) if cache_valid else {}
    cached_fingerprints = cache.get("fingerprints", {}) if cache_valid else {}
    chunk_counts = store.file_chunk_counts()
    documents = []
    parsed_files = []
    parsed_by_path = {}
    structured_symbols = structured_references = 0
    structured_limit_reached = False
    chunk_count = files_read = files_reused = chunks_reused = 0

    with lifecycle_stage("repository_file_content_processing") as processing_stage:
        for candidate in report.candidates:
            relative = candidate.relative_path
            old = indexed_by_path.get(relative)
            reusable = bool(
                cache_valid and old is not None and not force_rebuild
                and cached_fingerprints.get(relative) == fingerprints[relative]
                and old["size_bytes"] == candidate.size_bytes
                and old["mtime_ns"] == candidate.mtime_ns
                and old.get("chunk_signature", signature) == signature
                and relative in parsed_cache
            )
            if reusable:
                parsed = parsed_cache[relative]
                parsed_by_path[relative] = parsed
                if parsed is not None:
                    parsed_files.append(parsed)
                    if parsed.status == "parsed" and not structured_limit_reached:
                        structured_symbols += len(parsed.symbols)
                        structured_references += len(parsed.references)
                        if (structured_symbols > MAX_TOTAL_SYMBOLS
                                or structured_references > MAX_TOTAL_REFERENCES):
                            structured_limit_reached = True
                chunk_count += chunk_counts.get(relative, 0)
                chunks_reused += chunk_counts.get(relative, 0)
                if chunk_count > MAX_TOTAL_CHUNKS:
                    raise ScanError("Chunk limit exceeded; existing index was not changed.")
                documents.append({
                    "relative_path": relative,
                    "content_hash": old["content_hash"],
                    "size_bytes": candidate.size_bytes,
                    "mtime_ns": candidate.mtime_ns,
                    "chunks": (),
                    "parsed_file": parsed,
                })
                files_reused += 1
                continue

            try:
                text = read_candidate(candidate, report.root, limits.max_file_bytes)
            except ScanReadError as exc:
                report.add_issue(relative, exc.reason)
                continue
            encoded = text.encode("utf-8")
            files_read += 1
            chunks = chunk_text(relative, text, window_lines, overlap_lines)
            chunk_count += len(chunks)
            if chunk_count > MAX_TOTAL_CHUNKS:
                raise ScanError("Chunk limit exceeded; existing index was not changed.")
            parser = parser_for_path(relative)
            parsed = parser.parse(relative, text) if parser else None
            if parsed is not None:
                if structured_limit_reached:
                    parsed = replace(
                        parsed, status="failed",
                        error="ResourceLimitExceeded: repository structured-index limit reached",
                        symbols=(), references=(), scope_shadowed_names=(),
                    )
                elif parsed.status == "parsed":
                    next_symbols = structured_symbols + len(parsed.symbols)
                    next_references = structured_references + len(parsed.references)
                    if next_symbols > MAX_TOTAL_SYMBOLS or next_references > MAX_TOTAL_REFERENCES:
                        parsed = replace(
                            parsed, status="failed",
                            error="ResourceLimitExceeded: repository structured-index limit reached",
                            symbols=(), references=(), scope_shadowed_names=(),
                        )
                        structured_limit_reached = True
                    else:
                        structured_symbols = next_symbols
                        structured_references = next_references
                parsed_files.append(parsed)
            parsed_by_path[relative] = parsed
            documents.append({
                "relative_path": relative,
                "content_hash": hashlib.sha256(encoded).hexdigest(),
                # Persist the scanner's on-disk size. Decoding UTF-8-SIG strips
                # the BOM, so len(text.encode("utf-8")) can be smaller than
                # the actual file and make every later freshness check stale.
                "size_bytes": candidate.size_bytes,
                "mtime_ns": candidate.mtime_ns,
                "chunks": chunks,
                "parsed_file": parsed,
            })
        if processing_stage is not None:
            processing_stage.finish("COMPLETED", metadata={
                "files_read": files_read,
                "files_reused": files_reused,
                "chunk_count": chunk_count,
            })

    with lifecycle_stage("repository_reference_resolution") as reference_stage:
        resolved = {
            parsed.relative_path: parsed for parsed in resolve_references(parsed_files)
        }
        if reference_stage is not None:
            reference_stage.finish("COMPLETED", metadata={"parsed_file_count": len(parsed_files)})
    for document in documents:
        parsed = document["parsed_file"]
        if parsed is not None:
            document["parsed_file"] = resolved[parsed.relative_path]

    with lifecycle_stage("repository_sqlite_sync") as sqlite_stage:
        changes = store.sync(
            report.root, documents, signature, replace_repository,
            force_rebuild=force_rebuild,
        )
        if sqlite_stage is not None:
            sqlite_stage.finish("COMPLETED", metadata={
                "added_count": changes.get("added", 0),
                "changed_count": changes.get("changed", 0),
                "removed_count": changes.get("removed", 0),
            })

    parse_failures = [item for item in resolved.values() if item.status == "failed"]
    result = {
        **store.status(), **changes,
        "index_unchanged": not any(changes.get(key, 0) for key in ("added", "changed", "removed")),
        "files_read": files_read, "files_reused": files_reused,
        "chunks_reused": chunks_reused,
        "parsed_python_files": len(resolved),
        "parse_failures": len(parse_failures),
        "parse_failure_paths": [
            {"path": item.relative_path, "error": item.error}
            for item in sorted(parse_failures, key=lambda value: value.relative_path)
        ],
        "skipped": len(report.issues),
        "skip_reasons": dict(sorted(Counter(issue.reason for issue in report.issues).items())),
    }
    _cache_set(key, {
        "parser_version": _PARSER_VERSION,
        "fingerprints": fingerprints,
        "parsed_files": parsed_by_path,
        "database_snapshot": _database_snapshot(store),
    })
    return result
