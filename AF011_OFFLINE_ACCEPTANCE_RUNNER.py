"""Write the isolated AF-011 offline product acceptance result once."""

from __future__ import annotations

import json
import hashlib
from pathlib import Path
import shutil
import tempfile

from agentforge.product_acceptance import evaluate_product_tasks


ROOT = Path(__file__).resolve().parent
RESULT = ROOT / "AF011_OFFLINE_ACCEPTANCE_RESULTS_V4.json"
DATASET = ROOT / "tests" / "fixtures" / "af011_product_tasks_v3.json"


def main() -> int:
    if RESULT.exists():
        print(f"{RESULT.name} exists; refusing to overwrite it.")
        return 2
    # Evaluate the real product sources without indexing the task labels or the
    # test suite that contains deliberate secret-like safety fixtures.
    temp_parent = ROOT / "tests" / ".tmp"
    parent_existed = temp_parent.exists()
    temp_parent.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=temp_parent, prefix="af011-source-") as temp:
        snapshot = Path(temp) / "repository"
        shutil.copytree(
            ROOT / "agentforge", snapshot / "agentforge",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        for name in ("README.md", "ARCHITECTURE.md", "SECURITY.md"):
            shutil.copy2(ROOT / name, snapshot / name)
        manifest = []
        for path in sorted(item for item in snapshot.rglob("*") if item.is_file()):
            payload = path.read_bytes()
            manifest.append({
                "path": path.relative_to(snapshot).as_posix(),
                "size_bytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            })
        manifest_bytes = json.dumps(
            manifest, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
        result = evaluate_product_tasks(snapshot, DATASET)
        dataset_payload = DATASET.read_bytes()
        result["dataset_file"] = DATASET.relative_to(ROOT).as_posix()
        result["dataset_size_bytes"] = len(dataset_payload)
        result["dataset_sha256"] = hashlib.sha256(dataset_payload).hexdigest()
        result["source_repository"] = str(ROOT)
        result["evaluation_snapshot"] = "temporary_copy_of_agentforge_package_and_product_docs"
        result["evaluation_snapshot_file_count"] = len(manifest)
        result["evaluation_snapshot_manifest_sha256"] = hashlib.sha256(manifest_bytes).hexdigest()
        result["evaluation_snapshot_removed"] = True
    if not parent_existed:
        try:
            temp_parent.rmdir()
        except OSError:
            pass
    RESULT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "status": "completed", "result": str(RESULT),
        "task_count": result["task_count"], "splits": result["splits"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
