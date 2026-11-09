"""Canonical local repository identity, independent of storage adapters."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path


def canonical_repository(path: str | os.PathLike[str]) -> Path:
    if not isinstance(path, (str, os.PathLike)):
        raise ValueError("Repository path is invalid.")
    try:
        root = Path(path).resolve(strict=True)
    except (OSError, RuntimeError):
        raise ValueError("Repository path does not exist or cannot be resolved.") from None
    if not root.is_dir():
        raise ValueError("Repository path must be a directory.")
    return root


def repository_identity(path: str | os.PathLike[str]) -> str:
    root = canonical_repository(path)
    return hashlib.sha256(os.path.normcase(str(root)).encode("utf-8")).hexdigest()


__all__ = ["canonical_repository", "repository_identity"]
