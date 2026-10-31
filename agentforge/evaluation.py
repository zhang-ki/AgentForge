"""Deterministic, offline retrieval benchmark helpers."""

from collections import defaultdict
from time import perf_counter

from .retriever import search


RECALL_CUTOFFS = (1, 5, 10)
EVALUATION_TOP_K = max(RECALL_CUTOFFS)
DATASET_VERSION = "agentforge-retrieval-v1"
EXPANSION_DATASET_VERSION = "agentforge-retrieval-v1-expansion-candidates"
REVIEW_POOL_DATASET_VERSION = "agentforge-retrieval-v2-candidates"
PROVISIONAL_DATASET_VERSION = "agentforge-retrieval-provisional-v3"
GENERALIZATION_DATASET_VERSION = "agentforge-retrieval-generalization-v1"
PREPRODUCT_DATASET_VERSION = "agentforge-retrieval-preproduct-v1"
PACKAGE_RELATION_DATASET_VERSION = "agentforge-package-relations-v1"
PACKAGE_RELATION_MULTIREPO_DATASET_VERSION = "agentforge-package-relations-multirepo-v1"
SUPPORTED_DATASET_VERSIONS = {
    DATASET_VERSION, EXPANSION_DATASET_VERSION, REVIEW_POOL_DATASET_VERSION,
    PROVISIONAL_DATASET_VERSION, GENERALIZATION_DATASET_VERSION, PREPRODUCT_DATASET_VERSION,
    PACKAGE_RELATION_DATASET_VERSION,
    PACKAGE_RELATION_MULTIREPO_DATASET_VERSION,
}
CANDIDATE_DATASET_VERSIONS = {EXPANSION_DATASET_VERSION, REVIEW_POOL_DATASET_VERSION}
AI_PROVISIONAL_DATASET_VERSIONS = {
    PROVISIONAL_DATASET_VERSION, GENERALIZATION_DATASET_VERSION, PREPRODUCT_DATASET_VERSION,
    PACKAGE_RELATION_DATASET_VERSION,
    PACKAGE_RELATION_MULTIREPO_DATASET_VERSION,
}
EVIDENCE_DATASET_VERSIONS = CANDIDATE_DATASET_VERSIONS | AI_PROVISIONAL_DATASET_VERSIONS
REVIEW_EVIDENCE_DATASET_VERSIONS = {REVIEW_POOL_DATASET_VERSION} | AI_PROVISIONAL_DATASET_VERSIONS
ABSTENTION_THRESHOLDS = tuple(value / 20 for value in range(21))
_SYMBOL_FILTERS = {"name", "qualified_name", "kind", "relative_path"}


