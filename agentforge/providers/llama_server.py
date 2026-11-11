"""仅通过 HTTP 访问回环地址的客户端。不使用 DNS 主机名、代理或重定向，也不管理进程。"""

import http.client
import ipaddress
import json
import socket, time
from urllib.parse import urlsplit

from ..errors import ProviderError
from ..context_assembler import ContextAssembler
from ..models import Answer, BuiltContext
from ..model_types import LOCAL_QWEN
from ..observability import (current_lifecycle, current_stage, finish_provider_stage,
                             lifecycle_stage)
from ..token_budget import ContextBudgetManager
from .dialogue_protocol import (
    _ANSWER_SYSTEM_PROMPT, _GENERAL_SYSTEM_PROMPT, _HYBRID_SYSTEM_PROMPT,
    _conversation_messages, _external_data_unavailable_prompt,
    _model_evidence_text, _model_messages, with_planner_task_outline,
    validate_output_tokens, validate_timeout,
)




class LlamaServerClient:
    def __init__(
        self, endpoint: str, *, api_key: str | None = None,
        connect_timeout: float = 2.0, read_timeout: float = 60.0,
        total_timeout: float = 180.0, max_output_tokens: int = 512,
    ):
        try:
            parsed = urlsplit(endpoint)
            host = parsed.hostname
            if (parsed.scheme != "http" or not host or "%" in host
                    or parsed.username is not None or parsed.password is not None
                    or parsed.query or parsed.fragment):
                raise ValueError
            address = ipaddress.ip_address(host)
            if not address.is_loopback:
                raise ValueError
            port = parsed.port if parsed.port is not None else 80
            if not 1 <= port <= 65535:
                raise ValueError
            if parsed.path not in ("", "/v1/chat/completions"):
                raise ValueError
        except (ValueError, TypeError):
            raise ValueError(
                "Endpoint must be an explicit loopback IP over HTTP, with no credentials/query/fragment "
                "and path /v1/chat/completions."
            ) from None
        connect_timeout = validate_timeout(
            connect_timeout, "connect_timeout", 0, 120, inclusive_minimum=False
        )
        read_timeout = validate_timeout(
            read_timeout, "read_timeout", 5, 120, inclusive_minimum=True
        )
        total_timeout = validate_timeout(
            total_timeout, "total_timeout", 10, 300, inclusive_minimum=True
        )
        max_output_tokens = validate_output_tokens(
            max_output_tokens, maximum=LOCAL_QWEN.adapter_max_output_tokens,
        )
        if (not isinstance(api_key, str) or not api_key or api_key != api_key.strip()
                or any(ord(char) < 33 or ord(char) > 126 for char in api_key)):
            raise ValueError("A non-empty printable ASCII API key is required for llama-server.")
        self.host = str(address)
        self.port = port
        self.path = "/v1/chat/completions"
        self.api_key = api_key
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self.total_timeout = total_timeout
        self.max_output_tokens = max_output_tokens
        self._request_stats = {
            "attempted": 0, "sent": 0, "succeeded": 0, "failed": 0,
            "last_http_status": None, "last_failure_phase": None,
            "last_elapsed_seconds": None, "last_response_chars": None,
            "last_usage": None, "last_request_id": None,
            "last_requested_output_tokens": None,
            "last_first_response_ms": None,
            "last_finish_reason": None, "last_had_reasoning_content": False,
            "last_error_code": None, "last_response_diagnostic": None,
            "last_application_failure_phase": None, "last_application_error_code": None,
            "last_citation_diagnostic": None, "retries": 0,
        }

    def request_stats(self) -> dict:
        """返回用于执行审计的非敏感计数。"""
        return dict(self._request_stats)

    def record_application_failure(
        self, phase: str, code: str, diagnostic: dict | None = None,
    ) -> None:
        self._request_stats.update({"last_application_failure_phase": phase,
                                    "last_application_error_code": code,
                                    "last_citation_diagnostic": dict(diagnostic) if diagnostic else None})

    def chat(
        self, messages: list[dict], *, model: str,
        max_output_tokens: int | None = None,
        timeout_seconds: float | None = None,
    ) -> str:
        output_tokens = (self.max_output_tokens if max_output_tokens is None
                         else validate_output_tokens(
                             max_output_tokens, maximum=LOCAL_QWEN.adapter_max_output_tokens,
                         ))
        timeout = self.total_timeout if timeout_seconds is None else validate_timeout(
            timeout_seconds, "request timeout", 10, self.total_timeout, inclusive_minimum=True,
        )
        if current_lifecycle() is None:
            return _chat_request(self, messages, model, output_tokens=output_tokens,
                                 timeout_seconds=timeout)
        before = self.request_stats()
        parent_stage = current_stage()
        with lifecycle_stage("provider_request", metadata={
            "provider": "llama-server", "model": model,
        }) as request_stage:
            try:
                result = _chat_request(self, messages, model, output_tokens=output_tokens,
                                       timeout_seconds=timeout)
            except BaseException as exc:
                finish_provider_stage(request_stage, provider="llama-server", model=model,
                                      before=before, after=self.request_stats(), error=exc,
                                      parent_stage=parent_stage)
                raise
            finish_provider_stage(request_stage, provider="llama-server", model=model,
                                  before=before, after=self.request_stats(),
                                  parent_stage=parent_stage)
            return result




