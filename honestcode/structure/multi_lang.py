"""Optional tree-sitter multi-language symbol extraction.

Python remains the fully supported language (stdlib ``ast``). For JavaScript,
TypeScript and a few other popular languages, symbol extraction is available
when the optional extras are installed::

    pip install -e "honestcode[multi-language]"

Everything degrades gracefully: without tree-sitter, ``is_supported`` still
recognizes the file by extension, but ``extract_symbols`` raises
``TreeSitterUnavailable`` so callers can report a clear message instead of a
traceback.
"""

from __future__ import annotations

import importlib
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

LANG_BY_EXT: dict[str, str] = {
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
}

# node type -> symbol kind, per language (tree-sitter grammar node names).
_SYMBOL_RULES: dict[str, list[tuple[str, str]]] = {
    "javascript": [
        ("function_declaration", "function"),
        ("class_declaration", "class"),
        ("method_definition", "method"),
        ("function_expression", "function"),
    ],
    "typescript": [
        ("function_declaration", "function"),
        ("class_declaration", "class"),
        ("method_definition", "method"),
        ("function_expression", "function"),
        ("interface_declaration", "interface"),
        ("type_alias_declaration", "type"),
        ("enum_declaration", "enum"),
    ],
    "tsx": [
        ("function_declaration", "function"),
        ("class_declaration", "class"),
        ("method_definition", "method"),
        ("function_expression", "function"),
        ("interface_declaration", "interface"),
        ("type_alias_declaration", "type"),
        ("enum_declaration", "enum"),
    ],
    "go": [
        ("function_declaration", "function"),
        ("method_declaration", "method"),
        ("type_spec", "type"),
    ],
    "rust": [
        ("function_item", "function"),
        ("struct_item", "struct"),
        ("enum_item", "enum"),
        ("trait_item", "trait"),
    ],
    "java": [
        ("class_declaration", "class"),
        ("interface_declaration", "interface"),
        ("enum_declaration", "enum"),
        ("method_declaration", "method"),
    ],
}

# Some grammars name their declaration nodes with a ``type_identifier``
# instead of an ``identifier`` (e.g. TypeScript type aliases, Go type specs);
# the query patterns below use the correct node type per language.
# Grammars disagree about the node type used for a declaration's name; a query
# naming the wrong one is rejected outright ("Impossible pattern"), which broke
# every multi-language scan on the 0.25 grammars.
_NAME_NODE_TYPES: dict[str, dict[str, str]] = {
    "javascript": {"method_definition": "property_identifier"},
    "typescript": {
        "class_declaration": "type_identifier",
        "interface_declaration": "type_identifier",
        "type_alias_declaration": "type_identifier",
        "method_definition": "property_identifier",
    },
    "tsx": {
        "class_declaration": "type_identifier",
        "interface_declaration": "type_identifier",
        "type_alias_declaration": "type_identifier",
        "method_definition": "property_identifier",
    },
    "go": {
        "method_declaration": "field_identifier",
        "type_spec": "type_identifier",
    },
    "rust": {
        "struct_item": "type_identifier",
        "enum_item": "type_identifier",
        "trait_item": "type_identifier",
    },
}

# tree-sitter-typescript ships two grammars in one module and exposes them as
# named factories rather than a generic ``language()``.
_LANGUAGE_FACTORIES: dict[str, tuple[str, ...]] = {
    "typescript": ("language_typescript", "language"),
    "tsx": ("language_tsx", "language"),
}

_QUERIES: dict[str, object] = {}


class TreeSitterUnavailable(RuntimeError):
    """Raised when tree-sitter (or a language grammar) is not installed."""


def detect_language(path: str | Path) -> str | None:
    """Return the language name for *path* by extension, or None."""
    return LANG_BY_EXT.get(Path(path).suffix.lower())


def is_supported(path: str | Path) -> bool:
    return detect_language(path) is not None


def _import_tree_sitter():  # pragma: no cover - requires optional extras
    try:
        from tree_sitter import Language, Parser  # noqa: F401

        return Language, Parser
    except ImportError as exc:
        raise TreeSitterUnavailable(
            "tree-sitter is not installed. Run: pip install -e '.[multi-language]'"
        ) from exc