def _validate_dataset(dataset: dict) -> list[dict]:
    if not isinstance(dataset, dict) or dataset.get("dataset_version") not in SUPPORTED_DATASET_VERSIONS:
        raise ValueError(
            "Unsupported evaluation dataset; expected one of: "
            + ", ".join(sorted(SUPPORTED_DATASET_VERSIONS)) + "."
        )
    if not isinstance(dataset.get("label_rule"), str) or not dataset["label_rule"].strip():
        raise ValueError("Evaluation dataset must document its label rule.")
    queries = dataset.get("queries")
    if not isinstance(queries, list) or not queries:
        raise ValueError("Evaluation dataset must contain at least one query.")
    ids = set()
    for item in queries:
        if not isinstance(item, dict):
            raise ValueError("Each evaluation query must be an object.")
        query_id = item.get("id")
        category = item.get("category")
        query = item.get("query")
        if not all(isinstance(value, str) and value.strip() for value in (query_id, category, query)):
            raise ValueError("Each query needs a non-empty id, category and query text.")
        if query_id in ids:
            raise ValueError(f"Duplicate evaluation query id: {query_id}.")
        ids.add(query_id)
        for field in ("relevant_paths", "relevant_symbols"):
            if not isinstance(item.get(field), list):
                raise ValueError(f"{query_id}: {field} must be a list.")
        symbol_query = item.get("symbol_query")
        if symbol_query is not None:
            if (not isinstance(symbol_query, dict) or not symbol_query
                    or set(symbol_query) - _SYMBOL_FILTERS):
                raise ValueError(f"{query_id}: symbol_query has unsupported or empty filters.")
        elif item["relevant_symbols"]:
            raise ValueError(f"{query_id}: relevant_symbols require a symbol_query.")
        no_evidence = item.get("no_evidence", False)
        if not isinstance(no_evidence, bool):
            raise ValueError(f"{query_id}: no_evidence must be boolean.")
        if no_evidence and (item["relevant_paths"] or item["relevant_symbols"]):
            raise ValueError(f"{query_id}: no-evidence queries cannot have positive labels.")
        if not no_evidence and not item["relevant_paths"] and not item["relevant_symbols"]:
            raise ValueError(
                f"{query_id}: provide at least one relevance label (a manual relevance label "
                "in reviewed datasets) or set no_evidence=true."
            )
        if any(not isinstance(path, str) or not path.strip() for path in item["relevant_paths"]):
            raise ValueError(f"{query_id}: ground-truth paths must be non-empty strings.")
        if len(set(item["relevant_paths"])) != len(item["relevant_paths"]):
            raise ValueError(f"{query_id}: duplicate ground-truth path labels are not allowed.")
        symbol_keys = []
        for symbol in item["relevant_symbols"]:
            if (not isinstance(symbol, dict)
                    or not isinstance(symbol.get("path"), str)
                    or not isinstance(symbol.get("qualified_name"), str)
                    or not symbol["path"].strip() or not symbol["qualified_name"].strip()):
                raise ValueError(f"{query_id}: each symbol label needs path and qualified_name.")
            symbol_keys.append((symbol["path"], symbol["qualified_name"]))
        if len(set(symbol_keys)) != len(symbol_keys):
            raise ValueError(f"{query_id}: duplicate ground-truth symbol labels are not allowed.")
        if dataset["dataset_version"] in CANDIDATE_DATASET_VERSIONS:
            if item.get("review_status") != "pending_human_review":
                raise ValueError(f"{query_id}: expansion labels must remain pending_human_review.")
        if dataset["dataset_version"] in AI_PROVISIONAL_DATASET_VERSIONS:
            if (item.get("review_status") != "provisional_ai"
                    or item.get("review_method") != "ai_evidence_review"
                    or item.get("human_reviewed") is not False):
                raise ValueError(f"{query_id}: provisional labels must remain AI-only and provisional.")
        if dataset["dataset_version"] in EVIDENCE_DATASET_VERSIONS:
            evidence = item.get("evidence")
            if not isinstance(evidence, list):
                raise ValueError(f"{query_id}: expansion query evidence must be a list.")
            if not no_evidence and not evidence:
                raise ValueError(f"{query_id}: positive expansion labels need source evidence.")
            for entry in evidence:
                if (not isinstance(entry, dict)
                        or not isinstance(entry.get("path"), str)
                        or isinstance(entry.get("start_line"), bool)
                        or not isinstance(entry.get("start_line"), int)
                        or isinstance(entry.get("end_line"), bool)
                        or not isinstance(entry.get("end_line"), int)
                        or entry["start_line"] < 1 or entry["end_line"] < entry["start_line"]
                        or not isinstance(entry.get("reason"), str) or not entry["reason"].strip()):
                    raise ValueError(f"{query_id}: expansion evidence needs path, line range and reason.")
        if dataset["dataset_version"] in REVIEW_EVIDENCE_DATASET_VERSIONS:
            for field in ("leakage_group", "inclusion_reason", "exclusion_reason"):
                if not isinstance(item.get(field), str) or not item[field].strip():
                    raise ValueError(f"{query_id}: review-pool field {field} must not be empty.")
            if no_evidence:
                if item["evidence"]:
                    raise ValueError(f"{query_id}: no-evidence candidates cannot contain positive evidence.")
                if not isinstance(item.get("scope_checked"), str) or not item["scope_checked"].strip():
                    raise ValueError(f"{query_id}: no-evidence candidates need scope_checked.")
    return queries


