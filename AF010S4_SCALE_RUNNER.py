"""Bounded AF-010-S4 scanner/hash/cache and full 5,000-module benchmark."""

from __future__ import annotations

import gc
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import tracemalloc
import sys
from unittest.mock import patch

import agentforge.package_relations as relations
from agentforge.scanner import ScanLimits


ROOT = Path(__file__).resolve().parent
TEMP_ROOT = ROOT / "tests" / ".tmp"
OUTPUT = ROOT / "AF010S4_SCALE_RESULTS.json"
SCENARIOS = (100, 1000, 5000)
LIMITS = ScanLimits(
    max_file_bytes=64_000,
    max_total_bytes=32_000_000,
    max_files=6_000,
    max_entries=7_000,
)
MAX_PYTHON_PEAK_BYTES = 512 * 1024 * 1024
PROTECTED_GLOBS = ("AF009*", "AF010PRE*", "AF010S1*", "AF010S2*", "AF010S3*")
PROTECTED_FIXTURE_PREFIXES = (
    "tests/fixtures/retrieval_eval",
    "tests/fixtures/package_relation",
    "tests/fixtures/package_s2",
)
PROTECTED_EXACT = (
    ".agentforge/data/index.sqlite3",
    ".agentforge/data/read_only_evidence.json",
)


class _WorkingSetSampler:
    """Sample this runner's Windows working set without spawning a helper."""

    def __init__(self):
        self.available = False
        self.error = None
        self.peak_bytes = None
        self._reader = None
        self._stop = threading.Event()
        self._thread = None
        if os.name != "nt":
            self.error = "Windows process memory API unavailable"
            return
        try:
            import ctypes

            class Counters(ctypes.Structure):
                _fields_ = [
                    ("cb", ctypes.c_uint32), ("PageFaultCount", ctypes.c_uint32),
                    ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            psapi = ctypes.WinDLL("psapi", use_last_error=True)
            kernel.GetCurrentProcess.restype = ctypes.c_void_p
            psapi.GetProcessMemoryInfo.argtypes = [
                ctypes.c_void_p, ctypes.POINTER(Counters), ctypes.c_uint32
            ]
            psapi.GetProcessMemoryInfo.restype = ctypes.c_int
            handle = kernel.GetCurrentProcess()

            def read():
                counters = Counters()
                counters.cb = ctypes.sizeof(Counters)
                if not psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
                    raise OSError(ctypes.get_last_error(), "GetProcessMemoryInfo failed")
                return int(counters.WorkingSetSize)

            self._reader = read
            self.available = True
        except Exception as exc:
            self.error = type(exc).__name__

    def __enter__(self):
        if self.available:
            self._thread = threading.Thread(target=self._sample, daemon=True)
            self._thread.start()
        return self

    def _sample(self):
        while not self._stop.wait(0.01):
            try:
                value = self._reader()
                self.peak_bytes = value if self.peak_bytes is None else max(self.peak_bytes, value)
            except Exception as exc:
                self.available = False
                self.error = type(exc).__name__
                return

    def __exit__(self, *_exc):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1)
            try:
                value = self._reader()
                self.peak_bytes = value if self.peak_bytes is None else max(self.peak_bytes, value)
            except Exception as exc:
                self.available = False
                self.error = type(exc).__name__

    def result(self):
        return {"available": self.available, "sampled_peak_bytes": self.peak_bytes,
                "error": self.error}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def _protected_paths() -> list[Path]:
    selected = {ROOT / relative for relative in PROTECTED_EXACT}
    for pattern in PROTECTED_GLOBS:
        selected.update(path for path in ROOT.glob(pattern) if path.is_file())
    for prefix in PROTECTED_FIXTURE_PREFIXES:
        base = ROOT / Path(prefix).parent
        pattern = Path(prefix).name + "*"
        for item in base.glob(pattern):
            if item.is_file() and not item.is_symlink():
                selected.add(item)
            elif item.is_dir() and not item.is_symlink():
                selected.update(path for path in item.rglob("*")
                                if path.is_file() and not path.is_symlink())
    missing = [str(path.relative_to(ROOT)) for path in selected if not path.is_file()]
    if missing:
        raise FileNotFoundError("Protected files missing: " + ", ".join(missing))
    return sorted(selected, key=lambda item: str(item.relative_to(ROOT)).casefold())


