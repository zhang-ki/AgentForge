"""针对失败处理和数据完整性的离线回归测试。"""

import copy
import io
import json
import os
import stat
import struct
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from agentforge import eval_retrieval
from agentforge.calibration import calibrate_abstention, evaluate_fixed_rule
from agentforge.embeddings import EmbeddingError, VectorStore, embed_text, normalize_vector
from agentforge.embeddings.base import provider_identity
from agentforge.evaluation import _validate_dataset
from agentforge.generalization import (
    DEFAULT_SPLIT_PLAN, _read_json, load_generalization_dataset,
    validate_evidence, validate_generalization_split,
)
from agentforge.indexer import index_repository
from agentforge.package_evaluation import validate_package_dataset
from agentforge.package_s2 import validate_s2
from agentforge.package_relations import _parse_snapshot, _snapshot_repository
from agentforge.errors import ScanError, StoreError
from agentforge.scanner import ScanReadError, _canonical_inside, _excluded_file, _is_within, _sensitive_name, read_candidate, scan_repository
from agentforge.safeio import open_source
from agentforge.store import IndexStore
from tests.support import LocalTestCase


class WindowsHandleFailureTests(LocalTestCase):
    def test_windows_fd_conversion_failure_closes_descriptor(self):
        if os.name != "nt":
            self.skipTest("Windows handle API is unavailable on this platform")
        import ctypes
        import msvcrt
        source = self.write("file.py", b"data\n")
        kernel = Mock()
        kernel.CreateFileW.return_value = 12345
        def attributes(_handle, _kind, value, _size):
            value._obj.attributes = 0
            return True
        def final_path(_handle, buffer, _size, _flags):
            buffer.value = "\\\\?\\" + str(source)
            return len(buffer.value)
        kernel.GetFileInformationByHandleEx.side_effect = attributes
        kernel.GetFinalPathNameByHandleW.side_effect = final_path
        with patch.object(ctypes, "WinDLL", return_value=kernel), \
                patch.object(msvcrt, "open_osfhandle", return_value=123), \
                patch("agentforge.safeio.os.fdopen", side_effect=OSError("synthetic")), \
                patch("agentforge.safeio.os.close") as closed:
            with self.assertRaisesRegex(OSError, "synthetic"):
                open_source(self.root, source)
        closed.assert_called_once_with(123)
        kernel.CloseHandle.assert_not_called()

    def test_invalid_windows_handle_is_rejected(self):
        if os.name != "nt":
            self.skipTest("Windows handle API is unavailable on this platform")
        import ctypes
        kernel = Mock()
        kernel.CreateFileW.return_value = ctypes.c_void_p(-1).value
        with patch.object(ctypes, "WinDLL", return_value=kernel):
            with self.assertRaises(OSError):
                open_source(self.root, self.root / "missing.py")
        kernel.CloseHandle.assert_not_called()

    def test_windows_handle_failures_close_owned_handle(self):
        if os.name != "nt":
            self.skipTest("Windows handle API is unavailable on this platform")
        import ctypes
        source = self.write("file.py", b"data\n")
        for failure in ("attributes", "reparse", "path_length", "outside"):
            kernel = Mock()
            kernel.CreateFileW.return_value = 12345
            def attributes(_handle, _kind, value, _size):
                value._obj.attributes = 0x400 if failure == "reparse" else 0
                return failure != "attributes"
            def final_path(_handle, buffer, _size, _flags):
                if failure == "path_length":
                    return 0
                buffer.value = str(self.sandbox / "outside.py" if failure == "outside" else source)
                return len(buffer.value)
            kernel.GetFileInformationByHandleEx.side_effect = attributes
            kernel.GetFinalPathNameByHandleW.side_effect = final_path
            with self.subTest(failure=failure), patch.object(ctypes, "WinDLL", return_value=kernel):
                with self.assertRaises(OSError):
                    open_source(self.root, source)
                kernel.CloseHandle.assert_called_once_with(12345)

    def test_posix_nofollow_rejects_non_regular_and_closes_descriptor(self):
        source = self.root / "file.py"
        descriptor_stat = SimpleNamespace(st_mode=stat.S_IFDIR)
        with patch("agentforge.safeio.os.name", "posix"), \
                patch("agentforge.safeio.os.open", return_value=123) as opened, \
                patch("agentforge.safeio.os.fstat", return_value=descriptor_stat), \
                patch("agentforge.safeio.os.close") as closed:
            with self.assertRaisesRegex(OSError, "ordinary"):
                open_source(self.root, source)
        opened.assert_called_once()
        closed.assert_called_once_with(123)

    def test_posix_nofollow_returns_opened_regular_file(self):
        source = self.root / "file.py"
        stream = Mock()
        with patch("agentforge.safeio.os.name", "posix"), \
                patch("agentforge.safeio.os.open", return_value=123), \
                patch("agentforge.safeio.os.fstat", return_value=SimpleNamespace(st_mode=stat.S_IFREG)), \
                patch("agentforge.safeio.os.fdopen", return_value=stream):
            self.assertIs(open_source(self.root, source), stream)


