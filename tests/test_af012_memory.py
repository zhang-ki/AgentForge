"""Memory lifecycle tests. SQLite here is isolated logic testing, not PG evidence."""
from datetime import datetime, timedelta, timezone
import importlib.util
import json
import unittest
from unittest.mock import Mock


@unittest.skipUnless(importlib.util.find_spec("sqlalchemy") and importlib.util.find_spec("alembic"),
                     "AF-012 persistence dependencies unavailable")
class MemoryStoreTests(unittest.TestCase):
    def setUp(self):
        from sqlalchemy import create_engine, event
        from agentforge.conversation_migrations import upgrade
        from agentforge.conversation_store import ConversationStore
        from agentforge.memory_store import MemoryStore
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        @event.listens_for(self.engine, "connect")
        def fk(connection, _record):
            connection.execute("PRAGMA foreign_keys=ON")
        upgrade(self.engine)
        self.conversations = ConversationStore(self.engine)
        self.memories = MemoryStore(self.engine)
        self.repo = "repo-memory-test"
        self.conversation = self.conversations.create_conversation(self.repo, "memory source")
        self.conversations.append_turn(self.conversation["id"], self.repo,
                                       "I prefer concise answers.", "I will keep answers concise.")
        self.messages = self.conversations.get_messages(self.conversation["id"], self.repo)

    def tearDown(self):
        self.engine.dispose()

    def propose(self, **kwargs):
        return self.memories.propose(self.repo, self.conversation["id"],
            "User prefers concise answers.", [self.messages[0]["id"]],
            memory_type="preference", **kwargs)

    def test_pending_approval_rejection_and_repository_scope(self):
        pending = self.propose()
        self.assertEqual("pending", pending["approval_status"])
        self.assertEqual([], self.memories.approved(self.repo))
        with self.assertRaisesRegex(ValueError, "current repository"):
            self.memories.approve(pending["id"], "other-repo")
        approved = self.memories.approve(pending["id"], self.repo)
        self.assertEqual("approved", approved["approval_status"])
        self.assertEqual(pending["id"], self.memories.approved(self.repo)[0]["id"])
        self.assertEqual({"pending": 1}, self.memories.outbox_status(self.repo))
        rejected = self.propose()
        self.memories.reject(rejected["id"], self.repo)
        self.assertEqual("rejected", self.memories.get(rejected["id"], self.repo)["approval_status"])

    def test_update_requires_reapproval_and_revoke_excludes_memory(self):
        memory = self.propose()
        self.memories.approve(memory["id"], self.repo)
        updated = self.memories.update(memory["id"], self.repo, "User prefers short, direct answers.")
        self.assertEqual("pending", updated["approval_status"])
        self.assertEqual(2, updated["version"])
        self.assertEqual([], self.memories.approved(self.repo))
        self.memories.approve(memory["id"], self.repo)
        revoked = self.memories.revoke(memory["id"], self.repo)
        self.assertEqual("revoked", revoked["approval_status"])
        self.assertIsNone(self.memories.approved_by_id(memory["id"], self.repo))
        self.assertEqual("revoked", self.memories.events(memory["id"], self.repo)[-1]["event_type"])

    def test_expiry_and_deleting_source_conversation_do_not_resurrect_memory(self):
        memory = self.propose(expires_at=datetime.now(timezone.utc) + timedelta(days=1))
        self.memories.approve(memory["id"], self.repo)
        source = self.memories.source_records(memory["id"], self.repo)[0]
        self.assertEqual(self.messages[0]["id"], source["message_id"])
        from sqlalchemy import update
        from agentforge.af012_schema import Memory
        from uuid import UUID
        with self.engine.begin() as connection:
            connection.execute(update(Memory).where(Memory.id == UUID(memory["id"])).values(
                expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
        self.assertEqual(1, self.memories.expire_due())
        self.assertIsNone(self.memories.approved_by_id(memory["id"], self.repo))
        self.assertEqual("expired", self.memories.get(memory["id"], self.repo)["approval_status"])
        second = self.propose()
        self.conversations.delete_conversation(self.conversation["id"], self.repo)
        source = self.memories.source_records(second["id"], self.repo)[0]
        self.assertIsNone(source["message_id"])
        self.assertEqual(1, source["source_sequence"])

    def test_sources_and_secret_like_content_are_checked(self):
        other = self.conversations.create_conversation("other-repo", "other")
        with self.assertRaisesRegex(ValueError, "source message"):
            self.memories.propose(self.repo, self.conversation["id"], "fact",
                                  ["00000000-0000-0000-0000-000000000001"])
        with self.assertRaisesRegex(ValueError, "credential"):
            self.memories.propose(self.repo, self.conversation["id"], "password=do-not-store",
                                  [self.messages[0]["id"]])
        with self.assertRaisesRegex(ValueError, "requested repository"):
            self.memories.propose(self.repo, other["id"], "fact", [self.messages[0]["id"]])

    def test_outbox_failure_is_audited_then_explicit_retry_is_idempotent(self):
        pending = self.propose()
        self.memories.approve(pending["id"], self.repo)
        vector_store = Mock()
        vector_store.upsert.side_effect = [RuntimeError("hidden adapter detail"), None]
        class Provider:
            model_id = "embedding-test"
            dimension = 2
            def embed_document(self, text): return (1.0, 0.0)
        first = self.memories.sync_outbox(vector_store, Provider())
        self.assertEqual({"claimed": 1, "completed": 0, "failed": 1, "dead": 0, "stale_skipped": 0}, first)
        from sqlalchemy import select
        from agentforge.af012_schema import VectorSyncOutbox
        with self.engine.connect() as connection:
            row = connection.execute(select(VectorSyncOutbox)).mappings().one()
        self.assertEqual("pending", row["status"])
        self.assertEqual("RuntimeError", row["last_error"])
        second = self.memories.sync_outbox(vector_store, Provider())
        self.assertEqual(1, second["completed"])
        self.assertEqual(2, vector_store.upsert.call_count)

    def test_chroma_memory_retrieval_preserves_source_identity_and_repository_scope(self):
        from agentforge.embeddings.chroma_store import ChromaCandidateStore
        from agentforge.memory_retrieval import ChromaMemoryRetriever

        class Provider:
            model_id = "memory-test-model"
            dimension = 2
            def embed(self, _text): return (1.0, 0.0)
            def embed_document(self, text): return self.embed(text)
            def embed_query(self, text): return self.embed(text)

        class Collection:
            def __init__(self): self.rows = {}
            def upsert(self, *, ids, embeddings, metadatas, documents=None):
                for i, identity in enumerate(ids):
                    self.rows[identity] = {"embedding": embeddings[i], "metadata": metadatas[i],
                        "document": documents[i] if documents else None}
            def query(self, *, query_embeddings, n_results, where, include):
                filters = {item_key: value for group in where["$and"] for item_key, value in group.items()}
                rows = [(identity, row) for identity, row in self.rows.items()
                        if all(row["metadata"].get(key) == value for key, value in filters.items())]
                rows = rows[:n_results]
                return {"ids": [[identity for identity, _ in rows]],
                    "metadatas": [[row["metadata"] for _, row in rows]],
                    "distances": [[0.1 for _ in rows]],
                    "documents": [[row["document"] for _, row in rows]]}
            def get(self, *, where=None, ids=None, limit=None, offset=0, include=None):
                rows = list(self.rows.items())
                if ids is not None:
                    rows = [(identity, row) for identity, row in rows if identity in ids]
                if where:
                    filters = {item_key: value for group in where["$and"] for item_key, value in group.items()}
                    rows = [(identity, row) for identity, row in rows
                            if all(row["metadata"].get(key) == value for key, value in filters.items())]
                rows = rows[offset: offset + limit if limit is not None else None]
                return {"ids": [identity for identity, _ in rows],
                    "metadatas": [row["metadata"] for _, row in rows]}
            def delete(self, *, ids):
                for identity in ids: self.rows.pop(identity, None)

        memory = self.propose()
        self.memories.approve(memory["id"], self.repo)
        collection = Collection()
        vectors = ChromaCandidateStore(collection, dimension=2)
        sync = self.memories.sync_outbox(vectors, Provider())
        self.assertEqual(1, sync["completed"])
        retriever = ChromaMemoryRetriever(collection, Provider(), self.memories)
        found = retriever.search("A differently worded style preference query", repository_id=self.repo)
        self.assertEqual(1, len(found))
        candidate = found[0]
        self.assertEqual(memory["id"], candidate.memory_id)
        self.assertEqual("preference", candidate.memory_type)
        self.assertEqual(1, candidate.version)
        self.assertEqual(self.messages[0]["id"], candidate.sources[0].message_id)
        self.assertEqual(self.messages[0]["sequence"], candidate.sources[0].source_sequence)
        self.assertEqual("user", candidate.sources[0].source_role)
        self.assertEqual([], retriever.search("same query", repository_id="another-repo"))

        # The old vector is deliberately left behind: PostgreSQL approval state remains authoritative.
        self.memories.revoke(memory["id"], self.repo)
        self.assertEqual([], retriever.search("same query", repository_id=self.repo))

        expiring = self.propose(expires_at=datetime.now(timezone.utc) + timedelta(days=1))
        self.memories.approve(expiring["id"], self.repo)
        self.assertGreaterEqual(self.memories.sync_outbox(vectors, Provider())["completed"], 1)
        from sqlalchemy import update
        from agentforge.af012_schema import Memory
        from uuid import UUID
        with self.engine.begin() as connection:
            connection.execute(update(Memory).where(Memory.id == UUID(expiring["id"])).values(
                expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
        expired_results = retriever.search("same query", repository_id=self.repo)
        self.assertFalse(any(item.memory_id == expiring["id"] for item in expired_results))


@unittest.skipUnless(importlib.util.find_spec("sqlalchemy") and importlib.util.find_spec("alembic"),
                     "AF-012 persistence dependencies unavailable")
class MemoryExtractionTests(unittest.TestCase):
    def test_suggestions_are_untrusted_and_only_user_source_messages_are_allowed(self):
        from agentforge.memory_extraction import extract_memory_candidates
        client = Mock()
        client.chat.return_value = json.dumps({"candidates": [
            {"content": "User prefers concise answers.", "memory_type": "preference", "source_sequences": [1]},
            {"content": "fake", "memory_type": "other", "source_sequences": [2]},
            {"content": "api_key=secret", "memory_type": "other", "source_sequences": [1]},
            {"content": "invalid ref", "memory_type": "other", "source_sequences": [99]},
        ]})
        messages = [
            {"sequence": 1, "role": "user", "status": "completed", "content": "I prefer concise answers."},
            {"sequence": 2, "role": "assistant", "status": "completed", "content": "Model claim."},
        ]
        result = extract_memory_candidates(client, "local-model", messages)
        self.assertEqual(1, len(result))
        self.assertEqual([1], result[0]["source_sequences"])
        self.assertEqual("user", result[0]["source_messages"][0]["role"])
        self.assertIn("untrusted data", client.chat.call_args.args[0][0]["content"])

    def test_malformed_model_json_fails_without_proposal(self):
        from agentforge.memory_extraction import extract_memory_candidates
        client = Mock()
        client.chat.return_value = "not json"
        with self.assertRaisesRegex(ValueError, "not valid JSON"):
            extract_memory_candidates(client, "local-model", [
                {"sequence": 1, "role": "user", "status": "completed", "content": "I prefer concise answers."}])
