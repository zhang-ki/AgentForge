"""Explicit DeepSeek HTTPS Chat Completions provider; never used as a fallback."""
from __future__ import annotations

from dataclasses import dataclass
import http.client
import json
import math
import socket
import ssl
import time
from typing import Callable

from ..context import validate_model_citations
from ..errors import CitationValidationError, ProviderError
from ..models import Answer, BuiltContext
from .llama_server import (_ANSWER_SYSTEM_PROMPT, _model_descriptor, _model_evidence_text,
                           _model_messages, validate_output_tokens, validate_timeout)


_API_HOST = "api.deepseek.com"
_CHAT_PATH = "/chat/completions"
_MODELS_PATH = "/models"
_MAX_BODY = 2_000_000
_SAFE_ERRORS = {
    400: ("bad_request", "DeepSeek rejected the request parameters or context size (HTTP 400)."),
    401: ("authentication", "DeepSeek rejected the configured API key (HTTP 401)."),
    402: ("billing", "DeepSeek reports an account balance or payment issue (HTTP 402)."),
    429: ("rate_limited", "DeepSeek rate limit reached (HTTP 429); no retry was attempted."),
}


@dataclass(frozen=True)
class DeepSeekCompletion:
    content: str
    reasoning_content: str | None
    finish_reason: str | None
    tool_calls: tuple[dict, ...]
    usage: dict
    request_id: str | None


