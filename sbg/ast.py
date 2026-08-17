from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List, Optional

from .errors import LexError
from .globals import KEYWORDS, MULTI, SINGLE

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

