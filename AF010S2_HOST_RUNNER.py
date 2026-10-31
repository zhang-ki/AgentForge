"""One-shot, loopback-only real Vector evaluation for AF-010-S2."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid

import AF009GR3_HOST_RUNNER as host


ROOT = Path(__file__).resolve().parent
RESULT = ROOT / "AF010S2_RESULTS.json"
TEMP_ROOT = ROOT / "tests" / ".tmp"
API_MODEL = "qwen3-embedding-0.6b-q8_0-af010s2"
MODEL_ARGS = ("--ctx-size", "4096", "--n-gpu-layers", "0", "--embedding",
              "--pooling", "last", "--embd-normalize", "2", "--no-webui")
ADDITIONAL_PROTECTED = (
    "AF010S1_RESULTS.json", "AF010S1_EVALUATION.md", "AF010PRE_RESULTS.json",
    "AF010PRE_EVALUATION.md", "AF010S2_OFFLINE_RESULTS.json",
    "tests/fixtures/package_relation_eval.json", "tests/fixtures/package_relation_split.json",
    "tests/fixtures/package_s2_eval_v1.json", "tests/fixtures/package_s2_split_v1.json",
    "tests/fixtures/package_s2_design_v1.json", ".agentforge/data/index.sqlite3",
    ".agentforge/data/read_only_evidence.json",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def _record(path: Path) -> dict:
    return {"bytes": path.stat().st_size, "sha256": _sha256(path)}


def _protected_snapshot() -> dict:
    paths = set(host.PROTECTED_PATHS) | set(ADDITIONAL_PROTECTED)
    for pattern in ("retrieval_eval*", "package_relation*"):
        for path in (ROOT / "tests" / "fixtures").glob(pattern):
            if path.is_file():
                paths.add(path.relative_to(ROOT).as_posix())
            elif path.is_dir():
                paths.update(item.relative_to(ROOT).as_posix()
                             for item in path.rglob("*") if item.is_file())
    paths.update(item.relative_to(ROOT).as_posix()
                 for item in (ROOT / "tests" / "fixtures" / "package_s2_repos").rglob("*")
                 if item.is_file())
    return {relative: _record(ROOT / relative) for relative in sorted(paths)}


def _existing_llama_servers() -> list[str]:
    completed = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq llama-server.exe", "/FO", "CSV", "/NH"],
        check=False, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=10.0,
    )
    if completed.returncode != 0:
        raise RuntimeError("Could not confirm that no llama-server process is already running.")
    rows = []
    for row in csv.reader(io.StringIO(completed.stdout)):
        if row and row[0].casefold() == "llama-server.exe":
            rows.append(row[1] if len(row) > 1 else "pid-unavailable")
    return rows


def _owned_listeners(pid: int) -> list[dict]:
    command = (
        "$ErrorActionPreference='Stop'; "
        f"$rows=Get-NetTCPConnection -OwningProcess {int(pid)} -State Listen | "
        "Select-Object LocalAddress,LocalPort,OwningProcess; "
        "if ($null -eq $rows) { '[]' } else { $rows | ConvertTo-Json -Compress }"
    )
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
        check=False, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=10.0,
    )
    if completed.returncode != 0:
        raise RuntimeError("Could not verify the owned process listener addresses.")
    value = json.loads(completed.stdout.strip() or "[]")
    return value if isinstance(value, list) else [value]


def _transition(result: dict, state: str) -> None:
    host._transition(result, state)


def main() -> int:
    if RESULT.exists():
        print("AF010S2_RESULTS.json exists; refusing to overwrite it.", file=sys.stderr)
        return 2
    result = {
        "stage": "AF-010-S2-real-vector-evaluation", "status": "started",
        "state": None, "state_history": [],
        "execution_environment": {
            "python": sys.executable, "python_version": sys.version,
            "platform": sys.platform,
            "network_disabled_marker": os.environ.get("CODEX_SANDBOX_NETWORK_DISABLED"),
            "marker_was_not_modified": True,
        },
        "protected_before": _protected_snapshot(), "assets_before": {},
        "runtime_copy": {}, "service": {"started": False, "embedding_only": True},
        "chat_or_generation_requests_sent": False,
        "production_configuration_changed": False,
    }
    process = None
    temp_runtime = None
    source_manifest = []
    stdout_buffer = bytearray()
    stderr_buffer = bytearray()
    output_counts = {}
    reader_threads = []
    exit_status = 1
    try:
        if sys.platform != "win32":
            raise RuntimeError("The pinned Windows llama-server runtime requires the Windows host.")
        existing = _existing_llama_servers()
        result["preexisting_llama_server_pids"] = existing
        if existing:
            raise RuntimeError("An existing llama-server was found; this runner will not touch it.")

        for path, expected in host.EXPECTED_HASHES.items():
            if not path.is_file():
                raise FileNotFoundError(f"Required asset is missing: {path}")
            record = _record(path)
            record["expected_sha256"] = expected
            record["matches"] = record["sha256"] == expected
            result["assets_before"][str(path)] = record
            if not record["matches"]:
                raise RuntimeError(f"Required asset hash does not match: {path.name}")

        source_manifest = host._runtime_manifest(host.RUNTIME_PATH)
        result["runtime_copy"]["source_file_count"] = len(source_manifest)
        result["runtime_copy"]["source_manifest_sha256"] = hashlib.sha256(
            json.dumps(source_manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest().upper()
        if len(source_manifest) != 51:
            raise RuntimeError("Pinned llama-b11424 runtime no longer contains 51 files.")

        result["loopback_preflight"] = host._loopback_preflight()
        if result["loopback_preflight"]["status"] != "passed":
            raise RuntimeError("Loopback preflight failed; model startup is prohibited.")

        TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        temp_runtime = TEMP_ROOT / f"af010s2-runtime-{uuid.uuid4().hex}"
        shutil.copytree(host.RUNTIME_PATH, temp_runtime, copy_function=shutil.copy2)
        copied_manifest = host._runtime_manifest(temp_runtime)
        result["runtime_copy"].update({
            "path": str(temp_runtime), "copied_file_count": len(copied_manifest),
            "copy_matches_source": copied_manifest == source_manifest,
        })
        if copied_manifest != source_manifest:
            raise RuntimeError("Temporary runtime copy does not match the pinned source manifest.")

        port = host._reserve_ephemeral_port()
        result["service"].update({"host": "127.0.0.1", "port": port})
        executable = temp_runtime / "llama-server.exe"
        arguments = [
            str(executable), "--model", str(host.MODEL_PATH), "--alias", API_MODEL,
            "--host", "127.0.0.1", "--port", str(port), *MODEL_ARGS,
            "--cors-origins", f"http://127.0.0.1:{port}", "--no-cors-credentials",
        ]
        child_environment = os.environ.copy()
        child_environment["TEMP"] = str(temp_runtime)
        child_environment["TMP"] = str(temp_runtime)
        started = time.perf_counter()
        process = subprocess.Popen(
            arguments, cwd=temp_runtime, env=child_environment,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            shell=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        result["service"].update({
            "started": True, "pid": process.pid,
            "executable": str(executable.resolve()), "arguments": arguments[1:],
            "working_directory": str(temp_runtime),
        })
        _transition(result, "PROCESS_STARTED")
        for name, pipe, target in (
            ("stdout_bytes", process.stdout, stdout_buffer),
            ("stderr_bytes", process.stderr, stderr_buffer),
        ):
            thread = threading.Thread(
                target=host._drain, args=(pipe, target, output_counts, name),
                name=f"af010s2-{name}", daemon=True,
            )
            thread.start()
            reader_threads.append(thread)

        result["service"]["tcp_readiness"] = host._wait_for_tcp(process, port)
        _transition(result, "TCP_LISTENING")
        listeners = _owned_listeners(process.pid)
        result["service"]["listeners"] = listeners
        if not listeners or any(
            str(item.get("LocalAddress", "")).strip() != "127.0.0.1"
            or int(item.get("LocalPort", -1)) != port
            for item in listeners
        ):
            raise RuntimeError("Owned llama-server listener is absent or not restricted to 127.0.0.1.")

        _transition(result, "MODEL_LOADING")
        result["service"]["model_readiness"] = host._wait_for_model_ready(
            process, "127.0.0.1", port, total_timeout=120.0,
            request_timeout=2.0, poll_interval=1.0,
        )
        _transition(result, "MODEL_READY")

        from agentforge.package_s2 import evaluate_s2

        _transition(result, "EVALUATING")
        evaluation_started = time.perf_counter()
        result["evaluation"] = host._evaluate_after_model_ready(
            result["service"]["model_readiness"],
            lambda: evaluate_s2(
                base_url=f"http://127.0.0.1:{port}", model_id=host.MODEL_ID,
                api_model=API_MODEL, dimension=1024,
            ),
        )
        result["evaluation_elapsed_seconds"] = round(time.perf_counter() - evaluation_started, 6)
        result["service"]["resource_sample"] = host._process_memory_record(process)
        result["status"] = "completed"
        _transition(result, "COMPLETED")
        exit_status = 0
    except BaseException as exc:
        if result["status"] == "started":
            result["status"] = "failed"
        result["error"] = host._error_record(exc)
        if isinstance(exc, host.ModelReadinessError):
            result["error"].update({
                "code": exc.code, "observations": exc.observations,
                "process_exit_code": exc.exit_code,
            })
        _transition(result, "FAILED")
    finally:
        if process is not None:
            result["service"].setdefault("resource_sample", host._process_memory_record(process))
            result["service"].update(host._stop_owned_process(process))
            for thread in reader_threads:
                thread.join(timeout=5.0)
            result["service"]["output_bytes"] = output_counts
            result["service"]["stdout_summary"] = host._safe_log_summary(bytes(stdout_buffer))
            result["service"]["stderr_summary"] = host._safe_log_summary(bytes(stderr_buffer))
            result["service"]["port_released"] = host._port_released(
                result["service"].get("port", -1)
            )
        if temp_runtime is not None and temp_runtime.exists():
            post_manifest = host._runtime_manifest(temp_runtime)
            original_paths = {entry["path"] for entry in source_manifest}
            result["runtime_copy"].update({
                "post_run_file_count": len(post_manifest),
                "post_run_matches_source": post_manifest == source_manifest,
                "extra_files": [entry["path"] for entry in post_manifest
                                if entry["path"] not in original_paths],
            })
            shutil.rmtree(temp_runtime)
            result["runtime_copy"]["removed"] = not temp_runtime.exists()
        result["assets_after"] = {
            str(path): _record(path) for path in host.EXPECTED_HASHES
            if path.is_file()
        }
        result["protected_after"] = _protected_snapshot()
        result["protected_unchanged"] = result["protected_before"] == result["protected_after"]
        RESULT.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                          encoding="utf-8")
        print(json.dumps({
            "status": result["status"], "state": result["state"],
            "result_path": str(RESULT), "loopback": result.get("loopback_preflight", {}).get("status"),
            "service_started": result["service"].get("started"),
            "service_exit_code": result["service"].get("exit_code"),
            "port_released": result["service"].get("port_released"),
            "protected_unchanged": result.get("protected_unchanged"),
            "embedding_request_counts": result.get("evaluation", {}).get("request_counts"),
        }, ensure_ascii=False, indent=2))
    return exit_status


if __name__ == "__main__":
    raise SystemExit(main())
