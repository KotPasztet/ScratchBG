from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from .errors import LexError
from .globals import KEYWORDS, MULTI, SINGLE


def _sbg_preprocess_cpp_surface(text: str) -> str:
    """Tiny compile-time preprocessor for C++-style include ergonomics.

    Supported:
        #include <bits/stdc++.h>  -> import "bits";
        #include <std>            -> import "std";
        #include "lib/foo.sbg"    -> import "lib/foo.sbg";
        using namespace std;      -> ignored

    It is intentionally small; this is not a C++ preprocessor.
    """
    out: List[str] = []
    # Strip a UTF-8 BOM if present: it is not whitespace, so without this the
    # `^\s*#` include regex would miss line 1 and `#include <bits/stdc++.h>`
    # would silently not become `import "bits";` (breaking the iostream gate).
    text = text.lstrip("\ufeff")
    include_re = re.compile(r"^\s*#\s*include\s*([<\"])([^>\"]+)[>\"]\s*$")
    using_re = re.compile(r"^\s*using\s+namespace\s+std\s*;\s*$")
    for line in text.splitlines():
        m = include_re.match(line)
        if m:
            quote, spec = m.group(1), m.group(2).strip()
            if spec == "bits/stdc++.h":
                out.append('import "bits";')
            elif spec in {"std", "std.sbg"}:
                out.append('import "std";')
            elif spec in {"bits", "bits.sbg"}:
                out.append('import "bits";')
            else:
                # Quoted include keeps path semantics.  Angle include becomes package import.
                if quote == '"':
                    out.append(f'import "{spec}";')
                else:
                    pkg = spec[:-4] if spec.endswith('.sbg') else spec
                    out.append(f'import "{pkg}";')
            continue
        if using_re.match(line):
            out.append("// using namespace std; ignored by StageBG")
            continue
        out.append(line)
    return "\n".join(out)


def _sbg_cpp_preprocess_patch20(text: str) -> str:
    # Keep the patch18 include preprocessor. NOTE: this used to also strip
    # "std::" from the raw text here, before tokenization -- which made
    # std:: work even with no `import "std";`/`#include` anywhere in the
    # file, since the parser never even saw the prefix. That has been
    # removed: `std::` now reaches the parser as real `IDENT "::" IDENT`
    # tokens, same as `scratch::`, and is gated by
    # Parser._check_namespace_import (see parser.py).
    text = _sbg_preprocess_cpp_surface(text)
    return text


@dataclass
class Token:
    kind: str
    value: str
    line: int
    col: int
    filename: str = "<source>"

    def __repr__(self) -> str:
        return f"Token({self.kind!r}, {self.value!r}, {self.line}:{self.col})"

