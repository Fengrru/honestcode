"""Regression corpus: names bound by ``with ... as`` and lambda parameters.

Both bindings were missing from the defined-name set, so the bare calls below
were reported as undefined symbols.
"""


class Runner:
    def __enter__(self):
        return self.step

    def step(self):
        return 1

    def __exit__(self, *exc_info):
        return False


def drive():
    with Runner() as step:
        step()
    mapper = lambda convert: convert()  # noqa: E731
    return mapper