class LlamaServerProvider:
    """显式本地生成接口；不管理服务器进程。"""
    provider_id = "llama-server"

    def __init__(self, client: LlamaServerClient, model: str, descriptor=None,
                 output_budget_mode: str = "standard",
                 context_assembler: ContextAssembler | None = None):
        self.client = client
        self.model = model
        self.descriptor = descriptor or LOCAL_QWEN
        self.context_assembler = context_assembler or ContextAssembler()
        configured_output = getattr(client, "max_output_tokens", None)
        if configured_output is not None and (
                isinstance(configured_output, bool)
                or not isinstance(configured_output, int) or configured_output < 1):
            # The descriptor remains authoritative when a compatible client or
            # a test double does not expose a concrete transport ceiling.
            configured_output = None
        output_budget = self.context_assembler.budgets.resolve_output_budget(
            self.descriptor, mode=output_budget_mode,
            explicit_output_tokens=configured_output,
        )
        self.max_output_tokens = output_budget.effective_tokens
        self.output_budget = output_budget
        self.last_summary_budget_diagnostics = None

    def request_stats(self) -> dict | None:
        getter = getattr(self.client, "request_stats", None)
        if not callable(getter):
            return None
        value = getter()
        if not isinstance(value, dict):
            return None
        from dataclasses import asdict
        return {**value, "output_budget": asdict(self.output_budget)}

    def plan_completion(self, messages: list[dict], *, max_output_tokens: int,
                        timeout_seconds: float) -> str:
        """Run bounded non-streaming planning on the already selected local model."""
        output_tokens = validate_output_tokens(max_output_tokens, maximum=self.max_output_tokens)
        # LLMExecutionPlanner has already assembled the selected history and
        # summary for this model. Rebuilding it as a two-message dialogue here
        # rejects valid planner requests as soon as they contain prior turns.
        # Revalidate the complete request against the provider's model budget
        # without treating its messages as a new dialogue to assemble.
        planned, _diagnostics = self.context_assembler.fit_messages(
            messages, descriptor=self.descriptor,
            requested_output_tokens=output_tokens,
            phase="agent_execution_planner_provider",
        )
        text = self.client.chat(planned, model=self.model,
                                max_output_tokens=output_tokens,
                                timeout_seconds=min(timeout_seconds, self.client.total_timeout))
        if not isinstance(text, str) or not text.strip():
            raise ProviderError("The configured local model returned no planning output.")
        stats = self.request_stats()
        finish_reason = stats.get("last_finish_reason") if isinstance(stats, dict) else None
        if finish_reason != "stop":
            recorder = getattr(self.client, "record_application_failure", None)
            if callable(recorder):
                recorder("planning_response", "incomplete_planning_completion")
            raise ProviderError("The configured local model did not complete the planning response.")
        return text

    def answer(self, context: BuiltContext, *, conversation: dict | None = None,
               settings: dict | None = None, on_content=None) -> Answer:
        if not context.evidence:
            raise ProviderError("Repository generation requires request-local evidence.")
        descriptor = self.descriptor
        user_text = (f"Question:\n{context.question}\n\nUntrusted evidence:\n"
                     f"{_model_evidence_text(context)}\n\nUse only these evidence IDs: "
                     f"{', '.join('[' + source.source_id + ']' for source in context.evidence)}. "
                     "Cite the applicable IDs.")
        messages = _model_messages(context, conversation, [
            {"role": "system", "content": _ANSWER_SYSTEM_PROMPT},
            {"role": "user", "content": with_planner_task_outline(context, user_text)},
        ], descriptor=descriptor,
            max_output_tokens=self.max_output_tokens, context_assembler=self.context_assembler)
        text = self.client.chat(messages, model=self.model)
        # Provider 只负责模型传输和响应完整性。来源 ID、路径及行号
        # 由上层 Answer Contract 按当前请求上下文统一校验。
        return Answer(text, (), False)

    def answer_general(self, context: BuiltContext, *, conversation=None,
                       settings=None, on_content=None,
                       external_data_unavailable=False,
                       repository_evidence_unavailable=False) -> Answer:
        prompt = _external_data_unavailable_prompt(
            _GENERAL_SYSTEM_PROMPT, external_data_unavailable,
        )
        if repository_evidence_unavailable:
            prompt += (
                " A repository search was attempted but produced no verified source evidence. "
                "Do not claim what this repository implements. State that repository behavior cannot be "
                "confirmed, while still answering any general principle in the question."
            )
        messages = _model_messages(context, conversation, [
            {"role": "system", "content": prompt},
            {"role": "user", "content": with_planner_task_outline(
                context, "Question:\n" + context.question)},
        ], descriptor=self.descriptor,
            max_output_tokens=self.max_output_tokens, mode="general",
            context_assembler=self.context_assembler)
        text = self.client.chat(messages, model=self.model)
        if not isinstance(text, str) or not text.strip():
            raise ProviderError("The configured Qwen service returned no general answer.")
        return Answer(text, (), False)

    def answer_hybrid(self, context: BuiltContext, *, conversation=None,
                      settings=None, on_content=None,
                      external_data_unavailable=False) -> Answer:
        ids = ", ".join(f"[{source.source_id}]" for source in context.evidence)
        evidence = (_model_evidence_text(context) if context.evidence
                    else "No relevant repository evidence was retrieved.")
        user_text = (f"Question:\n{context.question}\n\nCurrent repository evidence:\n{evidence}\n"
                     f"\nAvailable source IDs: {ids or '(none)'}. Cite only applicable IDs in the Repository evidence section.")
        user_text = with_planner_task_outline(context, user_text)
        messages = _model_messages(context, conversation, [
            {"role": "system", "content": _external_data_unavailable_prompt(
                _HYBRID_SYSTEM_PROMPT, external_data_unavailable)},
            {"role": "user", "content": user_text},
        ], descriptor=self.descriptor,
            max_output_tokens=self.max_output_tokens, mode="hybrid",
            context_assembler=self.context_assembler)
        text = self.client.chat(messages, model=self.model)
        if not isinstance(text, str) or not text.strip():
            raise ProviderError("The configured Qwen service returned no hybrid answer.")
        return Answer(text, (), False)

    def summary_text(self, messages, *, max_output_tokens: int):
        from ..conversation_context import ContextBudgetExceeded
        output_tokens = validate_output_tokens(max_output_tokens, maximum=self.max_output_tokens)
        try:
            bounded_messages, diagnostics = self.context_assembler.fit_messages(
                messages, descriptor=self.descriptor,
                requested_output_tokens=output_tokens,
                phase="conversation_summary",
            )
        except ContextBudgetExceeded as exc:
            self.last_summary_budget_diagnostics = {
                "phase": "conversation_summary", **exc.diagnostics,
                "request_sent": False, "request_succeeded": False,
            }
            raise
        self.last_summary_budget_diagnostics = {
            **diagnostics, "request_sent": True, "request_succeeded": False,
        }
        text = self.client.chat(
            bounded_messages, model=self.model, max_output_tokens=output_tokens,
        )
        self.last_summary_budget_diagnostics["request_succeeded"] = True
        return text



