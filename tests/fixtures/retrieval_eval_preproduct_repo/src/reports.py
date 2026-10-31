"""Shift report composition."""

from .audit import write_audit_entry
from .sensors import record_temperature


def build_shift_report(container_id: str, celsius: float, operator: str) -> dict:
    """Combine a telemetry sample and audit entry for the shift handoff."""
    sample = record_temperature(container_id, celsius)
    audit = write_audit_entry(operator, "shift-report", container_id)
    return {"sample": sample, "audit": audit}
