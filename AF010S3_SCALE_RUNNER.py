"""Bounded, isolated AF-010-S3 incremental and package graph scale benchmark."""

from __future__ import annotations

import gc
import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import tracemalloc
import sys

from agentforge.package_relations import (
    IncrementalPackageRelationIndex,
    _build_package_relation_graph,
    _parse_module,
    _parse_snapshot,
    _snapshot_repository,
    _update_relation_graph_delta,
)
from agentforge.scanner import ScanLimits


ROOT = Path(__file__).resolve().parent
TEMP_ROOT = ROOT / "tests" / ".tmp"
OUTPUT = ROOT / "AF010S3_SCALE_RESULTS.json"
SCENARIOS = (100, 1000, 5000)
LIMITS = ScanLimits(
    max_file_bytes=1_000_000,
    max_total_bytes=10_000_000,
    max_files=6_000,
    max_entries=7_000,
)

PROTECTED_GLOBS = ("AF009*", "AF010PRE*", "AF010S1*", "AF010S2*")
PROTECTED_FIXTURES = (
    "tests/fixtures/retrieval_eval*",
    "tests/fixtures/package_relation*",
    "tests/fixtures/package_s2*",
)
PROTECTED_EXACT = (
    ".agentforge/data/index.sqlite3",
    ".agentforge/data/read_only_evidence.json",
)


