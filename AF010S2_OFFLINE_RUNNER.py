"""Reproducible offline and scale-only AF-010-S2 runner."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys

from agentforge.package_s2 import evaluate_s2, run_scale_benchmarks


ROOT = Path(__file__).resolve().parent
RESULT = ROOT / "AF010S2_OFFLINE_RESULTS.json"
TEMP = ROOT / "tests" / ".tmp"
FIXTURE_ROOT = ROOT / "tests" / "fixtures" / "package_s2_repos"
PROTECTED = (
    "AF010S1_RESULTS.json", "AF010S1_EVALUATION.md", "AF010PRE_RESULTS.json",
    "AF010PRE_EVALUATION.md", "AF009GR5_RESULTS.json", "AF009GR5_EVALUATION.md",
    "tests/fixtures/package_relation_eval.json",
    "tests/fixtures/package_relation_split.json",
    "tests/fixtures/package_s2_eval_v1.json",
    "tests/fixtures/package_s2_split_v1.json",
    "tests/fixtures/package_s2_design_v1.json",
    ".agentforge/data/index.sqlite3", ".agentforge/data/read_only_evidence.json",
)


def _hash(path: Path) -> dict:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return {"bytes": path.stat().st_size, "sha256": digest.hexdigest().upper()}


def _snapshot() -> dict:
    paths = {relative: ROOT / relative for relative in PROTECTED}
    paths.update({
        path.relative_to(ROOT).as_posix(): path
        for path in FIXTURE_ROOT.rglob("*") if path.is_file()
    })
    return {relative: _hash(path) for relative, path in sorted(paths.items())}


def main() -> int:
    if RESULT.exists():
        print("AF010S2_OFFLINE_RESULTS.json exists; refusing to overwrite it.", file=sys.stderr)
        return 2
    TEMP.mkdir(parents=True, exist_ok=True)
    before = _snapshot()
    result = {
        "stage": "AF-010-S2-offline-preflight",
        "status": "started",
        "protected_before": before,
        "vector_measured": False,
        "production_configuration_changed": False,
    }
    try:
        result["evaluation"] = evaluate_s2()
        result["scale"] = run_scale_benchmarks(TEMP)
        after = _snapshot()
        result["protected_after"] = after
        result["protected_unchanged"] = before == after
        result["status"] = "completed" if before == after else "integrity_mismatch"
    except BaseException as exc:
        result["status"] = "failed"
        result["error"] = {"type": type(exc).__name__, "message": str(exc)[:1000]}
        result["protected_after"] = _snapshot()
        result["protected_unchanged"] = before == result["protected_after"]
    RESULT.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                      encoding="utf-8")
    print(json.dumps({
        "status": result["status"], "result": str(RESULT),
        "protected_unchanged": result.get("protected_unchanged"),
        "calibration_router_accuracy": result.get("evaluation", {}).get("splits", {})
            .get("calibration", {}).get("routing", {}).get("accuracy"),
        "validation_router_accuracy": result.get("evaluation", {}).get("splits", {})
            .get("validation", {}).get("routing", {}).get("accuracy"),
        "scale": result.get("scale", {}).get("scenarios"),
    }, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