def validate_split_plan(dataset: dict, plan: dict) -> dict:
    """Validate a proposed group-isolated split without treating pending labels as approved."""
    queries = _validate_dataset(dataset)
    if dataset["dataset_version"] != REVIEW_POOL_DATASET_VERSION:
        raise ValueError("Split plans require the v2 review candidate pool.")
    if not isinstance(plan, dict) or plan.get("dataset_version") != dataset["dataset_version"]:
        raise ValueError("Split plan dataset version does not match the candidate pool.")
    if plan.get("status") != "proposed_pending_human_review":
        raise ValueError("Split plan must remain proposed_pending_human_review.")
    splits = plan.get("splits")
    if not isinstance(splits, dict) or set(splits) != {"calibration", "validation"}:
        raise ValueError("Split plan needs calibration and validation lists.")
    query_by_id = {item["id"]: item for item in queries}
    assigned = []
    group_splits = {}
    summary = {}
    for split_name in ("calibration", "validation"):
        ids = splits[split_name]
        if not isinstance(ids, list) or any(not isinstance(value, str) for value in ids):
            raise ValueError(f"{split_name} split must be a list of query IDs.")
        if len(ids) != len(set(ids)):
            raise ValueError(f"{split_name} split contains duplicate query IDs.")
        unknown = set(ids) - set(query_by_id)
        if unknown:
            raise ValueError(f"{split_name} split contains unknown query IDs.")
        assigned.extend(ids)
        rows = [query_by_id[query_id] for query_id in ids]
        for row in rows:
            previous = group_splits.setdefault(row["leakage_group"], split_name)
            if previous != split_name:
                raise ValueError("Leakage group appears in both calibration and validation splits.")
        summary[split_name] = {
            "query_count": len(rows),
            "evidence_count": sum(not row.get("no_evidence", False) for row in rows),
            "no_evidence_count": sum(row.get("no_evidence", False) for row in rows),
            "near_domain_negative_count": sum(
                row.get("no_evidence", False) and row.get("difficulty") == "near_domain"
                for row in rows
            ),
            "leakage_group_count": len({row["leakage_group"] for row in rows}),
        }
    if len(assigned) != len(set(assigned)):
        raise ValueError("A query appears in both calibration and validation splits.")
    if set(assigned) != set(query_by_id):
        raise ValueError("Split plan must assign every candidate exactly once.")
    expected = plan.get("expected_distribution")
    if not isinstance(expected, dict):
        raise ValueError("Split plan must record its expected distribution.")
    for split_name, values in summary.items():
        recorded = expected.get(split_name)
        if not isinstance(recorded, dict) or recorded != {
            "query_count": values["query_count"],
            "evidence": values["evidence_count"],
            "no_evidence": values["no_evidence_count"],
            "near_domain_negative": values["near_domain_negative_count"],
        }:
            raise ValueError(f"{split_name} expected distribution does not match assignments.")
    return {
        "status": plan["status"],
        "dataset_version": dataset["dataset_version"],
        "groups_are_disjoint": True,
        "summary": summary,
    }


def _validate_labels(store, queries: list[dict]) -> None:
    paths = {row[0] for row in store.connection.execute("SELECT relative_path FROM files")}
    symbols = {
        (row[0], row[1])
        for row in store.connection.execute(
            "SELECT files.relative_path, symbols.qualified_name "
            "FROM symbols JOIN files ON files.id = symbols.file_id"
        )
    }
    for item in queries:
        for path in item["relevant_paths"]:
            if not isinstance(path, str) or path not in paths:
                raise ValueError(f"{item['id']}: ground-truth path is absent from the indexed fixture.")
        for symbol in item["relevant_symbols"]:
            if (symbol["path"], symbol["qualified_name"]) not in symbols:
                raise ValueError(f"{item['id']}: ground-truth symbol is absent from the indexed fixture.")
        for evidence in item.get("evidence", []):
            path = evidence["path"]
            if path not in paths:
                raise ValueError(f"{item['id']}: evidence path is absent from the indexed fixture.")
            row = store.connection.execute(
                "SELECT MIN(chunks.start_line), MAX(chunks.end_line) FROM chunks "
                "JOIN files ON files.id = chunks.file_id WHERE files.relative_path = ?",
                (path,),
            ).fetchone()
            if row[0] is None or evidence["start_line"] < row[0] or evidence["end_line"] > row[1]:
                raise ValueError(f"{item['id']}: evidence line range is outside the indexed fixture.")


