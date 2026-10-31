"""One-shot AF-009G-R5 host evaluation runner.

This script is intentionally separate from AgentForge's product CLI.  It performs
one loopback preflight, starts only the pinned local embedding runtime, evaluates
the frozen AF-009G corpus, and always cleans up the runtime copy and child process.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
import uuid


PROJECT_ROOT = Path(__file__).resolve().parent
RESULT_PATH = PROJECT_ROOT / "AF009GR5_RESULTS.json"
TEMP_ROOT = PROJECT_ROOT / "tests" / ".tmp"
MODEL_ROOT = Path(os.environ.get("AGENTFORGE_MODEL_ROOT", PROJECT_ROOT.parent / "models")).resolve()
MODEL_PATH = MODEL_ROOT / "qwen3-embedding-0.6b-gguf" / "Qwen3-Embedding-0.6B-Q8_0.gguf"
RUNTIME_PATH = MODEL_ROOT / "qwen3.5-4b" / ".runtime" / "llama-b11424"
EXPECTED_HASHES = {
    MODEL_PATH: "06507C7B42688469C4E7298B0A1E16DEFF06CAF291CF0A5B278C308249C3E439",
    RUNTIME_PATH / "llama-server.exe": "F8DF891A5E4D10FA8CFC0F86C8D99617A06EDD18D046E18B59EC3354F17FC12D",
    RUNTIME_PATH / "llama-server-impl.dll": "F020D94DF5E91840F2C9626B86F9AFE414C16D17ED5E1F41FE9CB614D3CDCDCB",
}
MODEL_ID = (
    "Qwen/Qwen3-Embedding-0.6B-GGUF@"
    "370f27d7550e0def9b39c1f16d3fbaa13aa67728:Q8_0#"
    "06507C7B42688469C4E7298B0A1E16DEFF06CAF291CF0A5B278C308249C3E439"
)
API_MODEL = "qwen3-embedding-0.6b-q8_0-af009gr5"
HEALTH_RESPONSE_LIMIT = 64 * 1024

PROTECTED_PATHS = (
    "tests/fixtures/retrieval_eval.json",
    "tests/fixtures/retrieval_eval_expansion_candidates.json",
    "tests/fixtures/retrieval_eval_review_pool_v2.json",
    "tests/fixtures/retrieval_eval_split_plan_v2.json",
    "tests/fixtures/retrieval_eval_review_pool_v2_ai_adjudicated.json",
    "tests/fixtures/retrieval_eval_provisional_v3.json",
    "tests/fixtures/retrieval_eval_generalization_v1.json",
    "tests/fixtures/retrieval_eval_generalization_split_v1.json",
    "AF009FA_RRF_RESULTS.json",
    "AF009G_GENERALIZATION_RESULTS.json",
    "AF009GR_DIAGNOSTIC_RESULTS.json",
    "AF009GR_DIAGNOSTIC.md",
    "AF009GR2_DECISION.md",
    "AF009GR3_RESULTS.json",
    "AF009GR3_EVALUATION.md",
    "AF009GR4_RESULTS.json",
    "AF009GR4_EVALUATION.md",
    ".agentforge/data/index.sqlite3",
    ".agentforge/data/read_only_evidence.json",
)


class ModelReadinessError(RuntimeError):
    """A bounded, classified model-readiness failure."""

    def __init__(self, code: str, message: str, *, observations=None, exit_code=None):
        super().__init__(message)
        self.code = code
        self.observations = list(observations or [])
        self.exit_code = exit_code


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def _file_record(path: Path) -> dict:
    return {"bytes": path.stat().st_size, "sha256": _sha256(path)}


def _protected_snapshot() -> dict:
    return {
        relative: _file_record(PROJECT_ROOT / relative)
        for relative in PROTECTED_PATHS
    }


def _runtime_manifest(root: Path) -> list[dict]:
    records = []
    for path in sorted((item for item in root.rglob("*") if item.is_file()), key=lambda item: item.as_posix().casefold()):
        records.append({
            "path": path.relative_to(root).as_posix(),
            "bytes": path.stat().st_size,
            "sha256": _sha256(path),
        })
    return records


def _error_record(exc: BaseException) -> dict:
    return {
        "type": type(exc).__name__,
        "message": str(exc),
        "errno": getattr(exc, "errno", None),
        "winerror": getattr(exc, "winerror", None),
    }


def _loopback_preflight() -> dict:
    result = {"status": "started", "host": "127.0.0.1", "steps": {}}
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.settimeout(5.0)
    server_state: dict = {}
    thread: threading.Thread | None = None
    try:
        started = time.perf_counter()
        listener.bind(("127.0.0.1", 0))
        result["steps"]["bind_seconds"] = round(time.perf_counter() - started, 6)
        result["port"] = listener.getsockname()[1]

        started = time.perf_counter()
        listener.listen(1)
        result["steps"]["listen_seconds"] = round(time.perf_counter() - started, 6)

        def serve_once() -> None:
            accepted = None
            try:
                accept_started = time.perf_counter()
                accepted, peer = listener.accept()
                server_state["accept_seconds"] = round(time.perf_counter() - accept_started, 6)
                server_state["peer"] = [peer[0], peer[1]]
                accepted.settimeout(5.0)
                payload = accepted.recv(4)
                server_state["received"] = payload.decode("ascii", errors="replace")
                if payload != b"ping":
                    raise RuntimeError("Loopback server did not receive the expected ping payload.")
                accepted.sendall(b"pong")
            except BaseException as exc:  # captured for the controlling thread
                server_state["error"] = _error_record(exc)
            finally:
                if accepted is not None:
                    accepted.close()

        thread = threading.Thread(target=serve_once, name="af009gr3-loopback", daemon=True)
        thread.start()

        connect_started = time.perf_counter()
        with socket.create_connection(("127.0.0.1", result["port"]), timeout=5.0) as client:
            result["steps"]["connect_seconds"] = round(time.perf_counter() - connect_started, 6)
            client.settimeout(5.0)
            exchange_started = time.perf_counter()
            client.sendall(b"ping")
            payload = client.recv(4)
            result["steps"]["exchange_seconds"] = round(time.perf_counter() - exchange_started, 6)
            result["client_received"] = payload.decode("ascii", errors="replace")
            if payload != b"pong":
                raise RuntimeError("Loopback client did not receive the expected pong payload.")
        thread.join(timeout=6.0)
        if thread.is_alive():
            raise TimeoutError("Loopback accept thread did not finish.")
        if "error" in server_state:
            raise RuntimeError(f"Loopback server failed: {server_state['error']['type']}.")
        result["server"] = server_state
        result["status"] = "passed"
        return result
    except BaseException as exc:
        result["status"] = "failed"
        result["error"] = _error_record(exc)
        return result
    finally:
        listener.close()
        if thread is not None and thread.is_alive():
            thread.join(timeout=0.2)
        result["all_sockets_closed"] = True


def _reserve_ephemeral_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _drain(pipe, target: bytearray, counters: dict, name: str) -> None:
    captured_limit = 8 * 1024 * 1024
    total = 0
    while True:
        chunk = pipe.read(65536)
        if not chunk:
            break
        total += len(chunk)
        if len(target) < captured_limit:
            target.extend(chunk[: captured_limit - len(target)])
    counters[name] = total


def _safe_log_summary(raw: bytes) -> list[str]:
    selected = []
    markers = (
        "model loaded", "listening on", "server is listening", "error", "failed",
        "embedding", "pooling", "exiting", "shutdown",
    )
    for line in raw.decode("utf-8", errors="replace").splitlines():
        lowered = line.lower()
        if any(marker in lowered for marker in markers):
            selected.append(_sanitize_health_text(line[:500]))
        if len(selected) >= 40:
            break
    return selected


def _wait_for_tcp(process: subprocess.Popen, port: int, timeout: float = 60.0) -> dict:
    started = time.perf_counter()
    attempts = 0
    last_error = None
    while time.perf_counter() - started < timeout:
        code = process.poll()
        if code is not None:
            raise RuntimeError(f"llama-server exited before readiness with exit code {code}.")
        attempts += 1
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return {
                    "status": "passed",
                    "attempts": attempts,
                    "seconds": round(time.perf_counter() - started, 6),
                }
        except OSError as exc:
            last_error = _error_record(exc)
            time.sleep(0.1)
    raise TimeoutError(
        f"llama-server TCP readiness did not succeed within {timeout:.0f} seconds; "
        f"last error type was {(last_error or {}).get('type', 'unknown')}."
    )


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        return None


_SENSITIVE_HEALTH_KEYS = {
    "api_key", "apikey", "authorization", "password", "secret", "token",
}


def _sanitize_health_json(value):
    if isinstance(value, dict):
        return {
            str(key): (
                "<redacted>"
                if str(key).casefold().replace("-", "_") in _SENSITIVE_HEALTH_KEYS
                else _sanitize_health_json(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_sanitize_health_json(item) for item in value]
    return value


def _sanitize_health_text(value: str) -> str:
    return re.sub(
        r"(?i)(api[_-]?key|authorization|password|secret|token)(\s*[:=]\s*)([^\s,;]+)",
        r"\1\2<redacted>",
        value,
    )


def _health_probe(
    host: str,
    port: int,
    *,
    timeout: float = 2.0,
    opener=None,
) -> dict:
    """Perform one direct, proxy-free, non-redirecting GET /health request."""
    url = f"http://{host}:{port}/health"
    request = Request(url, headers={"Accept": "application/json"}, method="GET")
    local_opener = opener or build_opener(ProxyHandler({}), _NoRedirectHandler())
    response = None
    via_http_error = False
    try:
        try:
            response = local_opener.open(request, timeout=timeout)
        except HTTPError as exc:
            # HTTPError is also the response object; preserve its status, headers and body.
            response = exc
            via_http_error = True
        status_value = getattr(response, "status", None)
        if status_value is None:
            status_value = response.getcode()
        status = int(status_value)
        headers = getattr(response, "headers", None)
        content_type = headers.get("Content-Type") if headers is not None else None
        raw = response.read(HEALTH_RESPONSE_LIMIT + 1)
        truncated = len(raw) > HEALTH_RESPONSE_LIMIT
        retained = raw[:HEALTH_RESPONSE_LIMIT]
        observation = {
            "http_status": status,
            "content_type": content_type,
            "body_bytes": len(raw),
            "body_truncated": truncated,
            "via_http_error": via_http_error,
        }
        if truncated:
            observation["response_body"] = _sanitize_health_text(
                retained.decode("utf-8", errors="replace")
            )
            raise ModelReadinessError(
                "MODEL_HEALTH_RESPONSE_TOO_LARGE",
                "Model health response exceeded the configured size limit.",
                observations=[observation],
            )
        try:
            decoded = json.loads(retained.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            observation.update({
                "json_keys": [],
                "response_shape": "invalid_json",
                "response_body": _sanitize_health_text(
                    retained.decode("utf-8", errors="replace")
                ),
            })
            raise ModelReadinessError(
                "MODEL_HEALTH_INVALID_JSON",
                "Model health endpoint returned invalid JSON.",
                observations=[observation],
            ) from None

        sanitized = _sanitize_health_json(decoded)
        observation.update({
            "json_keys": sorted(decoded) if isinstance(decoded, dict) else [],
            "response_body": json.dumps(
                sanitized, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ),
            "response_shape": "unrecognized",
            "semantic_status": "unknown",
        })
        if 300 <= status < 400:
            raise ModelReadinessError(
                "MODEL_HEALTH_REDIRECT",
                "Model health endpoint returned a redirect; redirects are not followed.",
                observations=[observation],
            )
        if status == 200 and isinstance(decoded, dict) and decoded.get("status") == "ok":
            observation.update({
                "status": "ok",
                "response_shape": "top_level_status",
                "semantic_status": "ready",
            })
            return observation
        error = decoded.get("error") if isinstance(decoded, dict) else None
        if (
            status == 503
            and isinstance(error, dict)
            and error.get("code") == 503
            and error.get("message") == "Loading model"
            and error.get("type") == "unavailable_error"
        ):
            observation.update({
                "status": "Loading model",
                "error_keys": sorted(error),
                "response_shape": "llama_cpp_loading_error",
                "semantic_status": "loading",
            })
            return observation
        return observation
    except ModelReadinessError:
        raise
    except (OSError, URLError, socket.timeout) as exc:
        raise ModelReadinessError(
            "MODEL_HEALTH_REQUEST_FAILED",
            f"Model health request failed ({type(exc).__name__}).",
        ) from None
    finally:
        if response is not None:
            response.close()


def _wait_for_model_ready(
    process,
    host: str,
    port: int,
    *,
    total_timeout: float = 120.0,
    request_timeout: float = 2.0,
    poll_interval: float = 1.0,
    monotonic=time.monotonic,
    sleep=time.sleep,
    probe=_health_probe,
) -> dict:
    """Wait for llama-server's model-level health without sending embeddings."""
    started = monotonic()
    observations = []
    while True:
        exit_code = process.poll()
        if exit_code is not None:
            raise ModelReadinessError(
                "MODEL_PROCESS_EXITED",
                f"llama-server exited before MODEL_READY with exit code {exit_code}.",
                observations=observations,
                exit_code=exit_code,
            )
        elapsed = monotonic() - started
        if elapsed >= total_timeout:
            raise ModelReadinessError(
                "MODEL_READINESS_TIMEOUT",
                f"Model did not become ready within {total_timeout:.0f} seconds.",
                observations=observations,
            )
        try:
            observation = probe(host, port, timeout=request_timeout)
        except ModelReadinessError as exc:
            exc.observations = observations + exc.observations
            raise
        observation = {
            **observation,
            "elapsed_seconds": round(monotonic() - started, 6),
        }
        observations.append(observation)
        if observation.get("semantic_status") == "ready":
            return {
                "status": "passed",
                "seconds": observation["elapsed_seconds"],
                "poll_count": len(observations),
                "observations": observations,
            }
        if observation.get("semantic_status") == "loading":
            remaining = total_timeout - (monotonic() - started)
            if remaining <= 0:
                continue
            sleep(min(poll_interval, remaining))
            continue
        raise ModelReadinessError(
            "MODEL_HEALTH_UNEXPECTED_STATUS",
            "Model health endpoint returned an unexpected HTTP/status combination.",
            observations=observations,
        )


