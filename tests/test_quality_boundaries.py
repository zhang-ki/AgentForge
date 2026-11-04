"""隔离性、证据边界和 Agent 预算的离线回归检查。"""

import unittest

from agentforge.agent_loop import AgentLoop, ReadOnlyToolbox, ToolCall
from agentforge.indexer import index_repository
from agentforge.store import IndexStore
from tests.support import LocalTestCase, snapshot


class SequencePlanner:
    def __init__(self, calls):
        self.calls = calls

    def next_call(self, objective, audit):
        return self.calls[len(audit)] if len(audit) < len(self.calls) else None


class QualityBoundaryTests(LocalTestCase):
    def test_exact_step_budget_completes_without_an_extra_tool(self):
        self.write("module.py", "def marker():\n    return 7\n")
        before = snapshot(self.root)
        calls = [
            ToolCall("index", {"repository": str(self.root)}, "test"),
            ToolCall("search", {"query": "marker"}, "test"),
        ]
        with IndexStore(":memory:") as store:
            report = AgentLoop(ReadOnlyToolbox(self.root, store), SequencePlanner(calls),
                               max_steps=2).run("marker")
        self.assertEqual(report.status, "completed")
        self.assertEqual(report.completed_steps, 2)
        self.assertEqual([r.name for r in report.audit], ["index", "search"])
        self.assertEqual([r.status for r in report.audit], ["ok", "ok"])
        self.assertEqual(snapshot(self.root), before)

    def test_real_unicode_crlf_evidence_and_boundary_ranges(self):
        self.write("notes.py", "# 第一行\r\n\r\ndef café():\r\n    return '雪'\r\n".encode("utf-8"))
        with IndexStore(":memory:") as store:
            toolbox = ReadOnlyToolbox(self.root, store)
            first = toolbox.execute(ToolCall("read", {"path": "notes.py", "start_line": 1, "end_line": 2}, "test"))
            last = toolbox.execute(ToolCall("read", {"path": "notes.py", "start_line": 3, "end_line": 4}, "test"))
            self.assertEqual(first["text"], "# 第一行\n")
            self.assertEqual(last["text"], "def café():\n    return '雪'")
            self.assertEqual((last["start_line"], last["end_line"]), (3, 4))
            for start, end in ((0, 1), (4, 3), (1, 5)):
                with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                    toolbox.execute(ToolCall("read", {"path": "notes.py", "start_line": start, "end_line": end}, "test"))

    def test_separate_repositories_and_memory_indexes_do_not_share_results(self):
        self.write("alpha.py", "def alpha_marker():\n    return 1\n")
        other = self.sandbox / "other"
        other.mkdir()
        (other / "beta.py").write_text("def beta_marker():\n    return 2\n", encoding="utf-8")
        with IndexStore(":memory:") as first, IndexStore(":memory:") as second:
            index_repository(self.root, first)
            index_repository(other, second)
            self.assertTrue(first.search_symbols(name="alpha_marker"))
            self.assertFalse(first.search_symbols(name="beta_marker"))
            self.assertTrue(second.search_symbols(name="beta_marker"))
            self.assertFalse(second.search_symbols(name="alpha_marker"))

    def test_forbidden_tool_failure_is_audited_without_followup(self):
        self.write("safe.py", "value = 1\n")
        before = snapshot(self.root)
        calls = [ToolCall("shell", {"command": "ignored"}, "untrusted"),
                 ToolCall("read", {"path": "safe.py"}, "untrusted")]
        with IndexStore(":memory:") as store:
            report = AgentLoop(ReadOnlyToolbox(self.root, store), SequencePlanner(calls),
                               max_steps=2).run("inspect")
        self.assertEqual(report.status, "failed")
        self.assertEqual(len(report.audit), 1)
        self.assertEqual((report.audit[0].name, report.audit[0].status), ("shell", "rejected"))
        self.assertIn("forbidden", report.audit[0].error)
        self.assertEqual(snapshot(self.root), before)


if __name__ == "__main__":
    unittest.main()
