"""Regression corpus: absolute imports inside a monorepo sub-project.

Auto-discovery indexes from the repository root (the ``.git`` marker), so
``pkg.foo`` only exists in the index as ``services.web.pkg.foo``. The
verifier must re-point the import at the file's own package instead of
reporting ``Thing`` as an undefined symbol.
"""

from pkg.foo import Thing


def main():
    t = Thing()
    return t.go()