class PackageSnapshotFailureTests(LocalTestCase):
    def test_snapshot_reuses_unchanged_ast_and_rejects_mid_read_change(self):
        source = self.write("module.py", b"def original():\n    return 1\n")
        self.write("notes.md", b"not Python\n")
        report, manifest, candidates = _snapshot_repository(self.root)
        modules, failures, cache, count = _parse_snapshot(report, manifest, candidates)
        self.assertEqual((set(modules), failures, count), ({"module"}, [], 1))
        self.assertEqual(_parse_snapshot(report, manifest, candidates, cache)[3], 0)
        source.write_bytes(b"def changed():\n    return 2\n")
        with self.assertRaisesRegex(RuntimeError, "changed"):
            _parse_snapshot(report, manifest, candidates)

    def test_snapshot_read_failure_is_recorded_and_never_resolved(self):
        self.write("module.py", b"x = 1\n")
        with patch("agentforge.package_relations.read_candidate", side_effect=ScanReadError("unreadable_file")):
            report, manifest, candidates = _snapshot_repository(self.root)
        modules, failures, cache, count = _parse_snapshot(report, manifest, candidates)
        self.assertEqual(modules, {})
        self.assertEqual(failures, [("module.py", "ScanReadError")])
        self.assertEqual(count, 0)
        self.assertIsNone(cache["module.py"][1])

    def test_snapshot_reports_syntax_error_and_rejects_duplicate_module_identity(self):
        self.write("broken.py", b"def broken(:\n")
        report, manifest, candidates = _snapshot_repository(self.root)
        modules, failures, cache, count = _parse_snapshot(report, manifest, candidates)
        self.assertEqual((modules, failures, count), ({}, [("broken.py", "SyntaxError")], 1))
        self.assertIsNone(cache["broken.py"][1])
        self.write("duplicate.py", b"value = 1\n")
        self.write("duplicate/__init__.py", b"value = 2\n")
        report, manifest, candidates = _snapshot_repository(self.root)
        with self.assertRaisesRegex(ValueError, "same module name"):
            _parse_snapshot(report, manifest, candidates)


