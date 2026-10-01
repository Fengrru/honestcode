# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.4.0] - 2026-10-01

Real-world precision and performance release. A scan of `psf/requests` used to
produce 125 findings — nearly all false positives — and took up to 31 seconds
per large file. It now produces **zero findings** and verifies the whole corpus
in seconds; a vendored real-world benchmark (`benchmarks/realworld_fp/`) locks
the behaviour into CI. Detection recall is unchanged (agent-accuracy
benchmark: precision 1.0, recall 1.0).

### Fixed — false positives on real-world code

- **Standard-library imports are trusted — and checked exactly.** `from typing
  import TypeVar`, `from logging import NullHandler`, `from base64 import
  b64encode` were reported as undefined symbols because the stdlib was never a
  known dependency root. Stdlib sources are now verified against the live
  module, so valid names pass and invented ones (`from typing import TypeVarr`)
  are still rejected.
- **Keyword arguments satisfy required parameters.** `f(a, b)` called as
  `f(a=1, b=2)` was reported as "too few arguments: got 0". Arity checks now
  count parameters passed by keyword (including keyword-only parameters), so
  the keyword-heavy call style of modern Python — and of this project's own
  code — is no longer flagged.
- **`@staticmethod` members use the correct arity.** Static methods were
  treated as bound methods, shifting their arity by one and flagging valid
  calls like `RequestEncodingMixin._encode_params(data)`.
- **Classes with unresolvable bases stay silent.** A base class outside the
  project (stdlib ABCs such as `MutableMapping`, third-party parents) may
  legitimately provide any member, so absence cannot be proven. The previous
  "report with confidence: high" policy produced misleading findings on real
  code (`CaseInsensitiveDict.items()`, `did_you_mean: lower_items`); the
  verifier now follows its own silence-beats-noise rule.
- **Re-exports through project modules are grounded.** `from .compat import
  urlparse` — where `compat.py` itself imports the name — was rejected as an
  undefined symbol. The index now records each module's top-level bound names
  (definitions plus imported aliases), and such imports resolve.
- **Marker-less projects are still grounded.** A scratch directory with no
  `.git`/`pyproject.toml` was not auto-indexed, so `verify_file` silently
  returned `pass` for broken code; it now falls back to the file's own
  directory.

### Fixed — performance

- **Extraction is linear.** Edge collection walked the full subtree of every
  function and class, making extraction quadratic (~0.7s for a 3000-line file,
  313k AST visits). Each scope is now walked exactly once.
- **Files are parsed once, not 40 times.** The surface store re-parsed the
  same file for every base-class lookup within a single verification. Parsed
  facts (classes, functions, imports) are now cached by `(path, mtime, size)`
  and shared across verifications in the process — `requests/models.py`
  dropped from 31.2s to ~1.2s, the whole corpus to ~280ms/file.

### Added

- **`benchmarks/realworld_fp/`** — scans a vendored snapshot of `psf/requests`
  (Apache-2.0, commit pinned in `vendor/.upstream-commit`) and fails CI on any
  finding. Every false-positive class above is represented in that corpus.
- `load_project_deps(..., import_packages=False)` registers declared
  dependencies without importing them (used by the benchmark and useful for
  hermetic analysis).
- `tests/test_verify.py` now pins the stricter contract: near-miss typos on
  complete surfaces are still reported; on incomplete surfaces the verifier
  stays silent.

### Changed

- `confidence: high` findings are no longer emitted for classes with
  unresolved bases — the verifier stays silent instead (see above).
- The agent-accuracy `high_confidence_typo_with_unresolved_base` expectation
  was replaced by `near_miss_typo_on_complete_surface_is_reported` to match.

## [0.3.2] - 2026-10-01

Fixes two regressions found during real-world evaluation against
`psf/requests`; each is locked down by a regression test.

### Fixed

- **Nested explicit project roots lost to marker discovery** — a directory
  indexed explicitly with `index_project` (e.g. `demos/invented-api`) that
  lives inside a larger project with a `.git` marker was verified against the
  *enclosing* project's index, so its local module names stopped resolving and
  findings were silently dropped. The nearest explicitly indexed root now
  wins over upward marker discovery. (`demos/invented-api` reported
  "verification passed" for the invented `UserClient.refresh_token()` call.)
