"""Bounded host check of isolated 1.1 PostgreSQL, Chroma and Qwen paths.

Creates one uniquely named disposable PostgreSQL database and a temporary local
repository/Chroma directory. No existing model or database service is managed.
The result is PARTIAL until all requested desktop and RAG cases are measured.
"""
import json
import gc
import os
import sys
import time
import traceback
from pathlib import Path
import tempfile
from uuid import uuid4

ROOT = Path(__file__).resolve().parent


class CheckFailure(Exception):
    """A fixed, non-secret diagnostic code for a failed assertion."""


class HostBlocked(CheckFailure):
    """A prerequisite unavailable without changing host permissions or configuration."""


def _chat_request_totals(records):
    fields = ("attempted", "sent", "succeeded", "failed", "retries")
    totals = {key: sum(int(item.get(key, 0) or 0) for item in records)
              for key in fields}
    totals["request_count"] = len(records)
    return totals


def _summary_failure_stage(diagnostic):
    phase = (diagnostic or {}).get("failure_stage")
    if phase in {"provider", "summarizer"}:
        return "summary_generation"
    if phase == "persistence":
        return "summary_persistence"
    return "summary_validation"


def _failure_code(error, stage):
    """Return a bounded diagnostic code without persisting exception text."""
    if isinstance(error, CheckFailure):
        return str(error)
    if stage in {"semantic_summary", "summary_generation", "summary_validation",
                 "summary_persistence"} and isinstance(error, ValueError):
        known = {
            "A complete turn exceeds the bounded semantic summary input; existing summary and messages were retained.":
                "semantic_summary_turn_input_too_large",
            "No complete turn fits the bounded semantic summary input; existing summary and messages were retained.":
                "semantic_summary_no_complete_turn_fits",
            "Semantic summary request exceeds the bounded prompt size; history was retained.":
                "semantic_summary_prompt_too_large",
            "Semantic summary response is empty or exceeds its safe limit.":
                "semantic_summary_response_invalid",
            "Merged semantic summary exceeds its safe limit; existing summary and messages were retained.":
                "semantic_summary_merged_output_too_large",
            "The semantic summary was truncated; existing history and summary were retained.":
                "semantic_summary_response_truncated",
            "Semantic summary input must contain complete successful turns.":
                "semantic_summary_incomplete_turn",
            "Protected conversation facts exceed the summary byte budget; existing summary was retained.":
                "semantic_summary_fact_protection_overflow",
            "Protected conversation facts exceed the summary byte budget; existing summary and cursor were retained.":
                "semantic_summary_fact_protection_overflow",
            "Summary omitted a protected source fact; existing summary and cursor were retained.":
                "semantic_summary_missing_protected_fact",
            "Composed summary omitted a protected source fact; existing summary and cursor were retained.":
                "semantic_summary_missing_protected_fact",
            "Summary exceeds the bounded storage budget; existing summary was retained.":
                "semantic_summary_storage_budget_exceeded",
        }
        return known.get(str(error), "semantic_summary_validation_error")
    return "operation_failed"


def _exception_location(error):
    """Record only the final source location, never locals or source text."""
    extracted = traceback.extract_tb(error.__traceback__) if error.__traceback__ else []
    if not extracted:
        return None
    frame = extracted[-1]
    return {"file": Path(frame.filename).name,
            "function": frame.name,
            "line": frame.lineno}


def _close_owned_chroma(client, cleanup):
    if client is None:
        return True
    try:
        client.close()
        cleanup["chroma_client"] = "passed"
        return True
    except Exception as exc:
        cleanup["chroma_client"] = "failed_" + type(exc).__name__
        return False


def _cleanup_owned_temp(temp, cleanup):
    if temp is None:
        return True
    # Windows may release Chroma's file handles shortly after close().
    for attempt in range(6):
        try:
            gc.collect()
            temp.cleanup()
            cleanup["temporary_directory"] = "passed"
            cleanup["temporary_cleanup_attempts"] = attempt + 1
            return True
        except PermissionError:
            if attempt == 5:
                cleanup["temporary_directory"] = "failed_PermissionError"
                cleanup["temporary_cleanup_attempts"] = attempt + 1
                return False
            time.sleep(0.2)
        except OSError as exc:
            cleanup["temporary_directory"] = "failed_" + type(exc).__name__
            return False


