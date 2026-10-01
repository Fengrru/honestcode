"""Class member surfaces — the ground truth behind ``invented_api``.

To claim "``UserClient.refresh_token()`` does not exist" you must first know
*every* member ``UserClient`` actually has. This module builds that surface
from the AST, and — critically — records when it **cannot**:

* ``dynamic``    the class defines ``__getattr__`` / ``__getattribute__``, so
                 any attribute lookup may succeed at runtime.
* incomplete     a base class could not be resolved, so inherited members are
                 unknown.

Both cases force the verifier to stay quiet (or downgrade its confidence)
instead of guessing. A verification layer that cries wolf is worse than no
verification layer at all.
"""

from __future__ import annotations

import ast
import os
from collections import OrderedDict
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

from honestcode.structure.utils import call_name

if TYPE_CHECKING:
    from collections.abc import Iterator

__all__ = [
    "MethodInfo",
    "ClassSurface",
    "FunctionInfo",
    "extract_file_surfaces",
    "imported_symbols_of",
    "load_file_facts",
]

# Bases whose member set is empty and safe to treat as fully resolved.
EMPTY_BASES = frozenset({"object"})

# Decorators that do not change the callable's runtime signature.
ARITY_SAFE_DECORATORS = frozenset(
    {
        "staticmethod",
        "classmethod",
        "property",
        "cached_property",
        "functools.cached_property",
        "functools.wraps",
        "wraps",
        "override",
        "typing.override",
    }
)


@dataclass(frozen=True)
class MethodInfo:
    """Callable signature facts needed for an argument-count check."""

    name: str
    min_args: int  # required args a caller must pass
    max_args: int | None  # None = unbounded (*args present)
    keywords: frozenset[str] = frozenset()
    has_varargs: bool = False
    has_kwargs: bool = False
    decorators: frozenset[str] = frozenset()
    is_property: bool = False
    # Required parameter names, so an arity check can count parameters passed
    # by keyword — ``f(a, b)`` called as ``f(a=1, b=2)`` is valid Python, and
    # keyword-call style dominates modern code.
    required_positional: tuple[str, ...] = ()
    required_keywords: frozenset[str] = frozenset()

    @property
    def decorated(self) -> bool:
        return bool(self.decorators)

    @property
    def arity_trustworthy(self) -> bool:
        """True when no decorator could have rewritten the signature."""
        return all(d.split(".")[-1] in ARITY_SAFE_DECORATORS for d in self.decorators)

    def describe(self) -> str:
        if self.max_args is None:
            return f"at least {self.min_args} argument(s)"
        if self.min_args == self.max_args:
            return f"exactly {self.min_args} argument(s)"
        return f"{self.min_args}-{self.max_args} argument(s)"


@dataclass(frozen=True)
class FunctionInfo:
    """A module-level (or nested) function's callable facts."""

    qname: str
    method: MethodInfo


@dataclass
class ClassSurface:
    """Everything statically knowable about a class's public surface."""

    qname: str
    file: str
    line: int
    methods: dict[str, MethodInfo] = field(default_factory=dict)
    attributes: set[str] = field(default_factory=set)
    instance_attrs: dict[str, str] = field(default_factory=dict)
    bases: list[str] = field(default_factory=list)
    dynamic: bool = False
    decorated: bool = False

    @property
    def members(self) -> set[str]:
        return set(self.methods) | self.attributes


