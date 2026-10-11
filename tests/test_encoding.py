"""Every text file in the repository must be valid UTF-8.

A sibling repository lost 46 files to a code-page round trip that replaced the
final byte of 3-byte UTF-8 sequences with ``?`` and consumed the following
space, so ``e2 80 94`` (em dash) became ``e2 80 3f``. Nothing noticed: the
files still parsed, the tests still passed, Ruff still passed, and the damage
showed only in a browser or a strict reader.

That is the kind of failure a test has to own, because no other check will.
The marker is checked explicitly as well as the decode, so a file cannot pass by
containing bytes that happen to decode while carrying an obviously truncated
sequence.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

SKIP_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "dist",
    "build",
    ".coverage",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
}

TEXT_SUFFIXES = {
    ".md",
    ".py",
    ".toml",
    ".json",
    ".yml",
    ".yaml",
    ".txt",
    ".cfg",
    ".ini",
    ".ts",
    ".js",
    ".html",
    ".css",
}

# The damaged shape: a lead byte, a continuation byte, then `?` where the final
# continuation byte belongs.
TRUNCATED = (b"\xe2\x80?", b"\xe2\x86?", b"\xe2\x89?")


def _text_files() -> list[Path]:
    found = []
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.suffix.lower() in TEXT_SUFFIXES or path.name in {"LICENSE", "Makefile"}:
            found.append(path)
    return sorted(found)


def test_the_scan_finds_files():
    assert len(_text_files()) > 50, "the walk is broken, not the repository"


def test_every_text_file_is_valid_utf8():
    broken = []
    for path in _text_files():
        try:
            path.read_bytes().decode("utf-8")
        except UnicodeDecodeError as exc:
            broken.append(f"{path.relative_to(ROOT)} at byte {exc.start}")
    assert broken == [], f"files are not valid UTF-8: {broken}"


def test_no_file_carries_a_truncated_multibyte_sequence():
    damaged = []
    for path in _text_files():
        raw = path.read_bytes()
        for marker in TRUNCATED:
            if marker in raw:
                damaged.append(str(path.relative_to(ROOT)))
                break
    assert damaged == [], f"truncated multi-byte sequences in: {damaged}"


@pytest.mark.parametrize("name", [".editorconfig", "pyproject.toml"])
def test_the_encoding_policy_is_declared(name):
    """Prevention rather than detection.

    A repository that declares its line endings but not its encoding leaves the
    door open on a workstation where the default for a non-ASCII file is not
    UTF-8. `.editorconfig` is what tells an editor otherwise.
    """
    path = ROOT / name
    assert path.exists(), f"{name} is missing"
    text = path.read_text(encoding="utf-8")
    if name == ".editorconfig":
        assert "charset = utf-8" in text, ".editorconfig does not declare an encoding"
