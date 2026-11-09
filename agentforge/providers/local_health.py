"""Readiness and identity checks for an explicitly selected loopback model."""

import http.client
import ipaddress
import json
import math
from urllib.parse import urlsplit


def check_qwen_identity(endpoint: str, api_key: str, expected_model: str, *, timeout: float = 3.0) -> dict:
    """Verify the configured local Qwen service without starting or changing it."""
    try:
        parsed = urlsplit(endpoint)
        host = parsed.hostname
        if (parsed.scheme != "http" or not host or "%" in host
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment
                or parsed.path not in ("", "/v1/chat/completions")):
            raise ValueError
        address = ipaddress.ip_address(host)
        if not address.is_loopback:
            raise ValueError
        port = parsed.port if parsed.port is not None else 80
        if not 1 <= port <= 65535:
            raise ValueError
    except (ValueError, TypeError):
        raise ValueError("Endpoint must be an explicit loopback IP over HTTP with the chat path.") from None
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not math.isfinite(float(timeout)) or not 0 < float(timeout) <= 120):
        raise ValueError("Local model identity timeout is invalid.")
    if (not isinstance(api_key, str) or not api_key or api_key != api_key.strip()
            or any(ord(char) < 33 or ord(char) > 126 for char in api_key)):
        raise ValueError("A valid local model API key is required.")
    if not isinstance(expected_model, str) or not expected_model.strip():
        raise ValueError("An expected local model identity is required.")

    connection = http.client.HTTPConnection(str(address), port, timeout=float(timeout))
    try:
        connection.request("GET", "/health", headers={"Accept": "application/json"})
        response = connection.getresponse()
        raw = response.read(8193)
        if response.status != 200 or len(raw) > 8192:
            raise ValueError("Local Qwen health check failed; model selection was not changed.")
        try:
            health = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            raise ValueError("Local Qwen health response was invalid; model selection was not changed.") from None
        if not isinstance(health, dict) or health.get("status") != "ok":
            raise ValueError("Local Qwen model is not ready; model selection was not changed.")

        connection.close()
        connection = http.client.HTTPConnection(str(address), port, timeout=float(timeout))
        connection.request("GET", "/v1/models", headers={
            "Accept": "application/json", "Authorization": "Bearer " + api_key,
        })
        response = connection.getresponse()
        raw = response.read(65_537)
        if response.status != 200 or len(raw) > 65_536:
            raise ValueError("Local Qwen model identity could not be confirmed; model selection was not changed.")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            raise ValueError("Local Qwen model identity response was invalid; model selection was not changed.") from None
        entries = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(entries, list) or not any(
                isinstance(item, dict) and item.get("id") == expected_model for item in entries):
            raise ValueError("The running local service does not expose the expected Qwen model alias.")
        return {"health": "ready", "model_id": expected_model}
    except (OSError, http.client.HTTPException):
        raise ValueError("Local Qwen readiness or model identity request failed; selection was not changed.") from None
    finally:
        connection.close()


__all__ = ["check_qwen_identity"]
