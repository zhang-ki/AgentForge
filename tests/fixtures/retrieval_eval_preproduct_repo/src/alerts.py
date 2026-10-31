"""Temperature excursion escalation."""

from .notices import send_notice


def raise_excursion_alert(container_id: str, measured: float, maximum: float) -> dict:
    """Notify operations when a recorded temperature exceeds its ceiling."""
    detail = f"{container_id}: {measured} exceeds {maximum}"
    return send_notice("cold-chain-operations", "temperature excursion", detail)
