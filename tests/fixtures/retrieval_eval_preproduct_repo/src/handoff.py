"""Custody handoff workflow."""

from .audit import write_audit_entry


def handoff_container(container_id: str, from_team: str, to_team: str) -> dict:
    """Record transfer of custody between two operating teams."""
    event = write_audit_entry(from_team, "handoff", container_id)
    return {"container_id": container_id, "from": from_team, "to": to_team, "audit": event}
