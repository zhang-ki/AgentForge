"""针对 AF-012 MVP 宿主验收 Runner 合同的离线测试。"""
import json
import hashlib
import io
import os
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

import AF012_MVP_HOST_ACCEPTANCE as host_runner
from AF012_MVP_HOST_ACCEPTANCE import (
    EXPECTED_POSTGRES_IDENTITY,
    ROOT,
    _chat_counts,
    _answer_contract_status,
    _health_ready,
    _postgres_identity_diagnostic,
    _isolated_test_scope,
    _create_isolated_test_engine,
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
        self.assertIn("This is an isolated child runner", script.read_text(encoding="utf-8-sig"))

    def test_isolation_preflight_rejects_missing_or_formal_database_scope(self):
        with self.assertRaisesRegex(AcceptanceFailure, "isolated_test_database_required"):
            _isolated_test_scope({})
        with self.assertRaisesRegex(AcceptanceFailure, "isolated_test_database_required"):
            _create_isolated_test_engine("synthetic-test-password", "agentforge")

    def test_runner_refuses_missing_isolation_before_health_or_credentials(self):
        test_temp_root = ROOT / "tests" / ".tmp"
        test_temp_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="af012-host-runner-gate-", dir=test_temp_root,
        ) as temporary:
            env = {key: value for key, value in os.environ.items() if key not in {
                "CODEX_SANDBOX_NETWORK_DISABLED", "AGENTFORGE_DB_PASSWORD",
                "AGENTFORGE_LLAMA_API_KEY", "AGENTFORGE_EMBEDDING_BASE_URL",
                "AGENTFORGE_EMBEDDING_MODEL_ID", "AGENTFORGE_EMBEDDING_API_MODEL",
                "AF012_TEST_DATABASE", "AF012_TEST_CHROMA_PATH",
            }}
            env["CODEX_SANDBOX_NETWORK_DISABLED"] = "0"
            output = io.StringIO()
            with (patch("AF012_MVP_HOST_ACCEPTANCE.ROOT", Path(temporary)),
                  patch.dict(os.environ, env, clear=True),
                  patch("AF012_MVP_HOST_ACCEPTANCE._health_ready",
                        side_effect=AssertionError("health must not run without isolated scope")),
                  redirect_stdout(output)):
                exit_code = host_runner._main()
            payload = json.loads(output.getvalue())
            self.assertEqual(2, exit_code)
            self.assertEqual("isolated_test_database_required", payload["failure_code"])
            self.assertEqual(0, payload["chat_requests"]["attempted"])
            self.assertEqual(0, payload["embedding_requests"]["attempts"])

    def test_isolated_engine_uses_only_the_coordinator_database(self):
        with patch("sqlalchemy.create_engine", side_effect=lambda url, **_kwargs: Mock(url=url)) as factory:
            engine = _create_isolated_test_engine(
                "synthetic-test-password", "af012_accept_0123456789abcdefabcd",
            )
        self.assertEqual("agentforge_app", engine.url.username)
        self.assertEqual("af012_accept_0123456789abcdefabcd", engine.url.database)
        self.assertEqual("127.0.0.1", engine.url.host)
        self.assertEqual(5432, engine.url.port)
        kwargs = factory.call_args.kwargs
        self.assertTrue(kwargs["hide_parameters"])
        self.assertEqual(2, kwargs["pool_size"])

    def test_isolated_chroma_scope_must_be_under_uuid_temp_root(self):
        test_temp_root = ROOT / "tests" / ".tmp"
        test_temp_root.mkdir(exist_ok=True)
        owned_root = Path(tempfile.mkdtemp(
            prefix="af012-dual-host-" + "a" * 32 + "-", dir=test_temp_root,
        ))
        chroma_path = owned_root / "chroma"
        chroma_path.mkdir()
        try:
            database, resolved = _isolated_test_scope({
                "AF012_TEST_DATABASE": "af012_accept_0123456789abcdefabcd",
                "AF012_TEST_CHROMA_PATH": str(chroma_path),
            })
            self.assertEqual("af012_accept_0123456789abcdefabcd", database)
            self.assertEqual(chroma_path.resolve(), resolved)
            with self.assertRaisesRegex(AcceptanceFailure, "isolated_test_chroma_path_required"):
                _isolated_test_scope({
                    "AF012_TEST_DATABASE": "af012_accept_0123456789abcdefabcd",
                    "AF012_TEST_CHROMA_PATH": str(ROOT / ".agentforge" / "data" / "chroma"),
                })
        finally:
            shutil.rmtree(owned_root)

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
