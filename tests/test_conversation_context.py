import unittest
import hashlib

from agentforge.conversation_context import (
    ConversationTurn, MemoryCandidate, MemorySourceReference, plan_context,
)


class ContextPlanTests(unittest.TestCase):
    def test_fifty_turn_history_keeps_approved_preference_and_complete_pairs(self):
        turns = tuple(ConversationTurn("c1", "r1", i + 1,
                      "user" if i % 2 == 0 else "assistant",
                      ("问题" if i % 2 == 0 else "回答") + str(i) + "内容" * 20)
                      for i in range(100))
        memory = self.memory("请优先使用中文回答")
        plan = self.base(history=turns, memories=(memory,), budget_bytes=3325)
        self.assertEqual(("m1",), plan.memory_ids)
        self.assertEqual(0, len(plan.retained_history_sequences) % 2)
        self.assertEqual(100, plan.retained_history_sequences[-1])
        self.assertLessEqual(plan.bytes_used, 3325)
        self.assertIn("请优先使用中文回答", str(plan.messages))

    def test_approved_memory_has_priority_over_optional_history(self):
        turns = (ConversationTurn("c1", "r1", 1, "user", "q" * 150),
                 ConversationTurn("c1", "r1", 2, "assistant", "a" * 150))
        memory = self.memory("approved project preference")
        base = self.base().bytes_used
        memory_cost = self.base(memories=(memory,)).bytes_used - base
        history_cost = self.base(history=turns).bytes_used - base
        budget = base + max(memory_cost, history_cost) + 1
        plan = self.base(history=turns, memories=(memory,), budget_bytes=budget)
        self.assertEqual(("m1",), plan.memory_ids)
        self.assertEqual(2, plan.dropped_turns)

    def test_user_preference_is_not_discarded_under_code_query_budget(self):
        from agentforge.conversation_facts import extract_message_facts
        rows = [{"id": f"m{i}", "sequence": i, "role": "user",
                 "content": f"Project fact unrelated{i} " + "x" * 100 + "."}
                for i in range(1, 13)]
        rows.append({"id": "pref", "sequence": 13, "role": "user",
                     "content": "I prefer responses in Chinese."})
        plan = plan_context(conversation_id="c1", repository_id="r1", system="trusted",
                            question="Which caller calls launch_job?",
                            evidence="[E1] callsite\n[E2] definition " + "x" * 1900,
                            protected_facts=extract_message_facts(rows), budget_bytes=3500)
        self.assertIn("prefer responses in Chinese", str(plan.messages))

    def test_cross_file_budget_retains_both_evidence_and_relevant_constraint(self):
        from agentforge.conversation_facts import extract_message_facts
        rows = [{"id": f"m{i}", "sequence": i, "role": "user",
                 "content": f"Project fact unrelated{i} " + "x" * 100 + "."}
                for i in range(1, 13)]
        rows.append({"id": "constraint", "sequence": 13, "role": "user",
                     "content": "Constraint: never invent source citations."})
        ledger = extract_message_facts(rows)
        evidence = "[E1] app.py:1-2\nreturn launch_job(item)\n" + "# " + "x" * 1900 + "\n[E2] pkg/impl.py:1-2\ndef start_job(item):\n    return item"
        args = dict(conversation_id="c1", repository_id="r1", system="trusted",
                    question="Which caller calls launch_job and where is its implementation?",
                    evidence=evidence, protected_facts=ledger)
        baseline = plan_context(**args, budget_bytes=12000)
        plan = plan_context(**args, budget_bytes=3500)
        content = "\n".join(value for _, value in plan.messages)
        self.assertIn("[E1] app.py:1-2", content)
        self.assertIn("[E2] pkg/impl.py:1-2", content)
        self.assertIn("never invent source citations", content)
        self.assertNotIn("unrelated1 ", content)
        self.assertLessEqual(plan.bytes_used, 3500)
        self.assertGreater(baseline.bytes_used, 3500)
        self.assertEqual(12, plan.omitted_fact_count)
        self.assertEqual(plan.bytes_used, sum(dict(plan.byte_diagnostics).values()))

    def base(self, **kwargs):
        return plan_context(conversation_id="c1", repository_id="r1", system="trusted system",
                            question="它是什么？", evidence="[E1] source", **kwargs)

    def memory(self, content="approved", *, approved=True, repository_id="r1", memory_id="m1", scope="repository"):
        return MemoryCandidate(repository_id, content, approved, memory_id,
            "preference", 2, (MemorySourceReference("message-1", 3, "user",
                hashlib.sha256(b"source message").hexdigest()),), scope)

    def test_recent_complete_turns_are_structured_and_ordered(self):
        turns = (ConversationTurn("c1", "r1", 1, "user", "Conv2d?"),
                 ConversationTurn("c1", "r1", 2, "assistant", "layer [E1]"))
        plan = self.base(history=turns)
        self.assertEqual([r for r, _ in plan.messages], ["system", "user", "assistant", "user"])
        self.assertIn("它是什么？", plan.messages[-1][1])
        self.assertEqual(plan.dropped_turns, 0)

    def test_cross_session_and_repository_fail_closed(self):
        for turn in (ConversationTurn("c2", "r1", 1, "user", "secret"),
                     ConversationTurn("c1", "r2", 1, "user", "secret")):
            with self.subTest(turn=turn), self.assertRaisesRegex(ValueError, "scope"):
                self.base(history=(turn,))
        with self.assertRaisesRegex(ValueError, "different repository"):
            self.base(memories=(self.memory("secret", repository_id="r2"),))

    def test_budget_keeps_complete_recent_turns_and_no_partial_history(self):
        turns = (ConversationTurn("c1", "r1", 1, "user", "a" * 100),
                 ConversationTurn("c1", "r1", 2, "assistant", "b" * 100),
                 ConversationTurn("c1", "r1", 3, "user", "new"),
                 ConversationTurn("c1", "r1", 4, "assistant", "reply"))
        plan = self.base(history=turns, budget_bytes=160)
        self.assertEqual(plan.dropped_turns, 2)
        self.assertIn(("user", "new"), plan.messages)
        self.assertLessEqual(plan.bytes_used, 160)
        with self.assertRaisesRegex(ValueError, "exceed"):
            self.base(budget_bytes=5)

    def test_failed_and_incomplete_turns_not_reused(self):
        turns = (ConversationTurn("c1", "r1", 1, "user", "q"),
                 ConversationTurn("c1", "r1", 2, "assistant", "partial", "failed"))
        self.assertEqual(self.base(history=turns).dropped_turns, 2)

    def test_only_approved_memory_in_same_repository(self):
        plan = self.base(memories=(self.memory("approved"),
                                   self.memory("unapproved", approved=False, memory_id="m2")))
        text = str(plan.messages)
        self.assertIn("approved", text)
        self.assertNotIn("unapproved", text)
        self.assertNotIn(("system", "approved"), plan.messages)
        self.assertEqual(("m1",), plan.memory_ids)
        injected = next(content for role, content in plan.messages if role == "user" and "Memory ID" in content)
        self.assertIn("Memory ID: m1", injected)
        self.assertIn("message_id=message-1", injected)
        self.assertIn("sequence=3", injected)
        self.assertIn("sha256=" + hashlib.sha256(b"source message").hexdigest(), injected)

    def test_explicit_global_memory_is_context_only_and_still_current_repository_bound(self):
        candidate = self.memory(scope="global")
        plan = self.base(memories=(candidate,))
        self.assertIn("Scope: global", "\n".join(content for _role, content in plan.messages))
        self.assertEqual((candidate.memory_id,), plan.memory_ids)

    def test_approved_memory_without_identity_and_source_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "provenance"):
            self.base(memories=(MemoryCandidate("r1", "approved", True),))

    def test_cli_audit_metadata_contains_only_memories_really_injected(self):
        from agentforge.cli import _injected_memory_metadata
        injected = self.memory("do not echo in audit metadata")
        omitted = self.memory("too large for context", memory_id="m2")
        conversation = {"injected_memory_ids": ("m1",), "memories": (injected, omitted)}
        rows = _injected_memory_metadata(conversation)
        self.assertEqual(1, len(rows))
        self.assertEqual("m1", rows[0]["memory_id"])
        self.assertEqual("message-1", rows[0]["sources"][0]["message_id"])
        self.assertNotIn("content", rows[0])
        self.assertNotIn("do not echo", str(rows))

    def test_summary_is_not_silently_dropped_to_keep_recent_messages(self):
        turns = (ConversationTurn("c1", "r1", 1, "user", "x" * 50),
                 ConversationTurn("c1", "r1", 2, "assistant", "y" * 50))
        plan = self.base(history=turns, summary="important earlier fact", budget_bytes=190)
        self.assertIn("important earlier fact", str(plan.messages))
        self.assertEqual(plan.dropped_turns, 2)
        with self.assertRaisesRegex(ValueError, "summary"):
            self.base(summary="z" * 1000, budget_bytes=150)

    def test_recent_source_linked_facts_survive_when_history_turn_is_outside_budget(self):
        from agentforge.conversation_facts import extract_message_facts
        rows = [{"id": "message-recent", "sequence": 19, "role": "user",
                 "content": "Open task GAMMA953."}]
        ledger = extract_message_facts(rows)
        history = (ConversationTurn("c1", "r1", 17, "user", "older question " + "x" * 400),
                   ConversationTurn("c1", "r1", 18, "assistant", "older answer " + "y" * 400))
        plan = self.base(history=history, summary="Older semantic narrative.",
                         protected_facts=ledger, budget_bytes=600)
        joined = "\n".join(content for _role, content in plan.messages)
        self.assertIn("Open task GAMMA953.", joined)
        self.assertIn("message_id=message-recent", joined)
        self.assertEqual((ledger[0]["fact_id"],), plan.protected_fact_ids)
        self.assertEqual(("message-recent",), plan.protected_fact_source_message_ids)
        self.assertNotIn(("user", history[0].content), plan.messages)
        self.assertEqual((), plan.retained_history_sequences)
        self.assertLessEqual(plan.bytes_used, 600)

    def test_source_linked_fact_context_fails_closed_if_fact_exceeds_budget(self):
        from agentforge.conversation_facts import extract_message_facts
        ledger = extract_message_facts([{"id": "m1", "sequence": 1, "role": "user",
                                        "content": "Project fact GAMMA953."}])
        with self.assertRaisesRegex(ValueError, r"Protected conversation facts.*base_bytes=\d+, fact_bytes=\d+, budget_bytes=240"):
            self.base(protected_facts=ledger, budget_bytes=240)

    def test_invalid_budget_empty_summary_and_oversized_memory(self):
        for value in (True, 0, 1_000_001, "120"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.base(budget_bytes=value)
        with self.assertRaisesRegex(ValueError, "summary"):
            self.base(summary="  ")
        plan = self.base(memories=(self.memory("x" * 500),), budget_bytes=150)
        self.assertNotIn("x" * 500, str(plan.messages))
        self.assertEqual((), plan.memory_ids)

    def test_bad_roles_and_out_of_order_messages_rejected(self):
        with self.assertRaises(ValueError):
            self.base(history=(ConversationTurn("c1", "r1", 1, "system", "bad"),))
        with self.assertRaises(ValueError):
            self.base(history=(ConversationTurn("c1", "r1", 2, "user", "a"),
                               ConversationTurn("c1", "r1", 1, "assistant", "b")))