class DeepSeekClient:
    def __init__(self, api_key: str, *, connect_timeout: float = 5.0,
                 read_timeout: float = 90.0, total_timeout: float = 180.0,
                 connection_factory=None, ssl_context=None):
        if (not isinstance(api_key, str) or not api_key or api_key != api_key.strip()
                or len(api_key) > 4096 or any(ord(ch) < 33 or ord(ch) > 126 for ch in api_key)):
            raise ValueError("A configured DeepSeek API key is required.")
        self.api_key = api_key
        self.connect_timeout = validate_timeout(connect_timeout, "connect_timeout", 0, 120, inclusive_minimum=False)
        self.read_timeout = validate_timeout(read_timeout, "read_timeout", 5, 120, inclusive_minimum=True)
        self.total_timeout = validate_timeout(total_timeout, "total_timeout", 10, 300, inclusive_minimum=True)
        self.connection_factory = connection_factory or http.client.HTTPSConnection
        self.ssl_context = ssl_context or ssl.create_default_context()
        self._stats = {"attempted": 0, "sent": 0, "succeeded": 0, "failed": 0, "retries": 0,
            "last_http_status": None, "last_failure_phase": None, "last_elapsed_seconds": None,
            "last_error_code": None, "last_usage": None, "last_request_id": None,
            "last_reasoning_chars": None, "last_finish_reason": None,
            "last_response_diagnostic": None, "discovery_attempted": 0,
            "discovery_succeeded": 0, "discovery_failed": 0, "credential_source": None,
            "last_application_failure_phase": None, "last_application_error_code": None}

    def request_stats(self) -> dict:
        return dict(self._stats)

    def record_application_failure(self, phase: str, code: str, diagnostic=None):
        self._stats["last_application_failure_phase"] = phase
        self._stats["last_application_error_code"] = code

    def set_credential_source(self, source: str) -> None:
        if source not in {"environment", "dpapi"}:
            raise ValueError("Invalid credential source.")
        self._stats["credential_source"] = source

    def list_models(self):
        self._stats["discovery_attempted"] += 1
        payload = self._request("GET", _MODELS_PATH)
        self._stats["discovery_succeeded"] += 1
        return payload

    def chat(self, messages: list[dict], *, model: str, max_output_tokens: int,
             thinking: bool | None = None, effort: str | None = None, stream: bool = False,
             on_content: Callable[[str], None] | None = None, tools: list[dict] | None = None) -> DeepSeekCompletion:
        if not isinstance(model, str) or not model.strip() or len(model) > 256:
            raise ValueError("A discovered DeepSeek model ID is required.")
        output_tokens = validate_output_tokens(max_output_tokens)
        if thinking is not None and not isinstance(thinking, bool):
            raise ValueError("thinking must be a boolean or omitted when unsupported.")
        if effort is not None and effort not in {"low", "high", "max"}:
            raise ValueError("reasoning effort must be low, high or max.")
        if effort is not None and thinking is not True:
            raise ValueError("Reasoning effort requires thinking mode.")
        if on_content is not None and not callable(on_content):
            raise ValueError("on_content must be callable.")
        body = {"model": model.strip(), "messages": _validate_messages(messages),
                "max_tokens": output_tokens, "stream": bool(stream)}
        if thinking is not None:
            body["thinking"] = {"type": "enabled" if thinking else "disabled"}
        if effort is not None:
            body["reasoning_effort"] = effort
        if tools is not None:
            body["tools"] = _validate_tool_schemas(tools)
        if stream:
            body["stream_options"] = {"include_usage": True}
        payload = self._request("POST", _CHAT_PATH, body=body,
                                streaming=stream, on_content=on_content)
        try:
            completion = _parse_completion(payload)
        except ProviderError:
            self._stats["succeeded"] = max(0, self._stats["succeeded"] - 1)
            self._stats["failed"] += 1
            self._stats["last_error_code"] = "invalid_completion"
            raise
        if self._stats.get("last_request_id") is None and completion.request_id:
            self._stats["last_request_id"] = completion.request_id
        return completion

    def _connect(self):
        return self.connection_factory(_API_HOST, timeout=self.connect_timeout, context=self.ssl_context)

    def _request(self, method, path, *, body=None, streaming=False, on_content=None):
        encoded = None if body is None else json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if encoded is not None and len(encoded) > _MAX_BODY:
            raise ProviderError("DeepSeek request exceeds the bounded request size.")
        discovery = method == "GET"
        if not discovery:
            self._stats.update({"last_http_status": None, "last_failure_phase": None,
                "last_elapsed_seconds": None, "last_error_code": None, "last_usage": None,
                "last_request_id": None, "last_reasoning_chars": None, "last_finish_reason": None,
                "last_response_diagnostic": None, "last_application_failure_phase": None,
                "last_application_error_code": None})
            self._stats["attempted"] += 1
        started, deadline = time.monotonic(), time.monotonic() + self.total_timeout
        connection = None
        phase = "connect"
        success = False
        try:
            connection = self._connect()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            connection.timeout = min(self.connect_timeout, remaining)
            if connection.sock is not None:
                connection.sock.settimeout(min(self.read_timeout, remaining))
            headers = {"Accept": "text/event-stream" if streaming else "application/json",
                       "User-Agent": "AgentForge/1.2"}
            if encoded is not None:
                headers["Content-Type"] = "application/json"
            # The bearer credential is applied only to the fixed official TLS host.
            headers["Authorization"] = "Bearer " + self.api_key
            phase = "request_write"
            connection.request(method, path, body=encoded, headers=headers)
            if not discovery:
                self._stats["sent"] += 1
            if connection.sock is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError
                connection.sock.settimeout(min(self.read_timeout, remaining))
            phase = "response_headers"
            response = connection.getresponse()
            if not discovery:
                self._stats["last_http_status"] = response.status
            if 300 <= response.status < 400:
                raise _DeepSeekFailure("redirect_rejected", "DeepSeek redirect was rejected.")
            if response.status != 200:
                code, message = _SAFE_ERRORS.get(response.status, ("server_error", f"DeepSeek returned HTTP {response.status}."))
                raise _DeepSeekFailure(code, message, http_status=response.status)
            phase = "response_body"
            sock = getattr(connection, "sock", None)
            if sock is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError
                sock.settimeout(min(self.read_timeout, remaining))
            if streaming:
                def prepare_stream_read():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError
                    if connection.sock is not None:
                        connection.sock.settimeout(min(self.read_timeout, remaining))
                payload = _read_sse(response, on_content=on_content, max_bytes=_MAX_BODY,
                                    deadline=deadline, prepare_read=prepare_stream_read)
            else:
                raw = response.read(_MAX_BODY + 1)
                if len(raw) > _MAX_BODY:
                    raise _DeepSeekFailure("response_too_large", "DeepSeek response exceeds the bounded response size.")
                try:
                    payload = json.loads(raw.decode("utf-8"))
                except (UnicodeError, json.JSONDecodeError):
                    raise _DeepSeekFailure("invalid_json", "DeepSeek returned an invalid JSON response.") from None
            if not isinstance(payload, dict):
                raise _DeepSeekFailure("invalid_response", "DeepSeek returned an invalid response object.")
            if not discovery:
                self._stats["last_response_diagnostic"] = _response_diagnostic(payload)
                usage = payload.get("usage")
                self._stats["last_usage"] = _usage_metadata(usage)
                self._stats["last_request_id"] = _bounded_header(response, "x-request-id")
                choices = payload.get("choices")
                if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                    self._stats["last_finish_reason"] = choices[0].get("finish_reason")
                    message = choices[0].get("message", choices[0].get("delta", {}))
                    if isinstance(message, dict):
                        reason = message.get("reasoning_content")
                        self._stats["last_reasoning_chars"] = len(reason) if isinstance(reason, str) else 0
                self._stats["succeeded"] += 1
                success = True
            else:
                # Discovery is an HTTP operation too; mark its transport as
                # successful so the finally block does not count a failure.
                success = True
            return payload
        except _DeepSeekFailure as exc:
            if not discovery:
                self._stats["last_error_code"] = exc.code
                self._stats["last_failure_phase"] = phase
                if exc.http_status is not None:
                    self._stats["last_http_status"] = exc.http_status
            raise ProviderError(exc.message) from None
        except (socket.timeout, TimeoutError):
            if not discovery:
                self._stats["last_error_code"] = "timeout"
                self._stats["last_failure_phase"] = phase
            raise ProviderError("DeepSeek request timed out; no retry was attempted.") from None
        except (OSError, http.client.HTTPException, ssl.SSLError):
            if not discovery:
                self._stats["last_error_code"] = "network_error"
                self._stats["last_failure_phase"] = phase
            raise ProviderError("DeepSeek network request failed; no retry was attempted.") from None
        except ProviderError:
            if not discovery:
                self._stats["last_error_code"] = "provider_error"
                self._stats["last_failure_phase"] = phase
            raise
        finally:
            if connection is not None:
                connection.close()
            if not discovery:
                self._stats["last_elapsed_seconds"] = round(time.monotonic() - started, 6)
                if not success:
                    self._stats["failed"] += 1
            elif not success:
                self._stats["discovery_failed"] += 1


