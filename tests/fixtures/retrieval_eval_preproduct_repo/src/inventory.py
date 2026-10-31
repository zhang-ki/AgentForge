"""Cold-room lot allocation."""


def allocate_lot(container_id: str, zone: str) -> dict:
    """Place a container lot in a named cold-room zone."""
    return {"container_id": container_id, "zone": zone, "allocated": True}


def hold(identifier: str) -> dict:
    """Quarantine an inventory lot pending quality review."""
    return {"lot_id": identifier, "quarantined": True}
