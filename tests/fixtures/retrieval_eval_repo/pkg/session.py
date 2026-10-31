"""Session operations in an artificial application."""


def rotate_session_key(old_key):
    """Replace an expired session key with a fresh synthetic key."""
    return old_key + "-rotated"


def restore_identity_from_lease(lease):
    """Reconstitute an authorized principal from an extant lease."""
    return lease.principal


class SessionCoordinator:
    """Coordinate lease renewal for an existing session."""

    def renew(self, lease):
        """Extend an approved lease while keeping the same principal."""
        return restore_identity_from_lease(lease)
