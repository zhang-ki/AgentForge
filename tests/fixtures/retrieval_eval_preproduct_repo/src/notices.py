"""Operational notice formatting."""


def send_notice(recipient: str, subject: str, detail: str) -> dict:
    """Return a queued operational notice record."""
    return {"recipient": recipient, "subject": subject, "detail": detail, "queued": True}
