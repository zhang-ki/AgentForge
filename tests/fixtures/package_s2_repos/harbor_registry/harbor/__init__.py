"""Public harbor package surface."""

from .api import dispatch_manifest as book_manifest
from .models import CargoPlan

class HarborVersion:
    version = "s2-v1"


__all__ = ["book_manifest", "CargoPlan", "HarborVersion"]
