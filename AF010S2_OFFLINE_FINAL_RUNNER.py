"""Capture the frozen no-model S2 comparison after metric protocol checks."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys

from agentforge.package_s2 import evaluate_s2


ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "AF010S2_OFFLINE_FINAL_RESULTS.json"
FIXTURES = ROOT / "tests" / "fixtures"
PROTECTED = (
    "AF010S1_RESULTS.json", "AF010S1_EVALUATION.md", "AF010PRE_RESULTS.json",
    "AF010PRE_EVALUATION.md", "AF009GR5_RESULTS.json", "AF009GR5_EVALUATION.md",
    "tests/fixtures/package_relation_eval.json", "tests/fixtures/package_relation_split.json",
    "tests/fixtures/package_s2_eval_v1.json", "tests/fixtures/package_s2_split_v1.json",
    "tests/fixtures/package_s2_design_v1.json", ".agentforge/data/index.sqlite3",
    ".agentforge/data/read_only_evidence.json",
)


def _snapshot() -> dict:
    relative_paths = set(PROTECTED)
    for pattern in ("retrieval_eval*", "package_relation*"):
        for path in FIXTURES.glob(pattern):
            paths = [path] if path.is_file() else [item for item in path.rglob("*") if item.is_file()]
            relative_paths.update(item.relative_to(ROOT).as_posix() for item in paths)
    relative_paths.update(
        path.relative_to(ROOT).as_posix()
        for path in (FIXTURES / "package_s2_repos").rglob("*") if path.is_file()
    )
    result = {}
    for relative in sorted(relative_paths):
        path = ROOT / relative
        result[relative] = {
            "bytes": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest().upper(),
        }
    return result


def main() -> int:
    if OUTPUT.exists():
        print("AF010S2_OFFLINE_FINAL_RESULTS.json exists; refusing to overwrite it.", file=sys.stderr)
        return 2
    before = _snapshot()
    result = {"stage": "AF-010-S2-offline-final", "protected_before": before}
    try:
        result["evaluation"] = evaluate_s2()
        result["status"] = "completed"
    except BaseException as exc:
        result["status"] = "failed"
        result["error"] = {"type": type(exc).__name__, "message": str(exc)[:1000]}
    after = _snapshot()
    result["protected_after"] = after
    result["protected_unchanged"] = before == after
    if not result["protected_unchanged"]:
        result["status"] = "integrity_mismatch"
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                      encoding="utf-8")
    evaluation = result.get("evaluation", {})
    compact = {}
    for split, report in evaluation.get("splits", {}).items():
        compact[split] = {
            name: {
                "scored_positive_query_count": metrics.get("scored_positive_query_count"),
                "recall_at": metrics.get("recall_at"), "mrr": metrics.get("mrr"),
                "no_evidence_nonempty_count": metrics.get("no_evidence_nonempty_count"),
                "no_evidence_query_count": metrics.get("no_evidence_query_count"),
            }
            for name, metrics in report["retrievers"].items()
        }
    print(json.dumps({
        "status": result["status"], "path": str(OUTPUT),
        "protected_unchanged": result["protected_unchanged"],
        "ranking_digest_sha256": evaluation.get("ranking_digest_sha256"),
        "vector_measured": evaluation.get("vector_measured"),
        "metrics": compact,
        "routing_accuracy": {name: report["routing"]["accuracy"]
                             for name, report in evaluation.get("splits", {}).items()},
    }, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
