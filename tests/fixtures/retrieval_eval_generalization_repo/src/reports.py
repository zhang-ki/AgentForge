"""Synthetic reporting helpers for gallery appointments."""

from src.calendar import count_bookings

def daily_visit_summary(day: str) -> dict:
    """Aggregate booked and cancelled visits for one calendar day."""
    booked = count_bookings(day, state="booked")
    cancelled = count_bookings(day, state="cancelled")
    return {"booked": booked, "cancelled": cancelled}

def monthly_exhibit_totals(month: str) -> dict:
    """Return a placeholder summary for a month of exhibit visits."""
    return {"month": month, "total": 0}
