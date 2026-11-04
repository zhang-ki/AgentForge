"""完全离线运行有版本标记的合成检索基准。"""

import argparse
import json
from pathlib import Path
import sys

from .evaluation import evaluate_benchmark
from .indexer import index_repository
from .store import IndexStore


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATASET_PATH = PROJECT_ROOT / "tests" / "fixtures" / "retrieval_eval.json"
CANDIDATE_DATASET_PATH = (
    PROJECT_ROOT / "tests" / "fixtures" / "retrieval_eval_expansion_candidates.json"
)
REVIEW_POOL_DATASET_PATH = (
    PROJECT_ROOT / "tests" / "fixtures" / "retrieval_eval_review_pool_v2.json"
)
CORPUS_PATH = PROJECT_ROOT / "tests" / "fixtures" / "retrieval_eval_repo"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate lexical and exact-symbol retrieval offline.")
    parser.add_argument(
        "--dataset", choices=("baseline", "expansion", "review-pool"), default="baseline",
        help="Evaluate the frozen baseline, AF-009D expansion, or AF-009E review pool.",
    )
    args = parser.parse_args(argv)
    dataset_path = {
        "baseline": DATASET_PATH,
        "expansion": CANDIDATE_DATASET_PATH,
        "review-pool": REVIEW_POOL_DATASET_PATH,
    }[args.dataset]
    try:
        with dataset_path.open("r", encoding="utf-8") as stream:
            dataset = json.load(stream)
        with IndexStore(":memory:") as store:
            index_repository(CORPUS_PATH, store)
            report = evaluate_benchmark(store, dataset)
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Retrieval evaluation failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
