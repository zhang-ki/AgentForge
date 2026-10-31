"""Create the frozen AF-010-S2 synthetic multi-repository fixtures once."""

from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent
FIXTURE_ROOT = ROOT / "tests" / "fixtures" / "package_s2_repos"
DATASET_PATH = ROOT / "tests" / "fixtures" / "package_s2_eval_v1.json"
SPLIT_PATH = ROOT / "tests" / "fixtures" / "package_s2_split_v1.json"
DESIGN_PATH = ROOT / "tests" / "fixtures" / "package_s2_design_v1.json"


SPECS = [
    {
        "id": "harbor_registry", "root": "harbor", "facade": "harbor.api",
        "facade_path": "harbor/api.py", "impl": "harbor.engine",
        "impl_path": "harbor/engine.py", "function": "dispatch_manifest",
        "alias": "book_manifest", "module_alias": "harbor_core", "consumer_alias": "reserve",
        "class": "CargoPlan", "class_module": "harbor.models", "class_path": "harbor/models.py",
        "version_class": "HarborVersion", "second": "signal", "consumer": "client.py",
        "business": "route a cargo manifest to a berth queue",
        "policy": "Hazardous cargo requires a signed inspection note.",
        "missing": "cold_chain_override",
    },
    {
        "id": "clinic_gateway", "root": "care", "facade": "care.public",
        "facade_path": "care/public/__init__.py", "impl": "care.triage.engine",
        "impl_path": "care/triage/engine.py", "function": "assess_patient",
        "alias": "screen_patient", "module_alias": "triage_core", "consumer_alias": "screen",
        "class": "VisitRecord", "class_module": "care.records", "class_path": "care/records.py",
        "version_class": "CareVersion", "second": "billing", "consumer": "portal.py",
        "business": "assign a patient to a triage lane",
        "policy": "Emergency visits must retain an intake reason.",
        "missing": "genome_routing",
    },
    {
        "id": "observatory_sdk", "root": "orbit", "facade": "orbit.sdk",
        "facade_path": "orbit/sdk.py", "impl": "orbit.adapters.telemetry",
        "impl_path": "orbit/adapters/telemetry.py", "function": "collect_frame",
        "alias": "sample_frame", "module_alias": "telemetry_core", "consumer_alias": "sample",
        "class": "FramePlan", "class_module": "orbit.models", "class_path": "orbit/models.py",
        "version_class": "OrbitVersion", "second": "weather", "consumer": "console.py",
        "business": "collect a telemetry frame from an instrument",
        "policy": "Calibration frames must preserve the sensor timestamp.",
        "missing": "asteroid_deflection",
    },
]


