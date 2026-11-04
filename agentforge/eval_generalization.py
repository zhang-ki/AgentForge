"""在独立合成仓库和暂定标签上运行 AF-009G。"""

import argparse
import json
from pathlib import Path
import sys

from .generalization import evaluate_generalization


PROJECT_ROOT = Path(__file__).resolve().parent.parent
RESULT_PATH = PROJECT_ROOT / "AF009G_GENERALIZATION_RESULTS.json"


def _parser():
    parser = argparse.ArgumentParser(
        description=(
            "Evaluate the independent AF-009G synthetic corpus. Optional vectors are sent only "
            "to an explicitly configured loopback llama-server embedding endpoint."
        )
    )
    parser.add_argument("--base-url", help="Explicit loopback HTTP origin, e.g. http://127.0.0.1:PORT.")
    parser.add_argument("--model-id", help="Traceable local embedding model identity.")
    parser.add_argument("--api-model", help="llama-server embedding model alias.")
    parser.add_argument("--dimension", type=int, default=1024)
    parser.add_argument(
        "--vector-unavailable-reason",
        help="Record why real Vector measurements are unavailable while running offline channels.",
    )
    return parser


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        report = evaluate_generalization(
            base_url=args.base_url, model_id=args.model_id,
            api_model=args.api_model, dimension=args.dimension,
            vector_unavailable_reason=args.vector_unavailable_reason,
        )
        RESULT_PATH.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        summary = {
            "status": report["status"],
            "result_path": str(RESULT_PATH),
            "dataset": report["dataset"],
            "vector_measurement": report["vector_measurement"],
            "calibration": {
                "metrics": {
                    name: {
                        "recall_at": value.get("recall_at"), "mrr": value.get("mrr"),
                        "grounded_query_count": value.get("grounded_query_count"),
                    }
                    for name, value in report["calibration"]["retrievers"].items()
                },
                "threshold": report["calibration"]["historical_threshold_070"],
            },
            "validation": {
                "metrics": {
                    name: {
                        "recall_at": value.get("recall_at"), "mrr": value.get("mrr"),
                        "grounded_query_count": value.get("grounded_query_count"),
                    }
                    for name, value in report["validation"]["retrievers"].items()
                },
                "no_evidence_returns": report["validation"]["no_evidence_returns"],
                "threshold": report["validation"]["historical_threshold_070"],
            },
            "rrf_decision": report["rrf_decision"],
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"AF-009G evaluation failed ({type(exc).__name__}): {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