- **Declared-but-uninstalled dependencies were treated as known-empty
  modules** — a package listed in `requirements.txt`/`pyproject.toml` that
  could not be imported (`tomli` on Python ≥ 3.11) has no enumerated member
  surface, but member calls on it were still checked against an empty API set
  and reported as invented (`tomllib.loads() does not exist`). Member checks
  now require the module to be in the KB's enumerated set; the same gate also
  removes false positives on deep submodules and module-attribute receivers
  (`urllib3.contrib.pyopenssl.*`, `charset_normalizer.__version__.*`) that the
  one-level loader never walked into.

## [0.3.1] - 2026-10-01

Correctness release: five real-world false-positive / correctness bugs in the
verification loop are fixed, each locked down by a regression test and an
agent-accuracy benchmark task.

### Fixed

- **Stale-index false positives** — the in-process symbol index never
  invalidated, so a symbol written after the first scan (the typical
  agent flow: write module A, then module B importing it) was reported as
  `undefined_symbol` with `deterministic` confidence. `get_project_index` now
  validates its cache against each file's `(mtime_ns, size)` on every access
  and rebuilds incrementally (unchanged files reuse their parsed symbols).
  The file watcher's auto re-index relied on the same broken cache and was a
  no-op; it now picks up changes.
- **Dependency submodule aliases** — `from numpy import random; random.seed()`
  was flagged as `invented_api` (`numpy.seed() does not exist`) because the
  import mapping was collapsed to its root module. The check now runs against
  the full mapped source (`numpy.random.seed`).
- **PyPI name vs import name** — `load_package("PyYAML")` tried to
  `import PyYAML` and failed, so every `from yaml import safe_load` became a
  false `undefined_symbol`. Distribution names now resolve through
  `importlib.metadata` (plus a fallback table: Pillow→PIL,
  scikit-learn→sklearn, …), private C-extension modules (`_yaml`) are tried
  last, and a declared-but-uninstalled dependency keeps its imports trusted
  instead of producing findings.