def files_for(spec: dict) -> dict[str, str]:
    root = spec["root"]
    facade_parts = spec["facade_path"].split("/")
    second = spec["second"]
    if spec["id"] == "clinic_gateway":
        relative_to_impl = "from ..triage.engine import assess_patient"
    else:
        relative_to_impl = f"from .{spec['impl'].split('.', 1)[1]} import {spec['function']}"
    root_init = (
        f'"""Public {root} package surface."""\n\n'
        f"from .{spec['facade'].split('.', 1)[1]} import {spec['function']} as {spec['alias']}\n"
        f"from .{spec['class_module'].split('.', 1)[1]} import {spec['class']}\n\n"
        f"class {spec['version_class']}:\n    version = \"s2-v1\"\n\n\n"
        f"__all__ = [\"{spec['alias']}\", \"{spec['class']}\", \"{spec['version_class']}\"]\n"
    )
    facade = (
        f'"""Facade for {spec["business"]}."""\n\n{relative_to_impl}\n\n'
        f"__all__ = [\"{spec['function']}\"]\n"
    )
    impl = (
        f"def {spec['function']}(item_id: str) -> str:\n"
        f"    \"\"\"Implement: {spec['business']}.\"\"\"\n"
        f"    return f\"{root}:{{item_id}}\"\n"
    )
    klass = f"class {spec['class']}:\n    \"\"\"Plan data for {root}.\"\"\"\n    active = True\n"
    second_impl = (
        f"def {spec['function']}(item_id: str) -> str:\n"
        f"    \"\"\"Same name, unrelated {second} behavior.\"\"\"\n"
        f"    return f\"{second}:{{item_id}}\"\n"
    )
    consumer = (
        f"import {spec['impl']} as {spec['module_alias']}\n"
        f"from {root} import {spec['alias']} as {spec['consumer_alias']}\n\n\n"
        f"def run_workflow(item_id: str) -> str:\n    return {spec['consumer_alias']}(item_id)\n"
    )
    dynamic_root = f"{root}_dynamic"
    star_root = f"{root}_stars"
    cycle_root = f"{root}_cycle"
    return {
        f"{root}/__init__.py": root_init,
        spec["facade_path"]: facade,
        spec["impl_path"]: impl,
        spec["class_path"]: klass,
        f"{second}/__init__.py": (
            f"from .engine import {spec['function']}\n\n__all__ = [\"{spec['function']}\"]\n"
        ),
        f"{second}/engine.py": second_impl,
        spec["consumer"]: consumer,
        f"{cycle_root}/__init__.py": "from .left import pivot\n\n__all__ = [\"pivot\"]\n",
        f"{cycle_root}/left.py": "from .right import pivot\n\n__all__ = [\"pivot\"]\n",
        f"{cycle_root}/right.py": "from .left import pivot\n\n__all__ = [\"pivot\"]\n",
        f"{star_root}/__init__.py": "from .items import *\n\n__all__ = [\"alpha\"]\n",
        f"{star_root}/items.py": "def alpha():\n    return \"alpha\"\n\n\ndef beta():\n    return \"beta\"\n",
        f"{dynamic_root}/__init__.py": (
            "from .items import visible\n\ndef exported_names():\n    return [\"visible\"]\n\n"
            "__all__ = exported_names()\n"
        ),
        f"{dynamic_root}/items.py": (
            "import importlib\n\ndef visible(name):\n    return importlib.import_module(name)\n"
        ),
        "docs/policy.md": (
            f"# {root.title()} policy\n\n{spec['policy']}\n"
            f"The future name `{spec['missing']}` has no implementation or export in this snapshot.\n"
        ),
    }


def line_range(files: dict[str, str], path: str, needle: str) -> tuple[int, int]:
    lines = files[path].splitlines()
    matches = [index for index, value in enumerate(lines, 1) if needle in value]
    if len(matches) != 1:
        raise RuntimeError(f"Expected one evidence line for {path}: {needle}")
    return matches[0], matches[0]


