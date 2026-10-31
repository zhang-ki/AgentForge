from agentforge.indexer import index_repository
from agentforge.retriever import search
from agentforge.store import IndexStore
from tests.support import LocalTestCase


class RetrieverTests(LocalTestCase):
    def setUp(self):
        super().setUp()
        self.write("src/auth.py", "def refresh_access_token(session_id):\n    return session_id\n")
        self.write("docs/cache.md", "Cache lookup reads an existing session.\n缓存查询只读。\n")
        self.write("docs/duplicate.md", "Cache lookup reads an existing session.\n缓存查询只读。\n")
        self.store = IndexStore(":memory:")
        self.addCleanup(self.store.close)
        index_repository(self.root, self.store)

    def test_identifier_and_path_rank_correct_file(self):
        self.assertEqual(search(self.store, "refresh_access_token")[0].relative_path, "src/auth.py")
        self.assertEqual(search(self.store, "src/auth.py")[0].relative_path, "src/auth.py")

    def test_natural_keywords_stability_and_deduplication(self):
        results = search(self.store, "cache session", 10)
        self.assertEqual(results, search(self.store, "cache session", 10))
        self.assertEqual(len({item.content_hash for item in results}), len(results))
        self.assertTrue(results[0].relative_path.startswith("docs/"))
        self.assertEqual(len(search(self.store, "session", 1)), 1)

    def test_no_results_and_invalid_queries(self):
        self.assertEqual(search(self.store, "qzxnonexistent"), [])
        for query, count in [("", 5), ("x" * 513, 5), ("session", 0), ("session", 101)]:
            with self.assertRaises(ValueError):
                search(self.store, query, count)

    def test_chinese_keyword_substring(self):
        self.assertEqual(search(self.store, "缓存")[0].relative_path, "docs/cache.md")