- **`find_dead_code` reported live entry points as dead** — definitions are
  keyed by qualified name while references are keyed as written in source, so
  the two key spaces almost never intersect (1119 false deaths on the
  project's own codebase). References are now additionally recorded under
  their import-resolved qualified names, and aliveness matching falls back to
  the trailing name component.
- **`search_code` dropped matches when narrowing via FTS5** — token-prefix
  candidate selection missed regex matches inside longer tokens
  (`erifica` found nothing although `verification` did). The FTS table now
  uses the trigram tokenizer and narrows only when a required literal
  substring can be conservatively derived from the pattern; otherwise it
  falls back to a full scan.
- **Syntax errors bypassed the evidence protocol** — `verify_file` returned a
  bare `{"success": false, "error": ...}`; a syntax error is now a structured
  `syntax_error` finding like any other kind.
- **Non-Python scans were pure noise** — every call to a name imported from
  another module was reported as `undefined_call`. The heuristic path now
  recognizes per-language imports, declarations, builtins, and keywords, and
  marks findings `confidence: low`.
- **Symbol index pollution** — function-local variables were indexed as
  project symbols (breaking `check_symbol` semantics and bloating the index);
  only module-level and class-level definitions are indexed now.
- **Windows sandbox did not match its documentation** — the docstring claimed
  Job Object limits that were never implemented. Job Objects (process memory,
  job CPU time, kill-on-close) are now actually applied via ctypes, env
  scrubbing covers database URL/DSN names, and the module documents its real
  threat model (a guardrail against accidents, not a security boundary).

### Changed

- `scan_file` grounds each file against the index of the project the file
  belongs to, so one session scanning two projects no longer grounds one with
  the other's index.
- `HonestRouter.check_call` matches project symbols by trailing name as well
  as by qualified name.
- One shared implementation of module-name inference
  (`honestcode.structure.utils.module_name_for`) replaces four copies.
- `honestcode verify` CLI gains `--version`; ruff's `TCH` rule set is renamed
  to `TC` (requires ruff ≥ 0.6).
- Agent-accuracy benchmark: episodes stage multi-file trees and can reset
  dependency state; two new tasks cover the stale-index and dependency-alias
  regressions (6 tasks total).

## [0.3.0] - 2026-08-31

### Changed

- **Repositioned as a verification layer for AI coding agents** — README,
  pyproject description, and keywords now lead with product value rather than
  "MCP server".

### Added

- **Agent-facing evidence protocol** (`honestcode.verify.evidence`) with
  ``status``, ``kind``, ``owner``, ``evidence``, ``confidence``, and
  ``action`` fields.
- **`invented_api` detection** — resolves a call's receiver to a concrete
  class via annotations, constructors, imports, and `self`, then checks the
  member surface against the repository AST.
- **`verify_file` tool and CLI** — returns structured evidence plus a
  human-readable ``text`` summary, designed for agents to consume directly.
- **Auto-indexing in `scan_file`** — when no project index is loaded, HonestCode
  walks up from the target file and indexes the first project marker it finds
  (`.honestcode/`, `.git/`, `pyproject.toml`, `setup.py`, `requirements.txt`).
- **`scan --format text` / `honestcode verify`** — terminal-readable output for
  demos and CI.
- **Real-world demo** at `demos/invented-api/` reproducing the
  `UserClient.refresh_token()` hallucination and its fix, including a
  self-contained `render_gif.py` that generates the README GIF without
  requiring vhs/ttyd/ffmpeg.
- **Agent-accuracy benchmark** at `benchmarks/agent_accuracy/` with a
  deterministic, LLM-free dataset measuring hallucination detection.
- Import validation: `from myproject import does_not_exist` is now caught as
  an undefined symbol; relative intra-package imports remain supported.

### Changed

- `scan_file` now returns the new evidence shape (`status`, `findings`,
  `summary`) while keeping the legacy `issues` list for backwards compatibility.

## [0.2.0] - 2026-08-03

### Changed

- **Project renamed to HonestCode** — the package, CLI, and environment
  variables were renamed for a distinct brand identity (no longer riding on
  the "CodeGraph" family name):
  - PyPI package `repograph-honest-mcp` → **`honestcode`**
  - Python module `repograph_honest` → **`honestcode`**
  - CLI commands `repograph-honest` / `repograph-honest-mcp` →
    **`honestcode`** / **`honestcode-mcp`**
  - Environment variables `REPOGRAPH_*` → **`HONESTCODE_*`**
  - Project binding directory `.repograph/` → **`.honestcode/`**
  - GitHub repository `Fengrru/repograph-honest-mcp` → **`Fengrru/honestcode`**

### Fixed

- **Pin `mcp>=1.6.0,<2.0`** — `mcp` 2.0 removed `mcp.server.fastmcp`, breaking
  the server import in fresh environments; the constraint keeps the 1.x API
  until the code is adapted.
- **Align pre-commit hooks with ruff** — `ruff-pre-commit` bumped to
  `v0.16.1` (from `v0.11.13`), removing the stale UP038 rule drift that failed
  the lint job.
- **Sandbox: best-effort resource limits** — each `setrlimit` call in the
  POSIX `preexec_fn` is now guarded individually. Some platforms (e.g. macOS
  CI runners) reject certain limits, and a single failure inside
  `preexec_fn` killed the child before it could run (`Sandbox failed:
  Exception occurred in preexec_fn.`), breaking all sandbox tests on macOS.

### Added

- **Persistent call graph (SQLite)** — `graph/graph_store.py` persists the
  project-wide definitions/references graph next to the symbol index, keyed by
  content hashes of every source file. `explore_call_graph` and
  `find_dead_code` now read from disk instead of re-parsing on every call:
  measured **~1 ms** hot (was ~285 ms / ~340 ms). First call after a change
  pays a cold rebuild (~278 ms on this repo).
- **FTS5 full-text search** — `search_code` narrows plain-`*.py` regex scans
  to candidate files via a FTS5 virtual table, keeping results identical
  (superset candidate set) while touching far fewer files on large repos.
- **`explore_impact` tool** — blast radius of a symbol: transitively impacted
  symbols/files via bidirectional call-graph BFS; `explore_call_graph` now
  also returns an `impact` summary.
- **`affected_files` tool** — trace a `git diff` through the call graph to
  find affected files and tests (CI-friendly).
- **File watcher** — zero-dependency polling watcher (`graph/watcher.py`)
  with debounce; `index_project(watch=True)`, CLI `watch` subcommand, and
  `stop_watching` tool keep the index fresh automatically.
- **Project binding & client install** — `honestcode init` /
  `uninit` / `install` / `root`: `.honestcode/` binding directory plus
  auto-configuration of Cursor, VS Code and Claude Code MCP configs.
- **Multi-language support (optional)** — `honestcode[multi-language]`
  extras with tree-sitter symbol extraction for JS/TS/Go/Rust/Java;
  `scan_file` handles non-Python files when installed, and degrades with a
  clear message otherwise.
- **External-repo benchmarks** — `scripts/benchmark.py` gains `--repo`,
  `--repos`, `--format markdown|json`, and cold-vs-hot graph timings.
- **CLI** (`honestcode` command) with a 1:1 subcommand for every MCP
  tool: `index`, `deps`, `scan`, `check-symbol`, `check-api`, `validate`,
  `execute`, `dead-code`, `similar`, `call-graph`, `impact`, `affected`,
  `search`, `load-package`, `stats`, `choose-tool`, `init`, `uninit`,
  `install`, `root`, `watch`. Output is JSON; exit codes are non-zero when
  issues are found so the CLI drops into CI pipelines cleanly.
- **`HONESTCODE_TOOLS` environment variable** now actually controls which
  tools the MCP server exposes. Defaults to `scan_file`; accepts a
  comma-separated list or `all`. Unknown names are ignored with a warning.
- **SSE transport** for the MCP server (`honestcode-mcp --transport
  sse --host 0.0.0.0 --port 8000`), matching the CHANGELOG claim.
- False-positive regression tests: `scan_file` verified to stay silent on
  all builtins (any/all/frozenset/id/hash/ord/chr/pow/...), `self.method()`,
  `from X import Y`, relative imports, `import X as Y`, walrus assignments,
  loop variables, nested functions, dataclasses, and complex call targets.
- `explore_call_graph` callees tests (previously untested and broken).

### Fixed

- **`scan_file` false positives** — the defined-symbol set now includes:
  parameters and `self`/`cls` (so `self.helper()` is not flagged), every
  name introduced by `from X import Y` / `import X as Y` (so relative and
  aliased imports resolve), loop targets and walrus assignments, and the
  **full `builtins` module** instead of a hand-picked 40-name subset.
- **`explore_call_graph` callees** — reference contexts are now recorded as
  module-qualified names (e.g. `pkg.core.main`) instead of short names
  (`main`), so querying `pkg.core.main` actually returns its callees.
  Class method callees (`pkg.core.Worker.work`) also resolve correctly.
- **`check_api` dead branch** — the `unknown module` case had two
  identical return paths; collapsed into one and added an empty
  `suggestion` list for API consistency.
- **`StructureExtractor`** no longer hard-requires `tree-sitter-python` at
  construction time. The parser has always used `ast`; the tree-sitter
  import was a vestigial guard that raised a misleading `ImportError`.
- **MCP `index_project(watch=True)` now works** — the server wrapper
  previously dropped the `watch` flag, so MCP clients could never start the
  background file watcher (the CLI path was unaffected).
- **`affected_files` honors `max_depth`** — the reverse BFS previously ran
  unbounded (the parameter was accepted but never used), traversing the
  whole graph on large repos.
- **Environment variables actually read** — `HONESTCODE_TIMEOUT`,
  `HONESTCODE_MEMORY_MB` and `HONESTCODE_INDEX_DIR` are now honored by the
  sandbox and the symbol-index cache (previously documented but ignored,
  which also made tests write into the real user cache directory).
- **`install --client cursor` writes the right key** — Cursor config now
  uses `mcpServers` (as Cursor requires) instead of the VS Code-style
  `servers` key.
- **Call-graph reference lines corrected** — references recorded from the
  extractor's same-scope edges used the *definition* line as the reference
  line; the duplicated edge pass was removed (the AST walk already covers
  those references with proper context).
- **`index_project` reports true cache status** — the `cached` field now
  reflects whether the index actually came from cache instead of
  `not force_rebuild`.
- **Multi-language scan false positives** — attribute calls
  (`obj.method(`) and constructs like `new Foo(` are no longer reported as
  `undefined_call`.
- **Callee matching is file-aware** — `explore_call_graph` /
  `explore_impact` now require a reference to live in the same file as the
  definition, so short names that collide across modules no longer
  produce phantom callees.
- **Poetry `dict` dependencies parsed** — `tool.poetry.dependencies` in
  `{name: version}` form is now loaded (list form already worked).
- **Sandbox scrubs secrets** — the executed subprocess no longer inherits
  environment variables whose names contain KEY/TOKEN/SECRET/AUTH markers.
- **`GraphCache.is_fresh` connection leak fixed** — the SQLite connection
  is now closed on every error path.
- **`check_call` module matching** — compares against module path
  components instead of substring matching (`"util"` no longer matches
  `"pkg.utility"`).
- **Tree-sitter `type_identifier` nodes** — TS type aliases and Go type
  specs now extract their names (query previously expected `identifier`).
- **CLI `execute --known-names`** — the sandbox's typo-suggestion names
  are now reachable from the command line.
- Dead code removed: `_lazy_router`/`_router` globals, `validate_snippet`,
  `_healthcheck`, the unused `_TOOL_REGISTRY` scaffolding.
- `SandboxExecutor(timeout=0, ...)` no longer silently resets to the
  default limit (`memory_mb or 256` → explicit `None` checks).

### Changed

- Dropped `tree-sitter` and `tree-sitter-python` runtime dependencies —
  the extractor parses with the standard-library `ast` module, so there
  is no native parser to install. `tree-sitter` remains on the roadmap for
  future multi-language support.
- `[project.scripts]` now declares two entry points:
  `honestcode-mcp` (MCP server) and `honestcode` (CLI).
- README architecture diagram and module layout updated to reflect the
  `ast`-based extractor and the new `cli.py` module.

## [0.1.0] - 2026-07-31

### Added

- Initial open-source release of HonestCode MCP Server.
- **Indexing tools**: `index_project`, `load_project_deps`, `load_package_apis`, `get_project_stats`
  - Build module-qualified project symbol indices with content-hash caching.
  - Parse `requirements.txt` and `pyproject.toml` to load dependency API signatures.
- **Verification tools**: `check_symbol`, `check_api`, `validate_types`, `scan_file`
  - Verify identifiers are defined in the project.
  - Verify library API calls exist with typo suggestions via `difflib`.
  - Structural type checks: None iteration, wrong argument counts, non-callable calls.
  - AST-based undefined-call detection across entire files.
- **Analysis tools**: `explore_call_graph`, `find_dead_code`, `find_similar_code`, `search_code`
  - Explore callers and callees of any symbol.
  - Detect unused symbols with entrypoint support and ignore patterns.
  - Find function-level code clones via sequence similarity.
  - Regex search across project source files.
- **Execution**: `execute_code` sandboxed subprocess with timeout and optional POSIX memory limits.
- **Routing**: `choose_tool` natural-language query to tool mapping.
- MCP server with `stdio` and `SSE` transport support.
- Thread-safe global state protected by `RLock`.
- Content-hash based index caching in `~/.cache/honestcode/`.
- SQLite serialization support for project indices.
- 4 example scripts demonstrating library usage.
- CI pipeline on GitHub Actions (3 OS x 3 Python versions).
- Pre-commit hooks with ruff linting and formatting.

### Fixed

- Thread-safety issues in `APIKnowledgeBase` and `mcp/tools.py` global state.
- `explore_call_graph` caller matching bug.
- `check_api` fuzzy-match logic returning inconsistent results.
- `StructureExtractor` incorrectly treating classes as functions.
- Project-index cache invalidation now detects file deletions and content changes.

## [0.0.1] - 2026-07

- Pre-release prototype.
