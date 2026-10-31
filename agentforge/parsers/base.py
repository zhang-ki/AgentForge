"""Parser-neutral data contracts for structured source indexing."""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class ParsedSymbol:
    symbol_id: str
    source_id: str
    kind: str
    name: str
    qualified_name: str
    start_line: int
    end_line: int
    parent_symbol_id: str | None
    signature: str
    docstring: str | None
    structure_hash: str


@dataclass(frozen=True)
class ParsedReference:
    reference_id: str
    source_id: str
    source_symbol_id: str
    line: int
    reference_type: str
    target_name: str
    module_name: str | None = None
    imported_name: str | None = None
    local_name: str | None = None
    import_level: int = 0
    target_qualified_name: str | None = None
    resolution_status: str = "unresolved"
    target_symbol_id: str | None = None
    candidate_symbol_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class ParsedFile:
    relative_path: str
    source_id: str
    parser_name: str
    status: str
    error: str | None
    module_name: str | None
    package_name: str | None
    is_package_init: bool
    symbols: tuple[ParsedSymbol, ...]
    references: tuple[ParsedReference, ...]
    scope_shadowed_names: tuple[tuple[str, tuple[str, ...]], ...] = ()


class Parser(Protocol):
    """A parser consumes already-approved text and never opens source paths."""

    parser_name: str

    def parse(self, relative_path: str, source_text: str) -> ParsedFile:
        """Return symbols/references for one repository-relative text file."""
