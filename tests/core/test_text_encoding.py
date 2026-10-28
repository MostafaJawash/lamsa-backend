"""Repository text files stay UTF-8 and free of common double-decoding damage."""

from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[2]
TEXT_SUFFIXES = frozenset({".py", ".ts", ".tsx", ".json", ".md", ".sql", ".html", ".css"})
MOJIBAKE_MARKERS = ("\u00c2", "\u00c3", "\u00d8", "\u00d9", "\u00e2", "\u00f0", "\ufffd")


def tracked_text_files() -> list[Path]:
    output = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT)
    return [
        ROOT / raw.decode("utf-8")
        for raw in output.split(b"\0")
        if raw and Path(raw.decode("utf-8")).suffix.lower() in TEXT_SUFFIXES
    ]


def test_tracked_text_is_clean_utf8() -> None:
    failures: list[str] = []
    for path in tracked_text_files():
        relative = path.relative_to(ROOT).as_posix()
        try:
            text = path.read_bytes().decode("utf-8")
        except UnicodeDecodeError as exc:
            failures.append(f"{relative}: invalid UTF-8 at byte {exc.start}")
            continue
        for number, line in enumerate(text.splitlines(), 1):
            markers = [f"U+{ord(marker):04X}" for marker in MOJIBAKE_MARKERS if marker in line]
            if markers:
                failures.append(f"{relative}:{number}: mojibake marker(s) {', '.join(markers)}")
    assert failures == [], "\n" + "\n".join(failures)
