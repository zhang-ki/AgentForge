"""Command-line interface. Indexing and the explicit agent index tool write only the fixed index."""

import argparse
from dataclasses import asdict
import hashlib
import json
import os
import re
import sqlite3
import sys

from .agent_loop import AgentLoop, OfflineAgentPlanner, ReadOnlyToolbox
from .answer_contract import (
    build_offline_contract, validate_answer_contract, verify_generated_answer,
)
from .config import DATABASE_PATH
from .context import build_context
from .errors import AgentForgeError, ProviderError
from .indexer import index_repository
from .providers.llama_server import (
    LlamaServerClient, LlamaServerProvider, validate_output_tokens, validate_timeout,
)
from .providers.offline import OfflineProvider
from .product_retrieval import ProductRetriever
from .retriever import search
from .store import IndexStore


def _timeout_argument(value: str, name: str, minimum: float, maximum: float, *, inclusive_minimum: bool) -> float:
    try:
        parsed = float(value)
        return validate_timeout(parsed, name, minimum, maximum, inclusive_minimum=inclusive_minimum)
    except (ValueError, TypeError):
        lower = f"at least {minimum:g}" if inclusive_minimum else f"greater than {minimum:g}"
        raise argparse.ArgumentTypeError(
            f"{name} must be a finite number {lower} and at most {maximum:g} seconds."
        ) from None


def _connect_timeout_argument(value: str) -> float:
    return _timeout_argument(value, "connect-timeout", 0, 120, inclusive_minimum=False)


def _read_timeout_argument(value: str) -> float:
    return _timeout_argument(value, "read-timeout", 5, 120, inclusive_minimum=True)


def _positive_timeout_argument(value: str) -> float:
    return _timeout_argument(value, "tool-timeout", 0, 120, inclusive_minimum=False)


