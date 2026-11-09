from datetime import datetime, timezone
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from agentforge.config import DATABASE_PATH
from agentforge.repositories import repository_identity
from agentforge.store import IndexStore
from tests.support import FIXTURE, LocalTestCase, snapshot
from tests.test_cli import invoke


class IntegrationTests(LocalTestCase):
    def setUp(self):
        super().setUp()
        self.application_root = self.sandbox / "agentforge-home"
        self.application_root.mkdir()
        self.data_dir = self.application_root / ".agentforge" / "data"
        self.index_dir = self.data_dir / "repositories"
        self.catalog_path = self.data_dir / "repositories.sqlite3"
        self.database_path = self.index_dir / f"{repository_identity(FIXTURE)}.sqlite3"
        self._storage_patches = [
            patch("agentforge.cli.DATABASE_PATH", self.data_dir / "index.sqlite3"),
            patch("agentforge.cli.REPOSITORY_CATALOG_PATH", self.catalog_path),
            patch("agentforge.repositories.PROJECT_ROOT", self.application_root),
            patch("agentforge.repositories.DATA_DIR", self.data_dir),
            patch("agentforge.repositories.DATABASE_PATH", self.data_dir / "index.sqlite3"),
            patch("agentforge.repositories.REPOSITORY_INDEX_DIR", self.index_dir),
            patch("agentforge.repositories.REPOSITORY_CATALOG_PATH", self.catalog_path),
            patch("agentforge.store.PROJECT_ROOT", self.application_root),
            patch("agentforge.store.DATA_DIR", self.data_dir),
            patch("agentforge.store.DATABASE_PATH", self.data_dir / "index.sqlite3"),
            patch("agentforge.store.REPOSITORY_INDEX_DIR", self.index_dir),
        ]
        for item in self._storage_patches:
            item.start()
        self.addCleanup(self._stop_storage_patches)

    def _stop_storage_patches(self):
        for item in reversed(self._storage_patches):
            item.stop()

    def test_full_cli_pipeline_preserves_synthetic_source(self):
        before = snapshot(FIXTURE)
        evidence_path = self.sandbox / "read_only_evidence.json"
        code, output, errors = invoke(["index", "--repo", str(FIXTURE)])
        self.assertEqual((code, errors), (0, ""))
        first = json.loads(output)
        self.assertEqual(first["files"], 4)
        self.assertEqual(first["chunks"], 4)
        self.assertEqual(Path(first["database"]).resolve(), self.database_path.resolve())
        self.assertNotEqual(first["database"], str(DATABASE_PATH))

        # 旧索引只依据窗口与重叠参数生成。新的行级
        # 算法签名必须在重新索引时使旧分块失效。
        with IndexStore(self.database_path) as indexed:
            indexed.connection.execute("UPDATE files SET chunk_signature = ?", ("80:10",))
            indexed.connection.commit()
        code, output, errors = invoke(["index", "--repo", str(FIXTURE)])
        self.assertEqual((code, errors), (0, ""))
        migrated = json.loads(output)
        self.assertEqual((migrated["files"], migrated["changed"]), (4, 4))

        code, output, errors = invoke(["index", "--repo", str(FIXTURE)])
        self.assertEqual((code, errors), (0, ""))
        second = json.loads(output)
        self.assertEqual((second["files"], second["chunks"], second["unchanged"]), (4, 4, 4))

        code, output, errors = invoke(["search", "refresh_access_token", "--top-k", "5"])
        self.assertEqual((code, errors), (0, ""))
        results = json.loads(output)["results"]
        self.assertEqual(results[0]["relative_path"], "src/cache.py")
        expected_rows = [
            '"""Synthetic session cache helpers."""', "", "",
            "def refresh_access_token(session_id):",
            '    """Return a deterministic placeholder for a session."""',
            '    return f"offline-{session_id}"', "", "",
            "def cache_lookup(key, cache):",
            '    """Read an existing cache entry without modifying the cache."""',
            "    return cache.get(key)", "",
        ]
        expected_doc_rows = [
            "# Cache design", "",
            "cache_lookup reads one existing entry.",
            "refresh_access_token returns a deterministic placeholder.",
            "缓存查询只读取已有条目。", "",
        ]
        self.assertEqual((FIXTURE / "src" / "cache.py").read_text(encoding="utf-8"),
                         "\n".join(expected_rows) + "\n")
        self.assertEqual((FIXTURE / "docs" / "cache.md").read_text(encoding="utf-8"),
                         "\n".join(expected_doc_rows) + "\n")
        expected_by_path = {
            "src/cache.py": expected_rows,
            "docs/cache.md": expected_doc_rows,
        }
        for item in results:
            self.assertIn(item["relative_path"], expected_by_path)
            self.assertEqual(
                item["text"],
                "\n".join(expected_by_path[item["relative_path"]][item["start_line"] - 1:item["end_line"]]),
            )
        code, answer, errors = invoke(["ask", "refresh_access_token", "--mode", "repository"])
        self.assertEqual((code, errors), (0, ""))
        payload = json.loads(answer)
        self.assertEqual(payload["provider"], "offline")
        self.assertEqual(payload["answer_contract"]["status"], "limited")
        self.assertEqual(
            payload["answer_contract"]["evidence"][0]["relative_path"],
            "src/cache.py",
        )
        self.assertEqual(
            (payload["answer_contract"]["evidence"][0]["start_line"],
             payload["answer_contract"]["evidence"][0]["end_line"]),
            (4, 6),
        )
        code, unknown, errors = invoke(["ask", "qzxnonexistent", "--mode", "repository"])
        self.assertEqual((code, errors), (0, ""))
        unknown_payload = json.loads(unknown)
        self.assertEqual(unknown_payload["answer_contract"]["status"], "refused")
        self.assertIsNone(unknown_payload["answer"])
        self.assertIn("无法从当前索引中确定", unknown_payload["refused_output"])
        code, output, errors = invoke(["status"])
        self.assertEqual((code, errors), (0, ""))
        self.assertIsNotNone(json.loads(output)["indexed_at"])
        self.assertEqual(Path(json.loads(output)["database"]).resolve(), self.database_path.resolve())

        after = snapshot(FIXTURE)
        self.assertEqual(before, after)
        evidence = {
            "verified_at_utc": datetime.now(timezone.utc).isoformat(),
            "repository": str(FIXTURE),
            "database": str(self.database_path),
            "unchanged": before == after,
            "before": before,
            "after": after,
            "indexed_files": first["files"],
            "indexed_chunks": first["chunks"],
            "scope": "Handwritten fixture only; Agentexpr was not read.",
        }
        evidence_path.write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        self.assertEqual(evidence_path.parent, self.sandbox)

