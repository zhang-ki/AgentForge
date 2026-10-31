"""Manifest validation rules."""


class ManifestRules:
    """Required fields for refrigerated cargo manifests."""

    required = ("container_id", "commodity", "target_celsius")

    def check(self, manifest: dict) -> dict:
        missing = [name for name in self.required if name not in manifest]
        if missing:
            raise ValueError("missing manifest fields")
        return dict(manifest)


def validate_manifest(manifest: dict) -> dict:
    """Return a checked copy without importing or executing customer code."""
    return ManifestRules().check(manifest)
