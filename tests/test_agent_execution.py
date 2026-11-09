"""离线验证通用 Agent 执行协议、恢复边界与旧 AgentLoop 适配。"""
from concurrent.futures import ThreadPoolExecutor
import ast
import json
from dataclasses import replace
from pathlib import Path
import threading
import time
import unittest
from uuid import uuid4

from agentforge.agent_execution import (
    AgentExecutionEngine, AgentExecutionPlan, ExecutionSnapshot, ExecutionStatus,
    ExecutionCheckpointConflict, InMemoryExecutionStateStore, InMemoryToolResultStore, LLMExecutionPlanner,
    PostgresExecutionStateStore, PostgresToolResultStore, ToolAuthorization,
    RESULT_PAGE_TOOL_ID, RESULT_PAGE_MAX_CHARS,
    PlanValidationError, ToolCallRequest, ToolDefinition, ToolErrorCode, ToolExecutionFailure,
    ToolRegistry, ToolResult, ToolDiscovery, _snapshot_from_dict,
    _planner_payload, _parse_planner_action,
)
from agentforge.model_types import LOCAL_QWEN, ModelDescriptor
from agentforge.errors import ProviderError


def definition(tool_id="lookup", *, handler_schema=None, output_schema=None,
               side_effect=False, requires_approval=False, risk="low", timeout=1.0,
               retries=0, concurrency_group=None, parallel_safe=True, worker_safe=True):
    schema = handler_schema or {"type": "object", "properties": {},
                                "required": [], "additionalProperties": False}
    return ToolDefinition(
        tool_id=tool_id, name=f"{tool_id} lookup", description=f"Look up {tool_id} data",
        version="1", capabilities=frozenset({f"test.{tool_id}", "test.lookup"}),
        input_schema=schema, output_schema=output_schema or {"type": "object"},
        permission_scope="test:read", risk_level=risk, side_effect=side_effect,
        idempotent=not side_effect, timeout=timeout,
        concurrency_group=concurrency_group or tool_id,
        requires_approval=requires_approval, max_retries=retries,
        parallel_safe=parallel_safe, worker_safe=worker_safe,
    )


def call(tool_id="lookup", *, arguments=None, task_id="task-1", call_id="call-1", dependencies=()):
    return ToolCallRequest(call_id, task_id, tool_id, arguments or {},
                           "synthetic test purpose", "synthetic JSON result", tuple(dependencies))


class QueuePlanner:
    def __init__(self, *plans):
        self.plans = list(plans)
        self.seen_observations = []

    def plan(self, objective, observations, catalog, schema_loader):
        self.seen_observations.append(tuple(observations))
        if not self.plans:
            return AgentExecutionPlan(done=True, final_answer="complete")
        return self.plans.pop(0)


def engine_for(registry, planner, **kwargs):
    return AgentExecutionEngine(registry, planner,
        authorization=kwargs.pop("authorization", ToolAuthorization(
            permission_scopes=frozenset({"test:read"}))), **kwargs)


