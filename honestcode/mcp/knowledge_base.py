"""
Knowledge base of dependency APIs for HonestCode.

Loads API signatures from installed packages via ``pydoc`` / AST inspection
and stores them for fast lookup during hallucination checks.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
import re
import threading
from dataclasses import dataclass

logger = logging.getLogger(__name__)

__all__ = ["APISignature", "APIKnowledgeBase", "resolve_import_names"]

# Fallback for environments where importlib.metadata has no answer. Maps
# PyPI distribution name -> import name for the common offenders whose PyPI
# and import names differ; without this, ``pip install PyYAML`` would never
# load ``yaml`` APIs and every ``from yaml import safe_load`` would be
# reported as an undefined symbol.
_FALLBACK_DIST_TO_IMPORT: dict[str, str] = {
    "pillow": "PIL",
    "scikit-learn": "sklearn",
    "scikit-image": "skimage",
    "beautifulsoup4": "bs4",
    "python-dateutil": "dateutil",
    "opencv-python": "cv2",
    "pyyaml": "yaml",
    "pycryptodome": "Crypto",
    "python-dotenv": "dotenv",
    "gitpython": "git",
    "pyserial": "serial",
    "msgpack-python": "msgpack",
}

_import_to_distributions: dict[str, list[str]] | None = None


def _normalise_dist(name: str) -> str:
    """Normalize a distribution name per PEP 503 (``PyYAML`` -> ``pyyaml``)."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _import_distribution_map() -> dict[str, list[str]]:
    global _import_to_distributions
    if _import_to_distributions is None:
        try:
            from importlib.metadata import packages_distributions

            _import_to_distributions = packages_distributions()
        except Exception as e:  # noqa: BLE001
            logger.debug("packages_distributions unavailable: %s", e)
            _import_to_distributions = {}
    return _import_to_distributions


def resolve_import_names(package_name: str) -> set[str]:
    """Return import-name candidates for a PyPI distribution name.

    ``load_package("PyYAML")`` must import ``yaml``, not ``PyYAML``. Candidates
    come from :func:`importlib.metadata.packages_distributions` (import name ->
    installed distributions, reversed here), with a static fallback table for
    the well-known mismatches.
    """
    candidates = {package_name, package_name.replace("-", "_")}
    norm = _normalise_dist(package_name)
    for import_name, dists in _import_distribution_map().items():
        if any(_normalise_dist(d) == norm for d in dists):
            candidates.add(import_name)
    for dist, import_name in _FALLBACK_DIST_TO_IMPORT.items():
        if _normalise_dist(dist) == norm:
            candidates.add(import_name)
    return candidates


@dataclass
class APISignature:
    name: str
    signature: str = ""
    doc: str = ""
    module: str = ""


