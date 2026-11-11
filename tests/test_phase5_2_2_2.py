import json
import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from agentforge.embeddings.base import EmbeddingError
from agentforge.embeddings.chroma_index import ChromaCodeIndex
from agentforge.indexer import index_repository
from agentforge.product_retrieval import ProductRetriever
from agentforge.scanner import scan_repository
from agentforge.store import IndexStore

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEST_TEMP_ROOT = PROJECT_ROOT / ".tmp" / "phase5_2_2_3_r3_1"


class _BatchProvider:
    model_id = "phase5222-synthetic-embedding"
    dimension = 2

    def __init__(self, *, invalid_batch=False):
        self.batch_sizes = []
        self.invalid_batch = invalid_batch

    def embed(self, _text):
        return (1.0, 0.0)

    def embed_document(self, text):
        return self.embed(text)

    def embed_query(self, text):
        return self.embed(text)

    def embed_documents(self, texts):
        values = tuple(texts)
        self.batch_sizes.append(len(values))
        vectors = [(1.0, 0.0) for _ in values]
        return vectors[:-1] if self.invalid_batch else vectors


class _MemoryCollection:
    """Small Chroma contract fake; records are isolated to each test."""

    def __init__(self):
        self.name = "phase5222_test_collection"
        self.records = {}
        self.upsert_sizes = []
        self.get_calls = []

    def count(self):
        return len(self.records)

    def get(self, *, ids=None, where=None, include=(), limit=None, offset=0):
        self.get_calls.append(tuple(include))
        if ids is not None:
            selected = [(identity, self.records[identity]) for identity in ids
                        if identity in self.records]
        else:
            selected = list(self.records.items())
            conditions = where.get("$and", ()) if isinstance(where, dict) else ()
            selected = [(identity, record) for identity, record in selected
                        if all(record["metadata"].get(key) == value
                               for condition in conditions for key, value in condition.items())]
        if limit is not None:
            selected = selected[offset:offset + limit]
        else:
            selected = selected[offset:]
        output = {"ids": [identity for identity, _record in selected]}
        if "metadatas" in include:
            output["metadatas"] = [record["metadata"] for _identity, record in selected]
        if "embeddings" in include:
            output["embeddings"] = [record["embedding"] for _identity, record in selected]
        if "documents" in include:
            output["documents"] = [record["document"] for _identity, record in selected]
        return output

    def upsert(self, *, ids, embeddings, metadatas, documents=None):
        self.upsert_sizes.append(len(ids))
        documents = documents if documents is not None else [""] * len(ids)
        for identity, embedding, metadata, document in zip(ids, embeddings, metadatas, documents):
            self.records[identity] = {
                "embedding": tuple(embedding), "metadata": dict(metadata), "document": document,
            }

    def update(self, *, ids, embeddings=None, documents=None):
        for position, identity in enumerate(ids):
            record = self.records[identity]
            if embeddings is not None:
                record["embedding"] = tuple(embeddings[position])
            if documents is not None:
                record["document"] = documents[position]

    def delete(self, *, ids=None, where=None):
        if ids is not None:
            for identity in ids:
                self.records.pop(identity, None)
            return
        conditions = where.get("$and", ()) if isinstance(where, dict) else ()
        for identity, record in tuple(self.records.items()):
            if all(record["metadata"].get(key) == value
                   for condition in conditions for key, value in condition.items()):
                self.records.pop(identity, None)

    def query(self, *, query_embeddings, n_results, where, include):
        query = query_embeddings[0]
        conditions = where.get("$and", ())
        candidates = [(identity, record) for identity, record in self.records.items()
                      if all(record["metadata"].get(key) == value
                             for condition in conditions for key, value in condition.items())]
        candidates.sort(key=lambda pair: sum(a * b for a, b in zip(query, pair[1]["embedding"])),
                        reverse=True)
        candidates = candidates[:n_results]
        output = {"ids": [[identity for identity, _record in candidates]]}
        if "metadatas" in include:
            output["metadatas"] = [[record["metadata"] for _identity, record in candidates]]
        if "distances" in include:
            output["distances"] = [[1.0 - sum(a * b for a, b in zip(query, record["embedding"]))
                                     for _identity, record in candidates]]
        if "documents" in include:
            output["documents"] = [[record["document"] for _identity, record in candidates]]
        return output