def _connection_diagnostic(error):
    """Extract only fixed categories and codes, never raw connection errors."""
    import re
    visited, pending, codes, hints = set(), [error], set(), set()
    while pending and len(visited) < 12:
        current = pending.pop()
        if current is None or id(current) in visited:
            continue
        visited.add(id(current))
        state = getattr(current, "sqlstate", None)
        if isinstance(state, str) and re.fullmatch(r"[0-9A-Z]{5}", state):
            codes.add(state)
        text = str(current).casefold()
        if "password authentication failed" in text or "28p01" in text:
            hints.add("authentication_failed")
        if "no password supplied" in text:
            hints.add("password_not_supplied")
        if "connection refused" in text or "10061" in text:
            hints.add("connection_refused")
        if "timed out" in text or "10060" in text:
            hints.add("connection_timeout")
        for name in ("orig", "__cause__", "__context__"):
            nested = getattr(current, name, None)
            if isinstance(nested, BaseException):
                pending.append(nested)
    return {"sqlstates": sorted(codes), "categories": sorted(hints) or ["unknown"]}


def main(*, extension=None, observations=None, preflight=None, finalizer=None,
         result_prefix="AF011_1_JOINT_HOST_RESULT_"):
    run_id = uuid4().hex
    result = {"status": "BLOCKED", "stage": "environment", "checks": {},
              "stage_results": {},
              "chat_requests": [], "embedding_requests": None,
              "cleanup": {"database": "not_created", "temporary_directory": "not_created"},
              "unmeasured": ["desktop_process_restart", "real_ask_rag_and_citations",
                             "full_ten_case_rag_quality", "memory_injection_into_qwen_ask",
                             "chroma_incremental_lifecycle", "postgres_service_restart",
                             "exact_model_token_budget"]}
    result_path = ROOT / (result_prefix + run_id + ".json")
    admin = engine = temp = client = None
    indexes = []
    chat = None
    dbname = "af0111_test_" + run_id
    created = False
    provider = None
    try:
        if os.environ.get("CODEX_SANDBOX_NETWORK_DISABLED") == "1":
            result["failure_code"] = "NETWORK_RESTRICTED"
            return 2
        password = os.environ.get("AGENTFORGE_DB_PASSWORD")
        key = os.environ.get("AGENTFORGE_LLAMA_API_KEY")
        if not password or not key:
            result["failure_code"] = "MISSING_LOCAL_CREDENTIALS"
            return 2
        if preflight is not None:
            result["stage"] = "asset_and_integrity_preflight"
            preflight(result)
        from sqlalchemy import create_engine, text
        from agentforge.af012_connection import create_local_engine
        probe = create_local_engine(password)
        url = probe.url
        probe.dispose()
        admin = create_engine(url, connect_args={"connect_timeout": 3},
                              isolation_level="AUTOCOMMIT", hide_parameters=True)
        result["stage"] = "database_preflight"
        with admin.connect() as conn:
            identity = conn.execute(text("SELECT current_user, current_database(), "
                "inet_server_addr()::text, inet_server_port(), "
                "(SELECT rolcreatedb OR rolsuper FROM pg_roles WHERE rolname=current_user)" )).one()
            if (identity[0] != "agentforge_app" or identity[1] != "agentforge"
                    or str(identity[2]).split("/")[0] != "127.0.0.1" or identity[3] != 5432):
                result["failure_code"] = "POSTGRES_IDENTITY_MISMATCH"
                return 2
            if extension is not None:
                from alembic.config import Config
                from alembic.script import ScriptDirectory
                exists = conn.scalar(text("SELECT to_regclass('public.alembic_version') IS NOT NULL"))
                versions = list(conn.execute(text("SELECT version_num FROM public.alembic_version")).scalars()) if exists else []
                expected = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini"))).get_heads()
                result["postgresql_migration"] = {"observed": sorted(versions), "expected": sorted(expected)}
                if sorted(versions) != sorted(expected):
                    raise HostBlocked("FORMAL_DATABASE_MIGRATION_NOT_CURRENT")
                result["checks"]["formal_migration_status_read_only"] = "passed"
            can_create = bool(identity[4])
        if not can_create:
            if not sys.stdin.isatty():
                result["failure_code"] = "INTERACTIVE_POSTGRES_ADMIN_REQUIRED"
                return 2
            from getpass import getpass
            admin_password = getpass("PostgreSQL postgres administrator password (input hidden): ")
            if not admin_password:
                result["failure_code"] = "POSTGRES_ADMIN_CREDENTIAL_MISSING"
                return 2
            admin.dispose()
            admin = create_engine(url.set(username="postgres", password=admin_password, database="postgres"),
                                  connect_args={"connect_timeout": 3}, isolation_level="AUTOCOMMIT",
                                  hide_parameters=True)
            del admin_password
            with admin.connect() as conn:
                administrator = conn.execute(text("SELECT current_user, inet_server_addr()::text, "
                    "inet_server_port(), (SELECT rolcreatedb OR rolsuper FROM pg_roles WHERE rolname=current_user)" )).one()
                if (administrator[0] != "postgres" or str(administrator[1]).split("/")[0] != "127.0.0.1"
                        or administrator[2] != 5432 or not administrator[3]):
                    result["failure_code"] = "POSTGRES_ADMIN_IDENTITY_MISMATCH"
                    return 2
        with admin.connect() as conn:
            if conn.scalar(text("SELECT 1 FROM pg_database WHERE datname=:name"), {"name": dbname}):
                raise CheckFailure("test_database_name_collision")
            conn.execute(text('CREATE DATABASE "' + dbname + '" OWNER agentforge_app'))
            created = True
            result["cleanup"]["database"] = "pending"
        engine = create_engine(url.set(database=dbname), connect_args={"connect_timeout": 3},
                               pool_size=2, max_overflow=0, pool_timeout=5, hide_parameters=True)
        from agentforge.conversation_migrations import upgrade
        upgrade(engine)
        result["checks"]["isolated_postgresql_migration"] = "passed"
        result["stage"] = "model_readiness"
        from AF012_MVP_HOST_ACCEPTANCE import _health_ready
        result["chat_health"] = _health_ready(56572, timeout_seconds=120)
        from agentforge.af012_runtime import embedding_provider_from_environment
        provider = embedding_provider_from_environment()
        provider.wait_ready(timeout=120.0, poll_interval=1.0)
        result["checks"]["both_models_ready"] = "passed"
        parent = ROOT / "tests" / ".tmp"
        parent.mkdir(exist_ok=True)
        temp = tempfile.TemporaryDirectory(prefix="af0111-host-" + run_id + "-", dir=parent)
        base = Path(temp.name)
        result["cleanup"]["temporary_directory"] = "pending"
        repos = []
        from agentforge.store import IndexStore
        from agentforge.indexer import index_repository
        for name, marker in (("alpha", "alpha"), ("beta", "beta")):
            repo = base / name
            repo.mkdir()
            (repo / "shared.py").write_text("def shared(value, amount):\n    return value + amount  # " + marker + "\n", encoding="utf-8")
            index = IndexStore(":memory:")
            index_repository(repo, index)
            indexes.append(index)
            repos.append(repo)
        import chromadb
        from chromadb.config import Settings
        from agentforge.embeddings.chroma_index import ChromaCodeIndex, repository_identity
        from agentforge.embeddings.chroma_store import ChromaCandidateStore
        from agentforge.conversation_store import ConversationStore
        from agentforge.memory_store import MemoryStore, GLOBAL_MEMORY_REPOSITORY_ID
        from agentforge.memory_retrieval import ChromaMemoryRetriever
        result["stage"] = "chroma_indexing"
        client = chromadb.PersistentClient(path=str(base / "chroma"), settings=Settings(anonymized_telemetry=False))
        collection = client.create_collection("af0111_joint_" + run_id, embedding_function=None,
                                              configuration={"hnsw": {"space": "cosine"}})
        ids = [repository_identity(repo) for repo in repos]
        for index in indexes:
            ChromaCodeIndex(index, collection, provider).sync()
        for index, marker in zip(indexes, ("alpha", "beta")):
            found = ChromaCodeIndex(index, collection, provider).search("shared", top_k=5)
            if len(found) != 1 or marker not in found[0].text:
                raise CheckFailure("chroma_repository_isolation_failed")
        result["checks"]["real_embedding_two_repository_isolation"] = "passed"
        result["stage"] = "memory_scope"
        conversations, memories = ConversationStore(engine), MemoryStore(engine)
        sessions = [conversations.create_conversation(repo_id, "isolated synthetic") for repo_id in ids]
        conversations.append_turn(sessions[0]["id"], ids[0], "Use concise answers.", "Acknowledged.")
        source = conversations.get_messages(sessions[0]["id"], ids[0])[0]["id"]
        private = memories.propose(ids[0], sessions[0]["id"], "Private alpha preference.", [source], memory_type="preference")
        shared = memories.propose(ids[0], sessions[0]["id"], "Global concise preference.", [source], memory_type="preference", scope="global")
        memories.approve(private["id"], ids[0]); memories.approve(shared["id"], GLOBAL_MEMORY_REPOSITORY_ID)
        sync = memories.sync_outbox(ChromaCandidateStore(collection, dimension=provider.dimension), provider)
        if sync["completed"] != 2 or sync["failed"]:
            raise CheckFailure("memory_sync_failed")
        found = ChromaMemoryRetriever(collection, provider, memories).search("answer preference", repository_id=ids[1])
        if {m.content for m in found} != {"Global concise preference."}:
            raise CheckFailure("memory_scope_failed")
        result["checks"]["global_and_private_memory_scope"] = "passed"
        for number in range(30):
            facts = {0: "EARLY_DECISION ALPHA731", 14: "MID_CONSTRAINT BETA842", 29: "RECENT_TASK GAMMA953"}
            conversations.append_turn(sessions[0]["id"], ids[0], facts.get(number, "ordinary turn " + str(number)), "Acknowledged.")
        result["stage"] = "semantic_summary"
        from agentforge.providers.llama_server import LlamaServerClient
        chat = LlamaServerClient("http://127.0.0.1:56572/v1/chat/completions", api_key=key,
                                 read_timeout=120, total_timeout=180, max_output_tokens=512)
        from agentforge.cli import _semantic_summary_callback
        summarize = _semantic_summary_callback(type("Provider", (), {"client": chat, "model": "qwen3.5-4b"})())
        try:
            rows, summary = conversations.prepare_history(
                sessions[0]["id"], ids[0], summarizer=summarize,
                summary_model="qwen3.5-4b")
        except Exception as summary_exc:
            result["summary_diagnostics"] = list(conversations.last_summary_diagnostics)
            summary_attempts = result["summary_diagnostics"]
            last_diag = summary_attempts[-1] if summary_attempts else {}
            failed_phase = last_diag.get("failure_stage")
            failure_stage = _summary_failure_stage(last_diag)
            result["stage"] = failure_stage
            attempted = any(bool(item.get("request_attempted")) for item in summary_attempts)
            committed = any(bool(item.get("committed")) for item in summary_attempts)
            if failure_stage == "summary_generation":
                phase_statuses = {"summary_generation": "failed",
                                  "summary_validation": "not_started",
                                  "summary_persistence": "not_started"}
            elif failure_stage == "summary_persistence":
                phase_statuses = {"summary_generation": "completed",
                                  "summary_validation": "completed",
                                  "summary_persistence": "failed"}
            else:
                phase_statuses = {
                    "summary_generation": "completed" if attempted else "not_started",
                    "summary_validation": "failed",
                    "summary_persistence": "completed" if committed else "not_started",
                }
            for phase, status in phase_statuses.items():
                result.setdefault("stage_results", {})[phase] = {"status": status}
            result["stage_results"][failure_stage].update({
                "failure_stage": failed_phase,
                "failure_code": last_diag.get("failure_code", type(summary_exc).__name__),
            })
            result.setdefault("stage_results", {})["semantic_summary"] = {
                "status": "failed",
                "requests_attempted": sum(bool(item.get("request_attempted"))
                                           for item in summary_attempts),
                "cursor_after_last_attempt": (summary_attempts[-1].get("cursor_after")
                                               if summary_attempts else 0),
                "diagnostics": summary_attempts,
            }
            raise
        result["summary_diagnostics"] = list(conversations.last_summary_diagnostics)
        summary_attempts = result["summary_diagnostics"]
        result["stage_results"]["summary_generation"] = {
            "status": "completed",
            "requests_attempted": sum(bool(item.get("request_attempted"))
                                       for item in summary_attempts),
            "input_json_bytes_total": sum(item.get("input_json_bytes", 0) or 0
                                           for item in summary_attempts),
            "output_bytes_total": sum(item.get("output_bytes", 0) or 0
                                      for item in summary_attempts),
        }
        result["stage_results"]["summary_validation"] = {
            "status": "completed",
            "checks_passed": sum(item.get("fact_check") == "passed"
                                  for item in summary_attempts),
            "fact_check_failures": sum(item.get("fact_check") == "failed"
                                        for item in summary_attempts),
        }
        result["stage_results"]["summary_persistence"] = {
            "status": "completed",
            "committed_updates": sum(bool(item.get("committed"))
                                     for item in summary_attempts),
            "cursor_after_last_attempt": (summary_attempts[-1].get("cursor_after")
                                          if summary_attempts else 0),
        }
        result.setdefault("stage_results", {})["semantic_summary"] = {
            "status": "completed",
            "requests_attempted": sum(bool(item.get("request_attempted"))
                                       for item in conversations.last_summary_diagnostics),
            "fact_check_failures": sum(item.get("fact_check") == "failed"
                                       for item in conversations.last_summary_diagnostics),
            "committed_updates": sum(bool(item.get("committed"))
                                     for item in conversations.last_summary_diagnostics),
            "cursor_after_last_attempt": (
                conversations.last_summary_diagnostics[-1].get("cursor_after")
                if conversations.last_summary_diagnostics else 0),
            "diagnostics": list(conversations.last_summary_diagnostics),
        }
        result["summary"] = {"text": summary["content"], "covered_message_id": summary["covered_message_id"],
                             "recent_message_count": len(rows),
                             "covered_through": summary["covered_through"],
                             "fact_sources": [
                                 {"fact_id": fact["fact_id"], "kind": fact["kind"],
                                  "sources": [{"message_id": source["message_id"],
                                               "sequence": source["sequence"],
                                               "source_sha256": source["source_sha256"]}
                                              for source in fact["sources"]]}
                                 for fact in summary.get("fact_ledger", [])
                             ],
                             "history_origin": "synthetic_seeded_turns_not_real_model_turns"}
        result["chat_requests"].append(chat.request_stats())
        if not all(marker in summary["content"] for marker in ("ALPHA731", "BETA842")):
            raise CheckFailure("semantic_summary_missing_facts")
        result["checks"]["qwen_semantic_summary_30_turns"] = "passed"
        if extension is not None:
            result["stage"] = "extended_acceptance"
            extension({"root": ROOT, "base": base, "engine": engine, "repos": repos,
                       "indexes": indexes, "ids": ids, "provider": provider,
                       "client": client, "collection": collection,
                       "conversations": conversations, "memories": memories,
                       "sessions": sessions, "shared_memory": shared,
                       "private_memory": private, "observations": observations}, result)
        result["stage"] = "completed_partial"
        result["status"] = "PARTIAL"
        return 0
    except HostBlocked as exc:
        result["status"] = "BLOCKED"
        result["failure_stage"] = result["stage"]
        result["failure_code"] = str(exc)
        result["stage_results"].setdefault(result["stage"], {})["status"] = "blocked"
        return 2
    except Exception as exc:
        if (type(exc).__name__ in ("OperationalError", "InterfaceError", "ConnectionError", "TimeoutError")
                or (result["stage"] == "model_readiness" and
                    type(exc).__name__ in ("EmbeddingError", "AcceptanceFailure"))):
            result["status"] = "BLOCKED"
            result["error_type"] = type(exc).__name__
            result["failure_stage"] = result["stage"]
            result["failure_code"] = "SERVICE_UNAVAILABLE_OR_NOT_READY"
            result["stage_results"].setdefault(result["stage"], {})["status"] = "blocked"
            if result["stage"] == "database_preflight":
                result["database_diagnostic"] = _connection_diagnostic(exc)
            return 2
        result["status"] = "FAIL"
        result["error_type"] = type(exc).__name__
        result["exception_location"] = _exception_location(exc)
        result["failure_stage"] = result["stage"]
        result["failure_code"] = _failure_code(exc, result["stage"])
        result["stage_results"].setdefault(result["stage"], {})["status"] = "failed"
        if result["stage"] == "semantic_summary":
            result["failure_diagnostic"] = {
                "stage": result["stage"],
                "error_type": type(exc).__name__,
                "failure_code": result["failure_code"],
                "exception_message_saved": False,
            }
        return 1
    finally:
        if provider is not None:
            result["embedding_requests"] = provider.request_counts
        if observations is not None:
            result["request_observations"] = observations
            chat_records = observations.get("chat", [])
            totals = _chat_request_totals(chat_records)
            result["chat_requests"] = totals
            result["chat_request_totals"] = totals
            result["chat_requests_scope"] = "all_chat_request_observation_deltas"
            phase_status = (
                "not_started" if totals["attempted"] == 0
                else "failed" if totals["failed"] > 0
                else "completed"
            )
            result.setdefault("stage_results", {})["chat_request"] = {
                "status": phase_status, **totals,
            }
        if chat is not None:
            result["summary_provider_stats"] = chat.request_stats()
        for index in indexes:
            index.close()
        if engine is not None:
            engine.dispose()
        if not _close_owned_chroma(client, result["cleanup"]):
            result["status"] = "FAIL"
        if created and admin is not None:
            try:
                with admin.connect() as conn:
                    conn.execute(__import__("sqlalchemy").text('DROP DATABASE "' + dbname + '"'))
                result["cleanup"]["database"] = "passed"
            except Exception as exc:
                result["cleanup"]["database"] = "failed_" + type(exc).__name__
                result["status"] = "FAIL"
        if admin is not None:
            admin.dispose()
        if not _cleanup_owned_temp(temp, result["cleanup"]):
            result["status"] = "FAIL"
        if finalizer is not None:
            try:
                finalizer(result)
            except Exception as exc:
                result["integrity_finalizer"] = {"status": "not_measured", "error_type": type(exc).__name__}
                if result["status"] == "PASS":
                    result["status"] = "PARTIAL"
        result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
        if extension is not None:
            lines = ["# AgentForge 1.1 host acceptance", "", f"Status: {result['status']}",
                     f"Stage: {result['stage']}",
                     f"Failure stage: {result.get('failure_stage', 'not_applicable')}",
                     f"Failure code: {result.get('failure_code')}",
                     "", "## Checks", ""]
            lines.extend(f"- {name}: {state}" for name, state in result["checks"].items())
            if result.get("stage_results"):
                lines.extend(["", "## Stage Results", ""])
                for name, details in result["stage_results"].items():
                    lines.append(f"- {name}: {details.get('status', 'unknown')}")
            diagnostic = result.get("cli_failure_diagnostic")
            if isinstance(diagnostic, dict):
                lines.extend(["", "## CLI Failure Diagnostic", ""])
                for key in ("stage", "operation", "failure_kind", "exit_code",
                            "exception_type", "message_truncated", "stderr_truncated"):
                    if key in diagnostic:
                        lines.append(f"- {key}: {diagnostic[key]}")
                detail = (diagnostic.get("stderr_redacted") or
                          diagnostic.get("exception_message_redacted"))
                if detail:
                    lines.extend(["", "```text", detail, "```"])
            lines.extend(["", "## Unmeasured", ""])
            lines.extend(f"- {name}" for name in result.get("unmeasured", []))
            lines.extend(["", "Detailed responses, evidence, request counters and cleanup are in the adjacent JSON file."])
            result_path.with_suffix(".md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(json.dumps({"result_file": str(result_path), "status": result["status"],
                          "stage": result["stage"],
                          "failure_stage": result.get("failure_stage"),
                          "failure_code": result.get("failure_code"),
                          "stage_results": result.get("stage_results", {}),
                          "checks": result["checks"], "cleanup": result["cleanup"]}, ensure_ascii=False, indent=2))
        if result["status"] == "FAIL":
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
