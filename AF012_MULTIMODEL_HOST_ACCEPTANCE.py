"""Bounded real-host smoke test for explicitly selected DeepSeek models."""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sys
import uuid

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
MAX_MODELS = 2
MAX_OUTPUT_TOKENS = 48
MAX_THINKING_OUTPUT_TOKENS = 512


def _case_diagnostics(client, attempted_before):
    stats = client.request_stats()
    if stats.get("attempted", 0) == attempted_before:
        return {"status": "not_sent", "usage": {}, "finish_reason": None,
                "application_error_code": None}
    return {"usage": dict(stats.get("last_usage") or {}),
            "finish_reason": stats.get("last_finish_reason"),
            "application_error_code": stats.get("last_application_error_code")}


def _safe_error(exc: Exception, secret: str | None) -> str:
    message = str(exc)
    if secret:
        message = message.replace(secret, "<redacted>")
    message = re.sub(r"(?i)Bearer\s+[^\s,;]+", "Bearer <redacted>", message)
    message = re.sub(r"(?i)(api[_-]?key|authorization|password|secret|token)(\s*[:=]\s*)[^\s,;]+",
                     r"\1\2<redacted>", message)
    return message[:400]


def _base_result() -> dict:
    return {
        "schema_version": 1, "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "BLOCKED", "state": "PREFLIGHT", "failure_stage": None, "failure_code": None,
        "checks": {
            "network_policy": "not_measured", "deepseek_credential": "not_measured",
            "deepseek_model_discovery": "not_measured", "deepseek_synthetic_chat": "not_measured",
            "qwen_host_acceptance": "not_measured", "postgresql_host_acceptance": "not_measured",
            "embedding_and_chroma_host_acceptance": "not_measured",
            "session_binding_restart": "not_measured", "eleven_case_rag": "not_measured",
            "thirty_turn_long_context": "not_measured",
        },
        "deepseek": {"discovery_requests": 0, "models_discovered": 0, "models_smoke_tested": [],
                     "chat_requests": {"attempted": 0, "sent": 0, "succeeded": 0,
                                        "failed": 0, "retries": 0}},
        "usage": [], "synthetic_only": True, "formal_data_modified": False,
        "services_started_or_stopped": False, "retries": 0,
    }


def _finish(result: dict, exit_code: int) -> int:
    path = ROOT / f"AF012_MULTIMODEL_HOST_RESULT_{uuid.uuid4().hex}.json"
    result["result_file"] = str(path)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return exit_code


