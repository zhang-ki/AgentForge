import io
import json
import subprocess
import unittest
from urllib.error import HTTPError
from unittest.mock import Mock

import AF009GR3_HOST_RUNNER as runner


class FakeClock:
    def __init__(self):
        self.value = 0.0

    def monotonic(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


class FakeProcess:
    def __init__(self, *, exit_code=None, terminate_code=1):
        self.returncode = exit_code
        self.terminate_code = terminate_code
        self.terminate_calls = 0
        self.kill_calls = 0
        self.wait_calls = []

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminate_calls += 1
        self.returncode = self.terminate_code

    def kill(self):
        self.kill_calls += 1
        self.returncode = -9

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
        return self.returncode


class FakeResponse:
    def __init__(self, status, body, content_type="application/json"):
        self.status = status
        self.body = body
        self.headers = {"Content-Type": content_type}
        self.closed = False

    def read(self, limit):
        return self.body[:limit]

    def close(self):
        self.closed = True


class FakeOpener:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.open_calls = []

    def open(self, request, timeout=None):
        self.open_calls.append((request, timeout))
        if self.error is not None:
            raise self.error
        return self.response


class ModelReadinessTests(unittest.TestCase):
    def test_loading_503_then_ok_200_is_required_before_evaluation(self):
        clock = FakeClock()
        observations = iter((
            {"http_status": 503, "status": "Loading model", "semantic_status": "loading"},
            {"http_status": 200, "status": "ok", "semantic_status": "ready"},
        ))
        result = runner._wait_for_model_ready(
            FakeProcess(), "127.0.0.1", 12345,
            total_timeout=10, request_timeout=2, poll_interval=1,
            monotonic=clock.monotonic, sleep=clock.sleep,
            probe=lambda *_args, **_kwargs: next(observations),
        )
        self.assertEqual("passed", result["status"])
        self.assertEqual([503, 200], [row["http_status"] for row in result["observations"]])
        evaluator = Mock(return_value="evaluated")
        self.assertEqual("evaluated", runner._evaluate_after_model_ready(result, evaluator))
        evaluator.assert_called_once_with()

    def test_continuous_loading_503_times_out_and_evaluation_is_blocked(self):
        clock = FakeClock()
        with self.assertRaises(runner.ModelReadinessError) as raised:
            runner._wait_for_model_ready(
                FakeProcess(), "127.0.0.1", 12345,
                total_timeout=3, request_timeout=2, poll_interval=1,
                monotonic=clock.monotonic, sleep=clock.sleep,
                probe=lambda *_args, **_kwargs: {
                    "http_status": 503, "status": "Loading model", "semantic_status": "loading"
                },
            )
        self.assertEqual("MODEL_READINESS_TIMEOUT", raised.exception.code)
        self.assertEqual(3, len(raised.exception.observations))
        evaluator = Mock()
        with self.assertRaisesRegex(runner.ModelReadinessError, "prohibited"):
            runner._evaluate_after_model_ready({"status": "failed"}, evaluator)
        evaluator.assert_not_called()

    def test_process_exit_is_distinct_and_immediate(self):
        probe = Mock()
        with self.assertRaises(runner.ModelReadinessError) as raised:
            runner._wait_for_model_ready(
                FakeProcess(exit_code=23), "127.0.0.1", 12345,
                probe=probe,
            )
        self.assertEqual("MODEL_PROCESS_EXITED", raised.exception.code)
        self.assertEqual(23, raised.exception.exit_code)
        probe.assert_not_called()

    def test_unexpected_http_status_is_not_retried(self):
        probe = Mock(return_value={
            "http_status": 500, "status": "error", "semantic_status": "unknown"
        })
        with self.assertRaises(runner.ModelReadinessError) as raised:
            runner._wait_for_model_ready(FakeProcess(), "127.0.0.1", 12345, probe=probe)
        self.assertEqual("MODEL_HEALTH_UNEXPECTED_STATUS", raised.exception.code)
        probe.assert_called_once()

    def test_invalid_health_json_and_shape_are_explicit(self):
        response = FakeResponse(200, b"not-json")
        opener = FakeOpener(response=response)
        with self.assertRaises(runner.ModelReadinessError) as raised:
            runner._health_probe("127.0.0.1", 12345, opener=opener)
        self.assertEqual("MODEL_HEALTH_INVALID_JSON", raised.exception.code)
        self.assertEqual(1, len(opener.open_calls))
        request, timeout = opener.open_calls[0]
        self.assertEqual("GET", request.get_method())
        self.assertEqual("http://127.0.0.1:12345/health", request.full_url)
        self.assertEqual(2.0, timeout)
        self.assertTrue(response.closed)

        response = FakeResponse(200, json.dumps({"state": "ok"}).encode())
        probe = lambda *_args, **_kwargs: runner._health_probe(
            "127.0.0.1", 12345, opener=FakeOpener(response=response)
        )
        with self.assertRaises(runner.ModelReadinessError) as raised:
            runner._wait_for_model_ready(FakeProcess(), "127.0.0.1", 12345, probe=probe)
        self.assertEqual("MODEL_HEALTH_UNEXPECTED_STATUS", raised.exception.code)
        self.assertEqual(["state"], raised.exception.observations[0]["json_keys"])
        self.assertTrue(response.closed)

    def test_explicit_nested_loading_error_is_a_supported_503_shape(self):
        body = json.dumps({
            "error": {"code": 503, "message": "Loading model", "type": "unavailable_error"}
        }).encode()
        response = FakeResponse(503, body)
        result = runner._health_probe(
            "127.0.0.1", 12345,
            opener=FakeOpener(response=response),
        )
        self.assertEqual(503, result["http_status"])
        self.assertEqual("Loading model", result["status"])
        self.assertEqual("loading", result["semantic_status"])
        self.assertEqual("llama_cpp_loading_error", result["response_shape"])
        self.assertEqual(["code", "message", "type"], result["error_keys"])
        self.assertEqual("application/json", result["content_type"])
        self.assertFalse(result["via_http_error"])
        self.assertTrue(response.closed)

    def test_urllib_http_error_preserves_503_status_headers_and_body(self):
        body = json.dumps({
            "error": {"code": 503, "message": "Loading model", "type": "unavailable_error"}
        }).encode()
        error = HTTPError(
            "http://127.0.0.1:12345/health", 503, "Service Unavailable",
            {"Content-Type": "application/json; charset=utf-8"}, io.BytesIO(body),
        )
        result = runner._health_probe(
            "127.0.0.1", 12345,
            opener=FakeOpener(error=error),
        )
        self.assertEqual(503, result["http_status"])
        self.assertTrue(result["via_http_error"])
        self.assertEqual("application/json; charset=utf-8", result["content_type"])
        self.assertEqual("loading", result["semantic_status"])
        self.assertIn('"message":"Loading model"', result["response_body"])

    def test_unknown_503_structure_is_recorded_and_rejected_once(self):
        body = json.dumps({"error": {"message": "warming"}}).encode()
        response = FakeResponse(503, body)
        probe = lambda *_args, **_kwargs: runner._health_probe(
            "127.0.0.1", 12345, opener=FakeOpener(response=response)
        )
        with self.assertRaises(runner.ModelReadinessError) as raised:
            runner._wait_for_model_ready(FakeProcess(), "127.0.0.1", 12345, probe=probe)
        self.assertEqual("MODEL_HEALTH_UNEXPECTED_STATUS", raised.exception.code)
        self.assertEqual(503, raised.exception.observations[0]["http_status"])
        self.assertEqual("unknown", raised.exception.observations[0]["semantic_status"])

    def test_health_and_log_evidence_redacts_sensitive_values(self):
        body = json.dumps({"status": "ok", "token": "do-not-record"}).encode()
        result = runner._health_probe(
            "127.0.0.1", 12345,
            opener=FakeOpener(response=FakeResponse(200, body)),
        )
        self.assertEqual("ready", result["semantic_status"])
        self.assertIn('"token":"<redacted>"', result["response_body"])
        self.assertNotIn("do-not-record", result["response_body"])
        summary = runner._safe_log_summary(b"error Authorization: bearer-secret\n")
        self.assertEqual(["error Authorization: <redacted>"], summary)

    def test_output_drain_and_owned_process_exit_code_are_recoverable(self):
        target = bytearray()
        counters = {}
        runner._drain(io.BytesIO(b"known stdout\n"), target, counters, "stdout_bytes")
        self.assertEqual(b"known stdout\n", bytes(target))
        self.assertEqual(len(target), counters["stdout_bytes"])

        running = FakeProcess()
        cleanup = runner._stop_owned_process(running)
        self.assertEqual("terminated", cleanup["termination"])
        self.assertEqual(1, cleanup["exit_code"])
        self.assertTrue(cleanup["owned_process_stopped"])
        self.assertEqual(1, running.terminate_calls)

        exited = FakeProcess(exit_code=17)
        cleanup = runner._stop_owned_process(exited)
        self.assertEqual("exited_without_cleanup_signal", cleanup["termination"])
        self.assertEqual(17, cleanup["exit_code"])
        self.assertEqual(0, exited.terminate_calls)

    def test_terminate_timeout_kills_only_supplied_child(self):
        class SlowProcess(FakeProcess):
            def wait(self, timeout=None):
                self.wait_calls.append(timeout)
                if self.kill_calls == 0:
                    raise subprocess.TimeoutExpired("llama-server", timeout)
                return self.returncode

            def terminate(self):
                self.terminate_calls += 1

        process = SlowProcess()
        cleanup = runner._stop_owned_process(process, terminate_timeout=0.1)
        self.assertEqual("killed_after_terminate_timeout", cleanup["termination"])
        self.assertEqual(1, process.terminate_calls)
        self.assertEqual(1, process.kill_calls)
        self.assertTrue(cleanup["owned_process_stopped"])


if __name__ == "__main__":
    unittest.main()
