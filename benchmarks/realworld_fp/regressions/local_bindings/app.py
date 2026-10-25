"""Regression corpus: local binding forms that must count as defined names.

``except ... as`` names, lambda parameters, comprehension targets and
``global`` declarations are all defined; none of the calls below may be
reported as undefined symbols.
"""

import sys

RESULTS = []


def collect(items):
    total = 0
    try:
        total = sum(len(item) for item in items)
    except TypeError as err:
        print(f"skipped: {err}", file=sys.stderr)
    mapper = lambda convert: convert()  # noqa: E731
    RESULTS.append(total)
    return mapper