def _decorator_name(node: ast.expr) -> str:
    """Best-effort dotted name for a decorator expression."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parts: list[str] = []
        cur: ast.expr = node
        while isinstance(cur, ast.Attribute):
            parts.append(cur.attr)
            cur = cur.value
        if isinstance(cur, ast.Name):
            parts.append(cur.id)
            return ".".join(reversed(parts))
    if isinstance(node, ast.Call):
        return _decorator_name(node.func)
    return ""


def _arity(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    *,
    bound_params: int,
) -> MethodInfo:
    """Compute callable facts for *node*, dropping *bound_params* leading args.

    ``@staticmethod`` methods take no implicit first parameter, so the drop is
    forced to zero for them — treating ``self`` as bound used to shift every
    static method's arity by one and flag valid calls
    (``RequestEncodingMixin._encode_params(data)`` reported "expected 0").
    ``@classmethod`` keeps the drop (``cls`` is bound).
    """
    a = node.args
    names = [_decorator_name(d) for d in node.decorator_list]
    short_decorators = {n.split(".")[-1] for n in names if n}
    if "staticmethod" in short_decorators:
        bound_params = 0

    pos = list(getattr(a, "posonlyargs", []) or []) + list(a.args)
    n_defaults = len(a.defaults)
    required_pos = len(pos) - n_defaults
    kwonly = list(a.kwonlyargs)
    required_kw_names = frozenset(
        arg.arg for arg, dflt in zip(kwonly, a.kw_defaults, strict=False) if dflt is None
    )

    drop = min(bound_params, len(pos))
    after_drop = pos[drop:]
    required_positional = tuple(x.arg for x in after_drop[: max(0, required_pos - drop)])
    min_args = len(required_positional) + len(required_kw_names)
    max_args = None if a.vararg is not None else max(0, len(pos) - drop) + len(kwonly)
    keywords = frozenset([x.arg for x in pos[drop:]] + [x.arg for x in kwonly])

    return MethodInfo(
        name=node.name,
        min_args=min_args,
        max_args=max_args,
        keywords=keywords,
        has_varargs=a.vararg is not None,
        has_kwargs=a.kwarg is not None,
        decorators=frozenset(n for n in names if n),
        is_property=any(n.split(".")[-1] in ("property", "cached_property") for n in names),
        required_positional=required_positional,
        required_keywords=required_kw_names,
    )


def annotation_name(node: ast.expr) -> str | None:
    """Return a dotted name for an annotation (handles string annotations)."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parts: list[str] = []
        cur: ast.expr = node
        while isinstance(cur, ast.Attribute):
            parts.append(cur.attr)
            cur = cur.value
        if isinstance(cur, ast.Name):
            parts.append(cur.id)
            return ".".join(reversed(parts))
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        # ``from __future__ import annotations`` stores annotations verbatim.
        text = node.value.strip()
        if text.replace(".", "").replace("_", "").isidentifier():
            return text
    if isinstance(node, ast.Subscript):  # Optional[UserClient], list[UserClient], ...
        return annotation_name(node.value)
    return None


def _iter_scope(node: ast.AST, prefix: str) -> Iterator[tuple[ast.AST, str]]:
    """Yield (node, dotted-name-prefix) for every class scope under *node*."""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.ClassDef):
            name = f"{prefix}.{child.name}" if prefix else child.name
            yield child, name
            yield from _iter_scope(child, name)
        elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # Functions can contain locally-defined classes; keep walking.
            yield from _iter_scope(child, prefix)


def _attribute_root(node: ast.Attribute) -> str | None:
    """Return the leftmost name of an attribute chain."""
    current: ast.expr = node.value
    while isinstance(current, ast.Attribute):
        current = current.value
    return current.id if isinstance(current, ast.Name) else None


def _collect_instance_attrs(node: ast.ClassDef) -> dict[str, str]:
    """Collect ``self.x`` / ``cls.x`` assignments and their inferred types."""
    attrs: dict[str, str] = {}
    for method in ast.walk(node):
        if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for stmt in method.body:
            if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Attribute):
                root = _attribute_root(stmt.target)
                if root in ("self", "cls"):
                    name = annotation_name(stmt.annotation) if stmt.annotation else None
                    if name is None and isinstance(stmt.value, ast.Call):
                        name = call_name(stmt.value.func)
                    if name:
                        attrs[stmt.target.attr] = name
            elif isinstance(stmt, ast.Assign):
                for target in stmt.targets:
                    if isinstance(target, ast.Attribute):
                        root = _attribute_root(target)
                        if root in ("self", "cls") and isinstance(stmt.value, ast.Call):
                            name = call_name(stmt.value.func)
                            if name:
                                attrs[target.attr] = name
    return attrs