def create_provider(
    provider_name: str = "offline",
    base_url: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    connect_timeout: float = 2.0, read_timeout: float = 60.0,
    total_timeout: float = 180.0, max_output_tokens: int = 256,
):
    """Build the explicitly requested ask provider; constructing the client makes no request."""
    if provider_name == "offline":
        if (base_url is not None or model is not None or connect_timeout != 2.0
                or read_timeout != 60.0 or total_timeout != 180.0 or max_output_tokens != 256):
            raise ValueError(
                "llama-server endpoint, model, timeout and output-limit options require --provider llama-server."
            )
        return OfflineProvider()
    if provider_name != "llama-server":
        raise ValueError("Provider must be offline or llama-server.")
    if base_url is None or not base_url.strip():
        raise ValueError("--base-url is required for --provider llama-server; no endpoint is guessed.")
    if model is None or not model.strip():
        raise ValueError("--model is required for --provider llama-server.")
    if api_key is None or not api_key.strip():
        raise ValueError("AGENTFORGE_LLAMA_API_KEY is required for --provider llama-server.")
    try:
        client = LlamaServerClient(base_url, api_key=api_key, connect_timeout=connect_timeout,
                                   read_timeout=read_timeout, total_timeout=total_timeout,
                                   max_output_tokens=max_output_tokens)
    except ValueError as exc:
        if "API key" in str(exc):
            raise ValueError(f"Invalid AGENTFORGE_LLAMA_API_KEY: {exc}") from None
        if "timeout" in str(exc).lower():
            raise ValueError(str(exc)) from None
        raise ValueError(f"Invalid --base-url for llama-server: {exc}") from None
    return LlamaServerProvider(client, model.strip())


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        description="AgentForge: read-only repository search with offline summaries or explicitly configured local generation."
    )
    commands = root.add_subparsers(dest="command", required=True)
    index = commands.add_parser("index", help="Index one explicitly supplied repository.")
    index.add_argument("--repo", required=True, help="Explicit repository root; no default scan path.")
    index.add_argument("--window-lines", type=int, default=80)
    index.add_argument("--overlap-lines", type=int, default=10)
    index.add_argument("--replace-repository", action="store_true", help="Explicitly replace index records when switching repository roots.")
    for name, help_text in (("search", "Lexical search with source lines."),
                            ("ask", "Offline evidence summary or explicitly selected local model generation.")):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("query")
        command.add_argument("--top-k", type=int, default=5)
        if name == "ask":
            command.add_argument("--conversation-id", help="Persist and use this repository-scoped conversation UUID.")
            command.add_argument("--use-chroma", action="store_true",
                                 help="Use the persistent local Chroma code and approved-memory candidates.")
            command.add_argument("--context-budget", type=int, default=6000, help="Conservative UTF-8-byte token proxy, not exact tokens.")
            command.add_argument(
                "--provider", choices=("offline", "llama-server"), default="offline",
                help="offline (default, deterministic evidence summary) or llama-server (local model generation).",
            )
            command.add_argument(
                "--base-url",
                help="Explicit loopback HTTP URL for llama-server, for example http://<loopback-ip>:<port>/v1/chat/completions.",
            )
            command.add_argument("--model", help="Explicit llama-server model identifier; required for llama-server.")
            command.add_argument(
                "--connect-timeout", type=_connect_timeout_argument, default=2.0, metavar="SECONDS",
                help="TCP connection timeout for llama-server (default: 2; greater than 0 and at most 120).",
            )
            command.add_argument(
                "--read-timeout", type=_read_timeout_argument, default=60.0, metavar="SECONDS",
                help="Maximum wait for request I/O, response headers and body (default: 60; range: 5–120).",
            )
            _add_chat_limits(command)
            command.epilog = (
                "For --provider llama-server, set AGENTFORGE_LLAMA_API_KEY in the process environment. "
                "The key is never accepted as a command-line argument or saved by AgentForge."
            )
    symbols = commands.add_parser("symbols", help="Query structured symbols from the current index.")
    symbols.add_argument("--name", help="Exact case-insensitive symbol name.")
    symbols.add_argument("--qualified-name", help="Exact case-insensitive qualified symbol name.")
    symbols.add_argument("--kind", choices=("module", "class", "function", "method"))
    symbols.add_argument("--path", help="Exact repository-relative source path.")
    symbols.add_argument("--limit", type=int, default=100)
    references = commands.add_parser("references", help="Query statically extracted imports and calls.")
    references.add_argument("--path", help="Exact repository-relative source path.")
    references.add_argument("--target", help="Exact reference target or qualified target.")
    references.add_argument("--status", choices=("resolved", "unresolved", "ambiguous"))
    references.add_argument(
        "--type", choices=("import", "from_import", "call", "dynamic_import"),
        dest="reference_type",
    )
    references.add_argument("--limit", type=int, default=100)
    agent = commands.add_parser(
        "agent", help="Run a bounded read-only tool loop with the deterministic offline planner."
    )
    agent.add_argument("query")
    agent.add_argument("--repo", required=True, help="Explicit repository root; no default scan path.")
    agent.add_argument("--top-k", type=int, default=5)
    agent.add_argument("--max-steps", type=int, default=4, choices=range(1, 9), metavar="1..8")
    agent.add_argument(
        "--tool-timeout", type=_positive_timeout_argument, default=30.0, metavar="SECONDS",
        help="Per-tool cooperative wall-clock budget (default: 30; greater than 0 and at most 120).",
    )
    agent.add_argument(
        "--replace-repository", action="store_true",
        help="Explicitly replace index records when switching repository roots.",
    )
    commands.add_parser("status", help="Show fixed index path, counts and last update.")
    _add_session_commands(commands)
    _add_chroma_commands(commands)
    _add_memory_commands(commands)
    return root


