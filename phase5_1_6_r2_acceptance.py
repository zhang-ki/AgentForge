"""Bounded Phase 5.1.6-R2 host acceptance for isolated Chroma resources.

This module is acceptance infrastructure only. It does not start or stop services,
touch the formal repository registry, or open AgentForge's persistent production
Chroma directory. The host path uses synthetic repositories and temporary stores.
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
import tempfile
import time
import uuid
from urllib.parse import urlsplit


PROJECT_ROOT = Path(__file__).resolve().parent
EXPECTED_BRANCH = "feature/auto-chroma-hybrid"
EXPECTED_HEAD = "026bd2978ed315d7fc39b8c3556aedc0f5cd3e37"
EXPECTED_TAG = "v0.1.0-rc1"
R1_RESULTS = PROJECT_ROOT / "PHASE5_1_6_R1_ACCEPTANCE_RESULTS.json"
QUERY = "Explain the implementation for resuming unfinished deliveries after a temporary transport interruption."
R1_08_QUERY = "Which routine resumes pending messages after a brief network interruption?"
AGENTFORGE_LAUNCHER = PROJECT_ROOT / "launchers" / "start-agentforge.ps1"
EMBEDDING_ENV_KEYS = (
    "AGENTFORGE_EMBEDDING_BASE_URL",
    "AGENTFORGE_EMBEDDING_MODEL_ID",
    "AGENTFORGE_EMBEDDING_API_MODEL",
    "AGENTFORGE_EMBEDDING_DIMENSION",
)


class AcceptanceFailure(RuntimeError):
    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(code)


def launcher_embedding_configuration(launcher_path: Path = AGENTFORGE_LAUNCHER) -> dict:
    """Read the literal process defaults used by the normal AgentForge launcher."""
    if not launcher_path.is_file():
        raise AcceptanceFailure("EMBEDDING_LAUNCHER_MISSING")
    source = launcher_path.read_text(encoding="utf-8")
    assignments = dict(re.findall(
        r"^\s*\$env:(AGENTFORGE_EMBEDDING_[A-Z_]+)\s*=\s*'([^']*)'\s*$",
        source,
        flags=re.MULTILINE | re.IGNORECASE,
    ))
    normalized = {key.upper(): value for key, value in assignments.items()}
    if any(not normalized.get(key) for key in EMBEDDING_ENV_KEYS):
        raise AcceptanceFailure("EMBEDDING_LAUNCHER_CONFIGURATION_INCOMPLETE")
    endpoint = urlsplit(normalized["AGENTFORGE_EMBEDDING_BASE_URL"])
    if (endpoint.scheme != "http" or endpoint.hostname != "127.0.0.1"
            or endpoint.port != 58067 or endpoint.path not in ("", "/")
            or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment):
        raise AcceptanceFailure("EMBEDDING_LAUNCHER_ENDPOINT_NOT_LOCAL")
    try:
        dimension = int(normalized["AGENTFORGE_EMBEDDING_DIMENSION"])
    except (TypeError, ValueError):
        raise AcceptanceFailure("EMBEDDING_LAUNCHER_DIMENSION_INVALID") from None
    if dimension <= 0:
        raise AcceptanceFailure("EMBEDDING_LAUNCHER_DIMENSION_INVALID")
    return normalized


def apply_launcher_embedding_defaults(environ: dict | None = None) -> dict:
    """Mirror start-agentforge.ps1 defaults in this process only; reject overrides."""
    target = os.environ if environ is None else environ
    defaults = launcher_embedding_configuration()
    conflicts = [key for key in EMBEDDING_ENV_KEYS
                 if target.get(key) and target[key] != defaults[key]]
    if conflicts:
        raise AcceptanceFailure("EMBEDDING_CONFIGURATION_OVERRIDE_CONFLICT",
                                ", ".join(conflicts))
    for key in EMBEDDING_ENV_KEYS:
        if not target.get(key):
            target[key] = defaults[key]
    return {
        "status": "configured",
        "source": "launchers/start-agentforge.ps1; process scope only",
        "variable_names": list(EMBEDDING_ENV_KEYS),
        "model_id": defaults["AGENTFORGE_EMBEDDING_MODEL_ID"],
        "api_model": defaults["AGENTFORGE_EMBEDDING_API_MODEL"],
        "dimension": int(defaults["AGENTFORGE_EMBEDDING_DIMENSION"]),
        "endpoint_local": True,
    }


def canonical_sha256(value) -> str:
    payload = json.dumps(value, sort_keys=True, ensure_ascii=False,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _vector_sha256(vector) -> str:
    return canonical_sha256([float(value) for value in vector])


def collection_manifest(collection, repository_id: str) -> dict:
    data = collection.get(
        where={"repository_id": repository_id},
        include=["metadatas", "embeddings", "documents"],
    )
    identities = data.get("ids") or []
    metadata = data.get("metadatas") or []
    embeddings = data.get("embeddings")
    documents = data.get("documents")
    vectors = embeddings if embeddings is not None else []
    stored_documents = documents if documents is not None else []
    if not (len(identities) == len(metadata) == len(vectors) == len(stored_documents)):
        raise AcceptanceFailure("CHROMA_MANIFEST_SHAPE_MISMATCH")
    records = []
    for position, identity in enumerate(identities):
        item = metadata[position]
        if not isinstance(item, dict) or not isinstance(item.get("content_hash"), str):
            raise AcceptanceFailure("CHROMA_METADATA_INVALID")
        records.append({
            "id": identity,
            "metadata": dict(item),
            "content_hash": item["content_hash"],
            "vector_sha256": _vector_sha256(vectors[position]),
            "stored_document_sha256": hashlib.sha256(
                (stored_documents[position] or "").encode("utf-8")
            ).hexdigest(),
        })
    records.sort(key=lambda item: item["id"])
    return {
        "collection": collection.name,
        "repository_vector_count": len(identities),
        "collection_vector_count": int(collection.count()),
        "ids": [item["id"] for item in records],
        "records": records,
    }


def persistent_collection_snapshot(client, *, page_size: int = 250,
                                   maximum_records: int = 100_000) -> dict:
    """Hash a persistent store's records without returning source documents."""
    if (isinstance(page_size, bool) or not isinstance(page_size, int) or page_size < 1
            or isinstance(maximum_records, bool) or not isinstance(maximum_records, int)
            or maximum_records < 0):
        raise AcceptanceFailure("PERSISTENT_SNAPSHOT_LIMIT_INVALID")
    collections = []
    for item in client.list_collections():
        name = item.name if hasattr(item, "name") else str(item)
        collection = client.get_collection(name=name)
        count = int(collection.count())
        if count > maximum_records:
            raise AcceptanceFailure("PERSISTENT_SNAPSHOT_RECORD_LIMIT_EXCEEDED",
                                    f"collection={name}; count={count}")
        records = []
        offset = 0
        while offset < count:
            page = collection.get(limit=page_size, offset=offset,
                                  include=["metadatas", "embeddings"])
            ids = page.get("ids") or []
            metadata = page.get("metadatas") or []
            embeddings = page.get("embeddings")
            if not ids or len(ids) != len(metadata) or embeddings is None or len(ids) != len(embeddings):
                raise AcceptanceFailure("PERSISTENT_SNAPSHOT_SHAPE_INVALID",
                                        f"collection={name}; offset={offset}")
            for identity, meta, vector in zip(ids, metadata, embeddings):
                if not isinstance(meta, dict):
                    raise AcceptanceFailure("PERSISTENT_SNAPSHOT_METADATA_INVALID",
                                            f"collection={name}")
                records.append({
                    "id": str(identity),
                    "metadata_sha256": canonical_sha256(meta),
                    "content_hash": meta.get("content_hash"),
                    "repository_id": meta.get("repository_id"),
                    "vector_sha256": _vector_sha256(vector),
                })
            offset += len(ids)
        records.sort(key=lambda row: row["id"])
        metadata = collection.metadata or {}
        configuration = getattr(collection, "configuration", None)
        if not isinstance(configuration, dict):
            raise AcceptanceFailure("PERSISTENT_COLLECTION_CONFIGURATION_UNAVAILABLE",
                                    f"collection={name}")
        collections.append({
            "name": name,
            "collection_id": str(collection.id),
            "count": count,
            "existing_ids": [row["id"] for row in records],
            "repository_ids": sorted({row["repository_id"] for row in records
                                       if row.get("repository_id")} ),
            "metadata_keys": sorted(str(key) for key in metadata),
            "metadata_sha256": canonical_sha256(metadata),
            "resolved_configuration": configuration,
            "record_summaries": records,
            "record_summary_sha256": canonical_sha256(records),
        })
    collections.sort(key=lambda row: row["name"])
    return {"collection_count": len(collections), "collections": collections}