class _DeepSeekFailure(Exception):
    def __init__(self, code, message, http_status=None):
        self.code, self.message, self.http_status = code, message, http_status


def _validate_messages(messages):
    if not isinstance(messages, list) or not messages or len(messages) > 256:
        raise ValueError("DeepSeek messages must be a non-empty bounded list.")
    output = []
    allowed = {"system", "user", "assistant", "tool"}
    pending_tool_ids = set()
    seen_tool_ids = set()
    for item in messages:
        if not isinstance(item, dict) or item.get("role") not in allowed:
            raise ValueError("DeepSeek message role is invalid.")
        role = item["role"]
        if pending_tool_ids and role != "tool":
            raise ValueError("Every requested tool must receive a result before another chat message.")
        content = item.get("content")
        if content is not None and not isinstance(content, str):
            raise ValueError("DeepSeek message content must be text or null.")
        cleaned = {"role": role, "content": content}
        # Preserve protocol continuation fields only after strict shape checks.
        if "tool_calls" in item:
            calls = item["tool_calls"]
            if role != "assistant" or not isinstance(calls, list) or not 1 <= len(calls) <= 8:
                raise ValueError("DeepSeek tool call history is invalid.")
            checked_calls = []
            for call in calls:
                if not isinstance(call, dict) or call.get("type") != "function":
                    raise ValueError("DeepSeek tool-call protocol state is invalid.")
                call_id, function = call.get("id"), call.get("function")
                if (not isinstance(call_id, str) or not call_id or len(call_id) > 256
                        or any(ord(ch) < 32 for ch in call_id) or call_id in seen_tool_ids
                        or not isinstance(function, dict)):
                    raise ValueError("DeepSeek tool-call identity is invalid.")
                name, arguments = function.get("name"), function.get("arguments")
                if (not isinstance(name, str) or not name or len(name) > 128
                        or not isinstance(arguments, str) or len(arguments) > 16_384):
                    raise ValueError("DeepSeek tool-call request is invalid.")
                try:
                    parsed = json.loads(arguments)
                except json.JSONDecodeError:
                    raise ValueError("DeepSeek tool-call arguments are invalid.") from None
                if not isinstance(parsed, dict):
                    raise ValueError("DeepSeek tool-call arguments must be an object.")
                checked_calls.append({"id": call_id, "type": "function",
                    "function": {"name": name, "arguments": arguments}})
                seen_tool_ids.add(call_id)
                pending_tool_ids.add(call_id)
            cleaned["tool_calls"] = checked_calls
        if "tool_call_id" in item:
            call_id = item["tool_call_id"]
            if (role != "tool" or not isinstance(call_id, str) or len(call_id) > 256
                    or call_id not in pending_tool_ids):
                raise ValueError("DeepSeek tool response reference is invalid.")
            pending_tool_ids.remove(call_id)
            cleaned["tool_call_id"] = call_id
        elif role == "tool":
            raise ValueError("DeepSeek tool message requires its matching tool-call ID.")
        if "reasoning_content" in item:
            reason = item["reasoning_content"]
            if role != "assistant" or "tool_calls" not in item or not isinstance(reason, str) or len(reason) > 64_000:
                raise ValueError("DeepSeek reasoning protocol state is invalid.")
            cleaned["reasoning_content"] = reason
        if role in {"system", "user"} and not isinstance(content, str):
            raise ValueError("DeepSeek system and user messages require text content.")
        if role == "assistant" and content is None and "tool_calls" not in item:
            raise ValueError("DeepSeek assistant message requires final content or tool calls.")
        if role == "tool" and not isinstance(content, str):
            raise ValueError("DeepSeek tool results require text content.")
        output.append(cleaned)
    if pending_tool_ids:
        raise ValueError("DeepSeek tool-call history is incomplete.")
    return output


