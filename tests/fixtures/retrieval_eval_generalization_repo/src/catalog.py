"""Synthetic catalog archival operations."""

def archive_exhibit(exhibit_id: str, reason: str) -> dict:
    """Mark an exhibit record as archived while retaining its attachments."""
    record = load_record(exhibit_id)
    record["archived"] = True
    record["archive_reason"] = reason
    return record

def load_record(exhibit_id: str) -> dict:
    """Return a minimal synthetic catalog record."""
    return {"exhibit_id": exhibit_id, "archived": False}
