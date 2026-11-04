"""可插拔且不会执行源码的解析器。"""

from .base import ParsedFile, ParsedReference, ParsedSymbol, Parser
from .python_ast import PythonAstParser
from .resolver import parser_for_path, resolve_references

__all__ = [
    "ParsedFile", "ParsedReference", "ParsedSymbol", "Parser",
    "PythonAstParser", "parser_for_path", "resolve_references",
]