def _snapshot_protected() -> dict:
    return {
        path.relative_to(ROOT).as_posix(): {
            "bytes": path.stat().st_size, "sha256": _sha256(path),
        }
        for path in _protected_paths()
    }


def _write_repository(root: Path, module_count: int) -> float:
    started = time.perf_counter()
    package = root / "scale_pkg"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text('"""AF-010-S4 bounded scale fixture."""\n',
                                         encoding="utf-8")
    for index in range(module_count):
        path = package / f"m{index:05d}.py"
        if index == 0:
            source = "def process():\n    return 'ready'\n"
        else:
            source = (
                "from .m00000 import process\n"
                f"def entry_{index:05d}():\n    return process()\n"
            )
        path.write_text(source, encoding="utf-8")
    return time.perf_counter() - started


def _graph_from_cache(index):
    return relations._build_package_relation_graph(
        index.graph.repository,
        index._module_maps(index._parsed_cache),
        list(index.graph.parse_failures),
    )


def _counts(graph):
    result = {"total": len(graph.relations), "resolved": 0, "ambiguous": 0,
              "uncertain": 0, "unresolved": 0}
    for relation in graph.relations:
        result[relation.resolution_status] += 1
    return result


def _legacy_s3_full_build(root: Path) -> dict:
    """Measure S3's separate scan/hash pass followed by parse rereads."""
    observed_reads = 0
    original_read = relations.read_candidate

    def count_read(*args, **kwargs):
        nonlocal observed_reads
        observed_reads += 1
        return original_read(*args, **kwargs)

    with patch("agentforge.package_relations.read_candidate", side_effect=count_read):
        scan_started = time.perf_counter()
        report, manifest, candidates = relations._snapshot_repository(root, limits=LIMITS)
        scan_hash_seconds = time.perf_counter() - scan_started
        if not report.complete:
            raise ValueError("S3 baseline scan was incomplete.")
        parse_started = time.perf_counter()
        modules, failures, _cache, parsed_count = relations._parse_snapshot(
            report, manifest, candidates
        )
        parse_seconds = time.perf_counter() - parse_started
        graph_started = time.perf_counter()
        graph = relations._build_package_relation_graph(report.root, modules, failures)
        graph_seconds = time.perf_counter() - graph_started
    successful_hashes = sum(not digest.startswith("!read-error:") for digest in manifest.values())
    return {
        "scan_hash_seconds": round(scan_hash_seconds, 6),
        "parse_reread_seconds": round(parse_seconds, 6),
        "relation_graph_seconds": round(graph_seconds, 6),
        "total_seconds": round(scan_hash_seconds + parse_seconds + graph_seconds, 6),
        "files_discovered": len(report.candidates),
        "python_files": len(candidates),
        "content_read_count": observed_reads,
        "content_hash_count": successful_hashes + parsed_count,
        "ast_parse_count": parsed_count,
        "parse_failure_count": len(failures),
        "scan_issue_count": len(report.issues),
        "relations": _counts(graph),
        "graph": graph,
    }


def _event_result(label: str, index, root: Path, action=None) -> dict:
    if action is not None:
        action()
    started = time.perf_counter()
    outcome = index.refresh(root)
    total_seconds = time.perf_counter() - started
    oracle_started = time.perf_counter()
    oracle = _graph_from_cache(index)
    oracle_seconds = time.perf_counter() - oracle_started
    if outcome["status"] not in {"updated", "unchanged"}:
        raise RuntimeError(f"{label} refresh failed: {outcome}")
    if index.graph != oracle:
        raise AssertionError(f"{label} incremental graph differs from full cached-AST oracle")
    return {
        "label": label,
        "total_seconds": round(total_seconds, 6),
        "refresh": {key: outcome.get(key) for key in (
            "status", "discovery_seconds", "read_hash_seconds", "parse_seconds",
            "relation_update_seconds", "scan_candidate_count", "scanner_candidate_file_count",
            "python_file_count", "scan_entries_visited", "scan_issue_count",
            "content_read_count", "content_hash_count", "ast_cache_hit_count",
            "reparsed_file_count", "parse_failure_count", "cache_invalidated",
            "full_rebuild", "fallback_reason", "affected_source_count",
            "added_relation_count", "removed_relation_count", "modified_relation_count",
            "relation_count",
        )},
        "full_graph_oracle_seconds": round(oracle_seconds, 6),
        "relation_counts": _counts(index.graph),
        "oracle_equal": True,
    }