class ScannerFailureTests(LocalTestCase):
    def test_canonical_path_failures_are_classified(self):
        source = self.write("file.py", b"safe\n")
        outside = self.sandbox / "outside.py"
        outside.write_bytes(b"outside\n")
        with self.assertRaises(ScanReadError) as caught:
            _canonical_inside(self.root, outside)
        self.assertEqual(caught.exception.reason, "outside_root")
        with patch("agentforge.scanner._has_reparse_attribute", return_value=True):
            with self.assertRaises(ScanReadError) as caught:
                _canonical_inside(self.root, source)
        self.assertEqual(caught.exception.reason, "reparse_point")
        with patch("agentforge.scanner._is_within", side_effect=[True, False]):
            with self.assertRaises(ScanReadError) as caught:
                _canonical_inside(self.root, source)
        self.assertEqual(caught.exception.reason, "outside_root")
        with self.assertRaises(ScanReadError) as caught:
            _canonical_inside(self.root, self.root / "missing.py")
        self.assertEqual(caught.exception.reason, "unavailable_path")
        with patch("agentforge.scanner.os.path.commonpath", side_effect=ValueError("different drive")):
            self.assertFalse(_is_within(self.root, outside))
        self.assertTrue(_sensitive_name("id_rsa.backup"))
        self.assertEqual(_excluded_file("image.png"), "binary_extension")

    def test_unreadable_directory_keeps_prior_memory_index(self):
        self.write("file.py", b"def safe():\n    return 1\n")
        with IndexStore(":memory:") as store:
            index_repository(self.root, store)
            before = list(store.search_symbols(name="safe"))
            with patch("agentforge.scanner.os.scandir", side_effect=PermissionError("denied")):
                report = scan_repository(self.root)
                self.assertFalse(report.complete)
                self.assertEqual(report.issues[0].reason, "unreadable_directory")
                with self.assertRaises(ScanError):
                    index_repository(self.root, store)
            self.assertEqual(list(store.search_symbols(name="safe")), before)

    def test_read_rejects_reparse_nonfile_and_oversized_metadata(self):
        self.write("file.py", b"safe\n")
        report = scan_repository(self.root)
        candidate = report.candidates[0]
        actual = candidate.absolute_path.lstat()
        for mode, attrs, size, expected in (
            (stat.S_IFREG, 0x400, actual.st_size, "reparse_point"),
            (stat.S_IFDIR, 0, actual.st_size, "not_regular_file"),
            (stat.S_IFREG, 0, 9999, "file_size_limit"),
        ):
            fake = Mock()
            fake.lstat.return_value = SimpleNamespace(st_mode=mode, st_file_attributes=attrs,
                st_nlink=1, st_dev=actual.st_dev, st_ino=actual.st_ino, st_size=size)
            with self.subTest(expected=expected), patch("agentforge.scanner._canonical_inside", return_value=fake):
                with self.assertRaises(ScanReadError) as caught:
                    read_candidate(candidate, report.root, max_file_bytes=100)
                self.assertEqual(caught.exception.reason, expected)
        with patch("agentforge.scanner._canonical_inside", side_effect=ScanReadError("outside_root")):
            with self.assertRaises(ScanReadError) as caught:
                read_candidate(candidate, report.root)
        self.assertEqual(caught.exception.reason, "unavailable_path")

    def test_scan_rejects_metadata_failures_and_type_changes(self):
        source = self.write("file.py", b"safe\n")
        actual = source.stat()
        fake_stats = (
            (PermissionError("denied"), "unreadable_metadata"),
            (SimpleNamespace(st_mode=stat.S_IFREG, st_file_attributes=0x400), "reparse_point"),
            (SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0), "unsupported_file_type"),
        )
        for metadata, reason in fake_stats:
            fake_path = Mock()
            if isinstance(metadata, Exception):
                fake_path.lstat.side_effect = metadata
            else:
                fake_path.lstat.return_value = metadata
            def canonical(_root, path):
                return self.root if path == self.root else fake_path
            with self.subTest(reason=reason), patch("agentforge.scanner._canonical_inside", side_effect=canonical):
                report = scan_repository(self.root)
            self.assertEqual(report.candidates, [])
            self.assertEqual(report.issues[0].reason, reason)
        self.assertTrue(stat.S_ISREG(actual.st_mode))

    def test_scan_records_unreadable_entry_and_reparse_race(self):
        source = self.write("file.py", b"safe\n")
        entry = Mock()
        entry.name, entry.path = source.name, str(source)
        class Listing:
            def __enter__(self): return iter([entry])
            def __exit__(self, *_): return False
        entry.stat.side_effect = PermissionError("denied")
        with patch("agentforge.scanner.os.scandir", return_value=Listing()):
            report = scan_repository(self.root)
        self.assertEqual((report.candidates, report.issues[0].reason), ([], "unreadable_metadata"))
        entry.stat.side_effect = None
        entry.stat.return_value = source.stat()
        def canonical(_root, path):
            if path == self.root: return self.root
            raise ScanReadError("reparse_point")
        with patch("agentforge.scanner.os.scandir", return_value=Listing()), \
                patch("agentforge.scanner._canonical_inside", side_effect=canonical):
            report = scan_repository(self.root)
        self.assertEqual((report.candidates, report.issues[0].reason), ([], "reparse_point"))

    def test_scan_rejects_unsupported_and_unsafe_reference_names(self):
        self.write("bad[name].py", b"safe\n")
        report = scan_repository(self.root)
        self.assertEqual(report.candidates, [])
        self.assertEqual(report.issues[0].reason, "unsafe_reference_path")
        entry = Mock()
        entry.name, entry.path = "socket", str(self.root / "socket")
        entry.stat.return_value = SimpleNamespace(st_mode=stat.S_IFSOCK, st_file_attributes=0)
        class Listing:
            def __enter__(self): return iter([entry])
            def __exit__(self, *_): return False
        def canonical(_root, path): return path
        with patch("agentforge.scanner.os.scandir", return_value=Listing()), \
                patch("agentforge.scanner._canonical_inside", side_effect=canonical):
            report = scan_repository(self.root)
        self.assertEqual(report.candidates, [])
        self.assertEqual(report.issues[0].reason, "unsupported_file_type")


