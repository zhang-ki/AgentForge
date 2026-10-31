"""Synthetic gallery operations and visitor communications."""

from src.notices import send_closure_notice

def close_gallery_room(room_code: str, reason: str) -> dict:
    """Disable room bookings and notify affected visitors."""
    disable_room_bookings(room_code)
    return send_closure_notice(room_code, reason)