class AgentExecutionTests(unittest.TestCase):
    def test_core_execution_engine_does_not_import_legacy_loop(self):
        source = Path(__file__).parents[1] / "agentforge" / "agent_execution.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        imports_legacy_loop = any(
            isinstance(node, ast.ImportFrom) and node.module == "agent_loop"
            or isinstance(node, ast.Import) and any(
                alias.name == "agentforge.agent_loop" for alias in node.names)
            for node in ast.walk(tree)
        )
        self.assertFalse(imports_legacy_loop)

    def test_planner_output_budget_uses_verified_caps_not_chat_answer_budget(self):
        class Provider:
            descriptor = LOCAL_QWEN
            max_output_tokens = 256
            def plan_completion(self, *_args, **_kwargs):
                return "{}"

        self.assertEqual(384, LLMExecutionPlanner.output_tokens_for(Provider()))
        self.assertEqual(384, LLMExecutionPlanner(Provider()).max_output_tokens)

        class CappedProvider:
            max_output_tokens = 128
            descriptor = ModelDescriptor(
                provider_id="deepseek", model_id="deepseek-synthetic",
                display_name="Synthetic model", model_type="cloud",
                context_window=1_048_576, max_output_tokens=393_216,
                provider_max_output_tokens=393_216, adapter_max_output_tokens=4096,
            )
            def plan_completion(self, *_args, **_kwargs):
                return "{}"

        self.assertEqual(4096, LLMExecutionPlanner.output_tokens_for(CappedProvider()))
        self.assertEqual(4096, LLMExecutionPlanner(CappedProvider()).max_output_tokens)

        class UnknownProvider:
            descriptor = ModelDescriptor(
                provider_id="unknown", model_id="unknown", display_name="Unknown",
                model_type="cloud", context_window=None, max_output_tokens=None,
            )

        with self.assertRaisesRegex(ValueError, "verified model context window"):
            LLMExecutionPlanner.output_tokens_for(UnknownProvider())

    def test_observation_drives_next_plan_and_shared_engine_finishes(self):
        registry = ToolRegistry()
        registry.register(definition(), lambda _args, _ctx: {"answer": "from tool"})
        planner = QueuePlanner(AgentExecutionPlan(calls=(call(),)),
                               AgentExecutionPlan(done=True, final_answer="used observation"))
        report = engine_for(registry, planner).run("look this up")
        self.assertEqual(report.status, "completed")
        self.assertEqual(report.rounds, 2)
        self.assertEqual(report.final_answer, "used observation")
        self.assertEqual(planner.seen_observations[0], ())
        self.assertEqual(planner.seen_observations[1][0].output, {"answer": "from tool"})
        self.assertEqual(planner.seen_observations[1][0].request,
                         {"tool_id": "lookup", "arguments": {}})
        payload = json.loads(_planner_payload(
            "look this up", planner.seen_observations[1], ToolDiscovery(registry).discover("lookup"),
            {}, tools_only=True,
        ))
        self.assertEqual(payload["observations"][0]["request"],
                         {"tool_id": "lookup", "arguments": {}})

    def test_registry_snapshot_is_immutable_and_schema_cannot_be_mutated_externally(self):
        registry = ToolRegistry()
        registered = definition()
        registry.register(registered, lambda _args, _ctx: {"ok": True})
        engine = engine_for(registry, QueuePlanner(AgentExecutionPlan(calls=(call(),))))
        registry.unregister("lookup")
        report = engine.run("look")
        self.assertEqual(report.results[0].status, "completed")
        with self.assertRaises(RuntimeError):
            engine.registry.register(definition("other"), lambda _args, _ctx: {})
        self.assertIsNotNone(engine.registry.get("lookup"))

    def test_dynamic_discovery_is_bounded_and_catalog_exposes_safety_metadata(self):
        registry = ToolRegistry()
        for index in range(20):
            registry.register(definition(f"tool-{index}"), lambda _args, _ctx: {})
        discovered = ToolDiscovery(registry).discover("no matching capability")
        self.assertLessEqual(len(discovered), ToolDiscovery.MAX_CANDIDATES)
        self.assertTrue(all(item.permission_scope == "test:read" for item in discovered))
        selected = ToolDiscovery(registry).discover("tool-12")
        self.assertEqual(selected[0].tool_id, "tool-12")
        self.assertFalse(selected[0].side_effect)
        self.assertTrue(selected[0].idempotent)
        self.assertTrue(selected[0].parallel_safe)
        self.assertTrue(selected[0].worker_safe)

    def test_discovery_matches_split_tool_ids_and_hides_unauthorized_definitions(self):
        registry = ToolRegistry()
        allowed = replace(definition("fallback_synthetic_read"),
                          permission_scope="synthetic:read")
        forbidden = replace(definition("fallback_synthetic_write"),
                            permission_scope="synthetic:write")
        registry.register(allowed, lambda _args, _ctx: {})
        registry.register(forbidden, lambda _args, _ctx: {})

        catalog = ToolDiscovery(registry).discover(
            "use fallback_synthetic_read after primary failure",
            permission_scopes=frozenset({"synthetic:read"}),
        )

        self.assertEqual(["fallback_synthetic_read"], [item.tool_id for item in catalog])

    def test_execution_planner_catalog_contains_only_authorized_tools(self):
        registry = ToolRegistry()
        registry.register(replace(definition("allowed_lookup"),
                                  permission_scope="synthetic:read"),
                          lambda _args, _ctx: {})
        registry.register(replace(definition("forbidden_lookup"),
                                  permission_scope="synthetic:write"),
                          lambda _args, _ctx: {})

        class CatalogPlanner:
            catalog_ids = ()

            def plan(self, _objective, _observations, catalog, _schema_loader):
                self.catalog_ids = tuple(item.tool_id for item in catalog)
                return AgentExecutionPlan(done=True)

        planner = CatalogPlanner()
        report = AgentExecutionEngine(
            registry, planner,
            authorization=ToolAuthorization(permission_scopes=frozenset({"synthetic:read"})),
        ).run("inspect available tools")

        self.assertEqual("completed", report.status)
        self.assertEqual(("allowed_lookup",), planner.catalog_ids)

    def test_tool_unavailable_replans_with_authorized_fallback_catalog(self):
        registry = ToolRegistry()
        primary = replace(definition("primary_synthetic_read"),
                          permission_scope="synthetic:read")
        fallback = replace(definition("fallback_synthetic_read"),
                           permission_scope="synthetic:read")
        registry.register(primary, lambda _args, _ctx: (_ for _ in ()).throw(
            ToolExecutionFailure(ToolErrorCode.TOOL_UNAVAILABLE)))
        registry.register(fallback, lambda _args, _ctx: {"recovered": True})

        class FallbackPlanner:
            def __init__(self):
                self.catalogs = []
                self.observations = []

            def plan(self, _objective, observations, catalog, _schema_loader):
                self.catalogs.append(tuple(item.tool_id for item in catalog))
                self.observations.append(tuple(observations))
                if not self.observations[-1]:
                    return AgentExecutionPlan(calls=(call(
                        "primary_synthetic_read", call_id="primary-call",
                        task_id="primary-task"),))
                if any(item.tool_id == "fallback_synthetic_read" and item.status == "completed"
                       for item in self.observations[-1]):
                    return AgentExecutionPlan(done=True)
                if self.observations[-1][0].error_code == ToolErrorCode.TOOL_UNAVAILABLE.value:
                    self.asserted_alternative = "fallback_synthetic_read" in self.catalogs[-1]
                    self.asserted_primary_removed = "primary_synthetic_read" not in self.catalogs[-1]
                    return AgentExecutionPlan(calls=(call(
                        "fallback_synthetic_read", call_id="fallback-call",
                        task_id="fallback-task"),))
                return AgentExecutionPlan(done=True)

        planner = FallbackPlanner()
        report = AgentExecutionEngine(
            registry, planner,
            authorization=ToolAuthorization(permission_scopes=frozenset({"synthetic:read"})),
        ).run("read primary_synthetic_read and use fallback_synthetic_read if unavailable")

        self.assertEqual("completed", report.status)
        self.assertTrue(planner.asserted_alternative)
        self.assertTrue(planner.asserted_primary_removed)
        self.assertEqual(["primary_synthetic_read", "fallback_synthetic_read"],
                         [item.tool_id for item in report.results])
        self.assertEqual("completed", report.results[-1].status)

    def test_tool_unavailable_without_authorized_fallback_stops_explicitly(self):
        registry = ToolRegistry()
        primary = replace(definition("primary_synthetic_read"),
                          permission_scope="synthetic:read")
        registry.register(primary, lambda _args, _ctx: (_ for _ in ()).throw(
            ToolExecutionFailure(ToolErrorCode.TOOL_UNAVAILABLE)))
        planner = QueuePlanner(AgentExecutionPlan(calls=(call(
            "primary_synthetic_read", call_id="primary-call",
            task_id="primary-task"),)))

        report = AgentExecutionEngine(
            registry, planner,
            authorization=ToolAuthorization(permission_scopes=frozenset({"synthetic:read"})),
        ).run("read primary_synthetic_read")

        self.assertEqual("failed", report.status)
        self.assertEqual(ToolErrorCode.TOOL_UNAVAILABLE.value, report.error_code)
        self.assertEqual(1, len(planner.seen_observations))

    def test_schema_batches_support_one_five_twenty_and_fifty_registered_tools(self):
        for count in (1, 5, 20, 50):
            with self.subTest(count=count):
                registry = ToolRegistry()
                for index in range(count):
                    tool_id = f"tool-{index:02d}"
                    registry.register(definition(tool_id), lambda _args, _ctx: {})
                discovery = ToolDiscovery(registry)
                catalog = discovery.discover("no matching capability")
                self.assertEqual(min(count, ToolDiscovery.MAX_CANDIDATES), len(catalog))
                registered_ids = [f"tool-{index:02d}" for index in range(count)]
                batch = registry.schemas_for(registered_ids)
                self.assertEqual(set(registered_ids), set(batch))
                self.assertEqual(set(registered_ids), set(registry.schemas_for(registered_ids)))
                batch[registered_ids[0]]["input"]["type"] = "mutated outside registry"
                self.assertEqual("object", registry.schemas_for((registered_ids[0],))
                                 [registered_ids[0]]["input"]["type"])

    def test_schema_cache_invalidates_when_registered_definition_changes(self):
        registry = ToolRegistry()
        original = definition("lookup")
        registry.register(original, lambda _args, _ctx: {})
        discovery = ToolDiscovery(registry)
        self.assertEqual({}, discovery.schema_for("lookup")["input"]["properties"])
        registry.unregister("lookup")
        updated_schema = {"type": "object", "properties": {
            "query": {"type": "string", "minLength": 1}},
            "required": ["query"], "additionalProperties": False}
        registry.register(replace(original, version="2", input_schema=updated_schema),
                          lambda _args, _ctx: {})
        updated = discovery.schema_for("lookup")
        self.assertEqual("query", next(iter(updated["input"]["properties"])))
        with self.assertRaises(KeyError):
            discovery.schemas_for(("lookup", "unknown"))

    def test_input_schema_is_strict_and_output_schema_is_checked(self):
        schema = {"type": "object", "properties": {
            "count": {"type": "integer", "minimum": 1, "maximum": 3}},
            "required": ["count"], "additionalProperties": False}
        registry = ToolRegistry()
        registry.register(definition(handler_schema=schema,
            output_schema={"type": "object", "properties": {"ok": {"type": "boolean"}},
                           "required": ["ok"], "additionalProperties": False}),
            lambda _args, _ctx: {"ok": True})
        bad = call(arguments={"count": 2, "extra": 1})
        invalid_plan = AgentExecutionPlan(calls=(bad,))
        corrected_call = call(arguments={"count": 2, "extra": 1},
                               task_id="task-2", call_id="call-2")
        report = engine_for(registry, QueuePlanner(
            invalid_plan, AgentExecutionPlan(calls=(corrected_call,)))).run("look")
        self.assertEqual(report.status, "failed")
        self.assertEqual(report.error_code, ToolErrorCode.INVALID_ARGUMENTS.value)
        self.assertEqual(report.results[0].execution_metadata["stage"], "plan_validation")

        registry2 = ToolRegistry()
        registry2.register(definition(output_schema={"type": "object", "properties": {
            "ok": {"type": "boolean"}}, "required": ["ok"], "additionalProperties": False}),
            lambda _args, _ctx: {"ok": "not-a-boolean"})
        bad_output = engine_for(registry2, QueuePlanner(
            AgentExecutionPlan(calls=(call(),)), AgentExecutionPlan(done=True))).run("look")
        self.assertEqual(bad_output.results[0].error_code, ToolErrorCode.INVALID_RESULT.value)

    def test_output_persistence_failure_is_internal_and_side_effect_outcome_is_unknown(self):
        class BrokenResultStore:
            def put(self, *_args):
                raise OSError("synthetic storage unavailable")

            def page(self, **_kwargs):
                raise KeyError("missing")

        registry = ToolRegistry()
        registry.register(definition("large"), lambda _a, _c: {"value": "x" * 20_000})
        readonly = engine_for(registry, QueuePlanner(AgentExecutionPlan(calls=(call("large"),))),
                              result_store=BrokenResultStore()).run("large result")
        self.assertEqual(readonly.results[0].status, "failed")
        self.assertEqual(readonly.results[0].error_code, ToolErrorCode.INTERNAL_ERROR.value)

        side_registry = ToolRegistry()
        side_registry.register(definition("large-effect", side_effect=True),
                               lambda _a, _c: {"value": "x" * 20_000})
        side_effect = engine_for(side_registry, QueuePlanner(
            AgentExecutionPlan(calls=(call("large-effect"),))),
            result_store=BrokenResultStore()).run("large effect")
        self.assertEqual(side_effect.results[0].status, "interrupted")
        self.assertEqual(side_effect.results[0].error_code, ToolErrorCode.PARTIAL_RESULT.value)

    def test_run_deadline_bounds_side_effect_timeout(self):
        registry = ToolRegistry()
        release = threading.Event()
        registry.register(definition("slow-effect", side_effect=True, timeout=1.0),
                          lambda _a, _c: release.wait(0.2) or {"ok": True})
        started = time.monotonic()
        report = engine_for(registry, QueuePlanner(AgentExecutionPlan(calls=(call("slow-effect"),))),
                            run_timeout_seconds=0.04).run("bounded side effect")
        elapsed = time.monotonic() - started
        release.set()
        self.assertLess(elapsed, 0.15)
        self.assertEqual(report.status, "failed")
        self.assertEqual(report.results[0].status, "interrupted")
        self.assertEqual(report.results[0].execution_metadata["effect_outcome"], "unknown")

    def test_expired_run_does_not_start_a_planned_tool(self):
        registry = ToolRegistry()
        invoked = []
        registry.register(definition(), lambda _a, _c: invoked.append(True) or {})

        class SlowPlanner(QueuePlanner):
            def plan(self, *args, **kwargs):
                time.sleep(0.03)
                return super().plan(*args, **kwargs)

        report = engine_for(registry, SlowPlanner(AgentExecutionPlan(calls=(call(),))),
                            run_timeout_seconds=0.01).run("slow planning")
        self.assertEqual(report.status, "failed")
        self.assertEqual(report.error_code, ToolErrorCode.TIMEOUT.value)
        self.assertEqual(invoked, [])

    def test_thread_affine_side_effect_stops_cooperatively_and_is_marked_unknown(self):
        registry = ToolRegistry()
        observed = []

        def thread_affine(_arguments, context):
            while not context.cancel_event.is_set():
                time.sleep(0.001)
            observed.append(True)
            return {"ok": True}

        registry.register(definition("thread-affine-effect", side_effect=True, timeout=0.02,
                                     parallel_safe=False, worker_safe=False), thread_affine)
        report = engine_for(registry, QueuePlanner(
            AgentExecutionPlan(calls=(call("thread-affine-effect"),)))).run("thread-affine effect")
        self.assertEqual(report.status, "failed")
        self.assertEqual(report.results[0].status, "interrupted")
        self.assertEqual(report.results[0].error_code, ToolErrorCode.PARTIAL_RESULT.value)
        self.assertTrue(observed)

    def test_non_finite_tool_schema_bounds_and_values_are_rejected(self):
        registry = ToolRegistry()
        with self.assertRaisesRegex(ValueError, "finite"):
            registry.register(definition(handler_schema={"type": "object", "properties": {
                "value": {"type": "number", "minimum": float("nan")}},
                "required": ["value"], "additionalProperties": False}), lambda _a, _c: {})
        with self.assertRaisesRegex(ValueError, "finite JSON"):
            registry.register(definition(handler_schema={"type": "object", "properties": {
                "value": {"type": "number", "enum": [float("nan")]}},
                "required": ["value"], "additionalProperties": False}), lambda _a, _c: {})

        registry.register(definition(handler_schema={"type": "object", "properties": {
            "value": {"type": "number"}}, "required": ["value"], "additionalProperties": False}),
            lambda _a, _c: {})
        report = engine_for(registry, QueuePlanner(AgentExecutionPlan(calls=(
            call(arguments={"value": float("inf")} ),)))).run("reject non-finite")
        self.assertEqual(report.status, "failed")
        self.assertEqual(report.error_code, ToolErrorCode.INVALID_ARGUMENTS.value)

    def test_recursive_and_invalid_definition_schemas_fail_at_registration(self):
        registry = ToolRegistry()
        with self.assertRaises(ValueError):
            registry.register(definition(handler_schema={"type": "object", "properties": {
                "nested": {"type": "object", "properties": {}, "additionalProperties": True}},
                "additionalProperties": False}), lambda _args, _ctx: {})
        with self.assertRaises(ValueError):
            registry.register(definition(handler_schema={"type": "object", "properties": {},
                "required": [{"unhashable": True}], "additionalProperties": False}),
                lambda _args, _ctx: {})

    def test_dag_runs_dependencies_in_order_and_parallelizes_independent_groups(self):
        registry = ToolRegistry()
        gate = threading.Barrier(2)
        active_lock = threading.Lock()
        active, maximum_active = 0, 0

        def parallel_handler(label):
            def run(_args, _ctx):
                nonlocal active, maximum_active
                with active_lock:
                    active += 1
                    maximum_active = max(maximum_active, active)
                gate.wait(timeout=1)
                with active_lock:
                    active -= 1
                return {"label": label}
            return run

        registry.register(definition("first", concurrency_group="first"), parallel_handler("first"))
        registry.register(definition("second", concurrency_group="second"), parallel_handler("second"))
        plan = AgentExecutionPlan(calls=(
            call("first", task_id="first-task", call_id="first-call"),
            call("second", task_id="second-task", call_id="second-call"),
        ))
        report = engine_for(registry, QueuePlanner(plan, AgentExecutionPlan(done=True)),
                            max_concurrency=2).run("run two independent lookups")
        self.assertEqual(report.status, "completed")
        self.assertEqual(maximum_active, 2)

    def test_dag_failure_propagates_and_dependent_tool_is_not_called(self):
        registry = ToolRegistry()
        called = []
        registry.register(definition("first"), lambda _a, _c: (_ for _ in ()).throw(ValueError("no")))
        registry.register(definition("second"), lambda _a, _c: called.append("second") or {})
        plan = AgentExecutionPlan(calls=(
            call("first", task_id="first", call_id="first-call"),
            call("second", task_id="second", call_id="second-call", dependencies=("first",)),
        ))
        report = engine_for(registry, QueuePlanner(plan, AgentExecutionPlan(done=True))).run("run chain")
        self.assertEqual(report.status, "completed")
        self.assertEqual(called, [])
        self.assertEqual([result.status for result in report.results], ["failed", "skipped"])

    def test_cycle_unknown_dependency_and_duplicate_tasks_are_rejected(self):
        registry = ToolRegistry()
        registry.register(definition(), lambda _a, _c: {})
        cases = (
            (call(task_id="a", call_id="a-call", dependencies=("b",)),
             call(task_id="b", call_id="b-call", dependencies=("a",))),
            (call(task_id="a", call_id="a-call", dependencies=("missing",)),),
            (call(task_id="same", call_id="a-call"), call(task_id="same", call_id="b-call")),
        )
        for calls in cases:
            with self.subTest(calls=calls):
                plan = AgentExecutionPlan(calls=calls)
                repeated_task_ids = len({item.task_id for item in calls}) != len(calls)
                planner = QueuePlanner(plan, plan) if repeated_task_ids else QueuePlanner(plan)
                report = engine_for(registry, planner).run("look")
                self.assertEqual(report.status, "failed")
                self.assertEqual(report.results[0].execution_metadata["stage"], "plan_validation")

    def test_duplicate_task_id_emits_a_specific_safe_rejection_code(self):
        registry = ToolRegistry()
        invoked = []
        registry.register(definition(), lambda _args, _ctx: invoked.append(True) or {})
        events = []
        duplicate_plan = AgentExecutionPlan(calls=(
            call(task_id="same", call_id="first-call"),
            call(task_id="same", call_id="second-call"),
        ))

        planner = QueuePlanner(duplicate_plan,
                               AgentExecutionPlan(done=True, final_answer="Result is incomplete."))
        report = engine_for(registry, planner,
                            on_event=events.append).run("run two dependent operations")

        self.assertEqual("completed", report.status)
        self.assertEqual("Result is incomplete.", report.final_answer)
        self.assertEqual(0, report.tool_calls)
        self.assertEqual([], invoked)
        self.assertEqual((), report.results)
        rejection = planner.seen_observations[1][0]
        self.assertEqual("planner_validation", rejection.tool_id)
        self.assertEqual(ToolErrorCode.INVALID_ARGUMENTS.value, rejection.error_code)
        self.assertIn("duplicated", rejection.output["message"])
        rejected = next(item for item in events if item.get("type") == "PLAN_REJECTED")
        self.assertEqual("duplicate_task_ids", rejected["error_code"])

    def test_same_concurrency_group_serializes_calls(self):
        registry = ToolRegistry()
        lock = threading.Lock()
        active = 0
        maximum = 0

        def run(_args, _ctx):
            nonlocal active, maximum
            with lock:
                active += 1; maximum = max(maximum, active)
            time.sleep(0.015)
            with lock:
                active -= 1
            return {}

        for tool in ("one", "two"):
            registry.register(definition(tool, concurrency_group="same-group"), run)
        plan = AgentExecutionPlan(calls=(call("one", task_id="one", call_id="one-call"),
                                        call("two", task_id="two", call_id="two-call")))
        report = engine_for(registry, QueuePlanner(plan, AgentExecutionPlan(done=True)),
                            max_concurrency=2).run("run one and two")
        self.assertEqual(report.status, "completed")
        self.assertEqual(maximum, 1)

    def test_thread_affine_tool_runs_on_caller_thread(self):
        caller = threading.get_ident()
        tool_threads = []
        registry = ToolRegistry()
        registry.register(definition("inline", parallel_safe=False, worker_safe=False),
            lambda _args, _context: tool_threads.append(threading.get_ident()) or {})
        report = engine_for(registry, QueuePlanner(AgentExecutionPlan(calls=(call("inline"),)),
            AgentExecutionPlan(done=True))).run("inline tool")
        self.assertEqual(report.status, "completed")
        self.assertEqual(tool_threads, [caller])

    def test_side_effect_is_approved_and_approval_is_bound_to_definition(self):
        registry = ToolRegistry()
        invoked = []
        registry.register(definition("write", side_effect=True, requires_approval=True,
            risk="high", parallel_safe=False), lambda _a, _c: invoked.append("write") or {"ok": True})
        store = InMemoryExecutionStateStore()
        planner = QueuePlanner(AgentExecutionPlan(calls=(call("write"),)),
                               AgentExecutionPlan(done=True, final_answer="done"))
        first = engine_for(registry, planner, state_store=store,
            authorization=ToolAuthorization(frozenset({"test:read"}), lambda _d, _c: None))
        waiting = first.run("write once", repository_id="repo", conversation_id="conversation")
        self.assertEqual(waiting.status, "waiting_approval")
        self.assertEqual(invoked, [])

        second = engine_for(registry, planner, state_store=store,
            authorization=ToolAuthorization(frozenset({"test:read"}), lambda _d, _c: True))
        resumed = second.resume(waiting.execution_id, objective="write once", repository_id="repo",
                                conversation_id="conversation")
        self.assertEqual(resumed.status, "completed")
        self.assertEqual(invoked, ["write"])
        self.assertIsNotNone(store.load(waiting.execution_id).completed[0].get("request"))

        changed = ToolRegistry()
        changed.register(definition("write", side_effect=True, requires_approval=True,
            risk="high", parallel_safe=False, timeout=2.0), lambda _a, _c: invoked.append("wrong") or {})
        changed_engine = engine_for(changed, QueuePlanner(AgentExecutionPlan(done=True)),
            state_store=store, authorization=ToolAuthorization(frozenset({"test:read"}), lambda _d, _c: True))
        # The prior execution is terminal; a changed definition cannot reopen or replay it.
        self.assertEqual(changed_engine.resume(waiting.execution_id, objective="write once",
            repository_id="repo", conversation_id="conversation").status, "completed")
        self.assertEqual(invoked, ["write"])

    def test_unapproved_high_risk_tool_is_not_runnable(self):
        registry = ToolRegistry()
        with self.assertRaises(ValueError):
            registry.register(definition("danger", risk="critical"), lambda _a, _c: {})

    def test_side_effect_failure_is_unknown_and_never_replanned(self):
        registry = ToolRegistry()
        attempts = []

        def fail(_args, _context):
            attempts.append(1)
            raise RuntimeError("external response lost")

        registry.register(definition("effect", side_effect=True, parallel_safe=False), fail)
        planner = QueuePlanner(AgentExecutionPlan(calls=(call("effect"),)),
                               AgentExecutionPlan(calls=(call("effect", task_id="task-2", call_id="call-2"),)))
        report = engine_for(registry, planner).run("perform effect")
        self.assertEqual(report.status, "failed")
        self.assertEqual(report.error_code, ToolErrorCode.PARTIAL_RESULT.value)
        self.assertEqual(report.results[0].status, "interrupted")
        self.assertEqual(attempts, [1])

    def test_bounded_side_effect_timeout_records_unknown_and_does_not_retry(self):
        registry = ToolRegistry()
        called = []
        release = threading.Event()

        def slow(_args, _context):
            called.append("started")
            release.wait(0.2)
            return {"ok": True}

        registry.register(definition("slow-effect", side_effect=True, timeout=0.02), slow)
        start = time.monotonic()
        report = engine_for(registry, QueuePlanner(AgentExecutionPlan(calls=(call("slow-effect"),)),
            AgentExecutionPlan(done=True)), run_timeout_seconds=1).run("slow effect")
        elapsed = time.monotonic() - start
        release.set()
        self.assertLess(elapsed, 0.15)
        self.assertEqual(report.status, "failed")
        self.assertEqual(report.results[0].status, "interrupted")
        self.assertEqual(report.results[0].execution_metadata["effect_outcome"], "unknown")
        self.assertEqual(called, ["started"])

    def test_retry_is_limited_to_explicit_idempotent_read_tools(self):
        registry = ToolRegistry()
        attempts = []

        def retry_once(_args, _context):
            attempts.append(1)
            if len(attempts) == 1:
                raise ToolExecutionFailure(ToolErrorCode.RATE_LIMITED, retryable=True)
            return {"ok": True}

        registry.register(definition(retries=1), retry_once)
        report = engine_for(registry, QueuePlanner(AgentExecutionPlan(calls=(call(),)),
            AgentExecutionPlan(done=True))).run("retry safely")
        self.assertEqual(report.status, "completed")
        self.assertEqual(len(attempts), 2)
        self.assertEqual(report.results[0].execution_metadata["attempts"], 2)
        with self.assertRaises(ValueError):
            registry.register(definition("bad-retry", side_effect=True, retries=1), lambda _a, _c: {})

    def test_duplicate_tool_invocation_is_replanned_once_without_reexecution(self):
        registry = ToolRegistry()
        attempts = []
        registry.register(definition(), lambda _a, _c: attempts.append(1) or {})
        same = call()
        planner = QueuePlanner(AgentExecutionPlan(calls=(same,)),
            AgentExecutionPlan(calls=(call(task_id="second", call_id="second-call"),)))
        report = engine_for(registry, planner).run("repeat")
        self.assertEqual(report.status, "completed")
        self.assertEqual(attempts, [1])
        self.assertEqual(len(planner.seen_observations), 3)
        feedback = [item for item in planner.seen_observations[2]
                    if item.tool_id == "planner_validation"]
        self.assertEqual(len(feedback), 1)
        self.assertEqual(feedback[0].error_code, ToolErrorCode.DUPLICATE_CALL.value)
        self.assertEqual(len(report.results), 1)

    def test_failed_dependency_recovery_uses_fresh_ids_after_one_duplicate_id_correction(self):
        registry = ToolRegistry()
        fallback_calls = []

        def unavailable(_args, _context):
            raise ToolExecutionFailure(ToolErrorCode.TOOL_UNAVAILABLE)

        registry.register(definition("primary"), unavailable)
        registry.register(definition("fallback"),
                          lambda _args, _context: fallback_calls.append("ran") or {"recovered": True})
        planner = QueuePlanner(
            AgentExecutionPlan(calls=(
                call("primary", task_id="primary-task", call_id="t1"),
                call("fallback", task_id="t2", call_id="t2",
                     dependencies=("primary-task",)),
            )),
            # The first fallback was skipped; the model then reuses its old ID.
            AgentExecutionPlan(calls=(call("fallback", task_id="t2", call_id="t2"),)),
            AgentExecutionPlan(calls=(call("fallback", task_id="t3", call_id="t3"),)),
            AgentExecutionPlan(done=True, final_answer="Fallback recovered."),
        )

        report = engine_for(registry, planner).run(
            "read primary and use fallback_synthetic_read if primary_synthetic_read is unavailable")

        self.assertEqual("completed", report.status,
                         f"{report.error_code}: {[(item.tool_id, item.status, item.error_code) for item in report.results]}")
        self.assertEqual(["ran"], fallback_calls)
        actual_results = [item for item in report.results
                          if item.execution_metadata.get("stage") != "plan_validation"]
        self.assertEqual([("primary", "failed"), ("fallback", "skipped"),
                          ("fallback", "completed")],
                         [(item.tool_id, item.status) for item in actual_results])
        correction = [item for item in planner.seen_observations[2]
                      if item.tool_id == "planner_validation"]
        self.assertEqual(1, len(correction))
        self.assertIn("already used", correction[0].output["message"])
        self.assertEqual(ToolErrorCode.DUPLICATE_CALL.value, correction[0].error_code)

    def test_invalid_tool_arguments_get_one_safe_correction_without_running_invalid_plan(self):
        registry = ToolRegistry()
        executed = []
        schema = {"type": "object", "properties": {"amount": {"type": "integer"}},
                  "required": ["amount"], "additionalProperties": False}
        registry.register(definition("calculate", handler_schema=schema),
                          lambda args, _context: executed.append(args["amount"]) or {"total": args["amount"]})
        secret = "synthetic-sensitive-value-must-not-enter-diagnostics"
        planner = QueuePlanner(
            AgentExecutionPlan(calls=(call("calculate", task_id="bad", call_id="bad",
                                           arguments={"amount": secret}),)),
            AgentExecutionPlan(calls=(call("calculate", task_id="good", call_id="good",
                                           arguments={"amount": 7}),)),
            AgentExecutionPlan(done=True, final_answer="The total is 7."),
        )

        report = engine_for(registry, planner).run("calculate safely")

        self.assertEqual("completed", report.status)
        self.assertEqual([7], executed)
        rejected = next(item for item in report.results
                        if item.execution_metadata.get("stage") == "plan_validation")
        diagnostic_text = json.dumps(rejected.execution_metadata["diagnostic_details"])
        self.assertNotIn(secret, diagnostic_text)
        details = rejected.execution_metadata["diagnostic_details"]
        self.assertEqual("calculate", details["tool_id"])
        self.assertEqual(["amount"], details["argument_fields"])
        self.assertEqual({"amount": "string"}, details["argument_types"])
        self.assertEqual([{"field": "arguments.amount", "expected_type": "integer",
                           "actual_type": "string"}], details["type_mismatches"])

    def test_invalid_tool_argument_correction_exhaustion_fails_without_running_tool(self):
        registry = ToolRegistry()
        executed = []
        schema = {"type": "object", "properties": {"amount": {"type": "integer"}},
                  "required": ["amount"], "additionalProperties": False}
        registry.register(definition("calculate", handler_schema=schema),
                          lambda args, _context: executed.append(args["amount"]) or {})
        planner = QueuePlanner(
            AgentExecutionPlan(calls=(call("calculate", task_id="bad-1", call_id="bad-1",
                                           arguments={"amount": "wrong-1"}),)),
            AgentExecutionPlan(calls=(call("calculate", task_id="bad-2", call_id="bad-2",
                                           arguments={"amount": "wrong-2"}),)),
            AgentExecutionPlan(done=True, final_answer="must not be reached"),
        )

        report = engine_for(registry, planner).run("calculate safely")

        self.assertEqual("failed", report.status)
        self.assertEqual(ToolErrorCode.INVALID_ARGUMENTS.value, report.error_code)
        self.assertEqual([], executed)
        self.assertEqual(2, len(planner.seen_observations))

    def test_handler_argument_rejection_is_safely_reported_and_allows_one_correction(self):
        registry = ToolRegistry()
        received = []
        schema = {"type": "object", "properties": {"code": {"type": "string"}},
                  "required": ["code"], "additionalProperties": False}

        def handler(args, _context):
            received.append(args["code"])
            if args["code"] != "accepted":
                raise ToolExecutionFailure(ToolErrorCode.INVALID_ARGUMENTS)
            return {"accepted": True}

        registry.register(definition("check", handler_schema=schema), handler)
        planner = QueuePlanner(
            AgentExecutionPlan(calls=(call("check", task_id="bad", call_id="bad",
                                           arguments={"code": "synthetic-secret"}),)),
            AgentExecutionPlan(calls=(call("check", task_id="fixed", call_id="fixed",
                                           arguments={"code": "accepted"}),)),
            AgentExecutionPlan(done=True, final_answer="Accepted."),
        )

        report = engine_for(registry, planner).run("check input")

        self.assertEqual("completed", report.status)
        self.assertEqual(["synthetic-secret", "accepted"], received)
        observation = next(item for item in planner.seen_observations[1]
                           if item.tool_id == "check")
        details = observation.output["diagnostic_details"]
        self.assertEqual("handler_rejected_arguments", details["schema_error_code"])
        self.assertEqual({"code": "string"}, details["argument_types"])
        self.assertNotIn("synthetic-secret", json.dumps(details))

    def test_repeated_handler_argument_rejection_stops_after_single_correction(self):
        registry = ToolRegistry()
        schema = {"type": "object", "properties": {"code": {"type": "string"}},
                  "required": ["code"], "additionalProperties": False}
        invocations = []

        def handler(args, _context):
            invocations.append(args["code"])
            raise ToolExecutionFailure(ToolErrorCode.INVALID_ARGUMENTS)

        registry.register(definition("check", handler_schema=schema), handler)
        planner = QueuePlanner(
            AgentExecutionPlan(calls=(call("check", task_id="bad-1", call_id="bad-1",
                                           arguments={"code": "first"}),)),
            AgentExecutionPlan(calls=(call("check", task_id="bad-2", call_id="bad-2",
                                           arguments={"code": "second"}),)),
            AgentExecutionPlan(done=True, final_answer="must not be reached"),
        )

        report = engine_for(registry, planner).run("check input")

        self.assertEqual("failed", report.status)
        self.assertEqual(ToolErrorCode.INVALID_ARGUMENTS.value, report.error_code)
        self.assertEqual(["first", "second"], invocations)
        self.assertEqual(2, len(planner.seen_observations))

    def test_repeated_duplicate_after_bounded_correction_fails_closed(self):
        registry = ToolRegistry()
        attempts = []
        registry.register(definition(), lambda _a, _c: attempts.append(1) or {})
        planner = QueuePlanner(
            AgentExecutionPlan(calls=(call(),)),
            AgentExecutionPlan(calls=(call(task_id="second", call_id="second-call"),)),
            AgentExecutionPlan(calls=(call(task_id="third", call_id="third-call"),)),
        )
        report = engine_for(registry, planner).run("repeat")
        self.assertEqual(report.status, "failed")
        self.assertEqual(report.error_code, ToolErrorCode.DUPLICATE_CALL.value)
        self.assertEqual(attempts, [1])
        self.assertEqual(len(planner.seen_observations), 3)

    def test_duplicate_calls_in_one_plan_are_not_executed_before_correction(self):
        registry = ToolRegistry()
        attempts = []
        registry.register(definition(), lambda _a, _c: attempts.append(1) or {})
        planner = QueuePlanner(AgentExecutionPlan(calls=(
            call(), call(task_id="task-2", call_id="call-2"),
        )))
        report = engine_for(registry, planner).run("repeat within a plan")
        self.assertEqual(report.status, "completed")
        self.assertEqual(attempts, [])
        self.assertEqual(len(planner.seen_observations), 2)
        self.assertEqual(planner.seen_observations[1][0].error_code,
                         ToolErrorCode.DUPLICATE_CALL.value)

    def test_cancellation_and_call_timeout_are_visible_to_handler(self):
        registry = ToolRegistry()
        cancel = threading.Event()
        observed = []

        def wait_for_cancel(_args, context):
            while not context.cancel_event.is_set():
                time.sleep(0.001)
            observed.append(True)
            return {}

        registry.register(definition(timeout=0.01), wait_for_cancel)
        report = engine_for(registry, QueuePlanner(AgentExecutionPlan(calls=(call(),)),
            AgentExecutionPlan(done=True)), cancel_event=cancel).run("wait")
        self.assertEqual(report.status, "completed")
        deadline = time.monotonic() + 0.5
        while not observed and time.monotonic() < deadline:
            time.sleep(0.002)
        self.assertTrue(observed)
        self.assertEqual(report.results[0].error_code, ToolErrorCode.TIMEOUT.value)

        cancel.set()
        cancelled = engine_for(registry, QueuePlanner(AgentExecutionPlan(done=True)),
                               cancel_event=cancel).run("cancel now")
        self.assertEqual(cancelled.status, "cancelled")

    def test_waiting_for_user_input_can_resume_with_new_observation(self):
        registry = ToolRegistry()
        store = InMemoryExecutionStateStore()
        planner = QueuePlanner(AgentExecutionPlan(user_input_request="Which path?"),
                               AgentExecutionPlan(done=True, final_answer="path received"))
        first = engine_for(registry, planner, state_store=store)
        waiting = first.run("locate file", repository_id="repo", conversation_id="conversation")
        self.assertEqual(waiting.status, "waiting_user_input")
        resumed = engine_for(registry, planner, state_store=store).resume(
            waiting.execution_id, objective="locate file", repository_id="repo",
            conversation_id="conversation", user_input="src/main.py")
        self.assertEqual(resumed.final_answer, "path received")
        provided = [item for item in planner.seen_observations[1] if item.tool_id == "user_input"]
        self.assertEqual(provided[0].output["text"], "src/main.py")

    def test_json_roundtrip_restores_tuple_fields_and_pending_state(self):
        snapshot = ExecutionSnapshot("execution-1", "objective", "repo", "conversation",
            ExecutionStatus.OBSERVING.value, 2, completed=({"call_id": "c"},),
            observations=({"call_id": "c"},), active_calls=(),
            planner_request_diagnostics=({"phase": "agent_execution_planner",
                "input_budget_tokens": 1000, "usage": {"prompt_tokens": 10}},))
        restored = _snapshot_from_dict(json.loads(json.dumps({
            **snapshot.__dict__, "completed": list(snapshot.completed),
            "observations": list(snapshot.observations), "active_calls": [],
            "planner_request_diagnostics": list(snapshot.planner_request_diagnostics)})))
        self.assertIsInstance(restored.completed, tuple)
        self.assertIsInstance(restored.observations, tuple)
        self.assertIsInstance(restored.planner_request_diagnostics, tuple)
        self.assertEqual(restored.completed[0]["call_id"], "c")
        self.assertEqual(10, restored.planner_request_diagnostics[0]["usage"]["prompt_tokens"])

        legacy = {key: value for key, value in snapshot.__dict__.items()
                  if key != "planner_request_diagnostics"}
        legacy["completed"] = list(snapshot.completed)
        legacy["observations"] = list(snapshot.observations)
        legacy["active_calls"] = []
        migrated = _snapshot_from_dict(legacy)
        self.assertEqual((), migrated.planner_request_diagnostics)
        corrupt = dict(snapshot.__dict__); corrupt["unexpected"] = True
        with self.assertRaises(ValueError):
            _snapshot_from_dict(corrupt)
        corrupt = dict(snapshot.__dict__, repository_id={"unexpected": "object"})
        with self.assertRaises(ValueError):
            _snapshot_from_dict(corrupt)

    def test_checkpoint_compare_and_swap_prevents_two_runners_overwriting_state(self):
        store = InMemoryExecutionStateStore()
        initial = ExecutionSnapshot("execution-cas", "objective", "repo", "conversation",
            ExecutionStatus.PLANNING.value, 0)
        self.assertEqual(store.save(initial), 1)
        stale = store.load("execution-cas")
        self.assertEqual(store.save(ExecutionSnapshot("execution-cas", "objective", "repo",
            "conversation", ExecutionStatus.OBSERVING.value, 1, version=stale.version)), 2)
        with self.assertRaises(ExecutionCheckpointConflict):
            store.save(stale)

    def test_running_checkpoint_without_active_tools_can_safely_replan(self):
        registry = ToolRegistry()
        store = InMemoryExecutionStateStore()
        store.save(ExecutionSnapshot("execution-crash-gap", "objective", "repo", "conversation",
            ExecutionStatus.RUNNING.value, 0))
        report = engine_for(registry, QueuePlanner(AgentExecutionPlan(done=True, final_answer="recovered")),
            state_store=store).resume("execution-crash-gap", objective="objective",
                                      repository_id="repo", conversation_id="conversation")
        self.assertEqual(report.status, "completed")
        self.assertEqual(report.final_answer, "recovered")

    def test_resume_never_replays_an_interrupted_side_effect(self):
        registry = ToolRegistry()
        called = []
        registry.register(definition("effect", side_effect=True, parallel_safe=False),
                          lambda _a, _c: called.append(1) or {})
        store = InMemoryExecutionStateStore()
        snapshot = ExecutionSnapshot("execution-side-effect", "objective", "repo", "conversation",
            ExecutionStatus.RUNNING.value, 1,
            active_calls=({"call_id": "call-effect", "task_id": "task-effect",
                           "tool_id": "effect", "side_effect": True,
                           "request": {"call_id": "call-effect", "task_id": "task-effect",
                                       "tool_id": "effect", "arguments": {},
                                       "purpose": "test", "expected_output": "", "dependencies": []}},))
        store.save(snapshot)
        report = engine_for(registry, QueuePlanner(AgentExecutionPlan(done=True)),
                            state_store=store).resume("execution-side-effect", objective="objective",
                                repository_id="repo", conversation_id="conversation")
        self.assertEqual(report.status, "failed")
        self.assertEqual(report.error_code, ToolErrorCode.PARTIAL_RESULT.value)
        self.assertEqual(called, [])

    def test_interrupted_read_only_call_is_observed_and_replanned(self):
        registry = ToolRegistry()
        registry.register(definition(), lambda _a, _c: self.fail("interrupted read must not be replayed here"))
        store = InMemoryExecutionStateStore()
        snapshot = ExecutionSnapshot("execution-read", "objective", "repo", "conversation",
            ExecutionStatus.RUNNING.value, 1,
            active_calls=({"call_id": "call-read", "task_id": "task-read", "tool_id": "lookup",
                           "side_effect": False, "request": {}},))
        store.save(snapshot)
        planner = QueuePlanner(AgentExecutionPlan(done=True, final_answer="replanned"))
        report = engine_for(registry, planner, state_store=store).resume(
            "execution-read", objective="objective", repository_id="repo", conversation_id="conversation")
        self.assertEqual(report.status, "completed")
        self.assertEqual(planner.seen_observations[0][0].error_code, None)
        self.assertEqual(planner.seen_observations[0][0].status, "failed")

    def test_large_result_is_paged_without_leaking_across_execution_ids(self):
        store = InMemoryToolResultStore()
        ref = store.put("execution-a", "call-a", "abcdef" * 2_000)
        page = store.page(ref, offset=3_000, limit=25)
        self.assertEqual(page["text"], ("abcdef" * 2_000)[3_000:3_025])
        self.assertFalse(page["complete"])
        with self.assertRaises(PermissionError):
            engine = engine_for(ToolRegistry(), QueuePlanner(AgentExecutionPlan(done=True)))
            engine.execution_id = "execution-b"
            engine.result_store = store
            engine.read_result_page(ref)

    def test_execution_engine_exposes_paged_results_only_after_partial_observation(self):
        from agentforge.agent_execution import RESULT_PAGE_MAX_CHARS

        source_text = "synthetic-page-content-" + ("x" * 20_000)
        registry = ToolRegistry()
        registry.register(definition("large_report", output_schema={"type": "object"}),
                          lambda _args, _context: {"report": source_text})
        result_store = InMemoryToolResultStore()

        class PagePlanner:
            def __init__(self):
                self.catalogs = []
                self.loaded_schemas = []

            def plan(self, _objective, observations, catalog, schema_loader):
                self.catalogs.append(tuple(item.tool_id for item in catalog))
                if not observations:
                    return AgentExecutionPlan(calls=(call("large_report", call_id="large-1"),))
                page_observations = [item for item in observations
                                     if item.tool_id == RESULT_PAGE_TOOL_ID
                                     and item.status == "completed" and isinstance(item.output, dict)]
                pages = [item.output for item in page_observations]
                if pages and pages[-1]["complete"]:
                    return AgentExecutionPlan(done=True, final_answer="all pages observed")
                partial = next(item for item in observations
                               if item.tool_id == "large_report" and item.status == "partial")
                offset = pages[-1]["next_offset"] if pages else 0
                schema = schema_loader(RESULT_PAGE_TOOL_ID)
                self.loaded_schemas.append(schema["input"])
                return AgentExecutionPlan(calls=(ToolCallRequest(
                    call_id=f"page-{offset}", task_id=f"page-{offset}",
                    tool_id=RESULT_PAGE_TOOL_ID,
                    arguments={"result_ref": partial.output["result_ref"],
                               "offset": offset, "limit": RESULT_PAGE_MAX_CHARS},
                    dependencies=(partial.task_id,) + ((page_observations[-1].task_id,)
                                                        if page_observations else ()),
                ),))

        planner = PagePlanner()
        engine = AgentExecutionEngine(
            registry, planner, result_store=result_store,
            authorization=ToolAuthorization(permission_scopes=frozenset({"test:read"})),
            max_tool_calls=8, max_tasks_per_plan=2,
        )
        report = engine.run("read the large report")

        self.assertEqual("completed", report.status)
        self.assertEqual("all pages observed", report.final_answer)
        self.assertNotIn(RESULT_PAGE_TOOL_ID, planner.catalogs[0])
        self.assertTrue(all(RESULT_PAGE_TOOL_ID in ids for ids in planner.catalogs[1:]))
        page_results = [item.output for item in report.results
                        if item.tool_id == RESULT_PAGE_TOOL_ID and item.status == "completed"]
        self.assertEqual(len(page_results), len(planner.loaded_schemas))
        self.assertGreater(len(page_results), 1)
        self.assertEqual(list(range(len(page_results))),
                         [item["page_number"] - 1 for item in page_results])
        self.assertEqual(list(range(0, page_results[-1]["total_chars"], RESULT_PAGE_MAX_CHARS)),
                         [item["offset"] for item in page_results])
        self.assertTrue(page_results[-1]["complete"])
        self.assertTrue(all(item["source_call_id"] == "large-1" for item in page_results))
        self.assertTrue(all(item["source_sha256"] for item in page_results))
        self.assertTrue(all(item["page_sha256"] for item in page_results))
        recovered = "".join(item["text"] for item in page_results)
        expected = json.dumps({"report": source_text}, ensure_ascii=False, separators=(",", ":"))
        self.assertEqual(expected, recovered)
        from dataclasses import asdict
        from types import SimpleNamespace
        from agentforge.conversation_orchestrator import _tool_observation_context
        answer_context = _tool_observation_context(SimpleNamespace(
            execution_results=[asdict(item) for item in report.results]))
        context_pages = [item for item in answer_context
                         if item["tool_id"] == RESULT_PAGE_TOOL_ID]
        self.assertEqual(len(page_results), len(context_pages))
        self.assertEqual(expected, "".join(item["output_preview"] for item in context_pages))
        self.assertTrue(all(item["result_page"]["page_sha256"] for item in context_pages))

    def test_result_page_tool_rejects_skipped_offsets_without_claiming_completion(self):
        registry = ToolRegistry()
        registry.register(definition("large_report"), lambda _args, _context: {"text": "x" * 20_000})

        class SkipPlanner:
            def plan(self, _objective, observations, catalog, schema_loader):
                if not observations:
                    return AgentExecutionPlan(calls=(call("large_report", call_id="large-1"),))
                failed_page = next((item for item in observations
                                    if item.tool_id == RESULT_PAGE_TOOL_ID
                                    and item.status == "failed"), None)
                if failed_page:
                    return AgentExecutionPlan(done=True, final_answer="Result is incomplete.")
                if any(item.tool_id == RESULT_PAGE_TOOL_ID for item in observations):
                    partial = next(item for item in observations
                                   if item.tool_id == "large_report" and item.status == "partial")
                    previous_page = next(item for item in reversed(observations)
                                         if item.tool_id == RESULT_PAGE_TOOL_ID)
                    return AgentExecutionPlan(calls=(ToolCallRequest(
                        call_id="skip-page", task_id="skip-page", tool_id=RESULT_PAGE_TOOL_ID,
                        arguments={"result_ref": partial.output["result_ref"],
                                   "offset": 8_000, "limit": 4_000},
                        dependencies=(partial.task_id, previous_page.task_id)),))
                partial = next(item for item in observations
                               if item.tool_id == "large_report" and item.status == "partial")
                schema_loader(RESULT_PAGE_TOOL_ID)
                return AgentExecutionPlan(calls=(ToolCallRequest(
                    call_id="first-page", task_id="first-page", tool_id=RESULT_PAGE_TOOL_ID,
                    arguments={"result_ref": partial.output["result_ref"],
                               "offset": 0, "limit": 4_000},
                    dependencies=(partial.task_id,)),))

        report = engine_for(registry, SkipPlanner(), max_tool_calls=5).run("read the large report")

        self.assertEqual("completed", report.status)
        self.assertEqual("Result is incomplete.", report.final_answer)
        pages = [item for item in report.results if item.tool_id == RESULT_PAGE_TOOL_ID]
        self.assertEqual(["completed", "failed"], [item.status for item in pages])
        self.assertFalse(any(item.output.get("complete") for item in pages
                             if isinstance(item.output, dict)))

    def test_postgres_adapters_persist_execution_scope_and_large_output_on_sqlite(self):
        try:
            from sqlalchemy import create_engine
            from sqlalchemy.orm import Session
            from agentforge.af012_schema import Base, Conversation
        except ImportError:
            self.skipTest("SQLAlchemy optional dependency is unavailable.")
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        conversation_id = uuid4()
        with Session(engine) as session, session.begin():
            session.add(Conversation(id=conversation_id, repository_id="repo-a", title="test",
                                     provider_id="offline", model_id="offline"))
        state_store = PostgresExecutionStateStore(engine)
        snapshot = ExecutionSnapshot("execution-pg", "objective", "repo-a", str(conversation_id),
                                     ExecutionStatus.OBSERVING.value, 1)
        state_store.save(snapshot)
        loaded = state_store.load("execution-pg")
        self.assertEqual(loaded.conversation_id, str(conversation_id))
        state_store.save(loaded)
        with self.assertRaises(ExecutionCheckpointConflict):
            state_store.save(loaded)
        with self.assertRaises(ValueError):
            state_store.save(ExecutionSnapshot("execution-wrong", "objective", "repo-b",
                str(conversation_id), ExecutionStatus.PLANNING.value, 0))
        result_store = PostgresToolResultStore(engine)
        result_ref = result_store.put("execution-pg", "call-page", "长结果-text-" * 1_500)
        self.assertEqual(result_store.put("execution-pg", "call-page", "长结果-text-" * 1_500), result_ref)
        page = result_store.page(result_ref, offset=2, limit=12)
        self.assertEqual(page["text"], ("长结果-text-" * 1_500)[2:14])
        with self.assertRaises(ValueError):
            result_store.put("execution-pg", "call-page", "changed")
        engine.dispose()

    def test_llm_planner_loads_selected_schemas_before_calls(self):
        class Provider:
            def __init__(self):
                self.calls = 0
                self.descriptor = LOCAL_QWEN
            def plan_completion(self, messages, *, max_output_tokens, timeout_seconds):
                self.calls += 1
                self.system_prompt = messages[0]["content"]
                payload = json.loads(messages[-1]["content"])
                self.planner_schema = payload["schema"]
                if self.calls == 1:
                    return json.dumps({"action": "plan", "calls": [{
                        "call_id": "c1", "tool_id": "lookup", "arguments": {},
                        "dependencies": [], "purpose": "lookup", "expected_output": "object"}]})
                if "lookup" not in payload["requested_schemas"]:
                    raise AssertionError("The planner must receive the selected tool's schema.")
                return json.dumps({"action": "finish", "final_answer": "done"})
            def request_stats(self):
                return {"last_usage": {"prompt_tokens": 100, "completion_tokens": 20},
                        "last_request_id": f"request-{self.calls}", "last_finish_reason": "stop"}

        registry = ToolRegistry()
        registry.register(definition(), lambda _a, _c: {})
        provider = Provider()
        planner = LLMExecutionPlanner(provider)
        catalog = ToolDiscovery(registry).discover("lookup")
        plan = planner.plan("lookup", (), catalog, ToolDiscovery(registry).schema_for)
        self.assertTrue(plan.done)
        self.assertEqual(provider.calls, 2)
        self.assertEqual(planner.request_count, 2)
        self.assertEqual(2, len(planner.request_diagnostics))
        self.assertEqual("exact" if planner.request_diagnostics[-1]["tokenizer_method"] == "exact"
                         else "estimated", "exact" if False else "estimated")
        self.assertEqual(1, planner.last_request_diagnostics["planner_schema_count"])
        self.assertEqual(["lookup"], planner.last_request_diagnostics["planner_schema_tool_ids"])
        self.assertGreater(planner.last_request_diagnostics["planner_payload_utf8_bytes"], 0)
        self.assertIn("call_id/task_id are unique", provider.system_prompt)
        self.assertIn("dependencies name prior calls", provider.system_prompt)
        self.assertIn("call_id/task_id are unique", provider.system_prompt)
        self.assertIn("Engine enforces approval", provider.system_prompt)
        self.assertIn("ask_user only for missing required input", provider.system_prompt)
        self.assertEqual("unique call/task ID",
                         provider.planner_schema["calls"][0]["call_id"])
        self.assertEqual(["prerequisite call_id"],
                         provider.planner_schema["calls"][0]["dependencies"])
        self.assertNotIn("task_id", provider.planner_schema["calls"][0])

    def test_llm_planner_only_includes_result_paging_protocol_when_partial_result_exists(self):
        class Provider:
            descriptor = LOCAL_QWEN

            def __init__(self):
                self.system_prompts = []

            def plan_completion(self, messages, **_kwargs):
                self.system_prompts.append(messages[0]["content"])
                return json.dumps({"action": "finish", "final_answer": "Observed."})

        provider = Provider()
        planner = LLMExecutionPlanner(provider)
        planner.plan("answer", (), (), lambda _tool_id: {})
        self.assertNotIn("agent.read_result_page", provider.system_prompts[0])

        partial = ToolResult(
            "large-call", "large-task", "read_large", "partial",
            {"result_ref": "agent-result:synthetic:large-call:sha256"},
        )
        planner.plan("answer", (partial,), (), lambda _tool_id: {})
        self.assertIn("agent.read_result_page", provider.system_prompts[1])
        self.assertIn("each next_offset", provider.system_prompts[1])
        self.assertIn("source partial task", provider.system_prompts[1])
        self.assertIn("prior page task", provider.system_prompts[1])

    def test_planner_call_id_is_the_internal_task_and_dependency_identity(self):
        action = _parse_planner_action(json.dumps({"action": "plan", "calls": [
            {"call_id": "open", "tool_id": "open", "arguments": {},
             "dependencies": [], "purpose": "open", "expected_output": "opened"},
            {"call_id": "inspect", "tool_id": "inspect", "arguments": {},
             "dependencies": ["open"], "purpose": "inspect", "expected_output": "inspected"},
        ]}), tools_only=True)
        calls = action["calls"]
        self.assertEqual(["open", "inspect"], [item.task_id for item in calls])
        self.assertEqual(("open",), calls[1].dependencies)

    def test_llm_planner_loads_full_bounded_catalog_and_deduplicates_schema_ids(self):
        class Provider:
            descriptor = LOCAL_QWEN

            def __init__(self):
                self.calls = 0

            def plan_completion(self, messages, *, max_output_tokens, timeout_seconds):
                self.calls += 1
                payload = json.loads(messages[-1]["content"])
                if self.calls == 1:
                    tool_ids = sorted(item["tool_id"] for item in payload["available_tools"])
                    return json.dumps({"action": "load_schemas", "tool_ids": tool_ids + [tool_ids[0]]})
                self.asserted_schema_ids = sorted(payload["requested_schemas"])
                return json.dumps({"action": "finish"})

        registry = ToolRegistry()
        for index in range(5):
            tool_id = f"lookup_{index}"
            registry.register(definition(tool_id), lambda _args, _ctx: {})
        discovery = ToolDiscovery(registry)
        catalog = discovery.discover("lookup")
        self.assertEqual(5, len(catalog))

        provider = Provider()
        provider.descriptor = ModelDescriptor(
            provider_id="deepseek", model_id="deepseek-synthetic",
            display_name="Synthetic model budget", model_type="cloud",
            context_window=1_048_576, max_output_tokens=393_216,
            adapter_max_output_tokens=512,
        )
        planner = LLMExecutionPlanner(provider, tools_only=True)
        plan = planner.plan("lookup", (), catalog, discovery.schema_for)

        self.assertTrue(plan.done)
        self.assertEqual(2, provider.calls)
        self.assertEqual(sorted(item.tool_id for item in catalog), provider.asserted_schema_ids)
        self.assertEqual(5, planner.last_request_diagnostics["planner_schema_count"])

    def test_empty_schema_request_is_rejected_with_safe_count_diagnostics(self):
        class Provider:
            descriptor = LOCAL_QWEN

            def plan_completion(self, *_args, **_kwargs):
                return json.dumps({"action": "load_schemas", "tool_ids": []})

        registry = ToolRegistry()
        registry.register(definition(), lambda _args, _ctx: {})
        discovery = ToolDiscovery(registry)
        catalog = discovery.discover("lookup")
        planner = LLMExecutionPlanner(Provider(), tools_only=True)
        with self.assertRaises(PlanValidationError):
            planner.plan("lookup", (), catalog, discovery.schema_for)

        self.assertEqual({
            "requested_count": 0, "unique_count": 0, "duplicate_count": 0,
            "catalog_count": 1, "schema_limit": LLMExecutionPlanner.MAX_SCHEMA_LOAD_TOOLS,
            "empty_set": True, "over_limit": False,
        }, planner.last_request_diagnostics["planning_failure_details"])

    def test_repeated_schema_request_gets_one_bounded_feedback_turn(self):
        class Provider:
            descriptor = LOCAL_QWEN

            def __init__(self):
                self.payloads = []

            def plan_completion(self, messages, **_kwargs):
                payload = json.loads(messages[-1]["content"])
                self.payloads.append(payload)
                if len(self.payloads) <= 2:
                    return json.dumps({"action": "load_schemas", "tool_ids": ["lookup"]})
                return json.dumps({"action": "plan", "calls": [{
                    "call_id": "c1", "tool_id": "lookup", "arguments": {},
                    "dependencies": [], "purpose": "lookup", "expected_output": "object",
                }]})

        registry = ToolRegistry()
        registry.register(definition("lookup"), lambda _args, _ctx: {})
        discovery = ToolDiscovery(registry)
        provider = Provider()
        loaded = []
        planner = LLMExecutionPlanner(provider)

        plan = planner.plan("lookup", (), discovery.discover("lookup"),
                            lambda tool_id: loaded.append(tool_id) or discovery.schema_for(tool_id))

        self.assertEqual(["lookup"], [item.tool_id for item in plan.calls])
        self.assertEqual(["lookup"], loaded)
        self.assertEqual(3, planner.request_count)
        self.assertNotIn("schema_feedback", provider.payloads[0])
        self.assertIn("already present", provider.payloads[2]["schema_feedback"])
        self.assertEqual(["lookup"], sorted(provider.payloads[2]["requested_schemas"]))

    def test_repeated_schema_request_after_feedback_fails_with_specific_diagnostic(self):
        class Provider:
            descriptor = LOCAL_QWEN

            def __init__(self):
                self.payloads = []

            def plan_completion(self, messages, **_kwargs):
                self.payloads.append(json.loads(messages[-1]["content"]))
                return json.dumps({"action": "load_schemas", "tool_ids": ["lookup"]})

        registry = ToolRegistry()
        registry.register(definition("lookup"), lambda _args, _ctx: {})
        discovery = ToolDiscovery(registry)
        provider = Provider()
        planner = LLMExecutionPlanner(provider)

        with self.assertRaises(PlanValidationError) as caught:
            planner.plan("lookup", (), discovery.discover("lookup"), discovery.schema_for)

        self.assertEqual("repeated_schema_request", caught.exception.diagnostic_code)
        self.assertEqual(3, planner.request_count)
        self.assertEqual(1, len([payload for payload in provider.payloads
                                 if "schema_feedback" in payload]))
        self.assertEqual({
            "requested_count": 1,
            "unique_count": 1,
            "duplicate_count": 0,
            "catalog_count": 1,
            "schema_round": 1,
            "schema_batches": 1,
            "loaded_schema_tool_ids": ["lookup"],
        }, caught.exception.diagnostic_details)
        self.assertEqual(caught.exception.diagnostic_details,
                         planner.last_request_diagnostics["planning_failure_details"])

    def test_two_schema_load_rounds_and_one_correction_fit_the_separate_limits(self):
        class Provider:
            descriptor = LOCAL_QWEN

            def __init__(self):
                self.payloads = []
                self.responses = iter((
                    {"action": "load_schemas", "tool_ids": ["lookup"]},
                    {"action": "load_schemas", "tool_ids": ["inspect"]},
                    {"action": "load_schemas", "tool_ids": ["inspect"]},
                    {"action": "plan", "calls": [{
                        "call_id": "c1", "tool_id": "inspect", "arguments": {},
                        "dependencies": [], "purpose": "inspect", "expected_output": "object",
                    }]},
                ))

            def plan_completion(self, messages, **_kwargs):
                self.payloads.append(json.loads(messages[-1]["content"]))
                return json.dumps(next(self.responses))

        registry = ToolRegistry()
        registry.register(definition("lookup"), lambda _args, _ctx: {})
        registry.register(definition("inspect"), lambda _args, _ctx: {})
        discovery = ToolDiscovery(registry)
        provider = Provider()
        planner = LLMExecutionPlanner(provider)

        plan = planner.plan("lookup inspect", (), discovery.discover("lookup inspect"),
                            discovery.schema_for)

        self.assertEqual("inspect", plan.calls[0].tool_id)
        self.assertEqual(4, planner.request_count)
        self.assertEqual(1, LLMExecutionPlanner.MAX_SCHEMA_NO_PROGRESS_CORRECTIONS)
        self.assertIn("schema_feedback", provider.payloads[3])
        self.assertEqual(["inspect", "lookup"], sorted(provider.payloads[3]["requested_schemas"]))

    def test_schema_discovery_uses_finite_catalog_bound_not_two_batch_cap(self):
        class Provider:
            descriptor = LOCAL_QWEN

            def __init__(self):
                self.responses = iter((
                    {"action": "load_schemas", "tool_ids": ["lookup"]},
                    {"action": "load_schemas", "tool_ids": ["inspect"]},
                    {"action": "load_schemas", "tool_ids": ["summarize"]},
                    {"action": "plan", "calls": [{
                        "call_id": "c1", "tool_id": "summarize", "arguments": {},
                        "dependencies": [], "purpose": "summarize", "expected_output": "object",
                    }]},
                ))

            def plan_completion(self, *_args, **_kwargs):
                return json.dumps(next(self.responses))

        registry = ToolRegistry()
        for tool_id in ("lookup", "inspect", "summarize"):
            registry.register(definition(tool_id), lambda _args, _ctx: {})
        discovery = ToolDiscovery(registry)
        planner = LLMExecutionPlanner(Provider())

        plan = planner.plan("lookup inspect summarize", (),
                            discovery.discover("lookup inspect summarize"), discovery.schema_loader)
        self.assertEqual("summarize", plan.calls[0].tool_id)
        self.assertEqual(4, planner.request_count)
        self.assertEqual(["inspect", "lookup", "summarize"],
                         sorted(planner.last_request_diagnostics["planner_schema_tool_ids"]))

    def test_schema_discovery_with_fifty_tools_is_batch_bounded_and_terminates(self):
        class Provider:
            descriptor = ModelDescriptor(
                provider_id="deepseek", model_id="deepseek-synthetic",
                display_name="Synthetic long context", model_type="cloud",
                context_window=1_048_576, max_output_tokens=393_216,
            )

            def __init__(self):
                self.payloads = []
                self.responses = []
                ids = [f"tool-{index:02d}" for index in range(8)]
                for tool_id in ids:
                    self.responses.append({"action": "load_schemas", "tool_ids": [tool_id]})
                self.responses.append({"action": "plan", "calls": [{
                    "call_id": "c1", "tool_id": "tool-00", "arguments": {},
                    "dependencies": [], "purpose": "lookup", "expected_output": "object",
                }]})

            def plan_completion(self, messages, **_kwargs):
                self.payloads.append(json.loads(messages[-1]["content"]))
                return json.dumps(self.responses[len(self.payloads) - 1])

        registry = ToolRegistry()
        for index in range(50):
            registry.register(definition(f"tool-{index:02d}"), lambda _args, _ctx: {})
        discovery = ToolDiscovery(registry)
        provider = Provider()
        planner = LLMExecutionPlanner(provider)
        plan = planner.plan("inspect all tools", (), discovery.discover("inspect all tools"),
                            discovery.schema_loader)
        self.assertEqual("tool-00", plan.calls[0].tool_id)
        self.assertEqual(9, planner.request_count)
        self.assertEqual(ToolDiscovery.MAX_CANDIDATES,
                         planner.last_request_diagnostics["planner_schema_count"])

    def test_plan_validator_is_a_separate_engine_dependency(self):
        registry = ToolRegistry()
        registry.register(definition(), lambda _args, _ctx: {})
        engine = engine_for(registry, QueuePlanner(AgentExecutionPlan(done=True)))
        self.assertEqual("PlanValidator", type(engine.plan_validator).__name__)

    def test_selected_tool_schema_failure_records_only_missing_schema_identity(self):
        class Provider:
            descriptor = LOCAL_QWEN

            def __init__(self):
                self.responses = iter((
                    json.dumps({"action": "load_schemas", "tool_ids": ["lookup"]}),
                    json.dumps({"action": "load_schemas", "tool_ids": ["inspect"]}),
                    json.dumps({"action": "plan", "calls": [{
                        "call_id": "c1", "tool_id": "summarize", "arguments": {},
                        "dependencies": [], "purpose": "summarize", "expected_output": "object",
                    }]}),
                    json.dumps({"action": "plan", "calls": [{
                        "call_id": "c1", "tool_id": "summarize", "arguments": {},
                        "dependencies": [], "purpose": "summarize", "expected_output": "object",
                    }]}),
                ))

            def plan_completion(self, *_args, **_kwargs):
                return next(self.responses)

        registry = ToolRegistry()
        for tool_id in ("lookup", "inspect", "summarize"):
            registry.register(definition(tool_id), lambda _args, _ctx: {})
        discovery = ToolDiscovery(registry)
        planner = LLMExecutionPlanner(Provider())

        plan = planner.plan("lookup inspect summarize", (),
                            discovery.discover("lookup inspect summarize"), discovery.schema_for)
        self.assertEqual("summarize", plan.calls[0].tool_id)
        self.assertEqual(4, planner.request_count)

    def test_undiscovered_selected_tool_fails_closed_with_safe_identity_diagnostic(self):
        class Provider:
            descriptor = LOCAL_QWEN

            def __init__(self):
                self.responses = iter((
                    json.dumps({"action": "load_schemas", "tool_ids": ["lookup"]}),
                    json.dumps({"action": "plan", "calls": [{
                        "call_id": "c1", "tool_id": "not_in_catalog", "arguments": {},
                        "dependencies": [], "purpose": "synthetic lookup", "expected_output": "object",
                    }]}),
                ))

            def plan_completion(self, *_args, **_kwargs):
                return next(self.responses)

        registry = ToolRegistry()
        registry.register(definition("lookup"), lambda _args, _ctx: {})
        discovery = ToolDiscovery(registry)
        planner = LLMExecutionPlanner(Provider())

        with self.assertRaises(PlanValidationError) as raised:
            planner.plan("lookup", (), discovery.discover("lookup"), discovery.schema_loader)

        self.assertEqual(ToolErrorCode.TOOL_NOT_FOUND, raised.exception.code)
        self.assertEqual("undiscovered_selected_tool", raised.exception.diagnostic_code)
        self.assertEqual({
            "undiscovered_tool_ids": ["not_in_catalog"],
            "loaded_schema_tool_ids": ["lookup"],
            "schema_round": 1,
        }, raised.exception.diagnostic_details)
        self.assertEqual(2, planner.request_count)
        self.assertEqual(raised.exception.diagnostic_details,
                         planner.last_request_diagnostics["planning_failure_details"])

    def test_undiscovered_schema_request_gets_one_catalog_scoped_correction(self):
        class Provider:
            descriptor = LOCAL_QWEN

            def __init__(self):
                self.payloads = []
                self.responses = iter((
                    json.dumps({"action": "load_schemas", "tool_ids": ["hallucinated_tool"]}),
                    json.dumps({"action": "load_schemas", "tool_ids": ["fallback_synthetic_read"]}),
                    json.dumps({"action": "plan", "calls": [{
                        "call_id": "fallback", "tool_id": "fallback_synthetic_read",
                        "arguments": {}, "dependencies": [], "purpose": "fallback",
                        "expected_output": "synthetic result",
                    }]}),
                ))

            def plan_completion(self, messages, **_kwargs):
                self.payloads.append(json.loads(messages[-1]["content"]))
                return next(self.responses)

        registry = ToolRegistry()
        registry.register(definition("fallback_synthetic_read"), lambda _args, _ctx: {})
        discovery = ToolDiscovery(registry)
        provider = Provider()
        planner = LLMExecutionPlanner(provider, tools_only=True)

        plan = planner.plan("fallback_synthetic_read", (),
                            discovery.discover("fallback_synthetic_read"),
                            discovery.schema_loader)

        self.assertEqual(["fallback_synthetic_read"], [call.tool_id for call in plan.calls])
        self.assertEqual(3, planner.request_count)
        correction = provider.payloads[1]["schema_feedback"]
        self.assertIn("exact IDs in available_tools", correction)
        self.assertIn("fallback_synthetic_read", correction)
        self.assertNotIn("hallucinated_tool", correction)
        self.assertEqual({
            "requested_count": 1,
            "undiscovered_request_count": 1,
            "catalog_count": 1,
            "available_tool_ids": ["fallback_synthetic_read"],
            "schema_round": 0,
        }, planner.request_diagnostics[0]["planning_failure_details"])

    def test_repeated_undiscovered_schema_request_fails_engine_before_tool_execution(self):
        class Provider:
            descriptor = LOCAL_QWEN

            def plan_completion(self, *_args, **_kwargs):
                return json.dumps({"action": "load_schemas", "tool_ids": ["hallucinated_tool"]})

        executions = []
        registry = ToolRegistry()
        registry.register(definition("fallback_synthetic_read"),
                          lambda _args, _ctx: executions.append("ran"))
        planner = LLMExecutionPlanner(Provider())
        engine = AgentExecutionEngine(
            registry, planner,
            authorization=ToolAuthorization(permission_scopes=frozenset({"test:read"})),
        )
        report = engine.run("fallback_synthetic_read")

        self.assertEqual("failed", report.status)
        self.assertEqual(2, planner.request_count)
        self.assertEqual([], executions)
        diagnostics = planner.last_request_diagnostics
        self.assertEqual("undiscovered_tool_schema", diagnostics["planning_failure_reason"])
        self.assertEqual(["fallback_synthetic_read"],
                         diagnostics["planning_failure_details"]["available_tool_ids"])
        self.assertNotIn("hallucinated_tool", repr(diagnostics["planning_failure_details"]))

    def test_planner_payload_omits_engine_scheduling_metadata_but_keeps_safety_metadata(self):
        registry = ToolRegistry()
        registry.register(definition("lookup", timeout=7.0, concurrency_group="serial",
                                     parallel_safe=False, worker_safe=False), lambda _a, _c: {})
        catalog = ToolDiscovery(registry).discover("lookup")
        payload = json.loads(_planner_payload("lookup", (), catalog, {}))
        item = payload["available_tools"][0]

        self.assertEqual("lookup", item["tool_id"])
        self.assertEqual("test:read", item["permission_scope"])
        self.assertEqual("low", item["risk_level"])
        self.assertFalse(item["side_effect"])
        self.assertFalse(item["requires_approval"])
        self.assertIn("capabilities", item)
        self.assertNotIn("timeout", item)
        self.assertNotIn("concurrency_group", item)
        self.assertNotIn("parallel_safe", item)
        self.assertNotIn("worker_safe", item)
        self.assertNotIn("version", item)

    def test_tools_only_planner_finishes_without_drafting_the_user_answer(self):
        class Provider:
            descriptor = LOCAL_QWEN
            def plan_completion(self, messages, **_kwargs):
                self.messages = messages
                return json.dumps({"action": "finish"})

        registry = ToolRegistry()
        provider = Provider()
        planner = LLMExecutionPlanner(provider, tools_only=True)
        catalog = ToolDiscovery(registry).discover("no matching tools")
        plan = planner.plan("collect evidence", (), catalog, ToolDiscovery(registry).schema_for)
        self.assertTrue(plan.done)
        self.assertIsNone(plan.final_answer)
        schema = json.loads(provider.messages[-1]["content"])["schema"]
        self.assertNotIn("final_answer", schema)
        self.assertEqual("load_schemas only", schema["tool_ids"])
        self.assertIn("load_schemas(action,tool_ids), plan(action,calls), finish(action)",
                      provider.messages[0]["content"])
        self.assertIn("no extra keys", provider.messages[0]["content"])
        self.assertIn("ReAct from observations; stop when sufficient", provider.messages[0]["content"])
        self.assertIn("stop when sufficient, not after preset rounds", provider.messages[0]["content"])

    def test_tools_only_replan_records_safe_reason_when_model_action_is_rejected(self):
        class Provider:
            descriptor = LOCAL_QWEN
            def plan_completion(self, *_args, **_kwargs):
                return '{"action":"finish","final_answer":"private model text"}'
            def request_stats(self):
                return {
                    "last_usage": {"prompt_tokens": 40, "completion_tokens": 8},
                    "last_finish_reason": "stop",
                    "last_response_diagnostic": {
                        "body_bytes": 64, "choice_count": 1,
                        "content_chars": 64, "content_type": "str",
                    },
                }

        planner = LLMExecutionPlanner(Provider(), tools_only=True)
        observation = ToolResult("read-call", "read-task", "synthetic_read", "completed",
                                 {"fact": "synthetic observation"})
        with self.assertRaises(PlanValidationError) as raised:
            planner.plan("use the observation", (observation,), (), lambda _tool_id: {})

        self.assertEqual(ToolErrorCode.INVALID_ARGUMENTS, raised.exception.code)
        diagnostic = planner.last_request_diagnostics
        self.assertEqual("response_validation", diagnostic["planning_failure_phase"])
        self.assertEqual("action_fields_unexpected", diagnostic["planning_failure_reason"])
        self.assertEqual("rejected", diagnostic["response_validation"])
        self.assertTrue(diagnostic["request_succeeded"])
        self.assertEqual(64, diagnostic["response_shape"]["content_chars"])
        self.assertEqual({"action": "finish", "missing_fields": [],
                          "unexpected_fields": ["final_answer"],
                          "other_unexpected_field_count": 0},
                         diagnostic["planning_failure_details"])
        self.assertNotIn("private model text", repr(diagnostic))

    def test_tools_only_replanning_compacts_search_output_and_keeps_evidence_identity(self):
        class Provider:
            descriptor = LOCAL_QWEN
            def plan_completion(self, messages, **_kwargs):
                self.messages = messages
                return json.dumps({"action": "finish"})

        provider = Provider()
        planner = LLMExecutionPlanner(provider, tools_only=True)
        candidates = [{
            "repository": "D:/synthetic/repository",
            "relative_path": f"src/module_{index}.py",
            "start_line": index + 1,
            "end_line": index + 8,
            "text": f"def helper_{index}():\n" + ("return 'synthetic-value'\n" + "x" * 900),
            "content_hash": f"{index:064x}",
            "score": 0.9 - index / 100,
            "channels": ["symbols", "lexical"],
            "channel_scores": [["symbols", 0.9]],
            "symbol_ids": [f"symbol-{index}"],
            "relation_ids": [],
            "resolution_status": "resolved",
            "evidence_roles": ["definition"],
            "source_id": f"E{index + 1}",
            "citation": f"src/module_{index}.py:{index + 1}-{index + 8}",
        } for index in range(12)]
        observation = ToolResult("search-call", "find-source", "search", "completed", {
            "query": "helper",
            "route": "repository",
            "uncertainty": None,
            "unavailable_channels": [],
            "results": candidates,
        }, provenance={"repository_id": "synthetic-repo"})

        result = planner.plan("collect source evidence", (observation,), (), lambda _id: {})

        self.assertTrue(result.done)
        payload = json.loads(provider.messages[-1]["content"])
        compact = payload["observations"][0]["output"]
        self.assertTrue(compact["more_candidates_available"])
        self.assertEqual("E1", compact["results"][0]["source_id"])
        self.assertEqual("src/module_0.py", compact["results"][0]["relative_path"])
        self.assertEqual(1, compact["results"][0]["start_line"])
        self.assertNotIn("content_hash", compact["results"][0])
        self.assertNotIn("text", compact["results"][0])
        self.assertNotIn("text_preview", compact["results"][0])
        self.assertNotIn("score", compact["results"][0])
        self.assertEqual("synthetic-repo", payload["observations"][0]["provenance"]["repository_id"])
        self.assertLess(planner.last_request_diagnostics["estimated_input_tokens"], 3_400)

    def test_llm_planner_preserves_result_reference_and_records_request_usage(self):
        class Provider:
            descriptor = LOCAL_QWEN
            def __init__(self):
                self.messages = None
            def plan_completion(self, messages, **_kwargs):
                self.messages = messages
                return json.dumps({"action": "finish", "final_answer": "complete"})
            def request_stats(self):
                return {"last_usage": {"prompt_tokens": 74, "completion_tokens": 12,
                                       "reasoning_tokens": 4},
                        "last_request_id": "synthetic-request", "last_finish_reason": "stop"}

        provider = Provider()
        planner = LLMExecutionPlanner(provider)
        observation = ToolResult("call-1", "task-1", "search", "partial",
            {"truncated": True, "result_ref": "agent-result:execution-a:call-1",
             "byte_count": 100_000, "preview": "bounded preview"},
            error_code=ToolErrorCode.PARTIAL_RESULT.value)
        result = planner.plan("summarize tool result", (observation,), (), lambda _id: {})
        self.assertTrue(result.done)
        payload = json.loads(provider.messages[-1]["content"])
        self.assertEqual("agent-result:execution-a:call-1",
                         payload["observations"][0]["output"]["result_ref"])
        diagnostic = planner.last_request_diagnostics
        self.assertEqual("agent_execution_planner", diagnostic["phase"])
        self.assertTrue(diagnostic["request_sent"])
        self.assertEqual(74, diagnostic["usage"]["prompt_tokens"])
        self.assertEqual("synthetic-request", diagnostic["request_id"])

    def test_llm_planner_failure_uses_per_call_provider_deltas_and_safe_error_metadata(self):
        class Provider:
            descriptor = LOCAL_QWEN

            def __init__(self):
                self.calls = 0
                self.stats = {
                    "attempted": 0, "sent": 0, "succeeded": 0, "failed": 0, "retries": 0,
                    "last_usage": None, "last_request_id": None, "last_http_status": None,
                    "last_finish_reason": None, "last_error_code": None,
                    "last_application_failure_phase": None,
                    "last_application_error_code": None,
                    "last_response_diagnostic": None,
                }

            def request_stats(self):
                return dict(self.stats)

            def plan_completion(self, _messages, **_kwargs):
                self.calls += 1
                if self.calls == 1:
                    self.stats.update(attempted=1, sent=1, succeeded=1,
                                      last_usage={"prompt_tokens": 17, "completion_tokens": 9},
                                      last_request_id="previous-success", last_finish_reason="stop")
                    return json.dumps({"action": "finish", "final_answer": "done"})
                if self.calls == 2:
                    raise ProviderError("Do not persist this potentially sensitive exception text.")
                self.stats.update(
                    attempted=2, sent=2, succeeded=2, last_http_status=200,
                    last_finish_reason="length", last_application_failure_phase="planning_response",
                    last_application_error_code="incomplete_planning_completion",
                    last_response_diagnostic={"content_type": "str", "content_chars": 512,
                                             "response_text": "must not be persisted"},
                )
                raise ProviderError("Another sensitive response detail.")

        provider = Provider()
        planner = LLMExecutionPlanner(provider)
        registry = ToolRegistry()
        catalog = ToolDiscovery(registry).discover("no matching tools")
        loader = ToolDiscovery(registry).schema_for

        self.assertTrue(planner.plan("synthetic task", (), catalog, loader).done)
        with self.assertRaises(ToolExecutionFailure):
            planner.plan("synthetic task", (), catalog, loader)
        unsent = planner.last_request_diagnostics
        self.assertFalse(unsent["request_sent"])
        self.assertEqual(0, unsent["request_attempted_delta"])
        self.assertIsNone(unsent["usage"])
        self.assertIsNone(unsent["request_id"])
        self.assertEqual("ProviderError", unsent["exception_type"])

        with self.assertRaises(ToolExecutionFailure):
            planner.plan("synthetic task", (), catalog, loader)
        sent = planner.last_request_diagnostics
        self.assertTrue(sent["request_sent"])
        self.assertEqual(1, sent["request_sent_delta"])
        self.assertEqual("incomplete_planning_completion", sent["provider_failure_code"])
        self.assertEqual("planning_response", sent["provider_failure_phase"])
        self.assertEqual("length", sent["finish_reason"])
        self.assertEqual({"content_type": "str", "content_chars": 512},
                         sent["response_shape"])
        self.assertNotIn("must not be persisted", str(sent))
        self.assertNotIn("sensitive", str(planner.request_diagnostics))

    def test_planner_injects_only_approved_scoped_source_linked_memory_and_latest_input(self):
        from agentforge.conversation_context import MemoryCandidate, MemorySourceReference

        class Provider:
            descriptor = LOCAL_QWEN
            def plan_completion(self, messages, **_kwargs):
                self.messages = messages
                return json.dumps({"action": "finish", "final_answer": "complete"})

        source = MemorySourceReference("source-message", 7, "user", "a" * 64)
        approved = MemoryCandidate("repo-a", "Prefer concise parameter lists.", True,
            "memory-a", "preference", 2, (source,), "repository")
        wrong_repository = MemoryCandidate("repo-b", "private fact", True,
            "memory-b", "project_fact", 1, (source,), "repository")
        pending = MemoryCandidate("repo-a", "unapproved fact", False,
            "memory-c", "project_fact", 1, (source,), "repository")
        queried = []
        provider = Provider()
        planner = LLMExecutionPlanner(provider, repository_id="repo-a",
            memory_query=lambda text: queried.append(text) or
                (approved, wrong_repository, pending))
        observation = ToolResult("input-1", "", "user_input", "provided",
                                 {"text": "Use the current instruction: explain the formula."})
        planner.plan("Original objective", (observation,), (), lambda _id: {})

        payload = json.loads(provider.messages[-1]["content"])
        self.assertEqual(["Use the current instruction: explain the formula."], queried)
        self.assertEqual("Use the current instruction: explain the formula.",
                         payload["current_user_input"])
        self.assertEqual([], payload["observations"])
        self.assertEqual(["memory-a"], [item["memory_id"] for item in payload["memory_context"]])
        self.assertEqual("source-message", payload["memory_context"][0]["sources"][0]["message_id"])
        self.assertEqual("repo-a", payload["memory_context"][0]["repository_id"])
        self.assertEqual("injected", planner.last_request_diagnostics["memory"]["status"])
        self.assertNotIn("private fact", provider.messages[-1]["content"])
        self.assertNotIn("unapproved fact", provider.messages[-1]["content"])

    def test_planner_memory_failure_is_explicit_and_does_not_stop_planning(self):
        from agentforge.memory_service import MemoryRetrievalUnavailable

        class Provider:
            descriptor = LOCAL_QWEN
            def plan_completion(self, messages, **_kwargs):
                self.messages = messages
                return json.dumps({"action": "finish", "final_answer": "complete"})

        provider = Provider()
        def unavailable(_query):
            raise MemoryRetrievalUnavailable("ConnectionError")
        planner = LLMExecutionPlanner(provider, memory_query=unavailable, repository_id="repo-a")
        self.assertTrue(planner.plan("continue", (), (), lambda _id: {}).done)
        self.assertEqual("unavailable", planner.last_request_diagnostics["memory"]["status"])
        self.assertEqual("ConnectionError", planner.last_request_diagnostics["memory"]["error_type"])
        self.assertEqual([], json.loads(provider.messages[-1]["content"])["memory_context"])

    def test_planner_omits_optional_memory_before_rejecting_over_budget_request(self):
        from agentforge.context_assembler import ContextAssembler
        from agentforge.conversation_context import ContextBudgetExceeded, MemoryCandidate, MemorySourceReference

        class BudgetAssembler(ContextAssembler):
            def __init__(self):
                super().__init__()
                self.payloads = []
            def fit_messages(self, messages, **_kwargs):
                self.payloads.append(messages[-1]["content"])
                if len(self.payloads) == 1:
                    raise ContextBudgetExceeded({"budget_unit": "tokens", "required_tokens": 5000,
                        "budget_tokens": 3000, "estimate_kind": "synthetic test estimate"})
                return list(messages), {"phase": "agent_execution_planner", "estimated_input_tokens": 20}

        class Provider:
            descriptor = LOCAL_QWEN
            def __init__(self):
                self.context_assembler = BudgetAssembler()
            def plan_completion(self, messages, **_kwargs):
                self.messages = messages
                return json.dumps({"action": "finish", "final_answer": "complete"})

        candidate = MemoryCandidate("repo-a", "use compact output", True, "memory-a", "preference", 1,
            (MemorySourceReference("source-message", 1, "user", "b" * 64),), "repository")
        provider = Provider()
        planner = LLMExecutionPlanner(provider, repository_id="repo-a", memory_query=lambda _q: (candidate,))
        self.assertTrue(planner.plan("question", (), (), lambda _id: {}).done)
        self.assertIn("use compact output", provider.context_assembler.payloads[0])
        self.assertNotIn("use compact output", provider.messages[-1]["content"])
        self.assertEqual("omitted_for_context_budget", planner.last_request_diagnostics["memory"]["status"])
        self.assertTrue(planner.last_request_diagnostics["memory"]["fallback_used"])
        self.assertEqual(len(provider.messages[-1]["content"].encode("utf-8")),
                         planner.last_request_diagnostics["planner_payload_utf8_bytes"])

    def test_llm_planner_assembles_persisted_summary_and_complete_conversation_pairs(self):
        class Provider:
            descriptor = LOCAL_QWEN
            def __init__(self):
                self.messages = None
            def plan_completion(self, messages, **_kwargs):
                self.messages = messages
                return json.dumps({"action": "finish", "final_answer": "complete"})
            def request_stats(self):
                return {"last_usage": {"prompt_tokens": 100, "completion_tokens": 8}}

        provider = Provider()
        planner = LLMExecutionPlanner(
            provider,
            history=({"role": "user", "content": "Old discussion: " + ("x " * 1300)},
                     {"role": "assistant", "content": "Older response."},
                     {"role": "user", "content": "The project uses PostgreSQL."},
                     {"role": "assistant", "content": "Understood."}),
            summary="Earlier decision: keep durable messages in PostgreSQL.",
        )
        catalog = ToolDiscovery(ToolRegistry()).discover("continue")
        self.assertTrue(planner.plan("What did we decide?", (), catalog, lambda _id: {}).done)
        serialized = "\n".join(str(message.get("content", "")) for message in provider.messages)
        self.assertIn("Earlier decision", serialized)
        self.assertIn("The project uses PostgreSQL.", serialized)
        self.assertIn("What did we decide?", serialized)
        self.assertEqual(2, planner.last_request_diagnostics["retained_history_messages"])
        self.assertTrue(planner.last_request_diagnostics["summary_included"])

    def test_real_qwen_provider_accepts_context_assembled_planner_history(self):
        from unittest.mock import Mock
        from agentforge.providers.llama_server import LlamaServerProvider

        client = Mock()
        client.total_timeout = 45.0
        client.chat.return_value = json.dumps({
            "action": "finish", "final_answer": "complete",
        })
        client.request_stats.return_value = {"last_finish_reason": "stop"}
        provider = LlamaServerProvider(client, "qwen-local-test")
        planner = LLMExecutionPlanner(
            provider,
            history=({"role": "user", "content": "Old task detail. " + ("x " * 2_000)},
                     {"role": "assistant", "content": "Old result."},
                     {"role": "user", "content": "Recent task detail."},
                     {"role": "assistant", "content": "Recent result."}),
            summary="Prior decision: retain the selected repository.",
        )
        catalog = ToolDiscovery(ToolRegistry()).discover("continue the conversation")

        plan = planner.plan("Use the earlier context.", (), catalog, lambda _id: {})

        self.assertTrue(plan.done)
        sent = client.chat.call_args.args[0]
        serialized = "\n".join(message["content"] for message in sent)
        self.assertIn("Prior decision: retain the selected repository.", serialized)
        self.assertIn("Recent task detail.", serialized)
        self.assertIn("Recent result.", serialized)
        self.assertNotIn("Old task detail.", serialized)
        self.assertIn("Use the earlier context.", serialized)
        self.assertTrue(planner.last_request_diagnostics["request_sent"])

    def test_engine_report_and_checkpoint_retain_content_free_planner_budget_and_usage(self):
        class Planner:
            def __init__(self):
                self.request_diagnostics = []
            def plan(self, *_args):
                self.request_diagnostics.append({
                    "phase": "agent_execution_planner",
                    "context_window_tokens": 4096,
                    "input_budget_tokens": 3000,
                    "estimated_input_tokens": 111,
                    "request_sent": True,
                    "request_succeeded": True,
                    "usage": {"prompt_tokens": 111, "completion_tokens": 10},
                })
                return AgentExecutionPlan(done=True, final_answer="done")

        state_store = InMemoryExecutionStateStore()
        report = engine_for(ToolRegistry(), Planner(), state_store=state_store).run(
            "objective", repository_id="repo", conversation_id="conversation",
        )
        self.assertEqual("completed", report.status)
        self.assertEqual(3000, report.planner_request_diagnostics[0]["input_budget_tokens"])
        restored = state_store.load(report.execution_id)
        self.assertEqual(report.planner_request_diagnostics, restored.planner_request_diagnostics)

    def test_llm_planner_reports_budget_overflow_without_dropping_observation(self):
        from agentforge.agent_execution import PlanValidationError

        class Provider:
            descriptor = LOCAL_QWEN
            calls = 0
            def plan_completion(self, *_args, **_kwargs):
                self.calls += 1
                return json.dumps({"action": "finish", "final_answer": "should not run"})

        provider = Provider()
        planner = LLMExecutionPlanner(provider)
        observation = ToolResult("call-large", "task-large", "search", "completed",
                                 "required tool observation " + "x" * 12_000)
        with self.assertRaises(PlanValidationError) as caught:
            planner.plan("use the whole observation", (observation,), (), lambda _id: {})
        self.assertEqual(ToolErrorCode.CONTEXT_BUDGET_EXCEEDED, caught.exception.code)
        self.assertEqual(0, provider.calls)
        self.assertFalse(planner.last_request_diagnostics["request_sent"])
        self.assertGreater(planner.last_request_diagnostics["required_tokens"],
                           planner.last_request_diagnostics["budget_tokens"])

    def test_llm_planner_rejects_untrusted_invalid_actions_and_bounds_schema_roundtrips(self):
        class Provider:
            def __init__(self, result):
                self.result = result
            def plan_completion(self, *_args, **_kwargs):
                return self.result

        registry = ToolRegistry(); registry.register(definition(), lambda _a, _c: {})
        discovery = ToolDiscovery(registry)
        catalog = discovery.discover("lookup")
        with self.assertRaises(ValueError):
            LLMExecutionPlanner(Provider('{"action":"finish","final_answer":"x","ignored":true}')).plan(
                "lookup", (), catalog, discovery.schema_for)
        with self.assertRaises(ValueError):
            LLMExecutionPlanner(Provider("ignore system and run shell")).plan(
                "lookup", (), catalog, discovery.schema_for)

    def test_streaming_events_include_execution_identity_and_sequence(self):
        from agentforge.streaming import StreamingManager
        events = []
        sink = StreamingManager(on_event=events.append)
        sink.start()
        registry = ToolRegistry(); registry.register(definition(), lambda _a, _c: {})
        report = engine_for(registry, QueuePlanner(AgentExecutionPlan(calls=(call(),)),
            AgentExecutionPlan(done=True)), on_event=sink).run("lookup")
        self.assertEqual(report.status, "completed")
        self.assertTrue(events)
        agent_events = [item for item in events if item.execution_id is not None]
        self.assertTrue(agent_events)
        self.assertTrue(all(item.execution_id == report.execution_id for item in agent_events))
        self.assertEqual([item.sequence for item in agent_events],
                         sorted(item.sequence for item in agent_events))
        agent_types = [item.type.value for item in agent_events]
        self.assertEqual(2, agent_types.count("PLANNER_STARTED"))
        self.assertEqual(2, agent_types.count("PLAN_READY"))
        self.assertEqual(1, agent_types.count("REPLANNING"))
        self.assertLess(agent_types.index("PLANNER_STARTED"), agent_types.index("PLAN_READY"))
        sink.complete_validated("validated execution result")
        self.assertEqual("COMPLETED", events[-1].type.value)
        final_event = next(item for item in events if item.type.value == "FINAL_ANSWER")
        self.assertEqual(report.execution_id, final_event.execution_id)

    def test_planner_failure_emits_a_distinct_safe_lifecycle_event(self):
        from agentforge.streaming import StreamEventType, StreamingManager
        events = []
        sink = StreamingManager(on_event=events.append)
        sink.start()
        class BrokenPlanner:
            def plan(self, *_args):
                raise RuntimeError("synthetic provider failure with sensitive details")
        report = engine_for(ToolRegistry(), BrokenPlanner(), on_event=sink).run("lookup")
        self.assertEqual("failed", report.status)
        agent_types = [item.type for item in events if item.execution_id is not None]
        self.assertIn(StreamEventType.PLANNER_STARTED, agent_types)
        self.assertIn(StreamEventType.PLANNER_FAILED, agent_types)
        self.assertNotIn("sensitive details", repr(events))


