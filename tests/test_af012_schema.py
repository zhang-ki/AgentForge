"""SQLAlchemy 模型合同检查；这些不是 PostgreSQL 集成测试。"""
import importlib.util
import unittest


@unittest.skipUnless(importlib.util.find_spec("sqlalchemy"), "SQLAlchemy optional dependency unavailable")
class AF012SchemaTests(unittest.TestCase):
    def test_eight_tables_compile_for_postgresql(self):
        from sqlalchemy.dialects import postgresql
        from sqlalchemy.schema import CreateTable
        from agentforge.af012_schema import Base
        expected = {"conversations", "messages", "conversation_summaries", "conversation_selections",
                    "repository_metadata", "memories", "memory_sources", "memory_approvals",
                    "memory_events", "vector_sync_outbox"}
        self.assertEqual(set(Base.metadata.tables), expected)
        for table in Base.metadata.sorted_tables:
            with self.subTest(table=table.name):
                ddl = str(CreateTable(table).compile(dialect=postgresql.dialect()))
                self.assertIn("CREATE TABLE", ddl)
                if "id" in table.columns:
                    self.assertIn("UUID", ddl)

    def test_messages_and_outbox_have_idempotency_constraints(self):
        from sqlalchemy import UniqueConstraint
        from agentforge.af012_schema import Base
        def uniques(name):
            return {tuple(column.name for column in item.columns)
                    for item in Base.metadata.tables[name].constraints
                    if isinstance(item, UniqueConstraint)}
        self.assertIn(("conversation_id", "sequence_number"), uniques("messages"))
        self.assertIn(("repository_id", "source_type", "source_id", "source_version", "action"),
                      uniques("vector_sync_outbox"))
