"""持久化仓库身份和活动仓库选择。

本模块绝不修改仓库源码树。仓库目录保存
规范化路径，并将每个仓库关联到独立的 AgentForge 索引。
"""

from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3
import stat

from .config import DATA_DIR, DATABASE_PATH, PROJECT_ROOT, REPOSITORY_CATALOG_PATH, REPOSITORY_INDEX_DIR
from .errors import StoreError
from .repository_identity import canonical_repository, repository_identity


def _ordinary(path: Path, *, directory: bool) -> None:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise StoreError("Repository catalog storage cannot use links or reparse points.")
    if directory != stat.S_ISDIR(info.st_mode):
        raise StoreError("Repository catalog storage has an unexpected path type.")
    if not directory and (not stat.S_ISREG(info.st_mode) or info.st_nlink > 1):
        raise StoreError("Repository catalog must be an ordinary, unshared file.")


def _ensure_data_directory() -> None:
    _ordinary(PROJECT_ROOT, directory=True)
    app_dir = PROJECT_ROOT / ".agentforge"
    for directory in (app_dir, DATA_DIR):
        if directory.exists() or directory.is_symlink():
            _ordinary(directory, directory=True)
        else:
            directory.mkdir()


class RepositoryRegistry:
    """用于稳定仓库 ID 和活动仓库选择的本地小型目录。"""

    def __init__(self, database: str | os.PathLike[str] | None = None, *, readonly: bool = False):
        if database is None:
            database = REPOSITORY_CATALOG_PATH
        self.database = str(database)
        self.connection: sqlite3.Connection | None = None
        if self.database == ":memory:":
            if readonly:
                raise StoreError("An in-memory repository catalog cannot be opened read-only.")
            target = self.database
        else:
            supplied = Path(os.path.abspath(self.database))
            if os.path.normcase(str(supplied)) != os.path.normcase(str(REPOSITORY_CATALOG_PATH)):
                raise StoreError("Repository catalog path is fixed to AgentForge/.agentforge/data/repositories.sqlite3.")
            if readonly:
                for directory in (PROJECT_ROOT, PROJECT_ROOT / ".agentforge", DATA_DIR):
                    if not directory.exists() or directory.is_symlink():
                        raise StoreError("Repository catalog storage path is unavailable.")
                    _ordinary(directory, directory=True)
            else:
                _ensure_data_directory()
            existed = supplied.exists() or supplied.is_symlink()
            if existed:
                _ordinary(supplied, directory=False)
                for suffix in ("-journal", "-wal", "-shm"):
                    sidecar = supplied.with_name(supplied.name + suffix)
                    if sidecar.exists() or sidecar.is_symlink():
                        _ordinary(sidecar, directory=False)
            elif readonly:
                raise StoreError("Repository catalog does not exist.")
            target = self.database
        try:
            if readonly and self.database != ":memory:":
                target = Path(self.database).as_uri() + "?mode=ro"
            self.connection = sqlite3.connect(target, uri=readonly and self.database != ":memory:", timeout=2)
            self.connection.row_factory = sqlite3.Row
            self.connection.execute("PRAGMA foreign_keys = ON")
            self.connection.execute("PRAGMA busy_timeout = 2000")
            if not readonly:
                self.connection.executescript("""
                    CREATE TABLE IF NOT EXISTS repositories (
                        repository_id TEXT PRIMARY KEY,
                        canonical_path TEXT NOT NULL,
                        display_name TEXT NOT NULL,
                        index_path TEXT NOT NULL,
                        indexed_at TEXT,
                        registered_at TEXT NOT NULL,
                        UNIQUE(repository_id, canonical_path)
                    );
                    CREATE TABLE IF NOT EXISTS settings (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );
                """)
            else:
                self.connection.execute("SELECT 1 FROM repositories LIMIT 0")
        except (sqlite3.Error, OSError) as exc:
            self.close()
            raise StoreError(f"Cannot open repository catalog ({type(exc).__name__}).") from None

    def close(self) -> None:
        if self.connection is not None:
            self.connection.close()
            self.connection = None

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()

    @staticmethod
    def index_path(repository_id: str) -> Path:
        if not isinstance(repository_id, str) or len(repository_id) != 64 or any(
            char not in "0123456789abcdef" for char in repository_id
        ):
            raise ValueError("Repository ID is invalid.")
        return REPOSITORY_INDEX_DIR / f"{repository_id}.sqlite3"

    def register(self, path: str | os.PathLike[str], *, display_name: str | None = None,
                 index_path: str | os.PathLike[str] | None = None) -> dict:
        root = canonical_repository(path)
        repo_id = repository_identity(root)
        name = display_name.strip() if isinstance(display_name, str) and display_name.strip() else root.name
        if len(name) > 200 or any(ord(char) < 32 for char in name):
            raise ValueError("Repository display name is invalid.")
        db_path = Path(index_path) if index_path is not None else self.index_path(repo_id)
        if db_path not in (DATABASE_PATH, self.index_path(repo_id)):
            raise ValueError("Repository index path is outside AgentForge storage.")
        with self.connection:
            self.connection.execute("""
                INSERT INTO repositories(repository_id, canonical_path, display_name, index_path, indexed_at, registered_at)
                VALUES (?, ?, ?, ?, NULL, ?)
                ON CONFLICT(repository_id) DO UPDATE SET
                    canonical_path=excluded.canonical_path,
                    display_name=excluded.display_name,
                    index_path=CASE WHEN repositories.indexed_at IS NULL THEN excluded.index_path ELSE repositories.index_path END
            """, (repo_id, str(root), name, str(db_path), datetime.now(timezone.utc).isoformat()))
        return self.get(repo_id)

    def get(self, repository_id: str) -> dict:
        row = self.connection.execute(
            "SELECT * FROM repositories WHERE repository_id = ?", (repository_id,)
        ).fetchone()
        if row is None:
            raise ValueError("Repository is not registered.")
        return dict(row)

    def by_path(self, path: str | os.PathLike[str]) -> dict | None:
        root = canonical_repository(path)
        row = self.connection.execute(
            "SELECT * FROM repositories WHERE repository_id = ?", (repository_identity(root),)
        ).fetchone()
        return None if row is None else dict(row)

    def list(self) -> list[dict]:
        active = self.active_id()
        return [dict(row) | {"active": row["repository_id"] == active}
                for row in self.connection.execute(
                    "SELECT * FROM repositories ORDER BY display_name COLLATE NOCASE, repository_id")]

    def mark_indexed(self, repository_id: str) -> dict:
        db_path = Path(self.get(repository_id)["index_path"])
        if not db_path.exists():
            raise StoreError("Repository index was not created.")
        with self.connection:
            self.connection.execute("UPDATE repositories SET indexed_at = ? WHERE repository_id = ?",
                (datetime.now(timezone.utc).isoformat(), repository_id))
        return self.get(repository_id)

    def set_index_path(self, repository_id: str, index_path: str | os.PathLike[str]) -> dict:
        path = Path(index_path)
        if path != DATABASE_PATH and path != self.index_path(repository_id):
            raise ValueError("Repository index path is outside AgentForge storage.")
        with self.connection:
            self.connection.execute("UPDATE repositories SET index_path = ? WHERE repository_id = ?",
                                    (str(path), repository_id))
        return self.get(repository_id)

    def activate(self, repository_id: str) -> dict:
        item = self.get(repository_id)
        if not item["indexed_at"] or not Path(item["index_path"]).is_file():
            raise ValueError("Repository has no completed index; run repo index first.")
        with self.connection:
            self.connection.execute("""
                INSERT INTO settings(key, value) VALUES ('active_repository_id', ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """, (repository_id,))
        return item

    def active_id(self) -> str | None:
        row = self.connection.execute(
            "SELECT value FROM settings WHERE key='active_repository_id'"
        ).fetchone()
        return None if row is None else str(row[0])

    def active(self) -> dict | None:
        repo_id = self.active_id()
        return None if repo_id is None else self.get(repo_id)

    def remove_registration(self, repository_id: str) -> dict:
        item = self.get(repository_id)
        if self.active_id() == repository_id:
            raise ValueError("Switch to another indexed repository before removing the active repository.")
        with self.connection:
            self.connection.execute("DELETE FROM repositories WHERE repository_id = ?", (repository_id,))
        return item


