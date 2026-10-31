"""One-shot host acceptance for PostgreSQL, persistent Chroma and local Qwen.

Requires the user to start PostgreSQL, Start-Qwen.cmd and Start-Qwen-Embedding.cmd.
This runner never starts/stops a service, retries a model request or touches the
formal SQLite index. It creates uniquely scoped temporary PostgreSQL rows and
Chroma records, then deletes only those records.
"""
from contextlib import contextmanager, redirect_stderr, redirect_stdout
import hashlib
import http.client
import io
import ipaddress
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from uuid import uuid4


ROOT = Path(__file__).resolve().parent
MODEL_ROOT = Path(os.environ.get("AGENTFORGE_MODEL_ROOT", ROOT.parent / "models")).resolve()
CHAT_ENDPOINT = "http://127.0.0.1:56572/v1/chat/completions"
CHAT_MODEL = "qwen3.5-4b"
CHAT_KEY = "AGENTFORGE_LLAMA_API_KEY"
EXPECTED_ASSETS = {
    str(MODEL_ROOT / "qwen3.5-4b" / "Qwen3.5-4B-Q4_K_M.gguf"): "00FE7986FF5F6B463E62455821146049DB6F9313603938A70800D1FB69EF11A4",
    str(MODEL_ROOT / "qwen3-embedding-0.6b-gguf" / "Qwen3-Embedding-0.6B-Q8_0.gguf"): "06507C7B42688469C4E7298B0A1E16DEFF06CAF291CF0A5B278C308249C3E439",
    str(MODEL_ROOT / "qwen3.5-4b" / ".runtime" / "llama-b11424" / "llama-server.exe"): "F8DF891A5E4D10FA8CFC0F86C8D99617A06EDD18D046E18B59EC3354F17FC12D",
    str(MODEL_ROOT / "qwen3.5-4b" / ".runtime" / "llama-b11424" / "llama-server-impl.dll"): "F020D94DF5E91840F2C9626B86F9AFE414C16D17ED5E1F41FE9CB614D3CDCDCB",
}
EMBEDDING_MODEL_ID = (
    "Qwen/Qwen3-Embedding-0.6B-GGUF@370f27d7550e0def9b39c1f16d3fbaa13aa67728:Q8_0#"
    "06507C7B42688469C4E7298B0A1E16DEFF06CAF291CF0A5B278C308249C3E439"
)
EMBEDDING_API_MODEL = "qwen3-embedding-0.6b-q8_0-agentforge"


class AcceptanceFailure(RuntimeError):
    def __init__(self, stage, code):
        super().__init__(code)
        self.stage, self.code = stage, code


EXPECTED_POSTGRES_IDENTITY = {
    "current_user": "agentforge_app",
    "current_database": "agentforge",
    "server_address": "127.0.0.1",
    "server_port": 5432,
}


def _postgres_identity_diagnostic(identity):
    """Return only non-secret connection identity fields and their mismatches."""
    observed = {
        "current_user": identity["current_user"],
        "current_database": identity["current_database"],
        "server_address": identity["server_address"],
        "server_port": identity["server_port"],
    }
    raw_address = observed["server_address"]
    normalized_address = None
    if isinstance(raw_address, str):
        try:
            if "/" in raw_address:
                interface = ipaddress.ip_interface(raw_address)
                if interface.network.prefixlen == interface.max_prefixlen:
                    normalized_address = str(interface.ip)
            else:
                normalized_address = str(ipaddress.ip_address(raw_address))
        except ValueError:
            pass
    # The connection URL is fixed to IPv4 loopback. Accept psycopg/PostgreSQL
    # textual forms 127.0.0.1 and 127.0.0.1/32, but no other address or prefix.
    if normalized_address != "127.0.0.1":
        normalized_address = None
    mismatches = [key for key, expected in EXPECTED_POSTGRES_IDENTITY.items()
                  if (normalized_address if key == "server_address" else observed[key]) != expected]
    return {
        "expected": dict(EXPECTED_POSTGRES_IDENTITY),
        "observed": observed,
        "normalized_server_address": normalized_address,
        "mismatches": mismatches,
    }


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def _health_ready(port, *, timeout_seconds=120):
    started = time.monotonic()
    deadline = started + timeout_seconds
    observations = {"loading_503": 0, "ready_200": 0}
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        connection = http.client.HTTPConnection("127.0.0.1", port,
                                                 timeout=min(2.0, max(0.1, remaining)))
        try:
            connection.request("GET", "/health", headers={"Accept": "application/json"})
            response = connection.getresponse()
            raw = response.read(64 * 1024 + 1)
            if len(raw) > 64 * 1024:
                raise AcceptanceFailure("health", "health_body_too_large")
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise AcceptanceFailure("health", "health_invalid_json") from None
            if response.status == 200 and isinstance(payload, dict) and payload.get("status") == "ok":
                observations["ready_200"] += 1
                return round(time.monotonic() - started, 6), observations
            error = payload.get("error") if isinstance(payload, dict) else None
            message = error.get("message") if isinstance(error, dict) else None
            if response.status == 503 and message == "Loading model":
                observations["loading_503"] += 1
            else:
                raise AcceptanceFailure("health", "health_contract_rejected")
        except AcceptanceFailure:
            raise
        except (OSError, http.client.HTTPException) as exc:
            raise AcceptanceFailure("health", "health_transport_" + type(exc).__name__) from None
        finally:
            connection.close()
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(1.0, remaining))
    raise AcceptanceFailure("health", "model_readiness_timeout")