def _path_key(result) -> str:
    if isinstance(result, dict):
        return result["relative_path"]
    return result.relative_path


def _symbol_key(result) -> str:
    if isinstance(result, dict):
        path, qualified_name = result["relative_path"], result["qualified_name"]
    else:
        path, qualified_name = result.relative_path, result.qualified_name
    return f"{path}::{qualified_name}"


def _result_score(result) -> float | None:
    value = result.get("score") if isinstance(result, dict) else getattr(result, "score", None)
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _signal_summary(values: list[float]) -> dict:
    if not values:
        return {"count": 0, "minimum": None, "maximum": None, "mean": None}
    return {
        "count": len(values),
        "minimum": round(min(values), 6),
        "maximum": round(max(values), 6),
        "mean": round(sum(values) / len(values), 6),
    }


def evaluate_abstention(query_results: list[dict], thresholds=ABSTENTION_THRESHOLDS) -> dict:
    """Describe score-only abstention candidates without selecting a production threshold."""
    rows = [row for row in query_results if row.get("top_score") is not None]
    positives = [row for row in rows if not row.get("no_evidence", False)]
    negatives = [row for row in rows if row.get("no_evidence", False)]
    threshold_rows = []
    for threshold in thresholds:
        if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
            raise ValueError("Abstention thresholds must be numeric.")
        threshold = float(threshold)
        if not 0.0 <= threshold <= 1.0:
            raise ValueError("Abstention thresholds must be from 0 to 1.")
        accepted_positive = sum(row["top_score"] >= threshold for row in positives)
        accepted_negative = sum(row["top_score"] >= threshold for row in negatives)
        accepted = accepted_positive + accepted_negative
        threshold_rows.append({
            "threshold": round(threshold, 6),
            "coverage": round(accepted / len(rows), 6) if rows else None,
            "precision": round(accepted_positive / accepted, 6) if accepted else None,
            "false_accept_rate": round(accepted_negative / len(negatives), 6) if negatives else None,
            "false_reject_rate": round((len(positives) - accepted_positive) / len(positives), 6)
            if positives else None,
            "accepted_positive": accepted_positive,
            "accepted_no_evidence": accepted_negative,
        })
    return {
        "status": "exploratory_only",
        "decision_rule": "accept when top cosine similarity is greater than or equal to threshold",
        "sample_counts": {
            "total_with_scores": len(rows), "evidence": len(positives), "no_evidence": len(negatives),
        },
        "top_score": {
            "evidence": _signal_summary([row["top_score"] for row in positives]),
            "no_evidence": _signal_summary([row["top_score"] for row in negatives]),
        },
        "top_2_margin": {
            "evidence": _signal_summary([
                row["top_2_margin"] for row in positives if row.get("top_2_margin") is not None
            ]),
            "no_evidence": _signal_summary([
                row["top_2_margin"] for row in negatives if row.get("top_2_margin") is not None
            ]),
        },
        "thresholds": threshold_rows,
        "calibration_warning": (
            "Too few evidence/no-evidence examples for a production threshold; do not select or deploy one."
            if len(positives) < 20 or len(negatives) < 20 else None
        ),
    }


def _metrics(rows: list[tuple[list[str], set[str]]]) -> dict:
    if not rows:
        return {
            "scored_query_count": 0,
            "valid_label_count": 0,
            "recall_at": {str(k): None for k in RECALL_CUTOFFS},
            "mrr": None,
            "aggregation": "macro average over queries with non-empty ground truth",
        }
    recall_sums = {k: 0.0 for k in RECALL_CUTOFFS}
    reciprocal_ranks = []
    label_count = 0
    for ranked, relevant in rows:
        label_count += len(relevant)
        unique_ranked = list(dict.fromkeys(ranked))
        for cutoff in RECALL_CUTOFFS:
            recall_sums[cutoff] += len(set(unique_ranked[:cutoff]) & relevant) / len(relevant)
        first = next((rank for rank, key in enumerate(unique_ranked, 1) if key in relevant), None)
        reciprocal_ranks.append(0.0 if first is None else 1.0 / first)
    count = len(rows)
    return {
        "scored_query_count": count,
        "valid_label_count": label_count,
        "recall_at": {str(k): round(recall_sums[k] / count, 6) for k in RECALL_CUTOFFS},
        "mrr": round(sum(reciprocal_ranks) / count, 6),
        "aggregation": "macro average over queries with non-empty ground truth",
    }


