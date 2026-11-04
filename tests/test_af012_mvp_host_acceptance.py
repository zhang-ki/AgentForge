"""针对 AF-012 MVP 宿主验收 Runner 合同的离线测试。"""
import json
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from AF012_MVP_HOST_ACCEPTANCE import (
    EXPECTED_POSTGRES_IDENTITY,
    ROOT,
    _chat_counts,
    _answer_contract_status,
    _health_ready,
    _postgres_identity_diagnostic,
    _sha256,
    AcceptanceFailure,
)


class AF012MvpHostRunnerTests(unittest.TestCase):
    def test_memory_answer_contract_status_reads_nested_contract(self):
        self.assertEqual("limited", _answer_contract_status({
            "answer_contract": {"status": "limited", "claims": [], "evidence": []},
        }))
        self.assertIsNone(_answer_contract_status({"answer": "no contract"}))

    def test_windows_host_script_has_utf8_bom_for_windows_powershell(self):
        script = ROOT / "AF012_MVP_HOST_ACCEPTANCE.ps1"
        self.assertTrue(script.read_bytes().startswith(b"\xef\xbb\xbf"))

    def _connection(self, status, body):
        response = Mock()
        response.status = status
        response.read.return_value = json.dumps(body).encode("utf-8")
        connection = Mock()
        connection.getresponse.return_value = response
        return connection

    def test_health_wait_accepts_only_loading_then_ready_contract(self):
        loading = self._connection(503, {"error": {"message": "Loading model"}})
        ready = self._connection(200, {"status": "ok"})
        with (patch("AF012_MVP_HOST_ACCEPTANCE.http.client.HTTPConnection",
                    side_effect=[loading, ready]),
              patch("AF012_MVP_HOST_ACCEPTANCE.time.sleep")):
            elapsed, observed = _health_ready(56572, timeout_seconds=2)
        self.assertGreaterEqual(elapsed, 0)
        self.assertEqual({"loading_503": 1, "ready_200": 1}, observed)
        self.assertEqual([unittest.mock.call("GET", "/health", headers={"Accept": "application/json"})],
                         loading.request.call_args_list)
        self.assertEqual([unittest.mock.call("GET", "/health", headers={"Accept": "application/json"})],
                         ready.request.call_args_list)
        loading.close.assert_called_once()
        ready.close.assert_called_once()

    def test_unknown_health_response_fails_closed(self):
        unknown = self._connection(503, {"error": {"message": "busy"}})
        with patch("AF012_MVP_HOST_ACCEPTANCE.http.client.HTTPConnection", return_value=unknown):
            with self.assertRaisesRegex(AcceptanceFailure, "health_contract_rejected"):
                _health_ready(58067, timeout_seconds=2)

    def test_chat_counts_sum_events_without_retries(self):
        self.assertEqual({"attempted": 3, "sent": 2, "succeeded": 1, "failed": 1, "retries": 0},
            _chat_counts([{"attempted": 1, "sent": 1, "succeeded": 1, "failed": 0, "retries": 0},
                          {"attempted": 2, "sent": 1, "succeeded": 0, "failed": 1, "retries": 0}]))

    def test_postgres_identity_diagnostic_identifies_scope_mismatch_without_secrets(self):
        observed = dict(EXPECTED_POSTGRES_IDENTITY)
        observed["current_user"] = "postgres"
        diagnostic = _postgres_identity_diagnostic(observed)
        self.assertEqual(["current_user"], diagnostic["mismatches"])
        self.assertEqual("agentforge_app", diagnostic["expected"]["current_user"])
        self.assertEqual("postgres", diagnostic["observed"]["current_user"])
        self.assertEqual(
            {"current_user", "current_database", "server_address", "server_port"},
            set(diagnostic["observed"]),
        )
        self.assertNotIn("password", json.dumps(diagnostic).casefold())

    def test_postgres_identity_diagnostic_accepts_exact_local_scope(self):
        diagnostic = _postgres_identity_diagnostic(dict(EXPECTED_POSTGRES_IDENTITY))
        self.assertEqual([], diagnostic["mismatches"])

    def test_postgres_identity_accepts_host_cidr_rendering(self):
        observed = dict(EXPECTED_POSTGRES_IDENTITY)
        observed["server_address"] = "127.0.0.1/32"
        diagnostic = _postgres_identity_diagnostic(observed)
        self.assertEqual([], diagnostic["mismatches"])
        self.assertEqual("127.0.0.1", diagnostic["normalized_server_address"])

    def test_postgres_identity_rejects_non_loopback_and_non_host_prefix(self):
        for address in ("0.0.0.0", "127.0.0.2", "127.0.0.1/24", "not-an-address"):
            with self.subTest(address=address):
                observed = dict(EXPECTED_POSTGRES_IDENTITY)
                observed["server_address"] = address
                diagnostic = _postgres_identity_diagnostic(observed)
                self.assertEqual(["server_address"], diagnostic["mismatches"])
                self.assertIsNone(diagnostic["normalized_server_address"])

    def test_sha256_reads_exact_file_bytes(self):
        test_temp_root = ROOT / "tests" / ".tmp"
        test_temp_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="af012-mvp-hash-", dir=test_temp_root) as directory:
            file = Path(directory) / "fixture.bin"
            file.write_bytes(b"agentforge-mvp")
            self.assertEqual(hashlib.sha256(b"agentforge-mvp").hexdigest().upper(), _sha256(file))


if __name__ == "__main__":
    unittest.main()
