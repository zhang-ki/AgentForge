"""SQLite index; disk storage is restricted to this project's fixed data path."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import os
import sqlite3
import stat

from .config import (
    DATABASE_PATH,
    DATA_DIR,
    INDEX_VERSION,
    PROJECT_ROOT,
    REPOSITORY_INDEX_DIR,
    STRUCTURED_INDEX_VERSION,
    VECTOR_INDEX_VERSION,
)
from .errors import IndexConflictError, StoreError
from .repositories import repository_identity


def _ordinary(path: Path, directory: bool) -> None:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise StoreError("Index storage cannot use symbolic links or reparse points.")
    if directory != stat.S_ISDIR(info.st_mode):
        raise StoreError("Index storage has an unexpected path type.")
    if not directory and (not stat.S_ISREG(info.st_mode) or info.st_nlink > 1):
        raise StoreError("Index database must be an ordinary, unshared file.")


class IndexStore:
    """One repository per index. ':memory:' is available for isolated unit tests."""

    def __init__(self, database=DATABASE_PATH, *, readonly: bool = False):
        self.database = str(database)
        self.readonly = readonly
        self.schema_version: int | None = None
        memory = self.database == ":memory:"
        if memory and readonly:
            raise StoreError("An in-memory index cannot be opened read-only.")
        if not memory:
            supplied = Path(os.path.abspath(self.database))
            default_database = os.path.normcase(str(supplied)) == os.path.normcase(str(DATABASE_PATH))
            repository_database = (
                os.path.normcase(str(supplied.parent)) == os.path.normcase(str(REPOSITORY_INDEX_DIR))
                and supplied.name.endswith(".sqlite3")
                and len(supplied.stem) == 64
                and all(char in "0123456789abcdef" for char in supplied.stem)
            )
            if not (default_database or repository_database):
                raise StoreError("Database path must be the legacy index or an AgentForge repository index.")
            _ordinary(PROJECT_ROOT, directory=True)
            for directory in (PROJECT_ROOT / ".agentforge", DATA_DIR):
                if directory.exists() or directory.is_symlink():
                    _ordinary(directory, directory=True)
                elif readonly:
                    raise StoreError("No index exists. Run index --repo PATH first.")
                else:
                    directory.mkdir()
            if repository_database:
                if REPOSITORY_INDEX_DIR.exists() or REPOSITORY_INDEX_DIR.is_symlink():
                    _ordinary(REPOSITORY_INDEX_DIR, directory=True)
                elif readonly:
                    raise StoreError("No repository index exists. Run index --repo PATH first.")
                else:
                    REPOSITORY_INDEX_DIR.mkdir()
            existed = supplied.exists() or supplied.is_symlink()
            if existed:
                _ordinary(supplied, directory=False)
            elif readonly:
                raise StoreError("No index exists. Run index --repo PATH first.")
            for suffix in ("-journal", "-wal", "-shm"):
                sidecar = supplied.with_name(supplied.name + suffix)
                if sidecar.exists() or sidecar.is_symlink():
                    _ordinary(sidecar, directory=False)
        else:
            existed = False
        self.connection = None
        try:
            target = Path(self.database).as_uri() + "?mode=ro" if readonly and not memory else self.database
            self.connection = sqlite3.connect(target, uri=readonly, timeout=2)
            self.connection.row_factory = sqlite3.Row
            self.connection.execute("PRAGMA foreign_keys = ON")
            self.connection.execute("PRAGMA temp_store = MEMORY")
            if readonly:
                self.connection.execute("PRAGMA query_only = ON")
            if not existed:
                self._create_schema()
            self._check_version()
        except (sqlite3.Error, OSError, StoreError) as exc:
            self.close()
            if isinstance(exc, StoreError):
                raise
            raise StoreError(f"Cannot open index database ({type(exc).__name__}).") from None

    def _create_schema(self) -> None:
        with self.connection:
            self.connection.executescript("""
                CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE files (
                    id INTEGER PRIMARY KEY,
                    relative_path TEXT NOT NULL UNIQUE,
                    content_hash TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    mtime_ns INTEGER NOT NULL,
                    chunk_signature TEXT NOT NULL
                );
                CREATE TABLE chunks (
                    id INTEGER PRIMARY KEY,
                    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
                    start_line INTEGER NOT NULL CHECK(start_line > 0),
                    end_line INTEGER NOT NULL CHECK(end_line >= start_line),
                    text TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    UNIQUE(file_id, start_line, end_line)
                );
                CREATE INDEX chunks_file_idx ON chunks(file_id);
            """)
            self._create_structured_schema()
            self._create_vector_schema()
            self.connection.execute(
                "INSERT INTO metadata(key, value) VALUES (?, ?)", ("index_version", str(INDEX_VERSION))
            )

    def _create_structured_schema(self) -> None:
        self.connection.execute("""
            CREATE TABLE IF NOT EXISTS symbols (
                symbol_id TEXT PRIMARY KEY,
                source_id TEXT NOT NULL,
                file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
                kind TEXT NOT NULL,
                name TEXT NOT NULL,
                qualified_name TEXT NOT NULL,
                start_line INTEGER NOT NULL CHECK(start_line > 0),
                end_line INTEGER NOT NULL CHECK(end_line >= start_line),
                parent_symbol_id TEXT REFERENCES symbols(symbol_id) ON DELETE CASCADE,
                signature TEXT NOT NULL,
                docstring TEXT,
                structure_hash TEXT NOT NULL
            )
        """)
        self.connection.execute("CREATE INDEX IF NOT EXISTS symbols_file_idx ON symbols(file_id)")
        self.connection.execute("CREATE INDEX IF NOT EXISTS symbols_name_idx ON symbols(name COLLATE NOCASE)")
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS symbols_qualified_name_idx ON symbols(qualified_name COLLATE NOCASE)"
        )
        self.connection.execute("""
            CREATE TABLE IF NOT EXISTS symbol_references (
                reference_id TEXT PRIMARY KEY,
                source_id TEXT NOT NULL,
                source_file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
                source_symbol_id TEXT REFERENCES symbols(symbol_id) ON DELETE SET NULL,
                line INTEGER NOT NULL CHECK(line > 0),
                reference_type TEXT NOT NULL CHECK(
                    reference_type IN ('import', 'from_import', 'call', 'dynamic_import')
                ),
                target_name TEXT NOT NULL,
                target_qualified_name TEXT,
                resolution_status TEXT NOT NULL CHECK(
                    resolution_status IN ('resolved', 'unresolved', 'ambiguous')
                ),
                target_symbol_id TEXT,
                candidate_symbol_ids TEXT NOT NULL
            )
        """)
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS symbol_references_source_idx ON symbol_references(source_file_id, line)"
        )
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS symbol_references_target_idx ON symbol_references(target_symbol_id)"
        )
        self.connection.execute("""
            CREATE TABLE IF NOT EXISTS parse_status (
                file_id INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
                parser_name TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('parsed', 'failed')),
                error TEXT,
                updated_at TEXT NOT NULL
            )
        """)

    def _create_vector_schema(self) -> None:
        self.connection.execute("""
            CREATE TABLE IF NOT EXISTS vector_embeddings (
                embedding_id TEXT PRIMARY KEY,
                model_id TEXT NOT NULL,
                source_kind TEXT NOT NULL CHECK(source_kind IN ('chunk', 'symbol')),
                file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
                source_id TEXT NOT NULL,
                chunk_id INTEGER REFERENCES chunks(id) ON DELETE CASCADE,
                symbol_id TEXT,
                content_hash TEXT NOT NULL,
                dimension INTEGER NOT NULL CHECK(dimension > 0),
                vector BLOB NOT NULL CHECK(length(vector) = dimension * 4),
                updated_at TEXT NOT NULL,
                CHECK(
                    (source_kind = 'chunk' AND chunk_id IS NOT NULL AND symbol_id IS NULL) OR
                    (source_kind = 'symbol' AND chunk_id IS NULL AND symbol_id IS NOT NULL)
                ),
                UNIQUE(model_id, chunk_id),
                UNIQUE(model_id, symbol_id)
            )
        """)
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS vector_embeddings_model_idx ON vector_embeddings(model_id)"
        )
        self.connection.execute(
            "CREATE INDEX IF NOT EXISTS vector_embeddings_file_idx ON vector_embeddings(file_id)"
        )

    def _check_version(self) -> None:
        row = self.connection.execute(
            "SELECT value FROM metadata WHERE key = ?", ("index_version",)
        ).fetchone()
        try:
            version = int(row["value"]) if row is not None else -1
        except (TypeError, ValueError):
            version = -1
        if version == INDEX_VERSION:
            self.schema_version = version
            return
        if version == STRUCTURED_INDEX_VERSION:
            if self.readonly:
                self.schema_version = version
                return
            self._migrate_v2_to_v3()
            return
        if version == 1:
            if self.readonly:
                # Legacy lexical search stays available without mutating the database.
                self.schema_version = version
                return
            self._migrate_v1_to_v2()
            self._migrate_v2_to_v3()
            return
        if version != INDEX_VERSION:
            raise StoreError("Unsupported index version; automatic migration is not implemented.")

    def _migrate_v1_to_v2(self) -> None:
        """Add empty structured-index tables while preserving all v1 lexical rows."""
        with self.connection:
            self._create_structured_schema()
            self.connection.execute(
                "UPDATE metadata SET value = ? WHERE key = ?",
                (str(STRUCTURED_INDEX_VERSION), "index_version"),
            )
        self.schema_version = STRUCTURED_INDEX_VERSION

    def _migrate_v2_to_v3(self) -> None:
        """Add an empty vector table while preserving lexical and symbol rows."""
        with self.connection:
            self._create_vector_schema()
            self.connection.execute(
                "UPDATE metadata SET value = ? WHERE key = ?",
                (str(VECTOR_INDEX_VERSION), "index_version"),
            )
        self.schema_version = VECTOR_INDEX_VERSION

    def metadata(self) -> dict[str, str]:
        return dict(self.connection.execute("SELECT key, value FROM metadata").fetchall())

    def indexed_file_hash(self, relative_path: str) -> str | None:
        """Return the full content hash for one indexed repository-relative file."""
        if (not isinstance(relative_path, str) or not relative_path
                or relative_path.startswith(("/", "\\")) or ":" in relative_path
                or ".." in relative_path.replace("\\", "/").split("/")):
            raise ValueError("relative_path must be a normalized repository-relative path.")
        row = self.connection.execute(
            "SELECT content_hash FROM files WHERE relative_path = ?", (relative_path,)
        ).fetchone()
        return None if row is None else str(row[0])

    def check_repository(self, root: Path, replace_repository: bool = False) -> None:
        previous = self.metadata().get("repository")
        if previous and os.path.normcase(previous) != os.path.normcase(str(root)) and not replace_repository:
            raise IndexConflictError(
                "This index is bound to another repository. Use --replace-repository to explicitly replace its index."
            )

    def sync(self, root: Path, documents: list[dict], signature: str,
             replace_repository: bool = False, *, force_rebuild: bool = False) -> dict:
        if self.schema_version != INDEX_VERSION:
            raise StoreError("Writable index schema migration did not complete.")
        self.check_repository(root, replace_repository)
        old_root = self.metadata().get("repository")
        changing_owner = bool(old_root and os.path.normcase(old_root) != os.path.normcase(str(root)))
        rebuilding = bool(old_root and not changing_owner and force_rebuild)
        switching = changing_owner or rebuilding
        previous = {
            row["relative_path"]: row
            for row in self.connection.execute("SELECT * FROM files")
        }
        accepted = {document["relative_path"] for document in documents}
        added = changed = unchanged = removed = 0
        with self.connection:
            if switching:
                removed = len(previous) if changing_owner else 0
                self.connection.execute("DELETE FROM files")
                previous = {}
            for document in documents:
                relative = document["relative_path"]
                old = previous.get(relative)
                if old is None:
                    cursor = self.connection.execute(
                        "INSERT INTO files(relative_path, content_hash, size_bytes, mtime_ns, chunk_signature) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (relative, document["content_hash"], document["size_bytes"],
                         document["mtime_ns"], signature),
                    )
                    file_id = cursor.lastrowid
                    added += 1
                else:
                    file_id = old["id"]
                    same = old["content_hash"] == document["content_hash"] and old["chunk_signature"] == signature
                    self.connection.execute(
                        "UPDATE files SET content_hash = ?, size_bytes = ?, mtime_ns = ?, chunk_signature = ? WHERE id = ?",
                        (document["content_hash"], document["size_bytes"], document["mtime_ns"], signature, file_id),
                    )
                    if same:
                        unchanged += 1
                        continue
                    self.connection.execute(
                        "DELETE FROM vector_embeddings WHERE file_id = ?", (file_id,)
                    )
                    self.connection.execute("DELETE FROM chunks WHERE file_id = ?", (file_id,))
                    changed += 1
                self.connection.executemany(
                    "INSERT INTO chunks(file_id, start_line, end_line, text, content_hash) VALUES (?, ?, ?, ?, ?)",
                    [(file_id, chunk.start_line, chunk.end_line, chunk.text, chunk.content_hash)
                     for chunk in document["chunks"]],
                )
            for relative, old in previous.items():
                if relative not in accepted:
                    self.connection.execute("DELETE FROM files WHERE id = ?", (old["id"],))
                    removed += 1
            self._sync_structured(documents)
            for key, value in (
                ("repository", str(root)),
                ("repository_id", repository_identity(root)),
                ("indexed_at", datetime.now(timezone.utc).isoformat()),
                ("chunk_signature", signature),
                ("parser_version", "python-ast-v1"),
            ):
                self.connection.execute(
                    "INSERT INTO metadata(key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, value),
                )
        return {"added": added, "changed": changed, "unchanged": unchanged, "removed": removed}

    def _sync_structured(self, documents: list[dict]) -> None:
        """Rebuild bounded structured rows transactionally from the current scan."""
        self.connection.execute("DELETE FROM symbol_references")
        self.connection.execute("DELETE FROM symbols")
        self.connection.execute("DELETE FROM parse_status")
        file_ids = {
            row["relative_path"]: row["id"]
            for row in self.connection.execute("SELECT id, relative_path FROM files")
        }
        now = datetime.now(timezone.utc).isoformat()
        for document in documents:
            parsed = document.get("parsed_file")
            if parsed is None or parsed.relative_path not in file_ids:
                continue
            file_id = file_ids[parsed.relative_path]
            self.connection.execute(
                "INSERT INTO parse_status(file_id, parser_name, status, error, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (file_id, parsed.parser_name, parsed.status, parsed.error, now),
            )
            for symbol in parsed.symbols:
                self.connection.execute(
                    "INSERT INTO symbols(symbol_id, source_id, file_id, kind, name, qualified_name, "
                    "start_line, end_line, parent_symbol_id, signature, docstring, structure_hash) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        symbol.symbol_id, symbol.source_id, file_id, symbol.kind, symbol.name,
                        symbol.qualified_name, symbol.start_line, symbol.end_line,
                        symbol.parent_symbol_id, symbol.signature, symbol.docstring, symbol.structure_hash,
                    ),
                )
            for reference in parsed.references:
                self.connection.execute(
                    "INSERT INTO symbol_references(reference_id, source_id, source_file_id, source_symbol_id, "
                    "line, reference_type, target_name, target_qualified_name, resolution_status, "
                    "target_symbol_id, candidate_symbol_ids) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        reference.reference_id, reference.source_id, file_id, reference.source_symbol_id,
                        reference.line, reference.reference_type, reference.target_name,
                        reference.target_qualified_name, reference.resolution_status,
                        reference.target_symbol_id, json.dumps(reference.candidate_symbol_ids),
                    ),
                )
        self.connection.execute("""
            DELETE FROM vector_embeddings
            WHERE source_kind = 'symbol' AND NOT EXISTS (
                SELECT 1 FROM symbols
                WHERE symbols.symbol_id = vector_embeddings.symbol_id
                  AND symbols.file_id = vector_embeddings.file_id
                  AND symbols.structure_hash = vector_embeddings.content_hash
            )
        """)

    def status(self) -> dict:
        meta = self.metadata()
        result = {
            "database": self.database,
            "index_version": self.schema_version or int(meta["index_version"]),
            "repository": meta.get("repository"),
            "indexed_at": meta.get("indexed_at"),
            "files": self.connection.execute("SELECT COUNT(*) FROM files").fetchone()[0],
            "chunks": self.connection.execute("SELECT COUNT(*) FROM chunks").fetchone()[0],
            "symbol_index_available": (
                self.schema_version is not None and self.schema_version >= STRUCTURED_INDEX_VERSION
            ),
            "vector_index_available": self.schema_version == VECTOR_INDEX_VERSION,
        }
        if result["symbol_index_available"]:
            result.update({
                "symbols": self.connection.execute("SELECT COUNT(*) FROM symbols").fetchone()[0],
                "references": self.connection.execute("SELECT COUNT(*) FROM symbol_references").fetchone()[0],
                "parse_failures": self.connection.execute(
                    "SELECT COUNT(*) FROM parse_status WHERE status = 'failed'"
                ).fetchone()[0],
            })
        else:
            result.update({"symbols": 0, "references": 0, "parse_failures": 0})
        result["vectors"] = (
            self.connection.execute("SELECT COUNT(*) FROM vector_embeddings").fetchone()[0]
            if result["vector_index_available"] else 0
        )
        return result

    def search_symbols(
        self, *, name: str | None = None, qualified_name: str | None = None,
        kind: str | None = None, relative_path: str | None = None, limit: int = 100,
    ) -> list[dict]:
        if self.schema_version is None or self.schema_version < STRUCTURED_INDEX_VERSION:
            raise StoreError("Structured symbols are not available in this legacy index; run index --repo PATH to migrate it.")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ValueError("symbol limit must be between 1 and 500.")
        filters = []
        values = []
        for column, value, label in (
            ("symbols.name", name, "symbol name"),
            ("symbols.qualified_name", qualified_name, "qualified name"),
        ):
            if value is not None:
                if not isinstance(value, str) or not value.strip():
                    raise ValueError(f"{label} must not be empty.")
                filters.append(f"{column} = ? COLLATE NOCASE")
                values.append(value.strip())
        if kind is not None:
            if kind not in ("module", "class", "function", "method"):
                raise ValueError("symbol kind must be module, class, function or method.")
            filters.append("symbols.kind = ?")
            values.append(kind)
        if relative_path is not None:
            path = relative_path.replace("\\", "/")
            parts = path.split("/")
            if (not path or path.startswith("/") or ":" in path or any(p in ("", ".", "..") for p in parts)):
                raise ValueError("symbol path must be a normalized repository-relative path.")
            filters.append("files.relative_path = ? COLLATE NOCASE")
            values.append(path)
        if not filters:
            raise ValueError("Provide at least one of --name, --qualified-name, --kind or --path.")
        values.append(limit)
        query = (
            "SELECT symbols.symbol_id, symbols.source_id, files.relative_path, symbols.kind, symbols.name, "
            "symbols.qualified_name, symbols.start_line, symbols.end_line, symbols.parent_symbol_id, "
            "symbols.signature, symbols.docstring, symbols.structure_hash "
            "FROM symbols JOIN files ON files.id = symbols.file_id WHERE "
            + " AND ".join(filters)
            + " ORDER BY files.relative_path COLLATE NOCASE, symbols.start_line, symbols.end_line, "
              "symbols.qualified_name COLLATE NOCASE, symbols.symbol_id LIMIT ?"
        )
        return [dict(row) for row in self.connection.execute(query, values)]

    def search_symbols_by_ids(self, symbol_ids: list[str] | tuple[str, ...], *, limit: int = 100) -> list[dict]:
        """Fetch exact indexed definitions by parser-issued symbol identity.

        This is used to attach a definition excerpt to a uniquely resolved
        relationship. It deliberately does not fall back to name matching.
        """
        if self.schema_version is None or self.schema_version < STRUCTURED_INDEX_VERSION:
            raise StoreError("Structured symbols are not available in this legacy index; run index --repo PATH to migrate it.")
        if (not isinstance(symbol_ids, (list, tuple)) or not symbol_ids
                or len(symbol_ids) > 100
                or any(not isinstance(value, str) or not value.strip() or len(value) > 128
                       or any(ord(char) < 32 for char in value) for value in symbol_ids)):
            raise ValueError("symbol_ids must contain 1 to 100 valid identities.")
        if len(set(symbol_ids)) != len(symbol_ids):
            raise ValueError("symbol_ids must be unique.")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ValueError("symbol limit must be between 1 and 500.")
        placeholders = ",".join("?" for _ in symbol_ids)
        return [dict(row) for row in self.connection.execute(
            "SELECT symbols.symbol_id, symbols.source_id, files.relative_path, symbols.kind, "
            "symbols.name, symbols.qualified_name, symbols.start_line, symbols.end_line, "
            "symbols.parent_symbol_id, symbols.signature, symbols.docstring, symbols.structure_hash "
            "FROM symbols JOIN files ON files.id = symbols.file_id "
            f"WHERE symbols.symbol_id IN ({placeholders}) "
            "ORDER BY files.relative_path COLLATE NOCASE, symbols.start_line, "
            "symbols.end_line, symbols.symbol_id LIMIT ?",
            (*symbol_ids, limit),
        )]

    def search_references(
        self, *, relative_path: str | None = None, target: str | None = None,
        status: str | None = None, reference_type: str | None = None, limit: int = 100,
    ) -> list[dict]:
        if self.schema_version is None or self.schema_version < STRUCTURED_INDEX_VERSION:
            raise StoreError("Structured references are not available in this legacy index; run index --repo PATH to migrate it.")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise ValueError("reference limit must be between 1 and 500.")
        filters = []
        values = []
        if relative_path is not None:
            path = relative_path.replace("\\", "/")
            parts = path.split("/")
            if (not path or path.startswith("/") or ":" in path or any(p in ("", ".", "..") for p in parts)):
                raise ValueError("reference path must be a normalized repository-relative path.")
            filters.append("source.relative_path = ? COLLATE NOCASE")
            values.append(path)
        if target is not None:
            if not isinstance(target, str) or not target.strip():
                raise ValueError("reference target must not be empty.")
            filters.append("(refs.target_name = ? COLLATE NOCASE OR refs.target_qualified_name = ? COLLATE NOCASE)")
            values.extend((target.strip(), target.strip()))
        if status is not None:
            if status not in ("resolved", "unresolved", "ambiguous"):
                raise ValueError("reference status must be resolved, unresolved or ambiguous.")
            filters.append("refs.resolution_status = ?")
            values.append(status)
        if reference_type is not None:
            if reference_type not in ("import", "from_import", "call", "dynamic_import"):
                raise ValueError("reference type must be import, from_import, call or dynamic_import.")
            filters.append("refs.reference_type = ?")
            values.append(reference_type)
        if not filters:
            raise ValueError("Provide at least one of --path, --target, --status or --type.")
        values.append(limit)
        query = (
            "SELECT refs.reference_id, refs.source_id, refs.source_symbol_id, source.relative_path, refs.line, refs.reference_type, "
            "refs.target_name, refs.target_qualified_name, refs.resolution_status, refs.target_symbol_id, "
            "refs.candidate_symbol_ids, target_file.relative_path AS target_path "
            "FROM symbol_references AS refs "
            "JOIN files AS source ON source.id = refs.source_file_id "
            "LEFT JOIN symbols AS target ON target.symbol_id = refs.target_symbol_id "
            "LEFT JOIN files AS target_file ON target_file.id = target.file_id "
            + ("WHERE " + " AND ".join(filters) + " " if filters else "")
            + "ORDER BY source.relative_path COLLATE NOCASE, refs.line, refs.reference_type, "
              "refs.target_name COLLATE NOCASE, refs.reference_id LIMIT ?"
        )
        results = []
        for row in self.connection.execute(query, values):
            item = dict(row)
            item["candidate_symbol_ids"] = json.loads(item["candidate_symbol_ids"])
            results.append(item)
        return results

    def iter_chunks(self):
        return self.connection.execute(
            "SELECT files.relative_path, chunks.start_line, chunks.end_line, chunks.text, chunks.content_hash "
            "FROM chunks JOIN files ON files.id = chunks.file_id "
            "ORDER BY files.relative_path, chunks.start_line, chunks.end_line"
        )

    def iter_symbols(self):
        if self.schema_version is None or self.schema_version < STRUCTURED_INDEX_VERSION:
            raise StoreError("Structured symbols are not available in this legacy index.")
        return self.connection.execute(
            "SELECT symbols.symbol_id, symbols.source_id, files.relative_path, symbols.kind, "
            "symbols.name, symbols.qualified_name, symbols.start_line, symbols.end_line, "
            "symbols.signature, symbols.docstring, symbols.structure_hash "
            "FROM symbols JOIN files ON files.id = symbols.file_id "
            "ORDER BY files.relative_path COLLATE NOCASE, symbols.start_line, symbols.symbol_id"
        )

    def close(self) -> None:
        if self.connection is not None:
            self.connection.close()
            self.connection = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

