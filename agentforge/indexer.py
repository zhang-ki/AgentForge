"""Coordinate read-only scanning and atomic index updates."""

from collections import Counter
from dataclasses import replace
import hashlib

from .chunker import chunk_signature, chunk_text
from .config import (
    DEFAULT_OVERLAP_LINES,
    DEFAULT_WINDOW_LINES,
    MAX_TOTAL_CHUNKS,
    MAX_TOTAL_REFERENCES,
    MAX_TOTAL_SYMBOLS,
)
from .errors import ScanError
from .parsers import parser_for_path, resolve_references
from .scanner import ScanLimits, ScanReadError, read_candidate, scan_repository
from .store import IndexStore


def index_repository(
    root,
    store: IndexStore,
    *,
    window_lines: int = DEFAULT_WINDOW_LINES,
    overlap_lines: int = DEFAULT_OVERLAP_LINES,
    limits: ScanLimits | None = None,
    replace_repository: bool = False,
    force_rebuild: bool = False,
) -> dict:
    signature = chunk_signature(window_lines, overlap_lines)
    limits = limits or ScanLimits()
    report = scan_repository(root, limits)
    store.check_repository(report.root, replace_repository)
    if not report.complete:
        reasons = ", ".join(sorted({issue.reason for issue in report.issues}))
        raise ScanError(f"Scan was incomplete ({reasons}); existing index was not changed.")
    documents = []
    parsed_files = []
    structured_symbols = structured_references = 0
    structured_limit_reached = False
    chunk_count = actual_bytes = 0
    for candidate in report.candidates:
        try:
            text = read_candidate(candidate, report.root, limits.max_file_bytes)
        except ScanReadError as exc:
            report.add_issue(candidate.relative_path, exc.reason)
            continue
        encoded = text.encode("utf-8")
        actual_bytes += len(encoded)
        if actual_bytes > limits.max_total_bytes:
            raise ScanError("Files grew beyond the total byte budget; existing index was not changed.")
        chunks = chunk_text(candidate.relative_path, text, window_lines, overlap_lines)
        chunk_count += len(chunks)
        if chunk_count > MAX_TOTAL_CHUNKS:
            raise ScanError("Chunk limit exceeded; existing index was not changed.")
        parser = parser_for_path(candidate.relative_path)
        parsed = parser.parse(candidate.relative_path, text) if parser else None
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
        documents.append({
            "relative_path": candidate.relative_path,
            "content_hash": hashlib.sha256(encoded).hexdigest(),
            "size_bytes": len(encoded),
            "mtime_ns": candidate.mtime_ns,
            "chunks": chunks,
            "parsed_file": parsed,
        })
    resolved = {
        parsed.relative_path: parsed for parsed in resolve_references(parsed_files)
    }
    for document in documents:
        parsed = document["parsed_file"]
        if parsed is not None:
            document["parsed_file"] = resolved[parsed.relative_path]
    changes = store.sync(report.root, documents, signature, replace_repository,
                         force_rebuild=force_rebuild)
    parse_failures = [item for item in resolved.values() if item.status == "failed"]
    return {
        **store.status(),
        **changes,
        "parsed_python_files": len(resolved),
        "parse_failures": len(parse_failures),
        "parse_failure_paths": [
            {"path": item.relative_path, "error": item.error}
            for item in sorted(parse_failures, key=lambda value: value.relative_path)
        ],
        "skipped": len(report.issues),
        "skip_reasons": dict(sorted(Counter(issue.reason for issue in report.issues).items())),
    }