class Lexer:
    def __init__(self, text: str, filename: str = "<source>"):
        text = _sbg_cpp_preprocess_patch20(text)
        self.text = text
        self.filename = filename
        self.i = 0
        self.line = 1
        self.col = 1
        self.n = len(text)

    def peek(self, n: int = 0) -> str:
        j = self.i + n
        return self.text[j] if j < self.n else ""

    def advance(self) -> str:
        ch = self.peek()
        self.i += 1
        if ch == "\n":
            self.line += 1
            self.col = 1
        else:
            self.col += 1
        return ch

    def error(self, msg: str) -> LexError:
        return LexError(f"{self.filename}:{self.line}:{self.col}: {msg}")

    def skip_ws_comments(self) -> None:
        while True:
            while self.peek() and self.peek().isspace():
                self.advance()
            if self.peek() == "/" and self.peek(1) == "/":
                while self.peek() and self.peek() != "\n":
                    self.advance()
                continue
            if self.peek() == "/" and self.peek(1) == "*":
                self.advance(); self.advance()
                while True:
                    if not self.peek():
                        raise self.error("unterminated block comment")
                    if self.peek() == "*" and self.peek(1) == "/":
                        self.advance(); self.advance()
                        break
                    self.advance()
                continue
            break

    def lex_number(self) -> Token:
        line, col = self.line, self.col
        s = ""
        while self.peek().isdigit():
            s += self.advance()
        if self.peek() == "." and self.peek(1).isdigit():
            s += self.advance()
            while self.peek().isdigit():
                s += self.advance()
        return Token("NUMBER", s, line, col, self.filename)

    def lex_ident(self) -> Token:
        line, col = self.line, self.col
        s = ""
        while self.peek().isalnum() or self.peek() == "_":
            s += self.advance()
        return Token("KW" if s in KEYWORDS else "IDENT", s, line, col, self.filename)

    def lex_string(self) -> Token:
        quote = self.peek()
        line, col = self.line, self.col
        self.advance()
        out = ""
        while True:
            ch = self.peek()
            if not ch:
                raise self.error("unterminated string")
            if ch == quote:
                self.advance()
                break
            if ch == "\\":
                self.advance()
                esc = self.peek()
                if not esc:
                    raise self.error("unterminated escape")
                self.advance()
                out += {
                    "n": "\n", "t": "\t", "r": "\r", "\\": "\\", "\"": "\"", "'": "'",
                }.get(esc, esc)
            else:
                out += self.advance()
        return Token("STRING", out, line, col, self.filename)

    def tokens(self) -> List[Token]:
        toks: List[Token] = []
        while True:
            self.skip_ws_comments()
            ch = self.peek()
            if not ch:
                toks.append(Token("EOF", "", self.line, self.col, self.filename))
                return toks
            if ch.isdigit():
                toks.append(self.lex_number())
                continue
            if ch.isalpha() or ch == "_":
                toks.append(self.lex_ident())
                continue
            if ch in ('"', "'"):
                toks.append(self.lex_string())
                continue
            two = ch + self.peek(1)
            if two in MULTI:
                line, col = self.line, self.col
                self.advance(); self.advance()
                toks.append(Token("SYM", two, line, col, self.filename))
                continue
            if ch in SINGLE:
                line, col = self.line, self.col
                self.advance()
                toks.append(Token("SYM", ch, line, col, self.filename))
                continue
            raise self.error(f"unexpected character {ch!r}")

# =============================================================================
# AST
# =============================================================================

@dataclass
class Program:
    body: List[Any]

@dataclass
class ImportDecl:
    spec: str

@dataclass
class VarDecl:
    name: str
    expr: Any
    mutable: bool = True
    # Optional C++-style type annotation (e.g. the object-type handle declared
    # by patch19 for `priority_queue pq;`). Purely metadata; ignored by the
    # runtime and by Scratch codegen.
    type: Optional[str] = None

@dataclass
class ListDecl:
    name: str
    items: List[Any]

@dataclass
class EventDecl:
    kind: str  # flag/message
    value: Optional[str]
    body: List[Any]

@dataclass
class ProcDecl:
    name: str
    params: List[str]
    body: List[Any]
    warp: bool = False

@dataclass
class BlockStmt:
    body: List[Any]

@dataclass
class IfStmt:
    cond: Any
    then_body: List[Any]
    else_body: Optional[List[Any]]

@dataclass
class RepeatStmt:
    count: Any
    body: List[Any]

@dataclass
class ForeverStmt:
    body: List[Any]

@dataclass
class WhileStmt:
    cond: Any
    body: List[Any]

@dataclass
class ForStmt:
    init: Optional[Any]
    cond: Optional[Any]
    update: Optional[Any]
    body: List[Any]

@dataclass
class ReturnStmt:
    expr: Optional[Any]

@dataclass
class BreakStmt:
    pass

@dataclass
class ContinueStmt:
    pass

@dataclass
class AssignStmt:
    name: str
    op: str
    expr: Any

@dataclass
class ExprStmt:
    expr: Any

@dataclass
class Literal:
    value: Any

@dataclass
class VarExpr:
    name: str

@dataclass
class BinaryExpr:
    left: Any
    op: str
    right: Any

@dataclass
class UnaryExpr:
    op: str
    expr: Any

@dataclass
class CallExpr:
    callee: str
    args: List[Any]

@dataclass
class ArrayExpr:
    items: List[Any]


@dataclass
class TargetDecl:
    kind: str  # "stage" or "sprite"
    name: str
    body: List[Any]

@dataclass
class LValueAssignStmt:
    op: str
    target: Any
    expr: Any

@dataclass
class StructDecl:
    name: str
    fields: List[Tuple[str, str]]  # (type, field_name)

@dataclass
class StructVarDecl:
    typ: str
    name: str
    init: Any = None

@dataclass
class NestedVectorDecl:
    name: str
    typ: str
    rows: List[Any]