def _print_json(value) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def main(argv=None) -> int:
    arguments = parser().parse_args(argv)
    engine = None
    try:
        if arguments.command == "session":
            _print_json(_session_command(arguments))
            return 0
        if arguments.command == "chroma":
            _print_json(_chroma_command(arguments))
            return 0
        if arguments.command == "memory":
            _print_json(_memory_command(arguments))
            return 0
        provider = None
        if arguments.command == "ask":
            provider = create_provider(arguments.provider, arguments.base_url, arguments.model,
                api_key=os.environ.get("AGENTFORGE_LLAMA_API_KEY"),
                connect_timeout=arguments.connect_timeout, read_timeout=arguments.read_timeout,
                total_timeout=arguments.total_timeout, max_output_tokens=arguments.max_output_tokens)
        if arguments.command == "status" and not DATABASE_PATH.exists():
            _print_json({"database": str(DATABASE_PATH), "exists": False, "files": 0, "chunks": 0, "indexed_at": None})
            return 0
        with IndexStore(readonly=arguments.command not in ("index", "agent")) as store:
            if arguments.command == "index":
                _print_json(index_repository(arguments.repo, store,
                    window_lines=arguments.window_lines, overlap_lines=arguments.overlap_lines,
                    replace_repository=arguments.replace_repository))
            elif arguments.command == "status":
                _print_json(store.status())
            elif arguments.command == "search":
                results = search(store, arguments.query, arguments.top_k)
                _print_json({"query": arguments.query, "results": [asdict(result) for result in results]})
            elif arguments.command == "symbols":
                _print_json({"results": store.search_symbols(
                    name=arguments.name, qualified_name=arguments.qualified_name,
                    kind=arguments.kind, relative_path=arguments.path, limit=arguments.limit,
                )})
            elif arguments.command == "references":
                _print_json({"results": store.search_references(
                    relative_path=arguments.path, target=arguments.target,
                    status=arguments.status, reference_type=arguments.reference_type,
                    limit=arguments.limit,
                )})
            elif arguments.command == "agent":
                report = _run_offline_agent(arguments, store)
                _print_json(asdict(report))
                return 0 if report.status == "completed" else 1
            elif arguments.command == "ask":
                repository = store.metadata().get("repository")
                if not repository: raise ValueError("No repository is indexed; run index --repo PATH first.")
                conversation_store = engine = conversation = None
                memory_count = 0
                if arguments.conversation_id:
                    engine = _conversation_engine()
                    conversation_store, repo_id, rows, conversation = _load_conversation(engine, arguments.conversation_id, repository)
                vector_retriever = None
                chroma_client = None
                if arguments.use_chroma:
                    from .af012_runtime import create_persistent_chroma, embedding_provider_from_environment, collection_for_model
                    from .embeddings.chroma_index import ChromaCodeIndex
                    embedding_provider = embedding_provider_from_environment()
                    embedding_readiness_seconds = embedding_provider.wait_ready()
                    chroma_client = create_persistent_chroma()
                    collection = collection_for_model(chroma_client, embedding_provider.model_id,
                                                       embedding_provider.dimension)
                    vector_retriever = ChromaCodeIndex(store, collection, embedding_provider)
                    if conversation is not None:
                        from .memory_retrieval import ChromaMemoryRetriever
                        from .memory_store import MemoryStore
                        memory_store = MemoryStore(engine)
                        conversation = dict(conversation)
                        conversation["memories"] = tuple(ChromaMemoryRetriever(
                            collection, embedding_provider, memory_store).search(
                                arguments.query, repository_id=repo_id))
                        memory_count = len(conversation["memories"])
                retriever = ProductRetriever(store, repository, vector_retriever=vector_retriever)
                outcome = retriever.search(arguments.query, top_k=arguments.top_k)
                outcome, followup_symbol = _conversation_retrieval(outcome, retriever, conversation, arguments, store, rows if conversation else ())
                results = [item.as_search_result() for item in _context_candidates(outcome, conversation)]
                context = build_context(
                    arguments.query, results,
                    max_candidates=arguments.top_k, budget=arguments.context_budget,
                )
                candidate_map = {item.citation: item for item in outcome.candidates}
                evidence_gate_refusal = bool(
                    outcome.decision.confidence == "low" and outcome.uncertainty
                )
                if evidence_gate_refusal:
                    answer = OfflineProvider().answer(context)
                    contract = build_offline_contract(
                        answer, context, route=outcome.decision.route,
                        route_confidence=outcome.decision.confidence,
                        candidate_by_citation=candidate_map, uncertainty=outcome.uncertainty,
                    )
                    provider_mode = "evidence_gate_refusal"
                else:
                    answer = (provider.answer(context, conversation=conversation)
                              if conversation is not None and isinstance(provider, LlamaServerProvider)
                              else provider.answer(context))
                    if answer.offline:
                        contract = build_offline_contract(
                            answer, context, route=outcome.decision.route,
                            route_confidence=outcome.decision.confidence,
                            candidate_by_citation=candidate_map, uncertainty=outcome.uncertainty,
                        )
                        provider_mode = (
                            "offline" if arguments.provider == "offline"
                            else "offline_no_evidence"
                        )
                    else:
                        try:
                            contract = verify_generated_answer(
                                answer, context, route=outcome.decision.route,
                                route_confidence=outcome.decision.confidence,
                                candidate_by_citation=candidate_map, uncertainty=outcome.uncertainty,
                            )
                        except ProviderError:
                            provider.client.record_application_failure(
                                "answer_contract", "answer_contract_failed"
                            )
                            raise
                        provider_mode = "llama-server"
                validate_answer_contract(contract, context)
                if conversation_store is not None:
                    _persist_conversation(conversation_store, arguments, repo_id, answer, provider_mode)
                request_stats = (
                    provider.request_stats()
                    if isinstance(provider, LlamaServerProvider) else None
                )
                injected_memories = _injected_memory_metadata(conversation)
                _print_json({
                    "provider": provider_mode,
                    "answer": answer.text if contract.status != "refused" else None,
                    "refused_output": answer.text if contract.status == "refused" else None,
                    "answer_contract": asdict(contract),
                    "retrieval": {
                        "route": outcome.decision.route,
                        "rule": outcome.decision.rule,
                        "reason": outcome.decision.reason,
                        "confidence": outcome.decision.confidence,
                        "uncertainty": outcome.uncertainty,
                        "unavailable_channels": list(outcome.unavailable_channels),
                        "conversation_lookup_symbol": followup_symbol,
                    },
                    "context_budget": {
                        "estimated_units": context.estimated_tokens,
                        "limit": context.budget,
                        "estimate_kind": context.estimate_kind,
                    },
                    "persistent_chroma": {"enabled": bool(arguments.use_chroma),
                                          "memory_retrieved_count": memory_count,
                                          "memory_context_count": len(injected_memories),
                                          "memory_context": injected_memories,
                                          "embedding_model_ready_seconds": (
                                              embedding_readiness_seconds if arguments.use_chroma else None)},
                    "provider_request_stats": request_stats,
                })
        return 0
    except ValueError as exc:
        print(f"Invalid configuration: {exc}", file=sys.stderr)
        return 2
    except (AgentForgeError, OSError, sqlite3.Error) as exc:
        detail = str(exc) if isinstance(exc, AgentForgeError) else type(exc).__name__
        print(f"AgentForge error: {detail}", file=sys.stderr)
        return 1
    finally:
        if engine is not None:
            engine.dispose()


