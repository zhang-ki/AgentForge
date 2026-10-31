import sqlite3

from agentforge.errors import IndexConflictError, ScanError, StoreError
from agentforge.indexer import index_repository
from agentforge.scanner import ScanLimits
from agentforge.store import IndexStore
from tests.support import LocalTestCase


class StoreTests(LocalTestCase):
    def setUp(self):
        super().setUp()
        self.store = IndexStore(":memory:")
        self.addCleanup(self.store.close)

    def test_repeat_index_no_duplicates(self):
        self.write("a.py", "def useful_identifier():\n    return 1\n")
        first = index_repository(self.root, self.store)
        rows = list(self.store.connection.execute("SELECT * FROM chunks"))
        second = index_repository(self.root, self.store)
        self.assertEqual((first["files"], first["chunks"]), (second["files"], second["chunks"]))
        self.assertEqual(second["unchanged"], 1)
        self.assertEqual([tuple(row) for row in rows],
                         [tuple(row) for row in self.store.connection.execute("SELECT * FROM chunks")])

    def test_changed_deleted_empty_and_bad_encoding_invalidate(self):
        self.write("a.py", "old_symbol = 1\n")
        self.write("delete.py", "delete_symbol = 2\n")
        self.write("empty.txt", "")
        index_repository(self.root, self.store)
        self.write("a.py", "new_symbol = 3\n")
        (self.root / "delete.py").unlink()
        result = index_repository(self.root, self.store)
        self.assertEqual((result["changed"], result["removed"]), (1, 1))
        self.assertEqual((result["files"], result["chunks"]), (2, 1))
        self.assertIn("new_symbol", list(self.store.iter_chunks())[0]["text"])
        self.write("a.py", b"\xff\xfe")
        result = index_repository(self.root, self.store)
        self.assertEqual(result["chunks"], 0)
        self.assertEqual(result["skip_reasons"]["unsupported_encoding"], 1)

    def test_chunk_configuration_change_rebuilds(self):
        self.write("a.py", "one\ntwo\nthree\nfour\n")
        index_repository(self.root, self.store, window_lines=4, overlap_lines=0)
        result = index_repository(self.root, self.store, window_lines=2, overlap_lines=0)
        self.assertEqual((result["changed"], result["chunks"]), (1, 2))

    def test_sql_is_parameterized(self):
        self.write("quote';.py", "value = \"'; DROP TABLE files; --\"\n")
        result = index_repository(self.root, self.store)
        self.assertEqual(result["files"], 1)
        self.assertIn("DROP TABLE", list(self.store.iter_chunks())[0]["text"])

    def test_failed_transaction_rolls_back(self):
        self.write("a.py", "original\n")
        index_repository(self.root, self.store)
        self.store.connection.execute(
            "CREATE TRIGGER fail_insert BEFORE INSERT ON files WHEN NEW.relative_path = 'z.py' "
            "BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END"
        )
        self.write("a.py", "changed\n")
        self.write("z.py", "second\n")
        with self.assertRaises(sqlite3.IntegrityError):
            index_repository(self.root, self.store)
        self.assertEqual(list(self.store.iter_chunks())[0]["text"], "original")
        self.assertEqual(self.store.status()["files"], 1)

    def test_incomplete_scan_preserves_previous_index(self):
        self.write("a.py", "original\n")
        index_repository(self.root, self.store)
        self.write("b.py", "second\n")
        for limits in (ScanLimits(max_files=1), ScanLimits(max_total_bytes=8), ScanLimits(max_entries=1)):
            with self.assertRaises(ScanError):
                index_repository(self.root, self.store, limits=limits)
            self.assertEqual(self.store.status()["files"], 1)

    def test_second_repository_rejected(self):
        index_repository(self.root, self.store)
        with self.assertRaises(IndexConflictError):
            index_repository(self.sandbox, self.store)

    def test_explicit_repository_switch_is_atomic(self):
        self.write("old.py", "old\n")
        index_repository(self.root, self.store)
        other = self.sandbox / "second"
        other.mkdir()
        (other / "new.py").write_text("new\n", encoding="utf-8")
        (other / "second.py").write_text("second\n", encoding="utf-8")
        with self.assertRaises(ScanError):
            index_repository(other, self.store, replace_repository=True, limits=ScanLimits(max_files=1))
        self.assertEqual(self.store.status()["repository"], str(self.root))
        result = index_repository(other, self.store, replace_repository=True)
        self.assertEqual((result["added"], result["removed"], result["files"]), (2, 1, 2))
        self.assertEqual(result["repository"], str(other))

    def test_fixed_database_path_and_version(self):
        with self.assertRaises(StoreError):
            IndexStore(self.root / "forbidden.sqlite3")
        self.assertFalse((self.root / "forbidden.sqlite3").exists())
        self.store.connection.execute("UPDATE metadata SET value = ? WHERE key = ?", ("999", "index_version"))
        with self.assertRaises(StoreError):
            self.store._check_version()

