"""通过分支覆盖率驱动的安全边界回归测试，不进行联网。"""

from dataclasses import replace
import json
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace

from agentforge.agent_loop import AgentLoop, ReadOnlyToolbox, ToolCall
from agentforge.answer_contract import AnswerClaim, build_offline_contract, validate_answer_contract
from agentforge.context import build_context, validate_model_citations
from agentforge.errors import ProviderError
from agentforge.providers.llama_server import LlamaServerClient
from agentforge.providers.offline import OfflineProvider
from agentforge.indexer import index_repository
from agentforge.product_retrieval import ProductRetriever
from agentforge.scanner import ScanReadError, read_candidate, scan_repository
from agentforge.store import IndexStore
from tests.support import LocalTestCase
from tests.test_context import result
from tests.test_quality_boundaries import SequencePlanner


class ContractGuardTests(unittest.TestCase):
    def test_contract_rejects_inconsistent_claims_and_evidence(self):
        context = build_context("example", [result()])
        contract = build_offline_contract(OfflineProvider().answer(context), context,
                                         route="exact_symbol", route_confidence="high")
        evidence = contract.evidence[0]
        citation = evidence.citation
        invalid = (
            None,
            replace(contract, version="invalid"),
            replace(contract, status="invalid"),
            replace(contract, evidence=(evidence, evidence)),
            replace(contract, evidence=()),
            replace(contract, claims=(AnswerClaim("x", "invalid", ()),)),
            replace(contract, claims=(AnswerClaim(" ", "unknown", ()),)),
            replace(contract, claims=(AnswerClaim("x", "inference", ("other.py:1-1",)),)),
            replace(contract, claims=(AnswerClaim("x", "inference", ()),)),
            replace(contract, claims=(AnswerClaim("not in answer", "inference", (citation,)),)),
            replace(contract, status="answered"),
            replace(contract, status="refused", claims=(AnswerClaim(evidence.excerpt, "confirmed", (citation,)),)),
            replace(contract, evidence=(replace(evidence, channels=()),)),
            replace(contract, evidence=(replace(evidence, resolution_status="invented"),)),
        )
        for case in invalid:
            with self.subTest(case=case), self.assertRaises(ProviderError):
                validate_answer_contract(case, context)
        empty = build_context("missing", [])
        with self.assertRaises(ProviderError):
            validate_answer_contract(replace(contract, evidence=()), empty)

    def test_context_rejects_malformed_ids_and_excessive_locations(self):
        context = build_context("example", [result()])
        for value in (None, "[E", "[E1\n]", "file.py:" + "1" * 11):
            with self.subTest(value=value), self.assertRaises(ProviderError):
                validate_model_citations(value, context)


class ChatResponseGuardTests(unittest.TestCase):
    def test_malformed_response_shapes_fail_once_and_do_not_expose_contents(self):
        cases = (
            ([], "invalid_response_root"),
            ({"choices": [None]}, "invalid_choice"),
            ({"choices": [{"message": None}]}, "invalid_message"),
            ({"choices": [{"message": {"content": "ok", "reasoning_content": 12}}]}, "invalid_reasoning_content"),
            ({"choices": [{"message": {"content": "ok"}, "finish_reason": 12}]}, "invalid_finish_reason"),
        )
        for payload, code in cases:
            with self.subTest(code=code):
                conn = Mock()
                conn.getresponse.return_value.status = 200
                conn.getresponse.return_value.read.return_value = json.dumps(payload).encode()
                client = LlamaServerClient("http://127.0.0.1", api_key="synthetic-key")
                with patch("agentforge.providers.llama_server.http.client.HTTPConnection", return_value=conn):
                    with self.assertRaisesRegex(ProviderError, code):
                        client.chat([], model="test")
                stats = client.request_stats()
                self.assertEqual((stats["attempted"], stats["sent"], stats["failed"], stats["retries"]), (1, 1, 1, 0))
                conn.close.assert_called_once()

    def test_invalid_model_and_oversized_request_are_rejected_before_connection(self):
        client = LlamaServerClient("http://127.0.0.1", api_key="synthetic-key")
        with patch("agentforge.providers.llama_server.http.client.HTTPConnection") as factory:
            with self.assertRaises(ValueError):
                client.chat([], model=" ")
            with self.assertRaises(ProviderError):
                client.chat([{"role": "user", "content": "a" * 1_000_001}], model="test")
            factory.assert_not_called()
        self.assertEqual(client.request_stats()["attempted"], 0)


