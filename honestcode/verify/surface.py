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
from typing import TYPE_CHECKING

from honestcode.structure.utils import call_name, read_python_source

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

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
    # Syntactic bases whose name could not be extracted (e.g. ``*bases``):
    # they may provide members, so the surface is not complete.
    unknown_bases: int = 0
    # ``class C(metaclass=Meta)`` — a metaclass may inject arbitrary members.
    has_metaclass: bool = False
    dynamic: bool = False
    decorated: bool = False

    @property
    def members(self) -> set[str]:
        return set(self.methods) | self.attributes | set(self.instance_attrs)


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
    """Collect every ``self.x`` / ``cls.x`` attribute of the class.

    A name is a member whether or not its type can be inferred: the verifier
    may not know ``self.cb``'s type, but it must not claim that ``C.cb`` does
    not exist (``self.cb = cb`` then ``self.cb(1)`` used to be reported as an
    invented API — in this project's own ``graph/watcher.py``, no less).
    Unknown types map to the empty string, which the receiver-chain resolver
    treats as "cannot continue" (silence), never as a claim.
    """
    attrs: dict[str, str] = {}

    def record(name: str, inferred: str | None) -> None:
        if inferred:
            attrs[name] = inferred
        else:
            attrs.setdefault(name, "")

    for method in ast.walk(node):
        if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for stmt in ast.walk(method):
            inferred: str | None = None
            targets: list[ast.expr] = []
            if isinstance(stmt, ast.Assign):
                targets = list(stmt.targets)
                if isinstance(stmt.value, ast.Call):
                    inferred = call_name(stmt.value.func)
            elif isinstance(stmt, ast.AnnAssign):
                targets = [stmt.target]
                if stmt.annotation is not None:
                    inferred = annotation_name(stmt.annotation)
                if inferred is None and isinstance(stmt.value, ast.Call):
                    inferred = call_name(stmt.value.func)
            elif isinstance(stmt, ast.AugAssign):
                targets = [stmt.target]
            elif (
                isinstance(stmt, ast.Call)
                and isinstance(stmt.func, ast.Name)
                and stmt.func.id == "setattr"
                and len(stmt.args) >= 2
                and isinstance(stmt.args[0], ast.Name)
                and stmt.args[0].id in ("self", "cls")
                and isinstance(stmt.args[1], ast.Constant)
                and isinstance(stmt.args[1].value, str)
            ):
                record(stmt.args[1].value, None)
                continue
            for target in targets:
                if isinstance(target, ast.Attribute):
                    root = _attribute_root(target)
                    if root in ("self", "cls"):
                        record(target.attr, inferred)
    return attrs


