import hashlib
import json
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

from agentforge.chunker import chunk_signature
from agentforge import indexer as indexer_module
from agentforge.errors import StoreError
from agentforge.indexer import index_repository
from agentforge.parsers import PythonAstParser, parser_for_path, resolve_references
from agentforge.parsers import python_ast as python_ast_module
from agentforge.retriever import search
from agentforge.store import IndexStore
from tests.support import FIXTURE, LocalTestCase, snapshot
from tests.test_cli import invoke


SYMBOL_FIXTURE = Path(__file__).parent / "fixtures" / "symbol_repo"


class _StoreScope:
    def __init__(self, store):
        self.store = store

    def __enter__(self):
        return self.store

    def __exit__(self, *_):
        return False


class PythonParserTests(unittest.TestCase):
    def setUp(self):
        self.parser = PythonAstParser()

    def test_windows_relative_path_line_ranges_scopes_and_id_stability(self):
        source = (
            '"""Synthetic module."""\n'
            "\n"
            "class Outer:\n"
            "    def method(self):\n"
            "        def nested(value):\n"
            "            return value\n"
            "        return nested(1)\n"
        )
        parsed = self.parser.parse(r"pkg\windows_path.py", source)
        self.assertEqual(parsed.relative_path, "pkg/windows_path.py")
        self.assertEqual(parsed.module_name, "pkg.windows_path")
        symbols = {item.qualified_name: item for item in parsed.symbols}
        self.assertEqual((symbols["pkg.windows_path"].start_line, symbols["pkg.windows_path"].end_line), (1, 7))
        self.assertEqual((symbols["pkg.windows_path.Outer"].start_line,
                          symbols["pkg.windows_path.Outer"].end_line), (3, 7))
        self.assertEqual((symbols["pkg.windows_path.Outer.method"].start_line,
                          symbols["pkg.windows_path.Outer.method"].end_line), (4, 7))
        nested = symbols["pkg.windows_path.Outer.method.nested"]
        self.assertEqual((nested.start_line, nested.end_line, nested.kind), (5, 6, "function"))
        self.assertEqual(nested.parent_symbol_id, symbols["pkg.windows_path.Outer.method"].symbol_id)

        first = self.parser.parse("pkg/stable.py", "def target(value):\n    return value\n")
        changed = self.parser.parse(
            "pkg/stable.py",
            '"""Unrelated module docstring."""\n\n# unrelated comment\n'
            "def target(value):\n    return value\n\ndef other():\n    return None\n",
        )
        first_id = next(item.symbol_id for item in first.symbols if item.name == "target")
        changed_id = next(item.symbol_id for item in changed.symbols if item.name == "target")
        self.assertEqual(first_id, changed_id)

    def test_duplicate_definitions_have_distinct_ids_and_syntax_errors_are_safe(self):
        duplicate = self.parser.parse(
            "dupes.py", "def repeated():\n    return 1\n\ndef repeated():\n    return 2\n"
        )
        repeated = [item for item in duplicate.symbols if item.name == "repeated"]
        self.assertEqual(len(repeated), 2)
        self.assertNotEqual(repeated[0].symbol_id, repeated[1].symbol_id)
        invalid = self.parser.parse("broken.py", "broken_token = 1\ndef incomplete(:\n    pass\n")
        self.assertEqual(invalid.status, "failed")
        self.assertIn("SyntaxError", invalid.error)
        self.assertEqual(invalid.symbols, ())
        self.assertEqual(invalid.references, ())

    def test_empty_comment_and_docstring_modules(self):
        empty = self.parser.parse("empty.py", "")
        self.assertEqual(empty.status, "parsed")
        self.assertEqual((empty.symbols[0].start_line, empty.symbols[0].end_line), (1, 1))
        comment = self.parser.parse("comment.py", "# comment only\n")
        self.assertIsNone(comment.symbols[0].docstring)
        doc = self.parser.parse("doc.py", '"""module documentation"""\n')
        self.assertEqual(doc.symbols[0].docstring, "module documentation")

    def test_registry_supports_python_and_keeps_other_files_for_lexical_fallback(self):
        self.assertIsNotNone(parser_for_path("src\\windows.py"))
        self.assertIsNotNone(parser_for_path("src/stubs.pyi"))
        self.assertIsNone(parser_for_path("docs/readme.md"))
        with self.assertRaises(ValueError):
            self.parser.parse("../outside.py", "def ignored(): pass")


