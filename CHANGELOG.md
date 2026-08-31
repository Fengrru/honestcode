# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