class APIKnowledgeBase:
    """Lightweight cache of public API signatures for known packages."""

    def __init__(self):
        self._lock = threading.RLock()
        self.apis: dict[str, APISignature] = {}
        self._loaded_packages: set[str] = set()
        # Import-name roots seen so far — including packages that were
        # *declared* (in requirements.txt / pyproject.toml) but failed to
        # import. Imports from declared roots stay trusted even when the
        # package is not installed, so missing installs never turn into
        # false "undefined symbol" findings.
        self._roots: set[str] = set()
        # Module names whose member surface was actually enumerated (the
        # package root and the submodules that imported successfully). Only
        # these can be used to decide that ``mod.attr`` is *missing* — a
        # declared-but-uninstalled package, or a submodule we never walked
        # into, knows nothing about its members, so member checks must stay
        # silent for them rather than assume an empty surface.
        self._enumerated: set[str] = set()

    def declare_package(self, package_name: str) -> None:
        """Register a declared dependency without importing it.

        Imports from a declared package are trusted (the package is part of
        the project's contract), but no member surface exists, so invented-API
        checks stay silent for it — "not in the index" is not evidence of
        absence when the package was never imported.
        """
        import_names = resolve_import_names(package_name)
        with self._lock:
            self._roots.update(n.split(".")[0] for n in import_names)

    def load_package(self, package_name: str) -> int:
        """Extract public API signatures from an installed package.

        *package_name* may be a PyPI distribution name (``PyYAML``) or an
        import name (``yaml``); both resolve to the same module.

        Imports happen outside the lock to avoid blocking concurrent readers,
        but the shared cache is updated under the lock.
        """
        with self._lock:
            if package_name in self._loaded_packages:
                return sum(1 for v in self.apis.values() if v.module.startswith(package_name))

        import_names = resolve_import_names(package_name)
        with self._lock:
            self._loaded_packages.add(package_name)
            self._roots.update(n.split(".")[0] for n in import_names)

        mod = None
        last_error: Exception | None = None

        def _candidate_rank(name: str) -> tuple:
            # Order: the exact requested import name first (authoritative),
            # then its underscore spelling, then other public candidates
            # (e.g. "yaml" for "PyYAML"), private C-extension modules last
            # (importing "_yaml" would key the whole API base under "_yaml"
            # and make every "yaml.*" call look invented).
            return (
                name != package_name,
                name != package_name.replace("-", "_"),
                name.startswith("_"),
                name,
            )

        for candidate in sorted(import_names, key=_candidate_rank):
            try:
                mod = importlib.import_module(candidate)
                break
            except Exception as e:  # noqa: BLE001
                last_error = e
        if mod is None:
            logger.warning(
                "Cannot import %s (tried %s): %s",
                package_name,
                sorted(import_names),
                last_error,
            )
            with self._lock:
                # Allow a retry once the package is installed, but keep the
                # declared root so imports from it are still trusted.
                self._loaded_packages.discard(package_name)
            return 0

        import_root = mod.__name__.split(".")[0]
        new_apis: dict[str, APISignature] = {}
        seen: set[str] = set()
        enumerated: set[str] = {mod.__name__}
        count = self._collect_from_module(mod, mod.__name__, mod.__name__, new_apis, seen)

        # Recurse into submodules (one level) to enrich the base.
        try:
            paths = getattr(mod, "__path__", None)
            if paths is not None:
                for info in pkgutil.iter_modules(paths):
                    if info.name.startswith("_"):
                        continue
                    sub = f"{import_root}.{info.name}"
                    try:
                        submod = importlib.import_module(sub)
                    except Exception as e:  # noqa: BLE001
                        logger.debug("Cannot import submodule %s: %s", sub, e)
                        continue
                    enumerated.add(sub)
                    count += self._collect_from_module(submod, sub, sub, new_apis, seen)
        except Exception as e:  # noqa: BLE001
            logger.warning("Failed to enumerate submodules of %s: %s", package_name, e)

        with self._lock:
            self.apis.update(new_apis)
            self._loaded_packages.add(import_root)
            self._enumerated.update(enumerated)

        logger.info("Loaded %d APIs from %s", count, package_name)
        return count

    def _collect_from_module(
        self,
        mod,
        module_name: str,
        api_module: str,
        out: dict[str, APISignature],
        seen: set[str],
    ) -> int:
        """Collect public attributes from a module into *out*.

        Returns the number of newly added signatures.
        """
        count = 0
        try:
            names = dir(mod)
        except Exception as e:  # noqa: BLE001
            logger.warning("dir(%s) failed: %s", module_name, e)
            return 0

        for name in names:
            if name.startswith("_"):
                continue
            full = f"{api_module}.{name}"
            if full in seen:
                continue
            seen.add(full)
            try:
                obj = getattr(mod, name)
                sig = self._signature(obj)
            except Exception:  # noqa: BLE001
                sig = ""
            out[full] = APISignature(name=full, signature=sig, module=api_module)
            count += 1
        return count

    @staticmethod
    def _signature(obj) -> str:
        try:
            import inspect

            return str(inspect.signature(obj))
        except (TypeError, ValueError):
            return ""

    def get(self, name: str) -> APISignature | None:
        with self._lock:
            return self.apis.get(name)

    def has(self, name: str) -> bool:
        with self._lock:
            return name in self.apis

    def search(self, prefix: str, limit: int = 10) -> list[str]:
        with self._lock:
            return [k for k in self.apis if k.startswith(prefix)][:limit]

    def all_names(self) -> set[str]:
        with self._lock:
            return set(self.apis.keys())

    def roots(self) -> set[str]:
        """Import-name roots this KB knows about (loaded or declared)."""
        with self._lock:
            return set(self._roots)

    def enumerated_modules(self) -> set[str]:
        """Module names whose members were actually enumerated.

        Member checks (``mod.attr`` does not exist) are only sound for these
        modules; everything else must stay silent — see the field docstring.
        """
        with self._lock:
            return set(self._enumerated)

    def reset(self) -> None:
        """Drop every loaded API and root (used by tests and benchmarks)."""
        with self._lock:
            self.apis.clear()
            self._loaded_packages.clear()
            self._roots.clear()
            self._enumerated.clear()