class IndexPerformanceAndRetrievalTests(unittest.TestCase):
    def setUp(self):
        # Keep test data inside the writable project tree. Each TemporaryDirectory
        # call still creates a unique child so parallel test processes never share
        # a repository fixture.
        TEST_TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(
            prefix="agentforge-phase5222-", dir=str(TEST_TEMP_ROOT),
        )
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "repository"
        self.root.mkdir()

    def test_unchanged_repository_hot_path_does_not_read_or_reparse_files(self):
        source = self.root / "module.py"
        source.write_text("def compute_total(values):\n    return sum(values)\n", encoding="utf-8")
        with IndexStore(":memory:") as store:
            index_repository(self.root, store)
            with patch("agentforge.indexer.read_candidate", side_effect=AssertionError("hot path reread")):
                second = index_repository(self.root, store)
        self.assertTrue(second["index_unchanged"])
        self.assertEqual(0, second["files_read"])
        self.assertEqual(1, second["files_reused"])

    def test_utf8_bom_file_uses_disk_size_and_stays_fresh_after_indexing(self):
        decoded = "def calculate_total(amount):\r\n    return amount + 5\r\n"
        raw = b"\xef\xbb\xbf" + decoded.encode("utf-8")
        (self.root / "bom_module.py").write_bytes(raw)

        with IndexStore(":memory:") as store:
            first = index_repository(self.root, store)
            indexed = store.file_snapshot()[0]
            report = scan_repository(self.root)
            outcome = ProductRetriever(
                store, self.root, scan_report=report,
            ).search("What does calculate_total return?", top_k=3)
            second = index_repository(self.root, store, scan_report=report)

            self.assertEqual(len(raw), indexed["size_bytes"])
            self.assertEqual(
                hashlib.sha256(decoded.encode("utf-8")).hexdigest(),
                indexed["content_hash"],
            )
            self.assertEqual(1, first["added"])
            self.assertEqual("LEXICAL_ONLY", outcome.index_lifecycle["state"])
            self.assertTrue(outcome.candidates)
            self.assertEqual(1, second["files_reused"])
            self.assertEqual(0, second["files_read"])

    def test_incremental_index_updates_only_new_changed_and_removed_files(self):
        first = self.root / "first.py"
        removed = self.root / "removed.py"
        first.write_text("def first():\n    return 'old'\n", encoding="utf-8")
        removed.write_text("def removed():\n    return 3\n", encoding="utf-8")
        with IndexStore(":memory:") as store:
            index_repository(self.root, store)
            first.write_text("def first():\n    return 'changed'\n", encoding="utf-8")
            added = self.root / "added.py"
            added.write_text("def added():\n    return 4\n", encoding="utf-8")
            removed.unlink()
            with patch("agentforge.indexer.read_candidate", wraps=__import__(
                    "agentforge.scanner", fromlist=["read_candidate"]).read_candidate) as read:
                updated = index_repository(self.root, store)
            self.assertEqual({"first.py", "added.py"},
                             {call.args[0].relative_path for call in read.call_args_list})
            self.assertIsNone(store.indexed_file_hash("removed.py"))
            self.assertIsNotNone(store.indexed_file_hash("added.py"))
        self.assertEqual((1, 1, 1), (updated["added"], updated["changed"], updated["removed"]))
        self.assertEqual(2, updated["files_read"])

    def test_chroma_sync_uses_bounded_batches_then_skips_unchanged_embeddings(self):
        for number in range(17):
            (self.root / f"module_{number:02}.py").write_text(
                f"def function_{number}():\n    return {number}\n", encoding="utf-8",
            )
        collection = _MemoryCollection()
        provider = _BatchProvider()
        with IndexStore(":memory:") as store:
            index_repository(self.root, store)
            vector_index = ChromaCodeIndex(store, collection, provider)
            stats = vector_index.sync()
            inspected = vector_index.inspect_consistency()
            get_count_after_inspect = len(collection.get_calls)
            second_inspection = vector_index.inspect_consistency()
            self.assertEqual(get_count_after_inspect, len(collection.get_calls))
            unchanged = vector_index.sync()
        self.assertEqual([16, 1], provider.batch_sizes)
        self.assertEqual([16, 1], collection.upsert_sizes)
        self.assertEqual(17, stats.embedded)
        self.assertEqual(17, unchanged.unchanged)
        self.assertEqual(17, collection.count())
        self.assertTrue(inspected["complete"])
        self.assertTrue(second_inspection["complete"])
        # Explicit sync rechecks Chroma, but the unchanged snapshot is not re-embedded.
        self.assertEqual([16, 1], provider.batch_sizes)
        self.assertEqual([16, 1], collection.upsert_sizes)

    def test_invalid_embedding_batch_is_rejected_before_that_batch_is_written(self):
        for number in range(3):
            (self.root / f"module_{number}.py").write_text(
                f"def function_{number}():\n    return {number}\n", encoding="utf-8",
            )
        collection = _MemoryCollection()
        provider = _BatchProvider(invalid_batch=True)
        with IndexStore(":memory:") as store:
            index_repository(self.root, store)
            with self.assertRaises(EmbeddingError):
                ChromaCodeIndex(store, collection, provider).sync()
        self.assertEqual([], collection.upsert_sizes)
        self.assertEqual(0, collection.count())

    def test_vector_and_sqlite_candidates_merge_with_authoritative_hash_validation(self):
        source = self.root / "compute.py"
        source.write_text("def compute_total(values):\n    return sum(values)\n", encoding="utf-8")
        collection = _MemoryCollection()
        provider = _BatchProvider()
        with IndexStore(":memory:") as store:
            index_repository(self.root, store)
            vector_index = ChromaCodeIndex(store, collection, provider)
            vector_index.sync()
            retriever = ProductRetriever(
                store, self.root, scan_report=scan_repository(self.root),
                vector_retriever=vector_index,
            )
            outcome = retriever.search("What does function compute_total do?", top_k=5)
            verified_source = {}
            for item in outcome.candidates:
                source = (self.root / item.relative_path).read_text(encoding="utf-8")
                excerpt = "\n".join(source.splitlines()[item.start_line - 1:item.end_line])
                chunk = store.get_chunk(item.relative_path, item.start_line, item.end_line)
                accepted_hashes = {
                    hashlib.sha256(
                        f"{item.relative_path}\0{item.start_line}\0{item.end_line}\0{excerpt}".encode(
                            "utf-8",
                        )
                    ).hexdigest(),
                }
                if chunk is not None:
                    accepted_hashes.add(chunk["content_hash"])
                verified_source[(item.relative_path, item.start_line, item.end_line)] = (
                    excerpt, accepted_hashes,
                )
        self.assertTrue(any("vector" in candidate.channels for candidate in outcome.candidates))
        self.assertTrue(any("lexical" in candidate.channels for candidate in outcome.candidates))
        self.assertTrue(all(
            candidate.text == verified_source[
                (candidate.relative_path, candidate.start_line, candidate.end_line)
            ][0]
            and candidate.content_hash in verified_source[
                (candidate.relative_path, candidate.start_line, candidate.end_line)
            ][1]
            for candidate in outcome.candidates
        ))
        self.assertEqual("compute.py", outcome.candidates[0].relative_path)

    def test_vector_unavailable_retains_verified_sqlite_results(self):
        from agentforge.index_lifecycle import UnavailableVectorRetriever

        (self.root / "compute.py").write_text(
            "def compute_total(values):\n    return sum(values)\n", encoding="utf-8",
        )
        with IndexStore(":memory:") as store:
            index_repository(self.root, store)
            outcome = ProductRetriever(
                store, self.root, scan_report=scan_repository(self.root),
                vector_retriever=UnavailableVectorRetriever(
                    "embedding_service_unavailable", "ConnectionError",
                ),
            ).search("What does function compute_total do?", top_k=5)
        self.assertTrue(outcome.candidates)
        self.assertIn("vector", outcome.unavailable_channels)
        self.assertIn("lexical", outcome.candidates[0].channels)


