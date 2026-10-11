# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added — `SideEffectGuard.recovered_retry`: the step after a recovery review

`recover()` yields decisions and `resolve()` records what an operator found, but
nothing carried the effect through afterwards. `recovered_retry(effect_id, …)`
closes that gap, and refuses the two cases that must never be papered over:

* a `committed` effect is served from its record rather than re-run;
* an `executing` one raises, because an unresolved outcome is exactly what must
  not be retried blind — it may already have reached the outside world.

`approve_irreversible` defaults to True here, unlike `run`. Deliberate and
narrow: the method requires an `effect_id`, which only comes from a recovery
review, so the approval already happened in the real world.

### Fixed — pydantic-ai extra could break the core package it installs alongside

`pydantic-ai-slim` depends on `mcp` with no upper bound, so a fresh install of
the extra resolved `mcp` to 2.x, where `FastMCP` was renamed — and
`honestcode`'s own MCP server stopped importing. The extra now pins `mcp<2`.

### Fixed — read-only tools were refused, and redirections were unguarded

Two failures in the pydantic-ai integration's classification, both found by
running it rather than by reading it:

* A tool whose name the shared table did not recognise fell to the conservative
  default (irreversible) and was then refused, so a local file read cost an
  approval. `ChargePolicy.read_only_tools` lets an application declare its own
  reads; the generic names are recognised without declaring anything.
* Redirect detection was missing entirely, so `echo x > f` matched the
  read-only table and was treated as a read — a write that was not guarded.

### Added — pydantic-ai integration: durable idempotency for agent tool calls

`honestcode/integrations/pydantic_ai.py` wraps tool execution through the
framework's `wrap_tool_execute` capability hook, so an effect that already
reached the outside world is not carried out a second time — including across a
crash and restart, which is the case the framework cannot cover by itself.
`pydantic-ai` already offers in-process deferral (`CallDeferred`,
`ApprovalRequired`); this is for the different question, *did the outside world
already change and I no longer remember*, which survives the process.

The hook works because returning from `wrap_tool_execute` without calling
`handler()` skips execution entirely and the substitute value becomes the tool
result. Verified against `pydantic-ai` 2.55.0 before anything was built on it.

**Verified against a real model.** A DeepSeek session charged a card, the
process was killed with the record left `executing`, and a fresh process resumed
under the same run id. The vendor ledger shows **one** charge, and the model
replied that it could not confirm the charge so it would not claim success —
which is the behaviour the receipt text exists to produce.

Three things came out of building it that are worth recording:

1. **A guard that silently never ran.** `ToolCallPart.args` is a dict by type
   but a JSON string in practice for every real provider. The first version
   treated that as unkeyable and let the call through — so the guard reported
   nothing and protected nothing, on every real call. String args are now
   parsed, and the branch that still cannot key a call records it in
   `unguarded_calls` rather than passing silently.
2. **Session-scoped keys cannot dedupe a restart**, because a restart is
   precisely a new session id. See the `scope` entry below.
3. **Tool classification has to be declared, not guessed.** The shared
   classifier cannot tell a local read from an unknown external call, and
   defaults what it cannot classify to irreversible — so a `read_file`-shaped
   tool is refused until the application declares it pure. That is the safe
   direction, and it is now pinned by a test rather than left implicit.

### Changed — `scope` is the durability boundary, and it is authoritative

`SideEffectGuard.run` and the new `arun` accept `scope=`, built with the new
`durable_scope(run_id, *parts)` helper. A scoped effect's identity comes from the
scope and the effect's own arguments, and **not** from the session — so the same
scope in a different session resolves to the same record. Without it, a
crash-and-retry is unrecognisable as a retry and the effect happens twice; that
was demonstrated before the fix, not assumed. Passing no scope keeps the
previous session-scoped behaviour.

### Added — `SideEffectGuard.arun`

The async counterpart of `run`, for callers that are already async. The claim is
written *before* the await, so a process that dies while the tool is in flight
records an unknown outcome rather than looking unstarted. Needed because `run`
is synchronous and cannot drive an awaitable on a loop that is already running —
which is how the first version of the integration failed.

### Added — `honestcode.guards`: crash-safe wrappers for agent side effects

An agent that retries a tool call after a crash can charge a card twice, send a
message twice, or deploy twice. Durable-execution engines do not solve this —
Temporal and Restate both guarantee deterministic *replay of orchestration* and
leave exactly-once external effects to the application. This adds the missing
piece: classify the effect, key it, persist the key, refuse to run it again.

Derived from an earlier implementation whose design was sound and whose code
had three defects that are fixed here rather than carried over:

1. **The idempotency key was computed and discarded.** Upstream wrote
   `let _idempotency_key = ...` and then compared raw `request_hash` values in
   memory, so the key died with the process — the exact crash after which the
   agent retries. The key is now derived, persisted, and enforced by a
   `UNIQUE (session_id, idempotency_key)` constraint, so it works across a
   restart. This is the whole point of the module.
