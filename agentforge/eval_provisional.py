"""针对明确指定的本地 Embedding 服务运行一次 AF-009F/AF-010A。"""

import argparse
import json
from pathlib import Path
import sys
from time import perf_counter

from .calibration import calibrate_abstention, evaluate_fixed_rule
from .embeddings import LlamaServerEmbeddingProvider, VectorCandidateRetriever
from .evaluation import evaluate_benchmark
from .fusion import RrfRetriever
from .indexer import index_repository
from .provisional import load_provisional_dataset
from .store import IndexStore


PROJECT_ROOT = Path(__file__).resolve().parent.parent
CORPUS_PATH = PROJECT_ROOT / "tests" / "fixtures" / "retrieval_eval_repo"
BASELINE_PATH = PROJECT_ROOT / "tests" / "fixtures" / "retrieval_eval.json"
RESULT_PATH = PROJECT_ROOT / "AF009FA_RRF_RESULTS.json"


def _load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as stream:
        return json.load(stream)


def run_experiment(*, base_url: str, model_id: str, api_model: str,
                   dimension: int = 1024, rrf_k: int = 60) -> dict:
    provisional = load_provisional_dataset()
    baseline = _load_json(BASELINE_PATH)
    provider = LlamaServerEmbeddingProvider(
        base_url, model_id=model_id, api_model=api_model, dimension=dimension,
        connect_timeout=2.0, read_timeout=60.0,
    )
    started = perf_counter()
    with IndexStore(":memory:") as store:
        index_repository(CORPUS_PATH, store)
        vector = VectorCandidateRetriever(store, provider)
        index_started = perf_counter()
        vector_index = vector.index_current_index(include_symbols=False)
        vector_index_seconds = perf_counter() - index_started
        rrf = RrfRetriever(store, vector_retriever=vector, k=rrf_k, source_top_k=10)

        def vector_search(query, *, top_k):
            return vector.search(query, top_k=top_k)

        def rrf_search(query, *, top_k, symbol_query=None):
            return rrf.search(query, top_k=top_k, symbol_query=symbol_query)

        provisional_report = evaluate_benchmark(
            store, provisional, vector_search=vector_search, rrf_search=rrf_search,
        )
        vector_rows = provisional_report["retrievers"]["vector"]["query_results"]
        by_id = {row["query_id"]: row for row in vector_rows}
        calibration_ids = [
            item["id"] for item in provisional["queries"] if item["split"] == "calibration"
        ]
        validation_ids = [
            item["id"] for item in provisional["queries"] if item["split"] == "validation"
        ]
        calibration = calibrate_abstention([by_id[query_id] for query_id in calibration_ids])
        if calibration["selected"] is None:
            validation = {
                "status": "not_run_no_zero_false_accept_rule",
                "sample_counts": {"total": len(validation_ids)},
            }
        else:
            validation = {
                "status": "single_evaluation_with_frozen_calibration_rule",
                **evaluate_fixed_rule(
                    [by_id[query_id] for query_id in validation_ids],
                    calibration["selected"]["rule"],
                ),
            }
        baseline_report = evaluate_benchmark(store, baseline, vector_search=vector_search)
        vector_bytes = store.connection.execute(
            "SELECT COALESCE(SUM(length(vector)), 0) FROM vector_embeddings"
        ).fetchone()[0]

    return {
        "status": "provisional_ai_experiment_only",
        "human_reviewed": False,
        "production_threshold_approved": False,
        "dataset": {
            "version": provisional["dataset_version"],
            "query_total": len(provisional["queries"]),
            "calibration_count": len(calibration_ids),
            "validation_count": len(validation_ids),
            "rewrite_count": sum(
                item["ai_decision"] == "rewrite_query" for item in provisional["queries"]
            ),
        },
        "model": {
            "model_id": model_id, "api_model": api_model, "dimension": dimension,
            "endpoint": base_url + "/v1/embeddings",
        },
        "vector_index": {
            **vector_index, "embedding_seconds": round(vector_index_seconds, 6),
            "blob_bytes": vector_bytes, "database": "SQLite :memory:",
        },
        "calibration": calibration,
        "validation": validation,
        "provisional_benchmark": provisional_report,
        "baseline_v1_compatibility": baseline_report,
        "rrf": {
            "k": rrf_k,
            "identity": "repository-relative file path",
            "channels": ["lexical", "symbols when an exact symbol query exists", "vector"],
        },
        "elapsed_seconds": round(perf_counter() - started, 6),
        "limitations": [
            "Labels are provisional AI evidence review, not human reviewed.",
            "The selected abstention rule is exploratory and is not enabled in any retriever.",
            "RRF quality and abstention are separate questions; fusion does not prove evidence sufficiency.",
        ],
    }


def _parser():
    parser = argparse.ArgumentParser(
        description="Run one provisional AF-009F threshold calibration and AF-010A RRF experiment.",
    )
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--model-id", required=True)
    parser.add_argument("--api-model", required=True)
    parser.add_argument("--dimension", type=int, default=1024)
    parser.add_argument("--rrf-k", type=int, default=60)
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        report = run_experiment(
            base_url=args.base_url, model_id=args.model_id, api_model=args.api_model,
            dimension=args.dimension, rrf_k=args.rrf_k,
        )
        RESULT_PATH.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(json.dumps({
            "status": report["status"],
            "result_path": str(RESULT_PATH),
            "selected_rule": report["calibration"]["selected"],
            "validation": report["validation"],
            "provisional_metrics": {
                name: {
                    "recall_at": value.get("recall_at"), "mrr": value.get("mrr"),
                    "grounded_query_count": value.get("grounded_query_count"),
                }
                for name, value in report["provisional_benchmark"]["retrievers"].items()
            },
        }, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except (OSError, ValueError) as exc:
        print(f"Provisional experiment failed ({type(exc).__name__}): {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
