"""Synthetic storage helpers for catalog attachments."""

def retain_catalog_attachment(exhibit_id: str, object_key: str) -> dict:
    """Keep a digitized object key linked to its catalog entry."""
    return {"exhibit_id": exhibit_id, "object_key": object_key}

def read_catalog_attachment(exhibit_id: str) -> str:
    """Return the stored object key for a catalog entry."""
    return f"catalog/{exhibit_id}/attachment"
