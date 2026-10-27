"""Keep customer-facing backend and seed text free of Levantine dialect."""

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEXT_ROOTS = (ROOT / "app", ROOT / "scripts")

MARKERS = (
    "رح", "هلق", "هلأ", "هلّق", "بدك", "بدي", "شو", "هيك", "منيح", "كتير",
    "ما في", "مش", "ليش", "وين", "هون", "هاد", "هدول", "هيدا", "لسا", "لسّا",
    "عم", "تبع", "شوي", "مشان", "منشان", "إلك", "إلو", "هلقد", "كمان", "بس",
    "ناطر", "ناطرة", "فاضي", "فاضية", "بوكس", "فيك", "اللي", "تانية", "خلص",
    "مطفي",
)

# "مطفي" is the established cosmetics option value and must not be migrated.
ALLOWLIST = {"مطفي"}
ARABIC_LETTER_OR_MARK = r"ء-غف-يً-ٰٟ"
PATTERN = re.compile(
    rf"(?<![{ARABIC_LETTER_OR_MARK}])"
    rf"({'|'.join(re.escape(marker) for marker in MARKERS if marker not in ALLOWLIST)})"
    rf"(?![{ARABIC_LETTER_OR_MARK}])"
)


def test_backend_arabic_uses_modern_standard_arabic() -> None:
    failures: list[str] = []
    for root in TEXT_ROOTS:
        for path in sorted(root.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if match := PATTERN.search(line):
                    failures.append(f"{path.relative_to(ROOT)}:{line_number}: {match.group(0)}")
    assert not failures, "Levantine markers found:\n" + "\n".join(failures)
