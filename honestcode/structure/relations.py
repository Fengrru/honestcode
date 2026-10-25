"""Relation types and shared data structures for structural code analysis."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

__all__ = ["RelationType", "StructEdge", "ParseResult"]


class RelationType(Enum):
    DEF_CALL = "def_call"
    VAR_DEF_USE = "var_def_use"
    AST_PARENT_CHILD = "ast_parent_child"
    IMPORT = "import"
    IMPORT_REF = "import_ref"
    SAME_SCOPE = "same_scope"
    CROSS_FILE_REF = "cross_file_ref"  # def in file_a -> call in file_b


@dataclass(frozen=True)
class StructEdge:
    src_start: int
    src_end: int
    tgt_start: int
    tgt_end: int
    relation_type: str
    name: str | None = None
    # For cross-file edges: which file the source/target belongs to
    src_file: str | None = None
    tgt_file: str | None = None


@dataclass
class ParseResult:
    edges: list[StructEdge]
    func_defs: dict[str, tuple[int, int]]
    class_defs: dict[str, tuple[int, int]]
    var_defs: dict[str, tuple[int, int]]
    imports: set[str]
    # Cross-file: symbols imported from / exported to other files
    imported_symbols: dict[str, str] | None = None  # name -> source_module
    exported_symbols: set[str] | None = None  # names defined here, usable by others
    # class qname -> syntactic base names (dotted, as written). Recorded at
    # parse time so the symbol index can answer "is any class a subclass of
    # X" without re-parsing every class-defining file per verification.
    class_bases: dict[str, list[str]] | None = None
