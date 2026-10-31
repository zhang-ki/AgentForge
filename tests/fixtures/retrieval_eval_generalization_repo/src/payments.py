"""Synthetic visitor deposit operations."""

def charge_deposit(visitor_id: str, exhibit_id: str) -> str:
    """Record a deposit request for a gallery appointment."""
    return f"deposit:{visitor_id}:{exhibit_id}"

def hold(payment_id: str) -> bool:
    """Place a pending visitor payment into review."""
    return bool(payment_id)
