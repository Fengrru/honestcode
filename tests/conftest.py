"""Shared test setup.

Every test gets private index/graph cache directories. Without this, each
temporary project created by the suite wrote an index JSON plus a SQLite graph
database into ``~/.cache/honestcode`` and nothing ever removed them; a single
suite run left thousands of files in the developer's home directory.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolated_caches(tmp_path, monkeypatch):
    monkeypatch.setenv("HONESTCODE_CACHE_DIR", str(tmp_path / ".honestcode-cache"))
    monkeypatch.setenv("HONESTCODE_INDEX_DIR", str(tmp_path / ".honestcode-index"))
