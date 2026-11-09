"""Application use case for one complete repository conversation turn."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, replace

from .answer_contract import (
    build_general_contract, build_offline_contract, build_planner_stop_contract, validate_answer_contract,
    verify_generated_answer, verify_hybrid_answer,
)
from .context import MAX_INTERMEDIATE_EVIDENCE_BYTES, build_context
from .conversation_context import ContextBudgetExceeded
from .dialogue_retrieval import _context_candidates, _conversation_retrieval
from .errors import ProviderError
from .evidence_roles import required_evidence_roles, role_coverage
from .models import Answer, BuiltContext
from .providers.offline import OfflineProvider
from .product_retrieval import ProductRetriever
from .dialogue_router import DialogueMode, plan_dialogue
from .evidence_evaluator import assess_evidence
from .planner import LLMPlanner, PlanningError
from .session_service import SessionService
from .streaming import ConversationCancelled, StreamingManager


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
        try:
            result = self.planner.plan(provider, request.query, **planner_options)
        except PlanningError as exc:
            # Preserve only structured, bounded planner diagnostics on the failure.
            # This lets the CLI explain schema rejection without logging query text.
            if isinstance(self.planner, LLMPlanner):
                details = dict(exc.diagnostic_details or {})
                details["planner_diagnostics"] = self.planner.last_request_diagnostics or {}
                exc.diagnostic_details = details
            raise
        state.phase_diagnostics["planner"] = self.planner.last_request_diagnostics
        return plan_dialogue(requested, request.query, planner_result=result)

    def run(self, request: ConversationRequest, *, index_store=None, vector_retriever=None,
            session: ConversationSession | None = None, on_content=None, on_event=None,
            cancel_event=None, planned_plan=None) -> ConversationResult:
        streaming = StreamingManager(
            on_event=on_event, on_content=on_content, enabled=request.stream,
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
        agent_execution = None
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
                "tool_results": [{key: item.get(key) for key in (
                    "call_id", "task_id", "tool_id", "status", "error_code", "provenance",
                )} for item in report.execution_results],
            }
        if dialogue_plan.mode is DialogueMode.GENERAL:
            return self._run_general(request, state, dialogue_plan, streaming=streaming,
                                     agent_execution=agent_execution)
        if index_store is None:
            raise ValueError("Repository and Hybrid dialogue require an explicitly selected repository index.")
        conversation = state.conversation
        retriever = self.dependencies.retriever_factory(
            index_store, request.repository, vector_retriever=vector_retriever,
        )
        repo_tasks = [task for task in dialogue_plan.tasks if task.task_type == "repository"]
        retrieval_queries = []
        for task in repo_tasks:
            task_queries = task.search_queries or (task.question,)
            for query in task_queries:
                if query not in retrieval_queries:
                    retrieval_queries.append(query)
        if not retrieval_queries:
            raise PlanningError("Repository plan contains no retrieval query.")
        if len(retrieval_queries) > 6:
            raise PlanningError("Repository plan exceeds the bounded retrieval-query limit.")

        outcomes = []
        selections = []
        followup_symbols = []
        for position, query in enumerate(retrieval_queries, 1):
            streaming.check_cancelled()
            streaming.retrieval_progress(position - 1, len(retrieval_queries))
            outcome_item = retriever.search(query, top_k=request.top_k)

            class Arguments:
                pass
            arguments = Arguments()
            arguments.query = query
            arguments.top_k = request.top_k
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

        if agent_execution is not None:
            # Agent tool searches are still only candidates. The normal evidence
            # evaluator decides relevance and the existing Answer Contract owns
            # the final citation decision.
            selections.extend(agent_execution.toolbox.last_candidate_by_citation.values())

        outcome = _merge_retrieval_outcomes(request.query, outcomes)
        candidates = _merge_candidates(selections)
        followup_symbol = followup_symbols[0] if followup_symbols else None
        # Pronoun follow-ups may omit the symbol in the latest question. The
        # resolver supplies only a unique symbol grounded in prior session
        # turns and current index; do not use arbitrary planner search text as
        # relevance proof for its own retrieval results.
        evidence_query = (request.query + " " + followup_symbol) if followup_symbol else request.query
        evidence_assessment = assess_evidence(evidence_query, outcome.decision, candidates)
        selected = evidence_assessment.relevant_candidates
        results = [item.as_search_result() for item in selected]
        model_budget = self.dependencies.model_budget_resolver(
            state.provider, _resolved_answer_output(state.provider, request), request.context_budget,
            provider_id=request.provider_id, model_id=request.model_id,
        )
        required_roles = required_evidence_roles(request.query)
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
            context = replace(context,
                              tool_observations=_tool_observation_context(agent_execution.report))
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
        available_roles = (role for source in context.evidence
                           for role in source.result.evidence_roles)
        initial_role_audit = role_coverage(request.query, available_roles)
        role_gate_failure = bool(initial_role_audit["missing_available_roles"])
        if role_gate_failure:
            uncertainty = (
                "The available evidence does not cover required source roles: "
                + ", ".join(initial_role_audit["missing_available_roles"])
                + ". No model answer was requested."
            )
        evidence_gate_refusal = bool(
            not context.evidence or role_gate_failure
            or (outcome.decision.confidence == "low" and uncertainty)
        )
        if dialogue_plan.mode is DialogueMode.HYBRID:
            # Hybrid keeps the general component available when repository
            # evidence is missing or incomplete; the contract gates only its
            # repository section.
            evidence_gate_refusal = False

        provider = state.provider
        if not evidence_gate_refusal and provider is None:
            provider = self._create_provider(request)
            state.provider = provider

        if state.store is not None and not evidence_gate_refusal:
            if conversation is None:
                conversation = {"repository_id": state.repository_id}
                state.conversation = conversation
            self._prepare_history(request, state, provider)
            conversation = state.conversation
        elif not evidence_gate_refusal and not isinstance(conversation, dict):
            # Request-local diagnostics are retained even for stateless asks;
            # this mapping is never written to PostgreSQL without a store.
            conversation = {"repository_id": state.repository_id}
            state.conversation = conversation
        if isinstance(conversation, dict):
            conversation["input_token_budget_override"] = model_budget["input_budget"]

        if evidence_gate_refusal:
            answer = OfflineProvider().answer(context)
            contract = build_offline_contract(
                answer, context, route=outcome.decision.route,
                route_confidence=outcome.decision.confidence,
                candidate_by_citation=candidate_map, uncertainty=uncertainty,
                force_refusal_reason=uncertainty if role_gate_failure else None,
            )
            provider_mode = "evidence_gate_refusal"
        else:
            if isinstance(provider, OfflineProvider):
                answer = provider.answer(context)
            else:
                answer_method = (provider.answer_hybrid
                                 if dialogue_plan.mode is DialogueMode.HYBRID else provider.answer)
                answer_kwargs = {"conversation": conversation, "settings": conversation}
                if dialogue_plan.mode is DialogueMode.HYBRID:
                    answer_kwargs["external_data_unavailable"] = dialogue_plan.requires_external_data
                if request.stream:
                    answer_kwargs["on_content"] = streaming.capture_provider_delta
                streaming.check_cancelled()
                answer = answer_method(context, **answer_kwargs)
            if answer.offline:
                contract = build_offline_contract(
                    answer, context, route=outcome.decision.route,
                    route_confidence=outcome.decision.confidence,
                    candidate_by_citation=candidate_map, uncertainty=uncertainty,
                )
                provider_mode = "offline" if request.provider_id == "offline" else "offline_no_evidence"
            else:
                try:
                    if dialogue_plan.mode is DialogueMode.HYBRID:
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
        request_stats = (provider.request_stats()
                         if callable(getattr(provider, "request_stats", None)) else None)
        request_diagnostics = _phase_diagnostics(state, conversation, request_stats)
        if state.store is not None:
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
                      "matched_terms": list(item["matched_terms"]),
                      "reason": item["reason"]}
                     for item in evidence_assessment.candidate_results
                 ],
                 "semantic_entailment_verified": False,
             },
             "uncertainty": uncertainty, "unavailable_channels": list(outcome.unavailable_channels),
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
        if index_store is None and dialogue_plan.use_repository:
            raise ValueError("The selected plan requires an explicitly selected repository index.")
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
        execution = executor.execute(
            request, session=state, index_store=index_store,
            dialogue_plan=dialogue_plan, streaming=streaming,
            cancel_event=cancel_event,
        )
        report = execution.report
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
            "results": [{
                **{key: item.get(key) for key in (
                    "call_id", "task_id", "tool_id", "status", "error_code", "provenance",
                )},
                "failure_phase": ((item.get("execution_metadata") or {}).get("stage")
                                  if isinstance(item.get("execution_metadata"), dict) else None),
            } for item in report.execution_results],
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
        contract = build_planner_stop_contract(
            answer, route=dialogue_plan.mode.value, reason=reason,
            origin="general" if dialogue_plan.clarification_required else "external",
        )
        stats = (state.provider.request_stats()
                 if callable(getattr(state.provider, "request_stats", None)) else None)
        request_diagnostics = _phase_diagnostics(state, state.conversation, stats)
        if state.store is not None:
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

    def _run_general(self, request, state, dialogue_plan, *, streaming, agent_execution=None):
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
        if isinstance(provider, OfflineProvider) or not callable(answer_method):
            answer = OfflineProvider().answer(context)
        else:
            kwargs = {"conversation": conversation, "settings": conversation,
                      "external_data_unavailable": dialogue_plan.requires_external_data}
            if request.stream:
                kwargs["on_content"] = streaming.capture_provider_delta
            streaming.check_cancelled()
            answer = answer_method(context, **kwargs)
        try:
            contract = build_general_contract(answer)
            validate_answer_contract(contract, context)
        except ProviderError as exc:
            recorder = getattr(getattr(provider, "client", None), "record_application_failure", None)
            if callable(recorder):
                recorder("answer_contract", getattr(exc, "code", "answer_contract_failed"),
                         getattr(exc, "diagnostic", None))
            raise
        stats = provider.request_stats() if callable(getattr(provider, "request_stats", None)) else None
        request_diagnostics = _phase_diagnostics(state, conversation, stats)
        if state.store is not None:
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
        return ConversationResult(
            answer, contract, "offline" if isinstance(provider, OfflineProvider)
            else getattr(provider, "provider_id", "unknown"),
            {"mode": "general", "route": ("agent_execution" if agent_execution is not None else "general"),
             "rule": "dialogue_plan",
             "reason": dialogue_plan.reason, "planner_source": dialogue_plan.planner_source,
             "planner_confidence": dialogue_plan.confidence,
             "answer_strategy": dialogue_plan.answer_strategy,
             "requires_agent_execution": dialogue_plan.requires_agent_execution,
             "requires_external_data": dialogue_plan.requires_external_data,
             "tasks": [_task_payload(task) for task in dialogue_plan.tasks],
             "confidence": "not_applicable", "uncertainty": None,
             "unavailable_channels": [], "conversation_lookup_symbol": None,
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
        restored = self.session_service.restore_context(
            state.store, request.conversation_id, state.repository_id,
            summarizer=summary_callback,
            summary_model=getattr(provider, "model", None) if summary_callback else None,
        )
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


def _tool_observation_context(report):
    """Project execution results into a small, non-citable answer context."""
    import json

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
            "tool_id": tool_id[:128], "status": status[:32],
            "output_preview": preview,
            "result_ref": result_ref if isinstance(result_ref, str) else None,
            "error_code": (result.get("error_code")[:64]
                           if isinstance(result.get("error_code"), str) else None),
            "truncated": truncated,
        }
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