2. **Failure was recorded as success.** Upstream set `status = Committed` and
   *then* checked `if !result.success`, so a throwing executor left a committed
   record and the next attempt was served a receipt for work that never
   happened. The order is inverted.
3. **There was no state between "not started" and "done".** An `EXECUTING`
   state is committed *before* the outside world is touched, so a process that
   dies mid-call leaves evidence that the outcome is **unknown** rather than
   evidence that nothing happened — which is what stops the retry.

Also fixed: the precondition check was `precond == "deny"`, a comparison
against one magic string, so every other precondition was silently ignored —
documented protection that did not exist. Preconditions are now callables that
receive the caller's context and must actually pass.

Two deliberate divergences from upstream's tables:

* **Unknown actions default to irreversible, not idempotent.** An action this
  code does not recognise is the one you must not silently retry; the upstream
  default made it retryable.
* **Target hints use word boundaries.** Upstream's `target.contains("push")`
  classified `/srv/pushkin/report.txt` as irreversible.

`honestcode/guards/` is written to be extracted into its own distribution:
stdlib-only, no dependency on HonestCode's finding protocol, and not registered
as an MCP tool (a guard belongs in the caller's own process, not behind a client
configuration file). `tests/test_guards.py` enforces the isolation rule with an
AST import check — and pins that check against a planted violation, because the
first version of it looked for a *string* in the source and so passed on any
file that also imported a sibling.

### Added — value-drift benchmark: does formatting-tolerant matching change the wrong value?

`benchmarks/value_drift/` asks whether an agent's edit can land on a region the
caller never named, when its search string does not pin down a unique location.
It carries a behaviour-preserving port of the eight matching strategies from
`fuzzy-patch` **with and without** its value-literal guard, plus a matcher in
the shape the 2026 ecosystem converged on (similarity floor + uniqueness +
disproportionate-span rejection), over ten fake episodes in which a near-miss
sibling differs only in a value literal.

Findings, stated as measured rather than as claimed:

* The guarded cascade lands on the region the caller named in **10 of 10**
  scenarios; the crude similarity matcher **refuses 9 of 10**. Refusal is how a
  conservative matcher avoids drift, so this is a real difference in
  edit-application reliability.
* The value guard **changes no outcome on this corpus**, and the reason is
  structural: a region the guard accepts necessarily carries exactly the search
  string's literal multiset. So whenever the search string carries the value
  being replaced — as all ten scenarios do — the guard's accept and reject sets
  are both "regions whose values equal the caller's". Its effect is on recall,
  not precision: it refuses a match when the search string's literals match no
  region at all, converting a would-be drift into a safe `no_match`.
* An earlier draft of the corpus made this concrete. Scenarios that quoted a
  nested block without its structural keys were rejected by the guard **on the
  intended region itself**, because the literal counts differed — the guard
  behaving as designed, and the reason the benchmark cannot exhibit a drift case
  for it.

`tests/test_value_drift.py` asserts the corpus invariants, pins the ported
matcher's behaviour, and fails if the guard ever becomes load-bearing — so the
conclusion above has to be rewritten rather than quietly invalidated. Wired into
CI as the `benchmark-value-drift` job, which exits non-zero if the guarded
matcher drifts outside the caller's region.

### Fixed — a project without a marker file reported correct imports as undefined

`scan_file` grounds a file against a fresh index of its own project. When no
marker file (`.git`, `pyproject.toml`, `setup.py`, `requirements.txt`,
`.honestcode/`) exists anywhere above the file, the index was built from the
verified file's **own directory**. Any sibling package was therefore outside the
index, and a correct cross-package import — `from lib.http import Connection` in
`cases/app.py` — was reported as `undefined_symbol`.

That is the worst direction for this tool to be wrong in: the agent, following
the documented workflow, is told to fix an import that is already correct.

The fallback now indexes from the file's **package root** — the first ancestor
without an `__init__.py`, which is what Python itself treats as the import root —
so sibling packages stay in scope. The walk is bounded so a project root that is
itself a package cannot climb into a shared temp or home directory. Detection is
unchanged: an invented method or a name the sibling module does not define is
still reported, because the module is now genuinely indexed rather than trusted
wholesale.

Measured on an 11-case suite built to probe verifier false positives (inherited
members, aliased imports, `__getattr__`, stdlib calls, keyword arguments), the
no-marker path went from precision 0.40 / recall 0.40 to **precision 1.00 /
recall 1.00**.

### Added — invented API detection for `Cls().method()` calls

A method called on a freshly constructed object has no dotted name, so
`_check_call` fell into the "complex target — cannot judge" branch. `UserClient().refresh_token()`
was therefore never checked, even though the receiver type is plain from the
constructor. Since `Cls().method()` is one of the most common Python call styles,
invented APIs went unreported there.

The receiver is now resolved to the class and checked against its member surface
like any other call, which brings the arity check along with it
(`Connection().execute()` with a missing argument is reported as `wrong_call`).
Only the constructor form is handled; a factory-function receiver
(`make_client().refresh()`) stays unjudged, because function signatures carry no
return type in the surface model and guessing would trade a missed finding for a
false one. The README documents the distinction.

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