class EvaluationValidationTests(unittest.TestCase):
    def test_invalid_dataset_metadata_and_ground_truth_are_rejected(self):
        item = {"id": "q", "category": "code", "query": "where?",
                "relevant_paths": ["x.py"], "relevant_symbols": [], "no_evidence": False}
        good = {"dataset_version": "agentforge-retrieval-v1", "label_rule": "fixture",
                "queries": [item]}
        self.assertEqual(_validate_dataset(good), [item])
        invalid = []
        for field, value in (("dataset_version", "unknown"), ("label_rule", " "),
                             ("queries", []), ("queries", [None])):
            changed = copy.deepcopy(good); changed[field] = value; invalid.append(changed)
        for field, value in (("id", ""), ("relevant_paths", None), ("relevant_symbols", None),
                             ("symbol_query", {}), ("symbol_query", {"bad": "x"}),
                             ("no_evidence", "yes"), ("no_evidence", True),
                             ("relevant_paths", []), ("relevant_paths", ["x.py", "x.py"])):
            changed = copy.deepcopy(good); changed["queries"][0][field] = value; invalid.append(changed)
        invalid.append({**good, "queries": [item, copy.deepcopy(item)]})
        for symbols, symbol_query in (([{"path": "x.py", "qualified_name": "f"}], None),
                                      ([{"path": "", "qualified_name": "f"}], {"name": "f"}),
                                      ([{"path": "x.py", "qualified_name": "f"}] * 2, {"name": "f"})):
            changed = copy.deepcopy(good)
            changed["queries"][0].update(relevant_symbols=symbols, symbol_query=symbol_query)
            invalid.append(changed)
        for candidate in invalid:
            with self.subTest(candidate=candidate), self.assertRaises(ValueError):
                _validate_dataset(candidate)

    def test_calibration_rejects_bad_rows_and_thresholds(self):
        row = {"query_id": "q", "top_score": 0.5, "top_2_margin": 0.1, "no_evidence": False}
        for rows in ([], [None], [row, row], [{**row, "top_score": float("nan")}],
                     [{**row, "top_2_margin": float("inf")}], [{**row, "no_evidence": "yes"}]):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                evaluate_fixed_rule(rows, {"family": "top_score", "top_score_threshold": 0.5})
        for rule in ({}, {"family": "top_score", "top_score_threshold": float("nan")},
                     {"family": "top_2_margin", "top_2_margin_threshold": -1}):
            with self.subTest(rule=rule), self.assertRaises(ValueError):
                evaluate_fixed_rule([row], rule)
        with self.assertRaises(ValueError):
            calibrate_abstention([row], thresholds=[])


class OfflineEvaluationEntryTests(LocalTestCase):
    def test_frozen_baseline_entry_uses_memory_and_reports_results(self):
        output = io.StringIO()
        with redirect_stdout(output):
            status = eval_retrieval.main(["--dataset", "baseline"])
        report = json.loads(output.getvalue())
        self.assertEqual(status, 0)
        self.assertEqual(report["query_total"], 9)
        self.assertEqual(report["retrievers"]["vector"]["status"], "not_measured")

    def test_missing_dataset_entry_reports_error(self):
        errors = io.StringIO()
        with patch.object(eval_retrieval, "DATASET_PATH", self.sandbox / "absent.json"), redirect_stderr(errors):
            status = eval_retrieval.main([])
        self.assertEqual(status, 1)
        self.assertIn("Retrieval evaluation failed", errors.getvalue())