def _load_language(name: str) -> object:  # pragma: no cover - requires optional extras
    """Load a language grammar, supporting tree-sitter >= 0.21 binding APIs."""
    module_name = (
        "tree_sitter_typescript" if name in ("typescript", "tsx") else f"tree_sitter_{name}"
    )
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise TreeSitterUnavailable(
            f"Grammar for '{name}' not installed (tree-sitter-{name} package)"
        ) from exc

    factory = None
    for attr in _LANGUAGE_FACTORIES.get(name, ("language",)):
        factory = getattr(module, attr, None)
        if factory is not None:
            break
    if factory is None:  # old-style / unknown bindings
        raise TreeSitterUnavailable(
            f"Unsupported tree-sitter binding for '{name}' (no language factory)"
        )
    lang = factory()
    # tree-sitter >= 0.22 returns a Language instance; 0.21 returns a handle.
    try:
        from tree_sitter import Language

        if not isinstance(lang, Language):
            lang = Language(lang)
    except Exception:  # noqa: BLE001
        pass
    return lang


def _query_for(name: str, language) -> object:  # pragma: no cover - optional extras
    if name in _QUERIES:
        return _QUERIES[name]
    name_node_overrides = _NAME_NODE_TYPES.get(name, {})
    parts = [
        f"({node} name: ({name_node_overrides.get(node, 'identifier')}) @name.{kind})"
        for node, kind in _SYMBOL_RULES[name]
    ]
    source = " ".join(parts)
    # ``Language.query(source)`` is the instance API on 0.21-0.25; calling it
    # on the class (as this did) passes the string as ``self`` and fails on
    # 0.25 with "descriptor 'query' ... doesn't apply to a 'str' object".
    # 0.25 deprecates it in favour of the Query constructor, so prefer that
    # form where the installed binding accepts it.
    query = None
    try:
        from tree_sitter import Query
    except ImportError:
        Query = None  # type: ignore[assignment,misc]
    if Query is not None:
        try:
            query = Query(language, source)
        except (TypeError, ValueError):
            query = None
    if query is None:
        query = language.query(source)
    _QUERIES[name] = query
    return query


def _captures(query, node) -> list[tuple[object, str]]:
    """Return ``(node, capture-name)`` pairs across tree-sitter versions.

    0.21-0.24 expose ``Query.captures(node)`` returning a list of tuples; 0.25
    removed that method in favour of ``QueryCursor(query).captures(node)``,
    which returns ``{capture_name: [node, ...]}``.
    """
    if hasattr(query, "captures"):
        raw = query.captures(node)
        return [(item[0], str(item[1])) for item in raw]
    try:
        from tree_sitter import QueryCursor
    except ImportError as exc:  # pragma: no cover - impossible on supported versions
        raise TreeSitterUnavailable("tree-sitter QueryCursor API unavailable") from exc
    raw = QueryCursor(query).captures(node)
    pairs: list[tuple[object, str]] = []
    for tag, items in raw.items():
        for item in items:
            pairs.append((item[0] if isinstance(item, tuple) else item, str(tag)))
    return pairs


def extract_symbols(path: str | Path) -> list[dict]:  # pragma: no cover - requires optional extras
    """Extract ``{name, line, kind}`` symbols from a non-Python source file.

    Raises ``TreeSitterUnavailable`` when the optional dependency is missing.
    """
    path = Path(path)
    lang = detect_language(path)
    if lang is None:
        raise ValueError(f"Unsupported language for {path}")

    Language, Parser = _import_tree_sitter()
    grammar = _load_language(lang)
    parser = Parser()
    try:
        parser.language = grammar
    except AttributeError:  # tree-sitter 0.21
        parser.set_language(grammar)
    source = path.read_bytes()

    tree = parser.parse(source)
    query = _query_for(lang, grammar)
    captures = _captures(query, tree.root_node)

    symbols: list[dict] = []
    for node, tag in captures:
        if not tag.startswith("name."):
            continue
        name = node.text.decode("utf-8", errors="replace")
        if not name:
            continue
        symbols.append(
            {
                "name": name,
                "line": node.start_point[0] + 1,
                "kind": tag.split(".", 1)[1],
            }
        )
    return symbols
