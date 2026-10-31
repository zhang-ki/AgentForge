"""Command-line interface. Indexing and the explicit agent index tool write only the fixed index."""

import argparse
from dataclasses import asdict
import json
import os
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
    return root


def _print_json(value) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def main(argv=None) -> int:
    arguments = parser().parse_args(argv)
    try:
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
                _print_json(index_repository(
                    arguments.repo, store,
                    window_lines=arguments.window_lines, overlap_lines=arguments.overlap_lines,
                    replace_repository=arguments.replace_repository,
                ))
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
                toolbox = ReadOnlyToolbox(arguments.repo, store)
                planner = OfflineAgentPlanner(
                    toolbox.repository, top_k=arguments.top_k,
                    replace_repository=arguments.replace_repository,
                )
                report = AgentLoop(
                    toolbox, planner, max_steps=arguments.max_steps,
                    tool_timeout_seconds=arguments.tool_timeout,
                    run_timeout_seconds=min(600.0, max(120.0, arguments.tool_timeout * arguments.max_steps)),
                ).run(arguments.query)
                _print_json(asdict(report))
                return 0 if report.status == "completed" else 1
            elif arguments.command == "ask":
                repository = store.metadata().get("repository")
                if not repository:
                    raise ValueError("No repository is indexed; run index --repo PATH first.")
                outcome = ProductRetriever(store, repository).search(
                    arguments.query, top_k=arguments.top_k,
                )
                results = [item.as_search_result() for item in outcome.candidates]
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
                    answer = provider.answer(context)
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
                request_stats = (
                    provider.request_stats()
                    if isinstance(provider, LlamaServerProvider) else None
                )
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
                    },
                    "context_budget": {
                        "estimated_units": context.estimated_tokens,
                        "limit": context.budget,
                        "estimate_kind": context.estimate_kind,
                    },
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