def _transition(result: dict, state: str) -> None:
    result["state"] = state
    result.setdefault("state_history", []).append({
        "state": state,
        "monotonic_seconds": round(time.monotonic(), 6),
    })


def _evaluate_after_model_ready(readiness: dict, evaluator):
    """Hard gate: evaluation is callable only after a passed model health check."""
    if not isinstance(readiness, dict) or readiness.get("status") != "passed":
        raise ModelReadinessError(
            "MODEL_NOT_READY",
            "Embedding evaluation is prohibited until MODEL_READY is confirmed.",
        )
    return evaluator()


def _process_memory_record(process) -> dict | None:
    """Read Windows process memory counters without adding a dependency."""
    if sys.platform != "win32" or process.poll() is not None:
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class ProcessMemoryCounters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        counters = ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        success = ctypes.windll.psapi.GetProcessMemoryInfo(
            int(process._handle), ctypes.byref(counters), counters.cb
        )
        if not success:
            return None
        return {
            "working_set_bytes": int(counters.WorkingSetSize),
            "peak_working_set_bytes": int(counters.PeakWorkingSetSize),
            "pagefile_bytes": int(counters.PagefileUsage),
            "peak_pagefile_bytes": int(counters.PeakPagefileUsage),
        }
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def _stop_owned_process(process, *, terminate_timeout: float = 15.0) -> dict:
    """Stop only the supplied child and preserve its real exit code."""
    result = {"owned_process_stopped": False}
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=terminate_timeout)
            result["termination"] = "terminated"
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10.0)
            result["termination"] = "killed_after_terminate_timeout"
    else:
        result["termination"] = "exited_without_cleanup_signal"
    result["exit_code"] = process.returncode
    result["owned_process_stopped"] = process.poll() is not None
    return result


