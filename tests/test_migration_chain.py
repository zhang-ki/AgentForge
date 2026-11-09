"""Migration graph and isolated SQLite integration checks."""
from pathlib import Path
import tempfile
import unittest

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text

from agentforge.conversation_migrations import upgrade
from agentforge.conversation_store import ConversationStore
from agentforge.models import Answer
from agentforge.session_service import SessionService


class MigrationGraphTests(unittest.TestCase):
    def test_restored_preference_chain_and_merge_have_one_head(self):
        config = Config(str(Path(__file__).parents[1] / "alembic.ini"))
        script = ScriptDirectory.from_config(config)

        self.assertEqual(script.get_revision("af012cap_005").down_revision, "af012mm_004")
        self.assertEqual(script.get_revision("af012pref_006").down_revision, "af012cap_005")
        self.assertEqual(
            set(script.get_revision("af013mg_002").down_revision),
            {"af012pref_006", "af013ae_001"},
        )
        self.assertEqual(script.get_heads(), ["af013mg_002"])

    def test_isolated_migration_is_repeatable_and_session_restores(self):
        with tempfile.TemporaryDirectory(
                prefix=".agentforge-migration-", dir=Path(__file__).parent) as temporary:
            database_url = f"sqlite+pysqlite:///{Path(temporary) / 'isolated.sqlite3'}"
            engine = create_engine(database_url)
            try:
                config = Config(str(Path(__file__).parents[1] / "alembic.ini"))
                with engine.begin() as connection:
                    config.attributes["connection"] = connection
                    command.upgrade(config, "af012pref_006")

                # Reproduce the reported state: the DB references pref_006,
                # then the restored graph must advance the sibling branch and merge.
                with engine.connect() as connection:
                    self.assertEqual(
                        connection.execute(
                            text("SELECT version_num FROM alembic_version")
                        ).scalars().all(),
                        ["af012pref_006"],
                    )

                upgrade(engine)
                upgrade(engine)

                with engine.connect() as connection:
                    versions = connection.execute(text("SELECT version_num FROM alembic_version")).scalars().all()
                    self.assertEqual(versions, ["af013mg_002"])
                columns = {column["name"] for column in inspect(engine).get_columns("conversations")}
                self.assertTrue({"capability_mode", "presentation_preferences", "provider_id"}.issubset(columns))

                repository_id = "migration-integration-repository"
                store = ConversationStore(engine)
                service = SessionService()
                conversation = service.create(store, repository_id, "migration verification")
                service.commit_completed_turn(
                    store, conversation["id"], repository_id,
                    "What was restored?", Answer("The isolated session was restored.", (), True),
                )
                engine.dispose()
                engine = create_engine(database_url)
                store = ConversationStore(engine)
                restored = service.load_context(store, conversation["id"], repository_id)
                self.assertEqual(len(restored.history), 2)
                self.assertEqual(restored.history[0].content, "What was restored?")
                self.assertEqual(restored.history[1].content, "The isolated session was restored.")
                self.assertEqual(store.get_conversation(conversation["id"], repository_id)["title"],
                                 "migration verification")
            finally:
                engine.dispose()


if __name__ == "__main__":
    unittest.main()
