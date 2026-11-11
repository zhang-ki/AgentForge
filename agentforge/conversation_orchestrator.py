"""Application use case for one complete repository conversation turn."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace
import hashlib
from pathlib import Path, PurePosixPath
import re
import sqlite3

from .answer_contract import (
    build_general_contract, build_offline_contract, build_planner_stop_contract, validate_answer_contract,
    verify_generated_answer, verify_hybrid_answer,
)
from .context import MAX_INTERMEDIATE_EVIDENCE_BYTES, build_context
from .conversation_context import ContextBudgetExceeded
from .dialogue_retrieval import _context_candidates, _conversation_retrieval
from .errors import ProviderError, ScanError, StoreError
from .evidence_roles import required_evidence_roles, role_coverage
from .index_lifecycle import IndexLifecycle, IndexLifecycleState, derive_index_lifecycle
from .models import Answer, BuiltContext, EvidenceProvenance
from .agent_loop import ReadOnlyToolbox
from .chunker import split_source_lines
from .providers.offline import OfflineProvider
from .product_retrieval import (
    PRODUCT_ROUTE_VERSION, ProductIntentRouter, ProductRetrievalOutcome,
    ProductRetriever, RepositoryEvidenceUnavailable,
    _content_hash as _retrieval_content_hash,
)
from .dialogue_router import DialogueMode, plan_dialogue
from .evidence_evaluator import assess_evidence
from .planner import LLMPlanner, PlanningError
from .session_service import SessionService
from .streaming import ConversationCancelled, StreamingManager
from .observability import current_lifecycle, lifecycle_stage


@dataclass(frozen=True)
class ConversationRequest:
    query: str
    repository: str
    provider_id: str
    model_id: str | None = None
    base_url: str | None = None
    top_k: int = 5
    context_budget: int | None = None
    max_output_tokens: int | None = None
    output_budget_mode: str = "standard"
    connect_timeout: float = 2.0
    read_timeout: float = 60.0
    total_timeout: float = 180.0
    thinking: bool = False
    effort: str | None = None
    stream: bool = False
    conversation_id: str | None = None
    use_chroma: bool = False
    mode: str = "auto"
    cloud_data_authorized: bool = False
    execution_id: str | None = None
    agent_user_input: str | None = None
    approval_decision: str | None = None

    def __post_init__(self):
        if self.output_budget_mode not in {"concise", "standard", "long", "max"}:
            raise ValueError("Output budget mode must be concise, standard, long or max.")
        if (self.max_output_tokens is not None
                and (isinstance(self.max_output_tokens, bool)
                     or not isinstance(self.max_output_tokens, int)
                     or self.max_output_tokens < 1)):
            raise ValueError("Explicit output token override must be a positive integer.")
        if self.approval_decision not in {None, "approve", "deny"}:
            raise ValueError("Agent approval decision must be approve or deny.")
        if self.approval_decision is not None and self.execution_id is None:
            raise ValueError("An Agent approval decision requires a persisted execution ID.")


@dataclass
class ConversationSession:
    """Ephemeral state loaded for this turn; durable state stays in its store."""

    provider: object | None = None
    store: object | None = None
    repository_id: str | None = None
    rows: tuple[dict, ...] = ()
    conversation: dict | None = None
    memory_retrieved_count: int = 0
    embedding_readiness_seconds: float | None = None
    phase_diagnostics: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ConversationDependencies:
    provider_factory: object
    model_budget_resolver: object
    retriever_factory: object = ProductRetriever
    candidate_selector: object = _context_candidates
    retrieval_refiner: object = _conversation_retrieval
    summary_callback_factory: object | None = None
    planner: object | None = None
    session_service: object | None = None
    agent_executor: object | None = None
    repository_preparer: object | None = None


@dataclass(frozen=True)
class ConversationResult:
    answer: Answer
    contract: object
    provider_mode: str
    retrieval: dict
    context_budget: dict
    evidence_role_coverage: dict
    conversation_context: dict | None
    summary_diagnostics: tuple[dict, ...]
    persistent_chroma: dict
    provider_request_stats: dict | None
    provider_instance: object | None = field(compare=False, repr=False)
    execution_status: str = "completed"
    agent_execution: dict | None = None

    def to_payload(self) -> dict:
        completed = self.execution_status == "completed"
        return {
            "provider": self.provider_mode,
            "execution_status": self.execution_status,
            "answer": (self.answer.text if completed and self.contract is not None
                       and self.contract.status != "refused" else None),
            "refused_output": (self.answer.text if completed and self.contract is not None
                               and self.contract.status == "refused" else None),
            "answer_contract": asdict(self.contract) if self.contract is not None else None,
            "agent_execution": self.agent_execution,
            "retrieval": dict(self.retrieval),
            "context_budget": dict(self.context_budget),
            "evidence_role_coverage": dict(self.evidence_role_coverage),
            "conversation_context": self.conversation_context,
            "summary_diagnostics": list(self.summary_diagnostics),
            "persistent_chroma": dict(self.persistent_chroma),
            "provider_request_stats": self.provider_request_stats,
        }


class ConversationOrchestrator:
    """Coordinate retrieval, context, generation, validation, and turn commit."""

    def __init__(self, dependencies: ConversationDependencies):
        if not callable(dependencies.provider_factory) or not callable(dependencies.model_budget_resolver):
            raise ValueError("Conversation orchestration requires provider and budget services.")
        self.dependencies = dependencies
        self.planner = dependencies.planner or LLMPlanner()
        self.session_service = dependencies.session_service or SessionService()

    def plan(self, request: ConversationRequest, *,
             session: ConversationSession | None = None, index_store=None):
        """Resolve a user mode or obtain a validated plan from the bound model."""
        state = session or ConversationSession()
        if request.execution_id is not None:
            if (not isinstance(request.execution_id, str) or not request.execution_id.strip()
                    or not isinstance(request.conversation_id, str) or not request.conversation_id.strip()):
                raise PlanningError("Resuming an Agent execution requires its ID and persisted conversation.")
            from .planner import ConversationTask
            from .dialogue_router import DialoguePlan
            return DialoguePlan(
                mode=DialogueMode.REPOSITORY, use_repository=True,
                use_general_knowledge=False, reason="Continue the explicitly selected persisted Agent execution.",
                tasks=(ConversationTask("resume_execution", "repository", request.query, True,
                                        (request.query,), ()),),
                requires_agent_execution=True, answer_strategy="evidence_based",
                planner_source="execution_resume",
            )
        requested = DialogueMode(request.mode)
        if requested not in {DialogueMode.AUTO, DialogueMode.HYBRID}:
            tracker = current_lifecycle()
            if tracker is not None:
                tracker.skip("planner", "explicit_mode_compatibility")
            return plan_dialogue(requested, request.query)
        provider = state.provider or self._create_provider(request)
        state.provider = provider
        output_budget_for = getattr(self.planner, "output_tokens_for", None)
        planner_output_tokens = (output_budget_for(provider) if callable(output_budget_for)
                                 else getattr(self.planner, "OUTPUT_TOKENS", LLMPlanner.OUTPUT_TOKENS))
        budget = self.dependencies.model_budget_resolver(
            provider, planner_output_tokens, request.context_budget,
            provider_id=request.provider_id, model_id=request.model_id,
        )
        history = []
        for row in tuple(state.rows or ()):
            if (isinstance(row, dict) and row.get("role") in {"user", "assistant"}
                    and row.get("status", "completed") == "completed"
                    and isinstance(row.get("content"), str)):
                history.append({"role": row["role"], "content": row["content"],
                                "status": row.get("status", "completed")})
        summary = state.conversation.get("summary") if isinstance(state.conversation, dict) else None
        execution_available = _agent_execution_available(
            self.dependencies.agent_executor, request,
        )
        planner_options = {
            "requested_mode": request.mode, "history": history,
            "summary": summary, "input_budget": budget["input_budget"],
            "agent_execution_available": execution_available,
        }
        if isinstance(self.planner, LLMPlanner):
            conversation = state.conversation if isinstance(state.conversation, dict) else {}
            tool_catalog = ()
            executor = self.dependencies.agent_executor
            catalog_loader = getattr(type(executor), "planner_tool_catalog_for", None)
            if execution_available and callable(catalog_loader):
                tool_catalog = catalog_loader(
                    executor,
                    request, query=request.query, repository_id=state.repository_id,
                    index_store=index_store,
                )
                execution_available = bool(tool_catalog)
            elif execution_available:
                # Do not tell the LLM it can execute tools unless the exact
                # request-scoped authorized directory is available to it.
                execution_available = False
            planner_options.update({
                "memory_context": tuple(conversation.get("memories", ())),
                "repository_id": conversation.get("repository_id"),
                "memory_status": conversation.get("memory_retrieval"),
                "agent_execution_available": execution_available,
                "available_tools": tool_catalog,
            })
        with lifecycle_stage("planner", metadata={
            "provider": request.provider_id, "model": request.model_id,
        }) as planner_stage:
            try:
                result = self.planner.plan(provider, request.query, **planner_options)
            except PlanningError as exc:
                if planner_stage is not None:
                    diagnostics = getattr(self.planner, "last_request_diagnostics", None)
                    planner_stage.finish("FAILED", error_code=exc.diagnostic_code or "planning_error",
                                         metadata={
                                             "corrections": ((diagnostics or {}).get("correction_attempt", 0)
                                                             if isinstance(diagnostics, dict) else 0),
                                             "plan_valid": False,
                                         })
                # Preserve only structured, bounded planner diagnostics on the failure.
                # This lets the CLI explain schema rejection without logging query text.
                if isinstance(self.planner, LLMPlanner):
                    details = dict(exc.diagnostic_details or {})
                    details["planner_diagnostics"] = self.planner.last_request_diagnostics or {}
                    exc.diagnostic_details = details
                raise
            diagnostics = getattr(self.planner, "last_request_diagnostics", None)
            if planner_stage is not None:
                planner_stage.finish("COMPLETED", metadata={
                    "corrections": ((diagnostics or {}).get("correction_attempt", 0)
                                    if isinstance(diagnostics, dict) else 0),
                    "plan_valid": True,
                    "requires_repository_retrieval": bool(getattr(result, "requires_repository_retrieval", False)),
                    "requires_tool_execution": bool(getattr(result, "requires_agent_execution", False)),
                    "correction_status": (diagnostics or {}).get("correction_status")
                    if isinstance(diagnostics, dict) else None,
                    "final_validation_status": (diagnostics or {}).get("final_validation_status")
                    if isinstance(diagnostics, dict) else None,
                })
        state.phase_diagnostics["planner"] = self.planner.last_request_diagnostics
        return plan_dialogue(requested, request.query, planner_result=result)

    def run(self, request: ConversationRequest, *, index_store=None, vector_retriever=None,
            session: ConversationSession | None = None, on_content=None, on_event=None,
            cancel_event=None, planned_plan=None) -> ConversationResult:
        tracker = current_lifecycle()
        event_sink = on_event
        if tracker is not None:
            def event_sink(event):
                try:
                    tracker.record_stream_event(event)
                except Exception:
                    pass
                if on_event is None:
                    return
                publish = getattr(on_event, "publish", None)
                publish(event) if callable(publish) else on_event(event)
        streaming = StreamingManager(
            on_event=event_sink, on_content=on_content, enabled=request.stream,
            cancel_event=cancel_event,
        )
        streaming.start()
        try:
            return self._run_turn(
                request, index_store=index_store, vector_retriever=vector_retriever,
                session=session, planned_plan=planned_plan, streaming=streaming,
            )
        except ConversationCancelled:
            streaming.cancel()
            raise
        except Exception as exc:
            streaming.fail(type(exc).__name__)
            raise

    def _run_turn(self, request: ConversationRequest, *, index_store=None, vector_retriever=None,
                  session: ConversationSession | None = None, planned_plan=None,
                  streaming: StreamingManager) -> ConversationResult:
        streaming.check_cancelled()
        state = session or ConversationSession()
        dialogue_plan = planned_plan or self.plan(
            request, session=state, index_store=index_store,
        )
        if not hasattr(dialogue_plan, "mode"):
            raise PlanningError("Conversation plan has an invalid type.")
        planner_diagnostics = getattr(self.planner, "last_request_diagnostics", None)
        if isinstance(planner_diagnostics, dict):
            planner_diagnostics["execution_started"] = False
        streaming.plan_ready(dialogue_plan)
        streaming.check_cancelled()
        if dialogue_plan.clarification_required:
            return self._planner_stop(request, state, dialogue_plan,
                "The request needs clarification before it can be safely planned.", streaming)
        if (dialogue_plan.requires_external_data and not dialogue_plan.use_repository
                and not dialogue_plan.use_general_knowledge):
            return self._planner_stop(request, state, dialogue_plan,
                "This request needs live external data, but AgentForge has no authorized external-data tool.",
                streaming)
        scan_report = None
        retrieval_failure_code = None
        repository_bound = (
            isinstance(request.repository, str)
            and bool(request.repository.strip())
            and request.repository.strip().casefold() != "stateless"
        )
        if repository_bound and index_store is None:
            retrieval_failure_code = "repository_index_not_selected"
            tracker = current_lifecycle()
            if tracker is not None:
                tracker.skip("repository_preparation", "repository_index_not_selected")
        elif repository_bound:
            preparer = self.dependencies.repository_preparer
            if callable(preparer):
                with lifecycle_stage("repository_preparation", metadata={
                    "provider": request.provider_id, "model": request.model_id,
                }) as preparation_stage:
                    try:
                        prepared = preparer(index_store, request, vector_retriever)
                    except (RepositoryEvidenceUnavailable, StoreError, sqlite3.Error, OSError, ScanError) as exc:
                        retrieval_failure_code = _retrieval_failure_code(exc)
                        if preparation_stage is not None:
                            preparation_stage.finish("DEGRADED", error_code=retrieval_failure_code,
                                                     metadata={"error_type": type(exc).__name__})
                    else:
                        if not isinstance(prepared, dict):
                            raise PlanningError("Repository preparation returned an invalid result.")
                        vector_retriever = prepared.get("vector_retriever", vector_retriever)
                        scan_report = prepared.get("scan_report")
                        if preparation_stage is not None:
                            preparation_stage.finish("COMPLETED", metadata={
                                "index_state": "prepared",
                            })
            else:
                tracker = current_lifecycle()
                if tracker is not None:
                    tracker.skip("repository_preparation", "no_preparer_configured")
        agent_execution = None

        def execute_agent_if_requested():
            nonlocal agent_execution
            if dialogue_plan.requires_agent_execution:
                if isinstance(planner_diagnostics, dict):
                    planner_diagnostics["execution_started"] = True
                agent_execution = self._run_agent_execution(
                    request, state, dialogue_plan, index_store=index_store,
                    streaming=streaming, cancel_event=streaming.cancel_event,
                )
                if isinstance(agent_execution, ConversationResult):
                    return agent_execution
                report = agent_execution.report
                state.phase_diagnostics["agent_execution"] = {
                    "execution_id": report.execution_id,
                    "status": report.status,
                    "state": report.execution_state,
                    "rounds": report.execution_rounds,
                    "tool_call_count": len(report.execution_results),
                    "planner_request_count": len(report.planner_request_diagnostics),
                    "observation_count": len(report.execution_results),
                    "tool_results": [_safe_agent_tool_result(item)
                                     for item in report.execution_results],
                }
                return None
            tracker = current_lifecycle()
            if tracker is not None:
                tracker.skip("agent_execution", "planner_did_not_request_tool_execution")
            return None

        if not repository_bound:
            stopped = execute_agent_if_requested()
            if stopped is not None:
                return stopped
            tracker = current_lifecycle()
            if tracker is not None:
                tracker.skip("repository_retrieval", "no_repository_bound_to_session")
            return self._run_general(request, state, dialogue_plan, streaming=streaming,
                                     agent_execution=agent_execution, index_store=index_store)
        # A bound repository is optional context for every turn. Planner task
        # shape controls tool execution only; its route/search-task prediction
        # cannot suppress this independent lookup or make lookup failure fatal.
        retrieval_queries = [request.query]

        conversation = state.conversation
        outcomes = []
        selections = []
        followup_symbols = []
        retriever = None
        with lifecycle_stage("repository_retrieval", metadata={
            "retrieval_query_count": len(retrieval_queries),
        }) as repository_stage:
            if retrieval_failure_code is not None:
                if repository_stage is not None:
                    repository_stage.finish("DEGRADED", error_code=retrieval_failure_code,
                                            metadata={"evidence_count": 0})
            else:
                try:
                    retriever_options = {"vector_retriever": vector_retriever}
                    if scan_report is not None:
                        retriever_options["scan_report"] = scan_report
                    retriever = self.dependencies.retriever_factory(
                        index_store, request.repository, **retriever_options,
                    )
                    for position, query in enumerate(retrieval_queries, 1):
                        streaming.check_cancelled()
                        streaming.retrieval_progress(position - 1, len(retrieval_queries))
                        with lifecycle_stage("repository_search_query", metadata={
                            "operation": "search", "retrieval_query_count": position,
                        }):
                            outcome_item = retriever.search(query, top_k=request.top_k)

                        class Arguments:
                            pass
                        arguments = Arguments()
                        arguments.query = query
                        arguments.top_k = request.top_k
                        with lifecycle_stage("retrieval_refinement", metadata={
                            "operation": "rerank_and_select",
                        }):
                            outcome_item, followup_symbol = self.dependencies.retrieval_refiner(
                                outcome_item, retriever, conversation, arguments, index_store,
                                state.rows if conversation is not None else (),
                            )
                            outcomes.append(outcome_item)
                            selections.extend(self.dependencies.candidate_selector(
                                outcome_item, conversation, store=index_store, retriever=retriever,
                            ))
                        if followup_symbol:
                            followup_symbols.append(followup_symbol)
                    streaming.retrieval_progress(len(retrieval_queries), len(retrieval_queries))
                    streaming.check_cancelled()
                    if repository_stage is not None:
                        repository_stage.finish("COMPLETED", metadata={"candidate_count": len(selections)})
                except (RepositoryEvidenceUnavailable, StoreError, sqlite3.Error, OSError, ScanError) as exc:
                    retrieval_failure_code = _retrieval_failure_code(exc)
                    outcomes.clear()
                    selections.clear()
                    followup_symbols.clear()
                    if repository_stage is not None:
                        repository_stage.finish("DEGRADED", error_code=retrieval_failure_code,
                                                metadata={"candidate_count": 0})

        # Repository lookup is a turn-level context step. It must complete (or
        # explicitly degrade) before an independently planned tool execution,
        # so a missing tool authorization cannot suppress the bound-repository
        # retrieval path for this turn.
        stopped = execute_agent_if_requested()
        if stopped is not None:
            return stopped

        verified_tool_reads = ()
        tool_evidence_by_hash = {}
        verified_tool_candidates_by_citation = {}
        agent_read_source_diagnostics = []
        agent_read_relevance_diagnostics = []
        agent_read_search_route_confidences = []
        user_query_route_confidence = None
        if agent_execution is not None:
            # Search results and tool output are untrusted observations until a
            # completed read exactly matches the indexed SQLite source chunk.
            verified_tool_reads = _verified_agent_read_sources(
                agent_execution, index_store, state.repository_id, request.repository,
                diagnostics=agent_read_source_diagnostics,
            )
            # The planner's search phrase is a retrieval hint, not the user's
            # question. A broad but valid search phrase may route with low
            # confidence; it must not downgrade an exact SQLite-verified read
            # that is relevant to the current user request.
            user_query_route = ProductIntentRouter().route(request.query)
            user_query_route_confidence = user_query_route.confidence
            for candidate, provenance, search_query in verified_tool_reads:
                search_route = ProductIntentRouter().route(search_query)
                agent_read_search_route_confidences.append(search_route.confidence)
                assessment = assess_evidence(request.query, user_query_route, (candidate,))
                if candidate in assessment.relevant_candidates:
                    tool_evidence_by_hash[candidate.content_hash] = provenance
                    # A RAG candidate and an exact, SQLite-verified Agent read
                    # can describe the same path/range with distinct hash
                    # scopes. AnswerContract indexes evidence by citation, so
                    # retain the verified read as the canonical source for that
                    # citation instead of presenting an ambiguous duplicate.
                    verified_tool_candidates_by_citation[candidate.citation] = candidate
                elif assessment.candidate_results:
                    agent_read_relevance_diagnostics.append({
                        "stage": "user_query_relevance",
                        "reason_code": assessment.candidate_results[0]["reason_code"],
                    })

        with lifecycle_stage("retrieval_merge", metadata={
            "candidate_count": len(selections), "retrieval_query_count": len(outcomes),
        }) as merge_stage:
            outcome = (_merge_retrieval_outcomes(request.query, outcomes) if outcomes else
                       _unavailable_retrieval_outcome(
                           request.query, state.repository_id, retrieval_failure_code or "repository_unavailable",
                           sqlite_unavailable=(retrieval_failure_code == "sqlite_index_unavailable"),
                       ))
            candidates = _merge_candidates(selections)
            if merge_stage is not None:
                merge_stage.finish("COMPLETED", metadata={"candidate_count": len(candidates)})
        followup_symbol = followup_symbols[0] if followup_symbols else None
        # Pronoun follow-ups may omit the symbol in the latest question. The
        # resolver supplies only a unique symbol grounded in prior session
        # turns and current index; do not use arbitrary planner search text as
        # relevance proof for its own retrieval results.
        evidence_query = (request.query + " " + followup_symbol) if followup_symbol else request.query
        with lifecycle_stage("evidence_validation", metadata={"candidate_count": len(candidates)}):
            evidence_assessment = assess_evidence(evidence_query, outcome.decision, candidates)
        repository_candidates = tuple(
            candidate for candidate in evidence_assessment.relevant_candidates
            if candidate.citation not in verified_tool_candidates_by_citation
        )
        selected = _merge_candidates((
            *repository_candidates, *verified_tool_candidates_by_citation.values(),
        ))
        results = [replace(item.as_search_result(),
                           provenance=tool_evidence_by_hash.get(item.content_hash))
                   for item in selected]
        model_budget = self.dependencies.model_budget_resolver(
            state.provider, _resolved_answer_output(state.provider, request), request.context_budget,
            provider_id=request.provider_id, model_id=request.model_id,
        )
        required_roles = required_evidence_roles(request.query)
        with lifecycle_stage("context_build", metadata={"evidence_count": len(results)}):
            context = build_context(
                request.query, results,
                max_candidates=max(request.top_k, len(required_roles)),
                budget=(model_budget["input_budget"] if isinstance(state.provider, OfflineProvider)
                        else MAX_INTERMEDIATE_EVIDENCE_BYTES),
            )
        if dialogue_plan.planner_source == "llm":
            context = replace(context, planned_tasks=tuple({
                "task_id": task.task_id, "task_type": task.task_type,
                "question": task.question, "dependencies": task.dependencies,
            } for task in dialogue_plan.tasks))
        if agent_execution is not None:
            evidence_source_ids_by_call_id = {
                source.result.provenance.tool_call_id: source.source_id
                for source in context.evidence
                if isinstance(source.result.provenance, EvidenceProvenance)
            }
            context = replace(context, tool_observations=_tool_observation_context(
                agent_execution.report, evidence_source_ids_by_call_id,
            ))
        if results and not context.evidence:
            required_bytes = sum(len(item.text.encode("utf-8")) for item in results)
            raise ContextBudgetExceeded({
                "failure_code": "intermediate_evidence_byte_limit_exceeded",
                "required_bytes": required_bytes,
                "budget_bytes": MAX_INTERMEDIATE_EVIDENCE_BYTES,
                "budget_unit": "bytes",
                "components_bytes": {"question": len(request.query.encode("utf-8")),
                                     "evidence": required_bytes, "system": 0, "framing": 0,
                                     "intermediate_limit": MAX_INTERMEDIATE_EVIDENCE_BYTES},
                "model_input_budget_tokens": model_budget["input_budget"],
                "content_saved": False,
                "estimate_kind": "UTF-8 bytes for the intermediate evidence bound",
            })
        candidate_map = {item.citation: item for item in selected}
        uncertainty = outcome.uncertainty or (
            "The static configuration import is not proven as a runtime binding."
            if any(item.resolution_status in {"uncertain", "unresolved", "ambiguous"}
                   for item in selected) else None
        )
        if not context.evidence and not uncertainty:
            uncertainty = (
                "No verified current repository evidence is available; repository-specific facts "
                "cannot be confirmed for this turn."
            )
        available_roles = (role for source in context.evidence
                           for role in source.result.evidence_roles)
        initial_role_audit = role_coverage(request.query, available_roles)
        role_gate_failure = bool(initial_role_audit["missing_available_roles"])
        if role_gate_failure:
            uncertainty = (
                "The available evidence does not cover required source roles: "
                + ", ".join(initial_role_audit["missing_available_roles"])
                + ". Repository-specific conclusions must remain limited to what the verified excerpts support."
            )
        use_optional_evidence_answer = bool(
            dialogue_plan.mode is DialogueMode.HYBRID
            or not context.evidence
            or role_gate_failure
            or (outcome.decision.confidence == "low" and uncertainty)
            or dialogue_plan.mode is DialogueMode.GENERAL
        )

        provider = state.provider
        if provider is None:
            provider = self._create_provider(request)
            state.provider = provider

        if state.store is not None:
            if conversation is None:
                conversation = {"repository_id": state.repository_id}
                state.conversation = conversation
            self._prepare_history(request, state, provider)
            conversation = state.conversation
        elif not isinstance(conversation, dict):
            # Request-local diagnostics are retained even for stateless asks;
            # this mapping is never written to PostgreSQL without a store.
            conversation = {"repository_id": state.repository_id}
            state.conversation = conversation
        if isinstance(conversation, dict):
            conversation["input_token_budget_override"] = model_budget["input_budget"]

        with lifecycle_stage("final_answer", metadata={
            "provider": request.provider_id, "model": request.model_id,
        }) as answer_stage:
            try:
                if isinstance(provider, OfflineProvider):
                    answer = provider.answer(context)
                else:
                    # Retrieval is independent from answer shape. When a bound
                    # repository produced no verified evidence, use the
                    # provider's general-answer contract so an empty
                    # Repository section cannot reject an otherwise useful
                    # general response. The general prompt still forbids
                    # unverified claims about this repository.
                    no_repository_evidence = not context.evidence
                    answer_method = (
                        getattr(provider, "answer_general", None)
                        if no_repository_evidence else
                        getattr(provider, "answer_hybrid", None)
                        if use_optional_evidence_answer else
                        getattr(provider, "answer", None)
                    )
                    if not callable(answer_method):
                        raise ProviderError(
                            "The selected provider cannot safely answer with optional repository evidence."
                        )
                    answer_kwargs = {"conversation": conversation, "settings": conversation}
                    if use_optional_evidence_answer or no_repository_evidence:
                        answer_kwargs["external_data_unavailable"] = dialogue_plan.requires_external_data
                    if no_repository_evidence:
                        answer_kwargs["repository_evidence_unavailable"] = bool(repository_bound)
                    if request.stream:
                        answer_kwargs["on_content"] = streaming.capture_provider_delta
                    streaming.check_cancelled()
                    answer = answer_method(context, **answer_kwargs)
            except Exception as exc:
                if answer_stage is not None:
                    answer_stage.finish("TIMEOUT" if isinstance(exc, TimeoutError) else "FAILED",
                                        error_code=getattr(exc, "code", type(exc).__name__))
                raise
            if answer_stage is not None:
                answer_stage.finish("COMPLETED", metadata={"answer_produced": bool(getattr(answer, "text", ""))})
        with lifecycle_stage("answer_contract", metadata={"evidence_count": len(context.evidence)}) as contract_stage:
            try:
                if answer.offline:
                    contract = build_offline_contract(
                        answer, context, route=outcome.decision.route,
                        route_confidence=outcome.decision.confidence,
                        candidate_by_citation=candidate_map, uncertainty=uncertainty,
                    )
                    provider_mode = "offline" if request.provider_id == "offline" else "offline_no_evidence"
                else:
                    try:
                        if not context.evidence:
                            contract = build_general_contract(answer)
                        elif use_optional_evidence_answer:
                            contract = verify_hybrid_answer(
                                answer, context, candidate_by_citation=candidate_map,
                                route_confidence=outcome.decision.confidence,
                            )
                        else:
                            contract = verify_generated_answer(
                                answer, context, route=outcome.decision.route,
                                route_confidence=outcome.decision.confidence,
                                candidate_by_citation=candidate_map, uncertainty=uncertainty,
                            )
                    except ProviderError as exc:
                        recorder = getattr(getattr(provider, "client", None), "record_application_failure", None)
                        if callable(recorder):
                            recorder("answer_contract", getattr(exc, "code", "answer_contract_failed"),
                                     getattr(exc, "diagnostic", None))
                        raise
                    provider_mode = getattr(provider, "provider_id", "unknown")
                validate_answer_contract(contract, context)
            except Exception as exc:
                if contract_stage is not None:
                    contract_stage.finish("FAILED", error_code=getattr(exc, "code", type(exc).__name__),
                                          metadata={"answer_contract_status": "rejected",
                                                    "answer_produced": bool(getattr(answer, "text", ""))})
                raise
            if contract_stage is not None:
                contract_stage.finish("COMPLETED", metadata={
                    "answer_contract_status": getattr(contract, "status", "unknown"),
                    "answer_produced": bool(getattr(answer, "text", "")),
                })
        request_stats = (provider.request_stats()
                         if callable(getattr(provider, "request_stats", None)) else None)
        request_diagnostics = _phase_diagnostics(state, conversation, request_stats)
        if state.store is not None:
            with lifecycle_stage("session_persistence"):
                if agent_execution is not None:
                    agent_report = agent_execution.report
                    phases = _phase_diagnostics(state, conversation, request_stats)
                    phases["agent_execution"] = state.phase_diagnostics["agent_execution"]
                    phases.update({f"agent_planner_{position}": diagnostic
                        for position, diagnostic in enumerate(agent_report.planner_request_diagnostics, 1)
                        if isinstance(diagnostic, dict)})
                    self.session_service.commit_execution_turn(
                        state.store, request.conversation_id, state.repository_id,
                        agent_report.execution_id, request.query, answer, provider,
                        request_diagnostics=phases,
                    )
                else:
                    self.session_service.commit_completed_turn(
                        state.store, request.conversation_id, state.repository_id,
                        request.query, answer, provider, request_diagnostics=request_diagnostics,
                    )
        streaming.complete_validated(
            answer.text, usage=request_stats.get("last_usage") if isinstance(request_stats, dict) else None,
        )
        injected_memories = _injected_memory_metadata(conversation)
        cited = tuple(citation for claim in contract.claims for citation in claim.citations)
        cited_roles = {role for item in contract.evidence if item.citation in cited
                       for role in item.evidence_roles}
        final_role_audit = role_coverage(
            request.query,
            (role for item in contract.evidence for role in item.evidence_roles),
            cited_roles,
        )
        answer_budget_diagnostics = (conversation.get("token_budget_diagnostics")
                                     if isinstance(conversation, dict) else None)
        if isinstance(answer_budget_diagnostics, dict):
            context_usage = answer_budget_diagnostics.get("estimated_input_tokens")
            context_limit = answer_budget_diagnostics.get("input_budget_tokens")
            context_estimate_kind = answer_budget_diagnostics.get("tokenizer_method", "estimated")
        else:
            context_usage = context.estimated_tokens
            context_limit = context.budget
            context_estimate_kind = "UTF-8 bytes; no model request was assembled"
        retrieval_payload = {
             "mode": dialogue_plan.mode.value,
             "route": outcome.decision.route, "rule": outcome.decision.rule,
             "reason": outcome.decision.reason, "confidence": outcome.decision.confidence,
             "planner_source": dialogue_plan.planner_source,
             "planner_confidence": dialogue_plan.confidence,
             "answer_strategy": dialogue_plan.answer_strategy,
             "requires_external_data": dialogue_plan.requires_external_data,
             "requires_agent_execution": dialogue_plan.requires_agent_execution,
             "tasks": [_task_payload(task) for task in dialogue_plan.tasks],
             "retrieval_status": (
                 "unavailable" if retrieval_failure_code else
                 "degraded" if outcome.unavailable_channels else
                 "evidence_available" if context.evidence else "no_evidence"
             ),
             "candidate_count": evidence_assessment.candidate_count,
             "evidence_count": len(context.evidence),
             "verified_tool_read_evidence_count": len(tool_evidence_by_hash),
             "retrieval_failure_code": retrieval_failure_code,
             "evidence_assessment": {
                 "candidate_count": evidence_assessment.candidate_count,
                 "relevant_candidate_count": len(evidence_assessment.relevant_candidates),
                 "relevance": evidence_assessment.relevance,
                 "sufficiency": evidence_assessment.sufficiency,
                 "required_roles": list(evidence_assessment.required_roles),
                 "available_roles": list(evidence_assessment.available_roles),
                 "missing_roles": list(evidence_assessment.missing_roles),
                 "reason": evidence_assessment.reason,
                 "candidate_decisions": [
                     {"citation": item["candidate"].citation,
                      "status": item["status"],
                      "reason_code": item["reason_code"]}
                     for item in evidence_assessment.candidate_results
                 ],
                 "semantic_entailment_verified": False,
             },
             "agent_read_validation": {
                 "verified_read_count": len(verified_tool_reads),
                 "accepted_read_count": len(tool_evidence_by_hash),
                 "source_rejection_reason_codes": [
                     item["reason_code"] for item in agent_read_source_diagnostics
                 ],
                 "relevance_rejection_reason_codes": [
                     item["reason_code"] for item in agent_read_relevance_diagnostics
                 ],
                 "user_query_route_confidence": user_query_route_confidence,
                 "search_route_confidences": list(agent_read_search_route_confidences),
             },
             "uncertainty": uncertainty, "unavailable_channels": list(outcome.unavailable_channels),
             "index_lifecycle": outcome.index_lifecycle,
             "conversation_lookup_symbol": followup_symbol}
        if agent_execution is not None:
            retrieval_payload["agent_execution"] = dict(state.phase_diagnostics["agent_execution"])
        return ConversationResult(
            answer, contract, provider_mode,
            retrieval_payload,
            {"estimated_units": context_usage, "limit": context_limit,
             "estimate_kind": context_estimate_kind,
             "budget_unit": "tokens" if isinstance(answer_budget_diagnostics, dict) else "bytes",
             "intermediate_evidence_byte_limit": MAX_INTERMEDIATE_EVIDENCE_BYTES,
             "model_context_window": model_budget["context_window"],
             "reserved_output_tokens": model_budget["output_reserve"],
             "safety_margin_tokens": model_budget["safety_margin"],
             "token_estimate_kind": model_budget["estimate_kind"],
             "token_estimate_description": model_budget.get("estimate_description"),
             "model_max_input_tokens": model_budget.get("max_input_tokens"),
             "model_max_output_tokens": model_budget.get("max_output_tokens"),
             "reasoning_token_limit": model_budget.get("reasoning_token_limit"),
             "capability_source": model_budget.get("capability_source"),
             "capability_verified_at": model_budget.get("capability_verified_at"),
             "request_phases": _phase_diagnostics(state, conversation, request_stats)},
            final_role_audit,
            ({**conversation.get("context_diagnostics", {}),
              "token_estimate": conversation.get("token_budget_diagnostics")}
             if conversation else None),
            _summary_diagnostics(state.store),
            {"enabled": request.use_chroma,
             "memory_retrieved_count": state.memory_retrieved_count,
             "memory_context_count": len(injected_memories),
             "memory_context": injected_memories,
             "embedding_model_ready_seconds": state.embedding_readiness_seconds},
            request_stats, provider,
            agent_execution={"execution_id": agent_execution.report.execution_id,
                             "status": agent_execution.report.status,
                             "rounds": agent_execution.report.execution_rounds,
                             "tool_call_count": len(agent_execution.report.execution_results)}
            if agent_execution is not None else None,
        )

    def _run_agent_execution(self, request, state, dialogue_plan, *, index_store,
                             streaming, cancel_event=None):
        executor = self.dependencies.agent_executor
        execute = getattr(executor, "execute", None)
        if not callable(execute):
            raise PlanningError(
                "The selected plan requires tool execution, but no Agent execution service is configured."
            )
        if not _agent_execution_available(executor, request):
            raise PlanningError(
                "The selected plan requests tools, but no authorized tool registry is available for this intent."
            )
        provider = state.provider or self._create_provider(request)
        state.provider = provider
        conversation = state.conversation
        if state.store is not None and isinstance(conversation, dict):
            budget = self.dependencies.model_budget_resolver(
                provider, _resolved_answer_output(provider, request), request.context_budget,
                provider_id=request.provider_id, model_id=request.model_id,
            )
            conversation["input_token_budget_override"] = budget["input_budget"]
            self._prepare_history(request, state, provider)
            conversation = state.conversation
        with lifecycle_stage("agent_execution", metadata={
            "provider": request.provider_id, "model": request.model_id,
        }) as execution_stage:
            execution = executor.execute(
                request, session=state, index_store=index_store,
                dialogue_plan=dialogue_plan, streaming=streaming,
                cancel_event=cancel_event,
            )
            report = execution.report
            execution_status = getattr(report, "status", "failed")
            stage_status = {
                "completed": "COMPLETED", "waiting_approval": "PENDING",
                "waiting_user_input": "PENDING", "cancelled": "CANCELLED",
            }.get(execution_status, "FAILED")
            if execution_stage is not None:
                execution_stage.finish(stage_status,
                                       error_code=(getattr(report, "error_code", None)
                                                   if stage_status == "FAILED" else None),
                                       metadata={"execution_status": execution_status,
                                                 "tool_call_count": len(getattr(report, "execution_results", ())),
                                                 "observation_count": len(getattr(report, "execution_results", ()))})
        provider = execution.provider
        state.provider = provider
        stats = (provider.request_stats()
                 if callable(getattr(provider, "request_stats", None)) else None)
        planner_diagnostics = tuple(
            item for item in getattr(report, "planner_request_diagnostics", ())
            if isinstance(item, dict)
        )
        failure_code, failure_phase = _agent_failure_metadata(report, planner_diagnostics)
        execution_payload = {
            "execution_id": report.execution_id,
            "status": report.status,
            "state": report.execution_state,
            "rounds": report.execution_rounds,
            "tool_call_count": len(report.execution_results),
            "results": [_safe_agent_tool_result(item)
                        for item in report.execution_results],
            "pending_approval": _pending_approval_identity(report.pending_approval),
            "user_input_required": bool(report.user_input_request),
            "planner_request_count": len(planner_diagnostics),
        }
        if isinstance(failure_code, str) and failure_code:
            execution_payload["error_code"] = failure_code
        if isinstance(failure_phase, str) and failure_phase:
            execution_payload["failure_phase"] = failure_phase
        if report.status == "completed":
            return execution
        answer = Answer("", (), True)
        contract = None
        if report.status in {"waiting_approval", "waiting_user_input"}:
            streaming.suspend(report.status)
        elif report.status == "cancelled":
            streaming.cancel()
        else:
            # The engine state is often FAILED/COMPLETED at the control-plane level;
            # the report status carries the user-meaningful stop/failure reason.
            streaming.fail(report.status or report.execution_state or "agent_execution_failed")

        evidence = tuple(getattr(contract, "evidence", ())) if contract is not None else ()
        cited = {citation for claim in getattr(contract, "claims", ())
                 for citation in getattr(claim, "citations", ())}
        cited_roles = {role for item in evidence if item.citation in cited
                       for role in item.evidence_roles}
        available_roles = {role for item in evidence for role in item.evidence_roles}
        coverage = role_coverage(request.query, available_roles, cited_roles)
        diagnostics = (planner_diagnostics[-1] if planner_diagnostics else {})
        context_budget = {
            "request_phases": list(planner_diagnostics),
            "estimated_units": diagnostics.get("estimated_input_tokens", 0),
            "limit": diagnostics.get("input_budget_tokens", 0),
            "budget_unit": "tokens",
            "estimate_kind": diagnostics.get("tokenizer_method", "model-aware estimate"),
        }
        memory_context = _injected_memory_metadata(conversation)
        retrieval = {
            "mode": dialogue_plan.mode.value,
            "route": "agent_execution",
            "planner_source": dialogue_plan.planner_source,
            "planner_confidence": dialogue_plan.confidence,
            "answer_strategy": dialogue_plan.answer_strategy,
            "requires_agent_execution": True,
            "tasks": [_task_payload(task) for task in dialogue_plan.tasks],
            "tool_execution": execution_payload,
            "retrieval": report.retrieval,
        }
        return ConversationResult(
            answer, contract, getattr(provider, "provider_id", "unknown"),
            retrieval, context_budget, coverage,
            ({**conversation.get("context_diagnostics", {}),
              "token_estimate": conversation.get("token_budget_diagnostics")}
             if isinstance(conversation, dict) else None),
            _summary_diagnostics(state.store),
            {"enabled": request.use_chroma,
             "memory_retrieved_count": state.memory_retrieved_count,
             "memory_context_count": len(memory_context),
             "memory_context": memory_context,
             "embedding_model_ready_seconds": state.embedding_readiness_seconds},
            stats, provider, execution_status=report.status,
            agent_execution=execution_payload,
        )

    def _planner_stop(self, request, state, dialogue_plan, reason, streaming):
        text = ("需要补充问题信息后才能继续。" if dialogue_plan.clarification_required else
                "当前问题需要实时外部信息，但 AgentForge 尚未配置经授权的外部数据工具，无法代查。")
        answer = Answer(text, (), True)
        with lifecycle_stage("answer_contract", metadata={"answer_produced": True}) as contract_stage:
            contract = build_planner_stop_contract(
                answer, route=dialogue_plan.mode.value, reason=reason,
                origin="general" if dialogue_plan.clarification_required else "external",
            )
            if contract_stage is not None:
                contract_stage.finish("COMPLETED", metadata={
                    "answer_contract_status": getattr(contract, "status", "unknown"),
                    "answer_produced": True,
                })
        stats = (state.provider.request_stats()
                 if callable(getattr(state.provider, "request_stats", None)) else None)
        request_diagnostics = _phase_diagnostics(state, state.conversation, stats)
        if state.store is not None:
            with lifecycle_stage("session_persistence"):
                self.session_service.commit_completed_turn(
                    state.store, request.conversation_id, state.repository_id,
                    request.query, answer, state.provider, request_diagnostics=request_diagnostics,
                )
        streaming.complete_validated(
            answer.text, usage=stats.get("last_usage") if isinstance(stats, dict) else None,
        )
        return ConversationResult(
            answer, contract, "planner_refusal",
            {"mode": dialogue_plan.mode.value, "route": "planner_stop",
             "rule": "llm_plan_capability_gate", "reason": reason,
             "confidence": dialogue_plan.confidence,
             "requires_external_data": dialogue_plan.requires_external_data,
             "tasks": [_task_payload(task) for task in dialogue_plan.tasks],
             "unavailable_channels": [], "uncertainty": reason},
            {"estimated_units": 0, "limit": 0,
             "estimate_kind": "no answer model request was made"},
            role_coverage("", (), ()), None, (),
            {"enabled": request.use_chroma, "memory_retrieved_count": state.memory_retrieved_count,
             "memory_context_count": 0, "memory_context": [],
             "embedding_model_ready_seconds": state.embedding_readiness_seconds},
            stats, state.provider,
        )

    def _create_provider(self, request):
        return self.dependencies.provider_factory(
            request.provider_id, request.base_url, request.model_id,
            connect_timeout=request.connect_timeout, read_timeout=request.read_timeout,
            total_timeout=request.total_timeout, max_output_tokens=request.max_output_tokens,
            output_budget_mode=request.output_budget_mode,
            thinking=request.thinking, effort=request.effort, stream=request.stream,
        )

    def _run_general(self, request, state, dialogue_plan, *, streaming, index_store=None,
                     agent_execution=None):
        provider = state.provider or self._create_provider(request)
        state.provider = provider
        model_budget = self.dependencies.model_budget_resolver(
            provider, _resolved_answer_output(provider, request), request.context_budget,
            provider_id=request.provider_id, model_id=request.model_id,
        )
        conversation = state.conversation
        if state.store is not None and conversation is not None:
            self._prepare_history(request, state, provider)
            conversation = state.conversation
        elif not isinstance(conversation, dict) and not isinstance(provider, OfflineProvider):
            conversation = {"repository_id": state.repository_id}
            state.conversation = conversation
        if isinstance(conversation, dict):
            conversation["input_token_budget_override"] = model_budget["input_budget"]
        context = BuiltContext(
            request.query, (), "", 0, model_budget["input_budget"],
            ("UTF-8 bytes; offline provider" if isinstance(provider, OfflineProvider)
             else "model-aware token budget; prompt usage recorded after assembly"),
            tuple({"task_id": task.task_id, "task_type": task.task_type,
                   "question": task.question, "dependencies": task.dependencies}
                  for task in dialogue_plan.tasks) if dialogue_plan.planner_source == "llm" else (),
            _tool_observation_context(agent_execution.report) if agent_execution is not None else (),
        )
        answer_method = getattr(provider, "answer_general", None)
        with lifecycle_stage("final_answer", metadata={
            "provider": request.provider_id, "model": request.model_id,
        }) as answer_stage:
            try:
                if isinstance(provider, OfflineProvider) or not callable(answer_method):
                    answer = OfflineProvider().answer(context)
                else:
                    kwargs = {"conversation": conversation, "settings": conversation,
                              "external_data_unavailable": dialogue_plan.requires_external_data}
                    if request.stream:
                        kwargs["on_content"] = streaming.capture_provider_delta
                    streaming.check_cancelled()
                    answer = answer_method(context, **kwargs)
            except Exception as exc:
                if answer_stage is not None:
                    status = "CANCELLED" if isinstance(exc, ConversationCancelled) else (
                        "TIMEOUT" if isinstance(exc, TimeoutError) else "FAILED")
                    answer_stage.finish(status, error_code=getattr(exc, "code", type(exc).__name__))
                raise
            if answer_stage is not None:
                answer_stage.finish("COMPLETED", metadata={"answer_produced": bool(getattr(answer, "text", ""))})
        with lifecycle_stage("answer_contract", metadata={"evidence_count": 0}) as contract_stage:
            try:
                contract = build_general_contract(answer)
                validate_answer_contract(contract, context)
            except ProviderError as exc:
                recorder = getattr(getattr(provider, "client", None), "record_application_failure", None)
                if callable(recorder):
                    recorder("answer_contract", getattr(exc, "code", "answer_contract_failed"),
                             getattr(exc, "diagnostic", None))
                if contract_stage is not None:
                    contract_stage.finish("FAILED", error_code=getattr(exc, "code", "answer_contract_failed"),
                                          metadata={"answer_contract_status": "rejected",
                                                    "answer_produced": bool(getattr(answer, "text", ""))})
                raise
            if contract_stage is not None:
                contract_stage.finish("COMPLETED", metadata={
                    "answer_contract_status": getattr(contract, "status", "unknown"),
                    "answer_produced": bool(getattr(answer, "text", "")),
                })
        stats = provider.request_stats() if callable(getattr(provider, "request_stats", None)) else None
        request_diagnostics = _phase_diagnostics(state, conversation, stats)
        if state.store is not None:
            with lifecycle_stage("session_persistence"):
                if agent_execution is not None:
                    agent_report = agent_execution.report
                    phases = dict(request_diagnostics)
                    phases["agent_execution"] = state.phase_diagnostics["agent_execution"]
                    phases.update({f"agent_planner_{position}": diagnostic
                        for position, diagnostic in enumerate(agent_report.planner_request_diagnostics, 1)
                        if isinstance(diagnostic, dict)})
                    self.session_service.commit_execution_turn(
                        state.store, request.conversation_id, state.repository_id,
                        agent_report.execution_id, request.query, answer, provider,
                        request_diagnostics=phases,
                    )
                else:
                    self.session_service.commit_completed_turn(
                        state.store, request.conversation_id, state.repository_id,
                        request.query, answer, provider, request_diagnostics=request_diagnostics,
                    )
        streaming.complete_validated(
            answer.text, usage=stats.get("last_usage") if isinstance(stats, dict) else None,
        )
        memory_context = _injected_memory_metadata(conversation)
        index_lifecycle = (
            derive_index_lifecycle(repository_id=None).to_dict()
            if index_store is None else
            IndexLifecycle(IndexLifecycleState.NOT_REQUIRED, state.repository_id,
                           "not_used", "not_used", "general_route_skips_repository_index").to_dict()
        )
        return ConversationResult(
            answer, contract, "offline" if isinstance(provider, OfflineProvider)
            else getattr(provider, "provider_id", "unknown"),
            {"mode": "general", "route": ("agent_execution" if agent_execution is not None else "general"),
             "rule": "dialogue_plan",
             "retrieval_status": "not_required",
             "retrieval_failure_code": None,
             "retrieval_reason_code": "no_repository_bound_to_session",
             "evidence_count": 0,
             "reason": dialogue_plan.reason, "planner_source": dialogue_plan.planner_source,
             "planner_confidence": dialogue_plan.confidence,
             "answer_strategy": dialogue_plan.answer_strategy,
             "requires_agent_execution": dialogue_plan.requires_agent_execution,
             "requires_external_data": dialogue_plan.requires_external_data,
             "tasks": [_task_payload(task) for task in dialogue_plan.tasks],
             "confidence": "not_applicable", "uncertainty": None,
              "unavailable_channels": [], "index_lifecycle": index_lifecycle,
              "conversation_lookup_symbol": None,
             **({"agent_execution": state.phase_diagnostics["agent_execution"]}
                if agent_execution is not None else {})},
            {"estimated_units": ((conversation.get("token_budget_diagnostics") or {}).get("estimated_input_tokens", 0)
                                  if isinstance(conversation, dict) else 0),
             "limit": ((conversation.get("token_budget_diagnostics") or {}).get("input_budget_tokens",
                                                                               model_budget["input_budget"])
                       if isinstance(conversation, dict) else model_budget["input_budget"]),
             "budget_unit": "tokens",
             "estimate_kind": ((conversation.get("token_budget_diagnostics") or {}).get("tokenizer_method",
                                                                                      model_budget["estimate_kind"])
                               if isinstance(conversation, dict) else model_budget["estimate_kind"]),
             "model_context_window": model_budget["context_window"],
             "reserved_output_tokens": model_budget["output_reserve"],
             "safety_margin_tokens": model_budget["safety_margin"],
             "token_estimate_kind": model_budget["estimate_kind"],
             "token_estimate_description": model_budget.get("estimate_description"),
             "model_max_input_tokens": model_budget.get("max_input_tokens"),
             "model_max_output_tokens": model_budget.get("max_output_tokens"),
             "reasoning_token_limit": model_budget.get("reasoning_token_limit"),
             "capability_source": model_budget.get("capability_source"),
             "capability_verified_at": model_budget.get("capability_verified_at"),
             "request_phases": _phase_diagnostics(state, conversation, stats)},
            role_coverage("", (), ()),
            ({**conversation.get("context_diagnostics", {}),
              "token_estimate": conversation.get("token_budget_diagnostics")}
             if conversation else None),
            _summary_diagnostics(state.store),
            {"enabled": request.use_chroma,
             "memory_retrieved_count": state.memory_retrieved_count,
             "memory_context_count": len(memory_context), "memory_context": memory_context,
             "embedding_model_ready_seconds": state.embedding_readiness_seconds},
            stats, provider,
            agent_execution={"execution_id": agent_execution.report.execution_id,
                             "status": agent_execution.report.status,
                             "rounds": agent_execution.report.execution_rounds,
                             "tool_call_count": len(agent_execution.report.execution_results)}
            if agent_execution is not None else None,
        )

    def _prepare_history(self, request, state, provider):
        conversation = state.conversation
        summarizer = (state.store and callable(getattr(provider, "summary_text", None))
                      and callable(state.store.prepare_history))
        summary_callback = None
        if summarizer and callable(self.dependencies.summary_callback_factory):
            summary_callback = self.dependencies.summary_callback_factory(provider)
        fallback_codes = {
            "semantic_summary_turn_input_too_large",
            "semantic_summary_no_complete_turn_fits",
            "semantic_summary_response_invalid",
            "semantic_summary_merged_output_too_large",
            "semantic_summary_fact_protection_overflow",
            "semantic_summary_storage_budget_exceeded",
        }
        with lifecycle_stage("session_context_preparation", metadata={
            "provider": getattr(provider, "provider_id", None),
            "model": getattr(provider, "model", None),
        }) as context_stage:
            try:
                restored = self.session_service.restore_context(
                    state.store, request.conversation_id, state.repository_id,
                    summarizer=summary_callback,
                    summary_model=getattr(provider, "model", None) if summary_callback else None,
                )
            except Exception as exc:
                diagnostics = getattr(state.store, "last_summary_diagnostics", ())
                failure_code = (diagnostics[-1].get("failure_code")
                                if diagnostics and isinstance(diagnostics[-1], dict) else None)
                if failure_code not in fallback_codes:
                    raise
                # Compaction is an optional request-context optimization. Keep
                # the authoritative raw messages and existing summary intact,
                # then let the normal model context assembler select recent
                # turns under the active model's budget. Scope/database errors
                # do not carry one of these summary-only codes and still fail.
                restored = self.session_service.load_context(
                    state.store, request.conversation_id, state.repository_id,
                )
                if context_stage is not None:
                    context_stage.finish("DEGRADED", error_code="semantic_summary_overflow",
                                         metadata={
                                             "error_type": type(exc).__name__,
                                             "reason_code": failure_code,
                                             "fallback_used": True,
                                         })
        conversation["history"] = restored.history
        from .conversation_context import ConversationTurn
        conversation["persisted_history"] = tuple(ConversationTurn(
            request.conversation_id, state.repository_id, row["sequence"], row["role"],
            row["content"], row.get("status", "completed"),
        ) for row in restored.rows
            if row.get("role") in {"user", "assistant"}
            and row.get("status", "completed") == "completed"
            and isinstance(row.get("content"), str) and row.get("content").strip())
        conversation["summary"] = restored.summary["content"] if restored.summary else None
        conversation["summary_covered_through"] = restored.covered_through
        conversation["context_fact_ledger"] = restored.fact_ledger
        state.rows = restored.rows


def _task_payload(task):
    """Expose plan shape without copying question or search-query content to diagnostics."""
    return {"task_id": task.task_id, "task_type": task.task_type,
            "requires_retrieval": task.requires_retrieval,
            "search_query_count": len(task.search_queries),
            "dependencies": list(task.dependencies)}


def _agent_execution_available(executor, request):
    capability = getattr(executor, "execution_available_for", None)
    if callable(capability):
        try:
            return bool(capability(request))
        except Exception:
            return False
    return callable(getattr(executor, "execute", None))


def _verified_agent_read_sources(agent_execution, index_store, repository_id, repository, *,
                                diagnostics=None):
    """Return only read observations exactly matched to request-local SQLite chunks.

    Search output and read output are both treated as untrusted. The internal
    read-only toolbox must have produced a registered search candidate, the
    search result must carry that toolbox's request-local source identity, and
    the subsequent read must match the exact indexed SQLite chunk or a bounded
    subset of its lines. Anything else remains an ordinary observation.
    """
    def reject(reason_code):
        if isinstance(diagnostics, list) and len(diagnostics) < 64:
            diagnostics.append({
                "stage": "sqlite_source_resolution",
                "reason_code": reason_code,
            })

    if (index_store is None
            or not isinstance(repository_id, str) or not repository_id.strip()
            or not isinstance(repository, str) or not repository.strip()):
        reject("SOURCE_RESOLUTION_FAILED")
        return ()
    toolbox = getattr(agent_execution, "toolbox", None)
    if not isinstance(toolbox, ReadOnlyToolbox):
        reject("SOURCE_RESOLUTION_FAILED")
        return ()
    report = getattr(agent_execution, "report", None)
    execution_id = getattr(report, "execution_id", None)
    raw_results = tuple(getattr(report, "execution_results", ()) or ())
    if (not isinstance(execution_id, str) or not execution_id.strip()
            or not isinstance(getattr(report, "status", None), str)
            or getattr(report, "status", None) != "completed"):
        reject("SOURCE_RESOLUTION_FAILED")
        return ()
    try:
        root = Path(repository).resolve(strict=True)
        metadata = index_store.metadata()
        indexed_root = Path(metadata.get("repository", "")).resolve(strict=True)
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError, StoreError, sqlite3.Error):
        reject("SOURCE_RESOLUTION_FAILED")
        return ()
    try:
        metadata_version = int(metadata.get("index_version"))
        store_version = getattr(index_store, "schema_version", None)
    except (TypeError, ValueError):
        reject("INDEX_VERSION_MISMATCH")
        return ()
    if (isinstance(store_version, bool) or not isinstance(store_version, int)
            or metadata_version != store_version):
        reject("INDEX_VERSION_MISMATCH")
        return ()
    if root != indexed_root:
        reject("REPO_ID_MISMATCH")
        return ()
    try:
        from .repositories import repository_identity
        if repository_identity(root) != repository_id:
            reject("REPO_ID_MISMATCH")
            return ()
    except (OSError, RuntimeError, TypeError, ValueError):
        reject("REPO_ID_MISMATCH")
        return ()

    candidates_by_citation = getattr(toolbox, "last_candidate_by_citation", None)
    source_ids_by_hash = getattr(toolbox, "last_evidence_source_ids", None)
    if not isinstance(candidates_by_citation, dict) or not isinstance(source_ids_by_hash, dict):
        reject("CANDIDATE_METADATA_MISSING")
        return ()

    def _valid_call(result, expected_tool):
        if not isinstance(result, dict) or result.get("tool_id") != expected_tool:
            return False
        if result.get("status") != "completed":
            return False
        call_id = result.get("call_id")
        provenance = result.get("provenance")
        return (isinstance(call_id, str) and bool(call_id.strip())
                and isinstance(provenance, dict)
                and provenance.get("repository_id") == repository_id)

    def _valid_path(value):
        if not isinstance(value, str) or not value or "\\" in value or ":" in value:
            return False
        path = PurePosixPath(value)
        parts = value.split("/")
        return (not path.is_absolute() and ".." not in path.parts
                and all(part not in ("", ".", "..") for part in parts))

    verified = []
    seen = set()
    call_ids = [item.get("call_id") for item in raw_results if isinstance(item, dict)]
    if (len(call_ids) != len(raw_results)
            or any(not isinstance(value, str) or not value.strip() for value in call_ids)
            or len(call_ids) != len(set(call_ids))):
        reject("SOURCE_RESOLUTION_FAILED")
        return ()
    for read_position, read_result in enumerate(raw_results):
        if not _valid_call(read_result, "read"):
            if isinstance(read_result, dict) and read_result.get("tool_id") == "read":
                reject("REPO_ID_MISMATCH" if read_result.get("status") == "completed"
                       else "SOURCE_RESOLUTION_FAILED")
            continue
        read_output = read_result.get("output")
        if not isinstance(read_output, dict):
            reject("CANDIDATE_METADATA_MISSING")
            continue
        read_path = read_output.get("path")
        read_start = read_output.get("start_line")
        read_end = read_output.get("end_line")
        read_text = read_output.get("text")
        if (not _valid_path(read_path)
                or isinstance(read_start, bool) or not isinstance(read_start, int)
                or isinstance(read_end, bool) or not isinstance(read_end, int)
                or not isinstance(read_text, str) or not read_text
                or read_start < 1 or read_end < read_start):
            reject("LINE_RANGE_INVALID" if (
                isinstance(read_start, int) and not isinstance(read_start, bool)
                and isinstance(read_end, int) and not isinstance(read_end, bool)
            ) else "CANDIDATE_METADATA_MISSING")
            continue
        matched_prior_search = False
        for search_result in raw_results[:read_position]:
            if not isinstance(search_result, dict) or search_result.get("tool_id") != "search":
                continue
            if search_result.get("status") != "completed":
                continue
            if not _valid_call(search_result, "search"):
                reject("REPO_ID_MISMATCH")
                continue
            if search_result.get("call_id") == read_result.get("call_id"):
                continue
            search_output = search_result.get("output")
            if not isinstance(search_output, dict):
                reject("CANDIDATE_METADATA_MISSING")
                continue
            search_query = search_output.get("query")
            source_rows = search_output.get("results")
            if (not isinstance(search_query, str) or not search_query.strip()
                    or len(search_query) > 512 or not isinstance(source_rows, list)):
                reject("CANDIDATE_METADATA_MISSING")
                continue
            matched_prior_search = True
            if not source_rows:
                reject("SOURCE_NOT_FOUND")
            for source_row in source_rows:
                if not isinstance(source_row, dict):
                    reject("CANDIDATE_METADATA_MISSING")
                    continue
                citation = source_row.get("citation")
                candidate = candidates_by_citation.get(citation) if isinstance(citation, str) else None
                if candidate is None:
                    reject("CHUNK_ID_MISMATCH")
                    continue
                path = getattr(candidate, "relative_path", None)
                start = getattr(candidate, "start_line", None)
                end = getattr(candidate, "end_line", None)
                text = getattr(candidate, "text", None)
                content_hash = getattr(candidate, "content_hash", None)
                source_id = source_row.get("source_id")
                expected_source_id = source_ids_by_hash.get(content_hash)
                source_repository = source_row.get("repository")
                candidate_repository = getattr(candidate, "repository", None)
                try:
                    source_root = Path(source_repository).resolve(strict=True)
                    candidate_root = Path(candidate_repository).resolve(strict=True)
                except (OSError, RuntimeError, TypeError, ValueError):
                    reject("CANDIDATE_METADATA_MISSING")
                    continue
                source_start = source_row.get("start_line")
                source_end = source_row.get("end_line")
                if source_root != root or candidate_root != root:
                    reject("REPO_ID_MISMATCH")
                    continue
                if (not _valid_path(path) or citation != getattr(candidate, "citation", None)
                        or read_path != path or source_row.get("relative_path") != path):
                    reject("PATH_MISMATCH")
                    continue
                if (isinstance(source_start, bool) or not isinstance(source_start, int)
                        or source_start != start
                        or isinstance(source_end, bool) or not isinstance(source_end, int)
                        or source_end != end
                        or isinstance(start, bool) or not isinstance(start, int)
                        or isinstance(end, bool) or not isinstance(end, int)
                        or start < 1 or end < start):
                    reject("LINE_RANGE_INVALID")
                    continue
                if (source_row.get("text") != text
                        or source_row.get("content_hash") != content_hash
                        or not isinstance(content_hash, str)
                        or not re.fullmatch(r"[0-9a-f]{64}", content_hash)
                        or not isinstance(text, str)):
                    reject("CONTENT_HASH_MISMATCH")
                    continue
                if (not isinstance(source_id, str)
                        or source_id != expected_source_id
                        or re.fullmatch(r"E[1-9]\d*", source_id) is None):
                    reject("CHUNK_ID_MISMATCH")
                    continue
                try:
                    chunk = index_store.get_chunk(path, start, end)
                except (AttributeError, OSError, StoreError, sqlite3.Error, TypeError, ValueError):
                    reject("SOURCE_RESOLUTION_FAILED")
                    continue
                # ProductCandidate hashes have two established forms: SQLite
                # chunk hashes (raw text) and retrieval identity hashes
                # (path + line range + text). Validate the search record using
                # its own hash contract, then anchor trusted evidence to the
                # authoritative SQLite content hash.
                try:
                    retrieval_hash = _retrieval_content_hash(path, start, end, text)
                    sqlite_content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
                except (AttributeError, TypeError, UnicodeError):
                    reject("CONTENT_HASH_MISMATCH")
                    continue
                if not isinstance(chunk, dict):
                    reject("SOURCE_NOT_FOUND")
                    continue
                if (chunk.get("relative_path") != path
                        or chunk.get("start_line") != start
                        or chunk.get("end_line") != end):
                    reject("PATH_MISMATCH" if chunk.get("relative_path") != path
                           else "LINE_RANGE_INVALID")
                    continue
                if chunk.get("text") != text:
                    reject("SOURCE_STALE")
                    continue
                if (chunk.get("content_hash") != sqlite_content_hash
                        or content_hash not in {retrieval_hash, sqlite_content_hash}):
                    reject("CONTENT_HASH_MISMATCH")
                    continue
                if read_start < start or read_end > end:
                    reject("LINE_RANGE_INVALID")
                    continue
                lines = split_source_lines(text)
                expected_read = "\n".join(lines[read_start - start:read_end - start + 1])
                if read_text != expected_read:
                    reject("CONTENT_HASH_MISMATCH")
                    continue
                key = (sqlite_content_hash, read_result.get("call_id"), search_result.get("call_id"))
                if key in seen:
                    continue
                seen.add(key)
                verified_candidate = replace(candidate, content_hash=sqlite_content_hash)
                verified.append((verified_candidate, EvidenceProvenance(
                    source_kind="verified_agent_read",
                    repository_id=repository_id,
                    content_hash=sqlite_content_hash,
                    execution_id=execution_id,
                    search_call_id=search_result["call_id"],
                    tool_call_id=read_result["call_id"],
                    observation_id=read_result["call_id"],
                    tool_source_id=source_id,
                ), search_query))
        if not matched_prior_search:
            reject("SOURCE_NOT_FOUND")
    return tuple(verified)


def _tool_observation_context(report, evidence_source_ids_by_call_id=None):
    """Project tool results as observations and link only verified reads to evidence IDs."""
    import json

    evidence_source_ids_by_call_id = evidence_source_ids_by_call_id or {}
    output = []
    for result in tuple(getattr(report, "execution_results", ()) or ())[-8:]:
        if not isinstance(result, dict):
            continue
        tool_id = result.get("tool_id")
        status = result.get("status")
        if not isinstance(tool_id, str) or not isinstance(status, str):
            continue
        raw_output = result.get("output")
        result_ref = (raw_output.get("result_ref")
                      if isinstance(raw_output, dict) else None)
        is_result_page = (tool_id == "agent.read_result_page"
                          and isinstance(raw_output, dict)
                          and isinstance(raw_output.get("text"), str))
        if is_result_page:
            preview = raw_output["text"]
        elif isinstance(raw_output, dict) and isinstance(raw_output.get("preview"), str):
            preview = raw_output["preview"]
        elif raw_output is None:
            preview = ""
        else:
            try:
                preview = json.dumps(raw_output, ensure_ascii=False, separators=(",", ":"))
            except (TypeError, ValueError, OverflowError, RecursionError):
                preview = "[tool output could not be represented]"
        truncated = (not is_result_page and len(preview) > 1_200) or bool(
            isinstance(raw_output, dict) and raw_output.get("truncated"))
        if not is_result_page and len(preview) > 1_200:
            # Retain complete line boundaries where possible; the execution
            # result reference remains available for later bounded paging.
            prefix = preview[:1_200]
            boundary = prefix.rfind("\n")
            preview = prefix[:boundary] if boundary >= 600 else prefix
        item = {
            "call_id": result.get("call_id") if isinstance(result.get("call_id"), str) else None,
            "observation_id": result.get("call_id") if isinstance(result.get("call_id"), str) else None,
            "task_id": result.get("task_id") if isinstance(result.get("task_id"), str) else None,
            "tool_id": tool_id[:128], "status": status[:32],
            "output_preview": preview,
            "result_ref": result_ref if isinstance(result_ref, str) else None,
            "error_code": (result.get("error_code")[:64]
                           if isinstance(result.get("error_code"), str) else None),
            "truncated": truncated,
        }
        call_id = result.get("call_id")
        if isinstance(call_id, str) and call_id in evidence_source_ids_by_call_id:
            item["verified_evidence_source_ids"] = [evidence_source_ids_by_call_id[call_id]]
        if is_result_page:
            item["result_page"] = {
                key: raw_output[key] for key in (
                    "result_ref", "source_call_id", "source_sha256", "page_sha256",
                    "page_number", "offset", "next_offset", "total_chars", "complete",
                ) if key in raw_output
            }
        output.append(item)
    return tuple(output)


def _pending_approval_identity(value):
    if not isinstance(value, dict):
        return None
    request = value.get("request")
    if not isinstance(request, dict):
        return {"status": "pending"}
    return {key: request[key] for key in ("call_id", "task_id", "tool_id")
            if isinstance(request.get(key), str)}


def _safe_agent_tool_result(item):
    """Expose bounded execution outcome metadata without tool argument values."""
    if not isinstance(item, dict):
        return {}
    result = {key: item.get(key) for key in (
        "call_id", "task_id", "tool_id", "status", "error_code", "provenance",
    )}
    metadata = item.get("execution_metadata")
    if isinstance(metadata, dict):
        safe_metadata = {}
        stage = metadata.get("stage")
        if isinstance(stage, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", stage):
            safe_metadata["stage"] = stage
            result["failure_phase"] = stage
        diagnostic_code = metadata.get("diagnostic_code")
        if isinstance(diagnostic_code, str) and re.fullmatch(
                r"[a-z][a-z0-9_]{0,63}", diagnostic_code):
            safe_metadata["diagnostic_code"] = diagnostic_code
        argument_diagnostic = metadata.get("argument_diagnostic")
        if isinstance(argument_diagnostic, dict):
            # AgentExecutionEngine's sanitizer keeps stable codes, field names,
            # types and bounded numeric constraints, never parameter values.
            from .agent_execution import _safe_argument_diagnostic_details
            safe_diagnostic = _safe_argument_diagnostic_details(argument_diagnostic)
            if safe_diagnostic:
                safe_metadata["argument_diagnostic"] = safe_diagnostic
        if safe_metadata:
            result["execution_metadata"] = safe_metadata
    return result


def _agent_failure_metadata(report, planner_diagnostics):
    """Extract stable phase/code metadata without exposing planner or tool content."""
    code = getattr(report, "error_code", None)
    phase = getattr(report, "failure_phase", None)
    if isinstance(code, str) and code:
        return code, phase if isinstance(phase, str) else None
    for diagnostic in reversed(planner_diagnostics):
        code = diagnostic.get("planning_failure_code") or diagnostic.get("provider_failure_code")
        phase = diagnostic.get("planning_failure_phase") or diagnostic.get("provider_failure_phase")
        if isinstance(code, str) and code:
            return code, phase if isinstance(phase, str) else None
    for item in reversed(getattr(report, "execution_results", ())):
        code = item.get("error_code") if isinstance(item, dict) else None
        if isinstance(code, str) and code:
            metadata = item.get("execution_metadata")
            phase = metadata.get("stage") if isinstance(metadata, dict) else None
            return code, phase if isinstance(phase, str) and phase else "tool_execution"
    return None, None


def _merge_retrieval_outcomes(question, outcomes):
    if not outcomes:
        raise PlanningError("No repository retrieval task was executed.")
    confidence_rank = {"low": 0, "medium": 1, "high": 2}
    best = max(outcomes, key=lambda item: confidence_rank.get(item.decision.confidence, -1))
    candidates = _merge_candidates(candidate for item in outcomes for candidate in item.candidates)
    unavailable = tuple(dict.fromkeys(channel for item in outcomes for channel in item.unavailable_channels))
    failures = tuple(dict.fromkeys(failure for item in outcomes for failure in item.parse_failures))
    uncertainties = tuple(dict.fromkeys(item.uncertainty for item in outcomes if item.uncertainty))
    return replace(best, query=question, candidates=candidates,
                   unavailable_channels=unavailable, parse_failures=failures,
                   uncertainty="; ".join(uncertainties) if uncertainties else None)


def _retrieval_failure_code(error):
    """Return a bounded failure category; never expose exception text as evidence."""
    if isinstance(error, (StoreError, sqlite3.Error)):
        return "sqlite_index_unavailable"
    if isinstance(error, RepositoryEvidenceUnavailable):
        return "repository_evidence_unverified"
    if isinstance(error, ScanError):
        return "repository_scan_unavailable"
    if isinstance(error, OSError):
        return "repository_source_unavailable"
    return "repository_retrieval_unavailable"


def _unavailable_retrieval_outcome(question, repository_id, reason_code, *,
                                   sqlite_unavailable=False):
    """Represent a failed optional lookup with zero evidence and safe diagnostics."""
    decision = ProductIntentRouter().route(question)
    not_selected = reason_code == "repository_index_not_selected"
    stale_source = reason_code in {
        "repository_scan_unavailable", "repository_source_unavailable",
        "repository_evidence_unverified",
    }
    lifecycle = derive_index_lifecycle(
        repository_id=repository_id,
        sqlite_available=not sqlite_unavailable,
        indexed=not not_selected,
        stale=stale_source,
        reason_code=reason_code,
    ).to_dict()
    return ProductRetrievalOutcome(
        query=question,
        route_version=PRODUCT_ROUTE_VERSION,
        decision=decision,
        candidates=(),
        unavailable_channels=("sqlite" if sqlite_unavailable else "repository_index",),
        parse_failures=(),
        uncertainty=("Current repository evidence is unavailable for this turn; "
                     "repository-specific facts cannot be confirmed."),
        index_lifecycle=lifecycle,
    )


def _merge_candidates(candidates):
    values = []
    positions = {}
    for item in candidates:
        key = (item.relative_path.casefold(), item.start_line, item.end_line, item.content_hash)
        existing_index = positions.get(key)
        if existing_index is None:
            positions[key] = len(values)
            values.append(item)
            continue
        existing = values[existing_index]
        try:
            channel_scores = dict(existing.channel_scores)
            for channel, score in item.channel_scores:
                channel_scores[channel] = max(score, channel_scores.get(channel, score))
            resolution = (existing.resolution_status if existing.resolution_status == item.resolution_status
                          else "uncertain")
            values[existing_index] = replace(
                existing, score=max(existing.score, item.score),
                channels=tuple(dict.fromkeys(existing.channels + item.channels)),
                channel_scores=tuple(channel_scores.items()),
                symbol_ids=tuple(dict.fromkeys(existing.symbol_ids + item.symbol_ids)),
                relation_ids=tuple(dict.fromkeys(existing.relation_ids + item.relation_ids)),
                evidence_roles=tuple(dict.fromkeys(existing.evidence_roles + item.evidence_roles)),
                resolution_status=resolution,
            )
        except (AttributeError, TypeError, ValueError):
            continue
    return tuple(values)


def _injected_memory_metadata(conversation):
    """Expose only approved memories that were actually placed in this prompt."""
    if not isinstance(conversation, dict):
        return []
    injected_ids = set(conversation.get("injected_memory_ids", ()))
    return [{
        "memory_id": candidate.memory_id,
        "memory_type": candidate.memory_type,
        "version": candidate.version,
        "repository_id": candidate.repository_id,
        "scope": candidate.scope,
        "sources": [{"message_id": source.message_id,
                     "sequence": source.source_sequence,
                     "role": source.source_role,
                     "source_hash": source.source_hash}
                    for source in candidate.sources],
    } for candidate in conversation.get("memories", ()) if candidate.memory_id in injected_ids]


def _summary_diagnostics(store):
    value = getattr(store, "last_summary_diagnostics", ()) if store is not None else ()
    return tuple(value) if isinstance(value, (tuple, list)) else ()


def _resolved_answer_output(provider, request):
    resolved = getattr(provider, "max_output_tokens", None)
    if isinstance(resolved, int) and not isinstance(resolved, bool) and resolved > 0:
        return resolved
    return request.max_output_tokens


def _phase_diagnostics(state, conversation, request_stats):
    phases = dict(state.phase_diagnostics)
    memory_status = (conversation.get("memory_retrieval")
                     if isinstance(conversation, dict) else None)
    if isinstance(memory_status, dict):
        phases["memory_retrieval"] = {
            key: value for key, value in memory_status.items()
            if key in {"status", "candidate_count", "error_type"}
        }
    context_budget = (conversation.get("token_budget_diagnostics")
                      if isinstance(conversation, dict) else None)
    if isinstance(context_budget, dict):
        phases["answer"] = {
            **context_budget,
            "usage": request_stats.get("last_usage") if isinstance(request_stats, dict) else None,
            "request_id": request_stats.get("last_request_id") if isinstance(request_stats, dict) else None,
        }
    return phases


__all__ = [
    "ConversationDependencies", "ConversationOrchestrator", "ConversationRequest",
    "ConversationResult", "ConversationSession",
]
