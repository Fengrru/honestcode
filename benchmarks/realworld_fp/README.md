# Real-world false-positive benchmark

HonestCode's synthetic accuracy tasks (see `../agent_accuracy/`) pin *recall*:
they prove known hallucinations are caught. This benchmark pins *precision* on
**real-world code**: it scans a vendored snapshot of a real project and asserts
the verifier stays completely silent.

Real code exercises constructs the tiny synthetic tasks never do:

- standard-library imports (`from typing import TypeVar`, `from logging import NullHandler`)
- keyword-argument call style (`f(a=1, b=2)`, `build_report(file=..., findings=...)`)
- `@staticmethod` members (`RequestEncodingMixin._encode_params(data)`)
- classes inheriting unresolvable stdlib ABCs (`MutableMapping`, `CookieJar`)
- compat re-export modules (`from .compat import urlparse`)

Every false-positive class found during the v0.4.0 real-world evaluation is
represented here, so a regression in any of them fails CI.

## Run

```bash
python benchmarks/realworld_fp/run.py
python benchmarks/realworld_fp/run.py --format json
python benchmarks/realworld_fp/run.py --project /path/to/another/project
```

Exit code is `0` when the corpus is clean and `2` when any finding is
reported — a finding on this corpus is, by construction, a false positive.

## The vendored project

`vendor/` contains the `src/requests` package of [psf/requests](
https://github.com/psf/requests) (19 files, ~6.4k LOC), a mature, widely used
library that exercises all of the constructs above.

- Upstream commit: see `vendor/.upstream-commit`
- License: Apache-2.0 — the upstream `LICENSE` is included at `vendor/LICENSE`
- Contents: `src/requests/**` copied verbatim, plus a `requirements.txt`
  restating the project's declared runtime dependencies (used for declared-root
  import trust; the dependencies themselves are not imported)

The snapshot is intentionally not updated automatically: results must stay
deterministic. To refresh it, re-copy the directory at a newer commit, update
`.upstream-commit`, and re-run the benchmark.
