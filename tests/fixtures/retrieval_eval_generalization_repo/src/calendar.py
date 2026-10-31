"""Appointment slot operations for gallery visits."""

def book_slot(exhibit_id: str, slot: str) -> dict:
    """Assign an exhibit to one appointment slot."""
    if not slot:
        raise ValueError("slot is required")
    return {"exhibit": exhibit_id, "slot": slot, "state": "booked"}

def count_bookings(day: str, *, state: str) -> int:
    """Count calendar entries for one day and booking state."""
    return 0