def _measure(module_count: int, temporary_root: Path, s3_result: dict) -> dict:
    with tempfile.TemporaryDirectory(prefix=f"af010s4-{module_count}-", dir=temporary_root) as name:
        root = Path(name) / "repo"
        input_prep_seconds = _write_repository(root, module_count)
        tracemalloc.start()
        with _WorkingSetSampler() as working_set:
            legacy = _legacy_s3_full_build(root)
            legacy_graph = legacy.pop("graph")

            index = relations.IncrementalPackageRelationIndex(scan_limits=LIMITS)
            initial_started = time.perf_counter()
            initial = index.refresh(root)
            initial_total = time.perf_counter() - initial_started
            if initial["status"] != "updated" or not initial["full_rebuild"]:
                raise RuntimeError(f"New full build did not complete: {initial}")
            if index.graph != legacy_graph:
                raise AssertionError("S3 and S4 cold full-build graphs differ")
            initial_relation_counts = _counts(index.graph)

            events = []
            events.append(_event_result("repeat_same_snapshot", index, root))
            leaf = root / "scale_pkg" / f"m{module_count - 1:05d}.py"

            def change_leaf():
                leaf.write_text(
                    leaf.read_text(encoding="utf-8").replace(
                        "return process()", "return process()  # low-fan-in-change"
                    ),
                    encoding="utf-8",
                )

            events.append(_event_result("single_low_fan_in_modify", index, root, change_leaf))
            hub = root / "scale_pkg" / "m00000.py"

            def change_hub():
                hub.write_text("def process_v2():\n    return 'ready'\n", encoding="utf-8")

            events.append(_event_result("single_high_fan_in_modify", index, root, change_hub))
            package = root / "scale_pkg"
            delete_path = package / "m00001.py"
            rename_from = package / "m00002.py"
            rename_to = package / "renamed.py"

            def delete_and_rename():
                delete_path.unlink()
                rename_from.rename(rename_to)

            events.append(_event_result("delete_and_rename", index, root, delete_and_rename))
            index._parsed_cache.clear()
            events.append(_event_result("missing_cache_full_recovery", index, root))

            full_started = time.perf_counter()
            disk_oracle = relations.build_package_relation_graph(root, scan_limits=LIMITS)
            disk_full_build_seconds = time.perf_counter() - full_started
            if index.graph != disk_oracle:
                raise AssertionError("Final incremental graph differs from full disk rebuild")

            current, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            if peak > MAX_PYTHON_PEAK_BYTES:
                raise MemoryError(
                    f"Python allocation peak exceeded the {MAX_PYTHON_PEAK_BYTES}-byte benchmark cap."
                )
            historical = None
            if s3_result and module_count in (100, 1000):
                old = next(item for item in s3_result["scenarios"]
                           if item["requested_modules"] == module_count)
                historical = {
                    "s3_initial_refresh_seconds": old.get("initial_refresh_seconds"),
                    "s3_scan_hash_seconds": old.get("scan_snapshot_hash_seconds"),
                    "s3_ast_parse_seconds": old.get("ast_parse_seconds"),
                    "s3_single_file_update_seconds": old["single_file_update"].get("elapsed_seconds"),
                    "s3_high_fan_in_update_seconds": old["high_fan_in_update"].get("elapsed_seconds"),
                    "s3_delete_rename_update_seconds": old["delete_and_rename_update"].get("elapsed_seconds"),
                    "note": "Single historical run; compare cautiously because it includes host load variance.",
                }
            return {
                "requested_modules": module_count,
                "coverage_layer": "complete_scanner_read_hash_ast_relation_and_update_lifecycle",
                "input_preparation_seconds": round(input_prep_seconds, 6),
                "scanner_limits": {
                    "max_files": LIMITS.max_files, "max_entries": LIMITS.max_entries,
                    "max_file_bytes": LIMITS.max_file_bytes,
                    "max_total_bytes": LIMITS.max_total_bytes,
                },
                "s3_original_cold_full_build": legacy,
                "s4_single_pass_cold_full_build": {
                    "total_seconds": round(initial_total, 6),
                    "result": {key: initial.get(key) for key in (
                        "status", "discovery_seconds", "read_hash_seconds", "parse_seconds",
                        "relation_update_seconds", "scan_candidate_count",
                        "scanner_candidate_file_count", "python_file_count",
                        "scan_entries_visited", "scan_issue_count", "content_read_count",
                        "content_hash_count", "ast_cache_hit_count", "reparsed_file_count",
                        "parse_failure_count", "full_rebuild", "fallback_reason", "relation_count",
                    )},
                    "relations": initial_relation_counts,
                    "same_graph_as_s3_baseline": True,
                },
                "incremental_events": events,
                "final_full_disk_rebuild_seconds": round(disk_full_build_seconds, 6),
                "final_disk_rebuild_equal": True,
                "final_relation_counts": _counts(disk_oracle),
                "memory": {
                    "tracemalloc_peak_python_bytes": peak,
                    "tracemalloc_current_python_bytes_at_end": current,
                    "working_set": working_set.result(),
                    "note": "tracemalloc is Python allocation only; working set is sampled for this process.",
                },
                "historical_s3_comparison": historical,
                "status": "completed",
            }