def _total_timeout_argument(value: str) -> float:
    return _timeout_argument(value, "total-timeout", 10, 300, inclusive_minimum=True)


def _output_tokens_argument(value: str) -> int:
    try:
        return validate_output_tokens(int(value))
    except (ValueError, TypeError):
        raise argparse.ArgumentTypeError(
            "max-output-tokens must be an integer between 1 and 512."
        ) from None


def _add_chat_limits(command) -> None:
    command.add_argument(
        "--total-timeout", type=_total_timeout_argument, default=180.0, metavar="SECONDS",
        help="Whole local generation deadline (default: 180; range: 10–300).",
    )
    command.add_argument(
        "--max-output-tokens", type=_output_tokens_argument, default=256, metavar="TOKENS",
        help="Maximum generated tokens (default: 256; range: 1–512).",
    )



def _add_session_commands(commands):
    session = commands.add_parser("session", help="Manage PostgreSQL conversations for the indexed repository.")
    actions = session.add_subparsers(dest="session_action", required=True)
    actions.add_parser("migrate", help="Apply the conversation schema migration to the dedicated database.")
    actions.add_parser("health", help="Check connectivity to the dedicated local database.")
    create = actions.add_parser("create")
    create.add_argument("title")
    actions.add_parser("list")
    actions.add_parser("current")
    for name in ("show", "delete", "switch"):
        action = actions.add_parser(name)
        action.add_argument("conversation_id")
        if name == "delete":
            action.add_argument("--confirm", action="store_true", required=True,
                                help="Confirm deletion of this conversation and its history.")
    rename = actions.add_parser("rename")
    rename.add_argument("conversation_id")
    rename.add_argument("title")