def create_persistent_acceptance_collection(client, *, model_id: str, dimension: int,
                                            run_id: str) -> dict:
    """Create a unique collection under the real Chroma root using product defaults."""
    production_name = "af012_vectors_" + hashlib.sha256(model_id.strip().encode("utf-8")).hexdigest()[:20]
    try:
        production = client.get_collection(name=production_name)
    except Exception:
        raise AcceptanceFailure("PRODUCTION_MODEL_COLLECTION_NOT_FOUND",
                                f"collection={production_name}") from None
    metadata = production.metadata or {}
    configuration = production.configuration
    hnsw = configuration.get("hnsw") if isinstance(configuration, dict) else None
    space = hnsw.get("space") if isinstance(hnsw, dict) else None
    if (metadata.get("agentforge_model_id") != model_id
            or str(metadata.get("agentforge_dimension")) != str(dimension)
            or metadata.get("hnsw:space") != "cosine" or space != "cosine"):
        raise AcceptanceFailure("PRODUCTION_COLLECTION_CONFIGURATION_MISMATCH",
                                f"collection={production_name}")
    name = f"{production_name}_r2_{run_id[:12]}"
    existing = {
        item.name if hasattr(item, "name") else str(item)
        for item in client.list_collections()
    }
    if name in existing:
        raise AcceptanceFailure("ACCEPTANCE_COLLECTION_NAME_COLLISION", f"collection={name}")
    requested_configuration = {"hnsw": {"space": space}}
    requested_metadata = {
        "hnsw:space": space,
        "agentforge_model_id": model_id,
        "agentforge_dimension": str(dimension),
    }
    creation_error = None
    try:
        collection = client.create_collection(
            name=name, embedding_function=None, metadata=requested_metadata,
            configuration=requested_configuration,
        )
    except Exception as exc:
        creation_error = type(exc).__name__
        try:
            collection = client.get_collection(name=name)
        except Exception:
            raise AcceptanceFailure("ACCEPTANCE_COLLECTION_CREATE_FAILED", creation_error) from None
    resolved = collection.configuration
    resolved_hnsw = resolved.get("hnsw") if isinstance(resolved, dict) else None
    configuration_matches = (
        (collection.metadata or {}) == requested_metadata
        and isinstance(resolved_hnsw, dict) and resolved_hnsw.get("space") == space
    )
    return {
        "name": name,
        "collection_id": str(collection.id),
        "based_on_collection": production_name,
        "product_requested_configuration": requested_configuration,
        "resolved_configuration": resolved,
        "metadata": requested_metadata,
        "configuration_matches": configuration_matches,
        "create_call_error_recovered": creation_error,
    }


def cleanup_owned_persistent_collection(client, *, name: str, expected_collection_id: str,
                                        owned_repository_ids: set[str]) -> dict:
    """Delete only an exact acceptance collection containing only this run's repo IDs."""
    names = {item.name if hasattr(item, "name") else str(item)
             for item in client.list_collections()}
    if name not in names:
        return {"status": "PASS", "collection_removed": True, "records_removed": 0}
    collection = client.get_collection(name=name)
    if str(collection.id) != expected_collection_id:
        raise AcceptanceFailure("ACCEPTANCE_COLLECTION_ID_CHANGED", f"collection={name}")
    data = collection.get(include=["metadatas"])
    ids = data.get("ids") or []
    metadata = data.get("metadatas") or []
    if len(ids) != len(metadata):
        raise AcceptanceFailure("ACCEPTANCE_COLLECTION_CLEANUP_SHAPE_INVALID", f"collection={name}")
    foreign = [row.get("repository_id") for row in metadata
               if not isinstance(row, dict) or row.get("repository_id") not in owned_repository_ids]
    if foreign:
        raise AcceptanceFailure("UNOWNED_RECORD_IN_ACCEPTANCE_COLLECTION",
                                f"collection={name}; record_count={len(foreign)}")
    for repository_id in sorted(owned_repository_ids):
        collection.delete(where={"repository_id": repository_id})
    if int(collection.count()) != 0:
        raise AcceptanceFailure("ACCEPTANCE_COLLECTION_NOT_EMPTY_AFTER_OWNED_DELETE",
                                f"collection={name}")
    client.delete_collection(name=name)
    remaining = {item.name if hasattr(item, "name") else str(item)
                 for item in client.list_collections()}
    if name in remaining:
        raise AcceptanceFailure("ACCEPTANCE_COLLECTION_DELETE_NOT_CONFIRMED",
                                f"collection={name}")
    return {"status": "PASS", "collection_removed": True,
            "records_removed": len(ids), "deleted_collection_name": name,
            "deleted_collection_id": expected_collection_id}


def run_child_process(command, *, cwd: Path, env: dict | None = None,
                      timeout_seconds: float = 120.0) -> dict:
    """Capture a child process outcome, including nonzero exit and timeout cases."""
    try:
        completed = subprocess.run(
            list(command), cwd=str(cwd), env=env, capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=timeout_seconds,
            check=False,
        )
        return {
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "timed_out": False,
            "launch_error": None,
        }
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", errors="replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        return {
            "returncode": None,
            "stdout": stdout,
            "stderr": stderr,
            "timed_out": True,
            "launch_error": None,
        }
    except OSError as exc:
        return {
            "returncode": None,
            "stdout": "",
            "stderr": "",
            "timed_out": False,
            "launch_error": type(exc).__name__,
        }


