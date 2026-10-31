"""Refrigeration unit maintenance planning."""


class MaintenancePlanner:
    """Create deterministic maintenance work orders."""

    def schedule(self, unit_id: str, service_day: str) -> dict:
        return {"unit_id": unit_id, "service_day": service_day, "status": "scheduled"}


def schedule_maintenance(unit_id: str, service_day: str) -> dict:
    """Schedule service for a refrigeration unit."""
    return MaintenancePlanner().schedule(unit_id, service_day)
