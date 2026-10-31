"""Synthetic token cache operations."""


def lookup(key):
    """Look up a token record by its key."""
    return {"token": key}


def save_token_choice(choice):
    """Save a selected token policy."""
    return {"token_policy": choice}