def _parse_completion(payload):
    choices = payload.get("choices")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        raise ProviderError("DeepSeek returned an invalid completion choices list.")
    choice = choices[0]
    message = choice.get("message", choice.get("delta"))
    if not isinstance(message, dict):
        raise ProviderError("DeepSeek returned an invalid assistant message.")
    content = message.get("content")
    reasoning = message.get("reasoning_content")
    if content is not None and not isinstance(content, str):
        raise ProviderError("DeepSeek returned invalid final content.")
    if reasoning is not None and not isinstance(reasoning, str):
        raise ProviderError("DeepSeek returned invalid reasoning metadata.")
    calls = message.get("tool_calls", [])
    if not isinstance(calls, list) or len(calls) > 8:
        raise ProviderError("DeepSeek returned invalid tool-call data.")
    parsed_calls = []
    for call in calls:
        if not isinstance(call, dict) or not isinstance(call.get("function"), dict):
            raise ProviderError("DeepSeek returned a malformed tool call.")
        function = call["function"]
        name, arguments = function.get("name"), function.get("arguments")
        call_id = call.get("id")
        if (not isinstance(call_id, str) or not call_id or len(call_id) > 256
                or any(ord(ch) < 32 for ch in call_id)
                or not isinstance(name, str) or not name or len(name) > 128
                or any(ord(ch) < 32 for ch in name)
                or not isinstance(arguments, str) or len(arguments) > 16_384):
            raise ProviderError("DeepSeek returned malformed tool-call arguments.")
        try:
            decoded = json.loads(arguments)
        except json.JSONDecodeError:
            raise ProviderError("DeepSeek returned non-JSON tool-call arguments.") from None
        if not isinstance(decoded, dict):
            raise ProviderError("DeepSeek tool-call arguments must be a JSON object.")
        parsed_calls.append({"id": call_id, "name": name, "arguments": decoded,
                             "raw_arguments": arguments})
    finish = choice.get("finish_reason")
    if finish is not None and not isinstance(finish, str):
        raise ProviderError("DeepSeek returned an invalid finish reason.")
    usage = _usage_metadata(payload.get("usage"))
    request_id = payload.get("id")
    if not isinstance(request_id, str) or len(request_id) > 256:
        request_id = None
    return DeepSeekCompletion(content or "", reasoning, finish, tuple(parsed_calls), usage, request_id)