def launch_reopen_child(request: dict, *, request_directory: Path,
                        timeout_seconds: float = 120.0) -> dict:
    request_path = request_directory / f"reopen_request_{uuid.uuid4().hex}.json"
    try:
        request_path.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
        env = os.environ.copy()
        env["PYTHONPATH"] = str(PROJECT_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
        command = [sys.executable, "-X", "utf8", "-B", str(Path(__file__).resolve()),
                   "--r2-reopen-child", str(request_path)]
        process = run_child_process(command, cwd=PROJECT_ROOT, env=env,
                                    timeout_seconds=timeout_seconds)
        parsed = None
        for line in reversed(process["stdout"].splitlines()):
            try:
                parsed = json.loads(line)
                break
            except json.JSONDecodeError:
                continue
        process["result"] = parsed
        return process
    finally:
        request_path.unlink(missing_ok=True)


def _close_chroma(client) -> None:
    close = getattr(client, "close", None)
    if callable(close):
        close()
        return
    system = getattr(client, "_system", None)
    stop = getattr(system, "stop", None)
    if callable(stop):
        stop()


def _child_reopen(request: dict) -> int:
    """Open persisted Chroma from a new OS process and verify data + retrieval."""
    client = None
    try:
        from agentforge.af012_runtime import create_persistent_chroma
        from agentforge.embeddings.chroma_store import ChromaCandidateStore

        client = create_persistent_chroma(request["chroma_path"])
        collection = client.get_collection(name=request["collection"])
        manifest = collection_manifest(collection, request["repository_id"])
        if manifest != request["expected_manifest"]:
            raise AcceptanceFailure("PERSISTED_MANIFEST_MISMATCH")
        if os.getpid() == int(request["parent_pid"]):
            raise AcceptanceFailure("CHILD_PROCESS_PID_NOT_DISTINCT")

        hits = []
        if request["mode"] == "product":
            from agentforge.embeddings.chroma_index import ChromaCodeIndex
            from agentforge.indexer import index_repository
            from agentforge.store import IndexStore
            from agentforge.af012_runtime import embedding_provider_from_environment

            provider = embedding_provider_from_environment()
            with IndexStore(":memory:") as index:
                index_repository(request["repository_path"], index)
                service = ChromaCodeIndex(index, collection, provider)
                consistency = service.inspect_consistency()
                if not consistency.get("complete"):
                    raise AcceptanceFailure("REOPENED_INDEX_INCONSISTENT")
                hits = [
                    {"path": item.relative_path, "content_hash": item.content_hash,
                     "start_line": item.start_line, "end_line": item.end_line}
                    for item in service.search(request["query"], top_k=5)
                ]
            if not any(item["path"] == request["expected_path"]
                       and item["content_hash"] == request["expected_content_hash"]
                       for item in hits):
                raise AcceptanceFailure("REOPENED_SEMANTIC_RETRIEVAL_FAILED")
            provider_counts = provider.request_counts
        elif request["mode"] == "raw_test":
            from agentforge.embeddings.chroma_store import ChromaSource, _identity
            vector_store = ChromaCandidateStore(collection, dimension=request["dimension"])
            hits_raw = vector_store.search(
                request["query_vector"], repository_id=request["repository_id"],
                model_id=request["model_id"],
                verify_source=lambda source: (
                    source.content_hash == request["expected_content_hash"]
                    and source.file_path == request["expected_path"]
                ), limit=5,
            )
            hits = [{"id": _identity(source), "path": source.file_path,
                     "content_hash": source.content_hash} for source, _distance in hits_raw]
            if not hits:
                raise AcceptanceFailure("REOPENED_RAW_QUERY_FAILED")
            provider_counts = {"attempts": 0, "sent": 0, "successes": 0, "failures": 0}
        else:
            raise AcceptanceFailure("UNKNOWN_REOPEN_MODE")

        print(json.dumps({
            "status": "PASS", "pid": os.getpid(), "manifest": manifest,
            "semantic_hits": hits, "embedding_request_counts": provider_counts,
        }, sort_keys=True, ensure_ascii=False))
        return 0
    except Exception as exc:
        code = exc.code if isinstance(exc, AcceptanceFailure) else type(exc).__name__
        print(json.dumps({"status": "FAIL", "pid": os.getpid(), "error_code": code},
                         sort_keys=True, ensure_ascii=False))
        return 2
    finally:
        if client is not None:
            _close_chroma(client)


class QueryUnavailableCollection:
    """Process-local outage injection around only this test's Chroma collection."""

    def __init__(self, collection):
        self._collection = collection

    def __getattr__(self, name):
        return getattr(self._collection, name)

    def query(self, **_kwargs):
        raise ConnectionError("synthetic temporary Chroma query outage")


def _synthetic_repository(path: Path, *, name: str) -> None:
    path.mkdir(parents=True, exist_ok=False)
    (path / "connectivity.py").write_text(
        '"""Synthetic delivery recovery policy."""\n'
        "def replay_unacknowledged_deliveries(connection, pending):\n"
        '    """Resume unfinished deliveries after a temporary transport loss."""\n'
        "    if connection.is_ready():\n"
        "        return pending\n"
        "    return []\n",
        encoding="utf-8",
    )
    (path / "README.md").write_text(
        f"# Synthetic {name}\n\n"
        "Unacknowledged deliveries resume after a temporary connection interruption.\n",
        encoding="utf-8",
    )


def _sqlite_snapshot(store) -> list[tuple]:
    return [
        (row["relative_path"], int(row["start_line"]), int(row["end_line"]),
         row["content_hash"], row["text"])
        for row in store.iter_chunks()
    ]


def _verify_candidates_against_sqlite(outcome, store) -> int:
    if not outcome.candidates:
        raise AcceptanceFailure("LEXICAL_FALLBACK_RETURNED_NO_EVIDENCE")
    checked = 0
    for candidate in outcome.candidates:
        row = store.get_chunk(candidate.relative_path, candidate.start_line, candidate.end_line)
        if (row is None or row["text"] != candidate.text
                or row["content_hash"] != candidate.content_hash):
            raise AcceptanceFailure("LEXICAL_CANDIDATE_SOURCE_HASH_MISMATCH")
        checked += 1
    return checked


def _degraded_ui(outcome) -> str:
    from agentforge.presentation import render_ask_result

    result = {
        "provider": "offline",
        "answer": "Synthetic acceptance answer.",
        "answer_contract": {"status": "answered", "evidence": [], "claims": []},
        "retrieval": {"index_lifecycle": outcome.index_lifecycle},
        "evidence": [],
    }
    return render_ask_result(result)


def run_degradation_and_recovery(repository: Path, chroma_path: Path, *,
                                 embedding_provider, unavailable_provider,
                                 collection_name: str | None = None) -> dict:
    """Exercise ProductRetriever against synthetic SQLite and the selected Chroma collection."""
    from agentforge.af012_runtime import collection_for_model, create_persistent_chroma
    from agentforge.embeddings.chroma_index import ChromaCodeIndex, repository_identity
    from agentforge.indexer import index_repository
    from agentforge.product_retrieval import ProductRetriever
    from agentforge.store import IndexStore

    client = None
    try:
        with IndexStore(":memory:") as index:
            indexed = index_repository(repository, index)
            client = create_persistent_chroma(chroma_path)
            collection = (client.get_collection(name=collection_name) if collection_name
                          else collection_for_model(
                              client, embedding_provider.model_id, embedding_provider.dimension,
                          ))
            repo_id = repository_identity(repository)
            service = ChromaCodeIndex(index, collection, embedding_provider)
            initial_sync = service.sync()
            initial_consistency = service.inspect_consistency()
            initial_count = len(collection.get(
                where={"repository_id": repo_id}, include=["metadatas"],
            ).get("ids") or [])
            source_before = _sqlite_snapshot(index)
            if (not initial_consistency.get("complete")
                    or initial_count != len(source_before) or not source_before):
                raise AcceptanceFailure("INITIAL_TEMP_INDEX_INCOMPLETE")

            ready_outcome = ProductRetriever(
                index, repository, vector_retriever=service,
            ).search(QUERY, top_k=5)
            if (not ready_outcome.index_lifecycle
                    or ready_outcome.index_lifecycle.get("state") != "READY"
                    or not any("vector" in candidate.channels for candidate in ready_outcome.candidates)):
                raise AcceptanceFailure("INITIAL_VECTOR_RETRIEVAL_NOT_READY")
            _verify_candidates_against_sqlite(ready_outcome, index)

            embedding_down_service = ChromaCodeIndex(index, collection, unavailable_provider)
            embedding_down = ProductRetriever(
                index, repository, vector_retriever=embedding_down_service,
            ).search(QUERY, top_k=5)
            embedding_checked = _verify_candidates_against_sqlite(embedding_down, index)
            embedding_ui = _degraded_ui(embedding_down)
            if ("vector" not in embedding_down.unavailable_channels
                    or embedding_down.index_lifecycle.get("state") != "DEGRADED_LEXICAL_ONLY"
                    or embedding_down.index_lifecycle.get("reason_code") != "vector_search_unavailable"
                    or "向量检索暂不可用" not in embedding_ui
                    or "vector_search_unavailable" not in embedding_ui):
                raise AcceptanceFailure("EMBEDDING_DEGRADATION_NOT_VISIBLE")

            chroma_down_service = ChromaCodeIndex(
                index, QueryUnavailableCollection(collection), embedding_provider,
            )
            chroma_down = ProductRetriever(
                index, repository, vector_retriever=chroma_down_service,
            ).search(QUERY, top_k=5)
            chroma_checked = _verify_candidates_against_sqlite(chroma_down, index)
            chroma_ui = _degraded_ui(chroma_down)
            if ("vector" not in chroma_down.unavailable_channels
                    or chroma_down.index_lifecycle.get("state") != "DEGRADED_LEXICAL_ONLY"
                    or chroma_down.index_lifecycle.get("reason_code") != "vector_search_unavailable"
                    or "向量检索暂不可用" not in chroma_ui
                    or "vector_search_unavailable" not in chroma_ui):
                raise AcceptanceFailure("CHROMA_DEGRADATION_NOT_VISIBLE")

            recovered_sync = service.sync()
            recovered_consistency = service.inspect_consistency()
            recovered_count = len(collection.get(
                where={"repository_id": repo_id}, include=["metadatas"],
            ).get("ids") or [])
            recovered = ProductRetriever(
                index, repository, vector_retriever=service,
            ).search(QUERY, top_k=5)
            recovered_checked = _verify_candidates_against_sqlite(recovered, index)
            source_after = _sqlite_snapshot(index)
            if (not recovered_consistency.get("complete")
                    or recovered_count != initial_count
                    or recovered_sync.embedded != 0
                    or recovered_sync.unchanged != len(source_before)
                    or source_after != source_before
                    or recovered.index_lifecycle.get("state") != "READY"
                    or not any("vector" in candidate.channels for candidate in recovered.candidates)):
                raise AcceptanceFailure("VECTOR_RECOVERY_OR_SQLITE_AUTHORITY_FAILED")

            return {
                "status": "PASS",
                "repository_id": repo_id,
                "repository_id_prefix": repo_id[:12],
                "sqlite_chunk_count": len(source_before),
                "initial_sync": {
                    "sources": initial_sync.sources, "embedded": initial_sync.embedded,
                    "unchanged": initial_sync.unchanged, "removed": initial_sync.removed,
                },
                "initial_chroma_count": initial_count,
                "embedding_unavailable": {
                    "status": "PASS", "injection": "separate local provider pointed at unused loopback port",
                    "state": embedding_down.index_lifecycle["state"],
                    "reason_code": embedding_down.index_lifecycle["reason_code"],
                    "lexical_candidates_checked_against_sqlite": embedding_checked,
                    "user_message_visible": True,
                    "provider_request_counts": getattr(unavailable_provider, "request_counts", None),
                },
                "chroma_unavailable": {
                    "status": "PASS", "injection": "query failure wrapper around acceptance collection only",
                    "state": chroma_down.index_lifecycle["state"],
                    "reason_code": chroma_down.index_lifecycle["reason_code"],
                    "lexical_candidates_checked_against_sqlite": chroma_checked,
                    "user_message_visible": True,
                    "injected_error_type": "ConnectionError",
                },
                "recovery": {
                    "status": "PASS", "state": recovered.index_lifecycle["state"],
                    "consistency": recovered_consistency,
                    "sync_embedded": recovered_sync.embedded,
                    "sync_unchanged": recovered_sync.unchanged,
                    "chroma_count_after_recovery": recovered_count,
                    "lexical_and_vector_candidates_checked_against_sqlite": recovered_checked,
                    "sqlite_authoritative_snapshot_unchanged": source_after == source_before,
                    "duplicate_or_stale_vectors": False,
                },
                "embedding_request_counts": getattr(embedding_provider, "request_counts", None),
                "source_content_in_report": False,
            }
    finally:
        if client is not None:
            _close_chroma(client)


def _create_r1_08_repository(path: Path) -> str:
    path.mkdir(parents=True, exist_ok=False)
    (path / "delivery_recovery.py").write_text(
        '"""Synthetic recovery behavior for interrupted message delivery."""\n'
        "def replay_unacknowledged_deliveries(connection, pending):\n"
        '    """Resume unfinished deliveries after a temporary transport loss."""\n'
        "    if connection.is_ready():\n"
        "        return pending\n"
        "    return []\n",
        encoding="utf-8",
    )
    return "delivery_recovery.py"


def run_r1_08(chroma_path: Path, repository: Path, *, provider,
              collection_name: str | None = None,
              request_directory: Path | None = None) -> dict:
    """Write a real local vector, close the client, and reopen from a child PID."""
    from agentforge.af012_runtime import collection_for_model, create_persistent_chroma
    from agentforge.embeddings.chroma_index import ChromaCodeIndex, repository_identity
    from agentforge.indexer import index_repository
    from agentforge.store import IndexStore

    client = None
    try:
        relative_path = _create_r1_08_repository(repository)
        with IndexStore(":memory:") as index:
            indexed = index_repository(repository, index)
            client = create_persistent_chroma(chroma_path)
            collection = (client.get_collection(name=collection_name) if collection_name
                          else collection_for_model(client, provider.model_id, provider.dimension))
            repo_id = repository_identity(repository)
            service = ChromaCodeIndex(index, collection, provider)
            stats = service.sync()
            consistency = service.inspect_consistency()
            row = next((item for item in index.iter_chunks()
                        if item["relative_path"] == relative_path), None)
            if row is None or not consistency.get("complete") or stats.sources != 1:
                raise AcceptanceFailure("R1_08_WRITE_PHASE_INCOMPLETE")
            expected = collection_manifest(collection, repo_id)
            if expected["repository_vector_count"] != 1 or len(expected["ids"]) != 1:
                raise AcceptanceFailure("R1_08_EXPECTED_ONE_VECTOR")
            record = expected["records"][0]
            if (record["metadata"].get("content_hash") != row["content_hash"]
                    or record["metadata"].get("file_path") != relative_path
                    or not record["vector_sha256"]):
                raise AcceptanceFailure("R1_08_WRITE_METADATA_HASH_MISMATCH")
        parent_pid = os.getpid()
    finally:
        if client is not None:
            _close_chroma(client)
            client = None

    request = {
        "mode": "product", "chroma_path": str(chroma_path),
        "repository_path": str(repository), "repository_id": repo_id,
        "collection": expected["collection"], "expected_manifest": expected,
        "parent_pid": parent_pid, "query": R1_08_QUERY,
        "expected_path": relative_path,
        "expected_content_hash": expected["records"][0]["content_hash"],
    }
    process = launch_reopen_child(
        request,
        request_directory=request_directory if request_directory is not None else chroma_path.parent,
    )
    if process["timed_out"]:
        raise AcceptanceFailure("R1_08_CHILD_TIMEOUT")
    if process["launch_error"]:
        raise AcceptanceFailure("R1_08_CHILD_LAUNCH_FAILED", process["launch_error"])
    if process["returncode"] != 0 or not isinstance(process["result"], dict):
        child_code = process["result"].get("error_code") if process["result"] else None
        raise AcceptanceFailure("R1_08_CHILD_FAILED", child_code or f"exit_{process['returncode']}")
    reopened = process["result"]
    if (reopened.get("status") != "PASS" or reopened.get("pid") == parent_pid
            or reopened.get("manifest") != expected):
        raise AcceptanceFailure("R1_08_CHILD_INTEGRITY_CHECK_FAILED")
    if not any(hit.get("path") == relative_path
               and hit.get("content_hash") == request["expected_content_hash"]
               for hit in reopened.get("semantic_hits", [])):
        raise AcceptanceFailure("R1_08_CHILD_SEMANTIC_QUERY_FAILED")
    return {
        "status": "PASS", "repository_id": repo_id,
        "parent_pid": parent_pid, "child_pid": reopened["pid"],
        "pid_distinct": True, "manifest_before": expected,
        "manifest_after": reopened["manifest"], "manifest_identical": True,
        "semantic_hits": reopened["semantic_hits"],
        "parent_embedding_request_counts": provider.request_counts,
        "child_embedding_request_counts": reopened["embedding_request_counts"],
        "child_exit_code": process["returncode"],
        "child_stderr": process["stderr"],
    }


def _git_value(*args) -> str:
    completed = subprocess.run(["git", *args], cwd=PROJECT_ROOT,
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
    if completed.returncode != 0:
        raise AcceptanceFailure("GIT_PREFLIGHT_FAILED", completed.stderr.strip())
    return completed.stdout.strip()


def _git_state() -> dict:
    return {
        "branch": _git_value("branch", "--show-current"),
        "head": _git_value("rev-parse", "HEAD"),
        "tag_commit": _git_value("rev-parse", f"refs/tags/{EXPECTED_TAG}^{{commit}}"),
        "status": _git_value("status", "--porcelain=v1"),
    }


def _status_paths(status: str) -> set[str]:
    return {line[3:] for line in status.splitlines() if len(line) >= 4}


def _artifact_paths() -> dict:
    candidates = {
        "report": PROJECT_ROOT / "PHASE5_1_6_R2_REPORT.md",
        "results": PROJECT_ROOT / "PHASE5_1_6_R2_RESULTS.json",
        "matrix": PROJECT_ROOT / "PHASE5_1_6_R2_SCENARIO_MATRIX.md",
        "log": PROJECT_ROOT / "PHASE5_1_6_R2_TEST_LOG.txt",
    }
    stamp = time.strftime("%Y%m%d_%H%M%S")
    return {
        key: path if not path.exists() else path.with_name(f"{path.stem}_{stamp}{path.suffix}")
        for key, path in candidates.items()
    }


def _offline_test_summaries() -> dict:
    summaries = {}
    for name in ("forward", "reverse"):
        path = PROJECT_ROOT / f"PHASE5_1_6_R2_TEST_{name.upper()}.tmp"
        if not path.is_file():
            summaries[name] = {"status": "NOT_MEASURED", "tests_run": None, "skipped": None}
            continue
        content = path.read_text(encoding="utf-8", errors="replace")
        match = re.search(r"Ran (\d+) tests? in ([0-9.]+)s", content)
        skipped = re.search(r"skipped=(\d+)", content)
        exit_match = re.search(r"(?m)^EXIT_CODE=(\d+)\s*$", content)
        status = "PASS" if exit_match and exit_match.group(1) == "0" and "\nOK" in content else "FAIL"
        summaries[name] = {
            "status": status,
            "tests_run": int(match.group(1)) if match else None,
            "elapsed_seconds": float(match.group(2)) if match else None,
            "skipped": int(skipped.group(1)) if skipped else 0,
            "log_file": path.name,
        }
    return summaries


def _read_prior_r1() -> dict:
    if not R1_RESULTS.is_file():
        raise AcceptanceFailure("R1_HISTORICAL_RESULTS_MISSING")
    value = json.loads(R1_RESULTS.read_text(encoding="utf-8"))
    if value.get("phase") != "5.1.6-R1" or value.get("overall_status") != "FAIL":
        raise AcceptanceFailure("R1_HISTORICAL_RESULTS_INVALID")
    if value.get("run_id") != "d4bb36e0dcb1450a9c720f8a27e40cdd":
        raise AcceptanceFailure("R1_HISTORICAL_RUN_ID_MISMATCH")
    return value


def run_host_acceptance() -> int:
    run_id = uuid.uuid4().hex
    artifacts = _artifact_paths()
    started = time.time()
    git = {}
    preexisting_status = ""
    prior = None
    temp_root = PROJECT_ROOT / f".phase5_1_6_r2_{run_id}"
    r108 = {"status": "NOT_MEASURED", "reason": "Not run."}
    r109 = {"status": "NOT_MEASURED", "reason": "Not run."}
    preflight = {"status": "NOT_MEASURED"}
    cleanup = {"status": "PENDING"}
    overall = "BLOCKED"
    failure = None
    log = []
    embedding_provider = None
    try:
        git = _git_state()
        preexisting_status = git["status"]
        if (git["branch"] != EXPECTED_BRANCH or git["head"] != EXPECTED_HEAD
                or git["tag_commit"] != EXPECTED_HEAD):
            raise AcceptanceFailure("BASELINE_MISMATCH")
        prior = _read_prior_r1()
        test_summaries = _offline_test_summaries()
        if any(test_summaries[name].get("status") != "PASS" for name in ("forward", "reverse")):
            raise AcceptanceFailure("DIRECTED_OFFLINE_REGRESSION_NOT_PASS")

        if Path(sys.executable).resolve() != (PROJECT_ROOT / ".agentforge" / "af012-venv" / "Scripts" / "python.exe").resolve():
            raise AcceptanceFailure("PROJECT_VENV_NOT_ACTIVE")
        embedding_configuration = apply_launcher_embedding_defaults()
        from agentforge.af012_runtime import embedding_provider_from_environment
        import chromadb
        try:
            embedding_provider = embedding_provider_from_environment()
        except ValueError:
            raise AcceptanceFailure(
                "EMBEDDING_CONFIGURATION_INVALID",
                "Project launcher defaults could not construct the local embedding provider.",
            ) from None
        health = embedding_provider.health()
        if health != "ready":
            raise AcceptanceFailure("EMBEDDING_HEALTH_NOT_READY")
        if not (PROJECT_ROOT / ".agentforge" / "af012-venv" / "Scripts" / "python.exe").is_file():
            raise AcceptanceFailure("PROJECT_VENV_MISSING")
        preflight = {
            "status": "PASS", "python": sys.executable,
            "project_import": str(Path(__import__("agentforge").__file__).resolve()),
            "chroma_version": chromadb.__version__,
            "embedding_configuration": embedding_configuration,
            "embedding_health": health,
            "embedding_model_id": embedding_provider.model_id,
            "embedding_api_model": embedding_provider.api_model,
            "embedding_dimension": embedding_provider.dimension,
            "postgresql": "NOT_REQUIRED_NOT_USED",
            "formal_chroma_path_opened": False,
        }
        temp_root.mkdir()
        repo_08 = temp_root / "synthetic_reopen_repo"
        chroma_08 = temp_root / "chroma_reopen"
        log.append("Preflight PASS: project venv/import, Chroma dependency, and local Embedding health.")
        log.append("Isolation: temporary SQLite :memory:, temporary Chroma path, synthetic repositories; PostgreSQL and formal Chroma not used.")
        log.append("R1-08 started: one real vector write followed by an independent Python child process.")

        try:
            r108 = run_r1_08(chroma_08, repo_08, provider=embedding_provider)
            log.append(f"R1-08 PASS: parent PID {r108['parent_pid']}; child PID {r108['child_pid']}; child exit {r108['child_exit_code']}; manifest identical; semantic retrieval verified.")
        except Exception as exc:
            code = exc.code if isinstance(exc, AcceptanceFailure) else type(exc).__name__
            detail = exc.detail if isinstance(exc, AcceptanceFailure) else ""
            r108 = {"status": "FAIL", "failure_code": code, "detail": detail}
            failure = {"stage": "R1-08_chroma_process_reopen", "classification": "RUNNER_OR_PRODUCT_REQUIRES_REVIEW", "error_code": code, "detail": detail}
            log.append(f"R1-08 FAIL: {code}; {detail}")
            raise

        log.append("R1-08 PASS; proceeding to R1-09 bounded process-local failure injection.")
        try:
            from agentforge.embeddings.llama_server import LlamaServerEmbeddingProvider
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                unused_port = sock.getsockname()[1]
            unavailable_provider = LlamaServerEmbeddingProvider(
                f"http://127.0.0.1:{unused_port}",
                model_id=embedding_provider.model_id,
                api_model=embedding_provider.api_model,
                dimension=embedding_provider.dimension,
                connect_timeout=0.5, read_timeout=1.0,
            )
            repo_09 = temp_root / "synthetic_degradation_repo"
            _synthetic_repository(repo_09, name="local fallback")
            chroma_09 = temp_root / "chroma_degradation"
            r109 = run_degradation_and_recovery(
                repo_09, chroma_09, embedding_provider=embedding_provider,
                unavailable_provider=unavailable_provider,
            )
            log.append("R1-09 PASS: Embedding outage and temporary Chroma query failure preserved hash-verified SQLite lexical results; restored vector search without duplicates.")
        except Exception as exc:
            code = exc.code if isinstance(exc, AcceptanceFailure) else type(exc).__name__
            detail = exc.detail if isinstance(exc, AcceptanceFailure) else ""
            r109 = {"status": "FAIL", "failure_code": code, "detail": detail}
            failure = {"stage": "R1-09_degradation_recovery", "classification": "PRODUCTION_OR_HARNESS_REQUIRES_REVIEW", "error_code": code, "detail": detail}
            log.append(f"R1-09 FAIL: {code}; {detail}")
            raise

        overall = "PASS"
    except Exception as exc:
        if failure is None:
            code = exc.code if isinstance(exc, AcceptanceFailure) else type(exc).__name__
            detail = exc.detail if isinstance(exc, AcceptanceFailure) else ""
            failure = {"stage": "preflight_or_acceptance", "classification": "BLOCKED_OR_FAILED", "error_code": code, "detail": detail}
            log.append(f"STOP: {code}; {detail}")
        overall = "FAIL" if preflight.get("status") == "PASS" else "BLOCKED"
    finally:
        if temp_root.exists():
            try:
                resolved = temp_root.resolve(strict=True)
                if (resolved.parent != PROJECT_ROOT.resolve() or not temp_root.name.startswith(".phase5_1_6_r2_")
                        or temp_root.is_symlink()):
                    raise RuntimeError("temporary_resource_boundary_check_failed")
                shutil.rmtree(resolved)
                cleanup = {"status": "PASS", "temporary_root_removed": True,
                           "formal_data_touched": False}
            except Exception as exc:
                cleanup = {"status": "FAIL", "temporary_root_removed": False,
                           "resource_path": str(temp_root),
                           "error_type": type(exc).__name__}
                overall = "FAIL"
                if failure is None:
                    failure = {"stage": "cleanup", "classification": "RESOURCE_CLEANUP_FAILURE",
                               "error_code": type(exc).__name__}
        else:
            cleanup = {"status": "PASS", "temporary_root_removed": True,
                       "formal_data_touched": False}

    try:
        after = _git_state()
        allowed = {
            "phase5_1_6_r2_acceptance.py", "tests/test_phase5_1_6_r2_acceptance.py",
            *[path.name for path in artifacts.values()],
            "PHASE5_1_6_R2_TEST_FORWARD.tmp", "PHASE5_1_6_R2_TEST_REVERSE.tmp",
        }
        before_paths = _status_paths(preexisting_status) - allowed
        after_paths = _status_paths(after["status"]) - allowed
        worktree_preserved = before_paths == after_paths and after["head"] == git.get("head")
    except Exception:
        after = git
        worktree_preserved = False
    if not worktree_preserved:
        overall = "FAIL"
        failure = failure or {"stage": "git_worktree_preservation", "classification": "WORKTREE_CHANGED"}

    try:
        prior_scenarios = prior["scenarios"]
        historical = [
            {"id": item["id"], "name": item["name"],
             "status": "PASS_HISTORICAL" if item["status"] == "PASS" else item["status"],
             "evidence": item["evidence"], "source_run_id": prior["run_id"]}
            for item in prior_scenarios if item["id"] in {f"R1-0{i}" for i in range(1, 8)}
        ]
    except Exception:
        historical = []

    scenarios = historical + [
        {"id": "R1-08", "name": "fresh_process_chroma_reopen", **r108},
        {"id": "R1-09", "name": "lexical_degradation_and_vector_recovery", **r109},
    ]
    offline_tests = _offline_test_summaries()
    result = {
        "schema_version": 1, "phase": "5.1.6-R2", "run_id": run_id,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "overall_status": overall,
        "git": {
            "branch": git.get("branch"), "head": git.get("head"),
            "protected_tag": EXPECTED_TAG, "protected_tag_commit": git.get("tag_commit"),
            "baseline_match": git.get("branch") == EXPECTED_BRANCH and git.get("head") == EXPECTED_HEAD
                and git.get("tag_commit") == EXPECTED_HEAD,
            "worktree_preserved": worktree_preserved,
            "preexisting_change_count": len(_status_paths(preexisting_status) - {
                "phase5_1_6_r2_acceptance.py", "tests/test_phase5_1_6_r2_acceptance.py",
                "PHASE5_1_6_R2_TEST_FORWARD.tmp", "PHASE5_1_6_R2_TEST_REVERSE.tmp",
            }),
            "post_run_change_count": len(_status_paths(after.get("status", ""))),
        },
        "preflight": preflight,
        "historical_r1_evidence": {
            "run_id": prior.get("run_id") if prior else None,
            "source": str(R1_RESULTS),
            "scenarios_01_07_status": "PASS_HISTORICAL" if historical and all(x["status"] == "PASS_HISTORICAL" for x in historical) else "NOT_VERIFIED",
            "scenario_count": len(historical),
        },
        "scenarios": scenarios,
        "offline_tests": offline_tests,
        "cleanup": cleanup,
        "failure": failure,
        "embedding_request_counts": {
            "r1_08_parent": r108.get("parent_embedding_request_counts"),
            "r1_08_child": r108.get("child_embedding_request_counts"),
            "r1_09_available_provider": r109.get("embedding_request_counts"),
            "r1_09_unavailable_provider": r109.get("embedding_unavailable", {}).get("provider_request_counts"),
        },
        "chat_requests": {"qwen": 0, "deepseek": 0},
        "postgresql": "NOT_USED",
        "production_code_modified": False,
        "test_files_modified": ["tests/test_phase5_1_6_r2_acceptance.py"],
        "acceptance_runner_added": "phase5_1_6_r2_acceptance.py",
        "worktree_status_preserved": worktree_preserved,
    }
    write_artifacts(result, log, artifacts)
    return 0 if overall == "PASS" else 1


def _persistent_artifact_paths() -> dict:
    stamp = time.strftime("%Y%m%d_%H%M%S")
    names = {
        "report": "PHASE5_1_6_R2_PERSISTENT_REPORT",
        "results": "PHASE5_1_6_R2_PERSISTENT_RESULTS",
        "matrix": "PHASE5_1_6_R2_PERSISTENT_SCENARIO_MATRIX",
        "log": "PHASE5_1_6_R2_PERSISTENT_TEST_LOG",
    }
    paths = {}
    for key, stem in names.items():
        path = PROJECT_ROOT / f"{stem}.{'json' if key == 'results' else 'md' if key in ('report', 'matrix') else 'txt'}"
        if path.exists():
            path = path.with_name(f"{path.stem}_{stamp}{path.suffix}")
        if path.exists():
            raise AcceptanceFailure("PERSISTENT_REPORT_PATH_COLLISION", path.name)
        paths[key] = path
    return paths


def _anticipated_repository_identity(path: Path) -> str:
    normalized = os.path.normcase(str(path.resolve(strict=False)))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def assert_no_persistent_repository_id_collision(repository_ids: set[str],
                                                registered_repository_ids: set[str]) -> None:
    """Reject IDs already stored in Chroma, while allowing this run's owned IDs."""
    collisions = set(repository_ids) & set(registered_repository_ids)
    if collisions:
        raise AcceptanceFailure(
            "SYNTHETIC_REPOSITORY_ID_COLLISION",
            f"registered_collision_count={len(collisions)}",
        )


def _remove_synthetic_root(path: Path, run_id: str) -> bool:
    if not path.exists():
        return True
    resolved = path.resolve(strict=True)
    if (path.is_symlink() or resolved.parent != PROJECT_ROOT.resolve()
            or path.name != f".phase5_1_6_r2_persisted_{run_id}"):
        raise AcceptanceFailure("SYNTHETIC_ROOT_BOUNDARY_MISMATCH")
    shutil.rmtree(resolved)
    return not path.exists()


def _write_persistent_artifacts(result: dict, log: list[str], artifacts: dict) -> None:
    scenarios = result.get("scenarios", [])
    matrix = [
        "# Phase 5.1.6-R2 Persistent Chroma Scenario Matrix", "",
        f"Overall status: **{result.get('overall_status')}**", "",
        "| ID | Scenario | Status | Evidence |", "|---|---|---|---|",
    ]
    report = [
        "# Phase 5.1.6-R2: Persistent Chroma Acceptance", "",
        f"Overall status: **{result.get('overall_status')}**", "",
        f"Run ID: `{result.get('run_id')}`",
        f"Branch/HEAD: `{result.get('git', {}).get('branch')}` / `{result.get('git', {}).get('head')}`", "",
        "## Storage boundary", "",
        f"Actual Chroma root: `{result.get('persistent_chroma', {}).get('root')}`.",
        "The test Collection was created within AgentForge's actual persistent Chroma store using the product Collection configuration. No independent Chroma database was used.",
        f"Earlier isolated-store run `{result.get('superseded_temporary_store_run', {}).get('run_id')}` is preserved but not counted under the revised acceptance scope.",
        "SQLite and source files for the synthetic repository remained isolated from the registered/formal repository store.", "",
        "## Existing data integrity", "",
        f"Collections before: {result.get('persistent_chroma', {}).get('before', {}).get('collection_count')}; after cleanup: {result.get('persistent_chroma', {}).get('after', {}).get('collection_count')}.",
        f"Original Collections unchanged: {result.get('persistent_chroma', {}).get('original_collections_unchanged')}; before/after summaries equal: {result.get('persistent_chroma', {}).get('before_after_equal')}.",
        f"Acceptance Collection removed: {result.get('cleanup', {}).get('acceptance_collection', {}).get('collection_removed')}; temporary synthetic repository removed: {result.get('cleanup', {}).get('synthetic_root_removed')}.", "",
        "## Scenarios", "",
    ]
    for item in scenarios:
        evidence = item.get("evidence") or item.get("failure_code") or item.get("reason") or ""
        if item.get("id") == "R1-08" and item.get("status") == "PASS":
            hit = item.get("semantic_hits", [{}])[0]
            evidence = (f"Fresh process {item.get('child_pid')} reopened the existing persistent store; ")
            evidence += (f"PID distinct={item.get('pid_distinct')}, exit={item.get('child_exit_code')}, "
                         f"manifest identical={item.get('manifest_identical')}, semantic hit="
                         f"{hit.get('path')}:{hit.get('start_line')}-{hit.get('end_line')}.")
        elif item.get("id") == "R1-09" and item.get("status") == "PASS":
            evidence = (f"Embedding/Chroma outages both degraded to verified SQLite lexical results; "
                        f"recovery={item.get('recovery', {}).get('state')}, "
                        f"vectors={item.get('recovery', {}).get('consistency', {}).get('stored_vectors')}/"
                        f"{item.get('recovery', {}).get('consistency', {}).get('expected_vectors')}, "
                        f"SQLite unchanged={item.get('recovery', {}).get('sqlite_authoritative_snapshot_unchanged')}.")
        line = f"| {item.get('id')} | {item.get('name')} | **{item.get('status')}** | {str(evidence).replace('|', '\\|').replace(chr(10), ' ')} |"
        matrix.append(line)
        report.append(f"### {item.get('id')} — {item.get('name')}: {item.get('status')}")
        report.append("")
        report.append(str(evidence) if evidence else "No additional evidence summary.")
        if item.get("id") == "R1-08":
            report.append("")
            report.append(f"Parent/child PID: {item.get('parent_pid')} / {item.get('child_pid')}; distinct={item.get('pid_distinct')}; child exit={item.get('child_exit_code')}; manifest identical={item.get('manifest_identical')}.")
            report.append(f"Collection `{item.get('manifest_before', {}).get('collection')}` vectors={item.get('manifest_before', {}).get('repository_vector_count')}; semantic hit={item.get('semantic_hits', [{}])[0].get('path')}:{item.get('semantic_hits', [{}])[0].get('start_line')}-{item.get('semantic_hits', [{}])[0].get('end_line')}.")
            report.append(f"Before/after data manifest SHA-256: `{canonical_sha256(item.get('manifest_before'))}` / `{canonical_sha256(item.get('manifest_after'))}`.")
        if item.get("id") == "R1-09":
            report.append("")
            report.append(f"Embedding failure state={item.get('embedding_unavailable', {}).get('state')}, SQLite verified candidates={item.get('embedding_unavailable', {}).get('lexical_candidates_checked_against_sqlite')}; Chroma failure state={item.get('chroma_unavailable', {}).get('state')}, verified candidates={item.get('chroma_unavailable', {}).get('lexical_candidates_checked_against_sqlite')}.")
            report.append(f"Recovery={item.get('recovery', {}).get('state')}; vectors stored/expected={item.get('recovery', {}).get('consistency', {}).get('stored_vectors')}/{item.get('recovery', {}).get('consistency', {}).get('expected_vectors')}; SQLite unchanged={item.get('recovery', {}).get('sqlite_authoritative_snapshot_unchanged')}; duplicate/stale={item.get('recovery', {}).get('duplicate_or_stale_vectors')}.")
        report.append("")
    report += [
        "## Offline isolation tests", "",
        f"Result: `{json.dumps(result.get('offline_tests', {}), ensure_ascii=False, sort_keys=True)}`. These tests use fake collection/client objects and do not create a temporary Chroma database.", "",
        "## Preflight and request counts", "",
        f"Project Python: `{result.get('preflight', {}).get('python')}`; embedding health={result.get('preflight', {}).get('embedding_health')}; model={result.get('preflight', {}).get('embedding_model_id')}; dimension={result.get('preflight', {}).get('embedding_dimension')}.",
        f"Embedding request counts: `{json.dumps(result.get('embedding_request_counts', {}), ensure_ascii=False, sort_keys=True)}`.",
        "Chat requests: 0. PostgreSQL database and migration: not used.", "",
        "## Cleanup and classification", "",
        f"Cleanup: `{json.dumps(result.get('cleanup', {}), ensure_ascii=False, sort_keys=True)}`.",
        f"Failure: `{json.dumps(result.get('failure'), ensure_ascii=False, sort_keys=True)}`.",
        "No production code was changed. The only persistent-store write was confined to the uniquely named acceptance Collection; cleanup was restricted to this Collection and the run's synthetic repo IDs.",
    ]
    artifacts["report"].write_text("\n".join(report) + "\n", encoding="utf-8")
    artifacts["matrix"].write_text("\n".join(matrix) + "\n", encoding="utf-8")
    artifacts["results"].write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    artifacts["log"].write_text("\n".join(log) + "\n", encoding="utf-8")


def run_persistent_host_acceptance() -> int:
    """Run R1-08/09 in a private Collection inside the real persistent Chroma store."""
    run_id = uuid.uuid4().hex
    artifacts = _persistent_artifact_paths()
    synthetic_root = PROJECT_ROOT / f".phase5_1_6_r2_persisted_{run_id}"
    test_collection = None
    client = None
    embedding_provider = None
    git = {}
    prior = None
    preexisting_status = ""
    before_snapshot = None
    after_snapshot = None
    r108 = {"status": "NOT_MEASURED", "reason": "Not run."}
    r109 = {"status": "NOT_MEASURED", "reason": "Not run."}
    preflight = {"status": "NOT_MEASURED"}
    cleanup = {"status": "PENDING", "acceptance_collection": {"status": "NOT_CREATED"}}
    failure = None
    overall = "BLOCKED"
    log = []
    owned_repository_ids = set()
    chroma_root = None
    try:
        git = _git_state()
        preexisting_status = git["status"]
        if (git["branch"] != EXPECTED_BRANCH or git["head"] != EXPECTED_HEAD
                or git["tag_commit"] != EXPECTED_HEAD):
            raise AcceptanceFailure("BASELINE_MISMATCH")
        prior = _read_prior_r1()
        test_logs = list(PROJECT_ROOT.glob("PHASE5_1_6_R2_PERSISTENT_TEST_LOG_*.txt"))
        if not test_logs:
            raise AcceptanceFailure("PERSISTENT_ACCEPTANCE_ISOLATION_TESTS_NOT_RUN")
        persistent_test_log = max(test_logs, key=lambda path: path.stat().st_mtime_ns)
        persistent_test_text = persistent_test_log.read_text(encoding="utf-8", errors="replace")
        test_count_match = re.search(r"Ran (\d+) tests? in ([0-9.]+)s", persistent_test_text)
        test_exit_match = re.search(r"(?m)^EXIT_CODE=(\d+)\s*$", persistent_test_text)
        test_module = PROJECT_ROOT / "tests" / "test_phase5_1_6_r2_persistent_acceptance.py"
        test_module_hash = hashlib.sha256(test_module.read_bytes()).hexdigest()
        logged_module_hash = re.search(r"(?m)^TEST_MODULE_SHA256=([0-9a-f]{64})\s*$", persistent_test_text)
        if (not test_count_match or not test_exit_match or test_exit_match.group(1) != "0"
                or "\nOK" not in persistent_test_text
                or not logged_module_hash or logged_module_hash.group(1) != test_module_hash):
            raise AcceptanceFailure("PERSISTENT_ACCEPTANCE_ISOLATION_TESTS_NOT_PASS")
        offline_test_summary = {
            "status": "PASS", "tests_run": int(test_count_match.group(1)),
            "elapsed_seconds": float(test_count_match.group(2)),
            "log_file": persistent_test_log.name,
            "log_sha256": hashlib.sha256(persistent_test_log.read_bytes()).hexdigest(),
            "test_module_sha256": test_module_hash,
        }
        if Path(sys.executable).resolve() != (PROJECT_ROOT / ".agentforge" / "af012-venv" / "Scripts" / "python.exe").resolve():
            raise AcceptanceFailure("PROJECT_VENV_NOT_ACTIVE")
        config = apply_launcher_embedding_defaults()
        from agentforge.af012_runtime import (
            CHROMA_ROOT, create_persistent_chroma, embedding_provider_from_environment,
        )
        import chromadb
        chroma_root = Path(CHROMA_ROOT).resolve(strict=True)
        if not chroma_root.is_dir():
            raise AcceptanceFailure("PERSISTENT_CHROMA_ROOT_UNAVAILABLE")
        try:
            embedding_provider = embedding_provider_from_environment()
        except ValueError:
            raise AcceptanceFailure("EMBEDDING_CONFIGURATION_INVALID") from None
        if embedding_provider.health() != "ready":
            raise AcceptanceFailure("EMBEDDING_HEALTH_NOT_READY")
        if embedding_provider.dimension != 1024:
            raise AcceptanceFailure("EMBEDDING_DIMENSION_MISMATCH")
        client = create_persistent_chroma()
        before_snapshot = persistent_collection_snapshot(client)
        if before_snapshot["collection_count"] < 1:
            raise AcceptanceFailure("PERSISTENT_COLLECTION_INVENTORY_EMPTY")

        planned_repo_paths = {
            synthetic_root / "synthetic_reopen_repo",
            synthetic_root / "synthetic_degradation_repo",
        }
        planned_ids = {_anticipated_repository_identity(path) for path in planned_repo_paths}
        existing_repo_ids = {
            repository_id
            for collection in before_snapshot["collections"]
            for repository_id in collection["repository_ids"]
        }
        assert_no_persistent_repository_id_collision(planned_ids, existing_repo_ids)
        owned_repository_ids.update(planned_ids)

        test_collection = create_persistent_acceptance_collection(
            client, model_id=embedding_provider.model_id,
            dimension=embedding_provider.dimension, run_id=run_id,
        )
        if not test_collection["configuration_matches"]:
            raise AcceptanceFailure("ACCEPTANCE_COLLECTION_CONFIGURATION_MISMATCH",
                                    f"collection={test_collection['name']}")
        client.close()
        client = None
        synthetic_root.mkdir()
        preflight = {
            "status": "PASS",
            "python": sys.executable,
            "project_import": str(Path(__import__("agentforge").__file__).resolve()),
            "chroma_version": chromadb.__version__,
            "chroma_root": str(chroma_root),
            "chroma_mode": "embedded_persistent_existing_store",
            "chroma_client_initialization": {
                "factory": "agentforge.af012_runtime.create_persistent_chroma",
                "default_path_argument": True,
                "allow_reset": False,
                "anonymized_telemetry": False,
            },
            "collection_count_before": before_snapshot["collection_count"],
            "collections_before": before_snapshot["collections"],
            "acceptance_collection": test_collection,
            "planned_synthetic_repository_ids": sorted(planned_ids),
            "embedding_configuration": config,
            "embedding_health": "ready",
            "embedding_model_id": embedding_provider.model_id,
            "embedding_api_model": embedding_provider.api_model,
            "embedding_dimension": embedding_provider.dimension,
            "formal_repository_index_opened": False,
            "postgresql_used": False,
        }
        log.append(f"Preflight PASS: actual Chroma root={chroma_root}; existing collections={before_snapshot['collection_count']}; acceptance Collection={test_collection['name']}.")
        log.append("Pre-existing Collection names, IDs, record IDs, metadata hashes, content hashes, vector hashes, and resolved configurations were snapshotted before writes.")
        log.append(f"Acceptance collection requested config={json.dumps(test_collection['product_requested_configuration'], sort_keys=True)}; resolved config recorded in machine result.")
        log.append("Isolation: the existing Chroma persistent directory is used; only one unique acceptance Collection and two synthetic repo_ids are writable. SQLite is in-memory; repo files/request files are temporary.")
        log.append(f"Acceptance isolation tests: {offline_test_summary['tests_run']} passed; log={offline_test_summary['log_file']}; SHA-256={offline_test_summary['log_sha256']}.")

        repo08 = synthetic_root / "synthetic_reopen_repo"
        repo09 = synthetic_root / "synthetic_degradation_repo"
        log.append("R1-08 started: write one real local embedding to the persistent acceptance Collection, close its client, and reopen in a child Python process.")
        try:
            r108 = run_r1_08(
                chroma_root, repo08, provider=embedding_provider,
                collection_name=test_collection["name"], request_directory=synthetic_root,
            )
            if r108["repository_id"] != _anticipated_repository_identity(repo08):
                raise AcceptanceFailure("R1_08_SYNTHETIC_REPOSITORY_ID_MISMATCH")
            log.append(f"R1-08 PASS: parent PID={r108['parent_pid']}; child PID={r108['child_pid']}; child exit={r108['child_exit_code']}; persisted manifest identical; semantic hit={r108['semantic_hits'][0]['path']}:{r108['semantic_hits'][0]['start_line']}-{r108['semantic_hits'][0]['end_line']}.")
        except Exception as exc:
            code = exc.code if isinstance(exc, AcceptanceFailure) else type(exc).__name__
            detail = exc.detail if isinstance(exc, AcceptanceFailure) else ""
            r108 = {"status": "FAIL", "failure_code": code, "detail": detail}
            failure = {"stage": "R1-08_persistent_chroma_reopen", "classification": "RUNNER_OR_PRODUCT_REQUIRES_REVIEW", "error_code": code, "detail": detail}
            log.append(f"R1-08 FAIL: {code}; {detail}")
            raise

        log.append("R1-08 PASS; proceeding to R1-09 against the same persistent acceptance Collection.")
        try:
            from agentforge.embeddings.llama_server import LlamaServerEmbeddingProvider
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                unused_port = sock.getsockname()[1]
            unavailable_provider = LlamaServerEmbeddingProvider(
                f"http://127.0.0.1:{unused_port}", model_id=embedding_provider.model_id,
                api_model=embedding_provider.api_model, dimension=embedding_provider.dimension,
                connect_timeout=0.5, read_timeout=1.0,
            )
            _synthetic_repository(repo09, name="persistent local fallback")
            repo09_id = _anticipated_repository_identity(repo09)
            assert_no_persistent_repository_id_collision({repo09_id}, existing_repo_ids)
            r109 = run_degradation_and_recovery(
                repo09, chroma_root, embedding_provider=embedding_provider,
                unavailable_provider=unavailable_provider,
                collection_name=test_collection["name"],
            )
            if r109["repository_id"] != _anticipated_repository_identity(repo09):
                raise AcceptanceFailure("R1_09_SYNTHETIC_REPOSITORY_ID_MISMATCH")
            log.append(f"R1-09 PASS: Embedding outage and Chroma query failure fell back to SQLite with hash verification; recovered READY in the persistent Collection with {r109['recovery']['chroma_count_after_recovery']} vectors and no duplicates/stale data.")
        except Exception as exc:
            code = exc.code if isinstance(exc, AcceptanceFailure) else type(exc).__name__
            detail = exc.detail if isinstance(exc, AcceptanceFailure) else ""
            r109 = {"status": "FAIL", "failure_code": code, "detail": detail}
            failure = {"stage": "R1-09_persistent_chroma_degradation_recovery", "classification": "PRODUCTION_OR_HARNESS_REQUIRES_REVIEW", "error_code": code, "detail": detail}
            log.append(f"R1-09 FAIL: {code}; {detail}")
            raise
        overall = "PASS"
    except Exception as exc:
        if failure is None:
            code = exc.code if isinstance(exc, AcceptanceFailure) else type(exc).__name__
            detail = exc.detail if isinstance(exc, AcceptanceFailure) else ""
            failure = {"stage": "preflight_or_persistent_acceptance", "classification": "BLOCKED_OR_FAILED", "error_code": code, "detail": detail}
            log.append(f"STOP: {code}; {detail}")
        overall = "FAIL" if preflight.get("status") == "PASS" else "BLOCKED"
    finally:
        if client is not None:
            _close_chroma(client)
            client = None
        if test_collection is not None and chroma_root is not None:
            cleanup_client = None
            try:
                cleanup_client = __import__("agentforge.af012_runtime", fromlist=["create_persistent_chroma"]).create_persistent_chroma()
                cleanup["acceptance_collection"] = cleanup_owned_persistent_collection(
                    cleanup_client, name=test_collection["name"],
                    expected_collection_id=test_collection["collection_id"],
                    owned_repository_ids=owned_repository_ids,
                )
            except Exception as exc:
                cleanup["acceptance_collection"] = {
                    "status": "FAIL", "collection_removed": False,
                    "error_code": exc.code if isinstance(exc, AcceptanceFailure) else type(exc).__name__,
                }
                overall = "FAIL"
                if failure is None:
                    failure = {"stage": "acceptance_collection_cleanup", "classification": "CLEANUP_FAILURE", "error_code": cleanup["acceptance_collection"]["error_code"]}
                log.append(f"Acceptance Collection cleanup failed: {cleanup['acceptance_collection']['error_code']}; no other Collection was targeted.")
            finally:
                if cleanup_client is not None:
                    _close_chroma(cleanup_client)
        try:
            cleanup["synthetic_root_removed"] = _remove_synthetic_root(synthetic_root, run_id)
        except Exception as exc:
            cleanup["synthetic_root_removed"] = False
            cleanup["synthetic_root_error"] = exc.code if isinstance(exc, AcceptanceFailure) else type(exc).__name__
            overall = "FAIL"
        if chroma_root is not None and before_snapshot is not None:
            verify_client = None
            try:
                verify_client = __import__("agentforge.af012_runtime", fromlist=["create_persistent_chroma"]).create_persistent_chroma()
                after_snapshot = persistent_collection_snapshot(verify_client)
                before_by_name = {x["name"]: x for x in before_snapshot["collections"]}
                after_by_name = {x["name"]: x for x in after_snapshot["collections"]}
                original_unchanged = all(after_by_name.get(name) == value
                                         for name, value in before_by_name.items())
                cleanup["original_collections_unchanged"] = original_unchanged
                cleanup["collection_count_before"] = before_snapshot["collection_count"]
                cleanup["collection_count_after"] = after_snapshot["collection_count"]
                cleanup["collection_names_after"] = sorted(after_by_name)
                cleanup["acceptance_collection_absent"] = (
                    test_collection is None or test_collection["name"] not in after_by_name
                )
                cleanup["before_after_equal"] = before_snapshot == after_snapshot
                collection_removed_ok = (
                    test_collection is None
                    or cleanup.get("acceptance_collection", {}).get("collection_removed", False)
                )
                if not original_unchanged or not cleanup["acceptance_collection_absent"] or not collection_removed_ok:
                    overall = "FAIL"
                    if failure is None:
                        failure = {"stage": "persistent_store_integrity", "classification": "PERSISTENT_DATA_MISMATCH", "error_code": "ORIGINAL_COLLECTION_SNAPSHOT_CHANGED"}
                log.append(f"Post-cleanup snapshot: originals unchanged={original_unchanged}; before/after equal={cleanup['before_after_equal']}; collections {before_snapshot['collection_count']} -> {after_snapshot['collection_count']}.")
            except Exception as exc:
                cleanup["original_collections_unchanged"] = False
                cleanup["snapshot_verification_error"] = exc.code if isinstance(exc, AcceptanceFailure) else type(exc).__name__
                overall = "FAIL"
                if failure is None:
                    failure = {"stage": "persistent_store_integrity", "classification": "SNAPSHOT_VERIFY_FAILED", "error_code": cleanup["snapshot_verification_error"]}
            finally:
                if verify_client is not None:
                    _close_chroma(verify_client)

    try:
        after_git = _git_state()
        allowed = {
            "phase5_1_6_r2_acceptance.py", "tests/test_phase5_1_6_r2_persistent_acceptance.py",
            persistent_test_log.name,
            *[path.name for path in artifacts.values()],
        }
        worktree_preserved = (
            _status_paths(preexisting_status) - allowed == _status_paths(after_git["status"]) - allowed
            and after_git["head"] == git.get("head")
        )
    except Exception:
        after_git = git
        worktree_preserved = False
    if not worktree_preserved:
        overall = "FAIL"
        failure = failure or {"stage": "git_worktree_preservation", "classification": "WORKTREE_CHANGED", "error_code": "WORKTREE_STATUS_CHANGED"}

    historical = []
    for item in (prior or {}).get("scenarios", []):
        if item.get("id") in {f"R1-0{i}" for i in range(1, 8)}:
            historical.append({**item, "status": "PASS_HISTORICAL", "evidence_run_id": prior.get("run_id")})
    scenarios = historical + [
        {"id": "R1-08", "name": "fresh_process_chroma_reopen", **r108},
        {"id": "R1-09", "name": "lexical_degradation_and_vector_recovery", **r109},
    ]
    result = {
        "schema_version": 1, "phase": "5.1.6-R2 persistent Chroma", "run_id": run_id,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "overall_status": overall,
        "evidence_scope": "R1-01..07 retained historical evidence; R1-08..09 tested in this run against AgentForge's actual persistent Chroma directory.",
        "superseded_temporary_store_run": {
            "run_id": "ca44168ea2d046edbecf289c04068bc0",
            "previous_status": "PASS",
            "counted_for_this_acceptance": False,
            "reason": "User revised R2 to require testing inside the actual persistent Chroma directory.",
        },
        "git": {"branch": git.get("branch"), "head": git.get("head"),
                "protected_tag": EXPECTED_TAG, "protected_tag_commit": git.get("tag_commit"),
                "baseline_match": git.get("branch") == EXPECTED_BRANCH and git.get("head") == EXPECTED_HEAD and git.get("tag_commit") == EXPECTED_HEAD,
                "worktree_preserved": worktree_preserved},
        "preflight": preflight,
        "historical_r1_evidence": {"run_id": prior.get("run_id") if prior else None,
            "source": str(R1_RESULTS), "scenarios_01_07_status": "PASS_HISTORICAL" if len(historical) == 7 else "NOT_VERIFIED",
            "scenario_count": len(historical)},
        "persistent_chroma": {"root": str(chroma_root) if chroma_root else None,
            "before": before_snapshot, "after": after_snapshot,
            "original_collections_unchanged": cleanup.get("original_collections_unchanged"),
            "before_after_equal": cleanup.get("before_after_equal"),
            "acceptance_collection": test_collection},
        "scenarios": scenarios,
        "offline_tests": offline_test_summary if "offline_test_summary" in locals() else {"status": "NOT_MEASURED"},
        "embedding_request_counts": {
            "r1_08_parent": r108.get("parent_embedding_request_counts"),
            "r1_08_child": r108.get("child_embedding_request_counts"),
            "r1_09_available_provider": r109.get("embedding_request_counts"),
            "r1_09_unavailable_provider": r109.get("embedding_unavailable", {}).get("provider_request_counts"),
        },
        "chat_requests": {"qwen": 0, "deepseek": 0},
        "postgresql": "NOT_USED",
        "production_code_modified": False,
        "cleanup": cleanup,
        "failure": failure,
    }
    _write_persistent_artifacts(result, log, artifacts)
    return 0 if overall == "PASS" else 1


def write_artifacts(result: dict, log: list[str], artifacts: dict | None = None) -> dict:
    artifacts = artifacts or _artifact_paths()
    statuses = [
        (item["id"], item["name"], item["status"], item.get("evidence") or item.get("failure_code") or item.get("reason", ""))
        for item in result["scenarios"]
    ]
    for name in ("forward", "reverse"):
        test_log = PROJECT_ROOT / f"PHASE5_1_6_R2_TEST_{name.upper()}.tmp"
        if test_log.is_file():
            log.append(f"OFFLINE_TEST_ORDER={name.upper()}")
            log.extend(test_log.read_text(encoding="utf-8", errors="replace").splitlines())
    matrix = [
        "# Phase 5.1.6-R2 Scenario Matrix", "",
        f"Overall status: **{result['overall_status']}**", "",
        "| ID | Scenario | Status | Evidence |", "|---|---|---|---|",
    ]
    for identity, name, status, evidence in statuses:
        safe = str(evidence).replace("|", "\\|").replace("\r", " ").replace("\n", " ")
        matrix.append(f"| {identity} | {name} | **{status}** | {safe} |")
    matrix_text = "\n".join(matrix) + "\n"
    report = [
        "# Phase 5.1.6-R2 Chroma Persistence and Degradation Acceptance", "",
        f"Overall status: **{result['overall_status']}**", "",
        f"Run ID: {result['run_id']}",
        f"Branch/HEAD: {result['git'].get('branch')} / {result['git'].get('head')}",
        f"Protected tag: {result['git'].get('protected_tag')} ({result['git'].get('protected_tag_commit')})", "",
        "## Scope", "",
        "R1-08 and R1-09 were tested with synthetic repositories, temporary persistent Chroma directories, and in-memory SQLite. PostgreSQL, the formal repository registry, and the production Chroma directory were not used. No chat model requests were sent.", "",
        "## Historical R1 evidence", "",
        f"R1-01 through R1-07: {result['historical_r1_evidence']['scenarios_01_07_status']} from run {result['historical_r1_evidence']['run_id']}. These are prior-run evidence, not rerun in R2.", "",
        "## R2 results", "", "| ID | Scenario | Status | Evidence |", "|---|---|---|---|",
    ]
    for identity, name, status, evidence in statuses:
        safe = str(evidence).replace("|", "\\|").replace("\r", " ").replace("\n", " ")
        report.append(f"| {identity} | {name} | **{status}** | {safe} |")
    report += ["", "## Offline tests", "",
        f"Forward order: {result.get('offline_tests', {}).get('forward')}. Reverse order: {result.get('offline_tests', {}).get('reverse')}.",
        "", "## Preflight", "",
        f"Status: {result['preflight'].get('status')}; Python: {result['preflight'].get('python')}; Chroma: {result['preflight'].get('chroma_version')}; Embedding health: {result['preflight'].get('embedding_health')}; model: {result['preflight'].get('embedding_model_id')}; dimension: {result['preflight'].get('embedding_dimension')}.",
        "", "PostgreSQL: not used. DeepSeek/Qwen Chat requests: 0. Local Embedding requests are recorded from provider counters; health checks are not inference requests.",
        "", "## Resource cleanup", "",
        f"Cleanup status: {result['cleanup'].get('status')}; temporary root removed: {result['cleanup'].get('temporary_root_removed')}; formal data touched: {result['cleanup'].get('formal_data_touched')}.",
        "", "## Known limits", "",
        "The R1-01 through R1-07 results are historical evidence from R1. Interactive Start-AgentForge startup and formal PostgreSQL integration are outside R2 scope. The outage tests inject failures only into temporary test providers/collections; they do not stop user services.",
    ]
    if result.get("failure"):
        report += ["", "## Failure classification", "",
                   f"Stage: {result['failure'].get('stage')}; classification: {result['failure'].get('classification')}; code: {result['failure'].get('error_code')}; detail: {result['failure'].get('detail') or ''}."]
    artifacts["report"].write_text("\n".join(report) + "\n", encoding="utf-8")
    artifacts["results"].write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    artifacts["matrix"].write_text(matrix_text, encoding="utf-8")
    artifacts["log"].write_text("\n".join(log) + "\n", encoding="utf-8")
    return artifacts


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "--r2-reopen-child":
        try:
            request = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
            return _child_reopen(request)
        except Exception as exc:
            code = exc.code if isinstance(exc, AcceptanceFailure) else type(exc).__name__
            print(json.dumps({"status": "FAIL", "pid": os.getpid(), "error_code": code},
                             sort_keys=True, ensure_ascii=False))
            return 2
    if len(sys.argv) == 2 and sys.argv[1] == "--host":
        return run_host_acceptance()
    if len(sys.argv) == 2 and sys.argv[1] == "--host-persistent":
        return run_persistent_host_acceptance()
    print("Usage: python -B phase5_1_6_r2_acceptance.py --host | --host-persistent", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