def _chat_request(
    client: LlamaServerClient, messages: list[dict], model: str, *, output_tokens: int,
    timeout_seconds: float,
) -> str:
        if not isinstance(model, str) or not model.strip():
            raise ValueError("An explicit model name is required.")
        body = json.dumps({
            "model": model,
            "messages": messages,
            "temperature": 0,
            "stream": False,
            "max_tokens": output_tokens,
            "chat_template_kwargs": {"enable_thinking": False},
        }).encode("utf-8")
        if len(body) > 1_000_000:
            raise ProviderError("Request exceeds the client size limit.")
        client._request_stats.update({
            "last_http_status": None, "last_failure_phase": None,
            "last_elapsed_seconds": None, "last_response_chars": None,
            "last_usage": None, "last_request_id": None,
            "last_first_response_ms": None,
            "last_requested_output_tokens": output_tokens,
            "last_finish_reason": None, "last_had_reasoning_content": False,
            "last_error_code": None, "last_response_diagnostic": None,
            "last_application_failure_phase": None,
            "last_application_error_code": None,
            "last_citation_diagnostic": None,
        })
        client._request_stats["attempted"] += 1
        started = time.monotonic()
        deadline = started + timeout_seconds

        def remaining_timeout(limit: float) -> float:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProviderError("Local generation total timeout exceeded.")
            return min(limit, remaining)

        # http.client 直接建立连接，绝不会读取环境代理设置。
        # 它不支持跟随重定向。
        connection = http.client.HTTPConnection(
            client.host, client.port, timeout=remaining_timeout(client.connect_timeout)
        )
        timeout_phase = "connection"
        succeeded = False
        try:
            connection.connect()
            if connection.sock is None:
                raise ProviderError("Local connection did not provide a socket.")
            # 每个阻塞阶段开始前都重置超时，以同时执行阶段时限和
            # 整次生成请求的总时限。
            connection.sock.settimeout(remaining_timeout(client.read_timeout))
            timeout_phase = "request write"
            connection.request(
                "POST", client.path, body=body,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "Authorization": f"Bearer {client.api_key}",
                },
            )
            client._request_stats["sent"] += 1
            connection.sock.settimeout(remaining_timeout(client.read_timeout))
            timeout_phase = "response headers"
            response = connection.getresponse()
            client._request_stats["last_http_status"] = response.status
            tracker = current_lifecycle()
            if tracker is not None:
                client._request_stats["last_first_response_ms"] = max(
                    0, round((time.monotonic() - started) * 1000),
                )
                tracker.observe("provider_request", "RESPONSE_HEADERS", status="RUNNING",
                                metadata={"http_status": response.status,
                                          "first_response_ms": client._request_stats["last_first_response_ms"]},
                                console=True)
            def set_response_timeout():
                # 收到 Connection: close 后，HTTPConnection 可能会解除其 socket 关联；
                # 但 HTTPResponse 仍持有读取响应正文所需的文件或 socket。
                sock = connection.sock
                if sock is None:
                    sock = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
                if sock is None:
                    raise ProviderError("Cannot enforce local response read timeout.")
                sock.settimeout(remaining_timeout(client.read_timeout))
            if 300 <= response.status < 400:
                raise ProviderError("HTTP redirect rejected.")
            if response.status != 200:
                # 只保留结构化且经过白名单筛选的诊断信息。服务器
                # 错误可能回显提示词或凭证，因此绝不保存错误正文。
                diagnostic = {"http_status": response.status}
                try:
                    set_response_timeout()
                    error_body = response.read(8193)
                    diagnostic["body_bytes_bounded"] = len(error_body)
                    if len(error_body) <= 8192:
                        error_json = json.loads(error_body)
                        error = error_json.get("error") if isinstance(error_json, dict) else None
                        message = error.get("message") if isinstance(error, dict) else None
                        diagnostic["json_error_object"] = isinstance(error, dict)
                        if isinstance(message, str):
                            folded = message.casefold()
                            diagnostic["error_category"] = ("possible_context_limit" if
                                "context" in folded and any(word in folded for word in
                                    ("token", "length", "exceed", "large")) else "other")
                except (ValueError, UnicodeError, OSError, http.client.HTTPException):
                    diagnostic["body_parse"] = "unavailable"
                client._request_stats["last_response_diagnostic"] = diagnostic
                client._request_stats["last_error_code"] = "http_error"
                raise ProviderError(f"Local server returned HTTP {response.status}.")
            set_response_timeout()
            timeout_phase = "response body"
            raw = response.read(2_000_001)
            if len(raw) > 2_000_000:
                client._request_stats["last_error_code"] = "response_too_large"
                raise ProviderError("Local response exceeds the size limit.")
            timeout_phase = "response validation"
            diagnostic = {"body_bytes": len(raw)}

            def invalid_response(code: str) -> None:
                client._request_stats["last_error_code"] = code
                client._request_stats["last_response_diagnostic"] = dict(diagnostic)
                raise ProviderError(
                    f"Local server returned an invalid chat response ({code})."
                )

            try:
                decoded = raw.decode("utf-8")
            except UnicodeError:
                invalid_response("invalid_utf8")
            try:
                payload = json.loads(decoded)
            except (ValueError, TypeError):
                invalid_response("invalid_json")
            diagnostic["root_type"] = type(payload).__name__
            if not isinstance(payload, dict):
                invalid_response("invalid_response_root")
            client._request_stats["last_usage"] = _local_usage_metadata(payload.get("usage"))
            request_id = payload.get("id")
            if (isinstance(request_id, str) and len(request_id) <= 256
                    and all(ord(char) >= 32 for char in request_id)):
                client._request_stats["last_request_id"] = request_id
            choices = payload.get("choices")
            diagnostic["choices_type"] = type(choices).__name__
            diagnostic["choice_count"] = len(choices) if isinstance(choices, list) else None
            if not isinstance(choices, list) or not choices:
                invalid_response("invalid_choices")
            choice = choices[0]
            if not isinstance(choice, dict):
                invalid_response("invalid_choice")
            message = choice.get("message")
            diagnostic["message_type"] = type(message).__name__
            if not isinstance(message, dict):
                invalid_response("invalid_message")
            content = message.get("content")
            reasoning = message.get("reasoning_content")
            finish_reason = choice.get("finish_reason")
            diagnostic.update({
                "content_type": type(content).__name__,
                "content_chars": len(content) if isinstance(content, str) else None,
                "reasoning_content_type": type(reasoning).__name__,
                "reasoning_content_chars": len(reasoning) if isinstance(reasoning, str) else None,
                "finish_reason": finish_reason,
            })
            if reasoning is not None and not isinstance(reasoning, str):
                invalid_response("invalid_reasoning_content")
            if finish_reason is not None and not isinstance(finish_reason, str):
                invalid_response("invalid_finish_reason")
            client._request_stats.update({
                "last_finish_reason": finish_reason,
                "last_had_reasoning_content": bool(reasoning),
                "last_response_diagnostic": diagnostic,
            })
            if not isinstance(content, str) or not content.strip():
                invalid_response(
                    "empty_final_content_with_reasoning"
                    if isinstance(reasoning, str) and bool(reasoning.strip())
                    else "empty_final_content"
                )
            client._request_stats.update({
                "last_response_chars": len(content),
            })
            client._request_stats["succeeded"] += 1
            succeeded = True
            return content
        except (socket.timeout, TimeoutError):
            messages = {
                "connection": "Local server connection timed out.",
                "request write": "Timed out sending the request to the local server.",
                "response headers": "Local server response headers timed out.",
                "response body": "Local server response body timed out.",
            }
            raise ProviderError(messages[timeout_phase]) from None
        except ProviderError as exc:
            if "total timeout" in str(exc).casefold():
                timeout_phase = "total"
            raise
        except (OSError, http.client.HTTPException):
            raise ProviderError("Could not communicate with the configured local server.") from None
        finally:
            if not succeeded:
                client._request_stats["failed"] += 1
                client._request_stats["last_failure_phase"] = timeout_phase
            client._request_stats["last_elapsed_seconds"] = round(
                time.monotonic() - started, 6
            )
            connection.close()


def _local_usage_metadata(value):
    if not isinstance(value, dict):
        return {}
    output = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        number = value.get(key)
        if isinstance(number, int) and not isinstance(number, bool) and 0 <= number <= 1_000_000_000:
            output[key] = number
    prompt_details = value.get("prompt_tokens_details")
    if isinstance(prompt_details, dict):
        number = prompt_details.get("cached_tokens")
        if isinstance(number, int) and not isinstance(number, bool) and 0 <= number <= 1_000_000_000:
            output["prompt_cache_hit_tokens"] = number
    completion_details = value.get("completion_tokens_details")
    if isinstance(completion_details, dict):
        number = completion_details.get("reasoning_tokens")
        if isinstance(number, int) and not isinstance(number, bool) and 0 <= number <= 1_000_000_000:
            output["reasoning_tokens"] = number
    return output

