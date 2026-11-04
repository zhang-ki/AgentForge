"""AF-010-PRE 独立合成评测与决策分析。"""

from pathlib import Path

from .evaluation import PREPRODUCT_DATASET_VERSION
from .generalization import (
    PROJECT_ROOT,
    _read_json,
    corpus_manifest,
    evaluate_generalization,
    load_generalization_dataset,
    validate_evidence,
    validate_generalization_split,
)


DEFAULT_CORPUS = PROJECT_ROOT / "tests" / "fixtures" / "retrieval_eval_preproduct_repo"
DEFAULT_DATASET = PROJECT_ROOT / "tests" / "fixtures" / "retrieval_eval_preproduct_v1.json"
DEFAULT_SPLIT_PLAN = PROJECT_ROOT / "tests" / "fixtures" / "retrieval_eval_preproduct_split_v1.json"
EXPECTED_QUERY_TOTAL = 80
EXPECTED_SPLIT_TOTAL = 40
EXPECTED_SPLIT_POSITIVE = 20
EXPECTED_SPLIT_NEGATIVE = 20
EVIDENCE_TYPES = {"definition", "implementation", "caller", "import", "documentation", "none"}


def validate_preproduct_fixture(
        dataset_path: Path = DEFAULT_DATASET,
        split_plan_path: Path = DEFAULT_SPLIT_PLAN,
        corpus_path: Path = DEFAULT_CORPUS) -> dict:
    """验证人工编写的标签、证据、数据集隔离和声明的来源。"""
    dataset = load_generalization_dataset(
        dataset_path, expected_version=PREPRODUCT_DATASET_VERSION
    )
    plan = _read_json(Path(split_plan_path))
    split = validate_generalization_split(
        dataset, plan, expected_version=PREPRODUCT_DATASET_VERSION
    )
    evidence = validate_evidence(dataset, corpus_path)
    queries = dataset["queries"]
    if len(queries) != EXPECTED_QUERY_TOTAL:
        raise ValueError("AF-010-PRE fixture must contain exactly 80 queries.")
    if dataset.get("human_reviewed") is not False:
        raise ValueError("AF-010-PRE labels must not claim human review.")
    if dataset.get("query_total") != EXPECTED_QUERY_TOTAL:
        raise ValueError("AF-010-PRE declared query total does not match the fixture.")
    for row in queries:
        if row.get("label_origin") != "synthetic_fixture_design":
            raise ValueError(f"{row['id']}: label origin must remain synthetic_fixture_design.")
        if row.get("evidence_type") not in EVIDENCE_TYPES:
            raise ValueError(f"{row['id']}: unsupported or missing evidence type.")
        if row["no_evidence"] != (row["evidence_type"] == "none"):
            raise ValueError(f"{row['id']}: evidence type disagrees with no_evidence.")
    for split_name, summary in split["summary"].items():
        if (summary["query_count"] != EXPECTED_SPLIT_TOTAL
                or summary["evidence"] != EXPECTED_SPLIT_POSITIVE
                or summary["no_evidence"] != EXPECTED_SPLIT_NEGATIVE):
            raise ValueError(f"{split_name}: expected an exact 40-query 20/20 split.")
    if plan.get("engineering_targets") != {
        "false_accept_rate_max": 0.05,
        "false_reject_rate_max": 0.25,
        "rrf_requires_recall1_and_mrr_gain": True,
        "no_unexplained_recall5_or_recall10_drop": True,
    }:
        raise ValueError("AF-010-PRE engineering targets are missing or changed.")
    return {
        "status": "validated_synthetic_ai_fixture",
        "human_reviewed": False,
        "query_count": len(queries),
        "split": split,
        "evidence": evidence,
        "corpus_manifest": corpus_manifest(corpus_path),
    }


def _result_map(split_report: dict, retriever: str) -> dict:
    return {
        row["query_id"]: row
        for row in split_report["retrievers"][retriever].get("query_results", [])
    }


def _top1_hits(rows: dict, positive_ids: set[str]) -> set[str]:
    return {
        query_id for query_id, row in rows.items()
        if query_id in positive_ids and row.get("ranked")
        and row["ranked"][0] in set(row.get("relevant", []))
    }


def _mean_top10_jaccard(left: dict, right: dict, query_ids: list[str]) -> float:
    values = []
    for query_id in query_ids:
        a = set(left.get(query_id, {}).get("ranked", [])[:10])
        b = set(right.get(query_id, {}).get("ranked", [])[:10])
        union = a | b
        values.append(len(a & b) / len(union) if union else 1.0)
    return round(sum(values) / len(values), 6) if values else 0.0