def _add_chroma_commands(commands):
    chroma = commands.add_parser("chroma", help="Manage explicit persistent local Chroma vectors.")
    actions = chroma.add_subparsers(dest="chroma_action", required=True)
    actions.add_parser("init", help="Create the local collection without contacting an embedding service.")
    actions.add_parser("status", help="Show local Chroma directory and collection names.")
    actions.add_parser("index", help="Embed and synchronize the currently indexed repository.")
    actions.add_parser("rebuild", help="Re-embed the current repository, then prune stale vectors.")
    search_command = actions.add_parser("search", help="Search Chroma and verify candidates against the current SQLite snapshot.")
    search_command.add_argument("query")
    search_command.add_argument("--top-k", type=int, default=5)


def _add_memory_commands(commands):
    memory = commands.add_parser("memory", help="Review approved long-term memory for the indexed repository.")
    actions = memory.add_subparsers(dest="memory_action", required=True)
    listing = actions.add_parser("list")
    listing.add_argument("--status", choices=("pending", "approved", "rejected", "revoked", "expired"))
    listing.add_argument("--limit", type=int, default=100)
    show = actions.add_parser("show")
    show.add_argument("memory_id")
    for name in ("approve", "reject", "revoke"):
        action = actions.add_parser(name)
        action.add_argument("memory_id")
    update_memory = actions.add_parser("update")
    update_memory.add_argument("memory_id")
    update_memory.add_argument("content")
    update_memory.add_argument("--type", choices=("preference", "project_fact", "workflow", "other"))
    update_memory.add_argument("--expires-in-days", type=int)
    propose = actions.add_parser("propose")
    propose.add_argument("conversation_id")
    propose.add_argument("content")
    propose.add_argument("--source-message-id", action="append", required=True)
    propose.add_argument("--type", choices=("preference", "project_fact", "workflow", "other"), default="other")
    propose.add_argument("--expires-in-days", type=int)
    suggest = actions.add_parser("suggest")
    suggest.add_argument("conversation_id")
    suggest.add_argument("--base-url", required=True)
    suggest.add_argument("--model", required=True)
    actions.add_parser("sync", help="Process pending memory-to-Chroma outbox records once.")
    actions.add_parser("status", help="Show pending/completed/dead memory-vector sync counts.")
    actions.add_parser("expire", help="Expire approved memories whose configured lifetime elapsed.")


def _repository_id(repository: str) -> str:
    return hashlib.sha256(os.path.normcase(os.path.abspath(repository)).encode("utf-8")).hexdigest()


def _run_offline_agent(arguments, store):
    toolbox = ReadOnlyToolbox(arguments.repo, store)
    planner = OfflineAgentPlanner(toolbox.repository, top_k=arguments.top_k,
                                 replace_repository=arguments.replace_repository)
    return AgentLoop(toolbox, planner, max_steps=arguments.max_steps,
        tool_timeout_seconds=arguments.tool_timeout,
        run_timeout_seconds=min(600.0, max(120.0, arguments.tool_timeout * arguments.max_steps))).run(arguments.query)


def _conversation_retrieval(outcome, retriever, conversation, arguments, store, rows):
    symbol = None
    missing_named_symbol = False
    if conversation is not None and outcome.decision.route == "exact_symbol" and _is_followup(arguments.query):
        named = retriever._symbol_name(arguments.query)
        missing_named_symbol = bool(named and not store.search_symbols(name=named, limit=1))
    if conversation is not None and (outcome.decision.confidence == "low" or missing_named_symbol):
        symbol = _unique_mentioned_symbol(arguments.query, store) or _followup_symbol(arguments.query, rows, store)
        if symbol:
            outcome = retriever.search(symbol, top_k=arguments.top_k)
    return outcome, symbol