class AgentArgumentGuardTests(LocalTestCase):
    def test_invalid_arguments_fail_closed_and_preserve_audit(self):
        self.write("empty.py", "")
        with IndexStore(":memory:") as store:
            toolbox = ReadOnlyToolbox(self.root, store)
            cases = (
                ToolCall("search", {}, "test"),
                ToolCall("search", {"query": " "}, "test"),
                ToolCall("search", {"query": "x" * 513}, "test"),
                ToolCall("search", {"query": "bad\nvalue"}, "test"),
                ToolCall("index", {"repository": str(self.root), "replace_repository": "yes"}, "test"),
                ToolCall("list-symbols", {}, "test"),
                ToolCall("list-symbols", {"kind": "invalid"}, "test"),
            )
            for call in cases:
                with self.subTest(call=call), self.assertRaises(ValueError):
                    toolbox.execute(call)
            with self.assertRaises(ValueError):
                toolbox.execute(None)
            empty = toolbox.execute(ToolCall("read", {"path": "empty.py"}, "test"))
            self.assertEqual((empty["start_line"], empty["end_line"], empty["text"]), (0, 0, ""))
            report = AgentLoop(toolbox, SequencePlanner(["not a ToolCall"]), max_steps=2).run("test")
            self.assertEqual((report.status, report.audit[0].status), ("failed", "rejected"))

    def test_invalid_timeout_configuration_is_rejected(self):
        with IndexStore(":memory:") as store:
            toolbox = ReadOnlyToolbox(self.root, store)
            for kwargs in ({"tool_timeout_seconds": 0}, {"tool_timeout_seconds": 2, "run_timeout_seconds": 1}):
                with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                    AgentLoop(toolbox, SequencePlanner([]), **kwargs)


class VectorProvenanceGuardTests(LocalTestCase):
    def test_optional_vector_candidates_are_checked_against_current_source(self):
        self.write("sample.py", "def sample():\n    return 42\n")
        candidate = SimpleNamespace(relative_path="sample.py", start_line=1, end_line=2,
                                    text="def sample():\n    return 42", content_hash="test",
                                    score=0.7, symbol_id=None)
        vectors = Mock()
        vectors.search.return_value = [candidate]
        with IndexStore(":memory:") as store:
            index_repository(self.root, store)
            retriever = ProductRetriever(store, self.root, vector_retriever=vectors)
            accepted = retriever._vector("sample", 5)
            self.assertEqual(len(accepted), 1)
            self.assertEqual((accepted[0].citation, accepted[0].channels),
                             ("sample.py:1-2", ("vector",)))
            vectors.search.return_value = [SimpleNamespace(**{**vars(candidate), "text": "forged"})]
            self.assertEqual(retriever._vector("sample", 5), [])
            self.assertTrue(retriever._index_stale)
            vectors.search.return_value = [SimpleNamespace(**{**vars(candidate), "end_line": 99})]
            self.assertEqual(retriever._vector("sample", 5), [])
            vectors.search.return_value = [SimpleNamespace(**{**vars(candidate), "relative_path": "../outside.py"})]
            self.assertEqual(retriever._vector("sample", 5), [])


class ScannerRaceGuardTests(LocalTestCase):
    def test_file_identity_and_metadata_changes_are_refused(self):
        self.write("sample.py", "safe = 1\n")
        report = scan_repository(self.root)
        candidate = report.candidates[0]
        info = candidate.absolute_path.stat()
        def opened(**changes):
            return SimpleNamespace(**{
                "st_nlink": changes.get("st_nlink", 1),
                "st_dev": changes.get("st_dev", info.st_dev),
                "st_ino": changes.get("st_ino", info.st_ino),
                "st_size": changes.get("st_size", info.st_size),
                "st_mtime_ns": changes.get("st_mtime_ns", info.st_mtime_ns),
            })
        cases = (
            ([opened(st_nlink=2)], "file_changed_or_hard_link"),
            ([opened(st_ino=info.st_ino + 1)], "file_changed_or_hard_link"),
            ([opened(), opened(st_mtime_ns=info.st_mtime_ns + 1)], "file_changed_during_read"),
        )
        for states, reason in cases:
            with self.subTest(reason=reason), patch("agentforge.scanner.os.fstat", side_effect=states):
                with self.assertRaises(ScanReadError) as caught:
                    read_candidate(candidate, report.root)
                self.assertEqual(caught.exception.reason, reason)

    def test_incomplete_scan_cannot_authorize_toolbox_or_read(self):
        self.write("sample.py", "safe = 1\n")
        incomplete = SimpleNamespace(complete=False)
        with IndexStore(":memory:") as store:
            with patch("agentforge.agent_loop.scan_repository", return_value=incomplete):
                with self.assertRaisesRegex(ValueError, "incomplete"):
                    ReadOnlyToolbox(self.root, store)
            toolbox = ReadOnlyToolbox(self.root, store)
            with patch("agentforge.agent_loop.scan_repository", return_value=incomplete):
                with self.assertRaisesRegex(ValueError, "incomplete"):
                    toolbox.execute(ToolCall("read", {"path": "sample.py"}, "test"))


if __name__ == "__main__":
    unittest.main()
