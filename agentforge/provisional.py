"""加载不可变的 AF-009 AI 暂定数据集，不覆盖其来源文件。"""

import copy
import hashlib
import json
from pathlib import Path

from .evaluation import PROVISIONAL_DATASET_VERSION, validate_split_plan


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MANIFEST = PROJECT_ROOT / "tests" / "fixtures" / "retrieval_eval_provisional_v3.json"


def _read_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError(f"Provisional input must be a JSON object: {path.name}.")
    return value


def _source_path(value: str) -> Path:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError("Provisional source paths must be fixed project-relative POSIX paths.")
    candidate = (PROJECT_ROOT / value).resolve()
    try:
        candidate.relative_to(PROJECT_ROOT.resolve())
    except ValueError:
        raise ValueError("Provisional source path escapes the project root.") from None
    if not candidate.is_file():
        raise ValueError(f"Provisional source is missing: {value}.")
    return candidate


def _verified_source(manifest: dict, path_field: str, hash_field: str) -> tuple[Path, dict]:
    path = _source_path(manifest.get(path_field))
    expected = manifest.get(hash_field)
    actual = hashlib.sha256(path.read_bytes()).hexdigest().upper()
    if not isinstance(expected, str) or actual != expected.upper():
        raise ValueError(f"Frozen provisional source hash mismatch: {path.name}.")
    return path, _read_json(path)


def load_provisional_dataset(manifest_path: Path = DEFAULT_MANIFEST) -> dict:
    """在内存中将 v3 清单解析为标准评测数据集。"""
    manifest = _read_json(Path(manifest_path))
    if manifest.get("dataset_version") != PROVISIONAL_DATASET_VERSION:
        raise ValueError("Unsupported provisional dataset manifest version.")
    if manifest.get("status") != "provisional_ai" or manifest.get("human_reviewed") is not False:
        raise ValueError("Provisional dataset must remain provisional_ai and not human reviewed.")
    if manifest.get("review_method") != "ai_evidence_review":
        raise ValueError("Provisional dataset must record ai_evidence_review.")

    _, pool = _verified_source(manifest, "source_pool", "source_pool_sha256")
    _, adjudication = _verified_source(
        manifest, "source_adjudication", "source_adjudication_sha256"
    )
    _, split_plan = _verified_source(manifest, "source_split_plan", "source_split_plan_sha256")
    validate_split_plan(pool, split_plan)
    if adjudication.get("human_reviewed") is not False:
        raise ValueError("AI adjudication cannot claim human review.")

    pool_by_id = {item["id"]: item for item in pool["queries"]}
    adjudicated_by_id = {item["id"]: item for item in adjudication.get("records", [])}
    included = manifest.get("included_ids")
    if (not isinstance(included, list) or len(included) != len(set(included))
            or set(included) != set(pool_by_id) or set(included) != set(adjudicated_by_id)):
        raise ValueError("Provisional manifest must include every adjudicated candidate exactly once.")

    split_by_id = {}
    for split_name, ids in split_plan["splits"].items():
        for query_id in ids:
            split_by_id[query_id] = split_name
    rewrite_ids = set(manifest.get("rewrite_ids", []))
    actual_rewrites = {
        item["id"] for item in adjudication["records"] if item["decision"] == "rewrite_query"
    }
    if rewrite_ids != actual_rewrites:
        raise ValueError("Provisional rewrite IDs do not match the AI adjudication.")

    queries = []
    for query_id in included:
        original = pool_by_id[query_id]
        reviewed = adjudicated_by_id[query_id]
        if reviewed.get("review_method") != "ai_evidence_review" or reviewed.get("human_reviewed") is not False:
            raise ValueError(f"{query_id}: adjudication must remain AI-only.")
        decision = reviewed.get("decision")
        if decision not in {"accept_current", "no_evidence", "rewrite_query"}:
            raise ValueError(f"{query_id}: unresolved AI decision is excluded from provisional metrics.")
        label = reviewed["suggested_label"]
        no_evidence = label["kind"] == "no_evidence"
        query = reviewed.get("suggested_query") if decision == "rewrite_query" else original["query"]
        if not isinstance(query, str) or not query.strip():
            raise ValueError(f"{query_id}: provisional query is missing.")

        original_symbol_paths = {
            item["qualified_name"]: item["path"] for item in original.get("relevant_symbols", [])
        }
        relevant_symbols = []
        for qualified_name in label["relevant_symbols"]:
            path = original_symbol_paths.get(qualified_name)
            if path is None:
                raise ValueError(f"{query_id}: suggested symbol is not traceable to the source pool.")
            relevant_symbols.append({"path": path, "qualified_name": qualified_name})

        item = {
            "id": query_id,
            "category": original["category"],
            "leakage_group": original["leakage_group"],
            "split": split_by_id[query_id],
            "query": query,
            "original_query": original["query"],
            "relevant_paths": copy.deepcopy(label["relevant_paths"]),
            "relevant_symbols": relevant_symbols,
            "evidence": [
                {
                    "path": evidence["path"],
                    "start_line": evidence["start_line"],
                    "end_line": evidence["end_line"],
                    "reason": "Provisional AI evidence: " + evidence["excerpt"],
                }
                for evidence in reviewed["suggested_evidence"]
            ],
            "no_evidence": no_evidence,
            "review_status": "provisional_ai",
            "review_method": "ai_evidence_review",
            "human_reviewed": False,
            "ai_decision": decision,
            "confidence": reviewed["confidence"],
            "scope_checked": original.get("scope_checked", ""),
            "inclusion_reason": original["inclusion_reason"],
            "exclusion_reason": original["exclusion_reason"],
        }
        if original.get("symbol_query") is not None:
            item["symbol_query"] = copy.deepcopy(original["symbol_query"])
        if original.get("difficulty") is not None:
            item["difficulty"] = original["difficulty"]
        queries.append(item)

    return {
        "dataset_version": PROVISIONAL_DATASET_VERSION,
        "review_status": "provisional_ai",
        "human_reviewed": False,
        "label_rule": manifest["label_rule"],
        "corpus": pool["corpus"],
        "source_manifest": Path(manifest_path).name,
        "queries": queries,
    }


def dataset_split(dataset: dict, split_name: str) -> dict:
    if split_name not in {"calibration", "validation"}:
        raise ValueError("split_name must be calibration or validation.")
    subset = copy.deepcopy(dataset)
    subset["dataset_version"] = PROVISIONAL_DATASET_VERSION
    subset["queries"] = [item for item in dataset["queries"] if item.get("split") == split_name]
    if not subset["queries"]:
        raise ValueError(f"Provisional {split_name} split is empty.")
    subset["evaluation_split"] = split_name
    return subset
