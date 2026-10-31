def dispatch(alert_id: str) -> str:
    """Send a monitoring alert, not a parcel."""
    return f"beacon:{alert_id}"