class VectorStoreFailureTests(LocalTestCase):
    def test_limits_invalid_sources_and_corrupt_vectors_fail_closed(self):
        self.write("example.py", b"def sample():\n    return 42\n")
        with IndexStore(":memory:") as store:
            index_repository(self.root, store)
            vectors = VectorStore(store)
            row = next(store.iter_chunks())
            args = dict(model_id="unit-model", relative_path=row["relative_path"],
                        start_line=row["start_line"], end_line=row["end_line"],
                        content_hash=row["content_hash"], vector=[1.0, 0.0])
            for changes in ({"relative_path": "../escape.py"}, {"start_line": 0},
                            {"end_line": 0}, {"content_hash": "stale"}):
                with self.subTest(changes=changes), self.assertRaises(ValueError):
                    vectors.upsert_chunk(**{**args, **changes})
            for limit_name in ("MAX_VECTOR_RECORDS", "MAX_VECTOR_BYTES"):
                with self.subTest(limit=limit_name), patch("agentforge.embeddings.vector_store." + limit_name, 0):
                    with self.assertRaises(EmbeddingError):
                        vectors.upsert_chunk(**args)
            self.assertEqual(store.connection.execute("SELECT COUNT(*) FROM vector_embeddings").fetchone()[0], 0)
            self.assertTrue(vectors.upsert_chunk(**args))
            self.assertEqual(len(vectors.search([1.0, 0.0], model_id="unit-model")), 1)
            self.assertTrue(vectors.upsert_chunk(**{**args, "vector": [0.0, 1.0]}))
            for limit in (0, True, 101):
                with self.subTest(top_k=limit), self.assertRaises(ValueError):
                    vectors.search([1.0, 0.0], model_id="unit-model", top_k=limit)
            with self.assertRaises(ValueError):
                vectors.upsert_symbol(model_id="unit-model", relative_path="example.py",
                                      symbol_id="missing", content_hash="bad", vector=[1.0, 0.0])
            store.connection.execute("UPDATE vector_embeddings SET vector = ?",
                                     (struct.pack("<2f", float("nan"), 0.0),))
            with self.assertRaisesRegex(EmbeddingError, "invalid"):
                vectors.search([1.0, 0.0], model_id="unit-model")
            self.assertEqual(vectors.remove_model("unit-model"), 1)
            self.assertEqual(vectors.search([1.0, 0.0], model_id="unit-model"), [])

    def test_invalid_schema_dimension_and_invalid_blob_are_rejected(self):
        with self.assertRaises(StoreError):
            VectorStore(SimpleNamespace(schema_version=2))
        self.write("example.py", b"def sample():\n    return 42\n")
        with IndexStore(":memory:") as store:
            index_repository(self.root, store)
            vectors = VectorStore(store)
            row = next(store.iter_chunks())
            args = dict(model_id="unit-model", relative_path=row["relative_path"],
                        start_line=row["start_line"], end_line=row["end_line"],
                        content_hash=row["content_hash"], vector=[1.0, 0.0])
            vectors.upsert_chunk(**args)
            with self.assertRaisesRegex(EmbeddingError, "dimension"):
                vectors.upsert_chunk(**{**args, "vector": [1.0, 0.0, 0.0]})
            with self.assertRaises(ValueError):
                vectors.upsert_symbol(model_id="unit-model", relative_path="example.py",
                                      symbol_id="", content_hash="hash", vector=[1.0, 0.0])
            internal = dict(model_id="unit-model", source_kind="chunk", file_id=1,
                            source_id="source", chunk_id=1, relative_path="example.py", source_key="1:1")
            for vector, digest in (((), "hash"), ((1.0,), ""), ((1e100,), "hash")):
                with self.subTest(vector=vector, digest=digest), self.assertRaises((ValueError, EmbeddingError)):
                    vectors._upsert(**internal, content_hash=digest, vector=vector)
            with self.assertRaises(ValueError):
                vectors.search([1.0, 0.0], model_id="bad\nmodel")
            fake_connection = Mock()
            fake_connection.execute.return_value = [{"dimension": 2, "vector": b"bad"}]
            malformed_store = VectorStore(SimpleNamespace(schema_version=3, connection=fake_connection))
            with self.assertRaisesRegex(EmbeddingError, "inconsistent"):
                malformed_store.search([1.0, 0.0], model_id="model")


