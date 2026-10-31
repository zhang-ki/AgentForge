"""Inbound manifest workflow."""

from .docks import reserve_dock
from .validation import validate_manifest


def register_manifest(manifest: dict, dock_code: str) -> dict:
    """Validate an inbound manifest before reserving its receiving dock."""
    checked = validate_manifest(manifest)
    dock = reserve_dock(dock_code, checked["container_id"])
    return {"manifest": checked, "dock": dock}
