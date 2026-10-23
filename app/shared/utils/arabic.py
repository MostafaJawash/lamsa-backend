"""Arabic text normalization for search.

MySQL's FULLTEXT index does not treat Arabic letter variants as equal, so both the stored
products.search_text and every search query go through normalize_arabic().
"""

import re

# Harakat (fathatan .. sukun, maddah/hamza marks), superscript alef.
_TASHKEEL = re.compile("[ً-ٰٟ]")
_TATWEEL = "ـ"
_TRANSLATION = str.maketrans(
    {
        "أ": "ا",  # أ -> ا
        "إ": "ا",  # إ -> ا
        "آ": "ا",  # آ -> ا
        "ٱ": "ا",  # ٱ -> ا
        "ى": "ي",  # ى -> ي
        "ة": "ه",  # ة -> ه
        **{chr(0x0660 + i): str(i) for i in range(10)},  # Arabic-Indic digits
        **{chr(0x06F0 + i): str(i) for i in range(10)},  # Extended (Persian) digits
    }
)
_SPACES = re.compile(r"\s+")


def normalize_arabic(text: str | None) -> str:
    """Remove tashkeel and tatweel, unify alef/yeh/teh-marbuta forms, ASCII digits, lowercase."""
    if not text:
        return ""
    text = _TASHKEEL.sub("", text).replace(_TATWEEL, "")
    text = text.translate(_TRANSLATION).lower()
    return _SPACES.sub(" ", text).strip()