def _context_candidates(outcome, conversation):
    """For session exact-symbol questions, omit unrelated lexical fallback evidence."""
    if conversation is not None and outcome.decision.route == "exact_symbol":
        symbols = tuple(item for item in outcome.candidates if item.symbol_ids)
        if symbols:
            return symbols
    return outcome.candidates


def _load_conversation(engine, conversation_id, repository):
    from .conversation_store import ConversationStore
    from .conversation_context import ConversationTurn
    from sqlalchemy.exc import SQLAlchemyError
    store = ConversationStore(engine)
    repo_id = _repository_id(repository)
    try:
        store.get_conversation(conversation_id, repo_id)
        rows, summary = store.prepare_history(conversation_id, repo_id)
    except SQLAlchemyError:
        raise ValueError("PostgreSQL conversation storage is unavailable or not migrated.") from None
    covered = summary["covered_through"] if summary else 0
    conversation = {"conversation_id": conversation_id, "repository_id": repo_id,
        "history": tuple(ConversationTurn(conversation_id, repo_id, row["sequence"], row["role"],
                                          row["content"], row["status"])
                         for row in rows if row["sequence"] > covered),
        "summary": summary["content"] if summary else None}
    return store, repo_id, rows, conversation


def _persist_conversation(store, arguments, repo_id, answer, provider_mode):
    from sqlalchemy.exc import SQLAlchemyError
    try:
        store.append_turn(arguments.conversation_id, repo_id, arguments.query, answer.text,
                          model_name=arguments.model if provider_mode == "llama-server" else None)
    except SQLAlchemyError:
        raise ValueError("Could not persist the completed turn; no successful response was committed.") from None


def _injected_memory_metadata(conversation):
    """Expose audit metadata only for approved memories actually admitted to context."""
    if not isinstance(conversation, dict):
        return []
    injected_ids = set(conversation.get("injected_memory_ids", ()))
    output = []
    for candidate in conversation.get("memories", ()):
        if candidate.memory_id not in injected_ids:
            continue
        output.append({
            "memory_id": candidate.memory_id,
            "memory_type": candidate.memory_type,
            "version": candidate.version,
            "repository_id": candidate.repository_id,
            "sources": [{
                "message_id": source.message_id,
                "sequence": source.source_sequence,
                "role": source.source_role,
                "source_hash": source.source_hash,
            } for source in candidate.sources],
        })
    return output


def _followup_symbol(query, rows, store):
    """Resolve only one uniquely indexed symbol named in the preceding question."""
    if not _is_followup(query):
        return None
    if len(rows) < 2 or rows[-2]["role"] != "user" or rows[-1]["role"] != "assistant":
        return None
    mentioned = {_unique_mentioned_symbol(row["content"], store)
                 for row in rows[-12:] if row["role"] == "user"}
    mentioned.discard(None)
    return next(iter(mentioned)) if len(mentioned) == 1 else None


def _is_followup(query):
    return bool(re.search(r"(?:它|其|这个|该)|\b(?:it|its|that)\b|\bwere we discussing\b",
                          query, re.IGNORECASE))


def _unique_mentioned_symbol(query, store):
    words = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", query))
    matches = []
    generic = {"what", "which", "does", "function", "class", "method", "module", "symbol",
               "return", "returns", "cite", "evidence", "identify", "the", "and", "for"}
    for word in sorted(words):
        if len(word) < 3 or word.casefold() in generic:
            continue
        found = store.search_symbols(name=word, limit=2)
        if len(found) == 1:
            matches.append(word)
    return matches[0] if len(matches) == 1 else None


def _conversation_engine():
    try:
        from .af012_connection import create_local_engine
    except ImportError:
        raise ValueError("Conversation dependencies are unavailable; use the AF-012 project environment.") from None
    return create_local_engine(os.environ.get("AGENTFORGE_DB_PASSWORD"))


