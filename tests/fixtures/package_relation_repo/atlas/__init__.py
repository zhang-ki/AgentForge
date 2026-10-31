"""Public Atlas package surface."""

from .facade import dispatch as send
from .local import LocalPolicy


class PackageInfo:
    version = "fixture-v1"


__all__ = ["send", "LocalPolicy", "PackageInfo"]