def run() -> int:
    result = _base_result()
    secret = None
    client = None
    try:
        if os.environ.get("CODEX_SANDBOX_NETWORK_DISABLED") == "1":
            result.update(state="BLOCKED", failure_stage="environment",
                          failure_code="network_disabled_execution_environment")
            result["checks"]["network_policy"] = "blocked_by_environment"
            result["message"] = "Run from ordinary Windows PowerShell; no network probe was made."
            return _finish(result, 2)
        result["checks"]["network_policy"] = "host_execution_allowed"

        from agentforge.credential_store import CredentialStore
        key, source = CredentialStore().resolve()
        if not key:
            result.update(state="BLOCKED", failure_stage="credential_preflight",
                          failure_code="new_deepseek_credential_not_configured")
            result["checks"]["deepseek_credential"] = "not_configured"
            result["credential_source"] = "none"
            result["message"] = "Configure a new key locally with /model auth set deepseek; never paste it into chat."
            return _finish(result, 2)
        secret = key
        result["checks"]["deepseek_credential"] = "configured"
        result["credential_source"] = source

        print("Cloud notice: only synthetic source text and synthetic questions will be sent to DeepSeek.")
        print("Limit: at most 2 discovered models, 6 chat requests total; "
              "48 output tokens for normal/streaming and 512 for thinking, subject to model limits.")
        print("No repository, conversation, memory, API key or local database content is included.")
        if input("Type SEND SYNTHETIC TESTS to allow discovery and bounded chat smoke: ").strip() != "SEND SYNTHETIC TESTS":
            result.update(state="BLOCKED", failure_stage="cloud_consent",
                          failure_code="cloud_smoke_not_confirmed")
            result["checks"]["deepseek_credential"] = "configured_but_not_used"
            return _finish(result, 2)

        from agentforge.errors import AgentForgeError
        from agentforge.model_registry import parse_deepseek_models
        from agentforge.models import BuiltContext, Evidence, SearchResult
        from agentforge.providers.deepseek import DeepSeekClient, DeepSeekProvider

        client = DeepSeekClient(key, connect_timeout=5, read_timeout=45, total_timeout=90)
        client.set_credential_source(source)
        result["state"] = "deepseek_discovery"
        descriptors = parse_deepseek_models(client.list_models())
        result["deepseek"]["discovery_requests"] = 1
        result["deepseek"]["models_discovered"] = len(descriptors)
        result["checks"]["deepseek_model_discovery"] = "passed" if descriptors else "failed_empty_list"
        if not descriptors:
            result.update(status="FAIL", state="FAILED", failure_stage="deepseek_discovery",
                          failure_code="empty_model_list")
            return _finish(result, 1)

        excerpt = "def example_value():\n    return 7\n"
        search_result = SearchResult("synthetic://af012-multimodel", "src/example.py", 1, 2,
                                     excerpt, sha256(excerpt.encode("utf-8")).hexdigest(), 1.0)
        context = BuiltContext("What integer does example_value return? Reply with the integer and cite [E1].",
                               (Evidence("E1", search_result),), "[E1] src/example.py:1-2\n" + excerpt,
                               len(excerpt), 2048)
        result["model_limit_reached"] = len(descriptors) > MAX_MODELS
        failed = False
        for descriptor in descriptors[:MAX_MODELS]:
            item = {"model_id": descriptor.model_id, "normal_answer": "not_measured",
                    "thinking": "not_measured", "streaming": "not_measured"}
            if not descriptor.context_window or not descriptor.max_output_tokens:
                item["reason"] = "verified context/output limits are absent"
                result["deepseek"]["models_smoke_tested"].append(item)
                continue
            output_limit = min(MAX_OUTPUT_TOKENS, descriptor.max_output_tokens)
            thinking_limit = min(MAX_THINKING_OUTPUT_TOKENS, descriptor.max_output_tokens)
            item["normal_max_output_tokens"] = output_limit
            result["state"] = "deepseek_chat"
            attempted_before = client.request_stats().get("attempted", 0)
            answer = DeepSeekProvider(client, descriptor.model_id, max_output_tokens=output_limit,
                                      descriptor=descriptor).answer(context)
            valid = "7" in answer.text and answer.citations == ("src/example.py:1-2",)
            item["normal_answer"] = "passed" if valid else "failed_fact_or_citation_assertion"
            item["answer_chars"] = len(answer.text)
            item["citation_count"] = len(answer.citations)
            item["usage"] = dict(client.request_stats().get("last_usage") or {})
            item["normal_diagnostics"] = _case_diagnostics(client, attempted_before)
            failed |= not valid

            if descriptor.supports_thinking is True:
                item["thinking_max_output_tokens"] = thinking_limit
                effort = descriptor.default_effort or (descriptor.supported_efforts[0]
                                                         if descriptor.supported_efforts else None)
                attempted_before = client.request_stats().get("attempted", 0)
                try:
                    thinking_answer = DeepSeekProvider(client, descriptor.model_id,
                        max_output_tokens=thinking_limit, thinking=True, effort=effort,
                        descriptor=descriptor).answer(context)
                    valid = "7" in thinking_answer.text and thinking_answer.citations == ("src/example.py:1-2",)
                    item["thinking"] = "passed" if valid else "failed_fact_or_citation_assertion"
                    failed |= not valid
                except (ValueError, AgentForgeError) as exc:
                    item["thinking"] = "failed"
                    item["thinking_error"] = _safe_error(exc, secret)
                    failed = True
                finally:
                    item["thinking_diagnostics"] = _case_diagnostics(client, attempted_before)

            if descriptor.supports_streaming is True:
                pieces = []
                attempted_before = client.request_stats().get("attempted", 0)
                try:
                    streamed_answer = DeepSeekProvider(client, descriptor.model_id,
                        max_output_tokens=output_limit, stream=True, descriptor=descriptor).answer(
                            context, on_content=pieces.append)
                    valid = ("7" in streamed_answer.text and
                             streamed_answer.citations == ("src/example.py:1-2",) and bool(pieces))
                    item["streaming"] = "passed" if valid else "failed_fact_or_citation_assertion"
                    failed |= not valid
                except (ValueError, AgentForgeError) as exc:
                    item["streaming"] = "failed"
                    item["stream_error"] = _safe_error(exc, secret)
                    failed = True
                finally:
                    item["streaming_diagnostics"] = _case_diagnostics(client, attempted_before)
            result["deepseek"]["models_smoke_tested"].append(item)

        stats = client.request_stats()
        result["deepseek"]["chat_requests"] = {
            name: int(stats.get(name, 0))
            for name in ("attempted", "sent", "succeeded", "failed", "retries")
        }
        result["deepseek"]["http_status"] = stats.get("last_http_status")
        result["deepseek"]["last_error_code"] = stats.get("last_error_code")
        result["usage"] = [item.get("usage", {}) for item in result["deepseek"]["models_smoke_tested"]
                            if isinstance(item.get("usage"), dict)]
        result["usage_by_case"] = [
            {"model_id": item["model_id"], "case": case,
             **item[case + "_diagnostics"]}
            for item in result["deepseek"]["models_smoke_tested"]
            for case in ("normal", "thinking", "streaming")
            if case + "_diagnostics" in item
        ]
        result["checks"]["deepseek_synthetic_chat"] = "failed" if failed else "passed"
        if failed:
            result.update(status="FAIL", state="FAILED", failure_stage="deepseek_chat",
                          failure_code="bounded_smoke_assertion_failed")
            return _finish(result, 1)

        result.update(status="PARTIAL", state="COMPLETED_SMOKE_ONLY")
        result["message"] = (
            "DeepSeek discovery and bounded synthetic smoke completed. This is not full 1.2 acceptance: "
            "Qwen, PostgreSQL, Chroma persistence, session binding/restart, all fixed RAG cases, and "
            "long-context acceptance remain unmeasured by this runner."
        )
        if result["model_limit_reached"]:
            result["message"] += " More models were discovered than the two-model request cap; remaining models were not tested."
        return _finish(result, 0)
    except Exception as exc:
        result.update(status="FAIL", state="FAILED", failure_stage=result.get("state", "preflight"),
                      failure_code=type(exc).__name__, error=_safe_error(exc, secret))
        if client is not None:
            stats = client.request_stats()
            result["deepseek"]["discovery_requests"] = stats.get("discovery_attempted", 0)
            result["deepseek"]["chat_requests"] = {
                name: int(stats.get(name, 0))
                for name in ("attempted", "sent", "succeeded", "failed", "retries")
            }
            result["deepseek"]["last_error_code"] = stats.get("last_error_code")
        return _finish(result, 1)


if __name__ == "__main__":
    raise SystemExit(run())
