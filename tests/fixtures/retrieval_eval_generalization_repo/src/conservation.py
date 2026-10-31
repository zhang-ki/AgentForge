"""Synthetic conservation workflows for gallery objects."""

def hold(object_id: str) -> str:
    """Place an artwork in the conservation team's work queue."""
    return f"conservation:{object_id}"

def set_gallery_humidity(room_id: str, target_rh: float) -> float:
    """Set a validated relative-humidity target for one gallery room."""
    if not 20.0 <= target_rh <= 70.0:
        raise ValueError("humidity target outside supported range")
    return float(target_rh)