def main() -> int:
    if OUTPUT.exists():
        print("AF010S4_SCALE_RESULTS.json exists; refusing to overwrite it.", file=sys.stderr)
        return 2
    TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    before = _snapshot_protected()
    s3_path = ROOT / "AF010S3_SCALE_RESULTS.json"
    s3_result = json.loads(s3_path.read_text(encoding="utf-8")) if s3_path.is_file() else None
    scenarios = []
    errors = []
    for count in SCENARIOS:
        try:
            scenarios.append(_measure(count, TEMP_ROOT, s3_result))
        except Exception as exc:
            errors.append({"modules": count, "error_type": type(exc).__name__,
                           "message": str(exc)[:500]})
            break
        finally:
            gc.collect()
    after = _snapshot_protected()
    result = {
        "stage": "AF-010-S4",
        "production_default_max_files": ScanLimits().max_files,
        "python_allocation_peak_cap_bytes": MAX_PYTHON_PEAK_BYTES,
        "test_specific_limits_only": True,
        "metadata_shortcut_enabled": False,
        "correctness_policy": "content is read and hashed on every refresh; same-size/restored-mtime content changes remain detectable",
        "protected_before": before,
        "protected_after": after,
        "protected_unchanged": before == after,
        "temporary_directories_cleaned": not any(TEMP_ROOT.glob("af010s4-*")),
        "scenarios": scenarios,
        "errors": errors,
        "status": "completed" if len(scenarios) == len(SCENARIOS) and not errors and before == after else "failed",
    }
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                      encoding="utf-8")
    print(json.dumps({
        "status": result["status"],
        "protected_unchanged": result["protected_unchanged"],
        "temporary_directories_cleaned": result["temporary_directories_cleaned"],
        "scenarios": [{
            "modules": item["requested_modules"],
            "coverage_layer": item["coverage_layer"],
            "s3_cold_s": item["s3_original_cold_full_build"]["total_seconds"],
            "s4_cold_s": item["s4_single_pass_cold_full_build"]["total_seconds"],
            "python_files": item["s4_single_pass_cold_full_build"]["result"]["python_file_count"],
            "relations": item["final_relation_counts"]["total"],
            "events": [{"name": e["label"], "seconds": e["total_seconds"],
                        "reads": e["refresh"]["content_read_count"],
                        "hashes": e["refresh"]["content_hash_count"],
                        "parses": e["refresh"]["reparsed_file_count"],
                        "affected": e["refresh"]["affected_source_count"],
                        "oracle_equal": e["oracle_equal"]} for e in item["incremental_events"]],
            "tracemalloc_peak": item["memory"]["tracemalloc_peak_python_bytes"],
            "working_set": item["memory"]["working_set"],
        } for item in scenarios],
        "errors": errors,
    }, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