class _WorkingSetSampler:
    """Sample this Python process's working set using Windows standard APIs."""

    def __init__(self):
        self.available = False
        self.error = None
        self.peak_bytes = None
        self._stop = threading.Event()
        self._thread = None
        self._reader = None
        if os.name != "nt":
            self.error = "Windows process memory API unavailable"
            return
        try:
            import ctypes

            class Counters(ctypes.Structure):
                _fields_ = [
                    ("cb", ctypes.c_uint32),
                    ("PageFaultCount", ctypes.c_uint32),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            psapi = ctypes.WinDLL("psapi", use_last_error=True)
            kernel.GetCurrentProcess.restype = ctypes.c_void_p
            psapi.GetProcessMemoryInfo.argtypes = [
                ctypes.c_void_p, ctypes.POINTER(Counters), ctypes.c_uint32
            ]
            psapi.GetProcessMemoryInfo.restype = ctypes.c_int
            handle = kernel.GetCurrentProcess()

            def read_working_set():
                counters = Counters()
                counters.cb = ctypes.sizeof(Counters)
                if not psapi.GetProcessMemoryInfo(
                    handle, ctypes.byref(counters), counters.cb
                ):
                    raise OSError(ctypes.get_last_error(), "GetProcessMemoryInfo failed")
                return int(counters.WorkingSetSize)

            self._reader = read_working_set
            self.available = True
        except Exception as exc:
            self.error = type(exc).__name__

    def _sample(self):
        while not self._stop.wait(0.01):
            try:
                value = self._reader()
                self.peak_bytes = value if self.peak_bytes is None else max(self.peak_bytes, value)
            except Exception as exc:
                self.error = type(exc).__name__
                self.available = False
                return

    def __enter__(self):
        if self.available:
            self._thread = threading.Thread(target=self._sample, daemon=True)
            self._thread.start()
        return self

    def __exit__(self, *_exc):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            try:
                value = self._reader()
                self.peak_bytes = value if self.peak_bytes is None else max(self.peak_bytes, value)
            except Exception as exc:
                self.error = type(exc).__name__
                self.available = False

    def as_dict(self):
        return {"available": self.available, "sampled_peak_bytes": self.peak_bytes,
                "error": self.error}


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def _protected_paths() -> list[Path]:
    selected = {ROOT / relative for relative in PROTECTED_EXACT}
    for pattern in PROTECTED_GLOBS:
        selected.update(path for path in ROOT.glob(pattern) if path.is_file())
    for pattern in PROTECTED_FIXTURES:
        base = Path(pattern.split("*")[0])
        selected.update(path for path in (ROOT / base.parent).glob(base.name + "*")
                        if path.is_file())
        for directory in (ROOT / base.parent).glob(base.name + "*"):
            if directory.is_dir() and not directory.is_symlink():
                selected.update(path for path in directory.rglob("*")
                                if path.is_file() and not path.is_symlink())
    missing = [str(path.relative_to(ROOT)) for path in selected if not path.is_file()]
    if missing:
        raise FileNotFoundError("Protected benchmark inputs are missing: " + ", ".join(missing))
    return sorted(selected, key=lambda path: str(path.relative_to(ROOT)).casefold())


def _snapshot_protected() -> dict:
    return {
        str(path.relative_to(ROOT)).replace("\\", "/"): {
            "bytes": path.stat().st_size, "sha256": _hash(path),
        }
        for path in _protected_paths()
    }


def _write_repository(root: Path, module_count: int) -> float:
    started = time.perf_counter()
    package = root / "scale_pkg"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text('"""Scale fixture package."""\n', encoding="utf-8")
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


def _relation_counts(graph) -> dict:
    result = {"total": len(graph.relations), "resolved": 0, "ambiguous": 0,
              "uncertain": 0, "unresolved": 0}
    for relation in graph.relations:
        result[relation.resolution_status] += 1
    return result


def _assert_same_graph(actual, expected) -> None:
    if actual != expected:
        raise AssertionError("Incremental relation graph differs from a full-build oracle")


def _full_graph_from_cached_snapshot(root: Path, lifecycle: IncrementalPackageRelationIndex):
    """Run the full relation derivation over the lifecycle's exact parsed snapshot."""
    modules = lifecycle._module_maps(lifecycle._parsed_cache)
    failures = list(lifecycle.graph.parse_failures)
    return _build_package_relation_graph(str(root.resolve()), modules, failures)


def _measure_case(module_count: int, temporary_root: Path) -> dict:
    with tempfile.TemporaryDirectory(prefix=f"af010s3-{module_count}-", dir=temporary_root) as name:
        root = Path(name) / "repo"
        input_prep_seconds = _write_repository(root, module_count)
        tracemalloc.start()
        with _WorkingSetSampler() as working_set:
            scan_started = time.perf_counter()
            report, manifest, candidates = _snapshot_repository(root, limits=LIMITS)
            scan_snapshot_seconds = time.perf_counter() - scan_started
            if not report.complete:
                raise ValueError("Test-specific scanner limits were not sufficient")

            parse_started = time.perf_counter()
            modules, failures, cache, parsed_count = _parse_snapshot(
                report, manifest, candidates
            )
            parse_seconds = time.perf_counter() - parse_started

            graph_started = time.perf_counter()
            initial_graph = _build_package_relation_graph(str(report.root), modules, failures)
            relation_graph_seconds = time.perf_counter() - graph_started

            # Verify the public incremental lifecycle starts from a real full
            # scan and then maintains that same graph through scoped deltas.
            lifecycle = IncrementalPackageRelationIndex(scan_limits=LIMITS)
            initial_started = time.perf_counter()
            initial_refresh = lifecycle.refresh(root)
            initial_refresh_seconds = time.perf_counter() - initial_started
            _assert_same_graph(lifecycle.graph, initial_graph)
            if not initial_refresh["full_rebuild"] or initial_refresh["status"] != "updated":
                raise AssertionError("Initial refresh did not report a full build")

            single_path = root / "scale_pkg" / f"m{module_count - 1:05d}.py"
            single_path.write_text(
                single_path.read_text(encoding="utf-8").replace(
                    "return process()", "return process()  # changed body"
                ), encoding="utf-8",
            )
            single_started = time.perf_counter()
            single_update = lifecycle.refresh(root)
            single_seconds = time.perf_counter() - single_started
            single_oracle_started = time.perf_counter()
            _assert_same_graph(lifecycle.graph, _full_graph_from_cached_snapshot(root, lifecycle))
            single_oracle_seconds = time.perf_counter() - single_oracle_started
            if single_update["full_rebuild"] or single_update["reparsed_file_count"] != 1:
                raise AssertionError("Single-file update failed to use scoped parse/relations")

            hub = root / "scale_pkg" / "m00000.py"
            hub.write_text("def process_v2():\n    return 'ready'\n", encoding="utf-8")
            fanin_started = time.perf_counter()
            fanin_update = lifecycle.refresh(root)
            fanin_seconds = time.perf_counter() - fanin_started
            fanin_oracle_started = time.perf_counter()
            _assert_same_graph(lifecycle.graph, _full_graph_from_cached_snapshot(root, lifecycle))
            fanin_oracle_seconds = time.perf_counter() - fanin_oracle_started
            if fanin_update["full_rebuild"] or fanin_update["affected_source_count"] < module_count:
                raise AssertionError("High fan-in update did not invalidate reverse dependents")

            package = root / "scale_pkg"
            rename_from = package / "m00001.py"
            rename_to = package / "renamed.py"
            rename_from.rename(rename_to)
            (package / "m00002.py").unlink()
            delete_rename_started = time.perf_counter()
            delete_rename_update = lifecycle.refresh(root)
            delete_rename_seconds = time.perf_counter() - delete_rename_started
            delete_oracle_started = time.perf_counter()
            final_oracle = _full_graph_from_cached_snapshot(root, lifecycle)
            _assert_same_graph(lifecycle.graph, final_oracle)
            delete_oracle_seconds = time.perf_counter() - delete_oracle_started
            if delete_rename_update["full_rebuild"]:
                raise AssertionError("Delete/rename update unexpectedly fell back")

            current, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            return {
                "coverage_layer": "full_scanner_hash_ast_and_relation_lifecycle",
                "requested_modules": module_count,
                "input_preparation_seconds": round(input_prep_seconds, 6),
                "physical_python_files_initial": len(candidates),
                "scan_snapshot_hash_seconds": round(scan_snapshot_seconds, 6),
                "scanner_entries_visited": report.entries_visited,
                "scanner_skipped_or_issue_count": len(report.issues),
                "ast_parse_seconds": round(parse_seconds, 6),
                "ast_files_parsed_initial": parsed_count,
                "parse_failures_initial": len(failures),
                "relation_graph_derivation_seconds": round(relation_graph_seconds, 6),
                "initial_refresh_seconds": round(initial_refresh_seconds, 6),
                "initial_relations": _relation_counts(initial_graph),
                "initial_resolution_counts": _relation_counts(initial_graph),
                "single_file_update": {
                    "elapsed_seconds": round(single_seconds, 6),
                    "oracle_relation_graph_derivation_seconds": round(single_oracle_seconds, 6),
                    "result": {key: single_update.get(key) for key in (
                        "status", "full_rebuild", "reparsed_file_count", "affected_source_count",
                        "added_relation_count", "removed_relation_count", "modified_relation_count",
                    )},
                    "oracle_equal": True,
                },
                "high_fan_in_update": {
                    "elapsed_seconds": round(fanin_seconds, 6),
                    "oracle_relation_graph_derivation_seconds": round(fanin_oracle_seconds, 6),
                    "result": {key: fanin_update.get(key) for key in (
                        "status", "full_rebuild", "reparsed_file_count", "affected_source_count",
                        "added_relation_count", "removed_relation_count", "modified_relation_count",
                    )},
                    "oracle_equal": True,
                },
                "delete_and_rename_update": {
                    "elapsed_seconds": round(delete_rename_seconds, 6),
                    "oracle_relation_graph_derivation_seconds": round(delete_oracle_seconds, 6),
                    "result": {key: delete_rename_update.get(key) for key in (
                        "status", "full_rebuild", "reparsed_file_count", "affected_source_count",
                        "added_relation_count", "removed_relation_count", "modified_relation_count",
                        "added", "deleted",
                    )},
                    "final_relations": _relation_counts(final_oracle),
                    "oracle_equal": True,
                },
                "memory": {
                    "tracemalloc_peak_python_bytes": peak,
                    "tracemalloc_current_python_bytes_at_end": current,
                    "working_set": working_set.as_dict(),
                    "note": "tracemalloc is Python allocation only; working set is sampled for this runner process.",
                },
                "status": "completed",
            }


def _measure_memory_graph_case(module_count: int) -> dict:
    """Exercise the graph and dependency-delta layer without 5,000-file I/O."""
    repository = "AF010S3-memory-scale"
    tracemalloc.start()
    with _WorkingSetSampler() as working_set:
        input_started = time.perf_counter()
        modules = {}
        source_by_path = {}
        for index in range(module_count):
            path = f"scale_pkg/m{index:05d}.py"
            if index == 0:
                source = "def process():\n    return 'ready'\n"
            else:
                source = (
                    "from .m00000 import process\n"
                    f"def entry_{index:05d}():\n    return process()\n"
                )
            module = _parse_module(path, source)
            modules[module.name] = module
            source_by_path[path] = source
        package = _parse_module("scale_pkg/__init__.py", '"""Scale package."""\n')
        modules[package.name] = package
        source_by_path[package.path] = '"""Scale package."""\n'
        preparation_seconds = time.perf_counter() - input_started

        full_started = time.perf_counter()
        graph = _build_package_relation_graph(repository, modules, [])
        full_graph_seconds = time.perf_counter() - full_started

        single_path = f"scale_pkg/m{module_count - 1:05d}.py"
        single_source = source_by_path[single_path].replace(
            "return process()", "return process()  # changed body"
        )
        single_parse_started = time.perf_counter()
        updated_module = _parse_module(single_path, single_source)
        single_parse_seconds = time.perf_counter() - single_parse_started
        single_modules = dict(modules)
        single_modules[updated_module.name] = updated_module
        single_started = time.perf_counter()
        single_graph, single_stats = _update_relation_graph_delta(
            repository, graph, modules, single_modules, [], {single_path}
        )
        single_seconds = time.perf_counter() - single_started
        single_oracle_started = time.perf_counter()
        single_oracle = _build_package_relation_graph(repository, single_modules, [])
        single_oracle_seconds = time.perf_counter() - single_oracle_started
        _assert_same_graph(single_graph, single_oracle)

        hub_path = "scale_pkg/m00000.py"
        hub_source = "def process_v2():\n    return 'ready'\n"
        hub_module = _parse_module(hub_path, hub_source)
        fanin_modules = dict(single_modules)
        fanin_modules[hub_module.name] = hub_module
        fanin_started = time.perf_counter()
        fanin_graph, fanin_stats = _update_relation_graph_delta(
            repository, single_graph, single_modules, fanin_modules, [], {hub_path}
        )
        fanin_seconds = time.perf_counter() - fanin_started
        fanin_oracle_started = time.perf_counter()
        fanin_oracle = _build_package_relation_graph(repository, fanin_modules, [])
        fanin_oracle_seconds = time.perf_counter() - fanin_oracle_started
        _assert_same_graph(fanin_graph, fanin_oracle)

        deleted_paths = {"scale_pkg/m00001.py", "scale_pkg/m00002.py"}
        renamed_path = "scale_pkg/renamed.py"
        renamed_source = source_by_path["scale_pkg/m00001.py"].replace(
            "entry_00001", "renamed_entry"
        )
        renamed_module = _parse_module(renamed_path, renamed_source)
        final_modules = {
            name: module for name, module in fanin_modules.items()
            if module.path not in deleted_paths
        }
        final_modules[renamed_module.name] = renamed_module
        delete_started = time.perf_counter()
        delete_graph, delete_stats = _update_relation_graph_delta(
            repository, fanin_graph, fanin_modules, final_modules, [],
            deleted_paths | {renamed_path},
        )
        delete_seconds = time.perf_counter() - delete_started
        delete_oracle_started = time.perf_counter()
        delete_oracle = _build_package_relation_graph(repository, final_modules, [])
        delete_oracle_seconds = time.perf_counter() - delete_oracle_started
        _assert_same_graph(delete_graph, delete_oracle)

        current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        return {
            "coverage_layer": "in_memory_parsed_module_graph_only_no_file_scan_or_content_hash",
            "requested_modules": module_count,
            "physical_python_files": module_count + 1,
            "input_preparation_and_ast_parse_seconds": round(preparation_seconds, 6),
            "initial_relation_graph_derivation_seconds": round(full_graph_seconds, 6),
            "parse_failures": 0,
            "scanner_skipped_or_issue_count": None,
            "initial_relations": _relation_counts(graph),
            "single_file_update": {
                "changed_ast_parse_seconds": round(single_parse_seconds, 6),
                "relation_delta_seconds": round(single_seconds, 6),
                "full_graph_oracle_seconds": round(single_oracle_seconds, 6),
                "stats": {key: single_stats.get(key) for key in (
                    "full_rebuild", "fallback_reason", "added_relation_count",
                    "removed_relation_count", "modified_relation_count",
                )},
                "affected_source_count": len(single_stats["affected_paths"]),
                "oracle_equal": True,
            },
            "high_fan_in_update": {
                "relation_delta_seconds": round(fanin_seconds, 6),
                "full_graph_oracle_seconds": round(fanin_oracle_seconds, 6),
                "stats": {key: fanin_stats.get(key) for key in (
                    "full_rebuild", "fallback_reason", "added_relation_count",
                    "removed_relation_count", "modified_relation_count",
                )},
                "affected_source_count": len(fanin_stats["affected_paths"]),
                "oracle_equal": True,
            },
            "delete_and_rename_update": {
                "relation_delta_seconds": round(delete_seconds, 6),
                "full_graph_oracle_seconds": round(delete_oracle_seconds, 6),
                "stats": {key: delete_stats.get(key) for key in (
                    "full_rebuild", "fallback_reason", "added_relation_count",
                    "removed_relation_count", "modified_relation_count",
                )},
                "affected_source_count": len(delete_stats["affected_paths"]),
                "final_relations": _relation_counts(delete_oracle),
                "oracle_equal": True,
            },
            "memory": {
                "tracemalloc_peak_python_bytes": peak,
                "tracemalloc_current_python_bytes_at_end": current,
                "working_set": working_set.as_dict(),
                "note": "Python graph-layer allocations only; this scenario excludes file scanning and hashing.",
            },
            "status": "completed",
        }


def _print_summary(result: dict) -> None:
    """Print a compact summary across scanner and graph-only scenarios."""
    scenarios = []
    for item in result.get("scenarios", []):
        single = item["single_file_update"]
        high_fan_in = item["high_fan_in_update"]
        delete_rename = item["delete_and_rename_update"]
        scenarios.append({
            "modules": item["requested_modules"],
            "coverage_layer": item["coverage_layer"],
            "input_prep_s": item.get(
                "input_preparation_seconds",
                item.get("input_preparation_and_ast_parse_seconds"),
            ),
            "scan_hash_s": item.get("scan_snapshot_hash_seconds"),
            "ast_parse_s": item.get("ast_parse_seconds"),
            "graph_s": item.get(
                "relation_graph_derivation_seconds",
                item.get("initial_relation_graph_derivation_seconds"),
            ),
            "single_update_s": single.get(
                "elapsed_seconds", single.get("relation_delta_seconds")
            ),
            "high_fan_in_s": high_fan_in.get(
                "elapsed_seconds", high_fan_in.get("relation_delta_seconds")
            ),
            "delete_rename_s": delete_rename.get(
                "elapsed_seconds", delete_rename.get("relation_delta_seconds")
            ),
            "relations": item["initial_relations"],
            "python_peak_bytes": item["memory"]["tracemalloc_peak_python_bytes"],
            "working_set": item["memory"]["working_set"],
        })
    print(json.dumps({
        "status": result.get("status"),
        "protected_unchanged": result.get("protected_unchanged"),
        "temporary_directories_cleaned": result.get("temporary_directories_cleaned"),
        "scenarios": scenarios,
        "errors": result.get("errors", []),
    }, ensure_ascii=False, indent=2))


def _validate_existing_result() -> int:
    if not OUTPUT.is_file():
        print("AF010S3_SCALE_RESULTS.json is missing.", file=sys.stderr)
        return 2
    result = json.loads(OUTPUT.read_text(encoding="utf-8"))
    valid = (
        result.get("status") == "completed"
        and result.get("protected_unchanged") is True
        and result.get("temporary_directories_cleaned") is True
        and not result.get("errors")
        and len(result.get("scenarios", [])) == len(SCENARIOS)
        and all(
            item.get("status") == "completed"
            and all(item.get(event, {}).get("oracle_equal") is True for event in (
                "single_file_update", "high_fan_in_update", "delete_and_rename_update"
            ))
            for item in result.get("scenarios", [])
        )
    )
    _print_summary(result)
    return 0 if valid else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--validate-existing", action="store_true",
        help="validate and summarize an existing AF010S3_SCALE_RESULTS.json without rerunning benchmarks",
    )
    args = parser.parse_args()
    if args.validate_existing:
        return _validate_existing_result()
    if OUTPUT.exists():
        print("AF010S3_SCALE_RESULTS.json exists; refusing to overwrite it.", file=sys.stderr)
        return 2
    TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    before = _snapshot_protected()
    results = []
    errors = []
    for count in SCENARIOS[:2]:
        try:
            results.append(_measure_case(count, TEMP_ROOT))
        except Exception as exc:
            errors.append({"requested_modules": count, "type": type(exc).__name__,
                           "message": str(exc)[:500]})
            break
        finally:
            gc.collect()
    if not errors:
        try:
            results.append(_measure_memory_graph_case(SCENARIOS[2]))
        except Exception as exc:
            errors.append({"requested_modules": SCENARIOS[2], "type": type(exc).__name__,
                           "message": str(exc)[:500]})
    after = _snapshot_protected()
    result = {
        "stage": "AF-010-S3-scale",
        "scanner_defaults_unchanged": True,
        "test_specific_limits": {
            "max_files": LIMITS.max_files, "max_entries": LIMITS.max_entries,
            "max_file_bytes": LIMITS.max_file_bytes,
            "max_total_bytes": LIMITS.max_total_bytes,
        },
        "benchmark_scope": "full scanner + content hashing + AST + graph lifecycle using an explicit test-only limit; production defaults unchanged",
        "protected_before": before,
        "protected_after": after,
        "protected_unchanged": before == after,
        "temporary_directories_cleaned": not any(TEMP_ROOT.glob("af010s3-*")),
        "scenarios": results,
        "errors": errors,
        "status": "completed" if len(results) == len(SCENARIOS) and not errors and before == after else "failed",
    }
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                      encoding="utf-8")
    _print_summary(result)
    return 0 if result["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
