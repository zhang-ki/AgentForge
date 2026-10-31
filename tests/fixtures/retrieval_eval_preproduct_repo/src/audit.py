"""Append-only audit record construction."""


def write_audit_entry(actor: str, action: str, object_id: str) -> dict:
    """Construct an immutable audit event for the caller to persist."""
    return {"actor": actor, "action": action, "object_id": object_id}
