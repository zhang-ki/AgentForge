"""用于 AF-009G 独立语料的验证和评测辅助函数。"""

from collections import Counter
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
from time import perf_counter

from .chunker import split_source_lines
from .evaluation import GENERALIZATION_DATASET_VERSION, _validate_dataset


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CORPUS = PROJECT_ROOT / "tests" / "fixtures" / "retrieval_eval_generalization_repo"
DEFAULT_DATASET = PROJECT_ROOT / "tests" / "fixtures" / "retrieval_eval_generalization_v1.json"
DEFAULT_SPLIT_PLAN = PROJECT_ROOT / "tests" / "fixtures" / "retrieval_eval_generalization_split_v1.json"
RRF_K = 60
HISTORICAL_TOP_SCORE_THRESHOLD = 0.70


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read evaluation JSON {path.name} ({type(exc).__name__}).") from None
    if not isinstance(value, dict):
        raise ValueError(f"Evaluation JSON {path.name} must contain an object.")
    return value


def load_generalization_dataset(
        path: Path = DEFAULT_DATASET, *,
        expected_version: str = GENERALIZATION_DATASET_VERSION) -> dict:
    dataset = _read_json(Path(path))
    if dataset.get("dataset_version") != expected_version:
        raise ValueError("Unsupported generalization evaluation dataset version.")
    if (dataset.get("status") != "provisional_ai"
            or dataset.get("human_reviewed") is not False
            or dataset.get("review_method") != "ai_evidence_review"):
        raise ValueError("Generalization labels must remain AI-provisional and not human reviewed.")
    queries = _validate_dataset(dataset)
    if any(item.get("split") not in {"calibration", "validation"} for item in queries):
        raise ValueError("Every generalization query must have one assigned split.")
    if any(item.get("review_status") != "provisional_ai" for item in queries):
        raise ValueError("Every generalization query must retain provisional_ai status.")
    return dataset


