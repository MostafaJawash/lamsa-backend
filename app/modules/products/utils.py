from typing import Any


def build_options_signature(options: dict[str, Any]) -> str:
    """Canonical "key=value|key=value" string for a variant's options, sorted by key.

    Keys ending in "_hex" (display-only colour codes) are ignored, so two variants that
    differ only in hex value are treated as the same combination.
    """
    parts = [
        f"{key}={str(value).strip()}"
        for key, value in sorted(options.items())
        if not key.endswith("_hex")
    ]
    return "|".join(parts)
