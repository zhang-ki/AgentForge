"""Offline integration tests for automatic conversation-to-Agent execution."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor

from agentforge.conversation_agent import ConversationAgentExecutor
from agentforge.conversation_orchestrator import (
    ConversationDependencies, ConversationOrchestrator, ConversationRequest,
    ConversationSession,
)
from agentforge.indexer import index_repository
from agentforge.model_types import LOCAL_QWEN
from agentforge.models import Answer
from agentforge.store import IndexStore
from tests.support import LocalTestCase


def _conversation_plan():
    return json.dumps({
        "intent": "repository",
        "tasks": [{"task_id": "inspect", "task_type": "repository",
                   "question": "Inspect the target implementation.",
                   "requires_retrieval": True, "search_queries": ["compute_total"],
                   "dependencies": []},
                  {"task_id": "inspect_tool", "task_type": "tool",
                   "question": "Use an authorized tool to inspect the requested implementation.",
                   "requires_retrieval": False, "search_queries": [],
                   "dependencies": ["inspect"]}],
        "requires_repository": True,
        "requires_external_data": False,
        "requires_agent_execution": True,
        "answer_strategy": "evidence_based",
        "clarification_required": False,
        "confidence": 0.94,
        "reason": "The request needs a bounded iterative source inspection.",
    })


class _Provider:
    descriptor = LOCAL_QWEN
    provider_id = "llama-server"
    model = LOCAL_QWEN.model_id
    max_output_tokens = 256

    def __init__(self):
        self.planner_payloads = []
        self.planner_request_timeouts = []
        self.planner_output_budgets = []
        self.answer_context = None

    def plan_completion(self, messages, **_kwargs):
        payload = json.loads(messages[-1]["content"])
        self.planner_request_timeouts.append(("requested_mode" in payload,
                                              _kwargs.get("timeout_seconds")))
        self.planner_output_budgets.append(("requested_mode" in payload,
                                            _kwargs.get("max_output_tokens")))
        if "requested_mode" in payload:
            return _conversation_plan()
        self.planner_payloads.append(payload)
        if "search" not in payload["requested_schemas"]:
            return json.dumps({"action": "load_schemas", "tool_ids": ["search"]})
        if payload["observations"]:
            return json.dumps({"action": "finish"})
        return json.dumps({"action": "plan", "calls": [{
            "call_id": "find-implementation",
            "tool_id": "search", "arguments": {"query": "compute_total", "top_k": 5},
            "dependencies": [], "purpose": "Find the implementation requested by the user.",
            "expected_output": "Source candidates with paths and line ranges.",
        }]})

    def answer(self, context, **_kwargs):
        self.answer_context = context
        source = context.evidence[0]
        return Answer(f"The function returns the synthetic value [E1].", (source.citation,), False)

    def request_stats(self):
        return {"last_usage": {"prompt_tokens": 100, "completion_tokens": 20,
                                "total_tokens": 120}}


class _SingleToolProvider(_Provider):
    """Scripted offline Agent planner for unified-entry lifecycle tests."""

    def __init__(self, tool_id):
        super().__init__()
        self.tool_id = tool_id

    def plan_completion(self, messages, **_kwargs):
        payload = json.loads(messages[-1]["content"])
        if self.tool_id not in payload["requested_schemas"]:
            return json.dumps({"action": "load_schemas", "tool_ids": [self.tool_id]})
        if not payload["observations"]:
            return json.dumps({"action": "plan", "calls": [{
                "call_id": "lifecycle-call", "tool_id": self.tool_id,
                "arguments": {}, "dependencies": [], "purpose": "Read synthetic test data.",
                "expected_output": "A bounded synthetic tool result.",
            }]})
        return json.dumps({"action": "finish"})

    def answer_general(self, context, **_kwargs):
        self.answer_context = context
        return Answer("The synthetic tool completed.", (), False)


def _repository_plan_payload(intent="general", search_query="synthetic implementation"):
    return json.dumps({
        "intent": intent,
        "tasks": [{"task_id": "repository", "task_type": "repository",
                   "question": "Inspect the current repository implementation.",
                   "requires_retrieval": True, "search_queries": [search_query],
                   "dependencies": []}],
        "requires_repository": True,
        "requires_external_data": False,
        "requires_agent_execution": False,
        "answer_strategy": "direct", "clarification_required": False,
        "confidence": 0.92, "reason": "Repository facts need a fresh evidence lookup.",
    })


class _OptionalEvidenceProvider(_Provider):
    def __init__(self, *, intent="general", search_query="synthetic implementation"):
        super().__init__()
        self.plan_payload = _repository_plan_payload(intent, search_query)
        self.hybrid_context = None
        self.general_context = None

    def plan_completion(self, messages, **kwargs):
        payload = json.loads(messages[-1]["content"])
        if "requested_mode" in payload:
            return self.plan_payload
        return super().plan_completion(messages, **kwargs)

    def answer_hybrid(self, context, **_kwargs):
        self.hybrid_context = context
        return Answer(
            "General knowledge:\nA general explanation can still be provided.\n\n"
            "Repository evidence:\nI cannot confirm this repository's implementation from the "
            "current evidence.", (), False,
        )

    def answer_general(self, context, **_kwargs):
        self.general_context = context
        return Answer("A general explanation can still be provided.", (), False)


class _CountingRetrieverFactory:
    search_queries = []

    def __new__(cls, index_store, repository, **kwargs):
        from agentforge.product_retrieval import ProductRetriever

        cls.search_queries = []
        retriever = ProductRetriever(index_store, repository, **kwargs)
        original_search = retriever.search

        def search(query, *, top_k=5):
            cls.search_queries.append(query)
            return original_search(query, top_k=top_k)

        retriever.search = search
        return retriever


def _general_agent_dialogue_plan():
    from agentforge.dialogue_router import DialogueMode, DialoguePlan
    from agentforge.planner import ConversationTask

    return DialoguePlan(
        mode=DialogueMode.GENERAL, use_repository=False, use_general_knowledge=True,
        reason="The general answer requires a registered synthetic tool.",
        tasks=(ConversationTask("general", "general", "Use the synthetic tool result.",
                                False, (), ()),),
        requires_agent_execution=True, answer_strategy="direct", planner_source="test",
    )


class ConversationAgentIntegrationTests(LocalTestCase):
    def test_agent_search_then_observation_read_becomes_request_local_contract_evidence(self):
        import hashlib
        from types import SimpleNamespace
        from agentforge.agent_loop import ReadOnlyToolbox
        from agentforge.answer_contract import (
            ANSWER_CONTRACT_VERSION, AnswerClaim, VerifiedAnswerContract,
            _contract_evidence, validate_answer_contract,
        )
        from agentforge.context import build_context
        from agentforge.errors import ProviderError
        from agentforge.repositories import repository_identity
        from agentforge.conversation_orchestrator import _tool_observation_context

        source = self.write(
            "synthetic_calculation.py",
            "def calculate_weighted_total(amount, fee_rate):\n"
            "    return round(amount * (1 + fee_rate), 2)\n",
        )
        repository_id = repository_identity(self.root)
        with IndexStore(":memory:") as store:
            index_repository(self.root, store)
            toolbox = ReadOnlyToolbox(self.root, store)
            search_output = toolbox._tool_search({"query": "calculate_weighted_total"})
            self.assertTrue(search_output["results"])
            source_row = search_output["results"][0]
            read_output = toolbox._tool_read({
                "path": source_row["relative_path"],
                "start_line": source_row["start_line"],
                "end_line": source_row["end_line"],
            })
            execution_results = [
                {"call_id": "search-call", "task_id": "find", "tool_id": "search",
                 "status": "completed", "output": search_output,
                 "provenance": {"repository_id": repository_id}},
                {"call_id": "read-call", "task_id": "open", "tool_id": "read",
                 "status": "completed", "output": read_output,
                 "provenance": {"repository_id": repository_id}},
            ]
            report = SimpleNamespace(
                execution_id="execution-synthetic-1", status="completed",
                execution_results=execution_results,
            )
            execution = SimpleNamespace(toolbox=toolbox, report=report)
            from agentforge.conversation_orchestrator import _verified_agent_read_sources
            verified = _verified_agent_read_sources(
                execution, store, repository_id, str(self.root),
            )

            self.assertEqual(1, len(verified))
            candidate, provenance, search_query = verified[0]
            self.assertEqual("calculate_weighted_total", search_query)
            self.assertEqual(candidate.text, read_output["text"])
            self.assertEqual("verified_agent_read", provenance.source_kind)
            self.assertEqual("execution-synthetic-1", provenance.execution_id)
            self.assertEqual("search-call", provenance.search_call_id)
            self.assertEqual("read-call", provenance.tool_call_id)
            self.assertEqual("read-call", provenance.observation_id)
            self.assertEqual(source_row["source_id"], provenance.tool_source_id)
            authoritative_chunk = store.get_chunk(
                source_row["relative_path"], source_row["start_line"], source_row["end_line"],
            )
            authoritative_hash = hashlib.sha256(
                authoritative_chunk["text"].encode("utf-8"),
            ).hexdigest()
            self.assertEqual(authoritative_hash, candidate.content_hash)
            self.assertEqual(authoritative_hash, provenance.content_hash)
            self.assertNotEqual(source_row["content_hash"], authoritative_hash)

            from dataclasses import replace
            result = replace(candidate.as_search_result(), provenance=provenance)
            context = build_context(
                "What formula is implemented?", [result],
                evidence_source_ids={result.content_hash: "E1"},
            )
            contract = VerifiedAnswerContract(
                ANSWER_CONTRACT_VERSION, "limited", "Source result available.",
                "Semantic entailment is not measured by this contract.",
                (AnswerClaim("Source evidence is available.", "unknown", (), "general"),),
                _contract_evidence(context), "hybrid", "high",
            )
            validate_answer_contract(contract, context)
            observations = _tool_observation_context(
                report, {"read-call": context.evidence[0].source_id},
            )
            self.assertEqual(["E1"], observations[1]["verified_evidence_source_ids"])

            negative_mutations = {
                "hash_mismatch": lambda search, read: search["output"]["results"][0].__setitem__(
                    "content_hash", "0" * 64),
                "nonexistent_path": lambda search, read: read["output"].__setitem__(
                    "path", "missing/not_indexed.py"),
                "line_out_of_bounds": lambda search, read: read["output"].__setitem__(
                    "end_line", source_row["end_line"] + 1),
                "missing_source_metadata": lambda search, read: search["output"]["results"][0].pop(
                    "repository"),
                "forged_source_id": lambda search, read: search["output"]["results"][0].__setitem__(
                    "source_id", "E99"),
            }
            import copy
            for name, mutate in negative_mutations.items():
                with self.subTest(reason=name):
                    malformed = copy.deepcopy(execution_results)
                    mutate(malformed[0], malformed[1])
                    malformed_report = SimpleNamespace(
                        execution_id="execution-synthetic-1", status="completed",
                        execution_results=malformed,
                    )
                    diagnostics = []
                    self.assertEqual((), _verified_agent_read_sources(
                        SimpleNamespace(toolbox=toolbox, report=malformed_report),
                        store, repository_id, str(self.root), diagnostics=diagnostics,
                    ))
                    expected_reason = {
                        "hash_mismatch": "CONTENT_HASH_MISMATCH",
                        "nonexistent_path": "PATH_MISMATCH",
                        "line_out_of_bounds": "LINE_RANGE_INVALID",
                        "missing_source_metadata": "CANDIDATE_METADATA_MISSING",
                        "forged_source_id": "CHUNK_ID_MISMATCH",
                    }[name]
                    self.assertIn(expected_reason, {
                        item["reason_code"] for item in diagnostics
                    })

            diagnostics = []
            current_index_version = store.schema_version
            store.connection.execute(
                "UPDATE metadata SET value = ? WHERE key = 'index_version'",
                (str(current_index_version + 100),),
            )
            try:
                self.assertEqual((), _verified_agent_read_sources(
                    execution, store, repository_id, str(self.root), diagnostics=diagnostics,
                ))
                self.assertIn("INDEX_VERSION_MISMATCH", {
                    item["reason_code"] for item in diagnostics
                })
            finally:
                store.connection.execute(
                    "UPDATE metadata SET value = ? WHERE key = 'index_version'",
                    (str(current_index_version),),
                )

            from dataclasses import replace as dataclass_replace
            bad_hash = dataclass_replace(contract.evidence[0], content_hash="0" * 64)
            with self.assertRaisesRegex(ProviderError, "evidence text or provenance"):
                validate_answer_contract(dataclass_replace(contract, evidence=(bad_hash,)), context)
            forged_provenance = dataclass_replace(
                contract.evidence[0],
                provenance=dataclass_replace(provenance, tool_source_id="E99"),
            )
            with self.assertRaisesRegex(ProviderError, "evidence text or provenance"):
                validate_answer_contract(
                    dataclass_replace(contract, evidence=(forged_provenance,)), context,
                )

    def test_orchestrator_maps_verified_agent_read_into_answer_contract(self):
        import hashlib
        from types import SimpleNamespace
        from agentforge.agent_loop import ReadOnlyToolbox
        from agentforge.dialogue_router import DialogueMode, DialoguePlan
        from agentforge.models import EvidenceProvenance
        from agentforge.planner import ConversationTask
        from agentforge.repositories import repository_identity

        class EvidencedProvider(_Provider):
            def answer_hybrid(self, context, **_kwargs):
                self.answer_context = context
                evidence = context.evidence[0]
                text = ("General knowledge:\nA fee-adjusted total applies the rate to the amount.\n"
                        "Repository evidence:\nThe function rounds amount times one plus fee_rate [E1].")
                return Answer(text, (evidence.citation,), False)

        provider = EvidencedProvider()
        self.write(
            "synthetic_calculation.py",
            "def calculate_weighted_total(amount, fee_rate):\n"
            "    return round(amount * (1 + fee_rate), 2)\n",
        )
        repository_id = repository_identity(self.root)

        class CompletedAgent:
            def execute(_self, request, *, session, index_store, dialogue_plan,
                        streaming, cancel_event=None):
                toolbox = ReadOnlyToolbox(request.repository, index_store)
                # A Planner may use a broad retrieval phrase that has low
                # route confidence even when the user's actual question names
                # the exact repository symbol. The verified read must be
                # evaluated against the current user question, not this
                # model-generated search phrase.
                search_output = toolbox._tool_search({"query": "proportional fee round cents"})
                source = search_output["results"][0]
                read_output = toolbox._tool_read({
                    "path": source["relative_path"],
                    "start_line": source["start_line"],
                    "end_line": source["end_line"],
                })
                results = (
                    {"call_id": "search-call", "task_id": "find", "tool_id": "search",
                     "status": "completed", "output": search_output,
                     "provenance": {"repository_id": repository_id}},
                    {"call_id": "read-call", "task_id": "open", "tool_id": "read",
                     "status": "completed", "output": read_output,
                     "provenance": {"repository_id": repository_id}},
                )
                report = SimpleNamespace(
                    execution_id="execution-synthetic-1", status="completed",
                    execution_state="COMPLETED", execution_rounds=2,
                    execution_results=results, pending_approval=None,
                    user_input_request=None, planner_request_diagnostics=(),
                    retrieval={}, error_code=None,
                )
                return SimpleNamespace(report=report, provider=provider, toolbox=toolbox)

        dependencies = ConversationDependencies(
            provider_factory=lambda *_args, **_kwargs: provider,
            model_budget_resolver=lambda *_args, **_kwargs: {
                "input_budget": 3000, "output_reserve": 256,
                "safety_margin": 128, "context_window": 4096,
                "estimate_kind": "estimated",
            },
            agent_executor=CompletedAgent(),
        )
        with IndexStore(":memory:") as store:
            index_repository(self.root, store)
            orchestrator = ConversationOrchestrator(dependencies)
            request = ConversationRequest(
                "Use an authorized read-only tool to open synthetic_calculation.py in this "
                "repository, then report the formula in calculate_weighted_total and its result "
                "for amount 100 with fee rate 0.05.",
                str(self.root), "llama-server", mode="auto",
            )
            session = ConversationSession(provider=provider, repository_id=repository_id)
            plan = DialoguePlan(
                mode=DialogueMode.REPOSITORY, use_repository=True,
                use_general_knowledge=False,
                reason="The synthetic verification uses an authorized read-only tool.",
                tasks=(ConversationTask(
                    "inspect", "tool", "Read the identified synthetic source.",
                    True, ("calculate_weighted_total",), (),
                ),),
                requires_agent_execution=True, answer_strategy="evidence_based",
                confidence=0.9,
            )
            result = orchestrator.run(
                request, index_store=store, session=session, planned_plan=plan,
            )
            authoritative = store.get_chunk("synthetic_calculation.py", 1, 2)

        self.assertEqual("completed", result.execution_status)
        self.assertIn(result.contract.status, {"answered", "limited"})
        self.assertEqual(1, len(result.contract.evidence))
        evidence = result.contract.evidence[0]
        self.assertIsInstance(evidence.provenance, EvidenceProvenance)
        self.assertEqual("search-call", evidence.provenance.search_call_id)
        self.assertEqual("read-call", evidence.provenance.tool_call_id)
        self.assertEqual("read-call", evidence.provenance.observation_id)
        self.assertEqual(
            hashlib.sha256(authoritative["text"].encode("utf-8")).hexdigest(),
            evidence.content_hash,
        )
        observations = provider.answer_context.tool_observations
        self.assertEqual(["search-call", "read-call"],
                         [item["call_id"] for item in observations])
        self.assertEqual([evidence.source_id], observations[1]["verified_evidence_source_ids"])
        self.assertEqual({
            "verified_read_count": 1,
            "accepted_read_count": 1,
            "source_rejection_reason_codes": [],
            "relevance_rejection_reason_codes": [],
            "user_query_route_confidence": "medium",
            "search_route_confidences": ["low"],
        }, result.retrieval["agent_read_validation"])
        self.assertEqual("relevant", result.retrieval["evidence_assessment"][
            "candidate_decisions"][0]["status"])

    def test_conversation_request_rejects_invalid_budget_and_approval_state(self):
        invalid_requests = (
            {"output_budget_mode": "unbounded"},
            {"max_output_tokens": 0},
            {"max_output_tokens": True},
            {"approval_decision": "maybe"},
            {"approval_decision": "approve"},
        )
        for overrides in invalid_requests:
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                ConversationRequest("question", "synthetic", "llama-server", **overrides)

        approved = ConversationRequest(
            "resume approved task", "synthetic", "llama-server",
            conversation_id="conversation-1", execution_id="execution-1",
            approval_decision="approve",
        )
        self.assertEqual("approve", approved.approval_decision)

    def test_invalid_conversation_plan_fails_before_retrieval_or_tool_execution(self):
        from unittest.mock import Mock
        from agentforge.planner import PlanningError

        provider = _Provider()
        retriever_factory = Mock(side_effect=AssertionError("Invalid plans cannot retrieve."))
        executor = Mock()
        dependencies = ConversationDependencies(
            provider_factory=lambda *_args, **_kwargs: provider,
            model_budget_resolver=lambda *_args, **_kwargs: {"input_budget": 3000},
            retriever_factory=retriever_factory,
            agent_executor=executor,
        )
        events = []
        orchestrator = ConversationOrchestrator(dependencies)

        with self.assertRaisesRegex(PlanningError, "invalid type"):
            orchestrator.run(
                ConversationRequest("question", str(self.root), "llama-server"),
                index_store=object(), session=ConversationSession(provider=provider),
                planned_plan=object(), on_event=events.append,
            )

        retriever_factory.assert_not_called()
        executor.execute.assert_not_called()
        self.assertEqual("FAILED", events[-1].type.value)

    def test_execution_diagnostics_filter_malformed_observations_and_bound_error_metadata(self):
        from types import SimpleNamespace
        from agentforge.conversation_orchestrator import (
            _agent_failure_metadata, _pending_approval_identity,
            _tool_observation_context,
        )

        circular = {}
        circular["self"] = circular
        report = SimpleNamespace(execution_results=(
            "not-an-observation",
            {"tool_id": None, "status": "completed", "output": {"value": 1}},
            {"tool_id": "synthetic_reader", "status": "failed", "output": circular,
             "error_code": "TOOL_FAILED"},
        ))
        observations = _tool_observation_context(report)
        self.assertEqual(1, len(observations))
        self.assertEqual("synthetic_reader", observations[0]["tool_id"])
        self.assertEqual("[tool output could not be represented]", observations[0]["output_preview"])
        self.assertEqual("TOOL_FAILED", observations[0]["error_code"])

        self.assertEqual({"status": "pending"}, _pending_approval_identity({"request": "bad"}))
        self.assertEqual(
            {"call_id": "call-1", "tool_id": "read"},
            _pending_approval_identity({"request": {
                "call_id": "call-1", "tool_id": "read", "private": "not exposed",
            }}),
        )

        code, phase = _agent_failure_metadata(
            SimpleNamespace(error_code=None, failure_phase=None, execution_results=()),
            ({"planning_failure_code": "INVALID_PLAN", "planning_failure_phase": 17},),
        )
        self.assertEqual(("INVALID_PLAN", None), (code, phase))
        code, phase = _agent_failure_metadata(
            SimpleNamespace(error_code=None, failure_phase=None, execution_results=(
                {"error_code": "TOOL_TIMEOUT", "execution_metadata": {"stage": "execute"}},
            )), (),
        )
        self.assertEqual(("TOOL_TIMEOUT", "execute"), (code, phase))

    def test_agent_failure_payload_preserves_safe_read_reason_without_arguments(self):
        import json
        from agentforge.conversation_orchestrator import _safe_agent_tool_result

        secret = "synthetic-path-value-must-not-be-recorded"
        result = _safe_agent_tool_result({
            "call_id": "read-call", "task_id": "read-task", "tool_id": "read",
            "status": "failed", "error_code": "INVALID_ARGUMENTS",
            "provenance": {"repository_id": "synthetic-repository"},
            "request": {"arguments": {"path": secret}},
            "execution_metadata": {
                "stage": "tool_execution",
                "argument_diagnostic": {
                    "schema_error_code": "handler_rejected_arguments",
                    "handler_reason_code": "PATH_NOT_ALLOWED",
                    "argument_fields": ["path"],
                    "argument_types": {"path": "string"},
                    "handler_details": {"field": "path"},
                    "raw_value": secret,
                },
            },
        })

        serialized = json.dumps(result, sort_keys=True)
        self.assertEqual("PATH_NOT_ALLOWED", result["execution_metadata"][
            "argument_diagnostic"]["handler_reason_code"])
        self.assertEqual("tool_execution", result["failure_phase"])
        self.assertNotIn(secret, serialized)
        self.assertNotIn("request", result)

    def test_conversation_planner_catalog_uses_only_request_authorized_registry_tools(self):
        from agentforge.agent_execution import ToolAuthorization, ToolDefinition, ToolRegistry

        def registry_factory(_toolbox):
            registry = ToolRegistry()
            empty_schema = {"type": "object", "properties": {}, "required": [],
                            "additionalProperties": False}
            for tool_id, scope in (("synthetic_read", "synthetic:read"),
                                   ("unapproved_network", "network:access")):
                registry.register(ToolDefinition(
                    tool_id=tool_id, name=tool_id.replace("_", " "),
                    description="Inspect a bounded synthetic value.", version="1",
                    capabilities=frozenset({"synthetic.inspect"}),
                    input_schema=empty_schema, output_schema={"type": "object"},
                    permission_scope=scope,
                ), lambda _args, _context: {})
            return registry

        executor = ConversationAgentExecutor(
            tool_registry_factory=registry_factory,
            authorization_factory=lambda _request, _repository_id: ToolAuthorization(
                permission_scopes=frozenset({"synthetic:read"}),
            ),
        )
        request = ConversationRequest(
            "Use synthetic_read to inspect a value.", "stateless", "llama-server", mode="auto",
        )

        catalog = executor.planner_tool_catalog_for(request, query=request.query)

        self.assertEqual(["synthetic_read"], [item["tool_id"] for item in catalog])
        self.assertNotIn("input_schema", catalog[0])
        self.assertFalse(catalog[0]["side_effect"])

    def test_default_repository_tool_catalog_uses_the_injected_scope_without_index_tool(self):
        from agentforge.agent_execution import ToolAuthorization

        request = ConversationRequest(
            "Inspect the bound repository.", str(self.root), "llama-server", mode="auto",
        )
        read_authorized = ConversationAgentExecutor(
            authorization_factory=lambda _request, _repository_id: ToolAuthorization(
                permission_scopes=frozenset({"repository:read"}),
            ),
        )
        catalog = read_authorized.planner_tool_catalog_for(
            request, query=request.query, repository_id="synthetic-repository",
        )
        self.assertEqual({"read", "search", "list-symbols"},
                         {item["tool_id"] for item in catalog})
        self.assertNotIn("index", {item["tool_id"] for item in catalog})
        self.assertTrue(all(not item["side_effect"] for item in catalog))

        denied = ConversationAgentExecutor(
            authorization_factory=lambda _request, _repository_id: ToolAuthorization(
                permission_scopes=frozenset(),
            ),
        )
        self.assertEqual((), denied.planner_tool_catalog_for(
            request, query=request.query, repository_id="synthetic-repository",
        ))

    def test_auto_general_plan_skips_agent_engine(self):
        class GeneralProvider(_Provider):
            def plan_completion(self, messages, **_kwargs):
                payload = json.loads(messages[-1]["content"])
                if "requested_mode" not in payload:
                    raise AssertionError("General requests must not enter the Agent planner.")
                return json.dumps({
                    "intent": "general",
                    "tasks": [{"task_id": "explain", "task_type": "general",
                               "question": "Explain a general concept.",
                               "requires_retrieval": False, "search_queries": [], "dependencies": []}],
                    "requires_repository": False,
                    "requires_external_data": False,
                    "requires_agent_execution": False,
                    "answer_strategy": "direct", "clarification_required": False,
                    "confidence": 0.95, "reason": "No repository tools are needed.",
                })

            def answer_general(self, context, **_kwargs):
                return Answer("A general answer.", (), False)

        class ForbiddenExecutor:
            def execute(self, *_args, **_kwargs):
                raise AssertionError("The planner did not request tool execution.")

        provider = GeneralProvider()
        dependencies = ConversationDependencies(
            provider_factory=lambda *_args, **_kwargs: provider,
            model_budget_resolver=lambda *_args, **_kwargs: {
                "input_budget": 3000, "output_reserve": 256, "safety_margin": 128,
                "context_window": 4096, "estimate_kind": "estimated",
            },
            agent_executor=ForbiddenExecutor(),
        )
        result = ConversationOrchestrator(dependencies).run(
            ConversationRequest("Explain a general concept.", "stateless", "llama-server", mode="auto"),
            index_store=None, session=ConversationSession(provider=provider),
        )

        self.assertEqual("completed", result.execution_status, result.to_payload())
        self.assertNotEqual("agent_execution", result.retrieval.get("route"))
        self.assertEqual("A general answer.", result.answer.text)

    def test_bound_repository_retrieval_runs_even_when_plan_says_no_retrieval(self):
        from agentforge.dialogue_router import DialogueMode, DialoguePlan
        from agentforge.planner import ConversationTask

        self.write("src/compute_total.py", "def compute_total(values):\n    return sum(values)\n")
        provider = _OptionalEvidenceProvider(search_query="must not control retrieval")
        plan = DialoguePlan(
            mode=DialogueMode.GENERAL, use_repository=False,
            use_general_knowledge=True, reason="The plan suppresses repository lookup.",
            tasks=(ConversationTask("general", "general", "Answer generally.",
                                    False, (), ()),),
            requires_agent_execution=False, planner_source="llm",
        )
        dependencies = ConversationDependencies(
            provider_factory=lambda *_args, **_kwargs: provider,
            model_budget_resolver=lambda *_args, **_kwargs: {
                "input_budget": 3000, "output_reserve": 256, "safety_margin": 128,
                "context_window": 4096, "estimate_kind": "estimated",
            },
            retriever_factory=_CountingRetrieverFactory,
        )
        with IndexStore(":memory:") as store:
            index_repository(self.root, store)
            result = ConversationOrchestrator(dependencies).run(
                ConversationRequest("What does compute_total do?", str(self.root),
                                    "llama-server", mode="auto"),
                index_store=store,
                session=ConversationSession(provider=provider, repository_id="synthetic-repo"),
                planned_plan=plan,
            )

        self.assertIsNotNone(provider.hybrid_context)
        self.assertTrue(provider.hybrid_context.evidence)
        self.assertEqual("evidence_available", result.retrieval["retrieval_status"])
        self.assertEqual("completed", result.execution_status)
        self.assertFalse(result.retrieval["requires_agent_execution"])
        self.assertEqual(["What does compute_total do?"], _CountingRetrieverFactory.search_queries)

    def test_bound_repository_retrieval_and_default_read_only_tools_are_independent(self):
        import hashlib
        from dataclasses import replace
        from agentforge.repositories import repository_identity

        self.write(
            "synthetic_calculation.py",
            "def calculate_weighted_total(amount, fee_rate):\n"
            "    return round(amount * (1 + fee_rate), 2)\n",
        )
        order = []

        class DecoupledProvider(_Provider):
            # This integration test exercises planner/executor wiring, not the
            # local Qwen context ceiling. Keep its scripted multi-turn payload
            # within a representative larger-context fixture without changing
            # any production model or request-budget configuration.
            descriptor = replace(LOCAL_QWEN, context_window=8192)

            def __init__(self):
                super().__init__()
                self.agent_reads = []
                self.outer_plan = None
                self.outer_plan_output = None

            def plan_completion(self, messages, **_kwargs):
                payload = json.loads(messages[-1]["content"])
                if "requested_mode" in payload:
                    self.outer_plan = payload
                    self.outer_plan_output = {
                        "intent": "general",
                        "tasks": [{
                            "task_id": "inspect_with_tools", "task_type": "tool",
                            "question": "Inspect the calculation with authorized read-only tools.",
                            "requires_retrieval": False, "search_queries": [], "dependencies": [],
                        }],
                        "requires_repository": False,
                        "requires_external_data": False,
                        "requires_agent_execution": True,
                        "answer_strategy": "direct", "clarification_required": False,
                        "confidence": 0.95, "reason": "A tool observation is requested.",
                    }
                    return json.dumps(self.outer_plan_output)

                self.planner_payloads.append(payload)
                schemas = payload["requested_schemas"]
                observations = payload["observations"]
                if not observations:
                    if "search" not in schemas:
                        return json.dumps({"action": "load_schemas", "tool_ids": ["search"]})
                    return json.dumps({"action": "plan", "calls": [{
                        "call_id": "locate-source", "tool_id": "search",
                        "arguments": {"query": "calculate_weighted_total", "top_k": 5},
                        "dependencies": [], "purpose": "Find the requested calculation source.",
                        "expected_output": "Authorized indexed source candidates.",
                    }]})

                latest = observations[-1]
                if latest["tool_id"] == "search":
                    if "read" not in schemas:
                        return json.dumps({"action": "load_schemas", "tool_ids": ["read"]})
                    candidate = latest["output"]["results"][0]
                    arguments = {
                        "path": candidate["relative_path"],
                        "start_line": candidate["start_line"],
                        "end_line": candidate["end_line"],
                    }
                    self.agent_reads.append(dict(arguments))
                    return json.dumps({"action": "plan", "calls": [{
                        "call_id": "open-source", "tool_id": "read",
                        "arguments": arguments, "dependencies": ["locate-source"],
                        "purpose": "Read the exact authorized range returned by search.",
                        "expected_output": "The indexed source text for the calculation.",
                    }]})
                return json.dumps({"action": "finish"})

            def answer_hybrid(self, context, **_kwargs):
                self.answer_context = context
                source = context.evidence[0]
                return Answer(
                    "General knowledge:\nA weighted total applies a rate to an amount.\n\n"
                    "Repository evidence:\nThe function multiplies by one plus fee_rate and "
                    "rounds the result [E1].",
                    (source.citation,), False,
                )

        class TrackingRetrieverFactory:
            def __call__(self, index_store, repository, **kwargs):
                from agentforge.product_retrieval import ProductRetriever

                retriever = ProductRetriever(index_store, repository, **kwargs)
                original_search = retriever.search

                def search(query, *, top_k=5):
                    order.append(("repository_retrieval", query))
                    return original_search(query, top_k=top_k)

                retriever.search = search
                return retriever

        class TrackingExecutor(ConversationAgentExecutor):
            def execute(self, *args, **kwargs):
                order.append(("agent_execution", None))
                return super().execute(*args, **kwargs)

        provider = DecoupledProvider()
        dependencies = ConversationDependencies(
            provider_factory=lambda *_args, **_kwargs: provider,
            model_budget_resolver=lambda *_args, **_kwargs: {
                "input_budget": 3200, "output_reserve": 256,
                "safety_margin": 128, "context_window": 4096,
                "estimate_kind": "estimated",
            },
            agent_executor=TrackingExecutor(),
            retriever_factory=TrackingRetrieverFactory(),
        )
        repository_id = repository_identity(self.root)
        with IndexStore(":memory:") as store:
            index_repository(self.root, store)
            result = ConversationOrchestrator(dependencies).run(
                ConversationRequest(
                    "Explain calculate_weighted_total.",
                    str(self.root), "llama-server", mode="auto",
                ),
                index_store=store,
                session=ConversationSession(provider=provider, repository_id=repository_id),
            )
            authoritative = store.get_chunk("synthetic_calculation.py", 1, 2)

        self.assertEqual("completed", result.execution_status, result.to_payload())
        self.assertIsNotNone(provider.outer_plan)
        self.assertFalse(provider.outer_plan_output["requires_repository"])
        self.assertEqual("general", result.retrieval["mode"])
        self.assertTrue(result.retrieval["requires_agent_execution"])
        available_tools = {item["tool_id"] for item in provider.outer_plan["available_tools"]}
        self.assertEqual({"read", "search", "list-symbols"}, available_tools)
        self.assertEqual("evidence_available", result.retrieval["retrieval_status"])
        self.assertEqual(["repository_retrieval", "agent_execution"],
                         [item[0] for item in order[:2]])
        self.assertEqual(1, sum(item[0] == "repository_retrieval" for item in order))
        self.assertEqual(2, result.agent_execution["tool_call_count"])
        self.assertIn(result.contract.status, {"answered", "limited"})
        self.assertEqual(1, len(result.contract.evidence))
        self.assertTrue(provider.answer_context.evidence)
        evidence = provider.answer_context.evidence[0]
        self.assertEqual("locate-source", evidence.result.provenance.search_call_id)
        self.assertEqual("open-source", evidence.result.provenance.tool_call_id)
        self.assertEqual(
            hashlib.sha256(authoritative["text"].encode("utf-8")).hexdigest(),
            evidence.result.content_hash,
        )
        observations = provider.answer_context.tool_observations
        self.assertEqual(["locate-source", "open-source"],
                         [item["call_id"] for item in observations])
        self.assertEqual([evidence.source_id], observations[1]["verified_evidence_source_ids"])
        self.assertEqual({"path": "synthetic_calculation.py", "start_line": 1, "end_line": 2},
                         provider.agent_reads[0])
        self.assertIn("[E1]", result.answer.text)

    def test_unbound_repository_does_not_block_general_model_answer(self):
        provider = _OptionalEvidenceProvider(search_query="missing synthetic implementation")
        dependencies = ConversationDependencies(
            provider_factory=lambda *_args, **_kwargs: provider,
            model_budget_resolver=lambda *_args, **_kwargs: {
                "input_budget": 3000, "output_reserve": 256, "safety_margin": 128,
                "context_window": 4096, "estimate_kind": "estimated",
            },
        )

        result = ConversationOrchestrator(dependencies).run(
            ConversationRequest("Explain how this project implements a synthetic feature.",
                                "stateless", "llama-server", mode="auto"),
            index_store=None, session=ConversationSession(provider=provider),
        )

        self.assertEqual("completed", result.execution_status, result.to_payload())
        self.assertIsNotNone(provider.general_context)
        self.assertEqual((), provider.general_context.evidence)
        self.assertIn("A general explanation", result.answer.text)
        self.assertEqual("limited", result.contract.status)
        self.assertEqual("not_required", result.retrieval["retrieval_status"])
        self.assertEqual("no_repository_bound_to_session",
                         result.retrieval["retrieval_reason_code"])
        self.assertIsNone(result.retrieval["retrieval_failure_code"])
        self.assertEqual((), result.contract.evidence)

    def test_empty_repository_search_still_calls_model_without_fabricated_citations(self):
        self.write("src/other.py", "def unrelated_operation():\n    return 'different'\n")
        provider = _OptionalEvidenceProvider(
            intent="hybrid",
            search_query="synthetic_missing_function_9f7c2b",
        )
        dependencies = ConversationDependencies(
            provider_factory=lambda *_args, **_kwargs: provider,
            model_budget_resolver=lambda *_args, **_kwargs: {
                "input_budget": 3000, "output_reserve": 256, "safety_margin": 128,
                "context_window": 4096, "estimate_kind": "estimated",
            },
        )
        with IndexStore(":memory:") as store:
            index_repository(self.root, store)
            result = ConversationOrchestrator(dependencies).run(
                ConversationRequest("How would this project implement synthetic_missing_function_9f7c2b?",
                                    str(self.root), "llama-server", mode="auto"),
                index_store=store,
                session=ConversationSession(provider=provider, repository_id="synthetic-repo"),
            )

        self.assertEqual("completed", result.execution_status, result.to_payload())
        self.assertIsNone(provider.hybrid_context)
        self.assertEqual((), provider.general_context.evidence)
        self.assertIn("A general explanation", result.answer.text)
        self.assertEqual("no_evidence", result.retrieval["retrieval_status"])
        self.assertEqual((), result.contract.evidence)
        self.assertEqual((), result.answer.citations)
        self.assertEqual("limited", result.contract.status)
        self.assertEqual("general", result.contract.claims[0].origin)

    def test_evidence_free_repository_claim_with_fabricated_citation_is_rejected(self):
        from agentforge.errors import CitationValidationError

        class FabricatingProvider(_OptionalEvidenceProvider):
            def answer_general(self, context, **_kwargs):
                self.general_context = context
                return Answer(
                    "This project implements the feature in src/private.py [E1].",
                    ("src/private.py:1-4",), False,
                )

        provider = FabricatingProvider()
        dependencies = ConversationDependencies(
            provider_factory=lambda *_args, **_kwargs: provider,
            model_budget_resolver=lambda *_args, **_kwargs: {
                "input_budget": 3000, "output_reserve": 256, "safety_margin": 128,
                "context_window": 4096, "estimate_kind": "estimated",
            },
        )

        with self.assertRaises(CitationValidationError):
            ConversationOrchestrator(dependencies).run(
                ConversationRequest("Explain this project's feature implementation.",
                                    "stateless", "llama-server", mode="auto"),
                index_store=None, session=ConversationSession(provider=provider),
            )

        self.assertEqual((), provider.general_context.evidence)

    def test_sqlite_retrieval_failure_is_reported_and_answered_without_source_evidence(self):
        from agentforge.errors import StoreError

        provider = _OptionalEvidenceProvider(search_query="read missing SQLite index")

        def unavailable_retriever(*_args, **_kwargs):
            raise StoreError("synthetic isolated index unavailable")

        dependencies = ConversationDependencies(
            provider_factory=lambda *_args, **_kwargs: provider,
            model_budget_resolver=lambda *_args, **_kwargs: {
                "input_budget": 3000, "output_reserve": 256, "safety_margin": 128,
                "context_window": 4096, "estimate_kind": "estimated",
            },
            retriever_factory=unavailable_retriever,
        )
        result = ConversationOrchestrator(dependencies).run(
            ConversationRequest("Explain a concept even though the repository index is unavailable.",
                                "synthetic", "llama-server", mode="auto"),
            index_store=object(),
            session=ConversationSession(provider=provider, repository_id="synthetic-repo"),
        )

        self.assertEqual("completed", result.execution_status, result.to_payload())
        self.assertEqual("unavailable", result.retrieval["retrieval_status"])
        self.assertEqual("sqlite_index_unavailable", result.retrieval["retrieval_failure_code"])
        self.assertEqual("FAILED", result.retrieval["index_lifecycle"]["state"])
        self.assertIsNone(provider.hybrid_context)
        self.assertEqual((), provider.general_context.evidence)
        self.assertEqual((), result.contract.evidence)
        self.assertIn("A general explanation", result.answer.text)

    def test_embedding_and_chroma_failures_keep_verified_lexical_answer_available(self):
        from agentforge.index_lifecycle import UnavailableVectorRetriever
        from agentforge.dialogue_router import DialogueMode, DialoguePlan
        from agentforge.planner import ConversationTask

        class BrokenChroma:
            def inspect_consistency(self):
                return {"complete": True, "expected_vectors": 1, "stored_vectors": 1,
                        "invalid_vectors": 0}

            def search(self, *_args, **_kwargs):
                raise RuntimeError("synthetic Chroma failure")

        self.write("src/ops.py", "def compute_total(values):\n    return sum(values)\n")
        plan = DialoguePlan(
            mode=DialogueMode.REPOSITORY, use_repository=True, use_general_knowledge=False,
            reason="The synthetic implementation needs source context.",
            tasks=(ConversationTask(
                "repository", "repository", "Explain the sum implementation.",
                True, ("How does this implementation sum values?",), (),
            ),),
            planner_source="test",
        )
        for vector_retriever in (
            UnavailableVectorRetriever("embedding_service_unavailable", "RuntimeError"),
            BrokenChroma(),
        ):
            with self.subTest(vector_failure=type(vector_retriever).__name__):
                provider = _Provider()
                dependencies = ConversationDependencies(
                    provider_factory=lambda *_args, **_kwargs: provider,
                    model_budget_resolver=lambda *_args, **_kwargs: {
                        "input_budget": 3000, "output_reserve": 256, "safety_margin": 128,
                        "context_window": 4096, "estimate_kind": "estimated",
                    },
                )
                with IndexStore(":memory:") as store:
                    index_repository(self.root, store)
                    result = ConversationOrchestrator(dependencies).run(
                        ConversationRequest("How does this implementation sum values?",
                                            str(self.root), "llama-server", mode="auto", use_chroma=True),
                        index_store=store, vector_retriever=vector_retriever,
                        session=ConversationSession(provider=provider, repository_id="synthetic-repo"),
                        planned_plan=plan,
                    )

                self.assertEqual("completed", result.execution_status, result.to_payload())
                self.assertEqual("degraded", result.retrieval["retrieval_status"])
                self.assertIn("vector", result.retrieval["unavailable_channels"])
                self.assertEqual("DEGRADED_LEXICAL_ONLY",
                                 result.retrieval["index_lifecycle"]["state"])
                self.assertTrue(result.contract.evidence)
                self.assertTrue(result.answer.citations)

    def test_repository_retrieval_does_not_implicitly_start_agent_execution(self):
        self.write("src/ops.py", "def compute_total():\n    return 42\n")
        provider = _Provider()
        dependencies = ConversationDependencies(
            provider_factory=lambda *_args, **_kwargs: provider,
            model_budget_resolver=lambda *_args, **_kwargs: {
                "input_budget": 3000, "output_reserve": 256, "safety_margin": 128,
                "context_window": 4096, "estimate_kind": "estimated",
            },
        )
        with IndexStore(":memory:") as store:
            index_repository(self.root, store)
            result = ConversationOrchestrator(dependencies).run(
                ConversationRequest("Explain compute_total with a source citation.",
                                    str(self.root), "llama-server", mode="repository"),
                index_store=store,
                session=ConversationSession(provider=provider, repository_id="synthetic-repo"),
            )

        self.assertEqual("completed", result.execution_status, result.to_payload())
        self.assertEqual("repository", result.retrieval["mode"])
        self.assertIsNone(result.agent_execution)
        self.assertEqual(1, len(result.contract.evidence))
        self.assertTrue(result.answer.citations)

    def test_general_intent_can_execute_authorized_tool_without_repository_index(self):
        class GeneralToolProvider(_Provider):
            def __init__(self):
                super().__init__()
                self.executed_tool_answer_context = None

            def plan_completion(self, messages, **_kwargs):
                payload = json.loads(messages[-1]["content"])
                if "requested_mode" in payload:
                    return json.dumps({
                        "intent": "general",
                        "tasks": [{"task_id": "explain", "task_type": "general",
                                   "question": "Use the configured synthetic calculation tool.",
                                   "requires_retrieval": False, "search_queries": [],
                                   "dependencies": []},
                                  {"task_id": "calculate", "task_type": "tool",
                                   "question": "Call the listed synthetic calculation tool.",
                                   "requires_retrieval": False, "search_queries": [],
                                   "dependencies": ["explain"]}],
                        "requires_repository": False, "requires_external_data": False,
                        "requires_agent_execution": True,
                        "answer_strategy": "direct", "clarification_required": False,
                        "confidence": 0.98, "reason": "A tool result is needed for the requested value.",
                    })
                if "requested_schemas" not in payload:
                    raise AssertionError("The execution planner request shape was not used.")
                if "synthetic_calculate" not in payload["requested_schemas"]:
                    return json.dumps({"action": "load_schemas",
                                       "tool_ids": ["synthetic_calculate"]})
                if not payload["observations"]:
                    return json.dumps({"action": "plan", "calls": [{
                        "call_id": "calculate-once", "tool_id": "synthetic_calculate",
                        "arguments": {}, "dependencies": [], "purpose": "Calculate a synthetic value.",
                        "expected_output": "One synthetic value.",
                    }]})
                return json.dumps({"action": "finish"})

            def answer_general(self, context, **_kwargs):
                self.executed_tool_answer_context = context
                self.answer_context = context
                return Answer("The synthetic tool returned 42.", (), False)

        provider = GeneralToolProvider()
        tool_calls = []

        def registry_factory(toolbox):
            self.assertIsNone(toolbox)
            from agentforge.agent_execution import ToolDefinition, ToolRegistry
            registry = ToolRegistry()
            registry.register(ToolDefinition(
                tool_id="synthetic_calculate", name="Synthetic calculator",
                description="Calculate a synthetic numeric value.", version="1",
                capabilities=frozenset({"synthetic.calculate"}),
                input_schema={"type": "object", "properties": {}, "required": [],
                              "additionalProperties": False},
                output_schema={"type": "object"}, permission_scope="synthetic:read",
            ), lambda _args, _context: tool_calls.append("called") or {"value": 42})
            return registry

        executor = ConversationAgentExecutor(
            tool_registry_factory=registry_factory,
            authorization_factory=lambda _request, _repo: __import__(
                "agentforge.agent_execution", fromlist=["ToolAuthorization"]
            ).ToolAuthorization(permission_scopes=frozenset({"synthetic:read"})),
        )
        dependencies = ConversationDependencies(
            provider_factory=lambda *_args, **_kwargs: provider,
            model_budget_resolver=lambda *_args, **_kwargs: {
                "input_budget": 3000, "output_reserve": 256, "safety_margin": 128,
                "context_window": 4096, "estimate_kind": "estimated",
            },
            agent_executor=executor,
        )

        result = ConversationOrchestrator(dependencies).run(
            ConversationRequest("Use the synthetic calculator and explain the returned value.",
                                "stateless", "llama-server", mode="auto"),
            index_store=None, session=ConversationSession(provider=provider),
        )

        self.assertEqual("completed", result.execution_status, result.to_payload())
        self.assertEqual(["called"], tool_calls)
        self.assertTrue(result.retrieval["agent_execution"]["tool_call_count"])
        self.assertEqual("general", result.retrieval["mode"])
        self.assertEqual('{"value":42}',
                         provider.executed_tool_answer_context.tool_observations[0]["output_preview"])

    def test_general_tool_failure_uses_re_discovered_fallback_through_orchestrator(self):
        class FallbackProvider(_Provider):
            def __init__(self):
                super().__init__()
                self.execution_payloads = []

            def plan_completion(self, messages, **_kwargs):
                payload = json.loads(messages[-1]["content"])
                if "requested_mode" in payload:
                    return json.dumps({
                        "intent": "general",
                        "tasks": [{"task_id": "retrieve", "task_type": "general",
                                   "question": "Read the requested synthetic value.",
                                   "requires_retrieval": False, "search_queries": [],
                                   "dependencies": []},
                                  {"task_id": "read", "task_type": "tool",
                                   "question": "Try the available primary read tool.",
                                   "requires_retrieval": False, "search_queries": [],
                                   "dependencies": ["retrieve"]}],
                        "requires_repository": False, "requires_external_data": False,
                        "requires_agent_execution": True,
                        "answer_strategy": "direct", "clarification_required": False,
                        "confidence": 0.99, "reason": "A bounded synthetic read is required.",
                    })
                self.execution_payloads.append(payload)
                requested = set(payload["requested_schemas"])
                observations = payload["observations"]
                if not observations and "primary_synthetic_read" not in requested:
                    return json.dumps({"action": "load_schemas",
                                       "tool_ids": ["primary_synthetic_read"]})
                if not observations:
                    return json.dumps({"action": "plan", "calls": [{
                        "call_id": "primary-call", "tool_id": "primary_synthetic_read",
                        "arguments": {}, "dependencies": [], "purpose": "Try the primary source.",
                        "expected_output": "A primary synthetic result.",
                    }]})
                if any(item.get("tool_id") == "fallback_synthetic_read"
                       and item.get("status") == "completed" for item in observations):
                    return json.dumps({"action": "finish"})
                failed_primary = any(
                    item.get("tool_id") == "primary_synthetic_read"
                    and item.get("error_code") == "TOOL_UNAVAILABLE"
                    for item in observations)
                recovery_selection_rejected = any(
                    item.get("tool_id") == "planner_validation"
                    and item.get("error_code") == "TOOL_NOT_FOUND"
                    for item in observations)
                if failed_primary and not recovery_selection_rejected:
                    return json.dumps({"action": "plan", "calls": [{
                        "call_id": "unknown-recovery-call", "tool_id": "compute_total",
                        "arguments": {}, "dependencies": [],
                        "purpose": "Attempt an invalid recovery selection.",
                        "expected_output": "This tool is not in the authorized catalog.",
                    }]})
                if failed_primary and "fallback_synthetic_read" not in requested:
                    return json.dumps({"action": "load_schemas",
                                       "tool_ids": ["fallback_synthetic_read"]})
                if failed_primary:
                    return json.dumps({"action": "plan", "calls": [{
                        "call_id": "fallback-call", "tool_id": "fallback_synthetic_read",
                        "arguments": {}, "dependencies": [], "purpose": "Use the available fallback.",
                        "expected_output": "A recovered synthetic result.",
                    }]})
                return json.dumps({"action": "finish"})

            def answer_general(self, context, **_kwargs):
                self.answer_context = context
                return Answer("The fallback source supplied the result.", (), False)

        provider = FallbackProvider()
        fallback_runs = []

        def registry_factory(_toolbox):
            from agentforge.agent_execution import ToolDefinition, ToolExecutionFailure
            from agentforge.agent_execution import ToolErrorCode, ToolRegistry
            empty_schema = {"type": "object", "properties": {}, "required": [],
                            "additionalProperties": False}
            registry = ToolRegistry()
            for tool_id, description, handler in (
                ("primary_synthetic_read", "Read primary synthetic data.",
                 lambda _args, _context: (_ for _ in ()).throw(
                     ToolExecutionFailure(ToolErrorCode.TOOL_UNAVAILABLE))),
                ("fallback_synthetic_read", "Read fallback synthetic data after primary failure.",
                 lambda _args, _context: fallback_runs.append(True) or {"value": "recovered"}),
            ):
                registry.register(ToolDefinition(
                    tool_id=tool_id, name=tool_id.replace("_", " "), description=description,
                    version="1", capabilities=frozenset({"synthetic.read"}),
                    input_schema=empty_schema, output_schema={"type": "object"},
                    permission_scope="synthetic:read",
                ), handler)
            return registry

        from agentforge.agent_execution import ToolAuthorization
        executor = ConversationAgentExecutor(
            tool_registry_factory=registry_factory,
            authorization_factory=lambda _request, _repo: ToolAuthorization(
                permission_scopes=frozenset({"synthetic:read"})),
        )
        dependencies = ConversationDependencies(
            provider_factory=lambda *_args, **_kwargs: provider,
            model_budget_resolver=lambda *_args, **_kwargs: {
                "input_budget": 3000, "output_reserve": 256, "safety_margin": 128,
                "context_window": 4096, "estimate_kind": "estimated",
            },
            agent_executor=executor,
        )

        result = ConversationOrchestrator(dependencies).run(
            ConversationRequest("Try primary_synthetic_read, then use fallback_synthetic_read if unavailable.",
                                "stateless", "llama-server", mode="auto"),
            index_store=None, session=ConversationSession(provider=provider),
        )

        self.assertEqual("completed", result.execution_status, result.to_payload())
        self.assertEqual([True], fallback_runs)
        self.assertEqual("general", result.retrieval["mode"])
        catalog_rows = [[tool["tool_id"] for tool in item.get("available_tools", [])]
                        for item in provider.execution_payloads]
        self.assertTrue(any("fallback_synthetic_read" in row for row in catalog_rows))
        correction_observations = [
            item for payload in provider.execution_payloads
            for item in payload["observations"]
            if item.get("tool_id") == "planner_validation"
        ]
        self.assertTrue(correction_observations)
        self.assertIn("listed tool_id", correction_observations[0]["output"]["message"])
        self.assertTrue(any(item["tool_id"] == "fallback_synthetic_read"
                            and item["status"] == "completed"
                            for item in provider.answer_context.tool_observations))

    def test_hybrid_intent_can_combine_tool_execution_and_repository_evidence(self):
        self.write("src/ops.py", "def compute_total():\n    return 42\n")

        class HybridProvider(_Provider):
            def __init__(self):
                super().__init__()
                self.hybrid_context = None

            def plan_completion(self, messages, **_kwargs):
                payload = json.loads(messages[-1]["content"])
                if "requested_schemas" not in payload:
                    raise AssertionError("The turn should enter only the Agent planner.")
                if "synthetic_read" not in payload["requested_schemas"]:
                    return json.dumps({"action": "load_schemas", "tool_ids": ["synthetic_read"]})
                if not payload["observations"]:
                    return json.dumps({"action": "plan", "calls": [{
                        "call_id": "hybrid-read", "tool_id": "synthetic_read",
                        "arguments": {}, "dependencies": [], "purpose": "Read synthetic calculation.",
                        "expected_output": "A synthetic calculation value.",
                    }]})
                return json.dumps({"action": "finish"})

            def answer_hybrid(self, context, **_kwargs):
                self.hybrid_context = context
                source_id = context.evidence[0].source_id
                return Answer(
                    "General knowledge:\nThe tool returned a synthetic value.\n\n"
                    f"Repository evidence:\n`compute_total` returns 42. [{source_id}]",
                    (context.evidence[0].citation,), False,
                )

        provider = HybridProvider()

        def registry_factory(toolbox):
            self.assertIsNotNone(toolbox)
            from agentforge.agent_execution import ToolDefinition, ToolRegistry
            registry = ToolRegistry()
            registry.register(ToolDefinition(
                tool_id="synthetic_read", name="Synthetic reader",
                description="Read a synthetic tool value.", version="1",
                capabilities=frozenset({"synthetic.read"}),
                input_schema={"type": "object", "properties": {}, "required": [],
                              "additionalProperties": False},
                output_schema={"type": "object"}, permission_scope="synthetic:read",
            ), lambda _args, _context: {"value": "tool-observation"})
            return registry

        from agentforge.agent_execution import ToolAuthorization
        from agentforge.dialogue_router import DialogueMode, DialoguePlan
        from agentforge.planner import ConversationTask
        executor = ConversationAgentExecutor(
            tool_registry_factory=registry_factory,
            authorization_factory=lambda _request, _repo: ToolAuthorization(
                permission_scopes=frozenset({"synthetic:read"})),
        )
        dependencies = ConversationDependencies(
            provider_factory=lambda *_args, **_kwargs: provider,
            model_budget_resolver=lambda *_args, **_kwargs: {
                "input_budget": 3000, "output_reserve": 256, "safety_margin": 128,
                "context_window": 4096, "estimate_kind": "estimated",
            },
            agent_executor=executor,
        )
        plan = DialoguePlan(
            mode=DialogueMode.HYBRID, use_repository=True, use_general_knowledge=True,
            reason="The answer requires a general component and current source evidence.",
            tasks=(
                ConversationTask("general", "general", "Explain the tool result.",
                                 False, (), ()),
                ConversationTask("source", "repository", "Inspect compute_total.",
                                 True, ("compute_total",), ("general",)),
            ),
            requires_agent_execution=True, answer_strategy="compose", planner_source="llm",
        )
        with IndexStore(":memory:") as store:
            index_repository(self.root, store)
            result = ConversationOrchestrator(dependencies).run(
                ConversationRequest("Explain the tool result and cite compute_total.",
                                    str(self.root), "llama-server", mode="auto"),
                index_store=store,
                session=ConversationSession(provider=provider, repository_id="synthetic-repo"),
                planned_plan=plan,
            )

        self.assertEqual("completed", result.execution_status, result.to_payload())
        self.assertEqual("hybrid", result.retrieval["mode"])
        self.assertIsNotNone(provider.hybrid_context)
        self.assertIn("tool-observation", provider.hybrid_context.tool_observations[0]["output_preview"])
        self.assertIn("src/ops.py", result.contract.evidence[0].citation)

    def test_unified_general_tool_execution_pauses_for_approval(self):
        from agentforge.agent_execution import ToolAuthorization, ToolDefinition, ToolRegistry

        provider = _SingleToolProvider("approved_synthetic_action")
        invoked = []

        def registry_factory(_toolbox):
            registry = ToolRegistry()
            registry.register(ToolDefinition(
                tool_id="approved_synthetic_action", name="Approved synthetic action",
                description="Perform a reversible in-memory action after approval.", version="1",
                capabilities=frozenset({"synthetic.action"}),
                input_schema={"type": "object", "properties": {}, "required": [],
                              "additionalProperties": False},
                output_schema={"type": "object"}, permission_scope="synthetic:action",
                side_effect=True, idempotent=False, requires_approval=True,
                parallel_safe=False,
            ), lambda _args, _context: invoked.append(True) or {"completed": True})
            return registry

        executor = ConversationAgentExecutor(
            tool_registry_factory=registry_factory,
            authorization_factory=lambda _request, _repo: ToolAuthorization(
                permission_scopes=frozenset({"synthetic:action"})),
        )
        dependencies = ConversationDependencies(
            provider_factory=lambda *_args, **_kwargs: provider,
            model_budget_resolver=lambda *_args, **_kwargs: {
                "input_budget": 3000, "output_reserve": 256, "safety_margin": 128,
                "context_window": 1_048_576, "estimate_kind": "estimated",
            },
            agent_executor=executor,
        )
        result = ConversationOrchestrator(dependencies).run(
            ConversationRequest("Use approved_synthetic_action.", "stateless", "llama-server"),
            index_store=None, session=ConversationSession(provider=provider),
            planned_plan=_general_agent_dialogue_plan(),
        )

        self.assertEqual("waiting_approval", result.execution_status)
        self.assertIsNotNone(result.agent_execution["pending_approval"])
        self.assertEqual([], invoked)

    def test_unified_general_long_tool_result_passes_bounded_preview_and_reference(self):
        from agentforge.agent_execution import ToolAuthorization, ToolDefinition, ToolRegistry

        provider = _SingleToolProvider("read_large_synthetic_report")
        # The long-result acceptance is specifically for the larger verified
        # DeepSeek context profile; this remains an offline budget test.
        from agentforge.model_registry import parse_deepseek_models
        from tests.test_multimodel import discovery_payload
        provider.descriptor = parse_deepseek_models(discovery_payload())[0]

        def registry_factory(_toolbox):
            registry = ToolRegistry()
            registry.register(ToolDefinition(
                tool_id="read_large_synthetic_report", name="Read synthetic report",
                description="Read a long synthetic report.", version="1",
                capabilities=frozenset({"synthetic.read"}),
                input_schema={"type": "object", "properties": {}, "required": [],
                              "additionalProperties": False},
                output_schema={"type": "object"}, permission_scope="synthetic:read",
            ), lambda _args, _context: {"report": "synthetic-content-" + "x" * 24_000})
            return registry

        executor = ConversationAgentExecutor(
            tool_registry_factory=registry_factory,
            authorization_factory=lambda _request, _repo: ToolAuthorization(
                permission_scopes=frozenset({"synthetic:read"})),
        )
        dependencies = ConversationDependencies(
            provider_factory=lambda *_args, **_kwargs: provider,
            model_budget_resolver=lambda *_args, **_kwargs: {
                "input_budget": 3000, "output_reserve": 256, "safety_margin": 128,
                "context_window": 1_048_576, "estimate_kind": "estimated",
            },
            agent_executor=executor,
        )
        result = ConversationOrchestrator(dependencies).run(
            ConversationRequest("Read the synthetic report.", "stateless", "llama-server"),
            index_store=None, session=ConversationSession(provider=provider),
            planned_plan=_general_agent_dialogue_plan(),
        )

        self.assertEqual("completed", result.execution_status, result.to_payload())
        observation = provider.answer_context.tool_observations[0]
        self.assertTrue(observation["truncated"])
        self.assertTrue(observation["result_ref"].startswith("agent-result:"))
        self.assertLessEqual(len(observation["output_preview"]), 1_200)

    def test_unified_general_tool_cancellation_stops_without_answer_generation(self):
        from agentforge.agent_execution import ToolAuthorization, ToolDefinition, ToolRegistry

        provider = _SingleToolProvider("wait_for_cancel")
        started = threading.Event()
        cancel = threading.Event()

        def wait_for_cancel(_args, context):
            started.set()
            while not context.cancel_event.is_set():
                threading.Event().wait(0.005)
            return {"cancelled": True}

        def registry_factory(_toolbox):
            registry = ToolRegistry()
            registry.register(ToolDefinition(
                tool_id="wait_for_cancel", name="Wait for cancellation",
                description="Wait for host cancellation in synthetic memory.", version="1",
                capabilities=frozenset({"synthetic.wait"}),
                input_schema={"type": "object", "properties": {}, "required": [],
                              "additionalProperties": False},
                output_schema={"type": "object"}, permission_scope="synthetic:read",
                timeout=5.0,
            ), wait_for_cancel)
            return registry

        executor = ConversationAgentExecutor(
            tool_registry_factory=registry_factory,
            authorization_factory=lambda _request, _repo: ToolAuthorization(
                permission_scopes=frozenset({"synthetic:read"})),
        )
        dependencies = ConversationDependencies(
            provider_factory=lambda *_args, **_kwargs: provider,
            model_budget_resolver=lambda *_args, **_kwargs: {"input_budget": 3000},
            agent_executor=executor,
        )
        orchestrator = ConversationOrchestrator(dependencies)
        request = ConversationRequest("Wait for host cancellation.", "stateless", "llama-server")
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                orchestrator.run, request, index_store=None,
                session=ConversationSession(provider=provider),
                planned_plan=_general_agent_dialogue_plan(), cancel_event=cancel,
            )
            self.assertTrue(started.wait(5), "The unified entry never started its registered tool.")
            cancel.set()
            result = future.result(timeout=10)

        self.assertEqual("cancelled", result.execution_status)
        self.assertEqual(1, result.agent_execution["tool_call_count"])
        self.assertIsNone(provider.answer_context)

    def test_unified_general_tool_timeout_is_reported_without_answer_generation(self):
        from agentforge.agent_execution import ToolAuthorization, ToolDefinition, ToolRegistry

        provider = _SingleToolProvider("slow_synthetic_read")

        def registry_factory(_toolbox):
            registry = ToolRegistry()
            registry.register(ToolDefinition(
                tool_id="slow_synthetic_read", name="Slow synthetic read",
                description="Read synthetic data with a bounded short deadline.", version="1",
                capabilities=frozenset({"synthetic.read"}),
                input_schema={"type": "object", "properties": {}, "required": [],
                              "additionalProperties": False},
                output_schema={"type": "object"}, permission_scope="synthetic:read",
                timeout=0.03,
            ), lambda _args, _context: (threading.Event().wait(0.1) or {"late": True}))
            return registry

        executor = ConversationAgentExecutor(
            tool_registry_factory=registry_factory,
            authorization_factory=lambda _request, _repo: ToolAuthorization(
                permission_scopes=frozenset({"synthetic:read"})),
        )
        dependencies = ConversationDependencies(
            provider_factory=lambda *_args, **_kwargs: provider,
            model_budget_resolver=lambda *_args, **_kwargs: {"input_budget": 3000},
            agent_executor=executor,
        )
        result = ConversationOrchestrator(dependencies).run(
            ConversationRequest("Read the slow synthetic source.", "stateless", "llama-server"),
            index_store=None, session=ConversationSession(provider=provider),
            planned_plan=_general_agent_dialogue_plan(),
        )

        self.assertEqual("tool_timeout", result.execution_status)
        self.assertEqual("TIMEOUT", result.agent_execution["error_code"])
        self.assertIsNone(provider.answer_context)

    def test_auto_plan_runs_real_execution_engine_then_uses_answer_contract(self):
        self.write("src/ops.py", "def compute_total():\n    return 'synthetic-agent-value'\n")
        provider = _Provider()
        events = []
        budget = {"input_budget": 3200, "output_reserve": 256, "safety_margin": 128,
                  "context_window": 4096, "estimate_kind": "estimated"}
        dependencies = ConversationDependencies(
            provider_factory=lambda *_args, **_kwargs: provider,
            model_budget_resolver=lambda *_args, **_kwargs: dict(budget),
            agent_executor=ConversationAgentExecutor(),
        )
        with IndexStore(":memory:") as store:
            index_repository(self.root, store)
            result = ConversationOrchestrator(dependencies).run(
            ConversationRequest("Inspect compute_total using the available tools.",
                                    str(self.root), "llama-server", mode="auto"),
                index_store=store,
                session=ConversationSession(provider=provider, repository_id="synthetic-repo"),
                on_event=events.append,
            )

        self.assertEqual("completed", result.execution_status, result.to_payload())
        self.assertTrue(result.retrieval["requires_agent_execution"])
        self.assertEqual(1, result.agent_execution["tool_call_count"])
        self.assertGreaterEqual(len(provider.planner_payloads), 3)
        self.assertTrue(provider.planner_payloads[-1]["observations"])
        agent_budgets = [tokens for is_dialogue, tokens in provider.planner_output_budgets
                         if not is_dialogue]
        self.assertTrue(agent_budgets)
        self.assertEqual([384] * len(agent_budgets), agent_budgets)
        self.assertIsNotNone(provider.answer_context)
        self.assertIn("synthetic-agent-value", provider.answer_context.evidence[0].result.text)
        self.assertEqual("limited", result.contract.status)
        self.assertIn("TOOL_CALL_COMPLETED", [event.type.value for event in events])
        self.assertIn("OBSERVATION_RECEIVED", [event.type.value for event in events])
        self.assertIn("REPLANNING", [event.type.value for event in events])
        self.assertEqual("COMPLETED", events[-1].type.value)

    def test_repository_preparation_refreshes_index_before_agent_tool_execution(self):
        source = self.write("src/ops.py", "def compute_total():\n    return 'before-refresh'\n")
        provider = _Provider()
        preparations = []

        def prepare(store, request, vector_retriever):
            source.write_text(
                "def compute_total():\n    return 'refreshed-before-tool-call'\n",
                encoding="utf-8",
            )
            from agentforge.scanner import scan_repository
            report = scan_repository(request.repository)
            index_repository(request.repository, store, scan_report=report)
            preparations.append(True)
            return {"scan_report": report, "vector_retriever": vector_retriever}

        dependencies = ConversationDependencies(
            provider_factory=lambda *_args, **_kwargs: provider,
            model_budget_resolver=lambda *_args, **_kwargs: {
                "input_budget": 3200, "output_reserve": 256,
                "safety_margin": 128, "context_window": 4096,
                "estimate_kind": "estimated",
            },
            agent_executor=ConversationAgentExecutor(),
            repository_preparer=prepare,
        )
        with IndexStore(":memory:") as store:
            index_repository(self.root, store)
            result = ConversationOrchestrator(dependencies).run(
                ConversationRequest("Inspect compute_total using the available tools.",
                                    str(self.root), "llama-server", mode="auto"),
                index_store=store,
                session=ConversationSession(provider=provider, repository_id="synthetic-repo"),
            )

        self.assertEqual("completed", result.execution_status, result.to_payload())
        self.assertEqual([True], preparations)
        self.assertIn("refreshed-before-tool-call", provider.answer_context.evidence[0].result.text)

    def test_short_tool_timeout_does_not_shorten_agent_model_planner_timeout(self):
        from agentforge.agent_execution import LLMExecutionPlanner

        self.write("src/ops.py", "def compute_total():\n    return 'synthetic-agent-value'\n")
        provider = _Provider()
        dependencies = ConversationDependencies(
            provider_factory=lambda *_args, **_kwargs: provider,
            model_budget_resolver=lambda *_args, **_kwargs: {
                "input_budget": 3200, "output_reserve": 256, "safety_margin": 128,
                "context_window": 4096, "estimate_kind": "estimated",
            },
            agent_executor=ConversationAgentExecutor(tool_timeout_seconds=5.0),
        )
        with IndexStore(":memory:") as store:
            index_repository(self.root, store)
            result = ConversationOrchestrator(dependencies).run(
                ConversationRequest("Inspect compute_total using the available tools.",
                                    str(self.root), "llama-server", mode="auto"),
                index_store=store,
                session=ConversationSession(provider=provider, repository_id="synthetic-repo"),
            )

        self.assertEqual("completed", result.execution_status, result.to_payload())
        agent_timeouts = [timeout for is_dialogue_planner, timeout
                          in provider.planner_request_timeouts if not is_dialogue_planner]
        self.assertTrue(agent_timeouts)
        self.assertEqual([LLMExecutionPlanner.TIMEOUT_SECONDS] * len(agent_timeouts),
                         agent_timeouts)

    def test_agent_plan_without_service_fails_closed_before_retrieval(self):
        from unittest.mock import Mock
        from agentforge.planner import PlanningError

        retriever = Mock(side_effect=AssertionError("No direct fallback for an Agent plan."))
        dependencies = ConversationDependencies(
            provider_factory=lambda *_a, **_k: _Provider(),
            model_budget_resolver=lambda *_a, **_k: {"input_budget": 3000},
            retriever_factory=retriever,
        )
        with self.assertRaisesRegex(PlanningError, "no Agent execution service"):
            ConversationOrchestrator(dependencies).run(
                ConversationRequest("inspect", "synthetic", "llama-server", mode="auto"),
                index_store=object(), session=ConversationSession(provider=_Provider()),
            )
        retriever.assert_not_called()

    def test_orchestrator_agent_route_uses_injected_registry_through_existing_engine(self):
        from agentforge.agent_execution import ToolAuthorization, ToolDefinition, ToolRegistry

        self.write("src/ops.py", "def compute_total():\n    return 'synthetic-agent-value'\n")
        provider = _Provider()
        calls = []

        def registry_factory(_toolbox):
            registry = ToolRegistry()
            registry.register(ToolDefinition(
                tool_id="search", name="Synthetic search", description="Search the synthetic index.",
                version="1", capabilities=frozenset({"synthetic"}),
                input_schema={"type": "object", "properties": {
                    "query": {"type": "string"}, "top_k": {"type": "integer"}},
                    "required": ["query", "top_k"], "additionalProperties": False},
                output_schema={"type": "object"}, permission_scope="synthetic:read",
            ), lambda arguments, _context: calls.append(arguments) or {"accepted": True})
            return registry

        executor = ConversationAgentExecutor(
            tool_registry_factory=registry_factory,
            authorization_factory=lambda _request, _repo: ToolAuthorization(
                permission_scopes=frozenset({"synthetic:read"})),
        )
        dependencies = ConversationDependencies(
            provider_factory=lambda *_args, **_kwargs: provider,
            model_budget_resolver=lambda *_args, **_kwargs: {
                "input_budget": 3000, "output_reserve": 256, "safety_margin": 128,
                "context_window": 4096, "estimate_kind": "estimated",
            },
            agent_executor=executor,
        )
        with IndexStore(":memory:") as store:
            index_repository(self.root, store)
            result = ConversationOrchestrator(dependencies).run(
                ConversationRequest("Inspect compute_total using the available tools.",
                                    str(self.root), "llama-server", mode="auto"),
                index_store=store,
                session=ConversationSession(provider=provider, repository_id="synthetic-repo"),
            )

        self.assertEqual("completed", result.execution_status, result.to_payload())
        self.assertEqual(1, len(calls))
        self.assertEqual(1, result.retrieval["agent_execution"]["tool_call_count"])
        self.assertEqual("limited", result.contract.status)

    def test_explicit_execution_resume_is_routed_without_replanning_the_outer_dialogue(self):
        provider = _Provider()
        dependencies = ConversationDependencies(
            provider_factory=lambda *_args, **_kwargs: provider,
            model_budget_resolver=lambda *_args, **_kwargs: {"input_budget": 3000},
        )
        request = ConversationRequest(
            "Resume the saved synthetic task.", str(self.root), "llama-server",
            conversation_id="conversation-id", execution_id="execution-id",
        )
        plan = ConversationOrchestrator(dependencies).plan(request)

        self.assertTrue(plan.requires_agent_execution)
        self.assertEqual("execution_resume", plan.planner_source)
        self.assertEqual("repository", plan.mode.value)
        self.assertEqual("execution-id", request.execution_id)
        self.assertEqual([], provider.planner_payloads)

    def test_stream_failure_reports_semantic_execution_status_not_terminal_engine_state(self):
        from types import SimpleNamespace
        from agentforge.dialogue_router import DialogueMode, DialoguePlan
        from agentforge.planner import ConversationTask

        provider = _Provider()
        self.write("src/compute_total.py", "def compute_total():\n    return 1\n")
        report = SimpleNamespace(
            execution_id="synthetic-timeout-execution",
            status="tool_timeout",
            execution_state="COMPLETED",
            execution_rounds=1,
            execution_results=({"error_code": "TIMEOUT"},),
            pending_approval=None,
            user_input_request=None,
            planner_request_diagnostics=(),
            retrieval={},
        )

        class TimedOutExecutor:
            def execute(self, *_args, **_kwargs):
                return SimpleNamespace(report=report, provider=provider)

        plan = DialoguePlan(
            mode=DialogueMode.REPOSITORY,
            use_repository=True,
            use_general_knowledge=False,
            reason="The bounded synthetic tool timed out.",
            tasks=(ConversationTask("inspect", "repository", "Inspect synthetic source.",
                                    True, ("compute_total",), ()),),
            requires_agent_execution=True,
            answer_strategy="evidence_based",
            confidence=0.9,
        )
        dependencies = ConversationDependencies(
            provider_factory=lambda *_args, **_kwargs: provider,
            model_budget_resolver=lambda *_args, **_kwargs: {"input_budget": 3000},
            agent_executor=TimedOutExecutor(),
        )
        events = []
        with IndexStore(":memory:") as store:
            index_repository(self.root, store)
            result = ConversationOrchestrator(dependencies).run(
                ConversationRequest("Inspect synthetic source.", str(self.root),
                                    "llama-server", mode="auto"),
                index_store=store,
                session=ConversationSession(provider=provider, repository_id="synthetic-repo"),
                on_event=events.append,
                planned_plan=plan,
            )

        self.assertEqual("tool_timeout", result.execution_status)
        self.assertEqual("TIMEOUT", result.agent_execution["error_code"])
        self.assertEqual("tool_execution", result.agent_execution["failure_phase"])
        failed = [event for event in events if event.type.value == "FAILED"]
        self.assertEqual(1, len(failed))
        self.assertEqual("tool_timeout", failed[0].payload["error_code"])

    def test_agent_failure_metadata_uses_last_planner_diagnostic_code(self):
        from types import SimpleNamespace
        from agentforge.conversation_orchestrator import _agent_failure_metadata

        report = SimpleNamespace(error_code=None, failure_phase=None, execution_results=())
        code, phase = _agent_failure_metadata(report, ({
            "planning_failure_phase": "plan_validation",
            "planning_failure_code": "schema_round_limit",
        },))
        self.assertEqual(("schema_round_limit", "plan_validation"), (code, phase))

    def test_agent_failure_metadata_uses_plan_validation_phase_for_duplicate_call(self):
        from types import SimpleNamespace
        from agentforge.conversation_orchestrator import _agent_failure_metadata

        report = SimpleNamespace(error_code=None, failure_phase=None, execution_results=(
            {"error_code": "DUPLICATE_CALL",
             "execution_metadata": {"stage": "plan_validation"}},
        ))
        self.assertEqual(("DUPLICATE_CALL", "plan_validation"),
                         _agent_failure_metadata(report, ()))