def _usage_metadata(value):
    if not isinstance(value, dict):
        return {}
    result = {}
    for output, key in (("prompt_tokens", "prompt_tokens"), ("completion_tokens", "completion_tokens"),
                        ("total_tokens", "total_tokens")):
        number = value.get(key)
        if isinstance(number, int) and not isinstance(number, bool) and number >= 0:
            result[output] = number
    details = value.get("completion_tokens_details")
    if isinstance(details, dict):
        tokens = details.get("reasoning_tokens")
        if isinstance(tokens, int) and not isinstance(tokens, bool) and tokens >= 0:
            result["reasoning_tokens"] = tokens
    # Streaming usage has already been normalized by the SSE parser.
    tokens = value.get("reasoning_tokens")
    if "reasoning_tokens" not in result and isinstance(tokens, int) and not isinstance(tokens, bool) and tokens >= 0:
        result["reasoning_tokens"] = tokens
    for output, key in (("prompt_cache_hit_tokens", "prompt_cache_hit_tokens"),
                        ("prompt_cache_miss_tokens", "prompt_cache_miss_tokens")):
        number = value.get(key)
        if isinstance(number, int) and not isinstance(number, bool) and number >= 0:
            result[output] = number
    return result


def _validate_tool_schemas(tools):
    if not isinstance(tools, list) or len(tools) > 8:
        raise ValueError("Tool schema list exceeds the bounded limit.")
    checked, seen = [], set()
    for tool in tools:
        if not isinstance(tool, dict) or tool.get("type") != "function" or not isinstance(tool.get("function"), dict):
            raise ValueError("DeepSeek tool schema is invalid.")
        function = tool["function"]
        name, description, parameters = function.get("name"), function.get("description"), function.get("parameters")
        if (not isinstance(name, str) or not name or len(name) > 128 or name in seen
                or not isinstance(description, str) or len(description) > 512
                or not isinstance(parameters, dict) or len(json.dumps(parameters)) > 16_384):
            raise ValueError("DeepSeek tool schema fields are invalid.")
        seen.add(name)
        checked.append({"type": "function", "function": {
            "name": name, "description": description, "parameters": parameters,
        }})
    return checked


def _response_diagnostic(payload):
    choices = payload.get("choices")
    message = choices[0].get("message", {}) if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
    content = message.get("content") if isinstance(message, dict) else None
    reasoning = message.get("reasoning_content") if isinstance(message, dict) else None
    return {"root_type": type(payload).__name__, "choice_count": len(choices) if isinstance(choices, list) else None,
            "content_chars": len(content) if isinstance(content, str) else None,
            "reasoning_chars": len(reasoning) if isinstance(reasoning, str) else None,
            "tool_call_count": len(message.get("tool_calls", [])) if isinstance(message, dict) and isinstance(message.get("tool_calls", []), list) else None}


def _bounded_header(response, name):
    value = response.getheader(name)
    return value[:256] if isinstance(value, str) and all(ord(ch) >= 32 for ch in value) else None


