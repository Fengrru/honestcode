"""Zero-dependency file watcher for automatic re-indexing.

Polls the source tree's ``.py`` file mtimes on a background thread and fires
an ``on_change`` callback once files have stayed unchanged for ``debounce``
seconds. A poll-based design keeps the project dependency-free (pure stdlib),
cross-platform, and good enough for local dev machines — a single project of a
few thousand files is fully rescanned in well under a second.

Usage::

    watcher = ProjectWatcher(root, on_change=lambda changed: ...)
    watcher.start()
    ...
    watcher.stop()
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from honestcode.structure.utils import iter_python_files

logger = logging.getLogger(__name__)


def _take_snapshot(root: Path) -> dict[Path, float]:
    """Map each .py file to its mtime (ns), for change detection."""
    snap: dict[Path, float] = {}
    for p in iter_python_files(root):
        try:
            snap[p] = p.stat().st_mtime_ns
        except OSError:
            continue
    return snap


class ProjectWatcher:
    """Watch a project for source changes and fire a debounced callback.

    ``on_change(changed: list[Path])`` is invoked on the watcher thread once
    the tree is quiescent for ``debounce`` seconds. The callback must be quick
    or move heavy work to its own thread.
    """

    def __init__(
        self,
        root: Path,
        on_change,
        debounce: float = 2.0,
        poll_interval: float = 0.5,
        name: str = "honestcode-watcher",
    ) -> None:
        self.root = Path(root)
        self.on_change = on_change
        self.debounce = debounce
        self.poll_interval = poll_interval
        self._snapshot = _take_snapshot(self.root)
        self._pending: list[Path] = []
        self._last_change: float | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> ProjectWatcher:
        self._thread.start()
        logger.info("Watcher started for %s (debounce %.1fs)", self.root, self.debounce)
        return self

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)
        if self._thread.is_alive():
            logger.warning("Watcher thread for %s did not stop within 5s", self.root)
        else:
            logger.info("Watcher stopped for %s", self.root)

    # -- internals ---------------------------------------------------------

    def _run(self) -> None:
        while not self._stop.is_set():
            self._stop.wait(self.poll_interval)
            self._scan_once()

    def _scan_once(self) -> None:
        now = _take_snapshot(self.root)
        changed: list[Path] = []
        old = self._snapshot
        for p in old:
            if p not in now:
                changed.append(p)
        for p, mtime in now.items():
            if old.get(p) != mtime:
                changed.append(p)
        self._snapshot = now

        if changed:
            self._pending.extend(changed)
            self._last_change = time.monotonic()
            logger.debug("Detected %d changed files", len(changed))
            return

        if (
            self._pending
            and self._last_change is not None
            and (time.monotonic() - self._last_change) >= self.debounce
        ):
            pending, self._pending = self._pending, []
            self._last_change = None
            try:
                self.on_change(pending)
            except Exception:  # noqa: BLE001
                logger.exception("on_change callback failed")