def build_queries(spec: dict, files: dict[str, str]) -> list[dict]:
    rid = spec["id"]
    root, function, alias = spec["root"], spec["function"], spec["alias"]
    second, missing = spec["second"], spec["missing"]
    dynamic_root, star_root, cycle_root = f"{root}_dynamic", f"{root}_stars", f"{root}_cycle"
    patterns = [
        # Five Calibration groups: two positives followed by two near-domain negatives.
        ("calibration", "public-surface", True, f"Which package re-exports {function} under public name {alias}?", "package_api", "reexport", f"{root}/__init__.py", f"{function} as {alias}", {"relation_type":"reexports_symbol","source_module":root,"symbol_name":alias}),
        ("calibration", "public-surface", True, f"Where does public {root}.{alias} resolve?", "package_api", "resolution", spec["impl_path"], f"def {function}", {"relation_type":"resolves_to","source_module":root,"symbol_name":alias,"resolution_status":"resolved"}),
        ("calibration", "public-surface", False, f"Does {root} export {missing}?", "package_api", "reexport", None, None, {"relation_type":"reexports_symbol","source_module":root,"symbol_name":missing}),
        ("calibration", "public-surface", False, f"Does {second} export alias {alias}?", "package_api", "reexport", None, None, {"relation_type":"reexports_symbol","source_module":second,"symbol_name":alias}),
        ("calibration", "consumer-alias", True, f"Which module imports {spec['impl']} as {spec['module_alias']}?", "dependency_relation", "alias", spec["consumer"], f"as {spec['module_alias']}", {"relation_type":"import_alias","source_module":Path(spec['consumer']).stem,"target_module":spec['impl'],"symbol_name":spec['module_alias']}),
        ("calibration", "consumer-alias", True, f"Which caller imports {root}.{alias} as {spec['consumer_alias']} and calls it?", "dependency_relation", "caller", spec["consumer"], f"as {spec['consumer_alias']}", {"relation_type":"import_alias","source_module":Path(spec['consumer']).stem,"symbol_name":spec['consumer_alias']}),
        ("calibration", "consumer-alias", False, f"Which module imports {second}.{function} as {spec['consumer_alias']}?", "dependency_relation", "alias", None, None, {"relation_type":"import_alias","target_module":second,"symbol_name":spec['consumer_alias']}),
        ("calibration", "consumer-alias", False, f"Does {root} depend on package {second}?", "dependency_relation", "dependency", None, None, {"relation_type":"imports","source_module":root,"target_module":second}),
        ("calibration", "symbols", True, f"Where is the function {function} defined?", "exact_symbol", "exact_symbol", spec["impl_path"], f"def {function}", None),
        ("calibration", "symbols", True, f"Which file defines class {spec['class']}?", "exact_symbol", "exact_symbol", spec["class_path"], f"class {spec['class']}", None),
        ("calibration", "symbols", False, f"Where is the function {missing} defined?", "exact_symbol", "exact_symbol", None, None, None),
        ("calibration", "symbols", False, f"Which file defines class Missing{spec['class']}?", "exact_symbol", "exact_symbol", None, None, None),
        ("calibration", "business", True, f"How does this repository implement {spec['business']}?", "business_implementation", "business", spec["impl_path"], spec["business"], None),
        ("calibration", "business", True, f"Which module handles the {spec['business']} workflow?", "business_implementation", "business", spec["impl_path"], spec["business"], None),
        ("calibration", "business", False, f"How does this repository implement {missing} behavior?", "business_implementation", "business", None, None, None),
        ("calibration", "business", False, f"Which module handles a quantum {missing} workflow?", "business_implementation", "business", None, None, None),
        ("calibration", "dynamic", True, f"Which package has dynamically constructed __all__ for {dynamic_root}?", "package_api", "dynamic", f"{dynamic_root}/__init__.py", "__all__ =", {"relation_type":"exports_symbol","source_module":dynamic_root,"resolution_status":"uncertain"}),
        ("calibration", "dynamic", True, "Which module performs an unknown relationship dynamic import?", "uncertain", "dynamic", f"{dynamic_root}/items.py", "import_module", None),
        ("calibration", "dynamic", False, f"Is there evidence for unknown relationship {missing} in this repository?", "uncertain", "missing", None, None, None),
        ("calibration", "dynamic", False, f"Does {dynamic_root} import {missing} as a statically resolved dependency?", "dependency_relation", "dynamic", None, None, {"relation_type":"imports","source_module":dynamic_root,"target_module":missing,"resolution_status":"resolved"}),
        # Five Validation groups, never consulted by router construction.
        ("validation", "validation-api", True, f"Which package initializer lists {alias} in __all__?", "package_api", "export", f"{root}/__init__.py", "__all__ =", {"relation_type":"reexports_symbol","source_module":root,"symbol_name":alias}),
        ("validation", "validation-api", True, f"Which file resolves public {root}.{alias} to its implementation?", "package_api", "resolution", spec["impl_path"], f"def {function}", {"relation_type":"resolves_to","source_module":root,"symbol_name":alias,"resolution_status":"resolved"}),
        ("validation", "validation-api", False, f"Does {root} publicly export {function} under its original name?", "package_api", "export", None, None, {"relation_type":"exports_symbol","source_module":root,"symbol_name":function}),
        ("validation", "validation-api", False, f"Does {root} publicly export {missing} in __all__?", "package_api", "export", None, None, {"relation_type":"exports_symbol","source_module":root,"symbol_name":missing}),
        ("validation", "validation-import", True, f"Which initializer imports {spec['class']} from {spec['class_module']}?", "dependency_relation", "import", f"{root}/__init__.py", f"import {spec['class']}", {"relation_type":"imports_symbol","source_module":root,"symbol_name":spec['class']}),
        ("validation", "validation-import", True, f"Which caller imports the public {alias} alias and calls it?", "dependency_relation", "caller", spec["consumer"], f"as {spec['consumer_alias']}", {"relation_type":"import_alias","source_module":Path(spec['consumer']).stem,"symbol_name":spec['consumer_alias']}),
        ("validation", "validation-import", False, f"Does {root} import {function} from {second}.engine?", "dependency_relation", "import", None, None, {"relation_type":"imports_symbol","source_module":root,"target_module":f"{second}.engine","symbol_name":function}),
        ("validation", "validation-import", False, f"Which caller aliases {missing} as {spec['consumer_alias']}?", "dependency_relation", "alias", None, None, {"relation_type":"import_alias","symbol_name":missing}),
        ("validation", "same-name", True, f"Where is the function {second}.{function} defined?", "exact_symbol", "same_name", f"{second}/engine.py", f"def {function}", None),
        ("validation", "same-name", True, f"Where is class {spec['version_class']} defined?", "exact_symbol", "exact_symbol", f"{root}/__init__.py", f"class {spec['version_class']}", None),
        ("validation", "same-name", False, f"Where is function {root}.{missing} defined?", "exact_symbol", "same_name", None, None, None),
        ("validation", "same-name", False, f"Does {root}.{alias} resolve to {second}.engine?", "package_api", "resolution", None, None, {"relation_type":"resolves_to","source_module":root,"target_module":f"{second}.engine","symbol_name":alias,"resolution_status":"resolved"}),
        ("validation", "docs-star", True, f"Which document states the {root} policy rule?", "documentation", "documentation", "docs/policy.md", spec["policy"], None),
        ("validation", "docs-star", True, f"Which package imports all names from {star_root}.items?", "dependency_relation", "star", f"{star_root}/__init__.py", "import *", {"relation_type":"imports_symbol","source_module":star_root,"symbol_name":"*","resolution_status":"uncertain"}),
        ("validation", "docs-star", False, f"Does documentation of {missing} make it a public export?", "documentation", "documentation", None, None, None),
        ("validation", "docs-star", False, f"Can public {star_root}.alpha be deterministically resolved through star import?", "package_api", "star", None, None, {"relation_type":"resolves_to","source_module":star_root,"symbol_name":"alpha","resolution_status":"resolved"}),
        ("validation", "cycle-missing", True, f"Which module imports pivot from {cycle_root}.right?", "dependency_relation", "cycle", f"{cycle_root}/left.py", "from .right import pivot", {"relation_type":"imports_symbol","source_module":f"{cycle_root}.left","target_module":f"{cycle_root}.right","symbol_name":"pivot"}),
        ("validation", "cycle-missing", True, f"Which document records that {missing} is absent?", "documentation", "documentation", "docs/policy.md", missing, None),
        ("validation", "cycle-missing", False, f"Does {cycle_root}.pivot resolve to a concrete definition?", "package_api", "cycle", None, None, {"relation_type":"resolves_to","source_module":cycle_root,"symbol_name":"pivot","resolution_status":"resolved"}),
        ("validation", "cycle-missing", False, f"Is there evidence for unclassified request {missing} outside the documented absence?", "uncertain", "missing", None, None, None),
    ]
    rows = []
    for number, (split, group, positive, query, expected_route, category, path, needle, package_query) in enumerate(patterns, 1):
        query_id = f"S2-{rid.upper()}-{number:03d}"
        evidence = []
        if positive:
            start, end = line_range(files, path, needle)
            evidence = [{"path": path, "start_line": start, "end_line": end,
                         "reason": "Fixture design line directly establishes the labelled evidence."}]
        symbol_query = None
        relevant_symbols = []
        if positive and expected_route == "exact_symbol":
            if spec["class"] in query:
                qname = f"{spec['class_module']}.{spec['class']}"
            elif spec["version_class"] in query:
                qname = f"{root}.{spec['version_class']}"
            elif f"{second}.{function}" in query:
                qname = f"{second}.engine.{function}"
            else:
                qname = f"{spec['impl']}.{function}"
            symbol_query = {"qualified_name": qname}
            relevant_symbols = [{"path": path, "qualified_name": qname}]
        elif not positive and expected_route == "exact_symbol":
            symbol_query = {"name": missing if "function" in query.casefold() else f"Missing{spec['class']}"}
        rows.append({
            "id": query_id, "repository_id": rid, "query": query, "category": category,
            "split": split, "leakage_group": f"{rid}:{group}",
            "expected_route": expected_route, "no_evidence": not positive,
            "relevant_paths": [] if not positive else [path],
            "relevant_symbols": relevant_symbols, "symbol_query": symbol_query,
            "package_query": package_query, "package_applicable": package_query is not None,
            "evidence": evidence,
            "inclusion_reason": "Direct fixture-design evidence." if positive else "No qualifying evidence in the frozen repository.",
            "exclusion_reason": "Names or relations outside the cited intent are excluded.",
            "scope_checked": f"Complete frozen {rid} repository snapshot.",
            "difficulty": "grounded" if positive else "near_domain",
            "label_source": "synthetic_fixture_design", "review_status": "provisional_ai",
            "review_method": "ai_evidence_review", "human_reviewed": False,
        })
    return rows