def retrieval_complementarity(dataset: dict, report: dict) -> dict:
    """解释通道间重叠和唯一相关首位命中，不重新标注数据。"""
    output = {}
    for split_name in ("calibration", "validation"):
        split_report = report[split_name]
        split_queries = [row for row in dataset["queries"] if row["split"] == split_name]
        positive_ids = {row["id"] for row in split_queries if not row["no_evidence"]}
        ordered_ids = [row["id"] for row in split_queries]
        maps = {
            name: _result_map(split_report, name)
            for name in ("lexical", "symbols", "vector", "rrf")
        }
        hits = {name: _top1_hits(rows, positive_ids) for name, rows in maps.items()}
        base_union = hits["lexical"] | hits["symbols"] | hits["vector"]
        output[split_name] = {
            "positive_query_count": len(positive_ids),
            "top1_hit_ids": {name: sorted(value) for name, value in hits.items()},
            "unique_top1_hits": {
                name: sorted(value - set().union(*(hits[other] for other in ("lexical", "symbols", "vector") if other != name)))
                for name, value in hits.items() if name in {"lexical", "symbols", "vector"}
            },
            "lexical_vector_top1": {
                "both": sorted(hits["lexical"] & hits["vector"]),
                "lexical_only": sorted(hits["lexical"] - hits["vector"]),
                "vector_only": sorted(hits["vector"] - hits["lexical"]),
                "neither": sorted(positive_ids - (hits["lexical"] | hits["vector"])),
            },
            "rrf_vs_lexical_top1": {
                "improved": sorted(hits["rrf"] - hits["lexical"]),
                "regressed": sorted(hits["lexical"] - hits["rrf"]),
                "same_hit_count": len(hits["rrf"] & hits["lexical"]),
            },
            "candidate_overlap": {
                "mean_top10_jaccard_lexical_vector": _mean_top10_jaccard(
                    maps["lexical"], maps["vector"], ordered_ids
                ),
                "mean_top10_jaccard_lexical_rrf": _mean_top10_jaccard(
                    maps["lexical"], maps["rrf"], ordered_ids
                ),
            },
            "any_base_channel_top1_hit_count": len(base_union),
        }
    return output


def _product_decision(report: dict) -> dict:
    validation = report["validation"]["retrievers"]
    lexical = validation["lexical"]
    rrf = validation["rrf"]
    abstention = report.get("abstention_engineering_target")
    ranking_gain = (
        rrf["recall_at"]["1"] > lexical["recall_at"]["1"]
        and rrf["mrr"] > lexical["mrr"]
        and rrf["recall_at"]["5"] >= lexical["recall_at"]["5"]
        and rrf["recall_at"]["10"] >= lexical["recall_at"]["10"]
    )
    abstention_passed = bool(abstention and abstention.get("passed"))
    approved = ranking_gain and abstention_passed
    return {
        "af010_product_implementation_approved": approved,
        "rrf_ranking_requirement_passed": ranking_gain,
        "abstention_engineering_target_passed": abstention_passed,
        "production_threshold_enabled": False,
        "default_retriever_modified": False,
        "decision": "approve_product_implementation" if approved else "continue_to_defer",
        "reason": (
            "Independent ranking and abstention requirements both passed."
            if approved else
            "At least one predeclared ranking or abstention requirement did not pass independent Validation."
        ),
    }


def evaluate_preproduct(*, base_url: str | None = None, model_id: str | None = None,
                        api_model: str | None = None, dimension: int = 1024,
                        vector_unavailable_reason: str | None = None) -> dict:
    fixture = validate_preproduct_fixture()
    dataset = load_generalization_dataset(
        DEFAULT_DATASET, expected_version=PREPRODUCT_DATASET_VERSION
    )
    report = evaluate_generalization(
        base_url=base_url,
        model_id=model_id,
        api_model=api_model,
        dimension=dimension,
        vector_unavailable_reason=vector_unavailable_reason,
        corpus_path=DEFAULT_CORPUS,
        dataset_path=DEFAULT_DATASET,
        split_plan_path=DEFAULT_SPLIT_PLAN,
        expected_dataset_version=PREPRODUCT_DATASET_VERSION,
        calibrate_abstention_rule=base_url is not None,
    )
    report["phase"] = "AF-010-PRE"
    report["fixture_validation"] = fixture
    if base_url is not None:
        report["retrieval_complementarity"] = retrieval_complementarity(dataset, report)
        report["product_entry_decision"] = _product_decision(report)
    else:
        report["retrieval_complementarity"] = None
        report["product_entry_decision"] = {
            "af010_product_implementation_approved": False,
            "decision": "not_decidable_without_real_vectors",
            "production_threshold_enabled": False,
            "default_retriever_modified": False,
        }
    return report