class _SummaryStore:
    def __init__(self, diagnostics):
        self.last_summary_diagnostics = diagnostics


class SessionSummaryFallbackTests(unittest.TestCase):
    def test_summary_overflow_uses_read_only_context_fallback_without_losing_observation(self):
        from types import SimpleNamespace
        from agentforge.conversation_orchestrator import (
            ConversationDependencies, ConversationOrchestrator, ConversationRequest,
            ConversationSession,
        )

        rows = ({"sequence": 1, "role": "user", "content": "older turn",
                 "status": "completed"},)
        restored = SimpleNamespace(
            history=(SimpleNamespace(role="user", content="older turn"),), rows=rows,
            summary={"content": "existing summary"}, covered_through=0, fact_ledger=(),
        )

        class Service:
            def restore_context(self, *_args, **_kwargs):
                raise ValueError("A complete turn exceeds the bounded semantic summary input.")

            def load_context(self, *_args):
                self.loaded = True
                return restored

        service = Service()
        provider = object()
        store = _SummaryStore([{
            "failure_code": "semantic_summary_turn_input_too_large",
            "failure_stage": "input_validation",
        }])
        dependencies = ConversationDependencies(
            provider_factory=lambda *_args, **_kwargs: provider,
            model_budget_resolver=lambda *_args, **_kwargs: {"input_budget": 3000},
            session_service=service,
        )
        state = ConversationSession(
            provider=provider, store=store, repository_id="repo-1",
            conversation={"tool_observations": ({"tool_id": "read", "output_preview": "kept"},)},
        )
        request = ConversationRequest(
            "Current question remains current.", "repo", "deepseek",
            conversation_id="session-1",
        )
        ConversationOrchestrator(dependencies)._prepare_history(request, state, provider)
        self.assertTrue(service.loaded)
        self.assertEqual("existing summary", state.conversation["summary"])
        self.assertEqual("older turn", state.conversation["history"][0].content)
        self.assertEqual("Current question remains current.", request.query)
        self.assertEqual("kept", state.conversation["tool_observations"][0]["output_preview"])

    def test_summary_overflow_fallback_builds_bounded_request_and_preserves_raw_history(self):
        from dataclasses import replace
        from types import SimpleNamespace
        from agentforge.context_assembler import ContextAssembler
        from agentforge.conversation_orchestrator import (
            ConversationDependencies, ConversationOrchestrator, ConversationRequest,
            ConversationSession,
        )
        from agentforge.model_types import LOCAL_QWEN
        from agentforge.models import BuiltContext
        from agentforge.token_budget import ContextBudgetManager

        rows = []
        for turn in range(1, 7):
            rows.extend((
                {"sequence": 2 * turn - 1, "role": "user",
                 "content": f"OLD_TURN_{turn}_USER " + "u" * 90, "status": "completed"},
                {"sequence": 2 * turn, "role": "assistant",
                 "content": f"OLD_TURN_{turn}_ASSISTANT " + "a" * 90, "status": "completed"},
            ))
        restored = SimpleNamespace(
            history=tuple(SimpleNamespace(role=row["role"], content=row["content"])
                          for row in rows),
            rows=tuple(rows), summary={"content": "SUMMARY_MARKER: prior task context"},
            covered_through=8, fact_ledger=(),
        )

        class Service:
            def restore_context(self, *_args, **_kwargs):
                raise ValueError("A complete turn exceeds the bounded semantic summary input.")

            def load_context(self, *_args):
                return restored

        provider = object()
        state = ConversationSession(
            provider=provider, store=_SummaryStore([{
                "failure_code": "semantic_summary_turn_input_too_large",
                "failure_stage": "input_validation",
            }]), repository_id="repo-1", conversation={"repository_id": "repo-1"},
        )
        request = ConversationRequest(
            "CURRENT_QUESTION_MARKER: continue the active task", "repo-1", "deepseek",
            conversation_id="session-1",
        )
        state.conversation["conversation_id"] = request.conversation_id
        orchestrator = ConversationOrchestrator(ConversationDependencies(
            provider_factory=lambda *_args, **_kwargs: provider,
            model_budget_resolver=lambda *_args, **_kwargs: {"input_budget": 3000},
            session_service=Service(),
        ))
        orchestrator._prepare_history(request, state, provider)
        persisted_before = tuple(state.conversation["persisted_history"])
        self.assertEqual(12, len(persisted_before))

        context = BuiltContext(
            question=request.query, evidence=(), text="", estimated_tokens=0, budget=0,
            tool_observations=({
                "tool_id": "read", "status": "succeeded",
                "output_preview": "OBSERVATION_MARKER: required result",
                "result_ref": "synthetic://result/1", "error_code": None, "truncated": False,
            },),
        )
        state.conversation["input_token_budget_override"] = 1_100
        assembler = ContextAssembler(ContextBudgetManager(
            safety_ratio=0, minimum_safety_tokens=0,
        ))
        messages = assembler.assemble_context(
            context, state.conversation,
            [{"role": "system", "content": "System instructions remain first."},
             {"role": "user", "content": request.query}],
            descriptor=replace(LOCAL_QWEN, context_window=4096),
            requested_output_tokens=128, mode="hybrid",
        )

        assembled = "\n".join(message["content"] for message in messages)
        self.assertIn("CURRENT_QUESTION_MARKER", assembled)
        self.assertIn("OBSERVATION_MARKER", assembled)
        self.assertIn("SUMMARY_MARKER", assembled)
        self.assertIn("OLD_TURN_6_USER", assembled)
        self.assertNotIn("OLD_TURN_1_USER", assembled)
        self.assertLessEqual(assembler.last_diagnostics["estimated_input_tokens"], 1_100)
        self.assertGreater(assembler.last_diagnostics["dropped_history_messages"], 0)
        self.assertEqual(persisted_before, tuple(state.conversation["persisted_history"]))

    def test_database_or_scope_failure_does_not_trigger_summary_fallback(self):
        from agentforge.conversation_orchestrator import (
            ConversationDependencies, ConversationOrchestrator, ConversationRequest,
            ConversationSession,
        )

        class Service:
            def restore_context(self, *_args, **_kwargs):
                raise RuntimeError("database read failed")

            def load_context(self, *_args):
                raise AssertionError("non-summary errors must propagate")

        provider = object()
        store = _SummaryStore([])
        orchestrator = ConversationOrchestrator(ConversationDependencies(
            provider_factory=lambda *_args, **_kwargs: provider,
            model_budget_resolver=lambda *_args, **_kwargs: {"input_budget": 10},
            session_service=Service(),
        ))
        with self.assertRaisesRegex(RuntimeError, "database read failed"):
            orchestrator._prepare_history(
                ConversationRequest("question", "repo", "deepseek", conversation_id="id"),
                ConversationSession(provider=provider, store=store, repository_id="repo"),
                provider,
            )


