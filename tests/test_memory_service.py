import unittest
from unittest.mock import Mock

from agentforge.conversation_context import MemoryCandidate, MemorySourceReference
from agentforge.memory_service import MemoryService


class MemoryServiceTests(unittest.TestCase):
    def test_lifecycle_and_outbox_operations_delegate_to_repository(self):
        repository = Mock()
        repository.list.return_value = ["memory"]
        repository.get.return_value = {"memory_id": "m1"}
        repository.propose.return_value = "proposal"
        repository.approve.return_value = "approved"
        repository.reject.return_value = "rejected"
        repository.revoke.return_value = "revoked"
        repository.expire_due.return_value = 2
        repository.approved.return_value = ["approved-memory"]
        repository.sync_outbox.return_value = {"synced": 1}
        repository.outbox_status.return_value = {"pending": 0}
        repository.source_records.return_value = ["source"]
        repository.events.return_value = ["event"]
        repository.requeue_approved.return_value = 1
        service = MemoryService(repository)
        vector_store, embedding = object(), object()

        self.assertEqual(["memory"], service.list("repo-1", status="approved"))
        self.assertEqual({"memory_id": "m1"}, service.get("m1", "repo-1"))
        self.assertEqual("proposal", service.propose("repo-1", "c1", "fact", ["msg-1"]))
        self.assertEqual("approved", service.approve("m1", "repo-1"))
        self.assertEqual("rejected", service.reject("m1", "repo-1"))
        self.assertEqual("revoked", service.revoke("m1", "repo-1"))
        self.assertEqual(2, service.expire_due(now="bounded-test-time"))
        self.assertEqual(["approved-memory"], service.approved("repo-1"))
        self.assertEqual({"synced": 1}, service.sync_outbox(vector_store, embedding))
        self.assertEqual({"pending": 0}, service.outbox_status("repo-1"))
        self.assertEqual(["source"], service.source_records("m1", "repo-1"))
        self.assertEqual(["event"], service.events("m1", "repo-1"))
        self.assertEqual(1, service.requeue_approved("repo-1"))
        repository.sync_outbox.assert_called_once_with(vector_store, embedding)
        repository.expire_due.assert_called_once_with(now="bounded-test-time")

    def test_update_preserves_existing_expiry_when_no_new_expiry_is_supplied(self):
        repository = Mock()
        expiry = object()
        repository.get.return_value = {"expires_at": expiry}
        service = MemoryService(repository)
        service.update("memory-1", "repo-1", "updated content", memory_type="preference")
        repository.update.assert_called_once_with(
            "memory-1", "repo-1", "updated content", memory_type="preference",
            expires_at=expiry,
        )

    def test_approved_retrieval_is_scoped_to_the_requested_repository(self):
        repository = Mock()
        retriever = Mock()
        candidate = MemoryCandidate("repo-2", "approved fact", True, "memory-1",
            "project_fact", 1, (MemorySourceReference("message-1", 1, "user", "a" * 64),),
            "repository")
        retriever.search.return_value = [candidate]
        repository.approved_by_id.return_value = {"id": "memory-1", "approval_status": "approved",
            "scope": "repository", "version": 1, "memory_type": "project_fact",
            "content": "approved fact"}
        repository.source_records.return_value = [{"message_id": "message-1", "source_sequence": 1,
            "source_role": "user", "source_hash": "a" * 64}]
        result = MemoryService(repository).retrieve_approved(retriever, "question", "repo-2")
        self.assertEqual((candidate,), result)
        retriever.search.assert_called_once_with("question", repository_id="repo-2", limit=4)

    def test_approved_retrieval_filters_unapproved_cross_scope_and_untraceable_candidates(self):
        repository = Mock()
        retriever = Mock()
        valid = MemoryCandidate("repo-2", "valid", True, "m1", "project_fact", 1,
            (MemorySourceReference("msg-1", 1, "user", "b" * 64),), "repository")
        retriever.search.return_value = [
            MemoryCandidate("repo-2", "pending", False, "m2", "project_fact", 1,
                (MemorySourceReference("msg-2", 2, "user", "c" * 64),), "repository"),
            MemoryCandidate("repo-other", "private", True, "m3", "project_fact", 1,
                (MemorySourceReference("msg-3", 3, "user", "d" * 64),), "repository"),
            MemoryCandidate("repo-2", "no source", True, "m4", "project_fact", 1, (), "repository"),
            valid,
        ]
        repository.approved_by_id.return_value = {"id": "m1", "approval_status": "approved",
            "scope": "repository", "version": 1, "memory_type": "project_fact", "content": "valid"}
        repository.source_records.return_value = [{"message_id": "msg-1", "source_sequence": 1,
            "source_role": "user", "source_hash": "b" * 64}]
        result = MemoryService(repository).retrieve_approved(
            retriever, "question", "repo-2", limit=2)
        self.assertEqual((valid,), result)
        retriever.search.assert_called_once_with("question", repository_id="repo-2", limit=2)

    def test_retrieval_limit_is_bounded_before_calling_retriever(self):
        retriever = Mock()
        for limit in (0, 9, True):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                MemoryService(Mock()).retrieve_approved(retriever, "question", "repo-1", limit=limit)
        retriever.search.assert_not_called()

    def test_missing_retrieval_port_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "retrieval port"):
            MemoryService(Mock()).retrieve_approved(object(), "question", "repo-1")

    def test_retrieval_validates_query_scope_and_authority_before_search(self):
        retriever = Mock()
        service = MemoryService(Mock())
        for query, repository_id in ((" ", "repo-1"), ("question", " ")):
            with self.subTest(query=query, repository_id=repository_id):
                with self.assertRaises(ValueError):
                    service.retrieve_approved(retriever, query, repository_id)
        retriever.search.assert_not_called()

        repository = Mock()
        repository.approved_by_id = None
        repository.source_records = None
        with self.assertRaisesRegex(ValueError, "authority verification"):
            MemoryService(repository).retrieve_approved(retriever, "question", "repo-1")
        retriever.search.assert_not_called()

    def test_stale_authoritative_memory_version_is_never_returned(self):
        candidate = MemoryCandidate("repo-2", "old approved content", True, "memory-1",
            "project_fact", 1, (MemorySourceReference("message-1", 1, "user", "a" * 64),),
            "repository")
        retriever = Mock()
        retriever.search.return_value = [candidate]
        repository = Mock()
        repository.approved_by_id.return_value = {
            "id": "memory-1", "approval_status": "approved", "scope": "repository",
            "version": 2, "memory_type": "project_fact", "content": "new content",
        }

        result = MemoryService(repository).retrieve_approved(retriever, "question", "repo-2")

        self.assertEqual((), result)
        repository.approved_by_id.assert_called_once_with("memory-1", "repo-2")
        repository.source_records.assert_not_called()


if __name__ == "__main__":
    unittest.main()