def _iter_class_body_stmts(stmts: list[ast.stmt]) -> Iterator[ast.stmt]:
    """Yield class-body statements in document order, descending into the
    bodies of conditionals.

    Methods defined under ``if sys.version_info >= ...:`` or a
    ``try/except ImportError:`` compat fallback are real members; walking only
    the class body's direct children missed them and reported valid code as an
    invented API. Nested functions and classes are yielded as-is and not
    descended into — they are separate scopes.
    """
    for stmt in stmts:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            yield stmt
            continue
        if isinstance(
            stmt, (ast.If, ast.Try, ast.With, ast.AsyncWith, ast.For, ast.AsyncFor, ast.While)
        ):
            yield from _iter_class_body_stmts(stmt.body)
            for handler in getattr(stmt, "handlers", ()) or ():
                yield from _iter_class_body_stmts(handler.body)
            yield from _iter_class_body_stmts(getattr(stmt, "orelse", ()) or ())
            yield from _iter_class_body_stmts(getattr(stmt, "finalbody", ()) or ())
            continue
        yield stmt


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
        extracted_bases = [annotation_name(x) for x in node.bases]
        surface = ClassSurface(
            qname=qname,
            file=file_label,
            line=node.lineno,
            bases=[b for b in extracted_bases if b],
            unknown_bases=sum(1 for b in extracted_bases if not b),
            has_metaclass=bool(node.keywords),
            decorated=bool(node.decorator_list),
            instance_attrs=_collect_instance_attrs(node),
        )
        for item in _iter_class_body_stmts(node.body):
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
            elif isinstance(item, (ast.Import, ast.ImportFrom)):
                # A class-body import binds the name in the class namespace
                # (``try: from _json import encode_basestring as encode``).
                for alias in item.names:
                    local = alias.asname or alias.name
                    if local and local != "*":
                        surface.attributes.add(local)
        classes[qname] = surface

    # Module-level statements can add members no class-body scan can see:
    # ``setattr(Cls, "name", ...)``, and plain ``Cls.name = ...`` (the
    # monkey-patch/mixin-registration style used by real projects). Treat both
    # as attributes so an existing member is never claimed absent.
    for stmt in _iter_class_body_stmts(list(getattr(tree, "body", ()))):
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
            call = stmt.value
            if not (isinstance(call.func, ast.Name) and call.func.id == "setattr"):
                continue
            if len(call.args) < 2:
                continue
            target, name_arg = call.args[0], call.args[1]
            if not (
                isinstance(target, ast.Name)
                and isinstance(name_arg, ast.Constant)
                and isinstance(name_arg.value, str)
            ):
                continue
            cls_surface = classes.get(qualified(target.id))
            if cls_surface is not None:
                cls_surface.attributes.add(name_arg.value)
            continue
        targets: list[ast.expr] = []
        if isinstance(stmt, ast.Assign):
            targets = list(stmt.targets)
        elif isinstance(stmt, ast.AnnAssign) and stmt.target is not None:
            targets = [stmt.target]
        for target in targets:
            if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name):
                cls_surface = classes.get(qualified(target.value.id))
                if cls_surface is not None:
                    cls_surface.attributes.add(target.attr)

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
    Star imports get a key prefixed with ``*`` whose value is the source
    module, so several star imports in one file do not overwrite each other
    and the validator can expand them against the symbol index.
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
                if alias.name == "*":
                    out[f"*{module}"] = module
                    continue
                asname = alias.asname or alias.name
                out[asname] = f"{module}.{alias.name}" if module else alias.name
    return out


# Parsed file facts shared across verifications: definition files are read
# once per (path, module, file_label, mtime, size) even though a single verify
# walks dozens of classes, and long-lived MCP sessions reuse everything across
# calls. Ordering matters: this used to happen once per class-base lookup,
# which parsed the same multi-thousand-line file 40 times in one verification.
_MAX_CACHED_FILES = 512
_FILE_FACTS: OrderedDict[
    tuple[str, str, str, int, int],
    tuple[dict[str, ClassSurface], dict[str, MethodInfo], dict[str, str]],
] = OrderedDict()


def _read_source(path_str: str) -> str | None:
    """Read source fresh; ``None`` when unreadable.

    This used to be ``lru_cache``-d hidden behind ``load_file_facts``'s
    fingerprint key, so a file edit produced a *new* facts entry built from the
    *old* cached bytes: a long-lived MCP session kept reporting a method the
    agent had just added to a base class. The facts cache above already avoids
    repeated reads while the fingerprint is unchanged, so the extra layer only
    bought staleness.
    """
    return read_python_source(path_str)


def load_file_facts(
    path: Path,
    module: str,
    file_label: str,
) -> tuple[dict[str, ClassSurface], dict[str, MethodInfo], dict[str, str]]:
    """Parse *path* once and return ``(classes, functions, imported_symbols)``.

    Results are cached by ``(path, module, file_label, mtime_ns, size)`` across
    calls; unreadable or unparsable files cache an empty result so repeated
    scans do not retry. The cache key must include *module*: class qnames are
    module-qualified, so the same file indexed under two different roots (a
    repo that vendors another project, scanned once as a whole and once as its
    own package) produced facts under whichever module was seen first, and the
    other scan silently missed findings.
    """
    key = (str(path), module, file_label, *file_fingerprint(path))
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
