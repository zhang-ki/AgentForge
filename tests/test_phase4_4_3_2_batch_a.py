"""Behavioral coverage for bounded tool registration, planning, and replanning payloads."""

from dataclasses import replace
import math
import unittest

from agentforge.agent_execution import (
    AgentExecutionPlan,
    ExecutionSnapshot,
    ExecutionStatus,
    PlanValidationError,
    PlanValidator,
    ToolAuthorization,
    ToolCallRequest,
    ToolDefinition,
    ToolDiscovery,
    ToolErrorCode,
    ToolRegistry,
    _compact_replanning_output,
)


def _definition(**changes):
    value = ToolDefinition(
        tool_id="synthetic.lookup",
        name="Synthetic lookup",
        description="Read a synthetic value for acceptance tests.",
        version="1",
        capabilities=frozenset({"synthetic.lookup"}),
        input_schema={"type": "object", "properties": {}, "required": [],
                      "additionalProperties": False},
        output_schema={"type": "object"},
        permission_scope="synthetic:read",
    )
    return replace(value, **changes)


def _call(arguments=None, *, tool_id="synthetic.lookup", task_id="task-1", dependencies=()):
    return ToolCallRequest(
        call_id="call-1", task_id=task_id, tool_id=tool_id,
        arguments={} if arguments is None else arguments,
        purpose="Read the synthetic value", expected_output="A validated JSON object",
        dependencies=tuple(dependencies),
    )


class ToolRegistryBoundaryBehaviorTests(unittest.TestCase):
    def test_registration_rejects_unsafe_definitions_without_publishing_partial_state(self):
        base = _definition()
        invalid = (
            (replace(base, tool_id="bad tool"), "unsupported characters"),
            (replace(base, capabilities=frozenset()), "capabilities"),
            (replace(base, capabilities=frozenset({"  "})), "capabilities"),
            (replace(base, risk_level="unknown"), "risk level"),
            (replace(base, side_effect=1), "safety flags"),
            (replace(base, parallel_safe="yes"), "parallel-safety"),
            (replace(base, worker_safe=None), "worker-safety"),
            (replace(base, side_effect=True, permission_scope=""), "permission_scope"),
            (replace(base, timeout=True), "timeout"),
            (replace(base, timeout=121), "timeout"),
            (replace(base, max_retries=True), "retry limit"),
            (replace(base, max_retries=3), "retry limit"),
            (replace(base, idempotent=False, max_retries=1), "Only idempotent"),
            (replace(base, side_effect=True, idempotent=False, max_retries=1), "Only idempotent"),
            (replace(base, input_schema={"type": "string"}), "argument object"),
            (replace(base, risk_level="critical"), "require explicit approval"),
        )
        for definition, message in invalid:
            with self.subTest(message=message):
                registry = ToolRegistry()
                with self.assertRaisesRegex(ValueError, message):
                    registry.register(definition, lambda _args, _context: {"ok": True})
                self.assertEqual((), registry.definitions())
                self.assertIsNone(registry.handler(definition.tool_id))

    def test_registration_validates_nested_json_schema_and_rejects_unsafe_shapes(self):
        valid = {
            "type": "object",
            "properties": {
                "options": {
                    "type": "array", "minItems": 1, "maxItems": 3,
                    "items": {"type": "object", "properties": {
                        "mode": {"type": "string", "enum": ["fast", "safe"]},
                    }, "required": ["mode"], "additionalProperties": False},
                },
            },
            "required": ["options"], "additionalProperties": False,
        }
        registry = ToolRegistry()
        registry.register(_definition(input_schema=valid), lambda _args, _context: {"ok": True})
        self.assertEqual("synthetic.lookup", registry.get_many(["synthetic.lookup"])["synthetic.lookup"].tool_id)

        nested = {"type": "string"}
        for _ in range(13):
            nested = {"type": "array", "items": nested}
        bad_schemas = (
            {"type": "object", "unexpected": True},
            {"type": "object", "properties": {"x": {"type": "string", "enum": []}}},
            {"type": "object", "properties": {"x": {"type": "integer", "minimum": 4, "maximum": 2}}},
            {"type": "object", "properties": {"x": {"type": "array"}}},
            {"type": "object", "required": ["missing"]},
            {"type": "object", "required": ["x", "x"]},
            {"type": "object", "additionalProperties": True},
            {"type": "object", "properties": {"x": nested}},
        )
        for schema in bad_schemas:
            with self.subTest(schema=schema):
                candidate = ToolRegistry()
                with self.assertRaises(ValueError):
                    candidate.register(_definition(input_schema=schema), lambda *_: {})
                self.assertEqual((), candidate.definitions())

    def test_schema_batches_are_deduplicated_atomic_and_defensive_copies(self):
        registry = ToolRegistry()
        registry.register(_definition(), lambda _args, _context: {"ok": True})
        self.assertEqual({"synthetic.lookup"}, set(registry.get_many(
            ["synthetic.lookup", "synthetic.lookup"])))
        schemas = registry.schemas_for(["synthetic.lookup", "synthetic.lookup"])
        schemas["synthetic.lookup"]["input"]["properties"]["corrupt"] = {"type": "string"}
        self.assertNotIn("corrupt", registry.schemas_for(["synthetic.lookup"])["synthetic.lookup"]["input"]["properties"])

        for values in ([], [f"tool-{index}" for index in range(51)], [""]):
            with self.subTest(values_count=len(values)), self.assertRaises(ValueError):
                registry.get_many(values)
        with self.assertRaises(KeyError):
            registry.get_many(["missing"])
        with self.assertRaises(ValueError):
            registry.schemas_for(["synthetic.lookup", ""])


