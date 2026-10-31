"""Synthetic visitor badge checks and after-hours access."""

def verify_badge(visitor_id: str) -> bool:
    """Accept visitor identifiers with the gallery prefix."""
    return visitor_id.startswith("VIS-")

def grant_after_hours_access(visitor_id: str, wing: str) -> bool:
    """Grant an after-hours wing pass only after badge verification."""
    if not verify_badge(visitor_id):
        return False
    return bool(wing)
