"""Temperature telemetry records."""


class SensorMonitor:
    """Record temperature samples for one container."""

    def __init__(self, container_id: str):
        self.container_id = container_id

    def record_temperature(self, celsius: float) -> dict:
        return {"container_id": self.container_id, "celsius": float(celsius)}


def record_temperature(container_id: str, celsius: float) -> dict:
    """Convenience entry point for a single telemetry sample."""
    return SensorMonitor(container_id).record_temperature(celsius)