def corpus_manifest(root: Path = DEFAULT_CORPUS) -> dict:
    """生成确定性的清单，不跟随链接或重解析点。"""
    root = Path(root).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("Generalization corpus must be a directory.")
    entries = []
    for current, directories, filenames in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        kept_directories = []
        for name in sorted(directories, key=str.casefold):
            child = current_path / name
            attributes = getattr(child.stat(follow_symlinks=False), "st_file_attributes", 0)
            if (child.is_symlink() or getattr(child, "is_junction", lambda: False)()
                    or attributes & 0x400):
                raise ValueError("Generalization corpus contains a link or reparse point; refusing to follow it.")
            resolved = child.resolve(strict=True)
            if not resolved.is_relative_to(root):
                raise ValueError("Generalization corpus contains a directory outside its root.")
            # Runtime bytecode is neither frozen source nor evaluation evidence.
            if name.casefold() == "__pycache__":
                continue
            kept_directories.append(name)
        directories[:] = kept_directories
        for name in sorted(filenames, key=str.casefold):
            path = current_path / name
            attributes = getattr(path.stat(follow_symlinks=False), "st_file_attributes", 0)
            if path.is_symlink() or attributes & 0x400:
                raise ValueError("Generalization corpus contains a linked file; refusing to read it.")
            resolved = path.resolve(strict=True)
            if not resolved.is_relative_to(root) or not resolved.is_file():
                raise ValueError("Generalization corpus contains a file outside its root.")
            if path.suffix.casefold() in {".pyc", ".pyo"}:
                continue
            data = path.read_bytes()
            entries.append({
                "path": path.relative_to(root).as_posix(),
                "size_bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            })
    entries.sort(key=lambda item: item["path"].casefold())
    encoded = json.dumps(entries, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {
        "root_name": root.name,
        "file_count": len(entries),
        "files": entries,
        "manifest_sha256": hashlib.sha256(encoded).hexdigest(),
    }


def validate_evidence(dataset: dict, root: Path = DEFAULT_CORPUS) -> dict:
    root = Path(root).resolve(strict=True)
    queries = _validate_dataset(dataset)
    verified_ranges = 0
    for item in queries:
        relevant_paths = set(item["relevant_paths"])
        for evidence in item["evidence"]:
            relative = evidence["path"].replace("\\", "/")
            pure = PurePosixPath(relative)
            if (pure.is_absolute() or ":" in relative
                    or any(part in ("", ".", "..") for part in pure.parts)):
                raise ValueError(f"{item['id']}: evidence path is not normalized and relative.")
            target = root.joinpath(*pure.parts)
            attributes = getattr(target.stat(follow_symlinks=False), "st_file_attributes", 0) if target.exists() else 0
            if (target.is_symlink() or getattr(target, "is_junction", lambda: False)()
                    or attributes & 0x400 or not target.is_file()):
                raise ValueError(f"{item['id']}: evidence path is missing or linked: {relative}.")
            resolved = target.resolve(strict=True)
            if not resolved.is_relative_to(root):
                raise ValueError(f"{item['id']}: evidence path escapes the corpus root.")
            if relative not in relevant_paths:
                raise ValueError(f"{item['id']}: evidence path is not in its relevant path set.")
            lines = split_source_lines(target.read_text(encoding="utf-8"))
            if evidence["end_line"] > len(lines):
                raise ValueError(f"{item['id']}: evidence range exceeds {relative} line count.")
            if not any(line.strip() for line in lines[evidence["start_line"] - 1:evidence["end_line"]]):
                raise ValueError(f"{item['id']}: evidence range contains no source text.")
            verified_ranges += 1
        if item["no_evidence"] and item["evidence"]:
            raise ValueError(f"{item['id']}: no-evidence candidate contains positive evidence.")
        if not item["no_evidence"] and not item["evidence"]:
            raise ValueError(f"{item['id']}: positive candidate has no line-addressable evidence.")
    return {"verified_evidence_ranges": verified_ranges, "query_count": len(queries)}


def validate_generalization_split(
        dataset: dict, plan: dict, *,
        expected_version: str = GENERALIZATION_DATASET_VERSION) -> dict:
    queries = _validate_dataset(dataset)
    if dataset.get("dataset_version") != expected_version:
        raise ValueError("Generalization split plan requires the AF-009G dataset version.")
    if plan.get("dataset_version") != expected_version:
        raise ValueError("Split plan dataset version does not match the corpus labels.")
    if plan.get("status") != "fixed_provisional_ai_group_isolated":
        raise ValueError("Split plan must be fixed and remain AI-provisional.")
    by_id = {item["id"]: item for item in queries}
    splits = plan.get("splits")
    if not isinstance(splits, dict) or set(splits) != {"calibration", "validation"}:
        raise ValueError("Split plan needs calibration and validation ID lists.")
    all_assigned = []
    group_split = {}
    summary = {}
    for split_name in ("calibration", "validation"):
        ids = splits[split_name]
        if not isinstance(ids, list) or len(ids) != len(set(ids)):
            raise ValueError(f"{split_name} IDs must be a unique list.")
        if any(query_id not in by_id for query_id in ids):
            raise ValueError(f"{split_name} contains an unknown query ID.")
        all_assigned.extend(ids)
        rows = [by_id[query_id] for query_id in ids]
        for row in rows:
            if row["split"] != split_name:
                raise ValueError(f"{row['id']}: query split disagrees with the fixed plan.")
            previous = group_split.setdefault(row["leakage_group"], split_name)
            if previous != split_name:
                raise ValueError("A leakage group crosses Calibration and Validation.")
        summary[split_name] = {
            "query_count": len(rows),
            "evidence": sum(not row["no_evidence"] for row in rows),
            "no_evidence": sum(row["no_evidence"] for row in rows),
            "near_domain_negative": sum(
                row["no_evidence"] and row.get("difficulty") == "near_domain" for row in rows
            ),
            "out_of_domain_negative": sum(
                row["no_evidence"] and row.get("difficulty") == "out_of_domain" for row in rows
            ),
            "leakage_group_count": len({row["leakage_group"] for row in rows}),
        }
    if len(all_assigned) != len(set(all_assigned)) or set(all_assigned) != set(by_id):
        raise ValueError("Every candidate must appear exactly once in the two splits.")
    plan_groups = plan.get("group_assignments")
    if not isinstance(plan_groups, list):
        raise ValueError("Split plan must record complete leakage-group assignments.")
    observed = {}
    for entry in plan_groups:
        if not isinstance(entry, dict) or entry.get("split") not in summary:
            raise ValueError("Malformed leakage-group assignment.")
        group = entry.get("leakage_group")
        ids = entry.get("query_ids")
        if not isinstance(group, str) or group in observed or not isinstance(ids, list):
            raise ValueError("Duplicate or malformed leakage-group assignment.")
        observed[group] = (entry["split"], ids)
    expected_groups = {}
    for row in queries:
        expected_groups.setdefault(row["leakage_group"], (row["split"], []))[1].append(row["id"])
    if set(observed) != set(expected_groups):
        raise ValueError("Split plan does not assign every leakage group exactly once.")
    for group, (split_name, ids) in expected_groups.items():
        plan_split, plan_ids = observed[group]
        if plan_split != split_name or set(plan_ids) != set(ids):
            raise ValueError(f"Split plan group contents do not match dataset group {group}.")
    if plan.get("expected_distribution") != {
        name: {key: value for key, value in counts.items() if key != "leakage_group_count"}
        for name, counts in summary.items()
    }:
        raise ValueError("Split plan distribution does not match its assignments.")
    return {"groups_are_disjoint": True, "summary": summary}


def _split_dataset(dataset: dict, split_name: str) -> dict:
    if split_name not in {"calibration", "validation"}:
        raise ValueError("split_name must be calibration or validation.")
    return {
        **dataset,
        "queries": [item for item in dataset["queries"] if item["split"] == split_name],
    }


def _no_evidence_summary(report: dict, dataset: dict) -> dict:
    negatives = [item for item in dataset["queries"] if item["no_evidence"]]
    systems = report["retrievers"]
    result = {}
    for name in ("lexical", "symbols", "vector", "rrf"):
        system = systems.get(name, {})
        if system.get("status") == "not_measured":
            result[name] = {
                "status": "not_measured",
                "applicable_no_evidence_queries": None,
                "non_empty_returns": None,
                "non_empty_return_rate": None,
                "reason": system.get("reason"),
            }
            continue
        details = {row["query_id"]: row for row in system.get("query_results", [])}
        candidates = [
            item for item in negatives
            if name != "symbols" or item.get("symbol_query") is not None
        ]
        returned = sum(bool(details.get(item["id"], {}).get("ranked")) for item in candidates)
        near = [item for item in candidates if item.get("difficulty") == "near_domain"]
        out = [item for item in candidates if item.get("difficulty") == "out_of_domain"]
        result[name] = {
            "applicable_no_evidence_queries": len(candidates),
            "non_empty_returns": returned,
            "non_empty_return_rate": round(returned / len(candidates), 6) if candidates else None,
            "near_domain": {
                "count": len(near),
                "non_empty_returns": sum(bool(details.get(item["id"], {}).get("ranked")) for item in near),
            },
            "out_of_domain": {
                "count": len(out),
                "non_empty_returns": sum(bool(details.get(item["id"], {}).get("ranked")) for item in out),
            },
            "not_applicable_no_evidence_queries": (
                len(negatives) - len(candidates) if name == "symbols" else 0
            ),
        }
    return result


def _fixed_threshold_result(report: dict, dataset: dict) -> dict | None:
    vector = report["retrievers"].get("vector", {})
    if vector.get("status") == "not_measured":
        return None
    from .calibration import evaluate_fixed_rule

    rows = _vector_rule_rows(report, dataset)
    result = evaluate_fixed_rule(rows, {
        "family": "top_score",
        "top_score_threshold": HISTORICAL_TOP_SCORE_THRESHOLD,
    })
    result["no_candidate_score_policy"] = "A query with no vector candidate is assigned top_score=0 and is not accepted."
    return result


def _vector_rule_rows(report: dict, dataset: dict) -> list[dict]:
    """返回带标签的向量信号，并明确规定无候选时的评分方式。"""
    vector = report["retrievers"].get("vector", {})
    if vector.get("status") == "not_measured":
        return []
    details = {row["query_id"]: row for row in vector["query_results"]}
    rows = []
    for item in dataset["queries"]:
        detail = details[item["id"]]
        score = detail.get("top_score")
        rows.append({
            "query_id": item["id"],
            "top_score": 0.0 if score is None else score,
            "top_2_margin": detail.get("top_2_margin"),
            "no_evidence": item["no_evidence"],
        })
    return rows


def _metrics_for_validation(reports: dict) -> dict:
    validation = reports["validation"]["retrievers"]
    lexical = validation["lexical"]
    vector = validation["vector"]
    rrf = validation["rrf"]
    if vector.get("status") == "not_measured":
        return {
            "status": "not_decidable_without_real_vector_measurement",
            "productization_recommendation": "retain_explicit_RRF_prototype_only",
            "reason": "Independent validation lacks measured vectors, so no vector-plus-rank-fusion claim is supportable.",
        }
    r1 = rrf["recall_at"]["1"]
    mrr = rrf["mrr"]
    lexical_r1 = lexical["recall_at"]["1"]
    lexical_mrr = lexical["mrr"]
    vector_r1 = vector["recall_at"]["1"]
    vector_mrr = vector["mrr"]
    improved_over_lexical = r1 > lexical_r1 and mrr > lexical_mrr
    no_top_k_drop = all(
        rrf["recall_at"][str(k)] >= lexical["recall_at"][str(k)]
        for k in (5, 10)
    )
    return {
        "status": "provisional_ai_validation_only",
        "rrf_improves_recall1_and_mrr_over_lexical": improved_over_lexical,
        "rrf_recall5_and_recall10_do_not_drop_vs_lexical": no_top_k_drop,
        "rrf_recall1_vs_lexical_delta": round(r1 - lexical_r1, 6),
        "rrf_mrr_vs_lexical_delta": round(mrr - lexical_mrr, 6),
        "rrf_recall1_vs_vector_delta": round(r1 - vector_r1, 6),
        "rrf_mrr_vs_vector_delta": round(mrr - vector_mrr, 6),
        "validation_no_evidence_return_rate": reports["validation"]["no_evidence_returns"]["rrf"]["non_empty_return_rate"],
        "productization_recommendation": (
            "worth_a_default_off_explicit_experimental_option_for_further_review"
            if improved_over_lexical and no_top_k_drop
            else "retain_explicit_RRF_prototype_only"
        ),
        "qualification": (
            "AI-provisional labels on one independent synthetic corpus are not human ground truth; this decision is exploratory."
        ),
        "vector_is_also_a_baseline": {
            "rrf_recall1_vs_vector": round(r1 - vector_r1, 6),
            "rrf_mrr_vs_vector": round(mrr - vector_mrr, 6),
        },
    }


def evaluate_generalization(*, base_url: str | None = None, model_id: str | None = None,
                            api_model: str | None = None, dimension: int = 1024,
                            vector_unavailable_reason: str | None = None,
                            corpus_path: Path = DEFAULT_CORPUS,
                            dataset_path: Path = DEFAULT_DATASET,
                            split_plan_path: Path = DEFAULT_SPLIT_PLAN,
                            expected_dataset_version: str = GENERALIZATION_DATASET_VERSION,
                            calibrate_abstention_rule: bool = False) -> dict:
    """先运行 Calibration 并固定 k/阈值，再对 Validation 评测一次。"""
    from .embeddings import LlamaServerEmbeddingProvider, VectorCandidateRetriever
    from .evaluation import evaluate_benchmark
    from .fusion import RrfRetriever
    from .indexer import index_repository
    from .store import IndexStore

    dataset = load_generalization_dataset(
        dataset_path, expected_version=expected_dataset_version
    )
    plan = _read_json(Path(split_plan_path))
    split_validation = validate_generalization_split(
        dataset, plan, expected_version=expected_dataset_version
    )
    evidence_validation = validate_evidence(dataset, corpus_path)
    manifest = corpus_manifest(corpus_path)
    if (base_url is None) != (model_id is None) or (base_url is None) != (api_model is None):
        raise ValueError("Provide base_url, model_id and api_model together, or omit all for offline retrieval only.")
    if vector_unavailable_reason is not None and (
        not isinstance(vector_unavailable_reason, str) or not vector_unavailable_reason.strip()
    ):
        raise ValueError("vector_unavailable_reason must be a non-empty string when provided.")
    if not isinstance(calibrate_abstention_rule, bool):
        raise ValueError("calibrate_abstention_rule must be boolean.")

    started = perf_counter()
    provider = None
    if base_url is not None:
        provider = LlamaServerEmbeddingProvider(
            base_url, model_id=model_id, api_model=api_model, dimension=dimension,
            connect_timeout=2.0, read_timeout=60.0,
        )

    with IndexStore(":memory:") as store:
        indexed = index_repository(corpus_path, store)
        vector = VectorCandidateRetriever(store, provider) if provider is not None else None
        embedding_index_seconds = None
        if vector is not None:
            embedding_index_started = perf_counter()
            vector_index = vector.index_current_index(include_symbols=False)
            embedding_index_seconds = round(perf_counter() - embedding_index_started, 6)
        else:
            vector_index = None
        rrf = RrfRetriever(store, vector_retriever=vector, k=RRF_K, source_top_k=10)

        def vector_search(query: str, *, top_k: int):
            return vector.search(query, top_k=top_k)

        def rrf_search(query: str, *, top_k: int, symbol_query=None):
            return rrf.search(query, top_k=top_k, symbol_query=symbol_query)

        reports = {}
        fixed_configuration = None
        calibration_analysis = None
        selected_abstention_rule = None
        for split_name in ("calibration", "validation"):
            if split_name == "validation" and fixed_configuration is None:
                raise RuntimeError("Validation cannot run before the experiment configuration is frozen.")
            subset = _split_dataset(dataset, split_name)
            benchmark = evaluate_benchmark(
                store, subset,
                vector_search=vector_search if vector is not None else None,
                rrf_search=rrf_search,
                include_timings=True,
            )
            reports[split_name] = {
                "status": "evaluated_once_with_fixed_parameters" if split_name == "validation" else "calibration_partition",
                "query_count": len(subset["queries"]),
                "positive_query_count": sum(not row["no_evidence"] for row in subset["queries"]),
                "no_evidence_query_count": sum(row["no_evidence"] for row in subset["queries"]),
                "near_domain_negative_count": sum(row["no_evidence"] and row["difficulty"] == "near_domain" for row in subset["queries"]),
                "out_of_domain_negative_count": sum(row["no_evidence"] and row["difficulty"] == "out_of_domain" for row in subset["queries"]),
                "category_counts": dict(sorted(Counter(row["category"] for row in subset["queries"]).items())),
                "retrievers": benchmark["retrievers"],
                "no_evidence_returns": _no_evidence_summary(benchmark, subset),
                "historical_threshold_070": _fixed_threshold_result(benchmark, subset),
                "query_results": {
                    name: value.get("query_results", [])
                    for name, value in benchmark["retrievers"].items()
                },
            }
            if split_name == "calibration":
                # AF-009FA 既有规则和 k=60 事先固定；先记录 Calibration，再运行 Validation。
                fixed_configuration = {
                    "rrf_k": RRF_K,
                    "top_score_rule": "top_score >= 0.70",
                    "validation_used_for_tuning": False,
                    "validation_evaluations": 1,
                }
                if calibrate_abstention_rule and vector is not None:
                    from .calibration import calibrate_abstention

                    calibration_rows = _vector_rule_rows(benchmark, subset)
                    calibration_analysis = calibrate_abstention(calibration_rows)
                    selected = calibration_analysis["selected"]
                    selected_abstention_rule = None if selected is None else selected["rule"]
                    fixed_configuration["calibration_selected_rule"] = selected_abstention_rule
                    fixed_configuration["calibration_selection_protocol"] = calibration_analysis[
                        "selection_protocol"
                    ]
                    fixed_configuration["validation_consulted_for_selection"] = False
        vector_bytes = store.connection.execute(
            "SELECT COALESCE(SUM(length(vector)), 0) FROM vector_embeddings"
        ).fetchone()[0] if vector is not None else 0
        database_metrics = store.status()

    calibration_rule = reports["calibration"]["historical_threshold_070"]
    validation_rule = reports["validation"]["historical_threshold_070"]
    threshold_comparison = None
    if calibration_rule is not None and validation_rule is not None:
        threshold_comparison = {
            "rule": "top_score >= 0.70",
            "calibration": calibration_rule,
            "validation": validation_rule,
            "validation_minus_calibration": {
                key: round(validation_rule[key] - calibration_rule[key], 6)
                for key in ("coverage", "false_accept_rate", "false_reject_rate")
                if validation_rule[key] is not None and calibration_rule[key] is not None
            },
            "predeclared_acceptance_requirements": {
                "false_accept_rate_max": 0.0,
                "false_reject_rate_max": 0.10,
                "passed": (
                    validation_rule["false_accept_rate"] <= 0.0
                    and validation_rule["false_reject_rate"] <= 0.10
                ),
                "note": "A zero observed false-accept rate on 10 negative samples is not a population guarantee.",
            },
        }

    frozen_rule_validation = None
    engineering_target_result = None
    if selected_abstention_rule is not None:
        from .calibration import evaluate_fixed_rule

        validation_subset = _split_dataset(dataset, "validation")
        validation_rows = _vector_rule_rows(
            {"retrievers": reports["validation"]["retrievers"]}, validation_subset
        )
        frozen_rule_validation = evaluate_fixed_rule(
            validation_rows, selected_abstention_rule
        )
        targets = plan.get("engineering_targets")
        if isinstance(targets, dict):
            far_max = targets.get("false_accept_rate_max")
            frr_max = targets.get("false_reject_rate_max")
            if isinstance(far_max, (int, float)) and isinstance(frr_max, (int, float)):
                engineering_target_result = {
                    "predeclared": {
                        "false_accept_rate_max": float(far_max),
                        "false_reject_rate_max": float(frr_max),
                    },
                    "passed": (
                        frozen_rule_validation["false_accept_rate"] <= float(far_max)
                        and frozen_rule_validation["false_reject_rate"] <= float(frr_max)
                    ),
                }

    from .evaluation import _validate_dataset as validate_dataset
    validated_queries = validate_dataset(dataset)
    return {
        "status": "provisional_ai_generalization_experiment",
        "human_reviewed": False,
        "production_threshold_enabled": False,
        "dataset": {
            "version": dataset["dataset_version"],
            "review_status": dataset["status"],
            "query_total": len(validated_queries),
            "positive_queries": sum(not item["no_evidence"] for item in validated_queries),
            "no_evidence_queries": sum(item["no_evidence"] for item in validated_queries),
            "near_domain_negatives": sum(item["no_evidence"] and item["difficulty"] == "near_domain" for item in validated_queries),
            "out_of_domain_negatives": sum(item["no_evidence"] and item["difficulty"] == "out_of_domain" for item in validated_queries),
            "split_plan_validation": split_validation,
            "evidence_validation": evidence_validation,
            "corpus_manifest": manifest,
        },
        "model": None if provider is None else {
            "model_id": provider.model_id,
            "api_model": provider.api_model,
            "dimension": provider.dimension,
            "endpoint": base_url + "/v1/embeddings",
            "query_instruction": provider.query_instruction,
            "connect_timeout_seconds": provider.connect_timeout,
            "read_timeout_seconds": provider.read_timeout,
        },
        "vector_measurement": {
            "status": "measured" if provider is not None else "not_measured",
            "reason": None if provider is not None else (
                vector_unavailable_reason
                or "No validated local embedding endpoint was provided; Vector results were not measured."
            ),
            "embedding_requests_sent": provider is not None,
            "request_counts": provider.request_counts if provider is not None else None,
        },
        "index": {
            "store": "SQLite :memory:",
            "indexed_repository": indexed,
            "vector_index": vector_index,
            "embedding_index_seconds": embedding_index_seconds,
            "vector_blob_bytes": vector_bytes,
            "database_status": database_metrics,
        },
        "fixed_experiment_configuration": fixed_configuration,
        "calibration": reports["calibration"],
        "validation": reports["validation"],
        "threshold_transfer": threshold_comparison,
        "calibration_selected_abstention": calibration_analysis,
        "frozen_abstention_validation": frozen_rule_validation,
        "abstention_engineering_target": engineering_target_result,
        "rrf_decision": _metrics_for_validation(reports),
        "historical_reference": {
            "source": "AF009FA_RRF_EVALUATION.md and AF009FA_RRF_RESULTS.json",
            "dataset": "agentforge-retrieval-provisional-v3 (AI-only; 40 queries)",
            "rrf_k": 60,
            "validation_threshold_070": {
                "coverage": 0.10,
                "false_accept_rate": 0.0,
                "false_reject_rate": 0.80,
                "precision": 1.0,
                "sample_counts": {"total": 20, "evidence": 10, "no_evidence": 10},
            },
            "rrf_metrics": {"recall_at": {"1": 0.975, "5": 1.0, "10": 1.0}, "mrr": 1.0},
        },
        "elapsed_seconds": round(perf_counter() - started, 6),
        "limitations": [
            "All labels are AI evidence review and are not human ground truth.",
            "The second corpus is synthetic and small; results do not establish general performance on user repositories.",
            "RRF ranking quality and no-evidence rejection are separate; RRF does not provide abstention.",
            "The historical threshold remains an offline analysis only and is not enabled in production.",
            "Symbol metrics are applicable only to queries with explicit exact-symbol filters.",
            *([vector_unavailable_reason] if provider is None and vector_unavailable_reason else []),
        ],
    }