def _read_sse(response, *, on_content=None, max_bytes=_MAX_BODY, deadline=None,
              prepare_read=None, monotonic=time.monotonic):
    content, reasoning = [], []
    tool_calls: dict[int, dict] = {}
    usage = {}
    finish_reason = None
    request_id = None
    total = 0
    data_lines = []
    done = False

    def dispatch(data):
        nonlocal usage, finish_reason, request_id, done
        if data == "[DONE]":
            done = True
            return
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            raise _DeepSeekFailure("invalid_stream_json", "DeepSeek stream contained invalid JSON.") from None
        if not isinstance(chunk, dict):
            raise _DeepSeekFailure("invalid_stream_chunk", "DeepSeek stream contained an invalid event.")
        if request_id is None and isinstance(chunk.get("id"), str):
            request_id = chunk["id"][:256]
        choices = chunk.get("choices")
        if not isinstance(choices, list):
            raise _DeepSeekFailure("invalid_stream_chunk", "DeepSeek stream has no choices array.")
        if choices and isinstance(choices[0], dict):
            choice = choices[0]
            delta = choice.get("delta", {})
            if not isinstance(delta, dict):
                raise _DeepSeekFailure("invalid_stream_delta", "DeepSeek stream delta is invalid.")
            piece = delta.get("content")
            if piece is not None:
                if not isinstance(piece, str):
                    raise _DeepSeekFailure("invalid_stream_content", "DeepSeek stream content is invalid.")
                content.append(piece)
                if on_content is not None:
                    on_content(piece)
            reason = delta.get("reasoning_content")
            if reason is not None:
                if not isinstance(reason, str):
                    raise _DeepSeekFailure("invalid_stream_reasoning", "DeepSeek reasoning chunk is invalid.")
                reasoning.append(reason)
            calls = delta.get("tool_calls", [])
            if not isinstance(calls, list) or len(calls) > 8:
                raise _DeepSeekFailure("invalid_stream_tool_call", "DeepSeek tool-call stream is invalid.")
            for call in calls:
                if not isinstance(call, dict) or not isinstance(call.get("index"), int):
                    raise _DeepSeekFailure("invalid_stream_tool_call", "DeepSeek tool-call stream is invalid.")
                index = call["index"]
                current = tool_calls.setdefault(index, {"id": "", "type": "function", "name": "", "arguments": ""})
                if isinstance(call.get("id"), str): current["id"] += call["id"]
                function = call.get("function", {})
                if isinstance(function, dict):
                    if isinstance(function.get("name"), str): current["name"] += function["name"]
                    if isinstance(function.get("arguments"), str): current["arguments"] += function["arguments"]
            if choice.get("finish_reason") is not None:
                finish_reason = choice["finish_reason"]
        if isinstance(chunk.get("usage"), dict):
            usage = _usage_metadata(chunk["usage"])

    while not done:
        if prepare_read is not None:
            prepare_read()
        elif deadline is not None and monotonic() >= deadline:
            raise TimeoutError
        line = response.readline(65_537)
        if deadline is not None and monotonic() >= deadline:
            raise TimeoutError
        if not line:
            break
        total += len(line)
        if total > max_bytes or len(line) > 65_536:
            raise _DeepSeekFailure("stream_too_large", "DeepSeek stream exceeded its size limit.")
        line = line.rstrip(b"\r\n")
        if not line:
            if data_lines:
                dispatch("\n".join(data_lines))
                data_lines = []
            continue
        if line.startswith(b":"):
            continue
        if line.startswith(b"data:"):
            try:
                data_lines.append(line[5:].lstrip().decode("utf-8"))
            except UnicodeError:
                raise _DeepSeekFailure("invalid_stream_utf8", "DeepSeek stream contained invalid UTF-8.") from None
    if data_lines and not done:
        dispatch("\n".join(data_lines))
    if not done:
        raise _DeepSeekFailure("incomplete_stream", "DeepSeek stream ended before its completion marker.")
    calls = [{"id": value["id"], "type": value["type"],
              "function": {"name": value["name"], "arguments": value["arguments"]}}
             for _, value in sorted(tool_calls.items())]
    return {"id": request_id, "choices": [{"message": {"content": "".join(content),
              "reasoning_content": "".join(reasoning), "tool_calls": calls},
              "finish_reason": finish_reason}], "usage": usage}


