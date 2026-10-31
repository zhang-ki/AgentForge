"""Synthetic session cache helpers."""


def refresh_access_token(session_id):
    """Return a deterministic placeholder for a session."""
    return f"offline-{session_id}"


def cache_lookup(key, cache):
    """Read an existing cache entry without modifying the cache."""
    return cache.get(key)

