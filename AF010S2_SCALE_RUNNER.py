"""Run the corrected AF-010-S2 module-count and update-cost experiment once."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys

from agentforge.package_s2 import run_scale_benchmarks


ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "AF010S2_SCALE_RESULTS.json"
TEMP = ROOT / "tests" / ".tmp"
PROTECTED = (
    "AF010S1_RESULTS.json", "AF010S1_EVALUATION.md", "AF010PRE_RESULTS.json",
    "AF010PRE_EVALUATION.md", "AF010S2_OFFLINE_RESULTS.json",
    "tests/fixtures/package_relation_eval.json", "tests/fixtures/package_relation_split.json",
    "tests/fixtures/package_s2_eval_v1.json", "tests/fixtures/package_s2_split_v1.json",
    "tests/fixtures/package_s2_design_v1.json", ".agentforge/data/index.sqlite3",
    ".agentforge/data/read_only_evidence.json",
)


def snapshot() -> dict:
    result = {}
    for relative in PROTECTED:
        path = ROOT / relative
        digest = hashlib.sha256(path.read_bytes()).hexdigest().upper()
        result[relative] = {"bytes": path.stat().st_size, "sha256": digest}
    return result


def main() -> int:
    if OUTPUT.exists():
        print("AF010S2_SCALE_RESULTS.json exists; refusing to overwrite it.", file=sys.stderr)
        return 2
    TEMP.mkdir(parents=True, exist_ok=True)
    before = snapshot()
    result = {"stage": "AF-010-S2-scale", "protected_before": before}
    try:
        result["scale"] = run_scale_benchmarks(TEMP)
        result["status"] = "completed"
    except BaseException as exc:
        result["status"] = "failed"
        result["error"] = {"type": type(exc).__name__, "message": str(exc)[:1000]}
    after = snapshot()
    result["protected_after"] = after
    result["protected_unchanged"] = before == after
    result["status"] = "completed" if result["status"] == "completed" and before == after else "failed"
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                      encoding="utf-8")
    print(json.dumps({
        "status": result["status"], "protected_unchanged": result["protected_unchanged"],
        "scenarios": [{
            "modules": item["requested_modules"], "status": item["status"],
            "files": item["physical_python_files"],
            "full_build_seconds": item["full_build_seconds"],
            "relations": item["relation_count"],
            "peak_python_allocation_bytes": item["peak_python_allocation_bytes"],
            "incremental": item["incremental"],
        } for item in result.get("scale", {}).get("scenarios", [])],
    }, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