class ToolArgumentContractBehaviorTests(unittest.TestCase):
    def setUp(self):
        self.schema = {
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": ["safe", "fast"], "minLength": 4, "maxLength": 4},
                "count": {"type": "integer", "minimum": 1, "maximum": 4},
                "labels": {"type": "array", "minItems": 1, "maxItems": 2,
                           "items": {"type": "string", "minLength": 2}},
                "enabled": {"type": "boolean"},
                "ratio": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                "nothing": {"type": "null"},
            },
            "required": ["mode", "count", "labels", "enabled", "ratio", "nothing"],
            "additionalProperties": False,
        }
        self.registry = ToolRegistry()
        self.invocations = []
        self.registry.register(_definition(input_schema=self.schema),
                               lambda args, _ctx: self.invocations.append(dict(args)) or {"ok": True})
        self.validator = PlanValidator(self.registry,
            ToolAuthorization(frozenset({"synthetic:read"})), max_tasks_per_plan=3, max_tool_calls=5)
        self.snapshot = ExecutionSnapshot("exec", "objective", None, None,
            ExecutionStatus.PLANNING.value, 0)
        self.catalog = ToolDiscovery(self.registry).discover("synthetic lookup")

    def _validate(self, arguments):
        return self.validator.validate(AgentExecutionPlan(calls=(_call(arguments),)),
            snapshot=self.snapshot, catalog=self.catalog, seen_signatures=set())

    def test_valid_nested_types_and_inclusive_bounds_are_accepted(self):
        self._validate({"mode": "safe", "count": 1, "labels": ["ok"], "enabled": False,
                        "ratio": 0.0, "nothing": None})
        self._validate({"mode": "fast", "count": 4, "labels": ["ab", "cd"], "enabled": True,
                        "ratio": 1.0, "nothing": None})
        self.assertEqual([], self.invocations, "validation must not execute a tool")

    def test_invalid_nested_arguments_are_rejected_before_execution(self):
        cases = (
            ({"mode": "unsafe", "count": 1, "labels": ["ok"], "enabled": True, "ratio": .5, "nothing": None}, "enum"),
            ({"mode": "safe", "count": True, "labels": ["ok"], "enabled": True, "ratio": .5, "nothing": None}, "integer excludes booleans"),
            ({"mode": "safe", "count": 5, "labels": ["ok"], "enabled": True, "ratio": .5, "nothing": None}, "upper numeric bound"),
            ({"mode": "safe", "count": 1, "labels": [], "enabled": True, "ratio": .5, "nothing": None}, "minimum array length"),
            ({"mode": "safe", "count": 1, "labels": ["x"], "enabled": True, "ratio": .5, "nothing": None}, "nested string minimum"),
            ({"mode": "safe", "count": 1, "labels": ["ok"], "enabled": True, "ratio": math.nan, "nothing": None}, "finite numeric values"),
            ({"mode": "safe", "count": 1, "labels": ["ok"], "enabled": True, "ratio": .5, "nothing": None, "extra": 1}, "additional properties"),
            ({"mode": "safe", "count": 1, "labels": ["ok"], "enabled": True, "ratio": .5}, "required properties"),
        )
        for args, case in cases:
            with self.subTest(case=case), self.assertRaises(PlanValidationError) as raised:
                self._validate(args)
            self.assertEqual(ToolErrorCode.INVALID_ARGUMENTS, raised.exception.code)
        self.assertEqual([], self.invocations)


class ReplanningPayloadBehaviorTests(unittest.TestCase):
    def test_search_replanning_keeps_identity_and_marks_omitted_candidates(self):
        output = {"query": "lookup", "route": "symbols", "uncertainty": "static",
                  "unavailable_channels": ["vector"],
                  "results": [
                      {"source_id": f"E{i}", "relative_path": f"src/f{i}.py", "start_line": i,
                       "end_line": i + 1, "excerpt": "large body must not be copied"}
                      for i in range(1, 6)
                  ]}
        compact = _compact_replanning_output("search", output)
        self.assertEqual(["E1", "E2", "E3"], [row["source_id"] for row in compact["results"]])
        self.assertTrue(compact["more_candidates_available"])
        self.assertEqual(["vector"], compact["unavailable_channels"])
        self.assertNotIn("excerpt", compact["results"][0])
        self.assertEqual(5, len(output["results"]), "planner compaction must not mutate stored evidence")

    def test_read_replanning_uses_complete_line_preview_and_content_digest(self):
        source = "line one\n" + "x" * 930 + "\nline after oversized line\n"
        compact = _compact_replanning_output("read", {
            "path": "src/synthetic.py", "start_line": 1, "end_line": 3, "text": source,
        })
        self.assertEqual("src/synthetic.py", compact["path"])
        self.assertEqual(1, compact["start_line"])
        self.assertTrue(compact["text_truncated"])
        self.assertIn("text_sha256", compact)
        self.assertIn("use the registered read tool", compact["text_preview"])
        self.assertNotIn("line after oversized line", compact["text_preview"])

    def test_generic_replanning_output_is_bounded_and_reports_non_json_values_safely(self):
        oversized = {"result_ref": "result://synthetic/one", "payload": "x" * 3_000}
        compact = _compact_replanning_output("compute", oversized)
        self.assertTrue(compact["truncated"])
        self.assertEqual("result://synthetic/one", compact["result_ref"])
        self.assertGreater(compact["output_bytes"], 2_000)
        self.assertEqual({"small": True}, _compact_replanning_output("compute", {"small": True}))
        opaque = _compact_replanning_output("compute", object())
        self.assertEqual({"output_type": "object", "unavailable": True}, opaque)


if __name__ == "__main__":
    unittest.main()
