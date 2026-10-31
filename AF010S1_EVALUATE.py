"""Run the isolated AF-010-S1 package-relation experiment."""

from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path

from agentforge.indexer import index_repository
from agentforge.package_evaluation import evaluate_package_relations, validate_package_dataset
from agentforge.package_relations import build_package_relation_graph
from agentforge.store import IndexStore


ROOT = Path(__file__).resolve().parent
REPOSITORY = ROOT / "tests" / "fixtures" / "package_relation_repo"
DATASET_PATH = ROOT / "tests" / "fixtures" / "package_relation_eval.json"
SPLIT_PATH = ROOT / "tests" / "fixtures" / "package_relation_split.json"
RESULT_PATH = ROOT / "AF010S1_RESULTS.json"


def fingerprint(path: Path) -> dict:
    return {
        "path": path.relative_to(ROOT).as_posix(),
        "size_bytes": path.stat().st_size,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest().upper(),
    }


def main() -> int:
    dataset = json.loads(DATASET_PATH.read_text(encoding="utf-8"))
    split = json.loads(SPLIT_PATH.read_text(encoding="utf-8"))
    validation = validate_package_dataset(dataset, split, REPOSITORY)
    graph = build_package_relation_graph(REPOSITORY)
    store = IndexStore(":memory:")
    reports = {}
    try:
        index_repository(REPOSITORY, store)
        for split_name in ("calibration", "validation"):
            subset = {
                **dataset,
                "queries": [item for item in dataset["queries"] if item["split"] == split_name],
            }
            reports[split_name] = evaluate_package_relations(store, subset, graph)
    finally:
        store.close()

    protected = [
        ROOT / ".agentforge" / "data" / "index.sqlite3",
        ROOT / ".agentforge" / "data" / "read_only_evidence.json",
        ROOT / "AF010PRE_RESULTS.json",
        ROOT / "tests" / "fixtures" / "retrieval_eval.json",
        ROOT / "tests" / "fixtures" / "retrieval_eval_preproduct_v1.json",
        ROOT / "tests" / "fixtures" / "retrieval_eval_preproduct_split_v1.json",
    ]
    output = {
        "experiment": "AF-010-S1",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": "research_prototype_complete",
        "dataset_validation": validation,
        "fixture": {
            "repository": REPOSITORY.relative_to(ROOT).as_posix(),
            "file_count": sum(path.is_file() for path in REPOSITORY.rglob("*")),
            "dataset": fingerprint(DATASET_PATH),
            "split_plan": fingerprint(SPLIT_PATH),
            "label_source": "synthetic_fixture_design",
            "human_reviewed": False,
        },
        "graph": {
            "relation_count": len(graph.relations),
            "relation_types": dict(sorted(Counter(
                item.relation_type for item in graph.relations
            ).items())),
            "resolution_states": dict(sorted(Counter(
                item.resolution_status for item in graph.relations
            ).items())),
            "cycle_relation_count": sum(item.cycle_detected for item in graph.relations),
            "parse_failures": [list(value) for value in graph.parse_failures],
        },
        "splits": reports,
        "vector": {
            "status": "not_measured",
            "reason": (
                "AF-010-S1 is a static relation study; no local model service was restarted "
                "and no embedding/network request was sent."
            ),
        },
        "rrf": {
            "status": "offline_exploration_only",
            "k": 60,
            "channels": ["lexical", "symbols", "package_relations"],
            "vector_included": False,
        },
        "product_state": {
            "default_retriever_changed": False,
            "package_channel_enabled_by_default": False,
            "production_abstention_enabled": False,
            "schema_changed": False,
        },
        "protected_files_at_evaluation": [fingerprint(path) for path in protected if path.exists()],
    }
    temporary = RESULT_PATH.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(output, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temporary, RESULT_PATH)
    print(json.dumps({
        "result": RESULT_PATH.name,
        "query_count": validation["query_count"],
        "relation_count": len(graph.relations),
        "calibration": {
            name: reports["calibration"]["retrievers"][name].get("recall_at")
            for name in ("lexical", "symbols", "package_relations", "rrf_lexical_symbol_package")
        },
        "validation": {
            name: reports["validation"]["retrievers"][name].get("recall_at")
            for name in ("lexical", "symbols", "package_relations", "rrf_lexical_symbol_package")
        },
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
