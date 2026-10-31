"""Facade for route a cargo manifest to a berth queue."""

from .engine import dispatch_manifest

__all__ = ["dispatch_manifest"]
