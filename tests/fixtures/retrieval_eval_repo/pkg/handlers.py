"""Synthetic request handlers."""

from .session import rotate_session_key


def handle_expired_session(key):
    """Handle a stale session key at the request boundary."""
    return rotate_session_key(key)
