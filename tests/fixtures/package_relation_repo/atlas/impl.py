def dispatch(parcel_id: str) -> str:
    """Route a parcel to the Atlas delivery queue."""
    return f"atlas:{parcel_id}"
