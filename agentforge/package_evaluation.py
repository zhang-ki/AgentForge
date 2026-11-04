"""针对可选 PackageRelation 研究通道的离线评测。"""

from collections import defaultdict
from pathlib import Path, PurePosixPath
from time import perf_counter

from .evaluation import EVALUATION_TOP_K, _metrics, evaluate_benchmark
from .fusion import fuse_ranked_results
from .package_relations import PackageRelationRetriever
from .retriever import search as lexical_search


PACKAGE_FILTERS = {
    "relation_type", "source_module", "target_module", "symbol_name", "resolution_status",
}


def validate_package_dataset(dataset: dict, split: dict, repository) -> dict:
    """验证冻结的合成标签，不参考检索结果。"""
    if dataset.get("dataset_version") != "agentforge-package-relations-v1":
        raise ValueError("Unexpected package-relation dataset version.")
    queries = dataset.get("queries")
    if not isinstance(queries, list) or len(queries) < 40:
        raise ValueError("Package-relation research requires at least 40 queries.")
    ids = [item.get("id") for item in queries]
    if any(not isinstance(value, str) or not value for value in ids) or len(ids) != len(set(ids)):
        raise ValueError("Package-relation query IDs must be non-empty and unique.")
    root = Path(repository).resolve(strict=True)
    groups = {}
    for item in queries:
        if (item.get("review_method") != "ai_evidence_review"
                or item.get("human_reviewed") is not False
                or item.get("label_source") not in {"synthetic_fixture_design", "ai_evidence_review"}):
            raise ValueError(f"{item['id']}: label provenance is invalid.")
        if item.get("split") not in {"calibration", "validation"}:
            raise ValueError(f"{item['id']}: invalid split.")
        previous = groups.setdefault(item.get("leakage_group"), item["split"])
        if previous != item["split"]:
            raise ValueError("A leakage group crosses splits.")
        for evidence in item.get("evidence", []):
            relative = evidence.get("path")
            parts = PurePosixPath(relative).parts if isinstance(relative, str) else ()
            if (not parts or relative.startswith("/") or ":" in relative
                    or any(part in ("", ".", "..") for part in parts)):
                raise ValueError(f"{item['id']}: unsafe evidence path.")
            target = root.joinpath(*parts).resolve(strict=True)
            if root not in target.parents or not target.is_file():
                raise ValueError(f"{item['id']}: evidence escapes the fixture.")
            line_count = len(target.read_text(encoding="utf-8").splitlines())
            start, end = evidence.get("start_line"), evidence.get("end_line")
            if (isinstance(start, bool) or not isinstance(start, int)
                    or isinstance(end, bool) or not isinstance(end, int)
                    or start < 1 or end < start or end > max(1, line_count)):
                raise ValueError(f"{item['id']}: evidence line range is invalid.")
        if item.get("no_evidence"):
            if item.get("relevant_paths") or item.get("evidence"):
                raise ValueError(f"{item['id']}: no-evidence label has positive evidence.")
            if not item.get("scope_checked"):
                raise ValueError(f"{item['id']}: no-evidence scope is missing.")
    if split.get("dataset_version") != dataset["dataset_version"]:
        raise ValueError("Split dataset version mismatch.")
    assigned = split.get("splits", {})
    if set(assigned) != {"calibration", "validation"}:
        raise ValueError("Split must contain calibration and validation.")
    flat = assigned["calibration"] + assigned["validation"]
    if len(flat) != len(set(flat)) or set(flat) != set(ids):
        raise ValueError("Each query must be assigned exactly once.")
    by_id = {item["id"]: item for item in queries}
    for name, values in assigned.items():
        if any(by_id[value]["split"] != name for value in values):
            raise ValueError("Dataset and split plan disagree.")
    return {
        "query_count": len(queries), "leakage_groups_disjoint": True,
        "splits": {
            name: {
                "query_count": len(values),
                "evidence": sum(not by_id[value]["no_evidence"] for value in values),
                "no_evidence": sum(by_id[value]["no_evidence"] for value in values),
            } for name, values in assigned.items()
        },
    }


def _result_dict(item):
    return {
        "relative_path": item.relative_path, "start_line": item.start_line,
        "end_line": item.end_line, "text": item.text, "score": item.score,
    }


