"""Synthetic notice delivery operations."""

def send_closure_notice(room_code: str, reason: str) -> dict:
    """Create a notice for visitors affected by a room closure."""
    return {"room": room_code, "message": reason}