def extract_file_surfaces(
    tree: ast.AST,
    *,
    module: str,
    file_label: str,
) -> tuple[dict[str, ClassSurface], dict[str, MethodInfo]]:
    """Extract every class surface and module-level function from *tree*.

    Returns ``(classes, functions)`` keyed by module-qualified name. Nested
    classes get dotted names (``mod.Outer.Inner``), matching how the symbol
    index names them.
    """
    classes: dict[str, ClassSurface] = {}
    functions: dict[str, MethodInfo] = {}

    def qualified(local: str) -> str:
        return f"{module}.{local}" if module else local

    for node, prefix in _iter_scope(tree, ""):
        if not isinstance(node, ast.ClassDef):
            continue
        qname = qualified(prefix)
        surface = ClassSurface(
            qname=qname,
            file=file_label,
            line=node.lineno,
            bases=[b for b in (annotation_name(x) for x in node.bases) if b],
            decorated=bool(node.decorator_list),
            instance_attrs=_collect_instance_attrs(node),
        )
        for item in node.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                info = _arity(item, bound_params=1)
                surface.methods[item.name] = info
                if item.name in ("__getattr__", "__getattribute__"):
                    surface.dynamic = True
            elif isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                surface.attributes.add(item.target.id)
            elif isinstance(item, ast.Assign):
                for target in item.targets:
                    if isinstance(target, ast.Name):
                        surface.attributes.add(target.id)
        classes[qname] = surface

    # Module-level functions (used for arity checks on imported callables).
    for child in ast.iter_child_nodes(tree):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions[qualified(child.name)] = _arity(child, bound_params=0)

    return classes, functions


def file_fingerprint(path: Path) -> tuple[int, int]:
    """Cheap cache key for a parsed file (mtime + size)."""
    try:
        st = os.stat(path)
    except OSError:
        return (0, 0)
    return (int(st.st_mtime_ns), int(st.st_size))


def imported_symbols_of(tree: ast.AST) -> dict[str, str]:
    """Map every imported local name in *tree* to its source.

    Mirrors :class:`~honestcode.structure.extractor.StructureExtractor`'s
    convention (``import a.b`` binds ``b`` -> ``a.b``; ``from a import name``
    binds ``name`` -> ``a.name``). Local imports inside functions count too.
    """
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                asname = alias.asname or alias.name.split(".")[-1]
                out[asname] = alias.name
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for alias in node.names:
                asname = alias.asname or alias.name
                out[asname] = f"{module}.{alias.name}" if module else alias.name
    return out


# Parsed file facts shared across verifications: definition files are read
# once per (path, mtime, size) even though a single verify walks dozens of
# classes, and long-lived MCP sessions reuse everything across calls.
# Ordering matters: this used to happen once per class-base lookup, which
# parsed the same multi-thousand-line file 40 times in one verification.
_MAX_CACHED_FILES = 512
_FILE_FACTS: OrderedDict[
    tuple[str, int, int],
    tuple[dict[str, ClassSurface], dict[str, MethodInfo], dict[str, str]],
] = OrderedDict()


@lru_cache(maxsize=4096)
def _read_source(path_str: str) -> str | None:
    """Cached source read (bounded); ``None`` when unreadable."""
    try:
        return Path(path_str).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def load_file_facts(
    path: Path,
    module: str,
    file_label: str,
) -> tuple[dict[str, ClassSurface], dict[str, MethodInfo], dict[str, str]]:
    """Parse *path* once and return ``(classes, functions, imported_symbols)``.

    Results are cached by ``(path, mtime_ns, size)`` across calls; unreadable
    or unparsable files cache an empty result so repeated scans do not retry.
    """
    key = (str(path), *file_fingerprint(path))
    cached = _FILE_FACTS.get(key)
    if cached is not None:
        _FILE_FACTS.move_to_end(key)
        return cached

    source = _read_source(str(path))
    facts: tuple[dict[str, ClassSurface], dict[str, MethodInfo], dict[str, str]]
    if source is None:
        facts = ({}, {}, {})
    else:
        try:
            tree = ast.parse(source)
        except (SyntaxError, ValueError):
            facts = ({}, {}, {})
        else:
            classes, functions = extract_file_surfaces(tree, module=module, file_label=file_label)
            facts = (classes, functions, imported_symbols_of(tree))

    _FILE_FACTS[key] = facts
    if len(_FILE_FACTS) > _MAX_CACHED_FILES:
        _FILE_FACTS.popitem(last=False)
    return facts
