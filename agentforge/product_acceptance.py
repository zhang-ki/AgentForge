"""Deterministic AF-011 acceptance over scanner-approved AgentForge source."""

from __future__ import annotations

from collections import defaultdict
import json
from pathlib import Path
from time import perf_counter

from .answer_contract import build_offline_contract, validate_answer_contract
from .context import build_context, validate_citations
from .indexer import index_repository
from .product_retrieval import ProductRetriever
from .providers.offline import OfflineProvider
from .chunker import split_source_lines
from .scanner import read_candidate, scan_repository
from .store import IndexStore


TOP_KS = (1, 5, 10)


def load_and_validate_tasks(path: Path, repository: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    tasks = data.get("tasks")
    if (not isinstance(tasks, list) or len(tasks) < 2
            or data.get("human_reviewed") is not False):
        raise ValueError("AF-011 task data must be a non-human-reviewed task list.")
    label_source = data.get("label_source")
    if label_source not in {"ai_evidence_review", "synthetic_fixture_design", "deterministic_test"}:
        raise ValueError("Task label_source is missing or unsupported.")
    if label_source == "ai_evidence_review" and (
            not data.get("repository_scope") or not isinstance(data.get("split_plan"), dict)):
        raise ValueError("AI-reviewed task data must declare its snapshot scope and split plan.")
    ids, groups = set(), defaultdict(set)
    report = scan_repository(repository)
    if not report.complete:
        raise ValueError("Repository scan is incomplete.")
    candidates = {item.relative_path: item for item in report.candidates}
    for task in tasks:
        if (not isinstance(task, dict) or not isinstance(task.get("id"), str)
                or not task["id"] or task["id"] in ids
                or task.get("split") not in {"calibration", "validation"}
                or not isinstance(task.get("leakage_group"), str)
                or not isinstance(task.get("category"), str)
                or task.get("human_reviewed", False) is not False):
            raise ValueError("Task IDs must be unique and splits must be valid.")
        ids.add(task["id"])
        groups[task["leakage_group"]].add(task["split"])
        if not isinstance(task["query"], str) or not task["query"].strip():
            raise ValueError("Task queries must be non-empty strings.")
        relevant = task.get("relevant")
        if not isinstance(relevant, list):
            raise ValueError(f"Task relevant evidence must be a list: {task['id']}")
        if label_source == "ai_evidence_review":
            if task.get("review_method") != "ai_evidence_review" or task.get(
                    "review_status") != "ai_reviewed_not_human":
                raise ValueError(f"AI review provenance is invalid: {task['id']}")
            if relevant and not task.get("relevance_reason"):
                raise ValueError(f"Positive task has no relevance rationale: {task['id']}")
            if not relevant and (not task.get("no_evidence_scope") or not task.get("absence_reason")):
                raise ValueError(f"Negative task lacks a snapshot scope or absence rationale: {task['id']}")
        for evidence in relevant:
            if not isinstance(evidence, dict):
                raise ValueError(f"Evidence record is invalid: {task['id']}")
            candidate = candidates.get(evidence["path"])
            if candidate is None:
                raise ValueError(f"Task evidence path is not scanner-approved: {evidence['path']}")
            lines = split_source_lines(read_candidate(candidate, report.root))
            if (isinstance(evidence.get("start_line"), bool)
                    or not isinstance(evidence.get("start_line"), int)
                    or isinstance(evidence.get("end_line"), bool)
                    or not isinstance(evidence.get("end_line"), int)
                    or evidence["start_line"] < 1 or evidence["end_line"] < evidence["start_line"]
                    or evidence["end_line"] > len(lines)):
                raise ValueError(f"Task evidence line range is invalid: {task['id']}")
            if label_source == "ai_evidence_review":
                snippet = "\n".join(lines[evidence["start_line"] - 1:evidence["end_line"]])
                if (not evidence.get("anchor") or evidence["anchor"] not in snippet
                        or not evidence.get("evidence_type")
                        or not evidence.get("relevance_reason")):
                    raise ValueError(f"Task evidence anchor or rationale is invalid: {task['id']}")
    if any(len(values) != 1 for values in groups.values()):
        raise ValueError("A leakage group crosses Calibration and Validation.")
    if label_source == "ai_evidence_review":
        split_plan = data["split_plan"]
        for split in ("calibration", "validation"):
            planned = split_plan.get(split + "_ids")
            observed = [task["id"] for task in tasks if task["split"] == split]
            if planned != observed:
                raise ValueError(f"Split plan does not exactly match the {split} task IDs.")
    return data


def _metrics(rows: list[dict]) -> dict:
    positives = [row for row in rows if row["relevant"]]
    negatives = [row for row in rows if not row["relevant"]]
    recall = {}
    for k in TOP_KS:
        recall[str(k)] = round(sum(
            bool(set(row["ranked"][:k]) & set(row["relevant"])) for row in positives
        ) / len(positives), 6) if positives else None
    reciprocal = []
    for row in positives:
        relevant = set(row["relevant"])
        rank = next((index for index, path in enumerate(row["ranked"], 1)
                     if path in relevant), None)
        reciprocal.append(0.0 if rank is None else 1.0 / rank)
    return {
        "query_count": len(rows), "positive_count": len(positives),
        "no_evidence_count": len(negatives), "recall_at": recall,
        "mrr": round(sum(reciprocal) / len(reciprocal), 6) if reciprocal else None,
        "route_accuracy": round(sum(row["route_correct"] for row in rows) / len(rows), 6),
        "evidence_contract_valid_count": sum(row["evidence_contract_valid"] for row in rows),
        "evidence_contract_valid_rate": round(
            sum(row["evidence_contract_valid"] for row in rows) / len(rows), 6
        ),
        "model_citation_validation": "not_measured_offline_provider_does_not_generate_claims",
        "strict_refusal_count": sum(row["contract_status"] == "refused" for row in negatives),
        "strict_refusal_count_on_no_evidence": sum(
            row["contract_status"] == "refused" for row in negatives
        ),
        "strict_refusal_rate_on_no_evidence": round(sum(
            row["contract_status"] == "refused" for row in negatives
        ) / len(negatives), 6) if negatives else None,
        "non_assertive_count_on_no_evidence": sum(
            row["contract_status"] in {"refused", "limited"} for row in negatives
        ),
        "false_accept_count_on_no_evidence": sum(
            row["contract_status"] == "answered" for row in negatives
        ),
        "candidate_nonempty_count_on_no_evidence": sum(bool(row["ranked"]) for row in negatives),
        "candidate_nonempty_rate_on_no_evidence": round(
            sum(bool(row["ranked"]) for row in negatives) / len(negatives), 6
        ) if negatives else None,
        "false_refusal_count_on_positive": sum(
            row["contract_status"] == "refused" for row in positives
        ),
        "false_refusal_rate_on_positive": round(sum(
            row["contract_status"] == "refused" for row in positives
        ) / len(positives), 6) if positives else None,
        "end_to_end_success_count": sum(row["end_to_end_success"] for row in rows),
        "end_to_end_success_rate": round(sum(
            row["end_to_end_success"] for row in rows
        ) / len(rows), 6),
        "mean_latency_seconds": round(sum(row["latency_seconds"] for row in rows) / len(rows), 6),
    }


def evaluate_product_tasks(repository: Path, dataset_path: Path) -> dict:
    data = load_and_validate_tasks(dataset_path, repository)
    started = perf_counter()
    rows = []
    with IndexStore(":memory:") as store:
        index_result = index_repository(repository, store)
        retriever = ProductRetriever(store, repository)
        for task in data["tasks"]:
            query_started = perf_counter()
            outcome = retriever.search(task["query"], top_k=10)
            context = build_context(
                task["query"], [item.as_search_result() for item in outcome.candidates],
                max_candidates=10, budget=12000,
            )
            answer = OfflineProvider().answer(context)
            candidate_map = {item.citation: item for item in outcome.candidates}
            contract = build_offline_contract(
                answer, context, route=outcome.decision.route,
                route_confidence=outcome.decision.confidence,
                candidate_by_citation=candidate_map, uncertainty=outcome.uncertainty,
            )
            validate_answer_contract(contract, context)
            validate_citations(answer.citations, context)
            ranked = [item.relative_path for item in outcome.candidates]
            relevant = [item["path"] for item in task["relevant"]]
            positive_success = (
                bool(set(ranked[:5]) & set(relevant)) and contract.status == "limited"
                if relevant else True
            )
            safe = (
                contract.status in {"refused", "limited"}
                if not relevant else contract.status == "limited" and bool(contract.evidence)
            )
            rows.append({
                "id": task["id"], "split": task["split"],
                "leakage_group": task["leakage_group"], "category": task["category"],
                "query": task["query"], "expected_route": task["expected_route"],
                "actual_route": outcome.decision.route,
                "route_correct": outcome.decision.route == task["expected_route"],
                "route_rule": outcome.decision.rule,
                "route_confidence": outcome.decision.confidence,
                "unavailable_channels": list(outcome.unavailable_channels),
                "uncertainty": outcome.uncertainty,
                "relevant": relevant, "ranked": ranked,
                "candidate_provenance": [{
                    "citation": item.citation, "channels": list(item.channels),
                    "resolution_status": item.resolution_status,
                } for item in outcome.candidates],
                "contract_status": contract.status,
                "contract_reason": contract.reason,
                "evidence_contract_valid": True,
                "offline_citation_validation": "passed",
                "end_to_end_success": bool(positive_success and safe),
                "failure_reason": None if (positive_success and safe) else (
                    contract.reason if contract.status == "refused"
                    else "Relevant evidence not found in top five or answer status was not limited."
                ),
                "latency_seconds": round(perf_counter() - query_started, 6),
            })
    splits = {
        split: _metrics([row for row in rows if row["split"] == split])
        for split in ("calibration", "validation")
    }
    return {
        "stage": "AF-011-offline-product-acceptance",
        "dataset_version": data["dataset_version"],
        "label_source": data["label_source"],
        "human_reviewed": data["human_reviewed"],
        "repository": str(repository.resolve()),
        "index": index_result,
        "task_count": len(rows),
        "splits": splits,
        "query_results": rows,
        "elapsed_seconds": round(perf_counter() - started, 6),
        "production_vector_used": False,
        "rrf_used": False,
        "threshold_used": False,
    }


__all__ = ["evaluate_product_tasks", "load_and_validate_tasks"]
