"""Storage fee calculations and payment holds."""


def calculate_storage_fee(hours: int, hourly_rate: float) -> float:
    """Calculate billable cold-room storage with no network lookup."""
    billable_hours = max(0, int(hours) - 2)
    return round(billable_hours * hourly_rate, 2)


def hold(identifier: str) -> dict:
    """Place a billing authorization hold, not a dock or inventory hold."""
    return {"authorization_id": identifier, "payment_hold": True}