def open_index_for_repository(path: str | os.PathLike[str], *, readonly: bool = False):
    """打开仓库专属索引；仅当旧版索引确属该仓库时才复用。"""
    from .store import IndexStore

    root = canonical_repository(path)
    repo_id = repository_identity(root)
    dedicated = RepositoryRegistry.index_path(repo_id)
    if dedicated.exists() or dedicated.is_symlink():
        return IndexStore(dedicated, readonly=readonly)
    if DATABASE_PATH.exists() or DATABASE_PATH.is_symlink():
        with IndexStore(DATABASE_PATH, readonly=True) as legacy:
            owner = legacy.metadata().get("repository")
        if owner and os.path.normcase(owner) == os.path.normcase(str(root)):
            return IndexStore(DATABASE_PATH, readonly=readonly)
    return IndexStore(dedicated, readonly=readonly)


def open_active_index(*, readonly: bool = False):
    if not REPOSITORY_CATALOG_PATH.exists():
        from .store import IndexStore
        return IndexStore(readonly=readonly)
    with RepositoryRegistry(readonly=True) as registry:
        active = registry.active()
    if active is not None:
        return open_index_for_repository(active["canonical_path"], readonly=readonly)
    raise StoreError("No active repository is selected. Run repo list or repo switch REPOSITORY_ID.")
