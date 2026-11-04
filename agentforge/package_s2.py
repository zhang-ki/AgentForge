"""AF-010-S2 隔离多仓库评测与规模研究。"""

from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import tempfile
from time import perf_counter
import tracemalloc

from .embeddings import LlamaServerEmbeddingProvider, VectorCandidateRetriever
from .evaluation import EVALUATION_TOP_K, _metrics
from .fusion import fuse_ranked_results
from .indexer import index_repository
from .package_relations import (
    IncrementalPackageRelationIndex, PackageRelationRetriever,
    build_package_relation_graph,
)
from .query_routing import ExplainableQueryRouter
from .retriever import search as lexical_search
from .store import IndexStore


DATASET_VERSION = "agentforge-package-relations-multirepo-v1"
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPOSITORIES = ROOT / "tests" / "fixtures" / "package_s2_repos"
DEFAULT_DATASET = ROOT / "tests" / "fixtures" / "package_s2_eval_v1.json"
DEFAULT_SPLIT = ROOT / "tests" / "fixtures" / "package_s2_split_v1.json"


def _read(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def validate_s2(dataset: dict, split: dict, repositories_root: Path = DEFAULT_REPOSITORIES) -> dict:
    if dataset.get("dataset_version") != DATASET_VERSION or split.get("dataset_version") != DATASET_VERSION:
        raise ValueError("Unexpected S2 dataset version.")
    queries = dataset.get("queries")
    if not isinstance(queries, list) or len(queries) < 120:
        raise ValueError("S2 requires at least 120 queries.")
    repositories = dataset.get("repositories")
    if not isinstance(repositories, list) or len(repositories) < 3:
        raise ValueError("S2 requires at least three repositories.")
    repo_paths = {}
    for item in repositories:
        repo_id, relative = item.get("id"), item.get("path")
        parts = PurePosixPath(relative).parts if isinstance(relative, str) else ()
        if (not isinstance(repo_id, str) or not repo_id or not parts
                or relative.startswith("/") or ":" in relative
                or any(part in ("", ".", "..") for part in parts)):
            raise ValueError("Repository paths must be safe relative paths.")
        target = (Path(repositories_root) / Path(*parts)).resolve(strict=True)
        root = Path(repositories_root).resolve(strict=True)
        if root not in target.parents or not target.is_dir():
            raise ValueError(f"{repo_id}: repository escapes the fixture root.")
        repo_paths[repo_id] = target
    if len(repo_paths) != len(repositories):
        raise ValueError("Repository IDs must be unique.")
    ids = set()
    group_split = {}
    counts = defaultdict(Counter)
    for item in queries:
        query_id = item.get("id")
        if not isinstance(query_id, str) or not query_id or query_id in ids:
            raise ValueError("S2 query IDs must be non-empty and unique.")
        ids.add(query_id)
        repo = repo_paths.get(item.get("repository_id"))
        if repo is None or not repo.is_dir():
            raise ValueError(f"{query_id}: repository is missing.")
        split_name = item.get("split")
        if split_name not in {"calibration", "validation"}:
            raise ValueError(f"{query_id}: invalid split.")
        group = item.get("leakage_group")
        if not isinstance(group, str) or not group.strip():
            raise ValueError(f"{query_id}: leakage_group is required.")
        previous = group_split.setdefault(group, split_name)
        if previous != split_name:
            raise ValueError("Leakage group crosses Calibration and Validation.")
        if (item.get("label_source") not in {"synthetic_fixture_design", "ai_evidence_review"}
                or item.get("review_method") != "ai_evidence_review"
                or item.get("human_reviewed") is not False):
            raise ValueError(f"{query_id}: label provenance is invalid.")
        counts[split_name]["query_count"] += 1
        counts[split_name]["no_evidence" if item.get("no_evidence") else "evidence"] += 1
        evidence = item.get("evidence")
        if not isinstance(evidence, list):
            raise ValueError(f"{query_id}: evidence must be a list.")
        if item.get("no_evidence"):
            if evidence or item.get("relevant_paths") or not item.get("scope_checked"):
                raise ValueError(f"{query_id}: malformed no-evidence label.")
        elif not evidence or not item.get("relevant_paths"):
            raise ValueError(f"{query_id}: positive label lacks evidence.")
        elif set(item["relevant_paths"]) != {entry.get("path") for entry in evidence}:
            raise ValueError(f"{query_id}: relevant paths and evidence paths disagree.")
        for entry in evidence:
            relative = entry.get("path")
            parts = PurePosixPath(relative).parts if isinstance(relative, str) else ()
            if (not parts or relative.startswith("/") or ":" in relative
                    or any(part in ("", ".", "..") for part in parts)):
                raise ValueError(f"{query_id}: unsafe evidence path.")
            target = (repo / Path(*parts)).resolve(strict=True)
            root = repo.resolve(strict=True)
            if root not in target.parents or not target.is_file():
                raise ValueError(f"{query_id}: evidence escapes its repository.")
            line_count = max(1, len(target.read_text(encoding="utf-8").splitlines()))
            start, end = entry.get("start_line"), entry.get("end_line")
            if (isinstance(start, bool) or not isinstance(start, int)
                    or isinstance(end, bool) or not isinstance(end, int)
                    or start < 1 or end < start or end > line_count):
                raise ValueError(f"{query_id}: evidence line range is invalid.")
    assigned = split.get("splits")
    if not isinstance(assigned, dict) or set(assigned) != {"calibration", "validation"}:
        raise ValueError("S2 split plan is incomplete.")
    flat = assigned["calibration"] + assigned["validation"]
    if len(flat) != len(set(flat)) or set(flat) != ids:
        raise ValueError("Every S2 query must be assigned exactly once.")
    by_id = {item["id"]: item for item in queries}
    for name, values in assigned.items():
        if any(by_id[value]["split"] != name for value in values):
            raise ValueError("Dataset and split plan disagree.")
    return {
        "query_count": len(queries), "repository_count": len(repo_paths),
        "leakage_groups_disjoint": True,
        "splits": {name: dict(counts[name]) for name in ("calibration", "validation")},
    }


def _as_result(value) -> dict:
    if isinstance(value, dict):
        return {
            "relative_path": value["relative_path"],
            "start_line": value.get("start_line", 1), "end_line": value.get("end_line", 1),
            "text": value.get("signature") or value.get("text") or value.get("qualified_name", ""),
            "score": value.get("score", 1.0),
        }
    return {
        "relative_path": value.relative_path, "start_line": value.start_line,
        "end_line": value.end_line, "text": value.text, "score": value.score,
    }


def _paths(values) -> list[str]:
    return [value["relative_path"] if isinstance(value, dict) else value.relative_path for value in values]


def _system_report(rows: list[dict], queries: list[dict]) -> dict:
    scored_rows = [row for row in rows if row["applicable"] and row["relevant"]]
    scored = [(row["ranked"], set(row["relevant"])) for row in scored_rows]
    categories = sorted({item["category"] for item in queries})
    repos = sorted({item["repository_id"] for item in queries})
    def subset(field, value):
        selected = [row for row in rows if row[field] == value
                    and row["applicable"] and row["relevant"]]
        return _metrics([(row["ranked"], set(row["relevant"])) for row in selected])
    negatives = [row for row in rows if row["applicable"] and row["no_evidence"]]
    return {
        **_metrics(scored),
        "scored_positive_query_count": len(scored_rows),
        "applicable_query_count": sum(row["applicable"] for row in rows),
        "not_applicable_query_count": sum(not row["applicable"] for row in rows),
        "no_evidence_query_count": len(negatives),
        "no_evidence_nonempty_count": sum(bool(row["ranked"]) for row in negatives),
        "no_evidence_nonempty_rate": round(
            sum(bool(row["ranked"]) for row in negatives) / len(negatives), 6
        ) if negatives else None,
        "by_category": {value: subset("category", value) for value in categories},
        "by_repository": {value: subset("repository_id", value) for value in repos},
        "query_results": rows,
    }


def evaluate_s2(*, base_url: str | None = None, model_id: str | None = None,
                api_model: str | None = None, dimension: int = 1024,
                embedding_provider=None,
                repositories_root: Path = DEFAULT_REPOSITORIES,
                dataset_path: Path = DEFAULT_DATASET, split_path: Path = DEFAULT_SPLIT) -> dict:
    dataset, split = _read(dataset_path), _read(split_path)
    validation = validate_s2(dataset, split, repositories_root)
    local_parameters_present = any(value is not None for value in (base_url, model_id, api_model))
    if (base_url is None) != (model_id is None) or (base_url is None) != (api_model is None):
        raise ValueError("Provide all local embedding parameters together or omit all.")
    if embedding_provider is not None and local_parameters_present:
        raise ValueError("Provide either a local endpoint or a test provider, not both.")
    provider = embedding_provider or (LlamaServerEmbeddingProvider(
        base_url, model_id=model_id, api_model=api_model, dimension=dimension,
        connect_timeout=2.0, read_timeout=60.0,
    ) if base_url is not None else None)
    router = ExplainableQueryRouter()
    details = {split_name: defaultdict(list) for split_name in ("calibration", "validation")}
    route_rows = {split_name: [] for split_name in ("calibration", "validation")}
    timing_totals = defaultdict(lambda: {"seconds": 0.0, "calls": 0})
    repository_info = {}
    embedding_index_seconds = 0.0
    started = perf_counter()

    for repository in dataset["repositories"]:
        repo_id = repository["id"]
        repo_root = repositories_root / repository["path"]
        graph = build_package_relation_graph(repo_root)
        package = PackageRelationRetriever(graph)
        store = IndexStore(":memory:")
        try:
            indexed = index_repository(repo_root, store)
            vector = VectorCandidateRetriever(store, provider) if provider else None
            if vector:
                vector_started = perf_counter()
                vector_index = vector.index_current_index(include_symbols=False)
                embedding_index_seconds += perf_counter() - vector_started
            else:
                vector_index = None
            repository_info[repo_id] = {
                "files": indexed["files"], "chunks": indexed["chunks"],
                "relations": len(graph.relations), "parse_failures": list(graph.parse_failures),
                "vector_index": vector_index,
            }
            for item in [value for value in dataset["queries"] if value["repository_id"] == repo_id]:
                split_name = item["split"]
                call_started = perf_counter()
                lexical = lexical_search(store, item["query"], EVALUATION_TOP_K)
                timing_totals[(split_name, "lexical")]["seconds"] += perf_counter() - call_started
                timing_totals[(split_name, "lexical")]["calls"] += 1
                call_started = perf_counter()
                symbols = store.search_symbols(**item["symbol_query"], limit=EVALUATION_TOP_K) \
                    if item.get("symbol_query") else []
                if item.get("symbol_query"):
                    timing_totals[(split_name, "symbols")]["seconds"] += perf_counter() - call_started
                    timing_totals[(split_name, "symbols")]["calls"] += 1
                call_started = perf_counter()
                package_results = package.search(item["query"], top_k=EVALUATION_TOP_K)
                timing_totals[(split_name, "package_relations")]["seconds"] += perf_counter() - call_started
                timing_totals[(split_name, "package_relations")]["calls"] += 1
                call_started = perf_counter()
                vector_results = vector.search(item["query"], top_k=EVALUATION_TOP_K) if vector else []
                if vector:
                    timing_totals[(split_name, "vector")]["seconds"] += perf_counter() - call_started
                    timing_totals[(split_name, "vector")]["calls"] += 1
                channel_values = {
                    "lexical": lexical, "symbols": symbols,
                    "package_relations": package_results, "vector": vector_results,
                }
                rrf_channels = {
                    name: [_as_result(value) for value in values]
                    for name, values in channel_values.items()
                    if name != "vector" or vector is not None
                }
                call_started = perf_counter()
                rrf = fuse_ranked_results(rrf_channels, k=60, top_k=EVALUATION_TOP_K)
                timing_totals[(split_name, "rrf")]["seconds"] += perf_counter() - call_started
                timing_totals[(split_name, "rrf")]["calls"] += 1
                decision = router.route(item["query"])
                routed_channels = {
                    name: [_as_result(value) for value in channel_values[name]]
                    for name in decision.channels if name in channel_values
                    and (name != "vector" or vector is not None)
                }
                call_started = perf_counter()
                routed = fuse_ranked_results(
                    routed_channels, k=60, top_k=EVALUATION_TOP_K
                ) if routed_channels else []
                timing_totals[(split_name, "routed")]["seconds"] += perf_counter() - call_started
                timing_totals[(split_name, "routed")]["calls"] += 1
                specialist_channels = [name for name in decision.channels if name != "lexical"]
                fallback = (
                    decision.route == "uncertain"
                    or (bool(specialist_channels) and not any(
                        channel_values.get(name, []) for name in specialist_channels
                    ) and bool(channel_values.get("lexical", [])))
                )
                route_rows[split_name].append({
                    "query_id": item["id"], "repository_id": repo_id,
                    "expected": item["expected_route"], "actual": decision.route,
                    "correct": decision.route == item["expected_route"],
                    "channels": list(decision.channels), "rule": decision.rule,
                    "reason": decision.reason, "confidence": decision.confidence,
                    "fallback_used": fallback,
                })
                relevant = item["relevant_paths"]
                for name, values, applicable in (
                    ("lexical", lexical, True),
                    ("symbols", symbols, item.get("symbol_query") is not None),
                    ("package_relations", package_results, item.get("package_applicable", False)),
                    ("vector", vector_results, vector is not None),
                    ("rrf", rrf, True), ("routed", routed, True),
                ):
                    details[split_name][name].append({
                        "query_id": item["id"], "repository_id": repo_id,
                        "category": item["category"], "no_evidence": item["no_evidence"],
                        "applicable": applicable, "relevant": relevant,
                        "ranked": _paths(values) if applicable else [],
                    })
        finally:
            store.close()

    reports = {}
    for split_name in ("calibration", "validation"):
        queries = [item for item in dataset["queries"] if item["split"] == split_name]
        systems = {
            name: _system_report(rows, queries)
            for name, rows in details[split_name].items()
            if name != "vector" or provider is not None
        }
        if provider is None:
            systems["vector"] = {"status": "not_measured", "reason": "No local provider supplied."}
        routes = route_rows[split_name]
        expected_names = sorted({row["expected"] for row in routes} | {row["actual"] for row in routes})
        matrix = {
            expected: {actual: sum(
                row["expected"] == expected and row["actual"] == actual for row in routes
            ) for actual in expected_names}
            for expected in expected_names
        }
        reports[split_name] = {
            "query_count": len(queries), "positive_count": sum(not q["no_evidence"] for q in queries),
            "no_evidence_count": sum(q["no_evidence"] for q in queries),
            "retrievers": systems,
            "retrieval_timings": {
                name: {
                    "calls": values["calls"],
                    "total_seconds": round(values["seconds"], 6),
                    "mean_seconds": round(values["seconds"] / values["calls"], 6)
                    if values["calls"] else None,
                }
                for (measured_split, name), values in sorted(timing_totals.items())
                if measured_split == split_name
            },
            "routing": {
                "rule_version": router.version, "rules_frozen_before_validation": True,
                "validation_used_for_tuning": False,
                "accuracy": round(sum(row["correct"] for row in routes) / len(routes), 6),
                "correct": sum(row["correct"] for row in routes), "total": len(routes),
                "fallback_count": sum(row["fallback_used"] for row in routes),
                "fallback_rate": round(sum(row["fallback_used"] for row in routes) / len(routes), 6),
                "confusion_matrix": matrix, "decisions": routes,
            },
        }
    # 只在冻结的 Validation 集上比较路径级互补性。
    validation_rows = details["validation"]
    maps = {name: {row["query_id"]: row for row in rows} for name, rows in validation_rows.items()}
    positives = [item for item in dataset["queries"] if item["split"] == "validation" and not item["no_evidence"]]
    package_unique = []
    rescues = Counter()
    package_errors = []
    for item in positives:
        relevant = set(item["relevant_paths"])
        package_ranked = maps["package_relations"][item["id"]]["ranked"]
        baselines = {
            name: maps[name][item["id"]]["ranked"]
            for name in ("lexical", "symbols", "vector")
            if name in maps and maps[name][item["id"]]["applicable"]
        }
        if set(package_ranked) & relevant and not any(set(values) & relevant for values in baselines.values()):
            package_unique.append(item["id"])
        if package_ranked and package_ranked[0] in relevant:
            for name, values in baselines.items():
                if not values or values[0] not in relevant:
                    rescues[name] += 1
        package_errors.extend(
            {"query_id": item["id"], "path": path}
            for path in package_ranked if path not in relevant
        )
    ranking_digest_payload = {
        split_name: {
            channel: [(row["query_id"], row["ranked"])
                      for row in rows]
            for channel, rows in sorted(channels.items())
        }
        for split_name, channels in sorted(details.items())
    }
    ranking_digest = hashlib.sha256(json.dumps(
        ranking_digest_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest().upper()
    return {
        "stage": "AF-010-S2", "dataset_validation": validation,
        "vector_measured": provider is not None,
        "request_counts": getattr(provider, "request_counts", None) if provider else None,
        "embedding_provider": ({
            "model_id": getattr(provider, "model_id", None),
            "dimension": getattr(provider, "dimension", None),
        } if provider else None),
        "embedding_index_seconds": round(embedding_index_seconds, 6) if provider else None,
        "elapsed_seconds": round(perf_counter() - started, 6),
        "ranking_digest_sha256": ranking_digest,
        "repositories": repository_info, "splits": reports,
        "validation_complementarity": {
            "package_unique_top10_hits": package_unique,
            "package_unique_top10_hit_count": len(package_unique),
            "package_top1_rescues": dict(rescues),
            "package_error_candidate_count": len(package_errors),
            "package_error_candidates": package_errors,
        },
        "fixed_configuration": {
            "rrf_k": 60, "router_version": router.version,
            "validation_evaluations": 1, "production_enabled": False,
        },
    }


def _write_scale_repository(root: Path, count: int) -> None:
    package = root / "scale_pkg"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(
        "from .m00000 import symbol_00000\n__all__ = [\"symbol_00000\"]\n", encoding="utf-8"
    )
    for index in range(count):
        next_index = (index + 1) % count
        (package / f"m{index:05d}.py").write_text(
            f"from .m{next_index:05d} import symbol_{next_index:05d}\n\n"
            f"def symbol_{index:05d}():\n    return {index}\n",
            encoding="utf-8",
        )


def run_scale_benchmarks(temp_parent: Path) -> dict:
    """测量 Python 内存分配；5000 也用于展示扫描器的文件数上限。"""
    results = []
    for count in (100, 1000, 5000):
        directory = Path(tempfile.mkdtemp(prefix=f"s2-scale-{count}-", dir=temp_parent))
        try:
            _write_scale_repository(directory, count)
            tracemalloc.start()
            started = perf_counter()
            try:
                graph = build_package_relation_graph(directory)
                status = "completed"
                relation_count = len(graph.relations)
                error_type = None
            except Exception as exc:
                status = "bounded_failure"
                relation_count = None
                error_type = type(exc).__name__
            elapsed = perf_counter() - started
            _current, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            incremental = None
            if status == "completed":
                lifecycle = IncrementalPackageRelationIndex()
                initial = lifecycle.refresh(directory)
                target = directory / "scale_pkg" / f"m{count - 1:05d}.py"
                target.write_text(
                    f"from .m00000 import symbol_00000\n\ndef symbol_{count - 1:05d}():\n"
                    f"    return {count}\n", encoding="utf-8",
                )
                modified = lifecycle.refresh(directory)
                def summarize(value: dict) -> dict:
                    summary = dict(value)
                    for field in ("added", "modified", "deleted"):
                        entries = summary.pop(field, None)
                        if entries is not None:
                            summary[field + "_count"] = len(entries)
                            summary[field + "_sample"] = entries[:3]
                    return summary
                incremental = {"initial": summarize(initial), "modified": summarize(modified)}
            results.append({
                "requested_modules": count, "physical_python_files": count + 1,
                "status": status, "error_type": error_type,
                "full_build_seconds": round(elapsed, 6), "relation_count": relation_count,
                "peak_python_allocation_bytes": peak,
                "incremental": incremental,
                "note": "Incremental prototype performs an atomic full rebuild for changed snapshots."
                if incremental else "The fixed scanner file cap prevented a graph build.",
            })
        finally:
            shutil.rmtree(directory)
    return {"measure": "tracemalloc Python allocation peak", "scenarios": results}