def _port_released(port: int) -> bool:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        probe.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def main() -> int:
    if RESULT_PATH.exists():
        print("AF009GR5_RESULTS.json already exists; refusing to overwrite it.", file=sys.stderr)
        return 2

    result: dict = {
        "stage": "AF-009G-R5",
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
    process: subprocess.Popen | None = None
    temp_runtime: Path | None = None
    stdout_buffer = bytearray()
    stderr_buffer = bytearray()
    output_counts: dict = {}
    reader_threads: list[threading.Thread] = []
    exit_code = 1

    try:
        for path, expected in EXPECTED_HASHES.items():
            if not path.is_file():
                raise FileNotFoundError(f"Required asset is missing: {path}")
            actual = _sha256(path)
            result["assets"][str(path)] = {
                "bytes": path.stat().st_size,
                "sha256": actual,
                "expected_sha256": expected,
                "matches": actual == expected,
            }
            if actual != expected:
                raise RuntimeError(f"Required asset hash does not match: {path.name}")

        source_manifest = _runtime_manifest(RUNTIME_PATH)
        result["runtime_copy"]["source_file_count"] = len(source_manifest)
        result["runtime_copy"]["source_manifest_sha256"] = hashlib.sha256(
            json.dumps(source_manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest().upper()
        if len(source_manifest) != 51:
            raise RuntimeError("Pinned runtime no longer contains the expected 51 files.")

        result["loopback_preflight"] = _loopback_preflight()
        if result["loopback_preflight"]["status"] != "passed":
            result["status"] = "blocked_by_loopback_preflight"
            raise RuntimeError("Host loopback preflight failed; model startup is prohibited.")

        port = _reserve_ephemeral_port()
        result["service"]["host"] = "127.0.0.1"
        result["service"]["port"] = port
        temp_runtime = TEMP_ROOT / f"af009gr5-runtime-{uuid.uuid4().hex}"
        temp_runtime.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(RUNTIME_PATH, temp_runtime, copy_function=shutil.copy2)
        copied_manifest = _runtime_manifest(temp_runtime)
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
            "--model", str(MODEL_PATH),
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
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        started = time.perf_counter()
        process = subprocess.Popen(
            arguments,
            cwd=temp_runtime,
            env=child_environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            creationflags=creation_flags,
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
                target=_drain, args=(pipe, target, output_counts, name),
                name=f"af009gr3-{name}", daemon=True,
            )
            thread.start()
            reader_threads.append(thread)

        result["service"]["tcp_readiness"] = _wait_for_tcp(process, port)
        _transition(result, "TCP_LISTENING")
        result["service"]["startup_seconds"] = round(time.perf_counter() - started, 6)
        _transition(result, "MODEL_LOADING")
        result["service"]["model_readiness"] = _wait_for_model_ready(
            process,
            "127.0.0.1",
            port,
            total_timeout=120.0,
            request_timeout=2.0,
            poll_interval=1.0,
        )
        _transition(result, "MODEL_READY")

        from agentforge.generalization import evaluate_generalization

        _transition(result, "EVALUATING")
        evaluation_started = time.perf_counter()
        evaluation = _evaluate_after_model_ready(
            result["service"]["model_readiness"],
            lambda: evaluate_generalization(
                base_url=f"http://127.0.0.1:{port}",
                model_id=MODEL_ID,
                api_model=API_MODEL,
                dimension=1024,
            ),
        )
        result["evaluation"] = evaluation
        result["evaluation_elapsed_seconds"] = round(time.perf_counter() - evaluation_started, 6)
        result["service"]["resource_sample"] = _process_memory_record(process)
        result["status"] = "completed"
        _transition(result, "COMPLETED")
        exit_code = 0
    except BaseException as exc:
        if result["status"] == "started":
            result["status"] = "failed"
        result["error"] = _error_record(exc)
        if isinstance(exc, ModelReadinessError):
            result["error"]["code"] = exc.code
            result["error"]["observations"] = exc.observations
            result["error"]["process_exit_code"] = exc.exit_code
        _transition(result, "FAILED")
    finally:
        if process is not None:
            if "resource_sample" not in result["service"]:
                result["service"]["resource_sample"] = _process_memory_record(process)
            result["service"].update(_stop_owned_process(process))
            for thread in reader_threads:
                thread.join(timeout=5.0)
            result["service"]["output_bytes"] = output_counts
            result["service"]["stdout_summary"] = _safe_log_summary(bytes(stdout_buffer))
            result["service"]["stderr_summary"] = _safe_log_summary(bytes(stderr_buffer))
            result["service"]["port_released"] = _port_released(result["service"]["port"])

        if temp_runtime is not None and temp_runtime.exists():
            post_manifest = _runtime_manifest(temp_runtime)
            result["runtime_copy"]["post_run_file_count"] = len(post_manifest)
            result["runtime_copy"]["post_run_matches_source"] = post_manifest == source_manifest
            source_paths = {entry["path"] for entry in source_manifest}
            result["runtime_copy"]["extra_files"] = [
                entry["path"] for entry in post_manifest if entry["path"] not in source_paths
            ]
            shutil.rmtree(temp_runtime)
            result["runtime_copy"]["removed"] = not temp_runtime.exists()

        result["assets_after"] = {
            str(path): _file_record(path) for path in EXPECTED_HASHES
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
        }, ensure_ascii=False, indent=2))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
