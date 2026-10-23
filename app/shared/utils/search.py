"""Build a safe MySQL boolean-mode FULLTEXT query from user input."""

import re

from app.shared.utils.arabic import normalize_arabic

MIN_TOKEN_LENGTH = 2  # = ngram_token_size; shorter tokens can't match
MAX_TOKENS = 6
# Boolean-mode operators and quotes; replaced by spaces so they can never change the query.
_OPERATORS = re.compile(r'[+\-<>()~*"@\'`\\]')


def search_tokens(raw: str | None) -> list[str]:
    """Normalized, operator-free tokens of at least 2 characters (max 6)."""
    cleaned = _OPERATORS.sub(" ", normalize_arabic(raw))
    tokens = [token for token in cleaned.split() if len(token) >= MIN_TOKEN_LENGTH]
    unique: list[str] = []
    for token in tokens:
        if token not in unique:
            unique.append(token)
    return unique[:MAX_TOKENS]


def build_boolean_query(raw: str | None) -> str | None:
    """'+"tok1" +"tok2"' (every token required, as a phrase), or None if nothing usable remains."""
    tokens = search_tokens(raw)
    if not tokens:
        return None
    return " ".join(f'+"{token}"' for token in tokens)