class DeepSeekProvider:
    provider_id = "deepseek"

    def __init__(self, client: DeepSeekClient, model: str, *, max_output_tokens: int = 256,
                 thinking: bool = False, effort: str | None = None, stream: bool = False,
                 descriptor=None):
        if not isinstance(client, DeepSeekClient):
            raise ValueError("DeepSeek client is required.")
        if not isinstance(thinking, bool):
            raise ValueError("thinking must be a boolean.")
        if effort is not None and (not thinking or effort not in {"low", "high", "max"}):
            raise ValueError("Reasoning effort is invalid or thinking is disabled.")
        self.client, self.model = client, model
        self.descriptor = descriptor or _model_descriptor(model)
        if self.descriptor.provider_id != "deepseek" or self.descriptor.model_id != model:
            raise ValueError("Selected DeepSeek model does not match its registry metadata.")
        if effort is not None and effort not in self.descriptor.supported_efforts:
            raise ValueError("Reasoning effort is not supported by this DeepSeek model.")
        self.max_output_tokens, self.thinking, self.effort = validate_output_tokens(max_output_tokens), thinking, effort
        self.stream = bool(stream)
        if self.stream and self.descriptor.supports_streaming is not True:
            raise ValueError("Streaming capability is unknown or unsupported for this DeepSeek model.")
        if self.thinking and self.descriptor.supports_thinking is not True:
            raise ValueError("Thinking capability is unknown or unsupported for this DeepSeek model.")

    @property
    def thinking_request(self):
        return self.thinking if self.descriptor.supports_thinking is True else None

    def request_stats(self):
        return self.client.request_stats()

    def summary_text(self, messages, *, max_output_tokens: int):
        output_tokens = min(validate_output_tokens(max_output_tokens),
                            self.descriptor.max_output_tokens or validate_output_tokens(max_output_tokens))
        result = self.client.chat(messages, model=self.model, max_output_tokens=output_tokens,
                                  thinking=self.thinking_request, effort=self.effort, stream=False)
        if result.tool_calls or not result.content.strip():
            raise ProviderError("DeepSeek did not return plain summary text.")
        return result.content

    def answer(self, context: BuiltContext, *, conversation: dict | None = None,
               settings: dict | None = None, on_content=None) -> Answer:
        if not context.evidence:
            from .offline import OfflineProvider
            return OfflineProvider().answer(context)
        thinking = self.thinking if settings is None else settings.get("thinking_enabled", self.thinking)
        effort = self.effort if settings is None else settings.get("reasoning_effort", self.effort)
        if not isinstance(thinking, bool) or (thinking and self.descriptor.supports_thinking is not True):
            raise ProviderError("Thinking capability is unknown or unsupported; no request was sent.")
        if effort is not None and (thinking is not True or effort not in self.descriptor.supported_efforts):
            raise ProviderError("Reasoning effort is unsupported or thinking is disabled; no request was sent.")
        if self.stream and self.descriptor.supports_streaming is not True:
            raise ProviderError("Streaming capability is unknown or unsupported; no request was sent.")
        messages = _model_messages(context, conversation, [
            {"role": "system", "content": _ANSWER_SYSTEM_PROMPT},
            {"role": "user", "content": f"Question:\n{context.question}\n\nUntrusted evidence:\n"
                                        f"{_model_evidence_text(context)}\n\nUse only these evidence IDs: "
                                        f"{', '.join('[' + source.source_id + ']' for source in context.evidence)}. "
                                        "Cite the applicable IDs."},
        ], descriptor=self.descriptor, max_output_tokens=self.max_output_tokens)
        result = self.client.chat(messages, model=self.model, max_output_tokens=self.max_output_tokens,
                                  thinking=(thinking if self.descriptor.supports_thinking is True else None),
                                  effort=effort, stream=self.stream,
                                  on_content=on_content if self.stream else None)
        if result.finish_reason == "length":
            self.client.record_application_failure("response_validation", "truncated_final_content")
            raise ProviderError("DeepSeek stopped at the output limit; the partial answer was rejected.")
        if result.tool_calls:
            self.client.record_application_failure("tool_dispatch", "model_tools_not_enabled_for_rag_ask")
            raise ProviderError("DeepSeek requested a tool call, but this RAG ask path does not execute model tools.")
        if not result.content.strip():
            self.client.record_application_failure("response_validation", "empty_final_content")
            raise ProviderError("DeepSeek returned empty final content; reasoning is not a final answer.")
        if result.finish_reason not in {None, "stop"}:
            self.client.record_application_failure("response_validation", "unexpected_finish_reason")
            raise ProviderError("DeepSeek returned an unexpected final-response finish reason.")
        try:
            citations = validate_model_citations(result.content, context)
        except CitationValidationError as exc:
            self.client._stats.update({"last_application_failure_phase": "citation_validation",
                "last_application_error_code": exc.code})
            raise
        return Answer(result.content, citations, False)
