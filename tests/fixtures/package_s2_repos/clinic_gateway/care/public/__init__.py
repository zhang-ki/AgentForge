"""Facade for assign a patient to a triage lane."""

from ..triage.engine import assess_patient

__all__ = ["assess_patient"]
