import re
import secrets

from slugify import slugify

SLUG_MIN_LENGTH = 2
SLUG_MAX_LENGTH = 120
SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def is_valid_slug(value: str) -> bool:
    return SLUG_MIN_LENGTH <= len(value) <= SLUG_MAX_LENGTH and bool(SLUG_PATTERN.match(value))


def slug_from_name(name: str, fallback_prefix: str) -> str:
    """Slug from the name, or "<prefix>-<random>" when transliteration leaves nothing usable."""
    # Leave room for a "-NN" suffix added on conflicts.
    base = slugify(name, max_length=SLUG_MAX_LENGTH - 6, word_boundary=True, lowercase=True)
    if len(base) < SLUG_MIN_LENGTH:
        base = f"{fallback_prefix}-{secrets.token_hex(3)}"
    return base


def with_suffix(base: str, n: int) -> str:
    """base, base-2, base-3, ... kept within the max length."""
    if n <= 1:
        return base
    suffix = f"-{n}"
    return base[: SLUG_MAX_LENGTH - len(suffix)] + suffix