def main() -> int:
    for path in (FIXTURE_ROOT, DATASET_PATH, SPLIT_PATH, DESIGN_PATH):
        if path.exists():
            raise SystemExit(f"Refusing to overwrite frozen S2 fixture: {path}")
    all_rows = []
    designs = []
    for spec in SPECS:
        files = files_for(spec)
        repo_root = FIXTURE_ROOT / spec["id"]
        for relative, content in files.items():
            target = repo_root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8", newline="\n")
        all_rows.extend(build_queries(spec, files))
        designs.append({
            **spec, "file_count": len(files), "relationship_contract": [
                "public alias through a facade chain", "same-name symbol in another package",
                "consumer aliases and call site", "cyclic imports", "star import",
                "dynamic __all__ and dynamic import", "document-only missing capability",
            ],
        })
    dataset = {
        "dataset_version": "agentforge-package-relations-multirepo-v1",
        "review_status": "provisional_ai", "human_reviewed": False,
        "label_rule": (
            "Synthetic fixture design and cited source lines define relevance; retrieval output never "
            "defines or changes labels. No-evidence means no qualifying evidence in that repository snapshot."
        ),
        "repositories": [{"id": spec["id"], "path": spec["id"]} for spec in SPECS],
        "queries": all_rows,
    }
    split = {
        "dataset_version": dataset["dataset_version"], "status": "frozen_synthetic_fixture_design",
        "splits": {
            name: [row["id"] for row in all_rows if row["split"] == name]
            for name in ("calibration", "validation")
        },
        "group_policy": "No leakage_group may cross split or repository.",
        "expected_distribution": {
            name: {"query_count": 60, "evidence": 30, "no_evidence": 30}
            for name in ("calibration", "validation")
        },
    }
    DESIGN_PATH.write_text(json.dumps({"version":"package-s2-design-v1","repositories":designs}, indent=2) + "\n", encoding="utf-8")
    DATASET_PATH.write_text(json.dumps(dataset, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    SPLIT_PATH.write_text(json.dumps(split, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"repositories": len(SPECS), "queries": len(all_rows), "files": sum(item["file_count"] for item in designs)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
