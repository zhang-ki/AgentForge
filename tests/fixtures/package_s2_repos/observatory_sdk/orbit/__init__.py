"""Public orbit package surface."""

from .sdk import collect_frame as sample_frame
from .models import FramePlan

class OrbitVersion:
    version = "s2-v1"


__all__ = ["sample_frame", "FramePlan", "OrbitVersion"]
