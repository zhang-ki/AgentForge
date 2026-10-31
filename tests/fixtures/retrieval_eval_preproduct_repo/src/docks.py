"""Receiving dock reservations."""


def reserve_dock(dock_code: str, container_id: str) -> dict:
    """Associate one refrigerated container with a receiving dock."""
    return {"dock_code": dock_code, "container_id": container_id, "status": "reserved"}


def hold(identifier: str) -> dict:
    """Temporarily hold a physical dock door for inspection."""
    return {"dock_door": identifier, "held": True}