def _invoke_cli(cli, *arguments):
    output, errors = io.StringIO(), io.StringIO()
    with redirect_stdout(output), redirect_stderr(errors):
        code = cli.main(list(arguments))
    if code != 0:
        raise AcceptanceFailure(arguments[0], "cli_exit_" + str(code))
    try:
        return json.loads(output.getvalue())
    except json.JSONDecodeError:
        raise AcceptanceFailure(arguments[0], "cli_output_invalid_json") from None


def _chat_counts(stats_list):
    keys = ("attempted", "sent", "succeeded", "failed", "retries")
    return {key: sum(int(item.get(key, 0) or 0) for item in stats_list) for key in keys}


def _answer_contract_status(answer):
    contract = answer.get("answer_contract") if isinstance(answer, dict) else None
    return contract.get("status") if isinstance(contract, dict) else None


def _main():
    run_id = uuid4().hex
    result_path = ROOT / f"AF012_MVP_HOST_RESULT_{run_id}.json"
    result = {
        "schema": "af012-mvp-host-acceptance-v1", "run_id": run_id,
        "status": "failed", "state": "PRECHECK", "failure_stage": None,
        "failure_code": None,
        "environment": {"host_runner": True, "starts_or_stops_services": False,
                        "network_scope": "explicit 127.0.0.1 only"},
        "assets": {}, "health": {}, "checks": {},
        "chat_requests": {"attempted": 0, "sent": 0, "succeeded": 0, "failed": 0, "retries": 0},
        "embedding_requests": {"attempts": 0, "sent": 0, "successes": 0, "failures": 0},
        "temporary_rows_cleaned": False, "temporary_chroma_records_cleaned": False,
        "temporary_repository_created": False,
        "temporary_repository_cleanup_status": "not_created",
        # No directory exists yet, so there is currently nothing to clean.
        "temporary_repository_cleaned": True, "formal_sqlite_touched": False,
    }
    if os.environ.get("CODEX_SANDBOX_NETWORK_DISABLED") == "1":
        result.update(status="blocked_by_environment", state="ENVIRONMENT_GATE")
        result["environment"]["network_disabled_marker"] = True
        result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"result_file": str(result_path), "status": result["status"],
                          "chat_requests": result["chat_requests"],
                          "embedding_requests": result["embedding_requests"]}, ensure_ascii=False, indent=2))
        return 2

    missing = [name for name in ("AGENTFORGE_DB_PASSWORD", CHAT_KEY,
                                  "AGENTFORGE_EMBEDDING_BASE_URL", "AGENTFORGE_EMBEDDING_MODEL_ID",
                                  "AGENTFORGE_EMBEDDING_API_MODEL") if not os.environ.get(name)]
    if missing:
        result.update(status="blocked_by_configuration", state="CONFIGURATION")
        result["failure_stage"], result["failure_code"] = "configuration", "required_runtime_config_missing"
        result["missing_configuration_names"] = missing
        result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"result_file": str(result_path), "status": result["status"],
                          "missing_configuration_names": missing}, ensure_ascii=False, indent=2))
        return 2

    for filename, expected in EXPECTED_ASSETS.items():
        asset = Path(filename)
        if not asset.is_file():
            result.update(state="ASSET_PREFLIGHT", failure_stage="assets", failure_code="asset_missing")
            result["assets"][filename] = {"exists": False}
            break
        digest = _sha256(asset)
        result["assets"][filename] = {"bytes": asset.stat().st_size, "sha256": digest,
                                       "expected_sha256": expected, "matches": digest == expected}
        if digest != expected:
            result.update(state="ASSET_PREFLIGHT", failure_stage="assets", failure_code="asset_hash_mismatch")
            break
    if result["failure_code"]:
        result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"result_file": str(result_path), "status": result["status"],
                          "state": result["state"], "failure_code": result["failure_code"]}, ensure_ascii=False, indent=2))
        return 1

    engine = index = None
    temporary_repo = None
    repo_id = None
    conversation_ids = []
    memory_ids = []
    collection = None
    chroma_client = None
    embedding_provider = None
    collection_name = None
    source_repo_id = None
    tracked_chat_stats = []
    recorded_chat_messages = []
    result["assets_checked"] = True

    try:
        result["state"] = "MODEL_READINESS"
        chat_ready, chat_observations = _health_ready(56572)
        result["health"]["chat"] = {"status": "MODEL_READY", "wait_seconds": chat_ready,
                                     "observations": chat_observations, "http_contract": "200 status=ok"}
        from agentforge.af012_runtime import embedding_provider_from_environment
        embedding_provider = embedding_provider_from_environment()
        embedding_ready = embedding_provider.wait_ready(timeout=120.0, poll_interval=1.0)
        result["health"]["embedding"] = {"status": "MODEL_READY", "wait_seconds": embedding_ready,
                                          "http_contract": "200 status=ok"}
        if embedding_provider.model_id != EMBEDDING_MODEL_ID or embedding_provider.api_model != EMBEDDING_API_MODEL:
            raise AcceptanceFailure("configuration", "embedding_identity_mismatch")
        if embedding_provider.dimension != 1024:
            raise AcceptanceFailure("configuration", "embedding_dimension_mismatch")
        result["health"]["embedding"]["model_id"] = embedding_provider.model_id
        result["health"]["embedding"]["dimension"] = embedding_provider.dimension
        result["checks"]["both_models_ready"] = "passed"

        result["state"] = "DATABASE_MIGRATION"
        from agentforge.af012_connection import create_local_engine, check_database_health
        from agentforge.conversation_migrations import upgrade
        from agentforge.conversation_store import ConversationStore
        from agentforge.memory_store import MemoryStore
        from agentforge.af012_schema import (Memory, MemoryApproval, MemoryEvent, MemorySource,
                                              RepositoryMetadata, VectorSyncOutbox)
        from sqlalchemy import delete, text, update
        from datetime import datetime, timedelta, timezone
        engine = create_local_engine(os.environ["AGENTFORGE_DB_PASSWORD"])
        if not check_database_health(engine):
            raise AcceptanceFailure("database", "postgresql_health_failed")
        with engine.connect() as connection:
            identity = connection.execute(text(
                "SELECT current_user AS current_user, current_database() AS current_database, "
                "inet_server_addr()::text AS server_address, inet_server_port() AS server_port"
            )).mappings().one()
        result["postgresql_identity"] = _postgres_identity_diagnostic(identity)
        if result["postgresql_identity"]["mismatches"]:
            raise AcceptanceFailure("database", "postgresql_identity_or_scope_mismatch")
        upgrade(engine)
        result["checks"]["postgresql_connected_and_migrated"] = "passed"

        temp_parent = ROOT / "tests" / ".tmp"
        temp_parent.mkdir(parents=True, exist_ok=True)
        temporary_repo = Path(tempfile.mkdtemp(prefix="af012-mvp-" + run_id + "-", dir=temp_parent))
        result["temporary_repository_created"] = True
        result["temporary_repository_cleanup_status"] = "pending"
        result["temporary_repository_cleaned"] = False
        # Establish the exact disposable Chroma scope before any operation can
        # fail, so finally can remove code vectors even on early indexing errors.
        from agentforge.embeddings.chroma_index import repository_identity
        source_repo_id = repository_identity(str(temporary_repo))
        (temporary_repo / "src").mkdir()
        (temporary_repo / "src" / "geometry.py").write_text(
            'def shift_coordinate(value, amount):\n'
            '    """Return a coordinate shifted by an amount."""\n'
            '    return value + amount\n\n'
            'def scale_coordinate(value, factor):\n'
            '    """Scale a coordinate by a factor."""\n'
            '    return value * factor\n', encoding="utf-8")
        (temporary_repo / "README.md").write_text(
            "# Temporary geometry examples\nThese functions contain synthetic examples for a local AgentForge acceptance run.\n",
            encoding="utf-8")
        from agentforge.store import IndexStore
        from agentforge.indexer import index_repository
        index = IndexStore(":memory:")
        index_repository(temporary_repo, index)
        repo_id = __import__("agentforge.cli", fromlist=["_repository_id"])._repository_id(str(temporary_repo))
        conversation_store = ConversationStore(engine)
        memory_store = MemoryStore(engine)
        memory_store.register_repository(repo_id, str(temporary_repo.resolve()), "AF012 disposable acceptance")
        first = conversation_store.create_conversation(repo_id, "AF012 disposable geometry")
        conversation_ids.append(first["id"])
        second = conversation_store.create_conversation(repo_id, "AF012 disposable isolation")
        conversation_ids.append(second["id"])
        conversation_store.switch_conversation(first["id"], repo_id)
        result["checks"]["two_repository_scoped_sessions_created"] = "passed"

        from agentforge.af012_runtime import create_persistent_chroma, collection_for_model
        chroma_client = create_persistent_chroma()
        collection = collection_for_model(chroma_client, embedding_provider.model_id,
                                          embedding_provider.dimension)
        collection_name = collection.name

        @contextmanager
        def isolated_index(*_args, **_kwargs):
            yield index

        from unittest.mock import patch
        from agentforge.providers.llama_server import LlamaServerClient
        from agentforge import cli
        original_chat = LlamaServerClient.chat

        def record_chat(client, messages, *, model):
            recorded_chat_messages.append(messages)
            try:
                return original_chat(client, messages, model=model)
            finally:
                tracked_chat_stats.append(client.request_stats())

        def invoke(*args):
            return _invoke_cli(cli, *args)

        result["state"] = "CHROMA_INDEXING"
        with (patch("agentforge.cli.IndexStore", side_effect=isolated_index),
              patch("agentforge.cli._conversation_engine", return_value=engine),
              patch("agentforge.af012_runtime.create_persistent_chroma", return_value=chroma_client),
              patch("agentforge.af012_runtime.collection_for_model", return_value=collection),
              patch("agentforge.af012_runtime.embedding_provider_from_environment", return_value=embedding_provider),
              patch.object(LlamaServerClient, "chat", record_chat)):
            indexed = invoke("chroma", "index")
            sync_result = indexed["result"]
            if sync_result["sources"] < 2 or sync_result["embedded"] < 2:
                raise AcceptanceFailure("chroma_index", "expected_sources_not_embedded")
            result["checks"]["real_qwen_chroma_code_index"] = "passed"
            result["chroma_index"] = sync_result

            searched = invoke("chroma", "search", "coordinate shifted by an amount", "--top-k", "5")
            search_paths = [item["relative_path"] for item in searched["results"]]
            if "src/geometry.py" not in search_paths:
                raise AcceptanceFailure("chroma_search", "expected_synthetic_source_not_returned")
            result["checks"]["verified_chroma_candidate_returned"] = "passed"

            result["state"] = "ASKING"
            def ask(question, conversation_id):
                answer = invoke("ask", question, "--conversation-id", conversation_id,
                    "--provider", "llama-server", "--base-url", CHAT_ENDPOINT,
                    "--model", CHAT_MODEL, "--read-timeout", "120", "--total-timeout", "180",
                    "--max-output-tokens", "256", "--use-chroma")
                result.setdefault("answers", []).append({
                    "question": question, "provider": answer.get("provider"),
                    "answer": answer.get("answer"), "contract_status": answer.get("answer_contract", {}).get("status"),
                    "evidence": answer.get("answer_contract", {}).get("evidence", []),
                    "persistent_chroma": answer.get("persistent_chroma"),
                    "provider_request_stats": answer.get("provider_request_stats"),
                })
                if answer.get("provider") != "llama-server" or not isinstance(answer.get("answer"), str) or not answer["answer"].strip():
                    raise AcceptanceFailure("ask", "real_chat_answer_missing")
                if not answer.get("answer_contract", {}).get("claims") or not answer.get("answer_contract", {}).get("evidence"):
                    raise AcceptanceFailure("ask", "answer_contract_evidence_missing")
                return answer

            first_question = "What does function shift_coordinate return? Cite the source evidence."
            first_answer = ask(first_question, first["id"])
            first_text = first_answer["answer"].casefold()
            evidence = first_answer["answer_contract"]["evidence"]
            if ("src/geometry.py" not in {item["relative_path"] for item in evidence}
                    or "shift_coordinate" not in first_text
                    or not ("amount" in first_text and ("value" in first_text or "coordinate" in first_text))):
                raise AcceptanceFailure("semantic_check", "first_answer_did_not_match_synthetic_source")
            result["checks"]["evidence_answer_and_citation"] = "passed"

            followup = "What does its second parameter add?"
            second_answer = ask(followup, first["id"])
            prior_messages = recorded_chat_messages[-1]
            serialized_prior = "\n".join(item.get("content", "") for item in prior_messages[:-1])
            if (first_question not in serialized_prior or "amount" not in second_answer["answer"].casefold()):
                raise AcceptanceFailure("multiturn", "followup_history_or_semantics_missing")
            result["checks"]["real_qwen_multiturn_followup"] = "passed"

            third_answer = ask("What does function scale_coordinate return? Cite the source evidence.", second["id"])
            other_context = "\n".join(item.get("content", "") for item in recorded_chat_messages[-1][:-1])
            if first_question in other_context:
                raise AcceptanceFailure("session_isolation", "first_session_history_leaked")
            if "factor" not in third_answer["answer"].casefold() and "scale" not in third_answer["answer"].casefold():
                raise AcceptanceFailure("session_isolation", "second_session_answer_unexpected")
            result["checks"]["session_isolation"] = "passed"

            # This explicit test statement is the only source allowed for extraction.
            preference_turn = "I prefer explanations that name each parameter before describing the formula."
            conversation_store.append_turn(first["id"], repo_id, preference_turn, "Preference noted.")
            messages = conversation_store.get_messages(first["id"], repo_id)
            preference_source = next(item for item in reversed(messages)
                                     if item["role"] == "user" and item["content"] == preference_turn)
            result["state"] = "MEMORY_EXTRACTION"
            suggestion_output = invoke("memory", "suggest", first["id"], "--base-url", CHAT_ENDPOINT,
                                       "--model", CHAT_MODEL)
            candidates = suggestion_output["result"].get("candidates", [])
            chosen = next((item for item in candidates
                           if preference_source["sequence"] in item.get("source_sequences", [])
                           and preference_source["id"] in item.get("source_message_ids", [])), None)
            candidate_text = chosen.get("content", "").casefold() if chosen else ""
            candidate_semantic_match = bool(chosen
                and ("parameter" in candidate_text or "参数" in candidate_text)
                and ("formula" in candidate_text or "公式" in candidate_text))
            result["memory_extraction"] = {
                "chat_request_stats": suggestion_output["result"].get("request_stats"),
                "candidate_count": len(candidates),
                "source_bound_to_explicit_user_message": chosen is not None,
                "candidate_semantic_match": candidate_semantic_match,
                "candidate_content": chosen.get("content") if chosen else None,
            }
            if not chosen or not candidate_semantic_match:
                result["checks"]["qwen_memory_candidate_source_checked"] = "failed"
                code = ("qwen_candidate_not_bound_to_user_source" if not chosen
                        else "qwen_candidate_did_not_preserve_explicit_preference")
                raise AcceptanceFailure("memory_extraction", code)
            content = chosen["content"]
            source_ids = chosen["source_message_ids"]
            result["checks"]["qwen_memory_candidate_source_checked"] = "passed"

            proposed = invoke("memory", "propose", first["id"], content,
                              "--type", "preference", *sum((["--source-message-id", item] for item in source_ids), []))
            memory_id = proposed["result"]["id"]
            memory_ids.append(memory_id)
            if proposed["result"]["approval_status"] != "pending":
                raise AcceptanceFailure("memory_proposal", "memory_was_not_pending")
            result["checks"]["memory_starts_pending"] = "passed"

            # The host test approves only its own disposable synthetic record.
            invoke("memory", "approve", memory_id)
            sync = invoke("memory", "sync")["result"]
            if sync.get("failed", 0) or sync.get("completed", 0) < 1:
                raise AcceptanceFailure("memory_vector_sync", "approved_memory_sync_failed")
            result["memory_sync_approved"] = sync
            from agentforge.memory_retrieval import ChromaMemoryRetriever
            from agentforge.embeddings.chroma_store import ChromaCandidateStore
            memory_retriever = ChromaMemoryRetriever(collection, embedding_provider, memory_store)
            found_memory = memory_retriever.search("How should explanations be written?",
                                                  repository_id=repo_id)
            if not any(item.content == content for item in found_memory):
                raise AcceptanceFailure("memory_retrieval", "approved_memory_not_retrieved")
            result["checks"]["approved_memory_semantic_retrieval"] = "passed"

            other_repository_memory = memory_retriever.search(
                "Explain a coordinate helper in my preferred format.",
                repository_id=repo_id + "-different-repository")
            if other_repository_memory:
                raise AcceptanceFailure("memory_repository_scope", "other_repository_memory_returned")
            result["checks"]["memory_repository_isolation"] = "passed"

            # The earlier asks predated memory approval. This distinct-session ask
            # is deliberately after PostgreSQL approval and Chroma outbox sync.
            memory_question = "Explain shift_coordinate in my preferred format."
            memory_answer = ask(memory_question, second["id"])
            memory_context = memory_answer.get("persistent_chroma", {})
            injected_records = memory_context.get("memory_context", [])
            recorded_prompt = "\n".join(item.get("content", "") for item in recorded_chat_messages[-1])
            answer_text = memory_answer["answer"].casefold()
            expected_sources = memory_store.source_records(memory_id, repo_id)
            if (memory_context.get("memory_context_count", 0) < 1
                    or not any(item.get("memory_id") == memory_id for item in injected_records)
                    or not expected_sources
                    or not any(item.get("message_id") == preference_source["id"]
                               for record in injected_records for item in record.get("sources", []))
                    or f"Memory ID: {memory_id}" not in recorded_prompt
                    or preference_source["id"] not in recorded_prompt
                    or "shift_coordinate" not in answer_text
                    or "value" not in answer_text or "amount" not in answer_text
                    or not any(token in answer_text for token in ("parameter", "argument", "参数"))):
                raise AcceptanceFailure("cross_session_memory", "approved_memory_not_used_in_new_session_answer")
            result["cross_session_memory"] = {
                "conversation_id": second["id"],
                "question": memory_question,
                "memory_context_count": memory_context.get("memory_context_count"),
                "injected_memory_metadata": injected_records,
                "source_message_id": preference_source["id"],
                "source_sequence": preference_source["sequence"],
                "answer": memory_answer["answer"],
                "answer_contract_status": _answer_contract_status(memory_answer),
                "provider_request_stats": memory_answer.get("provider_request_stats"),
            }
            result["checks"]["approved_memory_injected_into_new_session"] = "passed"
            result["checks"]["real_qwen_used_cross_session_memory"] = "passed"

            updated_content = "User prefers explanations to identify parameters before formulas."
            updated = invoke("memory", "update", memory_id, updated_content)["result"]
            if updated["approval_status"] != "pending":
                raise AcceptanceFailure("memory_update", "updated_memory_did_not_require_reapproval")
            invoke("memory", "approve", memory_id)
            updated_sync = invoke("memory", "sync")["result"]
            if updated_sync.get("failed", 0):
                raise AcceptanceFailure("memory_update", "updated_memory_vector_sync_failed")
            found_updated = memory_retriever.search("How should explanations be written?",
                                                    repository_id=repo_id)
            if not any(item.content == updated_content for item in found_updated):
                raise AcceptanceFailure("memory_update", "updated_memory_not_retrieved")
            result["checks"]["memory_update_and_reapproval"] = "passed"
            invoke("memory", "revoke", memory_id)
            revoked_sync = invoke("memory", "sync")["result"]
            if revoked_sync.get("failed", 0):
                raise AcceptanceFailure("memory_revoke", "revocation_vector_cleanup_failed")
            found_revoked = memory_retriever.search("How should explanations be written?",
                                                   repository_id=repo_id)
            if any(item.content == updated_content for item in found_revoked):
                raise AcceptanceFailure("memory_revoke", "revoked_memory_still_retrievable")
            result["checks"]["memory_revoke_and_vector_delete"] = "passed"

            expiring = memory_store.propose(repo_id, first["id"],
                "Temporary preference expires during the disposable host acceptance.",
                [preference_source["id"]], memory_type="preference",
                expires_at=datetime.now(timezone.utc) + timedelta(days=1))
            memory_ids.append(expiring["id"])
            memory_store.approve(expiring["id"], repo_id)
            from agentforge.af012_schema import Memory as MemoryModel
            with engine.begin() as connection:
                connection.execute(update(MemoryModel).where(MemoryModel.id == __import__("uuid").UUID(expiring["id"]))
                                   .values(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
            expired_count = memory_store.expire_due()
            expiry_sync = memory_store.sync_outbox(ChromaCandidateStore(collection, dimension=1024),
                                                   embedding_provider)
            if expired_count != 1 or expiry_sync.get("failed", 0):
                raise AcceptanceFailure("memory_expiry", "expiration_or_vector_delete_failed")
            if memory_store.get(expiring["id"], repo_id)["approval_status"] != "expired":
                raise AcceptanceFailure("memory_expiry", "memory_not_marked_expired")
            result["checks"]["memory_expiry_and_vector_delete"] = "passed"

            # Exercise the complete authoritative SQLite snapshot -> Chroma
            # lifecycle on this disposable repository, after all code Q&A.
            result["state"] = "CHROMA_INCREMENTAL_LIFECYCLE"
            readme = temporary_repo / "README.md"
            revision_marker = "cedar atlas revision marker 7f4c"
            readme.write_text(readme.read_text(encoding="utf-8") +
                f"\nRevision note: {revision_marker}.\n", encoding="utf-8")
            probe = temporary_repo / "src" / "revision_probe.py"
            probe.write_text("def revision_probe():\n    return 'incremental probe active'\n", encoding="utf-8")
            snapshot_change = index_repository(temporary_repo, index)
            changed_sync = invoke("chroma", "index")["result"]
            if (snapshot_change.get("added", 0) < 1 or snapshot_change.get("changed", 0) < 1
                    or changed_sync.get("embedded", 0) < 2):
                raise AcceptanceFailure("chroma_incremental_update", "changed_sources_not_reembedded")
            updated_hits = invoke("chroma", "search", revision_marker, "--top-k", "10")["results"]
            if not any(item["relative_path"] == "README.md" and revision_marker in item["text"]
                       for item in updated_hits):
                raise AcceptanceFailure("chroma_incremental_update", "updated_content_not_retrieved")
            result["chroma_lifecycle"] = {"content_change_snapshot": snapshot_change,
                                           "content_change_sync": changed_sync}
            result["checks"]["chroma_real_embedding_content_update"] = "passed"

            probe.unlink()
            delete_snapshot = index_repository(temporary_repo, index)
            delete_sync = invoke("chroma", "index")["result"]
            rows = collection.get(where={"$and": [{"repository_id": source_repo_id},
                {"model_id": embedding_provider.model_id}]}, limit=10000, include=["metadatas"])
            live_paths = {item.get("file_path") for item in rows.get("metadatas", [])}
            if (delete_snapshot.get("removed", 0) < 1 or delete_sync.get("removed", 0) < 1
                    or "src/revision_probe.py" in live_paths):
                raise AcceptanceFailure("chroma_incremental_delete", "deleted_source_vector_remains")
            result["chroma_lifecycle"]["delete_snapshot"] = delete_snapshot
            result["chroma_lifecycle"]["delete_sync"] = delete_sync
            result["checks"]["chroma_real_embedding_file_delete"] = "passed"

            docs = temporary_repo / "docs"
            docs.mkdir()
            readme.rename(docs / "README.md")
            rename_snapshot = index_repository(temporary_repo, index)
            rename_sync = invoke("chroma", "index")["result"]
            rows = collection.get(where={"$and": [{"repository_id": source_repo_id},
                {"model_id": embedding_provider.model_id}]}, limit=10000, include=["metadatas"])
            live_paths = {item.get("file_path") for item in rows.get("metadatas", [])}
            if (rename_snapshot.get("added", 0) < 1 or rename_snapshot.get("removed", 0) < 1
                    or rename_sync.get("removed", 0) < 1 or "README.md" in live_paths
                    or "docs/README.md" not in live_paths):
                raise AcceptanceFailure("chroma_incremental_rename", "renamed_source_identity_not_refreshed")
            result["chroma_lifecycle"]["rename_snapshot"] = rename_snapshot
            result["chroma_lifecycle"]["rename_sync"] = rename_sync
            result["checks"]["chroma_real_embedding_rename"] = "passed"

            rebuild = invoke("chroma", "rebuild")["result"]
            rows = collection.get(where={"$and": [{"repository_id": source_repo_id},
                {"model_id": embedding_provider.model_id}]}, limit=10000, include=["metadatas"])
            rebuilt_paths = {item.get("file_path") for item in rows.get("metadatas", [])}
            if (rebuild.get("embedded") != rebuild.get("sources") or rebuild.get("removed", 0) != 0
                    or rebuilt_paths != {"src/geometry.py", "docs/README.md"}):
                raise AcceptanceFailure("chroma_rebuild", "full_rebuild_snapshot_mismatch")
            result["chroma_lifecycle"]["full_rebuild"] = rebuild
            result["chroma_lifecycle"]["final_record_count"] = len(rows.get("ids", []))
            result["checks"]["chroma_real_full_rebuild"] = "passed"

        # A new engine and process both reopen persisted PostgreSQL/Chroma state.
        result["state"] = "REOPEN_CHECK"
        engine.dispose()
        engine = create_local_engine(os.environ["AGENTFORGE_DB_PASSWORD"])
        restored = ConversationStore(engine).get_messages(first["id"], repo_id)
        if len(restored) < 6:
            raise AcceptanceFailure("postgresql_reopen", "conversation_history_not_restored")
        result["checks"]["postgresql_new_engine_reconnect"] = "passed"

        reopened_child = (
            "import json,sys; import chromadb; from chromadb.config import Settings; "
            "c=chromadb.PersistentClient(path=sys.argv[1],settings=Settings(anonymized_telemetry=False,allow_reset=False)); "
            "x=c.get_collection(sys.argv[2]); r=x.get(where={'$and':[{'repository_id':sys.argv[3]},{'model_id':sys.argv[4]}]},include=['metadatas']); "
            "print(json.dumps({'count':len(r.get('ids',[])),'paths':sorted(m.get('file_path') for m in r.get('metadatas',[]))}))"
        )
        child = subprocess.run([sys.executable, "-X", "utf8", "-B", "-c", reopened_child,
            str(ROOT / ".agentforge" / "data" / "chroma"), collection_name,
            source_repo_id, embedding_provider.model_id], cwd=ROOT, capture_output=True,
            timeout=30, check=False, env={**os.environ, "ANONYMIZED_TELEMETRY": "False"})
        if child.returncode != 0:
            raise AcceptanceFailure("chroma_reopen", "chroma_new_process_reopen_failed")
        reopened_count = json.loads(child.stdout.decode("utf-8").strip().splitlines()[-1])["count"]
        if reopened_count != 2:
            raise AcceptanceFailure("chroma_reopen", "chroma_data_not_persisted")
        result["checks"]["chroma_new_process_reopen"] = "passed"
        result["chroma_reopened_code_records"] = reopened_count
        result["chroma_reopened_paths"] = json.loads(child.stdout.decode("utf-8").strip().splitlines()[-1])["paths"]
        if result["chroma_reopened_paths"] != ["docs/README.md", "src/geometry.py"]:
            raise AcceptanceFailure("chroma_reopen", "reopened_chroma_snapshot_mismatch")
        result["checks"]["real_pg_crud_memory_and_outbox"] = "passed"
        result["status"] = ("passed" if all(value == "passed" for value in result["checks"].values())
                            else "partial")
        result["state"] = "COMPLETED"
    except AcceptanceFailure as exc:
        result["failure_stage"], result["failure_code"] = exc.stage, exc.code
        result["status"], result["state"] = "failed", "FAILED"
    except Exception as exc:
        result["failure_stage"] = result.get("state", "unknown").casefold()
        result["failure_code"] = "unhandled_" + type(exc).__name__
        result["status"], result["state"] = "failed", "FAILED"
    finally:
        try:
            if collection is not None and repo_id is not None:
                ids = []
                for owned_repo_id in {repo_id, source_repo_id} - {None}:
                    where = {"$and": [{"repository_id": owned_repo_id}, {"model_id": EMBEDDING_MODEL_ID}]}
                    owned = collection.get(where=where, limit=10000, include=["metadatas"])
                    ids.extend(owned.get("ids", []))
                if ids:
                    collection.delete(ids=ids)
                remaining = []
                for owned_repo_id in {repo_id, source_repo_id} - {None}:
                    where = {"$and": [{"repository_id": owned_repo_id}, {"model_id": EMBEDDING_MODEL_ID}]}
                    remaining.extend(collection.get(where=where, limit=1, include=["metadatas"]).get("ids", []))
                result["temporary_chroma_records_cleaned"] = not remaining
            else:
                result["temporary_chroma_records_cleaned"] = True
        except Exception as exc:
            result["temporary_chroma_records_cleaned"] = False
            result["cleanup_error_type"] = type(exc).__name__
            result["status"] = "failed"
        try:
            if engine is not None and repo_id is not None:
                from agentforge.af012_schema import (Conversation, ConversationSelection, ConversationSummary,
                    Memory as MemoryModel, MemoryApproval, MemoryEvent, MemorySource, Message,
                    RepositoryMetadata, VectorSyncOutbox)
                from sqlalchemy import delete
                with engine.begin() as connection:
                    if memory_ids:
                        connection.execute(delete(VectorSyncOutbox).where(
                            VectorSyncOutbox.repository_id == repo_id, VectorSyncOutbox.source_type == "memory"))
                        identities = [__import__("uuid").UUID(item) for item in memory_ids]
                        connection.execute(delete(MemorySource).where(MemorySource.memory_id.in_(identities)))
                        connection.execute(delete(MemoryApproval).where(MemoryApproval.memory_id.in_(identities)))
                        connection.execute(delete(MemoryEvent).where(MemoryEvent.memory_id.in_(identities)))
                        connection.execute(delete(MemoryModel).where(MemoryModel.id.in_(identities)))
                    if conversation_ids:
                        identities = [__import__("uuid").UUID(item) for item in conversation_ids]
                        connection.execute(delete(ConversationSelection).where(ConversationSelection.repository_id == repo_id))
                        connection.execute(delete(ConversationSummary).where(ConversationSummary.conversation_id.in_(identities)))
                        connection.execute(delete(Message).where(Message.conversation_id.in_(identities)))
                        connection.execute(delete(Conversation).where(Conversation.id.in_(identities)))
                    connection.execute(delete(RepositoryMetadata).where(RepositoryMetadata.repository_id == repo_id))
                result["temporary_rows_cleaned"] = True
            else:
                result["temporary_rows_cleaned"] = True
        except Exception as exc:
            result["temporary_rows_cleaned"] = False
            result["cleanup_error_type"] = type(exc).__name__
            result["status"] = "failed"
        if engine is not None:
            engine.dispose()
        if index is not None:
            index.close()
        if temporary_repo is not None:
            try:
                shutil.rmtree(temporary_repo)
                result["temporary_repository_cleaned"] = not temporary_repo.exists()
                result["temporary_repository_cleanup_status"] = (
                    "cleaned" if result["temporary_repository_cleaned"] else "cleanup_failed")
            except OSError as exc:
                result["temporary_repository_cleaned"] = False
                result["temporary_repository_cleanup_status"] = "cleanup_failed"
                result["cleanup_error_type"] = type(exc).__name__
                result["status"] = "failed"
        result["chat_requests"] = _chat_counts(tracked_chat_stats)
        if embedding_provider is not None:
            result["embedding_requests"] = embedding_provider.request_counts
        result["assets_after"] = {}
        for filename, expected in EXPECTED_ASSETS.items():
            try:
                path = Path(filename)
                digest = _sha256(path)
                result["assets_after"][filename] = {
                    "bytes": path.stat().st_size, "sha256": digest,
                    "matches_expected": digest == expected,
                    "matches_before": digest == result.get("assets", {}).get(filename, {}).get("sha256"),
                }
            except OSError:
                result["assets_after"][filename] = {"readable": False}
                result["status"] = "failed"
        integrity_ok = all(item.get("matches_before") is True and item.get("matches_expected") is True
                           for item in result["assets_after"].values())
        result["asset_integrity_passed"] = integrity_ok
        result["formal_sqlite_touched"] = False
        if not (result["temporary_rows_cleaned"] and result["temporary_chroma_records_cleaned"]
                and result["temporary_repository_cleaned"] and integrity_ok):
            result["status"] = "failed"
        if result["status"] == "failed":
            result["state"] = "FAILED"
        result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n",
                               encoding="utf-8")

    summary = {"result_file": str(result_path), "status": result["status"],
               "state": result["state"], "failure_stage": result["failure_stage"],
               "failure_code": result["failure_code"], "checks": result["checks"],
               "chat_requests": result["chat_requests"],
               "embedding_requests": result["embedding_requests"],
               "temporary_rows_cleaned": result["temporary_rows_cleaned"],
               "temporary_chroma_records_cleaned": result["temporary_chroma_records_cleaned"],
               "temporary_repository_cleaned": result["temporary_repository_cleaned"],
               "asset_integrity_passed": result["asset_integrity_passed"]}
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(_main())
