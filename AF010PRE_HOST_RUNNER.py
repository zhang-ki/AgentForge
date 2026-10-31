"""One-shot AF-010-PRE host runner; not part of the product CLI."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
import uuid

import AF009GR3_HOST_RUNNER as host


PROJECT_ROOT = Path(__file__).resolve().parent
RESULT_PATH = PROJECT_ROOT / "AF010PRE_RESULTS.json"
TEMP_ROOT = PROJECT_ROOT / "tests" / ".tmp"
API_MODEL = "qwen3-embedding-0.6b-q8_0-af010pre"
PROTECTED_PATHS = tuple(dict.fromkeys(host.PROTECTED_PATHS + (
    "AF009GR5_RESULTS.json",
    "AF009GR5_EVALUATION.md",
    "tests/fixtures/retrieval_eval_preproduct_v1.json",
    "tests/fixtures/retrieval_eval_preproduct_split_v1.json",
)))


def _protected_snapshot() -> dict:
    paths = list(PROTECTED_PATHS)
    corpus = PROJECT_ROOT / "tests" / "fixtures" / "retrieval_eval_preproduct_repo"
    paths.extend(
        path.relative_to(PROJECT_ROOT).as_posix()
        for path in sorted(corpus.rglob("*")) if path.is_file()
    )
    return {
        relative: host._file_record(PROJECT_ROOT / relative)
        for relative in paths
    }


def _transition(result: dict, state: str) -> None:
    host._transition(result, state)


def main() -> int:
    if RESULT_PATH.exists():
        print("AF010PRE_RESULTS.json already exists; refusing to overwrite it.", file=sys.stderr)
        return 2

    result = {
        "stage": "AF-010-PRE",
        "status": "started",
        "state": None,
        "state_history": [],
        "execution_environment": {
            "python": sys.executable,
            "python_version": sys.version,
            "platform": sys.platform,
            "network_disabled_marker": os.environ.get("CODEX_SANDBOX_NETWORK_DISABLED"),
            "marker_was_not_modified": True,
        },
        "protected_before": _protected_snapshot(),
        "assets": {},
        "runtime_copy": {},
        "service": {"started": False, "embedding_only": True},
    }
    process = None
    temp_runtime = None
    source_manifest = []
    stdout_buffer = bytearray()
    stderr_buffer = bytearray()
    output_counts = {}
    reader_threads = []
    exit_code = 1

    try:
        for path, expected in host.EXPECTED_HASHES.items():
            if not path.is_file():
                raise FileNotFoundError(f"Required asset is missing: {path}")
            actual = host._sha256(path)
            result["assets"][str(path)] = {
                "bytes": path.stat().st_size,
                "sha256": actual,
                "expected_sha256": expected,
                "matches": actual == expected,
            }
            if actual != expected:
                raise RuntimeError(f"Required asset hash does not match: {path.name}")

        source_manifest = host._runtime_manifest(host.RUNTIME_PATH)
        result["runtime_copy"]["source_file_count"] = len(source_manifest)
        result["runtime_copy"]["source_manifest_sha256"] = hashlib.sha256(
            json.dumps(source_manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest().upper()
        if len(source_manifest) != 51:
            raise RuntimeError("Pinned runtime no longer contains the expected 51 files.")

        result["loopback_preflight"] = host._loopback_preflight()
        if result["loopback_preflight"]["status"] != "passed":
            result["status"] = "blocked_by_loopback_preflight"
            raise RuntimeError("Host loopback preflight failed; model startup is prohibited.")

        port = host._reserve_ephemeral_port()
        result["service"].update({"host": "127.0.0.1", "port": port})
        temp_runtime = TEMP_ROOT / f"af010pre-runtime-{uuid.uuid4().hex}"
        temp_runtime.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(host.RUNTIME_PATH, temp_runtime, copy_function=shutil.copy2)
        copied_manifest = host._runtime_manifest(temp_runtime)
        result["runtime_copy"].update({
            "path": str(temp_runtime),
            "copied_file_count": len(copied_manifest),
            "copy_matches_source": copied_manifest == source_manifest,
        })
        if copied_manifest != source_manifest:
            raise RuntimeError("Temporary runtime copy does not match the pinned source manifest.")

        copied_exe = temp_runtime / "llama-server.exe"
        arguments = [
            str(copied_exe),
            "--model", str(host.MODEL_PATH),
            "--alias", API_MODEL,
            "--host", "127.0.0.1",
            "--port", str(port),
            "--ctx-size", "4096",
            "--n-gpu-layers", "0",
            "--embedding",
            "--pooling", "last",
            "--embd-normalize", "2",
            "--no-webui",
            "--cors-origins", f"http://127.0.0.1:{port}",
            "--no-cors-credentials",
        ]
        child_environment = os.environ.copy()
        child_environment["TEMP"] = str(temp_runtime)
        child_environment["TMP"] = str(temp_runtime)
        started = time.perf_counter()
        process = subprocess.Popen(
            arguments,
            cwd=temp_runtime,
            env=child_environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        result["service"].update({
            "started": True,
            "pid": process.pid,
            "executable": str(copied_exe.resolve()),
            "arguments": arguments[1:],
            "working_directory": str(temp_runtime),
        })
        _transition(result, "PROCESS_STARTED")
        for name, pipe, target in (
            ("stdout_bytes", process.stdout, stdout_buffer),
            ("stderr_bytes", process.stderr, stderr_buffer),
        ):
            thread = threading.Thread(
                target=host._drain,
                args=(pipe, target, output_counts, name),
                name=f"af010pre-{name}",
                daemon=True,
            )
            thread.start()
            reader_threads.append(thread)

        result["service"]["tcp_readiness"] = host._wait_for_tcp(process, port)
        _transition(result, "TCP_LISTENING")
        result["service"]["startup_seconds"] = round(time.perf_counter() - started, 6)
        _transition(result, "MODEL_LOADING")
        result["service"]["model_readiness"] = host._wait_for_model_ready(
            process,
            "127.0.0.1",
            port,
            total_timeout=120.0,
            request_timeout=2.0,
            poll_interval=1.0,
        )
        _transition(result, "MODEL_READY")

        from agentforge.preproduct import evaluate_preproduct

        _transition(result, "EVALUATING")
        evaluation_started = time.perf_counter()
        result["evaluation"] = host._evaluate_after_model_ready(
            result["service"]["model_readiness"],
            lambda: evaluate_preproduct(
                base_url=f"http://127.0.0.1:{port}",
                model_id=host.MODEL_ID,
                api_model=API_MODEL,
                dimension=1024,
            ),
        )
        result["evaluation_elapsed_seconds"] = round(
            time.perf_counter() - evaluation_started, 6
        )
        result["service"]["resource_sample"] = host._process_memory_record(process)
        result["status"] = "completed"
        _transition(result, "COMPLETED")
        exit_code = 0
    except BaseException as exc:
        if result["status"] == "started":
            result["status"] = "failed"
        result["error"] = host._error_record(exc)
        if isinstance(exc, host.ModelReadinessError):
            result["error"].update({
                "code": exc.code,
                "observations": exc.observations,
                "process_exit_code": exc.exit_code,
            })
        _transition(result, "FAILED")
    finally:
        if process is not None:
            if "resource_sample" not in result["service"]:
                result["service"]["resource_sample"] = host._process_memory_record(process)
            result["service"].update(host._stop_owned_process(process))
            for thread in reader_threads:
                thread.join(timeout=5.0)
            result["service"]["output_bytes"] = output_counts
            result["service"]["stdout_summary"] = host._safe_log_summary(bytes(stdout_buffer))
            result["service"]["stderr_summary"] = host._safe_log_summary(bytes(stderr_buffer))
            result["service"]["port_released"] = host._port_released(result["service"]["port"])

        if temp_runtime is not None and temp_runtime.exists():
            post_manifest = host._runtime_manifest(temp_runtime)
            result["runtime_copy"].update({
                "post_run_file_count": len(post_manifest),
                "post_run_matches_source": post_manifest == source_manifest,
                "extra_files": [
                    entry["path"] for entry in post_manifest
                    if entry["path"] not in {source["path"] for source in source_manifest}
                ],
            })
            shutil.rmtree(temp_runtime)
            result["runtime_copy"]["removed"] = not temp_runtime.exists()

        result["assets_after"] = {
            str(path): host._file_record(path) for path in host.EXPECTED_HASHES
        }
        result["protected_after"] = _protected_snapshot()
        result["protected_unchanged"] = result["protected_before"] == result["protected_after"]
        result["agentexpr_accessed"] = False
        result["chat_or_generation_requests_sent"] = False
        result["production_threshold_enabled"] = False
        result["default_retriever_modified"] = False
        RESULT_PATH.write_text(
            json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(json.dumps({
            "status": result["status"],
            "state": result["state"],
            "result_path": str(RESULT_PATH),
            "loopback": result.get("loopback_preflight", {}).get("status"),
            "service_started": result["service"].get("started"),
            "service_exit_code": result["service"].get("exit_code"),
            "port_released": result["service"].get("port_released"),
            "protected_unchanged": result.get("protected_unchanged"),
            "embedding_request_counts": result.get("evaluation", {}).get(
                "vector_measurement", {}
            ).get("request_counts"),
            "product_entry_decision": result.get("evaluation", {}).get(
                "product_entry_decision", {}
            ).get("decision"),
        }, ensure_ascii=False, indent=2))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