def _session_command(arguments):
    engine = _conversation_engine()
    try:
        from .conversation_store import ConversationStore
        from sqlalchemy.exc import SQLAlchemyError
        if arguments.session_action == "health":
            from .af012_connection import check_database_health
            if not check_database_health(engine):
                raise ValueError("PostgreSQL conversation storage is unavailable.")
            return {"status": "healthy"}
        if arguments.session_action == "migrate":
            from .conversation_migrations import upgrade
            upgrade(engine)
            return {"status": "migrated"}
        with IndexStore(readonly=True) as index:
            repository = index.metadata().get("repository")
        if not repository:
            raise ValueError("No repository is indexed; run index --repo PATH first.")
        repo_id = _repository_id(repository)
        store = ConversationStore(engine)
        from .memory_store import MemoryStore
        MemoryStore(engine).register_repository(repo_id, os.path.abspath(repository))
        action = arguments.session_action
        if action == "create":
            value = store.create_conversation(repo_id, arguments.title)
        elif action == "list":
            value = store.list_conversations(repo_id)
        elif action == "current":
            value = store.current_conversation(repo_id)
        elif action == "switch":
            value = store.switch_conversation(arguments.conversation_id, repo_id)
        elif action == "show":
            value = {"conversation": store.get_conversation(arguments.conversation_id, repo_id),
                     "messages": store.get_messages(arguments.conversation_id, repo_id)}
        elif action == "rename":
            value = store.rename_conversation(arguments.conversation_id, repo_id, arguments.title)
        else:
            store.delete_conversation(arguments.conversation_id, repo_id)
            value = {"deleted": arguments.conversation_id}
        return {"repository": repository, "result": value}
    except SQLAlchemyError:
        raise ValueError("PostgreSQL conversation storage is unavailable or not migrated.") from None
    finally:
        engine.dispose()


def _chroma_command(arguments):
    from .af012_runtime import (CHROMA_ROOT, collection_for_model,
                                create_persistent_chroma, embedding_provider_from_environment)
    action = arguments.chroma_action
    if action == "status":
        if not CHROMA_ROOT.exists():
            return {"path": str(CHROMA_ROOT), "exists": False, "collections": []}
        client = create_persistent_chroma()
        collections = client.list_collections()
        return {"path": str(CHROMA_ROOT), "exists": True,
                "collections": sorted(item.name if hasattr(item, "name") else str(item)
                                      for item in collections)}
    provider = embedding_provider_from_environment()
    if action in ("index", "rebuild", "search"):
        readiness_seconds = provider.wait_ready()
    else:
        readiness_seconds = None
    client = create_persistent_chroma()
    collection = collection_for_model(client, provider.model_id, provider.dimension)
    if action == "init":
        return {"path": str(CHROMA_ROOT), "collection": collection.name,
                "model_id": provider.model_id, "dimension": provider.dimension,
                "status": "ready"}
    with IndexStore(readonly=True) as index:
        repository = index.metadata().get("repository")
        if not repository:
            raise ValueError("No repository is indexed; run index --repo PATH first.")
        from .embeddings.chroma_index import ChromaCodeIndex
        service = ChromaCodeIndex(index, collection, provider)
        if action in ("index", "rebuild"):
            stats = service.sync(force=action == "rebuild")
            return {"repository": repository, "result": asdict(stats),
                    "embedding_model_ready_seconds": readiness_seconds}
        results = service.search(arguments.query, top_k=arguments.top_k)
        return {"repository": repository, "results": [asdict(item) for item in results],
                "verified_against": "current SQLite index snapshot",
                "embedding_model_ready_seconds": readiness_seconds}