class StructuredIndexTests(LocalTestCase):
    def setUp(self):
        super().setUp()
        self.store = IndexStore(":memory:")
        self.addCleanup(self.store.close)

    def test_fixture_symbols_imports_calls_and_conservative_resolution(self):
        before = snapshot(SYMBOL_FIXTURE)
        report = index_repository(SYMBOL_FIXTURE, self.store)
        self.assertEqual(report["parsed_python_files"], 9)
        self.assertEqual(report["parse_failures"], 1)
        self.assertEqual(report["symbols"], self.store.status()["symbols"])
        self.assertGreater(report["references"], 0)

        shared = self.store.search_symbols(name="shared_name")
        self.assertEqual(
            {(row["relative_path"], row["kind"]) for row in shared},
            {("app.py", "function"), ("pkg/helpers.py", "function")},
        )
        self.assertNotEqual(shared[0]["symbol_id"], shared[1]["symbol_id"])
        self.assertNotEqual(shared[0]["source_id"], shared[1]["source_id"])
        self.assertEqual(
            self.store.search_symbols(
                qualified_name="pkg.helpers.Worker.Nested.render",
                kind="method", relative_path=r"pkg\helpers.py",
            )[0]["start_line"],
            16,
        )
        nested = self.store.search_symbols(
            qualified_name="pkg.helpers.outer.inner", kind="function"
        )[0]
        outer = self.store.search_symbols(qualified_name="pkg.helpers.outer")[0]
        self.assertEqual(nested["parent_symbol_id"], outer["symbol_id"])

        api_refs = self.store.search_references(relative_path="pkg/api.py")
        resolved = [item for item in api_refs if item["resolution_status"] == "resolved"]
        self.assertTrue(any(
            item["reference_type"] == "from_import"
            and item["target_qualified_name"] == "pkg.helpers.shared_name"
            for item in resolved
        ))
        self.assertTrue(any(
            item["reference_type"] == "call"
            and item["target_qualified_name"] == "pkg.helpers.shared_name"
            and item["target_path"] == "pkg/helpers.py"
            for item in resolved
        ))
        self.assertTrue(any(
            item["reference_type"] == "import"
            and item["target_qualified_name"] == "pkg.helpers"
            for item in resolved
        ))
        self.assertTrue(any(
            item["reference_type"] == "call"
            and item["target_name"] == "absent_symbol"
            and item["resolution_status"] == "unresolved"
            for item in api_refs
        ))
        self.assertEqual(
            len([item for item in api_refs if item["reference_type"] == "dynamic_import"]),
            2,
        )
        self.assertTrue(all(
            item["resolution_status"] == "unresolved"
            for item in api_refs if item["reference_type"] == "dynamic_import"
        ))
        self.assertTrue(any(
            item["reference_type"] == "call"
            and item["target_name"] == "helpers.shared_name"
            and item["resolution_status"] == "unresolved"
            for item in api_refs
        ))
        shadowed = [item for item in api_refs if item["line"] == 30]
        self.assertEqual(len(shadowed), 1)
        self.assertEqual(shadowed[0]["resolution_status"], "unresolved")
        local_definition = [item for item in api_refs if item["line"] == 36]
        self.assertEqual(len(local_definition), 1)
        self.assertEqual(local_definition[0]["resolution_status"], "unresolved")
        ambiguous = self.store.search_references(
            relative_path="pkg/helpers.py", target="duplicate", reference_type="call"
        )
        self.assertEqual(len(ambiguous), 1)
        self.assertEqual(ambiguous[0]["resolution_status"], "ambiguous")
        self.assertEqual(len(ambiguous[0]["candidate_symbol_ids"]), 2)
        relative = self.store.search_references(
            relative_path="pkg/sub/use.py", reference_type="call", target="shared_name"
        )
        self.assertEqual(relative[0]["resolution_status"], "resolved")
        self.assertEqual(relative[0]["target_path"], "pkg/helpers.py")
        self.assertEqual(before, snapshot(SYMBOL_FIXTURE))

    def test_parse_failure_preserves_lexical_chunks_and_database_rows_are_stable(self):
        first = index_repository(SYMBOL_FIXTURE, self.store)
        broken = search(self.store, "broken_lexical_token")
        self.assertEqual(broken[0].relative_path, "broken.py")
        self.assertEqual(self.store.search_symbols(name="broken_lexical_token"), [])
        self.assertEqual(self.store.status()["parse_failures"], 1)

        symbol_rows = [tuple(row) for row in self.store.connection.execute(
            "SELECT * FROM symbols ORDER BY symbol_id"
        )]
        reference_rows = [tuple(row) for row in self.store.connection.execute(
            "SELECT * FROM symbol_references ORDER BY reference_id"
        )]
        second = index_repository(SYMBOL_FIXTURE, self.store)
        self.assertEqual(second["symbols"], first["symbols"])
        self.assertEqual(second["references"], first["references"])
        self.assertEqual(symbol_rows, [tuple(row) for row in self.store.connection.execute(
            "SELECT * FROM symbols ORDER BY symbol_id"
        )])
        self.assertEqual(reference_rows, [tuple(row) for row in self.store.connection.execute(
            "SELECT * FROM symbol_references ORDER BY reference_id"
        )])

    def test_structured_resource_limits_fail_closed_but_keep_lexical_index(self):
        with patch.object(python_ast_module, "MAX_AST_NODES_PER_FILE", 2):
            parsed = PythonAstParser().parse("large_ast.py", "def limited():\n    return 1\n")
        self.assertEqual(parsed.status, "failed")
        self.assertIn("AST node limit", parsed.error)
        with patch.object(python_ast_module, "MAX_SYMBOLS_PER_FILE", 1):
            parsed = PythonAstParser().parse("many_symbols.py", "def limited():\n    return 1\n")
        self.assertEqual(parsed.status, "failed")
        self.assertIn("symbol limit", parsed.error)
        with patch.object(python_ast_module, "MAX_REFERENCES_PER_FILE", 1):
            parsed = PythonAstParser().parse(
                "many_references.py", "def limited():\n    first()\n    second()\n"
            )
        self.assertEqual(parsed.status, "failed")
        self.assertIn("reference limit", parsed.error)

        with patch.object(indexer_module, "MAX_TOTAL_SYMBOLS", 1):
            result = index_repository(SYMBOL_FIXTURE, self.store)
        self.assertEqual(result["parse_failures"], result["parsed_python_files"])
        self.assertGreater(result["chunks"], 0)
        self.assertEqual(search(self.store, "broken_lexical_token")[0].relative_path, "broken.py")
        self.assertEqual(self.store.search_symbols(name="shared_name"), [])

    def test_changed_and_deleted_files_replace_old_symbols_and_references(self):
        self.write("pkg/__init__.py", "")
        self.write("pkg/helpers.py", "def keep():\n    return 1\n\ndef removed():\n    return 2\n")
        self.write(
            "pkg/user.py",
            "from .helpers import removed\n\ndef caller():\n    return removed()\n",
        )
        index_repository(self.root, self.store)
        self.assertEqual(
            self.store.search_references(relative_path="pkg/user.py", target="removed", status="resolved")[0][
                "target_path"
            ],
            "pkg/helpers.py",
        )

        self.write("pkg/helpers.py", "def keep():\n    return 3\n")
        index_repository(self.root, self.store)
        self.assertEqual(self.store.search_symbols(name="removed"), [])
        stale = self.store.search_references(relative_path="pkg/user.py", status="unresolved")
        self.assertEqual(len(stale), 2)
        self.assertTrue(all(item["resolution_status"] == "unresolved" for item in stale))

        (self.root / "pkg" / "user.py").unlink()
        index_repository(self.root, self.store)
        self.assertEqual(self.store.search_references(relative_path="pkg/user.py"), [])

    def test_cli_symbols_references_help_and_invalid_filters(self):
        index_repository(SYMBOL_FIXTURE, self.store)
        scope = _StoreScope(self.store)
        with patch("agentforge.cli.IndexStore", return_value=scope):
            code, output, errors = invoke([
                "symbols", "--qualified-name", "pkg.helpers.Worker.run",
                "--kind", "method", "--path", "pkg/helpers.py",
            ])
            self.assertEqual((code, errors), (0, ""))
            self.assertEqual(json.loads(output)["results"][0]["start_line"], 12)
            code, output, errors = invoke([
                "references", "--path", "pkg/api.py", "--status", "resolved", "--type", "call",
            ])
            self.assertEqual((code, errors), (0, ""))
            self.assertTrue(json.loads(output)["results"])
            code, _, errors = invoke(["symbols"])
            self.assertEqual(code, 2)
            self.assertIn("Provide at least one", errors)
            code, _, errors = invoke(["references", "--path", "../outside.py"])
            self.assertEqual(code, 2)
            self.assertIn("repository-relative", errors)

        for command in ("symbols", "references"):
            with self.subTest(command=command):
                with self.assertRaises(SystemExit) as raised:
                    invoke([command, "--help"])
                self.assertEqual(raised.exception.code, 0)

    def test_v1_read_only_compatibility_and_isolated_v2_migration(self):
        source = self.write("legacy.py", "def preserved_symbol():\n    return preserved_token\n")
        encoded = source.read_bytes()
        text = source.read_text(encoding="utf-8")
        content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        chunk_hash = hashlib.sha256(text.rstrip("\n").encode("utf-8")).hexdigest()
        mtime = source.stat().st_mtime_ns

        isolated_project = self.sandbox / "legacy-project"
        data_dir = isolated_project / ".agentforge" / "data"
        data_dir.mkdir(parents=True)
        db_path = data_dir / "index.sqlite3"
        connection = sqlite3.connect(db_path)
        try:
            connection.executescript("""
                CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE files (
                    id INTEGER PRIMARY KEY, relative_path TEXT NOT NULL UNIQUE,
                    content_hash TEXT NOT NULL, size_bytes INTEGER NOT NULL,
                    mtime_ns INTEGER NOT NULL, chunk_signature TEXT NOT NULL
                );
                CREATE TABLE chunks (
                    id INTEGER PRIMARY KEY, file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
                    start_line INTEGER NOT NULL CHECK(start_line > 0),
                    end_line INTEGER NOT NULL CHECK(end_line >= start_line),
                    text TEXT NOT NULL, content_hash TEXT NOT NULL,
                    UNIQUE(file_id, start_line, end_line)
                );
                CREATE INDEX chunks_file_idx ON chunks(file_id);
            """)
            connection.executemany(
                "INSERT INTO metadata(key, value) VALUES (?, ?)",
                [
                    ("index_version", "1"), ("repository", str(self.root)),
                    ("indexed_at", "2026-01-01T00:00:00+00:00"),
                    ("chunk_signature", chunk_signature(80, 10)),
                ],
            )
            connection.execute(
                "INSERT INTO files(id, relative_path, content_hash, size_bytes, mtime_ns, chunk_signature) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (1, "legacy.py", content_hash, len(encoded), mtime, chunk_signature(80, 10)),
            )
            connection.execute(
                "INSERT INTO chunks(file_id, start_line, end_line, text, content_hash) VALUES (?, ?, ?, ?, ?)",
                (1, 1, 2, text.rstrip("\n"), chunk_hash),
            )
            connection.commit()
        finally:
            connection.close()

        with (
            patch("agentforge.store.PROJECT_ROOT", isolated_project),
            patch("agentforge.store.DATA_DIR", data_dir),
            patch("agentforge.store.DATABASE_PATH", db_path),
        ):
            with IndexStore(db_path, readonly=True) as legacy:
                self.assertEqual(legacy.status()["index_version"], 1)
                self.assertFalse(legacy.status()["symbol_index_available"])
                self.assertEqual(search(legacy, "preserved_token")[0].relative_path, "legacy.py")
                with self.assertRaisesRegex(StoreError, "legacy index"):
                    legacy.search_symbols(name="preserved_symbol")

            with IndexStore(db_path) as migrated:
                self.assertEqual(migrated.status()["index_version"], 3)
                self.assertEqual(search(migrated, "preserved_token")[0].relative_path, "legacy.py")
                result = index_repository(self.root, migrated)
                self.assertEqual(result["files"], 1)
                self.assertEqual(migrated.search_symbols(name="preserved_symbol")[0]["relative_path"], "legacy.py")
                self.assertEqual(migrated.connection.execute(
                    "SELECT COUNT(*) FROM chunks"
                ).fetchone()[0], 1)

    def test_v2_read_only_compatibility_and_isolated_v3_vector_migration(self):
        self.write("legacy.py", "def preserved_symbol():\n    return preserved_token\n")
        legacy = IndexStore(":memory:")
        index_repository(self.root, legacy)
        legacy.connection.execute("DROP TABLE vector_embeddings")
        legacy.connection.execute(
            "UPDATE metadata SET value = ? WHERE key = ?", ("2", "index_version")
        )
        legacy.connection.commit()

        isolated_project = self.sandbox / "schema-v2-project"
        data_dir = isolated_project / ".agentforge" / "data"
        data_dir.mkdir(parents=True)
        db_path = data_dir / "index.sqlite3"
        target = sqlite3.connect(db_path)
        try:
            legacy.connection.backup(target)
        finally:
            target.close()
            legacy.close()

        with (
            patch("agentforge.store.PROJECT_ROOT", isolated_project),
            patch("agentforge.store.DATA_DIR", data_dir),
            patch("agentforge.store.DATABASE_PATH", db_path),
        ):
            with IndexStore(db_path, readonly=True) as old:
                self.assertEqual(old.status()["index_version"], 2)
                self.assertTrue(old.status()["symbol_index_available"])
                self.assertFalse(old.status()["vector_index_available"])
                self.assertEqual(search(old, "preserved_token")[0].relative_path, "legacy.py")
                self.assertEqual(old.search_symbols(name="preserved_symbol")[0]["relative_path"], "legacy.py")
            with IndexStore(db_path) as migrated:
                self.assertEqual(migrated.status()["index_version"], 3)
                self.assertTrue(migrated.status()["vector_index_available"])
                self.assertEqual(search(migrated, "preserved_token")[0].relative_path, "legacy.py")
                self.assertEqual(migrated.search_symbols(name="preserved_symbol")[0]["relative_path"], "legacy.py")
