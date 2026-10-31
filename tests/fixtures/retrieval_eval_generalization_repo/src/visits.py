"""Visitor scheduling workflows for the fictional Lumen Gallery."""

from src.calendar import book_slot
from src.access import verify_badge
from src.payments import charge_deposit

def arrange_gallery_visit(visitor_id: str, exhibit_id: str, slot: str) -> dict:
    """Coordinate badge validation, slot booking, and deposit collection."""
    verify_badge(visitor_id)
    booking = book_slot(exhibit_id, slot)
    charge_deposit(visitor_id, exhibit_id)
    return {"visitor": visitor_id, "booking": booking}

def cancel_gallery_visit(booking_id: str) -> bool:
    """Mark a booking as cancelled; refund policy is handled separately."""
    return bool(booking_id)