class FixtureValidationFailureTests(LocalTestCase):
    def test_multirepository_dataset_rejects_invalid_labels_and_splits(self):
        fixtures = Path(__file__).parent / "fixtures"
        dataset = json.loads((fixtures / "package_s2_eval_v1.json").read_text(encoding="utf-8"))
        plan = json.loads((fixtures / "package_s2_split_v1.json").read_text(encoding="utf-8"))
        root = fixtures / "package_s2_repos"
        self.assertEqual(validate_s2(dataset, plan, root)["query_count"], 120)
        cases = []
        for field, value in (("dataset_version", "bad"), ("queries", []), ("repositories", [])):
            changed = copy.deepcopy(dataset); changed[field] = value; cases.append(changed)
        for field, value in (("id", ""), ("repository_id", "missing"), ("split", "bad"),
                             ("leakage_group", ""), ("label_source", "unknown"),
                             ("review_method", "human"), ("human_reviewed", True),
                             ("evidence", None), ("relevant_paths", [])):
            changed = copy.deepcopy(dataset); changed["queries"][0][field] = value; cases.append(changed)
        for field, value in (("start_line", 0), ("end_line", 9999)):
            changed = copy.deepcopy(dataset); changed["queries"][0]["evidence"][0][field] = value; cases.append(changed)
        changed = copy.deepcopy(dataset); changed["repositories"][0]["path"] = "../escape"; cases.append(changed)
        changed = copy.deepcopy(dataset); changed["repositories"][1]["id"] = changed["repositories"][0]["id"]; cases.append(changed)
        changed = copy.deepcopy(dataset); changed["queries"][1]["id"] = changed["queries"][0]["id"]; cases.append(changed)
        for changed in cases:
            with self.subTest(version=changed.get("dataset_version"), first=changed.get("queries", [])[:1]), self.assertRaises(ValueError):
                validate_s2(changed, plan, root)
        for change in ("version", "splits", "duplicate", "wrong_split"):
            altered = copy.deepcopy(plan)
            if change == "version": altered["dataset_version"] = "bad"
            elif change == "splits": altered["splits"] = {}
            elif change == "duplicate": altered["splits"]["calibration"].append(altered["splits"]["calibration"][0])
            else:
                query_id = altered["splits"]["calibration"].pop()
                altered["splits"]["validation"].append(query_id)
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_s2(dataset, altered, root)

    def test_package_labels_reject_unsafe_paths_and_false_review_claims(self):
        fixtures = Path(__file__).parent / "fixtures"
        dataset = json.loads((fixtures / "package_relation_eval.json").read_text(encoding="utf-8"))
        plan = json.loads((fixtures / "package_relation_split.json").read_text(encoding="utf-8"))
        corpus = fixtures / "package_relation_repo"
        self.assertTrue(validate_package_dataset(dataset, plan, corpus)["leakage_groups_disjoint"])
        cases = []
        for field, value in (("review_method", "human"), ("human_reviewed", True),
                             ("label_source", "unknown"), ("split", "test")):
            changed = copy.deepcopy(dataset); changed["queries"][0][field] = value; cases.append(changed)
        for field, value in (("path", "../outside.py"), ("start_line", 0),
                             ("end_line", 9999)):
            changed = copy.deepcopy(dataset); changed["queries"][0]["evidence"][0][field] = value; cases.append(changed)
        changed = copy.deepcopy(dataset); changed["queries"][0]["id"] = changed["queries"][1]["id"]; cases.append(changed)
        for field, value in (("scope_checked", ""), ("relevant_paths", ["atlas/__init__.py"]),
                             ("evidence", [{"path": "atlas/__init__.py", "start_line": 1, "end_line": 1}])):
            changed = copy.deepcopy(dataset)
            negative = next(item for item in changed["queries"] if item["no_evidence"])
            negative[field] = value
            cases.append(changed)
        for changed in cases:
            with self.subTest(changed=changed["queries"][0]), self.assertRaises(ValueError):
                validate_package_dataset(changed, plan, corpus)
        for alteration in ("version", "missing_split", "duplicate", "mismatch"):
            changed = copy.deepcopy(plan)
            if alteration == "version": changed["dataset_version"] = "wrong"
            elif alteration == "missing_split": del changed["splits"]["validation"]
            elif alteration == "duplicate": changed["splits"]["calibration"].append(changed["splits"]["calibration"][0])
            else:
                first = changed["splits"]["calibration"].pop(); changed["splits"]["validation"].append(first)
            with self.subTest(alteration=alteration), self.assertRaises(ValueError):
                validate_package_dataset(dataset, changed, corpus)

    def test_generalization_rejects_bad_json_scope_and_split_assignments(self):
        for content in (b"not json", b"[]", b"\xff"):
            path = self.write("bad.json", content)
            with self.subTest(content=content), self.assertRaises(ValueError):
                _read_json(path)
        dataset = load_generalization_dataset()
        plan = json.loads(DEFAULT_SPLIT_PLAN.read_text(encoding="utf-8"))
        self.assertTrue(validate_generalization_split(dataset, plan)["groups_are_disjoint"])
        for change in ("version", "status", "splits", "duplicate", "unknown", "groups", "distribution"):
            bad = copy.deepcopy(plan)
            if change == "version": bad["dataset_version"] = "other"
            elif change == "status": bad["status"] = "human"
            elif change == "splits": bad["splits"] = {}
            elif change == "duplicate": bad["splits"]["calibration"].append(bad["splits"]["calibration"][0])
            elif change == "unknown": bad["splits"]["calibration"].append("unknown")
            elif change == "groups": bad["group_assignments"] = []
            else: bad["expected_distribution"] = {}
            with self.subTest(change=change), self.assertRaises(ValueError):
                validate_generalization_split(dataset, bad)
        bad = copy.deepcopy(dataset)
        first = next(item for item in bad["queries"] if item["evidence"])
        first["evidence"][0]["path"] = "../escape.py"
        with self.assertRaises(ValueError):
            validate_evidence(bad)

    def test_generalization_evidence_must_resolve_to_nonempty_in_scope_lines(self):
        original = load_generalization_dataset()
        row = copy.deepcopy(next(item for item in original["queries"] if item["evidence"]))
        row["relevant_paths"] = ["notes.py"]
        row["evidence"] = [{"path": "notes.py", "start_line": 1, "end_line": 1, "reason": "fixture"}]
        dataset = {**original, "queries": [row]}
        target = self.write("notes.py", b"valid\n")
        self.assertEqual(validate_evidence(dataset, self.root)["verified_evidence_ranges"], 1)
        mutations = (
            ("path", "missing.py"),
            ("path", "../escape.py"),
            ("end_line", 2),
        )
        for field, value in mutations:
            bad = copy.deepcopy(dataset); bad["queries"][0]["evidence"][0][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                validate_evidence(bad, self.root)
        target.write_bytes(b"\n")
        with self.assertRaisesRegex(ValueError, "no source text"):
            validate_evidence(dataset, self.root)

    def test_generalization_dataset_requires_correct_version_and_provenance(self):
        original = load_generalization_dataset()
        for field, value in (("dataset_version", "wrong"), ("status", "approved"),
                             ("human_reviewed", True), ("review_method", "human")):
            dataset = copy.deepcopy(original); dataset[field] = value
            path = self.write("dataset.json", json.dumps(dataset))
            with self.subTest(field=field), self.assertRaises(ValueError):
                load_generalization_dataset(path)


class EmbeddingContractFailureTests(unittest.TestCase):
    def test_invalid_identity_and_vector_data_are_rejected(self):
        for identity in (SimpleNamespace(model_id="", dimension=3),
                         SimpleNamespace(model_id="m", dimension=True),
                         SimpleNamespace(model_id="m", dimension=0)):
            with self.subTest(identity=identity), self.assertRaises(EmbeddingError):
                provider_identity(identity)
        for vector in ("not a vector", None, [], [1.0] * 4097,
                       [1.0, float("inf")], [0.0, 0.0], [1.0, "x"]):
            with self.subTest(vector_type=type(vector).__name__), self.assertRaises(EmbeddingError):
                normalize_vector(vector)
        with self.assertRaises(EmbeddingError):
            normalize_vector([1.0], expected_dimension=2)
        for vector in ([1e154, 1e154], [1e308, 1e308]):
            with self.subTest(vector=vector), self.assertRaisesRegex(EmbeddingError, "norm"):
                normalize_vector(vector)

    def test_invalid_role_and_provider_errors_are_sanitized(self):
        provider = SimpleNamespace(model_id="synthetic", dimension=2,
                                   embed=Mock(side_effect=RuntimeError("private detail")))
        for text, role in (("", "query"), ("query", "invalid")):
            with self.subTest(role=role), self.assertRaises(EmbeddingError):
                embed_text(provider, text, role=role)
        with self.assertRaises(EmbeddingError) as caught:
            embed_text(provider, "query", role="query")
        self.assertIn("RuntimeError", str(caught.exception))
        self.assertNotIn("private detail", str(caught.exception))
        self.assertEqual(provider.embed.call_count, 1)


if __name__ == "__main__":
    unittest.main()