def evaluate_benchmark(store, dataset: dict, *, vector_search=None, rrf_search=None,
                       include_timings: bool = False) -> dict:
    """Evaluate lexical, exact-symbol, and optional vector candidates against labelled evidence."""
    if not isinstance(include_timings, bool):
        raise ValueError("include_timings must be boolean.")
    queries = _validate_dataset(dataset)
    _validate_labels(store, queries)
    all_categories = sorted({item["category"] for item in queries})
    systems = {"lexical": [], "symbols": []}
    if vector_search is not None:
        systems["vector"] = []
    if rrf_search is not None:
        systems["rrf"] = []
    details = {name: [] for name in systems}
    timing_seconds = {name: 0.0 for name in systems}
    timing_calls = {name: 0 for name in systems}
    no_evidence_checks = []

    for item in queries:
        call_started = perf_counter()
        lexical = search(store, item["query"], EVALUATION_TOP_K)
        timing_seconds["lexical"] += perf_counter() - call_started
        timing_calls["lexical"] += 1
        lexical_paths = [_path_key(result) for result in lexical]
        symbol_results = []
        symbol_keys = []
        symbol_applicable = item.get("symbol_query") is not None
        if symbol_applicable:
            call_started = perf_counter()
            symbol_results = store.search_symbols(
                **item["symbol_query"], limit=EVALUATION_TOP_K
            )
            timing_seconds["symbols"] += perf_counter() - call_started
            timing_calls["symbols"] += 1
            symbol_keys = [_symbol_key(result) for result in symbol_results]
        vector_results = []
        vector_paths = []
        if vector_search is not None:
            call_started = perf_counter()
            vector_results = vector_search(item["query"], top_k=EVALUATION_TOP_K)
            timing_seconds["vector"] += perf_counter() - call_started
            timing_calls["vector"] += 1
            vector_paths = [_path_key(result) for result in vector_results]
        rrf_results = []
        rrf_paths = []
        if rrf_search is not None:
            call_started = perf_counter()
            rrf_results = rrf_search(
                item["query"], top_k=EVALUATION_TOP_K,
                symbol_query=item.get("symbol_query"),
            )
            timing_seconds["rrf"] += perf_counter() - call_started
            timing_calls["rrf"] += 1
            rrf_paths = [_path_key(result) for result in rrf_results]

        lexical_gold = set(item["relevant_paths"])
        symbol_gold = {
            f"{symbol['path']}::{symbol['qualified_name']}"
            for symbol in item["relevant_symbols"]
        }
        lexical_row = (lexical_paths, lexical_gold)
        systems["lexical"].append((item, lexical_row))
        details["lexical"].append({
            "query_id": item["id"], "category": item["category"],
            "applicable": bool(lexical_gold), "relevant": sorted(lexical_gold),
            "ranked": lexical_paths,
        })
        if symbol_applicable:
            systems["symbols"].append((item, (symbol_keys, symbol_gold)))
            details["symbols"].append({
                "query_id": item["id"], "category": item["category"],
                "applicable": bool(symbol_gold), "relevant": sorted(symbol_gold),
                "ranked": symbol_keys,
            })
        if vector_search is not None:
            vector_scores = [_result_score(result) for result in vector_results]
            numeric_scores = [score for score in vector_scores if score is not None]
            systems["vector"].append((item, (vector_paths, lexical_gold)))
            details["vector"].append({
                "query_id": item["id"], "category": item["category"],
                "applicable": bool(lexical_gold), "relevant": sorted(lexical_gold),
                "ranked": vector_paths,
                "ranked_scores": [
                    {"relative_path": path, "score": None if score is None else round(score, 6)}
                    for path, score in zip(vector_paths, vector_scores)
                ],
                "top_score": round(numeric_scores[0], 6) if numeric_scores else None,
                "top_2_margin": round(numeric_scores[0] - numeric_scores[1], 6)
                if len(numeric_scores) > 1 else None,
                "no_evidence": item.get("no_evidence", False),
            })
        if rrf_search is not None:
            systems["rrf"].append((item, (rrf_paths, lexical_gold)))
            details["rrf"].append({
                "query_id": item["id"], "category": item["category"],
                "applicable": bool(lexical_gold), "relevant": sorted(lexical_gold),
                "ranked": rrf_paths,
                "ranked_scores": [
                    {"relative_path": _path_key(result), "score": round(_result_score(result), 6)}
                    for result in rrf_results
                ],
            })
        if item.get("no_evidence", False):
            no_evidence_checks.append({
                "query_id": item["id"],
                "lexical_returned_empty": not lexical_paths,
                "symbols_returned_empty": not symbol_keys,
                "vector_returned_empty": None if vector_search is None else not vector_paths,
                "rrf_returned_empty": None if rrf_search is None else not rrf_paths,
            })

    report_systems = {}
    for name, system_rows in systems.items():
        scored = [result for _, result in system_rows if result[1]]
        by_category = {}
        for category in all_categories:
            category_items = [item for item, _ in system_rows if item["category"] == category]
            category_scored = [
                result for item, result in system_rows
                if item["category"] == category and result[1]
            ]
            by_category[category] = {
                "query_count": sum(item["category"] == category for item in queries),
                "applicable_query_count": len(category_items),
                "grounded_query_count": len(category_scored),
                **_metrics(category_scored),
                "not_applicable_reason": (
                    "No non-empty ground-truth labels for this retriever/category."
                    if not category_scored else None
                ),
            }
        system_report = {
            "grounded_query_count": len(scored),
            "valid_label_count": sum(len(result[1]) for result in scored),
            **_metrics(scored),
            "by_category": by_category,
            "query_results": details[name],
        }
        if include_timings:
            system_report["timing"] = {
                "retrieval_seconds": round(timing_seconds[name], 6),
                "measured_call_count": timing_calls[name],
                "mean_seconds_per_call": round(
                    timing_seconds[name] / timing_calls[name], 6
                ) if timing_calls[name] else None,
                "note": (
                    "RRF wall time includes the channel searches it invokes."
                    if name == "rrf" else
                    "Only applicable exact-symbol queries are timed."
                    if name == "symbols" else None
                ),
            }
        report_systems[name] = system_report
    if vector_search is None:
        report_systems["vector"] = {
            "status": "not_measured",
            "reason": "No validated local embedding provider is configured; deterministic test providers are not model evidence.",
        }

    vector_abstention = None
    if vector_search is not None:
        vector_abstention = evaluate_abstention(details["vector"])

    return {
        "dataset_version": dataset["dataset_version"],
        "review_status": dataset.get("review_status", "human_reviewed_legacy_v1"),
        "label_rule": dataset["label_rule"],
        "query_total": len(queries),
        "category_counts": {
            category: sum(item["category"] == category for item in queries)
            for category in all_categories
        },
        "no_evidence_query_count": sum(item.get("no_evidence", False) for item in queries),
        "no_evidence_checks": no_evidence_checks,
        "retrievers": report_systems,
        "vector_abstention": vector_abstention,
        "metric_notes": {
            "recall_at": "Macro-average per query: relevant returned among the first K divided by the number of labelled relevant items.",
            "mrr": "Macro mean reciprocal rank of the first relevant result; an unreturned relevant item contributes zero.",
            "query_units": {
                "lexical": "repository-relative file path",
                "symbols": "repository-relative file path plus qualified symbol name",
                "vector": "repository-relative file path",
                "rrf": "repository-relative file path fused across available lexical, symbol and vector ranks",
            },
            "empty_ground_truth": "Excluded from recall/MRR; reported separately as no-evidence checks.",
            "candidate_scope": "Lexical and vector use the same indexed chunk corpus and file-path labels; exact symbols use symbol records and path-plus-qualified-name labels.",
            "symbol_scope": "Natural-language semantic queries without exact symbol filters are not applicable to the current exact symbol API.",
        },
    }