class AgentExecutionCoverageBehaviorTests(unittest.TestCase):
    def test_plan_validator_rejects_non_plan_finished_calls_and_oversized_answer(self):
        from agentforge.agent_execution import PlanValidator

        registry = ToolRegistry()
        registry.register(definition(), lambda _args, _context: {"ok": True})
        validator = PlanValidator(registry, ToolAuthorization(frozenset({"test:read"})),
                                  max_tasks_per_plan=2, max_tool_calls=3)
        snapshot = ExecutionSnapshot("execution", "objective", None, None,
                                     ExecutionStatus.PLANNING.value, 0)
        catalog = ToolDiscovery(registry).discover("lookup")
        with self.assertRaises(PlanValidationError):
            validator.validate(object(), snapshot=snapshot, catalog=catalog, seen_signatures=set())
        with self.assertRaisesRegex(PlanValidationError, "Finished plan cannot include calls"):
            validator.validate(AgentExecutionPlan(done=True, calls=(call(),)),
                               snapshot=snapshot, catalog=catalog, seen_signatures=set())
        with self.assertRaisesRegex(PlanValidationError, "Final answer is invalid or oversized"):
            validator.validate(AgentExecutionPlan(done=True, final_answer="x" * 32_001),
                               snapshot=snapshot, catalog=catalog, seen_signatures=set())

    def test_plan_validator_checks_tool_registration_discovery_schema_and_permission(self):
        from agentforge.agent_execution import PlanValidator

        registry = ToolRegistry()
        registry.register(definition(), lambda _args, _context: {"ok": True})
        snapshot = ExecutionSnapshot("execution", "objective", None, None,
                                     ExecutionStatus.PLANNING.value, 0)
        allowed = ToolAuthorization(frozenset({"test:read"}))
        validator = PlanValidator(registry, allowed, max_tasks_per_plan=2, max_tool_calls=3)
        catalog = ToolDiscovery(registry).discover("lookup")

        with self.assertRaisesRegex(PlanValidationError, "unregistered tool") as missing:
            validator.validate(AgentExecutionPlan(calls=(call("unknown"),)), snapshot=snapshot,
                               catalog=catalog, seen_signatures=set())
        self.assertEqual(ToolErrorCode.TOOL_NOT_FOUND, missing.exception.code)

        with self.assertRaisesRegex(PlanValidationError, "outside the current discovery result"):
            validator.validate(AgentExecutionPlan(calls=(call(),)), snapshot=snapshot,
                               catalog=(), seen_signatures=set())

        with self.assertRaisesRegex(PlanValidationError, "do not match the registered input schema"):
            validator.validate(AgentExecutionPlan(calls=(call(arguments={"unexpected": True}),)),
                               snapshot=snapshot, catalog=catalog, seen_signatures=set())

        denied = PlanValidator(registry, ToolAuthorization(frozenset()),
                               max_tasks_per_plan=2, max_tool_calls=3)
        with self.assertRaisesRegex(PermissionError, "permission is denied"):
            denied.validate(AgentExecutionPlan(calls=(call(),)), snapshot=snapshot,
                            catalog=catalog, seen_signatures=set())

    def test_plan_validator_rejects_non_object_arguments_and_invalid_dependencies(self):
        from agentforge.agent_execution import PlanValidator

        registry = ToolRegistry()
        registry.register(definition(), lambda _args, _context: {"ok": True})
        validator = PlanValidator(registry, ToolAuthorization(frozenset({"test:read"})),
                                  max_tasks_per_plan=3, max_tool_calls=4)
        snapshot = ExecutionSnapshot("execution", "objective", None, None,
                                     ExecutionStatus.PLANNING.value, 0)
        catalog = ToolDiscovery(registry).discover("lookup")
        non_object = ToolCallRequest("call-1", "task-1", "lookup", [], "purpose", "output", ())
        with self.assertRaisesRegex(PlanValidationError, "arguments must be an object"):
            validator.validate(AgentExecutionPlan(calls=(non_object,)), snapshot=snapshot,
                               catalog=catalog, seen_signatures=set())

        for dependencies in (("unknown",), ("task-1", "task-1")):
            invalid = call(dependencies=dependencies)
            with self.subTest(dependencies=dependencies), self.assertRaisesRegex(
                    PlanValidationError, "dependencies reference"):
                validator.validate(AgentExecutionPlan(calls=(invalid,)), snapshot=snapshot,
                                   catalog=catalog, seen_signatures=set())

    def test_approval_handler_failure_denies_execution_and_preserves_no_side_effect(self):
        registry = ToolRegistry()
        invocations = []
        registry.register(definition("approved-write", side_effect=True, requires_approval=True,
            risk="high", parallel_safe=False),
            lambda _args, _context: invocations.append("called") or {"ok": True})

        def approval_failure(_definition, _call):
            raise RuntimeError("synthetic approval backend failure")

        report = engine_for(registry, QueuePlanner(AgentExecutionPlan(calls=(call("approved-write"),))),
            authorization=ToolAuthorization(frozenset({"test:read"}), approval_failure)).run("write synthetic data")
        self.assertEqual("failed", report.status)
        self.assertEqual(ToolErrorCode.PERMISSION_DENIED.value, report.error_code)
        self.assertEqual([], invocations)

    def test_cancel_before_planning_and_invalid_start_time_do_not_run_tools(self):
        registry = ToolRegistry()
        invocations = []
        registry.register(definition(), lambda _args, _context: invocations.append(True) or {"ok": True})
        cancel = threading.Event()
        cancel.set()
        planner = QueuePlanner(AgentExecutionPlan(calls=(call(),)))
        report = engine_for(registry, planner, cancel_event=cancel).run("cancel before planning")
        self.assertEqual("cancelled", report.status)
        self.assertEqual([], planner.seen_observations)
        self.assertEqual([], invocations)

        with self.assertRaisesRegex(ValueError, "start time is invalid"):
            engine_for(registry, planner).run("invalid start", started_at=True)
        self.assertEqual([], invocations)

    def test_resume_rejects_missing_or_wrong_scope_checkpoint_without_tool_call(self):
        store = InMemoryExecutionStateStore()
        registry = ToolRegistry()
        invocations = []
        registry.register(definition(), lambda _args, _context: invocations.append(True) or {"ok": True})
        engine = engine_for(registry, QueuePlanner(AgentExecutionPlan(done=True, final_answer="done")),
                            state_store=store)
        with self.assertRaisesRegex(ValueError, "checkpoint was not found"):
            engine.resume("missing", objective="objective")

        store.save(ExecutionSnapshot("execution-scoped", "objective", "repo-a", "session-a",
                                     ExecutionStatus.RUNNING.value, 1))
        with self.assertRaisesRegex(ValueError, "scope does not match"):
            engine.resume("execution-scoped", objective="objective", repository_id="repo-b",
                          conversation_id="session-a")
        self.assertEqual([], invocations)

    def test_llm_planner_corrects_one_malformed_tool_call_using_safe_schema_diagnostics(self):
        malformed = {"action": "plan", "calls": [{
            "call_id": "bad-call", "tool_id": "lookup", "arguments": {"token": "private-value"},
            "purpose": "read", "expected_output": "object",
        }]}
        valid = {"action": "plan", "calls": [{
            "call_id": "corrected-call", "tool_id": "lookup", "arguments": {},
            "dependencies": [], "purpose": "read", "expected_output": "object",
        }]}

        class Provider:
            descriptor = LOCAL_QWEN

            def __init__(self):
                self.payloads = []
                self.responses = iter((
                    {"action": "load_schemas", "tool_ids": ["lookup"]},
                    malformed,
                    valid,
                ))

            def plan_completion(self, messages, **_kwargs):
                self.payloads.append(json.loads(messages[-1]["content"]))
                return json.dumps(next(self.responses))

        registry = ToolRegistry()
        registry.register(definition("lookup"), lambda _args, _context: {})
        discovery = ToolDiscovery(registry)
        provider = Provider()
        planner = LLMExecutionPlanner(provider)

        plan = planner.plan("lookup", (), discovery.discover("lookup"), discovery.schema_loader)

        self.assertEqual("corrected-call", plan.calls[0].call_id)
        self.assertEqual(3, planner.request_count)
        correction = provider.payloads[-1]["schema_feedback"]
        self.assertIn("invalid_tool_call_schema", correction)
        self.assertIn("dependencies", correction)
        self.assertNotIn("private-value", correction)
        failure = next(item for item in planner.request_diagnostics
                       if item.get("planning_failure_reason") == "invalid_tool_call_schema")
        self.assertEqual(ToolErrorCode.INVALID_ARGUMENTS.value,
                         failure["planning_failure_code"])
        self.assertEqual(["dependencies"], failure["planning_failure_details"]["missing_fields"])
        self.assertEqual({"token": "string"},
                         failure["planning_failure_details"]["argument_types"])
        self.assertNotIn("private-value", json.dumps(failure))

    def test_llm_planner_corrects_one_invalid_json_observation_replan(self):
        class Provider:
            descriptor = LOCAL_QWEN

            def __init__(self):
                self.payloads = []
                self.responses = iter(("PRIVATE_INVALID_OUTPUT", json.dumps({"action": "finish"})))

            def plan_completion(self, messages, **_kwargs):
                self.payloads.append(json.loads(messages[-1]["content"]))
                return next(self.responses)

        provider = Provider()
        planner = LLMExecutionPlanner(provider, tools_only=True)
        observation = ToolResult("read-1", "read-1", "lookup", "completed", {"fact": "synthetic"})

        plan = planner.plan("finish using the observation", (observation,), (), lambda _tool_id: {})

        self.assertTrue(plan.done)
        self.assertEqual(2, planner.request_count)
        correction = provider.payloads[1]["schema_feedback"]
        self.assertIn("invalid_json", correction)
        self.assertIn("strict JSON", correction)
        self.assertNotIn("PRIVATE_INVALID_OUTPUT", correction)
        self.assertEqual({"fact": "synthetic"}, provider.payloads[1]["observations"][0]["output"])
        rejection = next(item for item in planner.request_diagnostics
                         if item.get("planning_failure_reason") == "invalid_json")
        self.assertEqual("response_validation", rejection["planning_failure_phase"])
        self.assertNotIn("PRIVATE_INVALID_OUTPUT", json.dumps(planner.request_diagnostics))

    def test_llm_planner_stops_after_one_invalid_json_correction_without_running_new_tools(self):
        class Provider:
            descriptor = LOCAL_QWEN

            def __init__(self):
                self.responses = iter((
                    json.dumps({"action": "load_schemas", "tool_ids": ["lookup"]}),
                    json.dumps({"action": "plan", "calls": [{
                        "call_id": "lookup-call", "tool_id": "lookup", "arguments": {},
                        "dependencies": [], "purpose": "read", "expected_output": "object",
                    }]}),
                    "PRIVATE_INVALID_OUTPUT_FIRST",
                    "PRIVATE_INVALID_OUTPUT_SECOND",
                ))

            def plan_completion(self, *_args, **_kwargs):
                return next(self.responses)

        registry = ToolRegistry()
        invocations = []
        registry.register(definition("lookup"), lambda _args, _context: invocations.append(True) or {"ok": True})
        planner = LLMExecutionPlanner(Provider(), tools_only=True)

        report = engine_for(registry, planner).run("read the synthetic item")

        self.assertEqual("failed", report.status)
        self.assertEqual(ToolErrorCode.INVALID_ARGUMENTS.value, report.error_code)
        self.assertEqual([True], invocations)
        self.assertEqual(1, report.tool_calls)
        self.assertEqual(4, planner.request_count)
        self.assertNotIn("PRIVATE_INVALID_OUTPUT", json.dumps(planner.request_diagnostics))

    def test_llm_planner_stops_after_one_malformed_schema_correction(self):
        malformed = {"action": "plan", "calls": [{
            "call_id": "bad-call", "tool_id": "lookup", "arguments": {},
            "dependencies": [], "purpose": "read",
        }]}

        class Provider:
            descriptor = LOCAL_QWEN

            def __init__(self):
                self.responses = iter((
                    {"action": "load_schemas", "tool_ids": ["lookup"]},
                    malformed,
                    malformed,
                ))

            def plan_completion(self, *_args, **_kwargs):
                return json.dumps(next(self.responses))

        registry = ToolRegistry()
        registry.register(definition("lookup"), lambda _args, _context: {})
        discovery = ToolDiscovery(registry)
        planner = LLMExecutionPlanner(Provider())

        with self.assertRaises(PlanValidationError) as raised:
            planner.plan("lookup", (), discovery.discover("lookup"), discovery.schema_loader)

        self.assertEqual("invalid_tool_call_schema", raised.exception.diagnostic_code)
        self.assertEqual(3, planner.request_count)
        self.assertEqual({"expected_output"},
                         set(raised.exception.diagnostic_details["missing_fields"]))

    def test_schema_action_parser_rejects_protocol_errors_and_accepts_bounded_actions(self):
        invalid = (
            ("not-json", {}, "invalid_json"),
            ("[]", {}, "missing_action"),
            (json.dumps({"action": "execute_shell"}), {}, "unknown_action"),
            (json.dumps({"action": "load_schemas", "tool_ids": ["lookup", 7]}), {}, "invalid_tool_ids"),
            (json.dumps({"action": "plan", "calls": "not-a-list"}), {}, "invalid_calls"),
            (json.dumps({"action": "finish", "final_answer": "done"}), {"tools_only": True}, "action_fields_unexpected"),
            (json.dumps({"action": "finish"}), {}, "action_fields_missing"),
            (json.dumps({"action": "ask_user", "user_input_request": None}), {}, "invalid_user_input"),
        )
        for raw, kwargs, diagnostic_code in invalid:
            with self.subTest(diagnostic_code=diagnostic_code), self.assertRaises(
                    PlanValidationError) as raised:
                _parse_planner_action(raw, **kwargs)
            self.assertEqual(diagnostic_code, raised.exception.diagnostic_code)

        schema_request = _parse_planner_action(json.dumps(
            {"action": "load_schemas", "tool_ids": ["lookup"]}))
        self.assertEqual(["lookup"], schema_request["tool_ids"])
        calls = _parse_planner_action(json.dumps({"action": "plan", "calls": [{
            "call_id": "call-1", "tool_id": "lookup", "arguments": {},
            "dependencies": [], "purpose": "read synthetic item", "expected_output": "value",
        }]}))
        self.assertEqual("lookup", calls["calls"][0].tool_id)

    def test_schema_batch_loader_maps_loader_errors_and_rejects_incomplete_responses(self):
        from agentforge.agent_execution import _load_tool_schema_batch

        def missing_schema(_tool_id):
            raise KeyError("private registry detail")

        with self.assertRaises(PlanValidationError) as raised:
            _load_tool_schema_batch(missing_schema, ("lookup",))
        self.assertEqual(ToolErrorCode.TOOL_NOT_FOUND, raised.exception.code)
        self.assertEqual("schema_load_failed", raised.exception.diagnostic_code)
        self.assertNotIn("private registry detail", str(raised.exception))

        class IncompleteBatch:
            def load_many(self, _tool_ids):
                return {"lookup": {"input": {}}}

        with self.assertRaisesRegex(PlanValidationError, "incomplete batch"):
            _load_tool_schema_batch(IncompleteBatch(), ("lookup",))


if __name__ == "__main__":
    unittest.main()