def evaluate_package_relations(store, dataset: dict, graph) -> dict:
    """在同一个 fixture 上比较现有检索通道与隔离的 PackageRelation 图。"""
    baseline = evaluate_benchmark(store, dataset, include_timings=True)
    retriever = PackageRelationRetriever(graph)
    package_rows = []
    rrf_rows = []
    package_details = []
    rrf_details = []
    category_package = defaultdict(list)
    category_rrf = defaultdict(list)
    package_seconds = 0.0
    rrf_seconds = 0.0
    package_calls = 0
    no_evidence = []
    unique_hits = []
    top1_rescues = []
    error_candidates = []

    for item in dataset["queries"]:
        package_query = item.get("package_query")
        package_results = []
        if package_query is not None:
            if not isinstance(package_query, dict) or set(package_query) - PACKAGE_FILTERS:
                raise ValueError(f"{item['id']}: invalid package_query filters.")
            started = perf_counter()
            package_results = retriever.search(
                item["query"], top_k=EVALUATION_TOP_K, **package_query
            )
            package_seconds += perf_counter() - started
            package_calls += 1
        package_paths = [value.relative_path for value in package_results]
        relevant = set(item["relevant_paths"])
        if package_query is not None:
            row = (package_paths, relevant)
            if relevant:
                package_rows.append(row)
                category_package[item["category"]].append(row)
            package_details.append({
                "query_id": item["id"], "category": item["category"],
                "applicable": True, "relevant": sorted(relevant), "ranked": package_paths,
                "relations": [value.relation_type for value in package_results],
            })

        lexical = lexical_search(store, item["query"], EVALUATION_TOP_K)
        symbols = store.search_symbols(**item["symbol_query"], limit=EVALUATION_TOP_K) \
            if item.get("symbol_query") else []
        started = perf_counter()
        fused = fuse_ranked_results({
            "lexical": [_result_dict(value) for value in lexical],
            "package_relations": [_result_dict(value) for value in package_results],
            "symbols": [dict(value) for value in symbols],
        }, k=60, top_k=EVALUATION_TOP_K)
        rrf_seconds += perf_counter() - started
        fused_paths = [value.relative_path for value in fused]
        if relevant:
            row = (fused_paths, relevant)
            rrf_rows.append(row)
            category_rrf[item["category"]].append(row)
        rrf_details.append({
            "query_id": item["id"], "category": item["category"],
            "relevant": sorted(relevant), "ranked": fused_paths,
            "channel_contributions": {
                value.relative_path: [entry.channel for entry in value.channels] for value in fused
            },
        })
        if relevant:
            lexical_paths = [value.relative_path for value in lexical]
            symbol_paths = [value["relative_path"] for value in symbols]
            if set(package_paths) & relevant and not (set(lexical_paths) | set(symbol_paths)) & relevant:
                unique_hits.append(item["id"])
            if package_paths and package_paths[0] in relevant and not (
                (lexical_paths and lexical_paths[0] in relevant)
                or (symbol_paths and symbol_paths[0] in relevant)
            ):
                top1_rescues.append(item["id"])
            error_candidates.extend(
                {"query_id": item["id"], "path": path}
                for path in package_paths if path not in relevant
            )
        if item.get("no_evidence", False):
            no_evidence.append({
                "query_id": item["id"],
                "package_returned_empty": not package_paths,
                "rrf_returned_empty": not fused_paths,
            })

    categories = sorted({item["category"] for item in dataset["queries"]})
    baseline["retrievers"]["package_relations"] = {
        "applicable_query_count": len(package_details),
        "not_applicable_query_count": len(dataset["queries"]) - len(package_details),
        **_metrics(package_rows),
        "by_category": {category: _metrics(category_package[category]) for category in categories},
        "query_results": package_details,
        "timing": {"retrieval_seconds": round(package_seconds, 6), "measured_call_count": package_calls},
    }
    baseline["retrievers"]["rrf_lexical_symbol_package"] = {
        "status": "offline_exploration_only",
        "channels": ["lexical", "symbols", "package_relations"],
        "vector_included": False,
        **_metrics(rrf_rows),
        "by_category": {category: _metrics(category_rrf[category]) for category in categories},
        "query_results": rrf_details,
        "timing": {"fusion_seconds": round(rrf_seconds, 6)},
    }
    baseline["package_relation_analysis"] = {
        "unique_relevant_hits_over_lexical_and_symbol": unique_hits,
        "unique_relevant_hit_count": len(unique_hits),
        "top1_rescues_over_lexical_and_symbol": top1_rescues,
        "top1_rescue_count": len(top1_rescues),
        "error_candidate_count": len(error_candidates),
        "error_candidates": error_candidates,
        "no_evidence": no_evidence,
        "no_evidence_nonempty_rate": round(
            sum(not row["package_returned_empty"] for row in no_evidence) / len(no_evidence), 6
        ) if no_evidence else None,
        "rrf_no_evidence_nonempty_rate": round(
            sum(not row["rrf_returned_empty"] for row in no_evidence) / len(no_evidence), 6
        ) if no_evidence else None,
        "parse_failures": [list(value) for value in graph.parse_failures],
        "relation_status_counts": {
            status: sum(value.resolution_status == status for value in graph.relations)
            for status in ("resolved", "ambiguous", "unresolved", "uncertain")
        },
    }
    return baseline