def _memory_command(arguments):
    from datetime import datetime, timedelta, timezone
    from sqlalchemy.exc import SQLAlchemyError
    engine = _conversation_engine()
    try:
        with IndexStore(readonly=True) as index:
            repository = index.metadata().get("repository")
        if not repository:
            raise ValueError("No repository is indexed; run index --repo PATH first.")
        repo_id = _repository_id(repository)
        from .conversation_store import ConversationStore
        from .memory_store import MemoryStore
        conversation_store = ConversationStore(engine)
        memory_store = MemoryStore(engine)
        memory_store.register_repository(repo_id, os.path.abspath(repository))
        action = arguments.memory_action
        if action == "list":
            value = memory_store.list(repo_id, status=arguments.status, limit=arguments.limit)
        elif action == "status":
            value = memory_store.outbox_status(repo_id)
        elif action == "expire":
            value = {"expired": memory_store.expire_due()}
        elif action == "show":
            record = memory_store.get(arguments.memory_id, repo_id)
            value = {"memory": record, "sources": memory_store.source_records(arguments.memory_id, repo_id),
                     "events": memory_store.events(arguments.memory_id, repo_id)}
        elif action == "propose":
            expires_at = None
            if arguments.expires_in_days is not None:
                if not 1 <= arguments.expires_in_days <= 3650:
                    raise ValueError("Memory expiration must be from 1 to 3650 days.")
                expires_at = datetime.now(timezone.utc) + timedelta(days=arguments.expires_in_days)
            value = memory_store.propose(repo_id, arguments.conversation_id, arguments.content,
                arguments.source_message_id, memory_type=arguments.type, expires_at=expires_at)
        elif action == "suggest":
            messages = conversation_store.get_messages(arguments.conversation_id, repo_id)
            api_key = os.environ.get("AGENTFORGE_LLAMA_API_KEY")
            from .providers.llama_server import LlamaServerClient
            if not api_key:
                raise ValueError("AGENTFORGE_LLAMA_API_KEY is required for local memory suggestions.")
            client = LlamaServerClient(arguments.base_url, api_key=api_key,
                                       total_timeout=180.0, max_output_tokens=512)
            from .memory_extraction import extract_memory_candidates
            candidates = extract_memory_candidates(client, arguments.model, messages)
            by_sequence = {row["sequence"]: row for row in messages}
            for candidate in candidates:
                candidate["source_message_ids"] = [
                    by_sequence[sequence]["id"] for sequence in candidate["source_sequences"]]
                candidate.pop("source_messages", None)
            value = {"saved": False, "human_approval_required": True,
                     "request_stats": client.request_stats(), "candidates": candidates}
        elif action in ("approve", "reject", "revoke"):
            value = getattr(memory_store, action)(arguments.memory_id, repo_id)
        elif action == "update":
            prior = memory_store.get(arguments.memory_id, repo_id)
            expires_at = prior["expires_at"]
            if arguments.expires_in_days is not None:
                if not 1 <= arguments.expires_in_days <= 3650:
                    raise ValueError("Memory expiration must be from 1 to 3650 days.")
                expires_at = datetime.now(timezone.utc) + timedelta(days=arguments.expires_in_days)
            value = memory_store.update(arguments.memory_id, repo_id, arguments.content,
                memory_type=arguments.type, expires_at=expires_at)
        elif action == "sync":
            from .af012_runtime import collection_for_model, create_persistent_chroma, embedding_provider_from_environment
            provider = embedding_provider_from_environment()
            status = memory_store.outbox_status(repo_id)
            if status.get("pending", 0) or status.get("processing", 0):
                readiness_seconds = provider.wait_ready()
                client = create_persistent_chroma()
                collection = collection_for_model(client, provider.model_id, provider.dimension)
                from .embeddings.chroma_store import ChromaCandidateStore
                value = memory_store.sync_outbox(ChromaCandidateStore(collection, dimension=provider.dimension),
                                                 provider)
            else:
                readiness_seconds = None
                value = {"claimed": 0, "completed": 0, "failed": 0, "dead": 0, "stale_skipped": 0}
            value["embedding_model_ready_seconds"] = readiness_seconds
        else:
            raise ValueError("Unsupported memory action.")
        return {"repository": repository, "result": value}
    except SQLAlchemyError:
        raise ValueError("PostgreSQL storage is unavailable or not migrated.") from None
    finally:
        engine.dispose()


\n