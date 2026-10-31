"""Small loopback-only client for llama.cpp's OpenAI-compatible embeddings API."""

from http.client import HTTPConnection, HTTPException
from ipaddress import ip_address
import json
import math
import socket
import time
from urllib.parse import urlsplit

from .base import EmbeddingError, normalize_vector


MAX_EMBEDDING_RESPONSE_BYTES = 1024 * 1024
DEFAULT_QUERY_INSTRUCTION = (
    "Given a question about a local source repository, retrieve source-code or "
    "documentation passages that answer the question."
)


class LlamaServerEmbeddingProvider:
    """Call a user-started local llama-server; never starts or discovers services."""

    def __init__(
        self,
        base_url: str,
        *,
        model_id: str,
        api_model: str,
        dimension: int = 1024,
        query_instruction: str = DEFAULT_QUERY_INSTRUCTION,
        connect_timeout: float = 2.0,
        read_timeout: float = 60.0,
    ):
        self._host, self._port = self._parse_base_url(base_url)
        self.model_id = self._validate_identifier(model_id, "model_id")
        self.api_model = self._validate_identifier(api_model, "api_model")
        if isinstance(dimension, bool) or not isinstance(dimension, int) or not 1 <= dimension <= 4096:
            raise EmbeddingError("Embedding dimension must be an integer from 1 to 4096.")
        self.dimension = dimension
        if not isinstance(query_instruction, str) or not query_instruction.strip():
            raise EmbeddingError("Query instruction must not be empty.")
        self.query_instruction = query_instruction.strip()
        self.connect_timeout = self._validate_timeout(connect_timeout, "connect_timeout")
        self.read_timeout = self._validate_timeout(read_timeout, "read_timeout")
        self.request_attempts = 0
        self.requests_sent = 0
        self.request_successes = 0
        self.request_failures = 0

    @property
    def request_counts(self) -> dict:
        """Return non-secret request counters for controlled evaluation evidence."""
        return {
            "attempts": self.request_attempts,
            "sent": self.requests_sent,
            "successes": self.request_successes,
            "failures": self.request_failures,
        }

    @staticmethod
    def _validate_identifier(value: str, name: str) -> str:
        if (not isinstance(value, str) or not value.strip() or len(value) > 512
                or any(ord(char) < 32 for char in value)):
            raise EmbeddingError(f"{name} must be a non-empty, bounded string.")
        return value.strip()

    @staticmethod
    def _validate_timeout(value: float, name: str) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise EmbeddingError(f"{name} must be a finite value from 0.1 to 120 seconds.")
        try:
            result = float(value)
        except (OverflowError, TypeError, ValueError):
            raise EmbeddingError(f"{name} must be a finite value from 0.1 to 120 seconds.") from None
        if not math.isfinite(result) or not 0.1 <= result <= 120:
            raise EmbeddingError(f"{name} must be a finite value from 0.1 to 120 seconds.")
        return result

    @staticmethod
    def _parse_base_url(base_url: str) -> tuple[str, int]:
        if not isinstance(base_url, str) or not base_url:
            raise EmbeddingError("Embedding base URL must be an explicit loopback HTTP origin.")
        try:
            parsed = urlsplit(base_url)
            address = ip_address(parsed.hostname or "")
            port = parsed.port
        except ValueError:
            raise EmbeddingError("Embedding base URL must use a literal loopback IP and explicit port.") from None
        if (
            parsed.scheme != "http" or not address.is_loopback or port is None
            or parsed.username is not None or parsed.password is not None
            or parsed.path not in ("", "/") or parsed.query or parsed.fragment
        ):
            raise EmbeddingError("Embedding base URL must be a plain HTTP loopback origin with an explicit port.")
        return parsed.hostname, port

    def embed(self, text: str):
        """Legacy Provider entry point; unqualified text is treated as a document."""
        return self.embed_document(text)

    def embed_query(self, text: str):
        return self._embed(text, role="query")

    def embed_document(self, text: str):
        return self._embed(text, role="document")

    def health(self, *, timeout: float = 2.0) -> str:
        """Check the model-specific llama.cpp health contract without inference."""
        timeout = self._validate_timeout(timeout, "health_timeout")
        connection = HTTPConnection(self._host, self._port, timeout=timeout)
        try:
            connection.request("GET", "/health", headers={"Accept": "application/json"})
            response = connection.getresponse()
            raw = response.read(64 * 1024 + 1)
            if len(raw) > 64 * 1024:
                raise EmbeddingError("Embedding health response exceeds the configured size limit.")
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise EmbeddingError("Embedding health response is not valid JSON.") from None
            if response.status == 200 and isinstance(payload, dict) and payload.get("status") == "ok":
                return "ready"
            message = payload.get("error", {}).get("message") if isinstance(payload, dict) else None
            if response.status == 503 and message == "Loading model":
                return "loading"
            raise EmbeddingError(f"Embedding health contract rejected HTTP {response.status}.")
        except socket.timeout:
            raise EmbeddingError("Embedding health request timed out.") from None
        except EmbeddingError:
            raise
        except (OSError, HTTPException) as exc:
            raise EmbeddingError(f"Embedding health endpoint is unavailable ({type(exc).__name__}).") from None
        finally:
            connection.close()

    def wait_ready(self, *, timeout: float = 120.0, poll_interval: float = 1.0) -> float:
        """Bounded model-readiness polling; it never sends an embedding request."""
        timeout = self._validate_timeout(timeout, "readiness_timeout")
        poll_interval = self._validate_timeout(poll_interval, "poll_interval")
        started = time.monotonic()
        deadline = started + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining < 0.1:
                raise EmbeddingError("Embedding model readiness timed out.")
            if self.health(timeout=min(2.0, remaining)) == "ready":
                return round(time.monotonic() - started, 6)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise EmbeddingError("Embedding model readiness timed out.")
            time.sleep(min(poll_interval, remaining))

    def _embed(self, text: str, *, role: str):
        if not isinstance(text, str) or not text.strip():
            raise EmbeddingError("Text to embed must not be empty.")
        input_text = text
        if role == "query":
            input_text = f"Instruct: {self.query_instruction}\nQuery: {text.strip()}"
        payload = json.dumps(
            {"input": input_text, "model": self.api_model, "encoding_format": "float"},
            ensure_ascii=False,
        ).encode("utf-8")
        connection = HTTPConnection(self._host, self._port, timeout=self.connect_timeout)
        self.request_attempts += 1
        sent = False
        succeeded = False
        try:
            # http.client connects directly to this parsed IP; it does not use environment proxies.
            connection.request(
                "POST", "/v1/embeddings", body=payload,
                headers={"Content-Type": "application/json", "Accept": "application/json"},
            )
            self.requests_sent += 1
            sent = True
            if connection.sock is not None:
                connection.sock.settimeout(self.read_timeout)
            response = connection.getresponse()
            if 300 <= response.status < 400:
                raise EmbeddingError("Local embedding server returned a redirect; redirects are not followed.")
            if response.status != 200:
                raise EmbeddingError(f"Local embedding server returned HTTP {response.status}.")
            content_length = response.getheader("Content-Length")
            if content_length is not None:
                try:
                    declared_length = int(content_length)
                except ValueError:
                    raise EmbeddingError("Local embedding response has an invalid Content-Length.") from None
                if declared_length > MAX_EMBEDDING_RESPONSE_BYTES:
                    raise EmbeddingError("Local embedding response exceeds the configured size limit.")
            raw = response.read(MAX_EMBEDDING_RESPONSE_BYTES + 1)
            if len(raw) > MAX_EMBEDDING_RESPONSE_BYTES:
                raise EmbeddingError("Local embedding response exceeds the configured size limit.")
            try:
                decoded = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise EmbeddingError("Local embedding server returned invalid JSON.") from None
            data = decoded.get("data") if isinstance(decoded, dict) else None
            if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
                raise EmbeddingError("Local embedding server returned an invalid embedding response.")
            vector = data[0].get("embedding")
            if not isinstance(vector, list) or not vector:
                raise EmbeddingError("Local embedding server returned an empty embedding.")
            normalized = normalize_vector(vector, expected_dimension=self.dimension)
            self.request_successes += 1
            succeeded = True
            return normalized
        except socket.timeout:
            raise EmbeddingError("Local embedding request timed out.") from None
        except EmbeddingError:
            raise
        except (OSError, HTTPException) as exc:
            raise EmbeddingError(
                f"Local embedding service is unavailable ({type(exc).__name__})."
            ) from None
        finally:
            if sent and not succeeded:
                self.request_failures += 1
            connection.close()


__all__ = ["DEFAULT_QUERY_INSTRUCTION", "LlamaServerEmbeddingProvider"]
