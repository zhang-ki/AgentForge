"""Facade for collect a telemetry frame from an instrument."""

from .adapters.telemetry import collect_frame

__all__ = ["collect_frame"]
