"""Public care package surface."""

from .public import assess_patient as screen_patient
from .records import VisitRecord

class CareVersion:
    version = "s2-v1"


__all__ = ["screen_patient", "VisitRecord", "CareVersion"]