class PresentationDebugModeTests(unittest.TestCase):
    def test_default_hides_lifecycle_from_console_but_keeps_jsonl_logging(self):
        from contextlib import redirect_stderr, redirect_stdout
        import io
        from agentforge import presentation

        value = {
            "provider": "offline", "answer": "A safe answer.",
            "answer_contract": {"status": "answered", "claims": [], "evidence": []},
        }
        output, error = io.StringIO(), io.StringIO()
        with patch("agentforge.presentation.cli.main", side_effect=lambda _args: print(json.dumps(value)) or 0), \
                patch("agentforge.presentation.RequestLifecycleTracker") as tracker, \
                redirect_stdout(output), redirect_stderr(error):
            self.assertEqual(0, presentation.main(["ask", "question"]))
        self.assertIsNone(tracker.call_args.kwargs["console_stream"])
        self.assertIn("处理中", output.getvalue())
        self.assertIn("A safe answer.", output.getvalue())

    def test_explicit_debug_connects_lifecycle_to_stderr(self):
        from contextlib import redirect_stderr, redirect_stdout
        import io
        from agentforge import presentation

        value = {
            "provider": "offline", "answer": "A safe answer.",
            "answer_contract": {"status": "answered", "claims": [], "evidence": []},
        }
        output, error = io.StringIO(), io.StringIO()
        with patch("agentforge.presentation.cli.main", side_effect=lambda _args: print(json.dumps(value)) or 0), \
                patch("agentforge.presentation.RequestLifecycleTracker") as tracker, \
                redirect_stdout(output), redirect_stderr(error):
            self.assertEqual(0, presentation.main(["ask", "question", "--debug"]))
        self.assertIs(tracker.call_args.kwargs["console_stream"], error)
        self.assertNotIn("处理中", output.getvalue())
