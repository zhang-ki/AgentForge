import unittest

from agentforge.index_lifecycle import IndexLifecycleState, derive_index_lifecycle


class IndexLifecycleTests(unittest.TestCase):
    def test_repository_and_index_initial_states_are_distinct(self):
        unregistered = derive_index_lifecycle(repository_id=None)
        self.assertEqual(IndexLifecycleState.UNREGISTERED, unregistered.state)
        self.assertEqual("no_repository", unregistered.reason_code)

        pending = derive_index_lifecycle(repository_id="repo-1", indexed=False)
        self.assertEqual(IndexLifecycleState.NOT_INITIALIZED, pending.state)
        self.assertEqual("not_initialized", pending.sqlite_status)

    def test_ready_degraded_and_recovered_states_follow_observed_backend_health(self):
        ready = derive_index_lifecycle(
            repository_id="repo-1", indexed=True, vector_requested=True,
            vector_available=True, vector_complete=True,
        )
        degraded = derive_index_lifecycle(
            repository_id="repo-1", indexed=True, vector_requested=True,
            vector_available=False, reason_code="embedding_service_unavailable",
        )
        recovered = derive_index_lifecycle(
            repository_id="repo-1", indexed=True, vector_requested=True,
            vector_available=True, vector_complete=True,
        )

        self.assertEqual(IndexLifecycleState.READY, ready.state)
        self.assertEqual(IndexLifecycleState.DEGRADED_LEXICAL_ONLY, degraded.state)
        self.assertEqual("embedding_service_unavailable", degraded.reason_code)
        self.assertEqual(IndexLifecycleState.READY, recovered.state)

    def test_sqlite_failure_and_incomplete_vector_index_are_not_reported_ready(self):
        sqlite_failure = derive_index_lifecycle(
            repository_id="repo-1", sqlite_available=False,
        )
        incomplete = derive_index_lifecycle(
            repository_id="repo-1", indexed=True, vector_requested=True,
            vector_available=True, vector_complete=False,
            expected_vectors=8, stored_vectors=7, invalid_vectors=0,
        )

        self.assertEqual(IndexLifecycleState.FAILED, sqlite_failure.state)
        self.assertEqual(IndexLifecycleState.STALE, incomplete.state)
        self.assertEqual((8, 7, 0), (incomplete.expected_vectors,
                                     incomplete.stored_vectors,
                                     incomplete.invalid_vectors))
        self.assertNotEqual(IndexLifecycleState.READY, incomplete.state)


if __name__ == "__main__":
    unittest.main()
