#!/usr/bin/env python3
"""
SBG / StageBG
A professional, text-first language for programming Scratch's Stage/background.

Workflow:
    python3 sbg.py run examples/main.sbg
    python3 sbg.py compile examples/main.sbg compiled/main.sb3
    python3 sbg.py unpack original.sb3 out_dir
    python3 sbg.py inspect original.sb3

Compiles professional text-based SBG code into Scratch 3 projects (.sb3). Supports Stage and invisible empty sprites.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import re
import shutil
import sys
import tempfile
import time
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union

VERSION = "0.9.0-patch24-keyboard"

# =============================================================================
# Errors
# =============================================================================

class SBGError(Exception):
    pass

class LexError(SBGError):
    pass

class ParseError(SBGError):
    pass

class RuntimeSBGError(SBGError):
    pass

class CompileError(SBGError):
    pass

class ImportSBGError(SBGError):
    pass

class PackageError(SBGError):
    pass

def attach_location(exc: Exception, node: Any) -> Exception:
    """Attach source location from an AST node to an SBGError if it has none."""
    if isinstance(exc, SBGError) and getattr(exc, "line", None) is None and node is not None:
        for attr in ("filename", "line", "col"):
            if hasattr(node, attr):
                setattr(exc, attr, getattr(node, attr))
    return exc

def set_location(node: Any, token: Token) -> Any:  # Token is available at runtime after lexer class creation; type hint postponed
    setattr(node, "filename", getattr(token, "filename", None) or "<source>")
    setattr(node, "line", token.line)
    setattr(node, "col", token.col)
    return node

def _parse_location_from_message(message: str) -> Tuple[Optional[str], Optional[int], Optional[int], str]:
    m = re.match(r"^(.*?):(\d+):(\d+):\s*(.*)$", message, flags=re.S)
    if not m:
        return None, None, None, message
    return m.group(1), int(m.group(2)), int(m.group(3)), m.group(4)

def format_diagnostic(error: Exception, *, source_text: str = "", fallback_filename: str = "<source>") -> str:
    """Pretty, compiler-style error output with file/line/column and a caret."""
    raw = str(error)
    filename = getattr(error, "filename", None)
    line = getattr(error, "line", None)
    col = getattr(error, "col", None)
    message = raw
    if line is None or col is None:
        pf, pl, pc, pm = _parse_location_from_message(raw)
        if pl is not None:
            filename = filename or pf
            line, col, message = pl, pc, pm
    filename = filename or fallback_filename
    title = error.__class__.__name__
    if line is None or col is None:
        return f"{title}: {message}"

    out = [f"{title}: {message}", f"  --> {filename}:{line}:{col}"]
    diag_source = source_text
    # When an imported file raises an error, the main file's source_text is not useful.
    # Try to load the exact file named in the diagnostic so the caret still points at
    # the real failing import/library line.
    if filename and filename not in (fallback_filename, "<source>"):
        try:
            diag_source = Path(filename).read_text(encoding="utf-8")
        except OSError:
            pass
    if diag_source:
        lines = diag_source.splitlines()
        if 1 <= int(line) <= len(lines):
            src = lines[int(line) - 1]
            gutter = str(line)
            caret_col = max(1, min(int(col), len(src) + 1))
            out.append(f"   |")
            out.append(f"{gutter:>3} | {src}")
            out.append(f"   | {' ' * (caret_col - 1)}^")
    return "\n".join(out)

# =============================================================================
# Lexer
# =============================================================================

KEYWORDS = {
    "let", "var", "const", "list", "import", "use", "on", "flag", "start", "message", "action",
    "proc", "fn", "if", "else", "repeat", "forever", "while", "for",
    "return", "break", "continue", "true", "false", "null",
}

MULTI = ["==", "!=", "<=", ">=", "&&", "||", "+=", "-=", "*=", "/=", "%="]
SINGLE = set("{}()[];,.+-*/%<>!=")

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

# =============================================================================
# Parser
# =============================================================================

class Parser:
    def __init__(self, tokens: List[Token], filename: str = "<source>"):
        self.toks = tokens
        self.i = 0
        self.filename = filename

    def peek(self, n: int = 0) -> Token:
        j = self.i + n
        return self.toks[j] if j < len(self.toks) else self.toks[-1]

    def at(self, value: str) -> bool:
        return self.peek().value == value

    def kind(self, kind: str, value: Optional[str] = None) -> bool:
        t = self.peek()
        return t.kind == kind and (value is None or t.value == value)

    def advance(self) -> Token:
        t = self.peek()
        self.i += 1
        return t

    def match(self, value: str) -> bool:
        if self.peek().value == value:
            self.advance()
            return True
        return False

    def match_kw(self, value: str) -> bool:
        if self.kind("KW", value):
            self.advance()
            return True
        return False

    def expect(self, value: str) -> Token:
        if not self.match(value):
            raise self.error(f"expected {value!r}, got {self.peek().value!r}")
        return self.toks[self.i - 1]

    def expect_ident(self) -> str:
        if self.peek().kind != "IDENT":
            raise self.error(f"expected identifier, got {self.peek().value!r}")
        return self.advance().value

    def error(self, msg: str) -> ParseError:
        t = self.peek()
        return ParseError(f"{self.filename}:{t.line}:{t.col}: {msg}")

    def loc(self, node: Any, token: Any) -> Any:
        setattr(node, "filename", getattr(token, "filename", self.filename))
        setattr(node, "line", getattr(token, "line", 1))
        setattr(node, "col", getattr(token, "col", 1))
        return node

    def parse(self) -> Program:
        body: List[Any] = []
        while self.peek().kind != "EOF":
            body.append(self.parse_top_or_stmt())
        return Program(body)

    def parse_top_or_stmt(self) -> Any:
        if self.match_kw("import") or self.match_kw("use"):
            return self.parse_import(self.toks[self.i - 1])
        if self.match_kw("on"):
            return self.parse_event(self.toks[self.i - 1])
        if self.match_kw("proc") or self.match_kw("fn"):
            return self.parse_proc(self.toks[self.i - 1])
        return self.parse_statement()

    def parse_import(self, start_token: Token) -> ImportDecl:
        spec = self.expect_string_value()
        self.expect(";")
        return self.loc(ImportDecl(spec), start_token)

    def parse_event(self, start_token: Token) -> EventDecl:
        if self.match_kw("flag") or self.match_kw("start"):
            return self.loc(EventDecl("flag", None, self.parse_block()), start_token)
        if self.match_kw("action"):
            # Console entrypoint. The generated .sb3 asks for terminal input, then calls
            # Scratch procedure: Action(input).
            param = "Input"
            if self.match("("):
                param = self.expect_ident()
                self.expect(")")
            return self.loc(EventDecl("action", param, self.parse_block()), start_token)
        if self.match_kw("message"):
            value: Optional[str]
            if self.match("("):
                value = self.expect_string_value()
                self.expect(")")
            else:
                value = self.expect_string_value()
            return self.loc(EventDecl("message", value, self.parse_block()), start_token)
        raise self.error("expected event type: flag/start/action/message")

    def parse_proc(self, start_token: Token) -> ProcDecl:
        name = self.expect_ident()
        self.expect("(")
        params: List[str] = []
        if not self.at(")"):
            while True:
                params.append(self.expect_ident())
                if not self.match(","):
                    break
        self.expect(")")
        return self.loc(ProcDecl(name, params, self.parse_block()), start_token)

    def parse_block(self) -> List[Any]:
        self.expect("{")
        body: List[Any] = []
        while not self.at("}"):
            if self.peek().kind == "EOF":
                raise self.error("unterminated block")
            body.append(self.parse_top_or_stmt() if self.kind("KW", "proc") else self.parse_statement())
        self.expect("}")
        return body

    def expect_string_value(self) -> str:
        if self.peek().kind != "STRING":
            raise self.error(f"expected string, got {self.peek().value!r}")
        return self.advance().value

    def parse_statement(self) -> Any:
        start_token = self.peek()
        if self.match_kw("let"):
            name = self.expect_ident()
            expr = Literal(0)
            if self.match("="):
                expr = self.parse_expr()
            self.expect(";")
            return self.loc(VarDecl(name, expr, True), start_token)
        if self.match_kw("var"):
            name = self.expect_ident()
            expr = Literal(0)
            if self.match("="):
                expr = self.parse_expr()
            self.expect(";")
            return self.loc(VarDecl(name, expr, True), start_token)
        if self.match_kw("const"):
            name = self.expect_ident()
            self.expect("=")
            expr = self.parse_expr()
            self.expect(";")
            return self.loc(VarDecl(name, expr, False), start_token)
        if self.match_kw("list"):
            name = self.expect_ident()
            items: List[Any] = []
            if self.match("="):
                arr = self.parse_expr()
                if not isinstance(arr, ArrayExpr):
                    raise self.error("list declaration needs an array literal, e.g. list xs = [1,2,3];")
                items = arr.items
            self.expect(";")
            return self.loc(ListDecl(name, items), start_token)
        if self.match_kw("if"):
            self.expect("(")
            cond = self.parse_expr()
            self.expect(")")
            then_body = self.parse_block()
            else_body: Optional[List[Any]] = None
            if self.match_kw("else"):
                else_body = self.parse_block()
            return self.loc(IfStmt(cond, then_body, else_body), start_token)
        if self.match_kw("repeat"):
            self.expect("(")
            count = self.parse_expr()
            self.expect(")")
            return self.loc(RepeatStmt(count, self.parse_block()), start_token)
        if self.match_kw("forever"):
            return self.loc(ForeverStmt(self.parse_block()), start_token)
        if self.match_kw("while"):
            self.expect("(")
            cond = self.parse_expr()
            self.expect(")")
            return self.loc(WhileStmt(cond, self.parse_block()), start_token)
        if self.match_kw("for"):
            return self.parse_for(start_token)
        if self.match_kw("return"):
            expr = None if self.at(";") else self.parse_expr()
            self.expect(";")
            return self.loc(ReturnStmt(expr), start_token)
        if self.match_kw("break"):
            self.expect(";")
            return self.loc(BreakStmt(), start_token)
        if self.match_kw("continue"):
            self.expect(";")
            return self.loc(ContinueStmt(), start_token)
        if self.peek().kind == "IDENT" and self.peek(1).value in ("=", "+=", "-=", "*=", "/=", "%="):
            name = self.advance().value
            op = self.advance().value
            expr = self.parse_expr()
            self.expect(";")
            return self.loc(AssignStmt(name, op, expr), start_token)
        expr = self.parse_expr()
        self.expect(";")
        return self.loc(ExprStmt(expr), start_token)

    def parse_for(self, start_token: Token) -> ForStmt:
        self.expect("(")
        init: Optional[Any] = None
        if not self.at(";"):
            if self.match_kw("let") or self.match_kw("var"):
                name = self.expect_ident()
                expr = Literal(0)
                if self.match("="):
                    expr = self.parse_expr()
                init = VarDecl(name, expr, True)
            elif self.peek().kind == "IDENT" and self.peek(1).value in ("=", "+=", "-=", "*=", "/=", "%="):
                name = self.advance().value
                op = self.advance().value
                init = AssignStmt(name, op, self.parse_expr())
            else:
                init = ExprStmt(self.parse_expr())
        self.expect(";")
        cond = None if self.at(";") else self.parse_expr()
        self.expect(";")
        update: Optional[Any] = None
        if not self.at(")"):
            if self.peek().kind == "IDENT" and self.peek(1).value in ("=", "+=", "-=", "*=", "/=", "%="):
                name = self.advance().value
                op = self.advance().value
                update = AssignStmt(name, op, self.parse_expr())
            else:
                update = ExprStmt(self.parse_expr())
        self.expect(")")
        body = self.parse_block()
        return self.loc(ForStmt(init, cond, update, body), start_token)

    # Pratt parser
    PRECEDENCE = {
        "||": 1,
        "&&": 2,
        "==": 3, "!=": 3,
        "<": 4, "<=": 4, ">": 4, ">=": 4,
        "+": 5, "-": 5,
        "*": 6, "/": 6, "%": 6,
    }

    def parse_expr(self, min_prec: int = 1) -> Any:
        left = self.parse_unary()
        while True:
            op = self.peek().value
            prec = self.PRECEDENCE.get(op)
            if prec is None or prec < min_prec:
                break
            self.advance()
            right = self.parse_expr(prec + 1)
            left = self.loc(BinaryExpr(left, op, right), left)
        return left

    def parse_unary(self) -> Any:
        if self.peek().value in ("!", "-"):
            token = self.advance()
            op = token.value
            return self.loc(UnaryExpr(op, self.parse_unary()), token)
        return self.parse_postfix()

    def parse_postfix(self) -> Any:
        expr = self.parse_primary()
        while True:
            if self.match("("):
                if not isinstance(expr, VarExpr):
                    raise self.error("only named function calls are supported")
                args: List[Any] = []
                if not self.at(")"):
                    while True:
                        args.append(self.parse_expr())
                        if not self.match(","):
                            break
                self.expect(")")
                expr = self.loc(CallExpr(expr.name, args), expr)
                continue
            break
        return expr

    def parse_primary(self) -> Any:
        t = self.peek()
        if t.kind == "NUMBER":
            self.advance()
            if "." in t.value:
                return self.loc(Literal(float(t.value)), t)
            return self.loc(Literal(int(t.value)), t)
        if t.kind == "STRING":
            self.advance()
            return self.loc(Literal(t.value), t)
        if self.match_kw("true"):
            return self.loc(Literal(True), self.toks[self.i - 1])
        if self.match_kw("false"):
            return self.loc(Literal(False), self.toks[self.i - 1])
        if self.match_kw("null"):
            return self.loc(Literal(None), self.toks[self.i - 1])
        if t.kind == "IDENT":
            tok = self.advance()
            return self.loc(VarExpr(tok.value), tok)
        if self.match("("):
            expr = self.parse_expr()
            self.expect(")")
            return expr
        if self.match("["):
            items: List[Any] = []
            if not self.at("]"):
                while True:
                    items.append(self.parse_expr())
                    if not self.match(","):
                        break
            self.expect("]")
            return self.loc(ArrayExpr(items), t)
        raise self.error(f"expected expression, got {t.value!r}")

SBG_MODULES_DIR = "sbg_modules"
PACKAGE_MANIFEST = "sbgpkg.json"

class ImportResolver:
    """Resolve `import "...";` declarations into a single StageBG program.

    Supported forms:
      import "./relative/file.sbg";
      import "../lib";          // resolves .sbg, main.sbg or index.sbg
      import "pkg:package";     // resolves sbg_modules/package/main.sbg
      import "package";         // package fallback when no relative file exists
    """
    def __init__(self):
        self.seen: set[Path] = set()
        self.stack: List[Path] = []
        self.source_cache: Dict[str, str] = {}

    def parse_entry(self, text: str, filename: str) -> Program:
        filename_path = self._safe_resolve(Path(filename)) if filename and filename != "<source>" else None
        if filename_path:
            self.stack.append(filename_path)
            self.source_cache[str(filename_path)] = text
            try:
                program = Parser(Lexer(text, str(filename_path)).tokens(), str(filename_path)).parse()
                program = self.resolve_program(program, filename_path)
                self.seen.add(filename_path)
                return program
            finally:
                if self.stack and self.stack[-1] == filename_path:
                    self.stack.pop()
        program = Parser(Lexer(text, filename).tokens(), filename).parse()
        return self.resolve_program(program, Path.cwd() / "<source>.sbg")

    def resolve_program(self, program: Program, current_file: Path) -> Program:
        out: List[Any] = []
        for stmt in program.body:
            if isinstance(stmt, ImportDecl):
                try:
                    imported = self.load_import(stmt.spec, current_file)
                    out.extend(imported.body)
                except ImportSBGError as e:
                    attach_location(e, stmt)
                    raise
            else:
                out.append(stmt)
        return Program(out)

    def load_import(self, spec: str, current_file: Path) -> Program:
        path = self.resolve_import_path(spec, current_file)
        if path in self.stack:
            chain = " -> ".join(str(p) for p in [*self.stack, path])
            raise ImportSBGError(f"circular import detected: {chain}")
        if path in self.seen:
            return Program([])
        self.stack.append(path)
        try:
            text = path.read_text(encoding="utf-8")
            self.source_cache[str(path)] = text
            program = Parser(Lexer(text, str(path)).tokens(), str(path)).parse()
            program = self.resolve_program(program, path)
            self.seen.add(path)
            return program
        except OSError as e:
            raise ImportSBGError(str(e)) from e
        finally:
            if self.stack and self.stack[-1] == path:
                self.stack.pop()

    def resolve_import_path(self, spec: str, current_file: Path) -> Path:
        base = current_file.parent if current_file.name != "<source>.sbg" else Path.cwd()
        searched: List[Path] = []

        if spec.startswith("pkg:"):
            found = self.resolve_package(spec[4:], base, searched)
            if found:
                return found
            raise ImportSBGError(self.import_not_found_message(spec, searched))

        raw = Path(spec)
        file_candidates: List[Path] = []
        if raw.is_absolute():
            file_candidates.extend(self.expand_module_candidates(raw))
        else:
            file_candidates.extend(self.expand_module_candidates(base / raw))
        for cand in file_candidates:
            searched.append(cand)
            if cand.is_file():
                return self._safe_resolve(cand)

        # Bare imports can be package names, e.g. import "arrays";
        if not spec.startswith(".") and not spec.startswith("/"):
            found = self.resolve_package(spec, base, searched)
            if found:
                return found

        raise ImportSBGError(self.import_not_found_message(spec, searched))

    def expand_module_candidates(self, path: Path) -> List[Path]:
        candidates = [path]
        if path.suffix != ".sbg":
            candidates.append(path.with_suffix(".sbg"))
            candidates.append(path / "main.sbg")
            candidates.append(path / "index.sbg")
        elif path.suffix == ".sbg":
            candidates.append(path / "main.sbg")
        return [self._safe_resolve(c) for c in candidates]

    def resolve_package(self, spec: str, base: Path, searched: List[Path]) -> Optional[Path]:
        parts = [p for p in spec.split("/") if p]
        if not parts:
            return None
        pkg = parts[0]
        sub = Path(*parts[1:]) if len(parts) > 1 else None
        for modules in self.package_roots(base):
            pkg_dir = modules / pkg
            direct = modules / f"{pkg}.sbg"
            for cand in self.expand_module_candidates(direct):
                searched.append(cand)
                if cand.is_file() and sub is None:
                    return self._safe_resolve(cand)
            searched.append(pkg_dir)
            if not pkg_dir.exists():
                continue
            if sub is not None:
                for cand in self.expand_module_candidates(pkg_dir / sub):
                    searched.append(cand)
                    if cand.is_file():
                        return self._safe_resolve(cand)
            manifest = pkg_dir / PACKAGE_MANIFEST
            main_name = "main.sbg"
            if manifest.is_file():
                try:
                    meta = json.loads(manifest.read_text(encoding="utf-8"))
                    main_name = str(meta.get("main") or main_name)
                except Exception:
                    main_name = "main.sbg"
            for cand in self.expand_module_candidates(pkg_dir / main_name):
                searched.append(cand)
                if cand.is_file():
                    return self._safe_resolve(cand)
            for fallback in (pkg_dir / "main.sbg", pkg_dir / "index.sbg"):
                searched.append(fallback)
                if fallback.is_file():
                    return self._safe_resolve(fallback)
        return None

    def package_roots(self, base: Path) -> List[Path]:
        roots: List[Path] = []
        cursor = self._safe_resolve(base if base.is_dir() else base.parent)
        for parent in [cursor, *cursor.parents]:
            roots.append(parent / SBG_MODULES_DIR)
            if (parent / PACKAGE_MANIFEST).is_file():
                break
        cwd_root = Path.cwd() / SBG_MODULES_DIR
        if cwd_root not in roots:
            roots.append(cwd_root)
        return roots

    def import_not_found_message(self, spec: str, searched: List[Path]) -> str:
        shown = "\n".join(f"    {p}" for p in searched[:12])
        extra = "" if len(searched) <= 12 else f"\n    ... and {len(searched) - 12} more"
        return f"cannot resolve import {spec!r}. Searched:\n{shown}{extra}"

    @staticmethod
    def _safe_resolve(path: Path) -> Path:
        try:
            return path.resolve()
        except OSError:
            return path.absolute()

def parse_source(text: str, filename: str = "<source>") -> Program:
    return ImportResolver().parse_entry(text, filename)

# =============================================================================
# Interpreter
# =============================================================================

class ReturnSignal(Exception):
    def __init__(self, value: Any): self.value = value
class BreakSignal(Exception): pass
class ContinueSignal(Exception): pass

class Runtime:
    def __init__(self, program: Program, *, fast: bool = False, filename: str = "<source>", source_text: str = ""):
        self.program = program
        self.filename = filename
        self.source_text = source_text
        self.vars: Dict[str, Any] = {}
        self.lists: Dict[str, List[Any]] = {}
        self.procs: Dict[str, ProcDecl] = {}
        self.flag_events: List[EventDecl] = []
        self.action_events: List[EventDecl] = []
        self.message_events: Dict[str, List[EventDecl]] = {}
        self.answer_value = ""
        self.fast = fast
        self.timer_start = time.monotonic()
        self.output: List[str] = []

    def prepare(self) -> None:
        for stmt in self.program.body:
            if isinstance(stmt, VarDecl):
                self.vars[stmt.name] = self.eval(stmt.expr)
            elif isinstance(stmt, ListDecl):
                self.lists[stmt.name] = [self.eval(x) for x in stmt.items]
            elif isinstance(stmt, ProcDecl):
                self.procs[stmt.name] = stmt
            elif isinstance(stmt, EventDecl):
                if stmt.kind == "flag":
                    self.flag_events.append(stmt)
                elif stmt.kind == "action":
                    self.action_events.append(stmt)
                else:
                    self.message_events.setdefault(stmt.value or "", []).append(stmt)
            else:
                # top-level loose code becomes init/flag-like code
                self.exec_stmt(stmt)

    def run_flag(self, input_value: str = "") -> None:
        self.prepare()
        if self.flag_events:
            for ev in self.flag_events:
                self.exec_block(ev.body)
        else:
            self.run_action(input_value)

    def run_action(self, input_value: str = "") -> None:
        self.answer_value = input_value
        for ev in self.action_events:
            param = ev.value or "Input"
            old_present = param in self.vars
            old_value = self.vars.get(param)
            self.vars[param] = input_value
            try:
                self.exec_block(ev.body)
            finally:
                if old_present:
                    self.vars[param] = old_value
                else:
                    self.vars.pop(param, None)

    def run_message(self, msg: str) -> None:
        for ev in self.message_events.get(msg, []):
            self.exec_block(ev.body)

    def prepare_scratch_console(self) -> None:
        """Prepare runtime using the same entrypoint model as compiled Scratch.

        The generated .sb3 owns the green-flag script and repeatedly calls
        Action(Input). Therefore native execution must not interpret top-level
        code as a separate Python-only startup phase. Instead it builds the same
        Action(Input) body that the compiler builds:

        - global let/list declarations initialise Stage variables/lists once,
        - `on action(input)` and `proc Action(input)` become console handlers,
        - `on flag` is treated as Action(Input) only when no explicit action
          handler exists,
        - loose top-level statements are appended to Action(Input).
        """
        self.vars.clear()
        self.lists.clear()
        self.procs.clear()
        self.flag_events.clear()
        self.action_events.clear()
        self.message_events.clear()
        self.output.clear()
        self.timer_start = time.monotonic()

        loose: List[Any] = []
        flag_entries: List[EventDecl] = []
        for stmt in self.program.body:
            if isinstance(stmt, VarDecl):
                self.vars[stmt.name] = self.eval(stmt.expr)
            elif isinstance(stmt, ListDecl):
                self.lists[stmt.name] = [self.eval(x) for x in stmt.items]
            elif isinstance(stmt, ProcDecl):
                if stmt.name == ACTION_PROC_NAME:
                    param = stmt.params[0] if stmt.params else "Input"
                    self.action_events.append(EventDecl("action", param, stmt.body))
                else:
                    self.procs[stmt.name] = stmt
            elif isinstance(stmt, EventDecl):
                if stmt.kind == "action":
                    self.action_events.append(stmt)
                elif stmt.kind == "flag":
                    flag_entries.append(EventDecl("action", "Input", stmt.body))
                else:
                    self.message_events.setdefault(stmt.value or "", []).append(stmt)
            else:
                loose.append(stmt)

        if not self.action_events:
            self.action_events.extend(flag_entries)
        if loose:
            self.action_events.append(EventDecl("action", "Input", loose))

    def run_scratch_once(self, input_value: str = "") -> None:
        """Run one native Action(Input) invocation after Scratch-compatible init."""
        self.prepare_scratch_console()
        if not self.action_events:
            raise RuntimeSBGError("nothing runnable: no Action(Input) body was prepared")
        self.run_action(input_value)

    def run_scratch_terminal(self, *, prompt: str = "sbg> ") -> None:
        """Run an interactive native terminal that mirrors the generated .sb3 loop."""
        self.prepare_scratch_console()
        if not self.action_events:
            raise RuntimeSBGError("nothing runnable: no Action(Input) body was prepared")
        print("StageBG native terminal. Type /exit or press Ctrl+D to quit.")
        while True:
            try:
                line = input(prompt)
            except EOFError:
                print()
                break
            if line in ("/exit", ":q", "quit", "exit"):
                break
            self.run_action(line)

    def exec_block(self, body: List[Any]) -> None:
        for stmt in body:
            self.exec_stmt(stmt)

    def exec_stmt(self, stmt: Any) -> None:
        try:
            return self._exec_stmt(stmt)
        except RuntimeSBGError as e:
            attach_location(e, stmt)
            raise
        except (TypeError, ValueError, ZeroDivisionError, IndexError) as e:
            err = RuntimeSBGError(str(e))
            attach_location(err, stmt)
            raise err from e

    def _exec_stmt(self, stmt: Any) -> None:
        if isinstance(stmt, VarDecl):
            self.vars[stmt.name] = self.eval(stmt.expr)
        elif isinstance(stmt, ListDecl):
            self.lists[stmt.name] = [self.eval(x) for x in stmt.items]
        elif isinstance(stmt, ProcDecl):
            self.procs[stmt.name] = stmt
        elif isinstance(stmt, AssignStmt):
            val = self.eval(stmt.expr)
            old = self.vars.get(stmt.name, 0)
            if stmt.op == "=": self.vars[stmt.name] = val
            elif stmt.op == "+=": self.vars[stmt.name] = old + val
            elif stmt.op == "-=": self.vars[stmt.name] = old - val
            elif stmt.op == "*=": self.vars[stmt.name] = old * val
            elif stmt.op == "/=": self.vars[stmt.name] = old / val
            elif stmt.op == "%=": self.vars[stmt.name] = old % val
        elif isinstance(stmt, ExprStmt):
            self.eval(stmt.expr)
        elif isinstance(stmt, IfStmt):
            if self.truthy(self.eval(stmt.cond)):
                self.exec_block(stmt.then_body)
            elif stmt.else_body is not None:
                self.exec_block(stmt.else_body)
        elif isinstance(stmt, RepeatStmt):
            n = int(self.num(self.eval(stmt.count)))
            for _ in range(max(0, n)):
                try:
                    self.exec_block(stmt.body)
                except ContinueSignal:
                    continue
                except BreakSignal:
                    break
        elif isinstance(stmt, ForeverStmt):
            # The runner protects you from accidental infinite terminal loops.
            for _ in range(1000000):
                try:
                    self.exec_block(stmt.body)
                except ContinueSignal:
                    continue
                except BreakSignal:
                    break
        elif isinstance(stmt, WhileStmt):
            guard = 0
            while self.truthy(self.eval(stmt.cond)):
                guard += 1
                if guard > 1000000:
                    raise RuntimeSBGError("while loop safety limit hit")
                try:
                    self.exec_block(stmt.body)
                except ContinueSignal:
                    continue
                except BreakSignal:
                    break
        elif isinstance(stmt, ForStmt):
            if stmt.init: self.exec_stmt(stmt.init)
            guard = 0
            while True:
                if stmt.cond is not None and not self.truthy(self.eval(stmt.cond)):
                    break
                guard += 1
                if guard > 1000000:
                    raise RuntimeSBGError("for loop safety limit hit")
                try:
                    self.exec_block(stmt.body)
                except ContinueSignal:
                    pass
                except BreakSignal:
                    break
                if stmt.update: self.exec_stmt(stmt.update)
        elif isinstance(stmt, ReturnStmt):
            raise ReturnSignal(None if stmt.expr is None else self.eval(stmt.expr))
        elif isinstance(stmt, BreakStmt):
            raise BreakSignal()
        elif isinstance(stmt, ContinueStmt):
            raise ContinueSignal()
        elif isinstance(stmt, EventDecl):
            pass
        else:
            raise RuntimeSBGError(f"unknown statement {stmt}")

    def eval(self, expr: Any) -> Any:
        try:
            return self._eval(expr)
        except RuntimeSBGError as e:
            attach_location(e, expr)
            raise
        except (TypeError, ValueError, ZeroDivisionError, IndexError) as e:
            err = RuntimeSBGError(str(e))
            attach_location(err, expr)
            raise err from e

    def _eval(self, expr: Any) -> Any:
        if isinstance(expr, Literal):
            return expr.value
        if isinstance(expr, VarExpr):
            if expr.name in self.vars:
                return self.vars[expr.name]
            if expr.name in self.lists:
                return self.lists[expr.name]
            raise RuntimeSBGError(f"unknown variable/list {expr.name!r}")
        if isinstance(expr, UnaryExpr):
            v = self.eval(expr.expr)
            if expr.op == "-": return -self.num(v)
            if expr.op == "!": return not self.truthy(v)
        if isinstance(expr, BinaryExpr):
            a = self.eval(expr.left)
            if expr.op == "&&":
                return self.truthy(a) and self.truthy(self.eval(expr.right))
            if expr.op == "||":
                return self.truthy(a) or self.truthy(self.eval(expr.right))
            b = self.eval(expr.right)
            if expr.op == "+":
                if isinstance(a, str) or isinstance(b, str): return str(a) + str(b)
                return a + b
            if expr.op == "-": return self.num(a) - self.num(b)
            if expr.op == "*": return self.num(a) * self.num(b)
            if expr.op == "/": return self.num(a) / self.num(b)
            if expr.op == "%": return self.num(a) % self.num(b)
            if expr.op == "==": return a == b
            if expr.op == "!=": return a != b
            if expr.op == "<": return a < b
            if expr.op == "<=": return a <= b
            if expr.op == ">": return a > b
            if expr.op == ">=": return a >= b
        if isinstance(expr, ArrayExpr):
            return [self.eval(x) for x in expr.items]
        if isinstance(expr, CallExpr):
            try:
                return self.call(expr.callee, [self.eval(x) for x in expr.args])
            except RuntimeSBGError as e:
                attach_location(e, expr)
                raise
        raise RuntimeSBGError(f"unknown expression {expr}")

    def call(self, name: str, args: List[Any]) -> Any:
        if name in self.procs:
            proc = self.procs[name]
            if len(args) != len(proc.params):
                raise RuntimeSBGError(f"{name} expects {len(proc.params)} args, got {len(args)}")
            saved = dict(self.vars)
            for p, a in zip(proc.params, args):
                self.vars[p] = a
            try:
                self.exec_block(proc.body)
            except ReturnSignal as r:
                self.vars = saved
                return r.value
            # preserve global mutations, but remove params
            for p in proc.params:
                self.vars.pop(p, None)
            for k, v in saved.items():
                if k not in self.vars:
                    self.vars[k] = v
            return None

        if name == "log":
            text = " ".join(str(a) for a in args)
            self.output.append(text)
            self.lists.setdefault("Terminal", []).append(text)
            print(text)
            return None
        if name == "wait":
            if not self.fast:
                time.sleep(float(args[0]) if args else 0)
            return None
        if name == "ask":
            question = str(args[0]) if args else ""
            self.answer_value = input(question + " ")
            return self.answer_value
        if name == "answer": return self.answer_value
        if name == "broadcast":
            self.run_message(str(args[0]))
            return None
        if name == "broadcastAndWait":
            self.run_message(str(args[0]))
            return None
        if name == "len":
            obj = args[0]
            return len(obj)
        if name == "item":
            lst = self.get_list_arg(args[0])
            idx = int(args[1]) - 1
            return lst[idx]
        if name == "push":
            lst = self.get_list_arg(args[0], require_name=True)
            lst.append(args[1])
            return None
        if name == "insert":
            lst = self.get_list_arg(args[0], require_name=True)
            lst.insert(max(0, int(args[1]) - 1), args[2])
            return None
        if name == "delete":
            lst = self.get_list_arg(args[0], require_name=True)
            del lst[int(args[1]) - 1]
            return None
        if name == "replace":
            lst = self.get_list_arg(args[0], require_name=True)
            lst[int(args[1]) - 1] = args[2]
            return None
        if name == "contains":
            return args[1] in self.get_list_arg(args[0])
        if name == "join":
            return "".join(str(a) for a in args)
        if name == "random": return random.uniform(float(args[0]), float(args[1]))
        if name == "round": return round(float(args[0]))
        if name == "floor": return math.floor(float(args[0]))
        if name == "ceil": return math.ceil(float(args[0]))
        if name == "sqrt": return math.sqrt(float(args[0]))
        if name == "abs": return abs(float(args[0]))
        if name == "min": return min(args)
        if name == "max": return max(args)
        if name == "timer": return time.monotonic() - self.timer_start
        if name == "resetTimer": self.timer_start = time.monotonic(); return None
        if name in ("setBackdrop", "nextBackdrop", "playSound", "stopAllSounds"):
            # Runner is headless; compiler emits real Scratch blocks for these.
            return None
        raise RuntimeSBGError(f"unknown function {name!r}")

    def get_list_arg(self, value: Any, require_name: bool = False) -> List[Any]:
        if isinstance(value, str) and value in self.lists:
            return self.lists[value]
        if isinstance(value, list):
            return value
        raise RuntimeSBGError("expected list value/name")

    @staticmethod
    def truthy(v: Any) -> bool:
        return bool(v)

    @staticmethod
    def num(v: Any) -> float:
        if isinstance(v, bool): return 1 if v else 0
        return float(v)

# =============================================================================
# Scratch compiler
# =============================================================================

BACKDROP_SVG = '''<svg version="1.1" width="2" height="2" viewBox="-1 -1 2 2" xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink">
  <!-- Exported by Scratch - http://scratch.mit.edu/ -->
</svg>'''

TERMINAL_LIST_NAME = "Terminal"
TERMINAL_LIST_ID = ",(0/{jAb*2vBd56rlG@1"
ACTION_PROC_NAME = "Action"

# Builtins that are emitted as real Scratch blocks. Procedure names may shadow
# them only in statement position; expression-position returns are not available
# for Scratch custom blocks, so diagnostics try to catch confusing cases early.
BUILTIN_EXPR_NAMES = {
    "answer", "random", "round", "abs", "floor", "ceil", "sqrt",
    "join", "len", "item", "contains", "timer",
}
BUILTIN_STMT_NAMES = {
    "log", "wait", "ask", "broadcast", "broadcastAndWait", "resetTimer",
    "push", "insert", "delete", "replace", "setBackdrop", "nextBackdrop",
    "playSound", "stopAllSounds",
}
BUILTIN_NAMES = BUILTIN_EXPR_NAMES | BUILTIN_STMT_NAMES

class ScratchBuilder:
    def __init__(self):
        self.blocks: Dict[str, Dict[str, Any]] = {}
        self.variables: Dict[str, str] = {}
        self.lists: Dict[str, str] = {}
        self.broadcasts: Dict[str, str] = {}
        self.counter = 0
        self.x = 40
        self.y = 40
        self.current_proc_params: Dict[str, str] = {}

    def uid(self, prefix: str = "b") -> str:
        self.counter += 1
        return f"{prefix}{self.counter:05d}"

    def add_block(self, opcode: str, *, next: Optional[str] = None, parent: Optional[str] = None,
                  inputs: Optional[Dict[str, Any]] = None, fields: Optional[Dict[str, Any]] = None,
                  shadow: bool = False, topLevel: bool = False, x: Optional[int] = None, y: Optional[int] = None,
                  mutation: Optional[Dict[str, Any]] = None) -> str:
        bid = self.uid()
        obj: Dict[str, Any] = {
            "opcode": opcode,
            "next": next,
            "parent": parent,
            "inputs": inputs or {},
            "fields": fields or {},
            "shadow": shadow,
            "topLevel": topLevel,
        }
        if topLevel:
            obj["x"] = self.x if x is None else x
            obj["y"] = self.y if y is None else y
            self.y += 140
        if mutation is not None:
            obj["mutation"] = mutation
        self.blocks[bid] = obj
        return bid

    def set_parent(self, bid: Optional[str], parent: str) -> None:
        if bid and bid in self.blocks:
            self.blocks[bid]["parent"] = parent

    def chain(self, first: Optional[str], second: Optional[str]) -> Optional[str]:
        if not first:
            return second
        if not second:
            return first
        last = first
        while self.blocks[last].get("next"):
            last = self.blocks[last]["next"]
        self.blocks[last]["next"] = second
        self.blocks[second]["parent"] = last
        return first

    def var_id(self, name: str) -> str:
        if name not in self.variables:
            self.variables[name] = self.uid("var")
        return self.variables[name]

    def list_id(self, name: str) -> str:
        if name not in self.lists:
            self.lists[name] = self.uid("list")
        return self.lists[name]

    def broadcast_id(self, name: str) -> str:
        if name not in self.broadcasts:
            self.broadcasts[name] = self.uid("msg")
        return self.broadcasts[name]

    def literal_input(self, value: Any) -> Any:
        if isinstance(value, bool):
            # No boolean literal primitive in Scratch. Use expression block elsewhere.
            raise CompileError("internal: boolean literal must be compiled as block")
        if isinstance(value, (int, float)):
            return [1, [4, str(value)]]
        if value is None:
            return [1, [10, ""]]
        return [1, [10, str(value)]]

    def expr_input(self, expr: Any, parent: Optional[str] = None) -> Any:
        if isinstance(expr, Literal) and not isinstance(expr.value, bool):
            return self.literal_input(expr.value)
        bid = self.compile_expr(expr, parent=parent)
        return [2, bid] if self.is_boolean_expr(expr) else [1, bid]

    def substack_input(self, first: Optional[str]) -> Any:
        return [2, first] if first else [1, None]

    def is_boolean_expr(self, expr: Any) -> bool:
        if isinstance(expr, Literal) and isinstance(expr.value, bool): return True
        if isinstance(expr, UnaryExpr) and expr.op == "!": return True
        if isinstance(expr, BinaryExpr) and expr.op in ("==", "!=", "<", "<=", ">", ">=", "&&", "||"): return True
        if isinstance(expr, CallExpr) and expr.callee == "contains": return True
        return False

    def compile_expr(self, expr: Any, parent: Optional[str] = None) -> str:
        try:
            return self._compile_expr(expr, parent=parent)
        except CompileError as e:
            attach_location(e, expr)
            raise
        except Exception as e:
            err = CompileError(str(e))
            attach_location(err, expr)
            raise err from e

    def _compile_expr(self, expr: Any, parent: Optional[str] = None) -> str:
        if isinstance(expr, Literal):
            if isinstance(expr.value, bool):
                a = Literal(1)
                b = Literal(1 if expr.value else 0)
                return self.compile_expr(BinaryExpr(a, "==", b), parent)
            # String/number literals generally appear as primitive inputs.
            bid = self.add_block("operator_join", parent=parent,
                                 inputs={"STRING1": self.literal_input(expr.value), "STRING2": self.literal_input("")})
            return bid
        if isinstance(expr, VarExpr):
            if expr.name in self.current_proc_params:
                return self.add_block("argument_reporter_string_number", parent=parent,
                                      fields={"VALUE": [expr.name, None]})
            if expr.name in self.lists:
                return self.add_block("data_listcontents", parent=parent,
                                      fields={"LIST": [expr.name, self.list_id(expr.name)]})
            return self.add_block("data_variable", parent=parent,
                                  fields={"VARIABLE": [expr.name, self.var_id(expr.name)]})
        if isinstance(expr, UnaryExpr):
            if expr.op == "!":
                bid = self.add_block("operator_not", parent=parent, inputs={})
                self.blocks[bid]["inputs"]["OPERAND"] = self.expr_input(expr.expr, bid)
                return bid
            if expr.op == "-":
                return self.compile_expr(BinaryExpr(Literal(0), "-", expr.expr), parent)
        if isinstance(expr, BinaryExpr):
            opmap = {
                "+": "operator_add", "-": "operator_subtract", "*": "operator_multiply", "/": "operator_divide",
                "%": "operator_mod", "==": "operator_equals", "<": "operator_lt", ">": "operator_gt",
                "&&": "operator_and", "||": "operator_or",
            }
            if expr.op == "!=":
                return self.compile_expr(UnaryExpr("!", BinaryExpr(expr.left, "==", expr.right)), parent)
            if expr.op == "<=":
                return self.compile_expr(UnaryExpr("!", BinaryExpr(expr.left, ">", expr.right)), parent)
            if expr.op == ">=":
                return self.compile_expr(UnaryExpr("!", BinaryExpr(expr.left, "<", expr.right)), parent)
            opcode = opmap[expr.op]
            bid = self.add_block(opcode, parent=parent, inputs={})
            if expr.op in ("&&", "||"):
                self.blocks[bid]["inputs"]["OPERAND1"] = self.expr_input(expr.left, bid)
                self.blocks[bid]["inputs"]["OPERAND2"] = self.expr_input(expr.right, bid)
            elif expr.op in ("<", ">", "=="):
                self.blocks[bid]["inputs"]["OPERAND1"] = self.expr_input(expr.left, bid)
                self.blocks[bid]["inputs"]["OPERAND2"] = self.expr_input(expr.right, bid)
            else:
                self.blocks[bid]["inputs"]["NUM1"] = self.expr_input(expr.left, bid)
                self.blocks[bid]["inputs"]["NUM2"] = self.expr_input(expr.right, bid)
            return bid
        if isinstance(expr, CallExpr):
            return self.compile_call_expr(expr, parent)
        raise CompileError(f"expression cannot be compiled to Scratch: {expr}")

    def compile_call_expr(self, expr: CallExpr, parent: Optional[str]) -> str:
        name = expr.callee
        a = expr.args
        if name == "answer":
            return self.add_block("sensing_answer", parent=parent)
        if name == "random":
            self.need_args(name, a, 2)
            bid = self.add_block("operator_random", parent=parent, inputs={})
            self.blocks[bid]["inputs"]["FROM"] = self.expr_input(a[0], bid)
            self.blocks[bid]["inputs"]["TO"] = self.expr_input(a[1], bid)
            return bid
        if name == "round":
            self.need_args(name, a, 1)
            bid = self.add_block("operator_round", parent=parent, inputs={})
            self.blocks[bid]["inputs"]["NUM"] = self.expr_input(a[0], bid)
            return bid
        if name in ("abs", "floor", "ceil", "sqrt"):
            self.need_args(name, a, 1)
            op = {"abs": "abs", "floor": "floor", "ceil": "ceiling", "sqrt": "sqrt"}[name]
            bid = self.add_block("operator_mathop", parent=parent, inputs={}, fields={"OPERATOR": [op, None]})
            self.blocks[bid]["inputs"]["NUM"] = self.expr_input(a[0], bid)
            return bid
        if name == "join":
            self.need_args(name, a, 2)
            bid = self.add_block("operator_join", parent=parent, inputs={})
            self.blocks[bid]["inputs"]["STRING1"] = self.expr_input(a[0], bid)
            self.blocks[bid]["inputs"]["STRING2"] = self.expr_input(a[1], bid)
            return bid
        if name == "len":
            self.need_args(name, a, 1)
            # Scratch has two different blocks:
            #   - length of list
            #   - length of string
            # Older SBG builds treated every VarExpr passed to len() as a list,
            # which silently turned procedure parameters like `text` into lists.
            # That could produce projects that looked empty/broken after import.
            if isinstance(a[0], VarExpr) and a[0].name in self.lists:
                lst_name = a[0].name
                return self.add_block("data_lengthoflist", parent=parent, fields={"LIST": [lst_name, self.list_id(lst_name)]})
            bid = self.add_block("operator_length", parent=parent, inputs={})
            self.blocks[bid]["inputs"]["STRING"] = self.expr_input(a[0], bid)
            return bid
        if name == "item":
            self.need_args(name, a, 2)
            lst_name = self.require_list_expr(a[0])
            bid = self.add_block("data_itemoflist", parent=parent, fields={"LIST": [lst_name, self.list_id(lst_name)]}, inputs={})
            self.blocks[bid]["inputs"]["INDEX"] = self.expr_input(a[1], bid)
            return bid
        if name == "contains":
            self.need_args(name, a, 2)
            lst_name = self.require_list_expr(a[0])
            bid = self.add_block("data_listcontainsitem", parent=parent, fields={"LIST": [lst_name, self.list_id(lst_name)]}, inputs={})
            self.blocks[bid]["inputs"]["ITEM"] = self.expr_input(a[1], bid)
            return bid
        if name == "timer":
            return self.add_block("sensing_timer", parent=parent)
        raise CompileError(f"function {name} cannot be used as an expression in Scratch output")

    def need_args(self, name: str, args: List[Any], n: int) -> None:
        if len(args) != n:
            raise CompileError(f"{name}() expects {n} args, got {len(args)}")

    def require_list_expr(self, expr: Any) -> str:
        if isinstance(expr, VarExpr):
            # Scratch custom blocks cannot receive a list as a real reference.
            # A parameter named `target_list` is only a string/number argument in
            # Scratch, not a dynamic list handle. Failing here is much better than
            # generating a project that imports as blank or behaves like it is blank.
            if expr.name in self.current_proc_params:
                raise CompileError(
                    f"Scratch output cannot use procedure parameter {expr.name!r} as a list reference. "
                    "Scratch custom blocks do not support list-reference parameters. "
                    "Use a concrete declared list name, or generate one wrapper proc per list."
                )
            if expr.name not in self.lists:
                raise CompileError(f"unknown list {expr.name!r}; declare it with `list {expr.name} = [];` before using list functions")
            self.list_id(expr.name)
            return expr.name
        raise CompileError("Scratch list functions need a plain list name, e.g. len(items), item(items, 1)")

    def compile_statement_chain(self, body: List[Any]) -> Optional[str]:
        first: Optional[str] = None
        for stmt in body:
            try:
                sid = self.compile_stmt(stmt)
            except CompileError as e:
                attach_location(e, stmt)
                raise
            except Exception as e:
                err = CompileError(str(e))
                attach_location(err, stmt)
                raise err from e
            first = self.chain(first, sid)
        return first

    def compile_stmt(self, stmt: Any) -> Optional[str]:
        if isinstance(stmt, VarDecl):
            self.var_id(stmt.name)
            bid = self.add_block("data_setvariableto", fields={"VARIABLE": [stmt.name, self.var_id(stmt.name)]}, inputs={})
            self.blocks[bid]["inputs"]["VALUE"] = self.expr_input(stmt.expr, bid)
            return bid
        if isinstance(stmt, ListDecl):
            self.list_id(stmt.name)
            # Project JSON initializes lists. Runtime reset is emulated by delete all + add items.
            first = self.add_block("data_deletealloflist", fields={"LIST": [stmt.name, self.list_id(stmt.name)]})
            chain = first
            for item in stmt.items:
                add = self.add_block("data_addtolist", fields={"LIST": [stmt.name, self.list_id(stmt.name)]}, inputs={})
                self.blocks[add]["inputs"]["ITEM"] = self.expr_input(item, add)
                chain = self.chain(chain, add) or chain
            return first
        if isinstance(stmt, AssignStmt):
            self.var_id(stmt.name)
            if stmt.op == "+=":
                bid = self.add_block("data_changevariableby", fields={"VARIABLE": [stmt.name, self.var_id(stmt.name)]}, inputs={})
                self.blocks[bid]["inputs"]["VALUE"] = self.expr_input(stmt.expr, bid)
                return bid
            expr = stmt.expr
            if stmt.op == "-=": expr = BinaryExpr(VarExpr(stmt.name), "-", stmt.expr)
            elif stmt.op == "*=": expr = BinaryExpr(VarExpr(stmt.name), "*", stmt.expr)
            elif stmt.op == "/=": expr = BinaryExpr(VarExpr(stmt.name), "/", stmt.expr)
            elif stmt.op == "%=": expr = BinaryExpr(VarExpr(stmt.name), "%", stmt.expr)
            bid = self.add_block("data_setvariableto", fields={"VARIABLE": [stmt.name, self.var_id(stmt.name)]}, inputs={})
            self.blocks[bid]["inputs"]["VALUE"] = self.expr_input(expr, bid)
            return bid
        if isinstance(stmt, ExprStmt):
            if isinstance(stmt.expr, CallExpr):
                return self.compile_call_stmt(stmt.expr)
            raise CompileError("only function calls can be used as expression statements in Scratch output")
        if isinstance(stmt, IfStmt):
            if stmt.else_body is not None:
                then_first = self.compile_statement_chain(stmt.then_body)
                else_first = self.compile_statement_chain(stmt.else_body)
                bid = self.add_block("control_if_else", inputs={})
                self.blocks[bid]["inputs"]["CONDITION"] = self.expr_input(stmt.cond, bid)
                self.blocks[bid]["inputs"]["SUBSTACK"] = self.substack_input(then_first)
                self.blocks[bid]["inputs"]["SUBSTACK2"] = self.substack_input(else_first)
                self.set_parent(then_first, bid); self.set_parent(else_first, bid)
                return bid
            then_first = self.compile_statement_chain(stmt.then_body)
            bid = self.add_block("control_if", inputs={})
            self.blocks[bid]["inputs"]["CONDITION"] = self.expr_input(stmt.cond, bid)
            self.blocks[bid]["inputs"]["SUBSTACK"] = self.substack_input(then_first)
            self.set_parent(then_first, bid)
            return bid
        if isinstance(stmt, RepeatStmt):
            sub = self.compile_statement_chain(stmt.body)
            bid = self.add_block("control_repeat", inputs={})
            self.blocks[bid]["inputs"]["TIMES"] = self.expr_input(stmt.count, bid)
            self.blocks[bid]["inputs"]["SUBSTACK"] = self.substack_input(sub)
            self.set_parent(sub, bid)
            return bid
        if isinstance(stmt, ForeverStmt):
            sub = self.compile_statement_chain(stmt.body)
            bid = self.add_block("control_forever", inputs={"SUBSTACK": self.substack_input(sub)})
            self.set_parent(sub, bid)
            return bid
        if isinstance(stmt, WhileStmt):
            sub = self.compile_statement_chain(stmt.body)
            bid = self.add_block("control_repeat_until", inputs={})
            self.blocks[bid]["inputs"]["CONDITION"] = self.expr_input(UnaryExpr("!", stmt.cond), bid)
            self.blocks[bid]["inputs"]["SUBSTACK"] = self.substack_input(sub)
            self.set_parent(sub, bid)
            return bid
        if isinstance(stmt, ForStmt):
            # Compile for(init;cond;update){body} as init; repeat until !(cond){body;update}
            first = self.compile_stmt(stmt.init) if stmt.init else None
            sub_body = list(stmt.body)
            if stmt.update:
                sub_body.append(stmt.update)
            cond = stmt.cond if stmt.cond is not None else Literal(True)
            loop = self.compile_stmt(WhileStmt(cond, sub_body))
            return self.chain(first, loop)
        if isinstance(stmt, ReturnStmt):
            raise CompileError("Scratch procedures do not return values. Use output variables/lists instead of return.")
        if isinstance(stmt, (BreakStmt, ContinueStmt)):
            raise CompileError("break/continue cannot be represented safely in Scratch blocks")
        if isinstance(stmt, ProcDecl):
            return None
        if isinstance(stmt, EventDecl):
            return None
        raise CompileError(f"statement cannot be compiled: {stmt}")

    def compile_call_stmt(self, expr: CallExpr) -> Optional[str]:
        name, a = expr.callee, expr.args
        if name == "log":
            self.list_id(TERMINAL_LIST_NAME)
            val = Literal("") if not a else a[0] if len(a) == 1 else self.join_many(a)
            bid = self.add_block("data_addtolist", fields={"LIST": [TERMINAL_LIST_NAME, self.list_id(TERMINAL_LIST_NAME)]}, inputs={})
            self.blocks[bid]["inputs"]["ITEM"] = self.expr_input(val, bid)
            return bid
        if name == "wait":
            self.need_args(name, a, 1)
            bid = self.add_block("control_wait", inputs={})
            self.blocks[bid]["inputs"]["DURATION"] = self.expr_input(a[0], bid)
            return bid
        if name == "ask":
            self.need_args(name, a, 1)
            bid = self.add_block("sensing_askandwait", inputs={})
            self.blocks[bid]["inputs"]["QUESTION"] = self.expr_input(a[0], bid)
            return bid
        if name in ("broadcast", "broadcastAndWait"):
            self.need_args(name, a, 1)
            if not isinstance(a[0], Literal) or not isinstance(a[0].value, str):
                raise CompileError("broadcast() target must be a string literal for Scratch output")
            msg = a[0].value
            bid = self.add_block("event_broadcastandwait" if name == "broadcastAndWait" else "event_broadcast", inputs={})
            menu = self.add_block("event_broadcast_menu", parent=bid, shadow=True,
                                  fields={"BROADCAST_OPTION": [msg, self.broadcast_id(msg)]})
            self.blocks[bid]["inputs"]["BROADCAST_INPUT"] = [1, menu]
            return bid
        if name == "resetTimer":
            return self.add_block("sensing_resettimer")
        if name == "push":
            self.need_args(name, a, 2)
            lst = self.require_list_expr(a[0])
            bid = self.add_block("data_addtolist", fields={"LIST": [lst, self.list_id(lst)]}, inputs={})
            self.blocks[bid]["inputs"]["ITEM"] = self.expr_input(a[1], bid)
            return bid
        if name == "insert":
            self.need_args(name, a, 3)
            lst = self.require_list_expr(a[0])
            bid = self.add_block("data_insertatlist", fields={"LIST": [lst, self.list_id(lst)]}, inputs={})
            self.blocks[bid]["inputs"]["INDEX"] = self.expr_input(a[1], bid)
            self.blocks[bid]["inputs"]["ITEM"] = self.expr_input(a[2], bid)
            return bid
        if name == "delete":
            self.need_args(name, a, 2)
            lst = self.require_list_expr(a[0])
            bid = self.add_block("data_deleteoflist", fields={"LIST": [lst, self.list_id(lst)]}, inputs={})
            self.blocks[bid]["inputs"]["INDEX"] = self.expr_input(a[1], bid)
            return bid
        if name == "replace":
            self.need_args(name, a, 3)
            lst = self.require_list_expr(a[0])
            bid = self.add_block("data_replaceitemoflist", fields={"LIST": [lst, self.list_id(lst)]}, inputs={})
            self.blocks[bid]["inputs"]["INDEX"] = self.expr_input(a[1], bid)
            self.blocks[bid]["inputs"]["ITEM"] = self.expr_input(a[2], bid)
            return bid
        if name == "setBackdrop":
            self.need_args(name, a, 1)
            # The generated project has one backdrop; dynamic backdrop names work if user later adds them in Scratch.
            bid = self.add_block("looks_switchbackdropto", inputs={})
            self.blocks[bid]["inputs"]["BACKDROP"] = self.expr_input(a[0], bid)
            return bid
        if name == "nextBackdrop":
            return self.add_block("looks_nextbackdrop")
        if name == "playSound":
            self.need_args(name, a, 1)
            if not isinstance(a[0], Literal):
                raise CompileError("playSound() needs a string literal in Scratch output")
            return self.add_block("sound_play", fields={"SOUND_MENU": [str(a[0].value), None]})
        if name == "stopAllSounds":
            return self.add_block("sound_stopallsounds")
        # Procedure call
        return self.compile_proc_call(name, a)

    def join_many(self, args: List[Any]) -> Any:
        expr = args[0]
        for nxt in args[1:]:
            expr = CallExpr("join", [expr, nxt])
        return expr

    def compile_proc_call(self, name: str, args: List[Any]) -> str:
        proccode, argids = self.proc_signatures.get(name, (None, None))  # type: ignore[attr-defined]
        if proccode is None or argids is None:
            raise CompileError(f"unknown procedure {name}()")
        if len(args) != len(argids):
            raise CompileError(f"{name}() expects {len(argids)} args, got {len(args)}")
        bid = self.add_block("procedures_call", inputs={}, mutation={
            "tagName": "mutation", "children": [], "proccode": proccode,
            "argumentids": json.dumps(argids), "warp": "false"
        })
        for argid, arg in zip(argids, args):
            self.blocks[bid]["inputs"][argid] = self.expr_input(arg, bid)
        return bid

    def compile_proc_definition(self, proc: ProcDecl) -> str:
        proccode, argids = self.proc_signatures[proc.name]  # type: ignore[attr-defined]
        def_id = self.add_block("procedures_definition", topLevel=True, x=520, y=self.y)
        proto_id = self.uid()
        self.blocks[def_id]["inputs"] = {"custom_block": [1, proto_id]}
        proto_inputs: Dict[str, Any] = {}
        for param, argid in zip(proc.params, argids):
            reporter_id = self.add_block("argument_reporter_string_number", parent=proto_id, shadow=True,
                                         fields={"VALUE": [param, None]})
            proto_inputs[argid] = [1, reporter_id]
        self.blocks[proto_id] = {
            "opcode": "procedures_prototype",
            "next": None,
            "parent": def_id,
            "inputs": proto_inputs,
            "fields": {},
            "shadow": True,
            "topLevel": False,
            "mutation": {
                "tagName": "mutation",
                "children": [],
                "proccode": proccode,
                "argumentids": json.dumps(argids),
                "argumentnames": json.dumps(proc.params),
                "argumentdefaults": json.dumps(["" for _ in proc.params]),
                "warp": "false"
            }
        }
        saved = dict(self.current_proc_params)
        self.current_proc_params = {p: aid for p, aid in zip(proc.params, argids)}
        body_first = self.compile_statement_chain(proc.body)
        self.current_proc_params = saved
        self.blocks[def_id]["next"] = body_first
        if body_first:
            self.blocks[body_first]["parent"] = def_id
        return def_id

class Compiler:
    def __init__(self, program: Program, *, allow_library: bool = False):
        self.program = program
        self.allow_library = allow_library
        self.b = ScratchBuilder()
        # Use the uploaded Empty Console.sb3 layout: a visible fullscreen list monitor
        # named "Terminal" plus a green-flag prompt loop that calls Action(Input).
        self.b.lists[TERMINAL_LIST_NAME] = TERMINAL_LIST_ID
        self.init_values: Dict[str, Any] = {}
        self.init_lists: Dict[str, List[Any]] = {TERMINAL_LIST_NAME: []}
        self.procs: Dict[str, ProcDecl] = {}
        self.message_events: List[EventDecl] = []
        self.action_entries: List[Tuple[str, List[Any]]] = []
        self.action_param_names: set[str] = {"Input"}
        self.action_argid: Optional[str] = None

    def compile_error(self, message: str, node: Any = None) -> CompileError:
        err = CompileError(message)
        if node is not None:
            attach_location(err, node)
        return err

    @staticmethod
    def has_duplicate(values: List[str]) -> Optional[str]:
        seen: set[str] = set()
        for value in values:
            if value in seen:
                return value
            seen.add(value)
        return None

    def analyze(self) -> None:
        """Collect globals, procedures, events and Scratch ids before block emission."""
        loose: List[Any] = []
        flag_entries: List[Tuple[str, List[Any]]] = []
        self._sbg_main_candidate: Optional[List[Any]] = None
        self._sbg_saw_non_main_entry = False

        globals_seen: Dict[str, Any] = {}

        # First pass: decide what becomes the console Action(Input) body and fail
        # on source patterns that previously produced confusing/blank-looking sb3s.
        for stmt in self.program.body:
            if isinstance(stmt, VarDecl):
                if stmt.name == TERMINAL_LIST_NAME:
                    raise self.compile_error(f"{TERMINAL_LIST_NAME!r} is reserved for the fullscreen terminal list", stmt)
                if stmt.name in globals_seen:
                    raise self.compile_error(f"duplicate global name {stmt.name!r}", stmt)
                globals_seen[stmt.name] = stmt
                self.b.var_id(stmt.name)
                self.init_values[stmt.name] = self.literal_value_or_zero(stmt.expr)
            elif isinstance(stmt, ListDecl):
                if stmt.name == TERMINAL_LIST_NAME:
                    raise self.compile_error(f"{TERMINAL_LIST_NAME!r} is reserved; use log(...) / clearTerminal() instead of redeclaring it", stmt)
                if stmt.name in globals_seen:
                    raise self.compile_error(f"duplicate global name {stmt.name!r}", stmt)
                globals_seen[stmt.name] = stmt
                self.b.list_id(stmt.name)
                self.init_lists[stmt.name] = [self.literal_value_or_zero(x) for x in stmt.items]
            elif isinstance(stmt, ProcDecl):
                dup_param = self.has_duplicate(stmt.params)
                if dup_param:
                    raise self.compile_error(f"duplicate parameter {dup_param!r} in {stmt.name}()", stmt)
                if stmt.name == ACTION_PROC_NAME:
                    if len(stmt.params) > 1:
                        raise self.compile_error("Action() may have at most one parameter because the console passes one input string", stmt)
                    param = stmt.params[0] if stmt.params else "Input"
                    self.action_entries.append((param, stmt.body))
                    self.action_param_names.add(param)
                    self._sbg_saw_non_main_entry = True
                else:
                    if stmt.name in self.procs:
                        raise self.compile_error(f"duplicate procedure {stmt.name}()", stmt)
                    self.procs[stmt.name] = stmt
            elif isinstance(stmt, EventDecl):
                if stmt.kind == "action":
                    param = stmt.value or "Input"
                    self.action_entries.append((param, stmt.body))
                    self.action_param_names.add(param)
                    if getattr(stmt, "sbg_is_cpp_main", False) and self._sbg_main_candidate is None:
                        self._sbg_main_candidate = stmt.body
                    else:
                        self._sbg_saw_non_main_entry = True
                elif stmt.kind == "flag":
                    # In console projects the real green-flag script is the terminal prompt loop.
                    # Old SBG code using `on flag { ... }` is treated as Action(Input) body.
                    flag_entries.append(("Input", stmt.body))
                    self._sbg_saw_non_main_entry = True
                elif stmt.kind == "message":
                    if stmt.value is not None:
                        self.b.broadcast_id(stmt.value)
                    self.message_events.append(stmt)
            else:
                loose.append(stmt)

        had_explicit_entrypoint = bool(self.action_entries) or bool(flag_entries)

        if not self.action_entries:
            self.action_entries.extend(flag_entries)
        if loose:
            if not had_explicit_entrypoint:
                first_loose = loose[0]
                raise self.compile_error(
                    "no entry point found: top-level statements must be wrapped in "
                    "int main() { ... }, on action(input) { ... }, on flag { ... }, "
                    "or proc Action(input) { ... }. (This usually means main()'s "
                    "braces are missing or were commented out.)",
                    first_loose,
                )
            self.action_entries.append(("Input", loose))
            self._sbg_saw_non_main_entry = True

        self.single_cpp_main_body: Optional[List[Any]] = (
            self._sbg_main_candidate
            if (self._sbg_main_candidate is not None
                and not self._sbg_saw_non_main_entry
                and len(self.action_entries) == 1)
            else None
        )

        if not self.allow_library and not self.action_entries:
            first = self.program.body[0] if self.program.body else None
            raise self.compile_error(
                "nothing runnable was compiled: this file looks like a library. "
                "Add `on action(input) { ... }`, add loose top-level statements, "
                "or compile a main.sbg that imports this file. Use `--allow-library` only when you intentionally want a library-only sb3.",
                first,
            )

        def walk_stmt(stmt: Any, local_params: Optional[set[str]] = None) -> None:
            local_params = local_params or set()
            if isinstance(stmt, VarDecl):
                self.b.var_id(stmt.name)
                walk_expr(stmt.expr, local_params)
            elif isinstance(stmt, ListDecl):
                self.b.list_id(stmt.name)
                for x in stmt.items:
                    walk_expr(x, local_params)
            elif isinstance(stmt, AssignStmt):
                if stmt.name not in local_params and stmt.name not in self.action_param_names:
                    self.b.var_id(stmt.name)
                walk_expr(stmt.expr, local_params)
            elif isinstance(stmt, ProcDecl):
                proc_params = set(stmt.params)
                for s in stmt.body:
                    walk_stmt(s, proc_params)
            elif isinstance(stmt, EventDecl):
                event_params = set()
                if stmt.kind == "action":
                    event_params.add(stmt.value or "Input")
                for s in stmt.body:
                    walk_stmt(s, event_params)
            elif isinstance(stmt, IfStmt):
                walk_expr(stmt.cond, local_params)
                for s in stmt.then_body:
                    walk_stmt(s, local_params)
                for s in (stmt.else_body or []):
                    walk_stmt(s, local_params)
            elif isinstance(stmt, RepeatStmt):
                walk_expr(stmt.count, local_params)
                for s in stmt.body:
                    walk_stmt(s, local_params)
            elif isinstance(stmt, ForeverStmt):
                for s in stmt.body:
                    walk_stmt(s, local_params)
            elif isinstance(stmt, WhileStmt):
                walk_expr(stmt.cond, local_params)
                for s in stmt.body:
                    walk_stmt(s, local_params)
            elif isinstance(stmt, ForStmt):
                if stmt.init:
                    walk_stmt(stmt.init, local_params)
                if stmt.cond:
                    walk_expr(stmt.cond, local_params)
                if stmt.update:
                    walk_stmt(stmt.update, local_params)
                for s in stmt.body:
                    walk_stmt(s, local_params)
            elif isinstance(stmt, ExprStmt):
                walk_expr(stmt.expr, local_params)
            elif isinstance(stmt, ReturnStmt) and stmt.expr:
                walk_expr(stmt.expr, local_params)

        def walk_expr(expr: Any, local_params: Optional[set[str]] = None) -> None:
            local_params = local_params or set()
            if isinstance(expr, VarExpr):
                # Procedure/action arguments are Scratch argument reporters, not Stage variables.
                if expr.name in local_params or expr.name in self.action_param_names:
                    return
                if expr.name not in self.b.lists:
                    self.b.var_id(expr.name)
            elif isinstance(expr, BinaryExpr):
                walk_expr(expr.left, local_params)
                walk_expr(expr.right, local_params)
            elif isinstance(expr, UnaryExpr):
                walk_expr(expr.expr, local_params)
            elif isinstance(expr, CallExpr):
                if expr.callee in ("broadcast", "broadcastAndWait") and expr.args and isinstance(expr.args[0], Literal):
                    self.b.broadcast_id(str(expr.args[0].value))
                if expr.args and isinstance(expr.args[0], VarExpr):
                    first_name = expr.args[0].name
                    if first_name in local_params:
                        # Do not promote procedure parameters to lists. Scratch cannot
                        # pass list references through custom-block arguments; the real
                        # diagnostic is emitted during block generation.
                        pass
                    elif expr.callee in ("push", "insert", "delete", "replace", "contains", "item"):
                        self.b.list_id(first_name)
                        self.b.variables.pop(first_name, None)
                    elif expr.callee == "len" and first_name in self.b.lists:
                        self.b.variables.pop(first_name, None)
                if expr.callee == "log":
                    self.b.list_id(TERMINAL_LIST_NAME)
                for a in expr.args:
                    walk_expr(a, local_params)
            elif isinstance(expr, ArrayExpr):
                for x in expr.items:
                    walk_expr(x, local_params)

        for proc in self.procs.values():
            walk_stmt(proc)
        for ev in self.message_events:
            walk_stmt(ev)
        for param, body in self.action_entries:
            for stmt in body:
                walk_stmt(stmt, {param})

        signatures: Dict[str, Tuple[str, List[str]]] = {}
        for name, proc in self.procs.items():
            argids = [self.b.uid("arg") for _ in proc.params]
            proccode = name + (" " + " ".join(["%s" for _ in proc.params]) if proc.params else "")
            signatures[name] = (proccode, argids)
        for proc in self.procs.values():
            for param in proc.params:
                if param not in self.init_values:
                    self.b.variables.pop(param, None)
        for param in self.action_param_names:
            if param not in self.init_values:
                self.b.variables.pop(param, None)
        self.b.proc_signatures = signatures  # type: ignore[attr-defined]

    def literal_value_or_zero(self, expr: Any) -> Any:
        if isinstance(expr, Literal):
            return expr.value
        if isinstance(expr, ArrayExpr):
            return [self.literal_value_or_zero(x) for x in expr.items]
        return 0

    def compile(self) -> Dict[str, Any]:
        self.analyze()
        self.action_argid = self.b.uid("arg")

        # User procedures first so Action(Input) and message handlers can call them.
        for proc in self.procs.values():
            self.b.compile_proc_definition(proc)

        self.compile_console_flag_loop()
        self.compile_console_action_definition()
        for ev in self.message_events:
            self.compile_message_event(ev)

        asset_id = hashlib.md5(BACKDROP_SVG.encode("utf-8")).hexdigest()
        costume = {
            "name": "backdrop1",
            "dataFormat": "svg",
            "assetId": asset_id,
            "md5ext": asset_id + ".svg",
            "rotationCenterX": 240,
            "rotationCenterY": 180,
        }
        variables_obj = {vid: [name, self.init_values.get(name, 0)] for name, vid in self.b.variables.items()}
        lists_obj = {lid: [name, self.init_lists.get(name, [])] for name, lid in self.b.lists.items()}
        broadcasts_obj = {bid: name for name, bid in self.b.broadcasts.items()}
        return {
            "targets": [
                {
                    "isStage": True,
                    "name": "Stage",
                    "variables": variables_obj,
                    "lists": lists_obj,
                    "broadcasts": broadcasts_obj,
                    "blocks": self.b.blocks,
                    "comments": {},
                    "currentCostume": 0,
                    "costumes": [costume],
                    "sounds": [],
                    "volume": 100,
                    "layerOrder": 0,
                    "tempo": 60,
                    "videoTransparency": 50,
                    "videoState": "on",
                    "textToSpeechLanguage": None,
                }
            ],
            "monitors": [
                {
                    "id": TERMINAL_LIST_ID,
                    "mode": "list",
                    "opcode": "data_listcontents",
                    "params": {"LIST": TERMINAL_LIST_NAME},
                    "spriteName": None,
                    "value": self.init_lists.get(TERMINAL_LIST_NAME, []),
                    "width": 479,
                    "height": 307,
                    "x": 0,
                    "y": 0,
                    "visible": True,
                }
            ],
            "extensions": [],
            "meta": {
                "semver": "3.0.0",
                "vm": "14.0.0",
                "agent": f"StageBG/SBG {VERSION}",
            },
        }

    def compile_console_flag_loop(self) -> None:
        assert self.action_argid is not None
        hat = self.b.add_block("event_whenflagclicked", topLevel=True, x=536, y=455)
        forever = self.b.add_block("control_forever", parent=hat, inputs={})
        ask = self.b.add_block("sensing_askandwait", parent=forever, inputs={
            "QUESTION": [1, [10, ""]]
        })
        call = self.b.add_block("procedures_call", parent=ask, inputs={}, mutation={
            "tagName": "mutation",
            "children": [],
            "proccode": "Action %s",
            "argumentids": json.dumps([self.action_argid]),
            "warp": "true",
        })
        answer = self.b.add_block("sensing_answer", parent=call)
        self.b.blocks[hat]["next"] = forever
        self.b.blocks[forever]["inputs"]["SUBSTACK"] = [2, ask]
        self.b.blocks[ask]["next"] = call
        self.b.blocks[call]["inputs"][self.action_argid] = [3, answer, [10, ""]]

    def compile_console_action_definition(self) -> None:
        assert self.action_argid is not None
        display_param = self.action_entries[0][0] if self.action_entries else "Input"
        def_id = self.b.add_block("procedures_definition", topLevel=True, x=849, y=450)
        proto_id = self.b.uid()
        reporter_id = self.b.add_block(
            "argument_reporter_string_number",
            parent=proto_id,
            fields={"VALUE": [display_param, None]},
        )
        self.b.blocks[def_id]["inputs"] = {"custom_block": [2, proto_id]}
        self.b.blocks[proto_id] = {
            "opcode": "procedures_prototype",
            "next": None,
            "parent": def_id,
            "inputs": {self.action_argid: [2, reporter_id]},
            "fields": {},
            "shadow": False,
            "topLevel": False,
            "mutation": {
                "tagName": "mutation",
                "children": [],
                "proccode": "Action %s",
                "argumentids": json.dumps([self.action_argid]),
                "argumentnames": json.dumps([display_param]),
                "argumentdefaults": json.dumps([""]),
                "warp": "true",
            },
        }

        first: Optional[str] = None
        saved_params = dict(self.b.current_proc_params)
        for param, body in self.action_entries:
            self.b.current_proc_params = {param: self.action_argid}
            part = self.b.compile_statement_chain(body)
            first = self.b.chain(first, part)
        self.b.current_proc_params = saved_params
        self.b.blocks[def_id]["next"] = first
        if first:
            self.b.blocks[first]["parent"] = def_id

    def compile_message_event(self, ev: EventDecl) -> None:
        msg = ev.value or ""
        hat = self.b.add_block("event_whenbroadcastreceived", topLevel=True,
                               fields={"BROADCAST_OPTION": [msg, self.b.broadcast_id(msg)]})
        first = self.b.compile_statement_chain(ev.body)
        self.b.blocks[hat]["next"] = first
        if first:
            self.b.blocks[first]["parent"] = hat

def _input_block_refs(value: Any) -> Iterable[str]:
    """Yield block ids referenced from a Scratch input value."""
    if isinstance(value, list) and value:
        # Scratch input shapes are usually [1, primitive-or-block], [2, block],
        # or [3, block, primitive-shadow]. Primitive shadows like [10, "text"]
        # are nested lists, not ids.
        if isinstance(value[0], int):
            for item in value[1:]:
                if isinstance(item, str):
                    yield item
                elif isinstance(item, list) and item and isinstance(item[0], int) and item[0] in (1, 2, 3):
                    yield from _input_block_refs(item)
    elif isinstance(value, dict):
        for sub in value.values():
            yield from _input_block_refs(sub)

def validate_scratch_project(project: Dict[str, Any]) -> None:
    """Catch invalid/blank-looking .sb3 output before the user opens Scratch."""
    targets = project.get("targets")
    if not isinstance(targets, list) or not targets:
        raise CompileError("generated Scratch project has no targets")
    stage_targets = [t for t in targets if isinstance(t, dict) and t.get("isStage")]
    if len(stage_targets) != 1:
        raise CompileError("generated Scratch project must contain exactly one Stage target")
    stage = stage_targets[0]
    blocks = stage.get("blocks")
    if not isinstance(blocks, dict) or not blocks:
        raise CompileError("generated Scratch project contains no blocks; refusing to write an empty sb3")

    opcodes = [b.get("opcode") for b in blocks.values() if isinstance(b, dict)]
    if "event_whenflagclicked" not in opcodes:
        raise CompileError("generated Scratch project has no green-flag entrypoint")
    if "procedures_definition" not in opcodes:
        raise CompileError("generated Scratch project has no Action(Input) procedure")

    action_found = False
    for block in blocks.values():
        if not isinstance(block, dict):
            continue
        if block.get("opcode") == "procedures_prototype":
            mutation = block.get("mutation") or {}
            if mutation.get("proccode") == "Action %s":
                action_found = True
                break
    if not action_found:
        raise CompileError("generated Scratch project is missing the Action(Input) prototype")

    if TERMINAL_LIST_ID not in stage.get("lists", {}):
        raise CompileError("generated Scratch project is missing the Terminal list")
    monitor_ok = False
    for monitor in project.get("monitors", []):
        if isinstance(monitor, dict) and monitor.get("id") == TERMINAL_LIST_ID and monitor.get("visible") is True:
            monitor_ok = True
            break
    if not monitor_ok:
        raise CompileError("generated Scratch project is missing the visible fullscreen Terminal monitor")

    for bid, block in blocks.items():
        if not isinstance(block, dict):
            raise CompileError(f"generated Scratch block {bid!r} is not an object")
        nxt = block.get("next")
        if nxt is not None and nxt not in blocks:
            raise CompileError(f"generated Scratch block {bid!r} points to missing next block {nxt!r}")
        parent = block.get("parent")
        if parent is not None and parent not in blocks:
            raise CompileError(f"generated Scratch block {bid!r} points to missing parent block {parent!r}")
        for ref in _input_block_refs(block.get("inputs", {})):
            if ref not in blocks:
                raise CompileError(f"generated Scratch block {bid!r} has an input pointing to missing block {ref!r}")

def verify_sb3_file(path: Union[str, Path]) -> None:
    path = Path(path)
    try:
        with zipfile.ZipFile(path) as z:
            if "project.json" not in z.namelist():
                raise CompileError("written sb3 has no project.json")
            project = json.loads(z.read("project.json"))
    except zipfile.BadZipFile as e:
        raise CompileError(f"written sb3 is not a valid zip file: {e}") from e
    except json.JSONDecodeError as e:
        raise CompileError(f"written project.json is invalid JSON: {e}") from e
    validate_scratch_project(project)

def write_sb3_project(project: Dict[str, Any], output_path: Union[str, Path], *, verify: bool = True) -> None:
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if verify:
        validate_scratch_project(project)
    asset_id = hashlib.md5(BACKDROP_SVG.encode("utf-8")).hexdigest()
    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("project.json", json.dumps(project, ensure_ascii=False, separators=(",", ":")))
        z.writestr(asset_id + ".svg", BACKDROP_SVG)
    if verify:
        try:
            verify_sb3_file(output_path)
        except Exception:
            # Do not leave a broken file that Scratch later opens as blank.
            try:
                output_path.unlink()
            except OSError:
                pass
            raise

def compile_to_sb3(source_path: Union[str, Path], output_path: Union[str, Path], *, allow_library: bool = False, verify: bool = True) -> None:
    source_path = Path(source_path)
    program = parse_source(source_path.read_text(encoding="utf-8"), str(source_path))
    project = Compiler(program, allow_library=allow_library).compile()
    write_sb3_project(project, output_path, verify=verify)

# =============================================================================
# Tools
# =============================================================================

def inspect_sb3(path: Union[str, Path]) -> Dict[str, Any]:
    path = Path(path)
    with zipfile.ZipFile(path) as z:
        project = json.loads(z.read("project.json"))
    opcodes: Dict[str, int] = {}
    for target in project.get("targets", []):
        for block in target.get("blocks", {}).values():
            if isinstance(block, dict):
                op = block.get("opcode")
                if op:
                    opcodes[op] = opcodes.get(op, 0) + 1
    return {
        "targets": [t.get("name") for t in project.get("targets", [])],
        "stage_only": all(t.get("isStage") for t in project.get("targets", [])),
        "variables": sum(len(t.get("variables", {})) for t in project.get("targets", [])),
        "lists": sum(len(t.get("lists", {})) for t in project.get("targets", [])),
        "blocks": sum(len(t.get("blocks", {})) for t in project.get("targets", [])),
        "opcodes": dict(sorted(opcodes.items(), key=lambda kv: (-kv[1], kv[0]))),
    }

def unpack_sb3(path: Union[str, Path], out_dir: Union[str, Path]) -> None:
    out = Path(out_dir)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path) as z:
        z.extractall(out)

# =============================================================================
# Package manager
# =============================================================================

def is_url(ref: str) -> bool:
    return ref.startswith("http://") or ref.startswith("https://")

def safe_package_name(name: str) -> str:
    name = name.strip().replace(" ", "-")
    if not re.match(r"^[A-Za-z0-9_.-]+$", name):
        raise PackageError(f"invalid package name {name!r}; use letters, digits, _, . or -")
    return name

def read_json_ref(ref: Union[str, Path]) -> Dict[str, Any]:
    ref_s = str(ref)
    try:
        if is_url(ref_s):
            with urllib.request.urlopen(ref_s, timeout=20) as r:  # nosec - user provided package URL
                return json.loads(r.read().decode("utf-8"))
        return json.loads(Path(ref_s).read_text(encoding="utf-8"))
    except Exception as e:
        raise PackageError(f"cannot read JSON from {ref_s!r}: {e}") from e

def write_json(path: Path, data: Dict[str, Any]) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

def package_manifest_path(root: Union[str, Path]) -> Path:
    return Path(root) / PACKAGE_MANIFEST

def load_project_manifest(root: Union[str, Path]) -> Dict[str, Any]:
    path = package_manifest_path(root)
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            raise PackageError(f"cannot parse {path}: {e}") from e
    else:
        data = {}
    data.setdefault("name", Path(root).resolve().name)
    data.setdefault("dependencies", {})
    return data

def save_project_manifest(root: Union[str, Path], data: Dict[str, Any]) -> None:
    write_json(package_manifest_path(root), data)

def package_init(root: Union[str, Path], name: Optional[str] = None) -> Path:
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    data = load_project_manifest(root)
    if name:
        data["name"] = safe_package_name(name)
    data.setdefault("version", "0.1.0")
    data.setdefault("dependencies", {})
    save_project_manifest(root, data)
    (root / SBG_MODULES_DIR).mkdir(exist_ok=True)
    return package_manifest_path(root)

def load_registry_entry(package: str, registry: Optional[str]) -> Tuple[str, Dict[str, Any]]:
    if not registry:
        raise PackageError(f"{package!r} is not a local path/URL. Pass --registry registry.json or install from a file/folder/URL")
    registry_data = read_json_ref(registry)
    packages = registry_data.get("packages", registry_data)
    if package not in packages:
        raise PackageError(f"package {package!r} not found in registry {registry!r}")
    entry = packages[package]
    def absolutize_local_source(source: str) -> str:
        if is_url(source) or is_url(str(registry)) or Path(source).is_absolute():
            return source
        return str((Path(str(registry)).parent / source).resolve())

    if isinstance(entry, str):
        source = absolutize_local_source(entry)
        return source, {"name": package, "source": source}
    if isinstance(entry, dict):
        source = entry.get("source") or entry.get("url") or entry.get("path")
        if not source:
            raise PackageError(f"registry entry for {package!r} has no source/url/path")
        source = absolutize_local_source(str(source))
        meta = dict(entry)
        meta.setdefault("name", package)
        return source, meta
    raise PackageError(f"invalid registry entry for {package!r}")

def find_package_main(directory: Path, meta: Optional[Dict[str, Any]] = None) -> str:
    meta = meta or {}
    if meta.get("main"):
        return str(meta["main"])
    manifest = directory / PACKAGE_MANIFEST
    if manifest.is_file():
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            if data.get("main"):
                return str(data["main"])
        except Exception:
            pass
    for candidate in ("main.sbg", "index.sbg"):
        if (directory / candidate).is_file():
            return candidate
    files = sorted(directory.glob("*.sbg"))
    if files:
        return files[0].name
    raise PackageError(f"package directory {directory} contains no .sbg entry file")

def infer_package_name(source: str, explicit_name: Optional[str], meta: Optional[Dict[str, Any]] = None) -> str:
    if explicit_name:
        return safe_package_name(explicit_name)
    if meta and meta.get("name"):
        return safe_package_name(str(meta["name"]))
    if is_url(source):
        tail = source.rstrip("/").split("/")[-1]
        stem = re.sub(r"\.(zip|sbg)$", "", tail, flags=re.I) or "package"
        return safe_package_name(stem)
    return safe_package_name(Path(source).stem if Path(source).is_file() else Path(source).name)

def copy_package_dir(src: Path, dst: Path) -> None:
    def ignore(dirpath: str, names: List[str]) -> set[str]:
        banned = {".git", "__pycache__", SBG_MODULES_DIR}
        return {n for n in names if n in banned or n.endswith(".pyc")}
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst, ignore=ignore)

def install_from_directory(src: Path, root: Path, package_name: str, meta: Optional[Dict[str, Any]], source_desc: str) -> Dict[str, Any]:
    main = find_package_main(src, meta)
    dst = root / SBG_MODULES_DIR / package_name
    (root / SBG_MODULES_DIR).mkdir(exist_ok=True)
    copy_package_dir(src, dst)
    pkg_manifest = dst / PACKAGE_MANIFEST
    if pkg_manifest.is_file():
        try:
            pkg_meta = json.loads(pkg_manifest.read_text(encoding="utf-8"))
        except Exception:
            pkg_meta = {}
    else:
        pkg_meta = {}
    pkg_meta.setdefault("name", package_name)
    pkg_meta.setdefault("version", (meta or {}).get("version", "0.1.0"))
    pkg_meta["main"] = main
    write_json(pkg_manifest, pkg_meta)
    return {"name": package_name, "main": main, "source": source_desc, "path": str(dst)}

def install_from_file(src: Path, root: Path, package_name: str, meta: Optional[Dict[str, Any]], source_desc: str) -> Dict[str, Any]:
    if src.suffix != ".sbg":
        raise PackageError(f"single-file packages must be .sbg files, got {src}")
    dst = root / SBG_MODULES_DIR / package_name
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst / "main.sbg")
    pkg_meta = {"name": package_name, "version": (meta or {}).get("version", "0.1.0"), "main": "main.sbg"}
    write_json(dst / PACKAGE_MANIFEST, pkg_meta)
    return {"name": package_name, "main": "main.sbg", "source": source_desc, "path": str(dst)}

def download_to_temp(source: str) -> Tuple[tempfile.TemporaryDirectory[str], Path]:
    tmp = tempfile.TemporaryDirectory()
    tail = source.rstrip("/").split("/")[-1] or "package"
    dst = Path(tmp.name) / tail
    try:
        urllib.request.urlretrieve(source, dst)  # nosec - user provided package URL
    except Exception as e:
        tmp.cleanup()
        raise PackageError(f"download failed for {source!r}: {e}") from e
    return tmp, dst

def install_from_source(source: str, *, root: Union[str, Path] = Path.cwd(), name: Optional[str] = None, registry: Optional[str] = None) -> Dict[str, Any]:
    root = Path(root)
    package_init(root)
    meta: Dict[str, Any] = {}
    source_desc = source
    actual_source = source
    path = Path(source)

    if not is_url(source) and not path.exists():
        actual_source, meta = load_registry_entry(source, registry)
        source_desc = source
        path = Path(actual_source)

    package_name = infer_package_name(source if source_desc == source else source_desc, name, meta)

    tmp: Optional[tempfile.TemporaryDirectory[str]] = None
    try:
        if is_url(actual_source):
            tmp, downloaded = download_to_temp(actual_source)
            if zipfile.is_zipfile(downloaded):
                extract_dir = Path(tmp.name) / "extract"
                with zipfile.ZipFile(downloaded) as z:
                    z.extractall(extract_dir)
                children = [p for p in extract_dir.iterdir() if p.name not in ("__MACOSX",)]
                src_dir = children[0] if len(children) == 1 and children[0].is_dir() else extract_dir
                result = install_from_directory(src_dir, root, package_name, meta, actual_source)
            else:
                result = install_from_file(downloaded, root, package_name, meta, actual_source)
        else:
            local = path.resolve()
            if local.is_dir():
                if not meta:
                    manifest = local / PACKAGE_MANIFEST
                    if manifest.is_file():
                        try:
                            meta = json.loads(manifest.read_text(encoding="utf-8"))
                            package_name = infer_package_name(source, name, meta)
                        except Exception:
                            pass
                result = install_from_directory(local, root, package_name, meta, str(local))
            elif local.is_file():
                result = install_from_file(local, root, package_name, meta, str(local))
            else:
                raise PackageError(f"source does not exist: {source}")
    finally:
        if tmp is not None:
            tmp.cleanup()

    manifest = load_project_manifest(root)
    deps = manifest.setdefault("dependencies", {})
    deps[result["name"]] = {
        "source": result["source"],
        "main": result["main"],
        "installedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    save_project_manifest(root, manifest)
    return result

def list_packages(root: Union[str, Path]) -> List[Dict[str, Any]]:
    root = Path(root)
    manifest = load_project_manifest(root)
    modules = root / SBG_MODULES_DIR
    rows: List[Dict[str, Any]] = []
    for name, dep in sorted(manifest.get("dependencies", {}).items()):
        pkg_manifest = modules / name / PACKAGE_MANIFEST
        version = "?"
        main = dep.get("main", "main.sbg") if isinstance(dep, dict) else "main.sbg"
        if pkg_manifest.is_file():
            try:
                data = json.loads(pkg_manifest.read_text(encoding="utf-8"))
                version = str(data.get("version", version))
                main = str(data.get("main", main))
            except Exception:
                pass
        rows.append({"name": name, "version": version, "main": main, "installed": (modules / name).exists()})
    return rows

def remove_package(root: Union[str, Path], name: str) -> None:
    root = Path(root)
    name = safe_package_name(name)
    dst = root / SBG_MODULES_DIR / name
    if dst.exists():
        shutil.rmtree(dst)
    manifest = load_project_manifest(root)
    manifest.setdefault("dependencies", {}).pop(name, None)
    save_project_manifest(root, manifest)

# =============================================================================
# Native runner compatibility guard
# =============================================================================

def assert_scratch_compatible(program: Program) -> None:
    """Fail before native execution if the same program cannot compile to Scratch.

    This keeps the promise: every SBG program accepted by `sbg run` is also
    accepted by `sbg compile` for the Stage-only .sb3 target. The generated
    project is built in memory only; nothing is written to disk.
    """
    project = Compiler(program).compile()
    validate_scratch_project(project)

# =============================================================================
# CLI
# =============================================================================

def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="sbg", description="StageBG/SBG: professional text code -> Scratch .sb3 compiler")
    ap.add_argument("--version", action="version", version=f"SBG {VERSION}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    runp = sub.add_parser("run", help="run .sbg natively in the console, after verifying it can compile to Scratch")
    runp.add_argument("source")
    runp.add_argument("--fast", action="store_true", help="do not sleep on wait()")
    runp.add_argument("--input", default="", help="input value for one-shot Action(Input) execution")
    runp.add_argument("--terminal", action="store_true", help="start an interactive console loop that mirrors the generated Scratch terminal")
    runp.add_argument("--once", action="store_true", help="run exactly one Action(Input) invocation and exit; this is the default unless --terminal is used")

    comp = sub.add_parser("compile", help="compile .sbg source into a Scratch .sb3 project")
    comp.add_argument("source")
    comp.add_argument("output")
    comp.add_argument("--allow-library", action="store_true", help="allow compiling a file with no on action/on flag/top-level code")
    comp.add_argument("--no-verify", action="store_true", help="skip generated .sb3 structural verification")

    insp = sub.add_parser("inspect", help="inspect an .sb3 file and print JSON stats")
    insp.add_argument("sb3")

    unp = sub.add_parser("unpack", help="unzip an .sb3 project into a directory")
    unp.add_argument("sb3")
    unp.add_argument("out_dir")

    pkg = sub.add_parser("pkg", help="manage SBG libraries/packages")
    pkg_sub = pkg.add_subparsers(dest="pkg_cmd", required=True)

    pkg_init = pkg_sub.add_parser("init", help="create sbgpkg.json and sbg_modules/")
    pkg_init.add_argument("--name", default=None)

    pkg_install = pkg_sub.add_parser("install", help="install a package from .sbg file, folder, URL, zip URL or registry name")
    pkg_install.add_argument("source", help="local .sbg/folder, URL, or package name when --registry is used")
    pkg_install.add_argument("--name", default=None, help="override installed package name")
    pkg_install.add_argument("--registry", default=None, help="registry JSON path/URL for named packages")

    pkg_list = pkg_sub.add_parser("list", help="list installed packages")

    pkg_remove = pkg_sub.add_parser("remove", help="remove an installed package")
    pkg_remove.add_argument("name")

    args = ap.parse_args(argv)
    source_text = ""
    fallback_filename = "<source>"
    try:
        if args.cmd == "run":
            fallback_filename = args.source
            source_text = Path(args.source).read_text(encoding="utf-8")
            program = parse_source(source_text, args.source)
            assert_scratch_compatible(program)
            rt = Runtime(program, fast=args.fast, filename=args.source, source_text=source_text)
            if args.terminal:
                rt.run_scratch_terminal()
            else:
                rt.run_scratch_once(args.input)
            return 0
        if args.cmd == "compile":
            fallback_filename = args.source
            source_text = Path(args.source).read_text(encoding="utf-8")
            program = parse_source(source_text, args.source)
            project = Compiler(program, allow_library=args.allow_library).compile()
            write_sb3_project(project, args.output, verify=not args.no_verify)
            print(f"compiled: {args.output}")
            if args.allow_library:
                print("warning: compiled in --allow-library mode; Action(Input) may intentionally have no body")
            return 0
        if args.cmd == "inspect":
            print(json.dumps(inspect_sb3(args.sb3), ensure_ascii=False, indent=2))
            return 0
        if args.cmd == "unpack":
            unpack_sb3(args.sb3, args.out_dir)
            print(f"unpacked: {args.out_dir}")
            return 0
        if args.cmd == "pkg":
            root = Path.cwd()
            if args.pkg_cmd == "init":
                path = package_init(root, args.name)
                print(f"initialized: {path}")
                return 0
            if args.pkg_cmd == "install":
                result = install_from_source(args.source, root=root, name=args.name, registry=args.registry)
                print(f"installed: {result['name']} -> {result['path']} ({result['main']})")
                return 0
            if args.pkg_cmd == "list":
                rows = list_packages(root)
                if not rows:
                    print("no packages installed")
                else:
                    for row in rows:
                        status = "ok" if row["installed"] else "missing"
                        print(f"{row['name']}@{row['version']}  main={row['main']}  {status}")
                return 0
            if args.pkg_cmd == "remove":
                remove_package(root, args.name)
                print(f"removed: {args.name}")
                return 0
    except SBGError as e:
        print(format_diagnostic(e, source_text=source_text, fallback_filename=fallback_filename), file=sys.stderr)
        return 1
    except OSError as e:
        print(f"FileError: {e}", file=sys.stderr)
        return 1
    return 2


# =============================================================================
# Patch 9: compiler-level return values + sprite targets
# =============================================================================

KEYWORDS.update({"sprite", "stage"})

@dataclass
class TargetDecl:
    kind: str  # "stage" or "sprite"
    name: str
    body: List[Any]

# ---- Parser extensions -------------------------------------------------------

_old_parser_parse_top_or_stmt = Parser.parse_top_or_stmt
_old_parser_parse_block = Parser.parse_block

def _parser_parse_target_decl(self: Parser, start_token: Token, kind: str) -> TargetDecl:
    if kind == "stage":
        name = "Stage"
        # stage { ... } or stage Main { ... } / stage "Main" { ... }
        if self.peek().kind in ("IDENT", "STRING") and self.peek().value != "{":
            name = self.advance().value
    else:
        if self.peek().kind not in ("IDENT", "STRING"):
            raise self.error("expected sprite name, e.g. `sprite Worker { ... }`")
        name = self.advance().value
    body = self.parse_block()
    return self.loc(TargetDecl(kind, name, body), start_token)

def _parser_parse_top_or_stmt_patch9(self: Parser) -> Any:
    if self.match_kw("sprite"):
        return _parser_parse_target_decl(self, self.toks[self.i - 1], "sprite")
    if self.match_kw("stage"):
        return _parser_parse_target_decl(self, self.toks[self.i - 1], "stage")
    return _old_parser_parse_top_or_stmt(self)

def _parser_parse_block_patch9(self: Parser) -> List[Any]:
    self.expect("{")
    body: List[Any] = []
    while not self.at("}"):
        if self.peek().kind == "EOF":
            raise self.error("unterminated block")
        # Target bodies are real modules: they may contain events, procedures,
        # imports and normal statements. Nested sprite/stage blocks are rejected
        # later by the compiler with a normal diagnostic.
        if self.kind("KW") and self.peek().value in ("proc", "fn", "on", "import", "use", "sprite", "stage"):
            body.append(self.parse_top_or_stmt())
        else:
            body.append(self.parse_statement())
    self.expect("}")
    return body

Parser.parse_top_or_stmt = _parser_parse_top_or_stmt_patch9  # type: ignore[method-assign]
Parser.parse_block = _parser_parse_block_patch9  # type: ignore[method-assign]

# ---- Shared AST helpers ------------------------------------------------------

def _sbg_walk_stmt_tree(stmt: Any) -> Iterable[Any]:
    yield stmt
    if isinstance(stmt, TargetDecl):
        for s in stmt.body:
            yield from _sbg_walk_stmt_tree(s)
    elif isinstance(stmt, ProcDecl):
        for s in stmt.body:
            yield from _sbg_walk_stmt_tree(s)
    elif isinstance(stmt, EventDecl):
        for s in stmt.body:
            yield from _sbg_walk_stmt_tree(s)
    elif isinstance(stmt, IfStmt):
        for s in stmt.then_body:
            yield from _sbg_walk_stmt_tree(s)
        for s in stmt.else_body or []:
            yield from _sbg_walk_stmt_tree(s)
    elif isinstance(stmt, (RepeatStmt, ForeverStmt, WhileStmt)):
        for s in stmt.body:
            yield from _sbg_walk_stmt_tree(s)
    elif isinstance(stmt, ForStmt):
        if stmt.init:
            yield from _sbg_walk_stmt_tree(stmt.init)
        if stmt.update:
            yield from _sbg_walk_stmt_tree(stmt.update)
        for s in stmt.body:
            yield from _sbg_walk_stmt_tree(s)


def _sbg_stmt_has_return(stmt: Any) -> bool:
    return any(isinstance(s, ReturnStmt) for s in _sbg_walk_stmt_tree(stmt))


def _sbg_body_has_return(body: List[Any]) -> bool:
    return any(_sbg_stmt_has_return(s) for s in body)


def _sbg_clone_call(expr: CallExpr, args: List[Any]) -> CallExpr:
    out = CallExpr(expr.callee, args)
    for attr in ("filename", "line", "col"):
        if hasattr(expr, attr):
            setattr(out, attr, getattr(expr, attr))
    return out

# ---- Runtime patch: flatten sprite/stage blocks for native console mode -------

_old_runtime_prepare_scratch_console = Runtime.prepare_scratch_console
_old_runtime_call = Runtime.call

def _flatten_targets_for_runtime(program: Program) -> Program:
    body: List[Any] = []
    for stmt in program.body:
        if isinstance(stmt, TargetDecl):
            # Native mode is intentionally headless. It cannot reproduce Scratch's
            # separate sprite variable stores perfectly, but it can run the same
            # source-level code paths for quick console testing.
            body.extend(stmt.body)
        else:
            body.append(stmt)
    return Program(body)

def _runtime_prepare_scratch_console_patch9(self: Runtime) -> None:
    old = self.program
    self.program = _flatten_targets_for_runtime(old)
    try:
        _old_runtime_prepare_scratch_console(self)
    finally:
        self.program = old

def _runtime_call_patch9(self: Runtime, name: str, args: List[Any]) -> Any:
    # Keep runtime return semantics closer to Scratch codegen: params are restored,
    # target/global variables stay mutated, and only return value leaves the proc.
    if name in self.procs:
        proc = self.procs[name]
        if len(args) != len(proc.params):
            raise RuntimeSBGError(f"{name} expects {len(proc.params)} args, got {len(args)}")
        saved_params = {p: (p in self.vars, self.vars.get(p)) for p in proc.params}
        for p, a in zip(proc.params, args):
            self.vars[p] = a
        ret_value = None
        try:
            self.exec_block(proc.body)
        except ReturnSignal as r:
            ret_value = r.value
        finally:
            for p, (present, value) in saved_params.items():
                if present:
                    self.vars[p] = value
                else:
                    self.vars.pop(p, None)
        return ret_value
    return _old_runtime_call(self, name, args)

Runtime.prepare_scratch_console = _runtime_prepare_scratch_console_patch9  # type: ignore[method-assign]
Runtime.call = _runtime_call_patch9  # type: ignore[method-assign]

# ---- ScratchBuilder return/value lowering -----------------------------------

_old_builder_compile_stmt = ScratchBuilder.compile_stmt
_old_builder_compile_statement_chain = ScratchBuilder.compile_statement_chain
_old_builder_compile_call_stmt = ScratchBuilder.compile_call_stmt
_old_builder_compile_proc_definition = ScratchBuilder.compile_proc_definition
_old_builder_compile_call_expr = ScratchBuilder.compile_call_expr
_old_compiler_analyze = Compiler.analyze
_old_compiler_compile = Compiler.compile
_old_validate_scratch_project = validate_scratch_project

def _builder_ensure_patch_state(self: ScratchBuilder) -> None:
    if not hasattr(self, "proc_return_vars"):
        self.proc_return_vars = {}  # name -> (return_var, returning_flag)
    if not hasattr(self, "current_return_var"):
        self.current_return_var = None
    if not hasattr(self, "current_return_flag"):
        self.current_return_flag = None
    if not hasattr(self, "return_temp_counter"):
        self.return_temp_counter = 0

def _builder_user_proc_names(self: ScratchBuilder) -> set[str]:
    sigs = getattr(self, "proc_signatures", {})
    return set(sigs.keys())

def _builder_lower_expr(self: ScratchBuilder, expr: Any) -> Tuple[List[Any], Any]:
    """Lower expression-position procedure calls into Scratch command calls.

    Scratch custom blocks are commands, not reporter blocks. This turns:
        let x = add(2, 3);
    into roughly:
        add(2, 3);
        __tmp = __return_add;
        x = __tmp;
    before block emission. Nested calls are lowered left-to-right.
    """
    _builder_ensure_patch_state(self)
    proc_names = _builder_user_proc_names(self)

    if isinstance(expr, CallExpr):
        prelude: List[Any] = []
        lowered_args: List[Any] = []
        for arg in expr.args:
            p, lowered = _builder_lower_expr(self, arg)
            prelude.extend(p)
            lowered_args.append(lowered)
        lowered_call = _sbg_clone_call(expr, lowered_args)
        if expr.callee in proc_names:
            ret_info = getattr(self, "proc_return_vars", {}).get(expr.callee)
            if not ret_info:
                raise CompileError(
                    f"procedure {expr.callee}() is used as a value but has no `return`. "
                    "Add `return expr;` inside the proc or call it as a statement."
                )
            ret_var, _flag = ret_info
            self.return_temp_counter += 1
            temp_name = f"__sbg_tmp_{expr.callee}_{self.return_temp_counter}"
            self.var_id(temp_name)
            prelude.append(ExprStmt(lowered_call))
            prelude.append(AssignStmt(temp_name, "=", VarExpr(ret_var)))
            return prelude, VarExpr(temp_name)
        return prelude, lowered_call

    if isinstance(expr, BinaryExpr):
        p1, left = _builder_lower_expr(self, expr.left)
        p2, right = _builder_lower_expr(self, expr.right)
        out = BinaryExpr(left, expr.op, right)
        for attr in ("filename", "line", "col"):
            if hasattr(expr, attr): setattr(out, attr, getattr(expr, attr))
        return [*p1, *p2], out

    if isinstance(expr, UnaryExpr):
        p, inner = _builder_lower_expr(self, expr.expr)
        out = UnaryExpr(expr.op, inner)
        for attr in ("filename", "line", "col"):
            if hasattr(expr, attr): setattr(out, attr, getattr(expr, attr))
        return p, out

    if isinstance(expr, ArrayExpr):
        prelude: List[Any] = []
        items: List[Any] = []
        for item in expr.items:
            p, lowered = _builder_lower_expr(self, item)
            prelude.extend(p)
            items.append(lowered)
        out = ArrayExpr(items)
        for attr in ("filename", "line", "col"):
            if hasattr(expr, attr): setattr(out, attr, getattr(expr, attr))
        return prelude, out

    return [], expr


def _builder_lower_exprs(self: ScratchBuilder, args: List[Any]) -> Tuple[List[Any], List[Any]]:
    prelude: List[Any] = []
    lowered_args: List[Any] = []
    for arg in args:
        p, lowered = _builder_lower_expr(self, arg)
        prelude.extend(p)
        lowered_args.append(lowered)
    return prelude, lowered_args


def _builder_make_set_var(self: ScratchBuilder, name: str, expr: Any) -> str:
    self.var_id(name)
    bid = self.add_block("data_setvariableto", fields={"VARIABLE": [name, self.var_id(name)]}, inputs={})
    self.blocks[bid]["inputs"]["VALUE"] = self.expr_input(expr, bid)
    return bid


def _builder_make_return_guard(self: ScratchBuilder, body_first: Optional[str]) -> Optional[str]:
    _builder_ensure_patch_state(self)
    flag = getattr(self, "current_return_flag", None)
    if not flag or not body_first:
        return body_first
    cond = BinaryExpr(VarExpr(flag), "==", Literal(0))
    bid = self.add_block("control_if", inputs={})
    self.blocks[bid]["inputs"]["CONDITION"] = self.expr_input(cond, bid)
    self.blocks[bid]["inputs"]["SUBSTACK"] = self.substack_input(body_first)
    self.set_parent(body_first, bid)
    return bid


def _builder_compile_statement_chain_patch9(self: ScratchBuilder, body: List[Any]) -> Optional[str]:
    _builder_ensure_patch_state(self)
    first: Optional[str] = None
    for stmt in body:
        try:
            sid = self.compile_stmt(stmt)
            # Inside a returning procedure, every statement is conditional on the
            # compiler-level return flag. That gives early-return behavior even
            # though Scratch custom blocks cannot natively return.
            if getattr(self, "current_return_flag", None):
                sid = _builder_make_return_guard(self, sid)
        except CompileError as e:
            attach_location(e, stmt)
            raise
        except Exception as e:
            err = CompileError(str(e))
            attach_location(err, stmt)
            raise err from e
        first = self.chain(first, sid)
    return first


def _builder_compile_stmt_patch9(self: ScratchBuilder, stmt: Any) -> Optional[str]:
    _builder_ensure_patch_state(self)

    if isinstance(stmt, VarDecl):
        pre, expr = _builder_lower_expr(self, stmt.expr)
        core = _old_builder_compile_stmt(self, VarDecl(stmt.name, expr, stmt.mutable))
        return self.chain(self.compile_statement_chain(pre), core)

    if isinstance(stmt, ListDecl):
        prelude: List[Any] = []
        items: List[Any] = []
        for item in stmt.items:
            p, lowered = _builder_lower_expr(self, item)
            prelude.extend(p)
            items.append(lowered)
        core = _old_builder_compile_stmt(self, ListDecl(stmt.name, items))
        return self.chain(self.compile_statement_chain(prelude), core)

    if isinstance(stmt, AssignStmt):
        pre, expr = _builder_lower_expr(self, stmt.expr)
        core = _old_builder_compile_stmt(self, AssignStmt(stmt.name, stmt.op, expr))
        return self.chain(self.compile_statement_chain(pre), core)

    if isinstance(stmt, ExprStmt) and isinstance(stmt.expr, CallExpr):
        pre, lowered_args = _builder_lower_exprs(self, stmt.expr.args)
        core = _old_builder_compile_call_stmt(self, CallExpr(stmt.expr.callee, lowered_args))
        return self.chain(self.compile_statement_chain(pre), core)

    if isinstance(stmt, IfStmt):
        pre, cond = _builder_lower_expr(self, stmt.cond)
        core = _old_builder_compile_stmt(self, IfStmt(cond, stmt.then_body, stmt.else_body))
        return self.chain(self.compile_statement_chain(pre), core)

    if isinstance(stmt, RepeatStmt):
        pre, count = _builder_lower_expr(self, stmt.count)
        core = _old_builder_compile_stmt(self, RepeatStmt(count, stmt.body))
        return self.chain(self.compile_statement_chain(pre), core)

    if isinstance(stmt, WhileStmt):
        pre, cond = _builder_lower_expr(self, stmt.cond)
        flag = getattr(self, "current_return_flag", None)
        if flag:
            cond = BinaryExpr(cond, "&&", BinaryExpr(VarExpr(flag), "==", Literal(0)))
        core = _old_builder_compile_stmt(self, WhileStmt(cond, stmt.body))
        return self.chain(self.compile_statement_chain(pre), core)

    if isinstance(stmt, ForeverStmt):
        flag = getattr(self, "current_return_flag", None)
        if flag:
            # In returning procs, forever must become while(!returned), otherwise
            # it would keep running empty iterations forever after return.
            return _old_builder_compile_stmt(self, WhileStmt(BinaryExpr(VarExpr(flag), "==", Literal(0)), stmt.body))
        return _old_builder_compile_stmt(self, stmt)

    if isinstance(stmt, ReturnStmt):
        ret_var = getattr(self, "current_return_var", None)
        ret_flag = getattr(self, "current_return_flag", None)
        if not ret_flag:
            raise CompileError("return is only supported inside proc definitions")
        prelude: List[Any] = []
        if stmt.expr is not None and ret_var:
            p, expr = _builder_lower_expr(self, stmt.expr)
            prelude.extend(p)
            prelude.append(AssignStmt(ret_var, "=", expr))
        prelude_first = self.compile_statement_chain(prelude)
        set_flag = _builder_make_set_var(self, ret_flag, Literal(1))
        return self.chain(prelude_first, set_flag)

    return _old_builder_compile_stmt(self, stmt)


def _builder_compile_call_stmt_patch9(self: ScratchBuilder, expr: CallExpr) -> Optional[str]:
    pre, lowered_args = _builder_lower_exprs(self, expr.args)
    core = _old_builder_compile_call_stmt(self, CallExpr(expr.callee, lowered_args))
    return self.chain(self.compile_statement_chain(pre), core)


def _builder_compile_call_expr_patch9(self: ScratchBuilder, expr: CallExpr, parent: Optional[str]) -> str:
    # User procedures are command blocks. Expression-position calls should have
    # been lowered by _builder_lower_expr. If one leaks here, produce a direct
    # compiler diagnostic instead of an invalid Scratch block graph.
    if expr.callee in _builder_user_proc_names(self):
        raise CompileError(
            f"internal lowering error: procedure {expr.callee}() reached expression compiler. "
            "This is a compiler bug; try assigning the call to a variable first."
        )
    return _old_builder_compile_call_expr(self, expr, parent)


def _builder_compile_proc_definition_patch9(self: ScratchBuilder, proc: ProcDecl) -> str:
    _builder_ensure_patch_state(self)
    proccode, argids = self.proc_signatures[proc.name]  # type: ignore[attr-defined]
    def_id = self.add_block("procedures_definition", topLevel=True, x=520, y=self.y)
    proto_id = self.uid()
    self.blocks[def_id]["inputs"] = {"custom_block": [1, proto_id]}
    proto_inputs: Dict[str, Any] = {}
    for param, argid in zip(proc.params, argids):
        reporter_id = self.add_block("argument_reporter_string_number", parent=proto_id, shadow=True,
                                     fields={"VALUE": [param, None]})
        proto_inputs[argid] = [1, reporter_id]
    self.blocks[proto_id] = {
        "opcode": "procedures_prototype",
        "next": None,
        "parent": def_id,
        "inputs": proto_inputs,
        "fields": {},
        "shadow": True,
        "topLevel": False,
        "mutation": {
            "tagName": "mutation",
            "children": [],
            "proccode": proccode,
            "argumentids": json.dumps(argids),
            "argumentnames": json.dumps(proc.params),
            "argumentdefaults": json.dumps(["" for _ in proc.params]),
            "warp": "false"
        }
    }
    saved_params = dict(self.current_proc_params)
    saved_ret_var = getattr(self, "current_return_var", None)
    saved_ret_flag = getattr(self, "current_return_flag", None)
    self.current_proc_params = {p: aid for p, aid in zip(proc.params, argids)}

    ret_info = getattr(self, "proc_return_vars", {}).get(proc.name)
    if ret_info:
        self.current_return_var, self.current_return_flag = ret_info
        init_flag = _builder_make_set_var(self, self.current_return_flag, Literal(0))
        body_first = self.chain(init_flag, self.compile_statement_chain(proc.body))
    else:
        self.current_return_var = None
        self.current_return_flag = None
        body_first = self.compile_statement_chain(proc.body)

    self.current_proc_params = saved_params
    self.current_return_var = saved_ret_var
    self.current_return_flag = saved_ret_flag
    self.blocks[def_id]["next"] = body_first
    if body_first:
        self.blocks[body_first]["parent"] = def_id
    return def_id

ScratchBuilder.compile_stmt = _builder_compile_stmt_patch9  # type: ignore[method-assign]
ScratchBuilder.compile_statement_chain = _builder_compile_statement_chain_patch9  # type: ignore[method-assign]
ScratchBuilder.compile_call_stmt = _builder_compile_call_stmt_patch9  # type: ignore[method-assign]
ScratchBuilder.compile_call_expr = _builder_compile_call_expr_patch9  # type: ignore[method-assign]
ScratchBuilder.compile_proc_definition = _builder_compile_proc_definition_patch9  # type: ignore[method-assign]

# ---- Compiler patch: register return storage --------------------------------

def _register_return_vars(builder: ScratchBuilder, procs: Dict[str, ProcDecl], init_values: Optional[Dict[str, Any]] = None) -> None:
    _builder_ensure_patch_state(builder)
    builder.proc_return_vars = {}
    for name, proc in procs.items():
        if _sbg_body_has_return(proc.body):
            ret_var = f"__sbg_ret_{name}"
            flag_var = f"__sbg_returning_{name}"
            builder.proc_return_vars[name] = (ret_var, flag_var)
            builder.var_id(ret_var)
            builder.var_id(flag_var)
            if init_values is not None:
                init_values.setdefault(ret_var, "")
                init_values.setdefault(flag_var, 0)

def _compiler_analyze_patch9(self: Compiler) -> None:
    _old_compiler_analyze(self)
    _register_return_vars(self.b, self.procs, self.init_values)

Compiler.analyze = _compiler_analyze_patch9  # type: ignore[method-assign]

# ---- Sprite target compiler --------------------------------------------------

class SpriteTargetCompiler:
    def __init__(self, name: str, body: List[Any], *, layer_order: int = 1, broadcasts: Optional[Dict[str, str]] = None):
        self.name = name
        self.body = body
        self.layer_order = layer_order
        self.b = ScratchBuilder()
        # Sprite code can log() to the Stage Terminal monitor without creating
        # a duplicate sprite-local Terminal list. Scratch blocks may reference
        # the Stage/global list id from sprite targets.
        self.b.lists[TERMINAL_LIST_NAME] = TERMINAL_LIST_ID
        if broadcasts is not None:
            self.b.broadcasts = broadcasts
        self.init_values: Dict[str, Any] = {}
        self.init_lists: Dict[str, List[Any]] = {}
        self.procs: Dict[str, ProcDecl] = {}
        self.flag_events: List[EventDecl] = []
        self.message_events: List[EventDecl] = []

    def compile_error(self, message: str, node: Any = None) -> CompileError:
        err = CompileError(message)
        if node is not None:
            attach_location(err, node)
        return err

    def literal_value_or_zero(self, expr: Any) -> Any:
        if isinstance(expr, Literal):
            return expr.value
        if isinstance(expr, ArrayExpr):
            return [self.literal_value_or_zero(x) for x in expr.items]
        return 0

    def analyze(self) -> None:
        loose: List[Any] = []
        globals_seen: Dict[str, Any] = {}

        for stmt in self.body:
            if isinstance(stmt, TargetDecl):
                raise self.compile_error("nested sprite/stage blocks are not supported", stmt)
            if isinstance(stmt, VarDecl):
                if stmt.name in globals_seen:
                    raise self.compile_error(f"duplicate sprite variable/list name {stmt.name!r}", stmt)
                globals_seen[stmt.name] = stmt
                self.b.var_id(stmt.name)
                self.init_values[stmt.name] = self.literal_value_or_zero(stmt.expr)
            elif isinstance(stmt, ListDecl):
                if stmt.name in globals_seen:
                    raise self.compile_error(f"duplicate sprite variable/list name {stmt.name!r}", stmt)
                globals_seen[stmt.name] = stmt
                self.b.list_id(stmt.name)
                self.init_lists[stmt.name] = [self.literal_value_or_zero(x) for x in stmt.items]
            elif isinstance(stmt, ProcDecl):
                dup_param = Compiler.has_duplicate(stmt.params)
                if dup_param:
                    raise self.compile_error(f"duplicate parameter {dup_param!r} in {stmt.name}()", stmt)
                if stmt.name in self.procs:
                    raise self.compile_error(f"duplicate procedure {stmt.name}() in sprite {self.name}", stmt)
                self.procs[stmt.name] = stmt
            elif isinstance(stmt, EventDecl):
                if stmt.kind == "flag":
                    self.flag_events.append(stmt)
                elif stmt.kind == "message":
                    if stmt.value is not None:
                        self.b.broadcast_id(stmt.value)
                    self.message_events.append(stmt)
                elif stmt.kind == "action":
                    # Sprite-local console/action handler. It becomes a custom block
                    # named Action(input) inside the sprite; Stage does not call it
                    # automatically unless your code broadcasts/calls into it later.
                    param = stmt.value or "Input"
                    if ACTION_PROC_NAME in self.procs:
                        raise self.compile_error("sprite already defines Action(); cannot also use `on action`", stmt)
                    self.procs[ACTION_PROC_NAME] = ProcDecl(ACTION_PROC_NAME, [param], stmt.body)
            else:
                loose.append(stmt)
        if loose:
            self.flag_events.append(EventDecl("flag", None, loose))

        def walk_expr(expr: Any, local_params: Optional[set[str]] = None) -> None:
            local_params = local_params or set()
            if isinstance(expr, VarExpr):
                if expr.name in local_params:
                    return
                if expr.name not in self.b.lists:
                    self.b.var_id(expr.name)
            elif isinstance(expr, BinaryExpr):
                walk_expr(expr.left, local_params); walk_expr(expr.right, local_params)
            elif isinstance(expr, UnaryExpr):
                walk_expr(expr.expr, local_params)
            elif isinstance(expr, ArrayExpr):
                for x in expr.items: walk_expr(x, local_params)
            elif isinstance(expr, CallExpr):
                if expr.callee in ("broadcast", "broadcastAndWait") and expr.args and isinstance(expr.args[0], Literal):
                    self.b.broadcast_id(str(expr.args[0].value))
                if expr.args and isinstance(expr.args[0], VarExpr):
                    first_name = expr.args[0].name
                    if first_name not in local_params and expr.callee in ("push", "insert", "delete", "replace", "contains", "item"):
                        self.b.list_id(first_name)
                        self.b.variables.pop(first_name, None)
                    elif first_name not in local_params and expr.callee == "len" and first_name in self.b.lists:
                        self.b.variables.pop(first_name, None)
                for a in expr.args:
                    walk_expr(a, local_params)

        def walk_stmt(stmt: Any, local_params: Optional[set[str]] = None) -> None:
            local_params = local_params or set()
            if isinstance(stmt, VarDecl):
                self.b.var_id(stmt.name); walk_expr(stmt.expr, local_params)
            elif isinstance(stmt, ListDecl):
                self.b.list_id(stmt.name)
                for x in stmt.items: walk_expr(x, local_params)
            elif isinstance(stmt, AssignStmt):
                if stmt.name not in local_params:
                    self.b.var_id(stmt.name)
                walk_expr(stmt.expr, local_params)
            elif isinstance(stmt, ExprStmt):
                walk_expr(stmt.expr, local_params)
            elif isinstance(stmt, IfStmt):
                walk_expr(stmt.cond, local_params)
                for s in stmt.then_body: walk_stmt(s, local_params)
                for s in stmt.else_body or []: walk_stmt(s, local_params)
            elif isinstance(stmt, RepeatStmt):
                walk_expr(stmt.count, local_params)
                for s in stmt.body: walk_stmt(s, local_params)
            elif isinstance(stmt, ForeverStmt):
                for s in stmt.body: walk_stmt(s, local_params)
            elif isinstance(stmt, WhileStmt):
                walk_expr(stmt.cond, local_params)
                for s in stmt.body: walk_stmt(s, local_params)
            elif isinstance(stmt, ForStmt):
                if stmt.init: walk_stmt(stmt.init, local_params)
                if stmt.cond: walk_expr(stmt.cond, local_params)
                if stmt.update: walk_stmt(stmt.update, local_params)
                for s in stmt.body: walk_stmt(s, local_params)
            elif isinstance(stmt, ReturnStmt) and stmt.expr:
                walk_expr(stmt.expr, local_params)
            elif isinstance(stmt, ProcDecl):
                params = set(stmt.params)
                for s in stmt.body: walk_stmt(s, params)
            elif isinstance(stmt, EventDecl):
                params = {stmt.value or "Input"} if stmt.kind == "action" else set()
                for s in stmt.body: walk_stmt(s, params)

        for proc in self.procs.values():
            walk_stmt(proc)
        for ev in [*self.flag_events, *self.message_events]:
            walk_stmt(ev)

        signatures: Dict[str, Tuple[str, List[str]]] = {}
        for name, proc in self.procs.items():
            argids = [self.b.uid("arg") for _ in proc.params]
            proccode = name + (" " + " ".join(["%s" for _ in proc.params]) if proc.params else "")
            signatures[name] = (proccode, argids)
        for proc in self.procs.values():
            for param in proc.params:
                if param not in self.init_values:
                    self.b.variables.pop(param, None)
        self.b.proc_signatures = signatures  # type: ignore[attr-defined]
        _register_return_vars(self.b, self.procs, self.init_values)

    def compile_flag_event(self, ev: EventDecl) -> None:
        hat = self.b.add_block("event_whenflagclicked", topLevel=True)
        first = self.b.compile_statement_chain(ev.body)
        self.b.blocks[hat]["next"] = first
        if first:
            self.b.blocks[first]["parent"] = hat

    def compile_message_event(self, ev: EventDecl) -> None:
        msg = ev.value or ""
        hat = self.b.add_block("event_whenbroadcastreceived", topLevel=True,
                               fields={"BROADCAST_OPTION": [msg, self.b.broadcast_id(msg)]})
        first = self.b.compile_statement_chain(ev.body)
        self.b.blocks[hat]["next"] = first
        if first:
            self.b.blocks[first]["parent"] = hat

    def compile_target(self) -> Dict[str, Any]:
        self.analyze()
        for proc in self.procs.values():
            self.b.compile_proc_definition(proc)
        for ev in self.flag_events:
            self.compile_flag_event(ev)
        for ev in self.message_events:
            self.compile_message_event(ev)

        asset_id = hashlib.md5(BACKDROP_SVG.encode("utf-8")).hexdigest()
        costume = {
            "name": "blank",
            "bitmapResolution": 1,
            "dataFormat": "svg",
            "assetId": asset_id,
            "md5ext": asset_id + ".svg",
            "rotationCenterX": 0,
            "rotationCenterY": 0,
        }
        return {
            "isStage": False,
            "name": self.name,
            "variables": {vid: [name, self.init_values.get(name, 0)] for name, vid in self.b.variables.items()},
            "lists": {lid: [name, self.init_lists.get(name, [])] for name, lid in self.b.lists.items() if name != TERMINAL_LIST_NAME},
            "broadcasts": {bid: name for name, bid in self.b.broadcasts.items()},
            "blocks": self.b.blocks,
            "comments": {},
            "currentCostume": 0,
            "costumes": [costume],
            "sounds": [],
            "volume": 100,
            "layerOrder": self.layer_order,
            "visible": False,
            "x": 0,
            "y": 0,
            "size": 100,
            "direction": 90,
            "draggable": False,
            "rotationStyle": "all around",
        }

# ---- Multi-target project compiler ------------------------------------------

def _program_has_targets(program: Program) -> bool:
    return any(isinstance(stmt, TargetDecl) for stmt in program.body)


def _compiler_compile_patch9(self: Compiler) -> Dict[str, Any]:
    if not _program_has_targets(self.program):
        return _old_compiler_compile(self)

    stage_body: List[Any] = []
    sprite_decls: List[TargetDecl] = []
    for stmt in self.program.body:
        if isinstance(stmt, TargetDecl):
            if stmt.kind == "stage":
                stage_body.extend(stmt.body)
            elif stmt.kind == "sprite":
                sprite_decls.append(stmt)
            else:
                raise self.compile_error(f"unknown target kind {stmt.kind!r}", stmt)
        else:
            stage_body.append(stmt)

    # In multi-target mode sprites may be the only runnable targets, so the Stage
    # is allowed to be library/terminal-only.
    stage_compiler = Compiler(Program(stage_body), allow_library=True)
    project = Compiler.compile(stage_compiler)
    shared_broadcasts = stage_compiler.b.broadcasts

    seen_names = {"Stage"}
    for idx, sprite in enumerate(sprite_decls, start=1):
        if sprite.name in seen_names:
            raise self.compile_error(f"duplicate target name {sprite.name!r}", sprite)
        seen_names.add(sprite.name)
        target = SpriteTargetCompiler(sprite.name, sprite.body, layer_order=idx, broadcasts=shared_broadcasts).compile_target()
        project["targets"].append(target)

    # Refresh broadcasts on all targets so message ids added by sprites are visible.
    broadcasts_obj = {bid: name for name, bid in shared_broadcasts.items()}
    for target in project.get("targets", []):
        target["broadcasts"] = broadcasts_obj
    project.setdefault("meta", {})["agent"] = f"StageBG/SBG {VERSION}"
    return project

Compiler.compile = _compiler_compile_patch9  # type: ignore[method-assign]

# ---- Validation patch: validate all targets, not only Stage ------------------

def validate_scratch_project(project: Dict[str, Any]) -> None:  # type: ignore[no-redef]
    _old_validate_scratch_project(project)
    for target in project.get("targets", []):
        if not isinstance(target, dict):
            raise CompileError("generated Scratch target is not an object")
        blocks = target.get("blocks", {})
        if not isinstance(blocks, dict):
            raise CompileError(f"generated Scratch target {target.get('name')!r} has invalid blocks")
        for bid, block in blocks.items():
            if not isinstance(block, dict):
                raise CompileError(f"generated Scratch block {bid!r} in target {target.get('name')!r} is not an object")
            nxt = block.get("next")
            if nxt is not None and nxt not in blocks:
                raise CompileError(f"generated Scratch block {bid!r} in target {target.get('name')!r} points to missing next block {nxt!r}")
            parent = block.get("parent")
            if parent is not None and parent not in blocks:
                raise CompileError(f"generated Scratch block {bid!r} in target {target.get('name')!r} points to missing parent block {parent!r}")
            for ref in _input_block_refs(block.get("inputs", {})):
                if ref not in blocks:
                    raise CompileError(f"generated Scratch block {bid!r} in target {target.get('name')!r} has an input pointing to missing block {ref!r}")


# =============================================================================
# Patch 11: terminal echo + Action(input) return values
# =============================================================================

ACTION_RETURN_VAR = "__sbg_ret_Action"
ACTION_RETURN_FLAG = "__sbg_returning_Action"


def _sbg_action_entries_have_return(entries: List[Tuple[str, List[Any]]]) -> bool:
    return any(_sbg_body_has_return(body) for _param, body in entries)


def _builder_make_log_to_terminal(self: ScratchBuilder, expr: Any) -> str:
    self.list_id(TERMINAL_LIST_NAME)
    bid = self.add_block(
        "data_addtolist",
        fields={"LIST": [TERMINAL_LIST_NAME, self.list_id(TERMINAL_LIST_NAME)]},
        inputs={},
    )
    self.blocks[bid]["inputs"]["ITEM"] = self.expr_input(expr, bid)
    return bid


_old_compiler_analyze_patch11 = Compiler.analyze

def _compiler_analyze_patch11(self: Compiler) -> None:
    _old_compiler_analyze_patch11(self)
    if _sbg_action_entries_have_return(self.action_entries):
        self.b.var_id(ACTION_RETURN_VAR)
        self.b.var_id(ACTION_RETURN_FLAG)
        self.init_values.setdefault(ACTION_RETURN_VAR, "")
        self.init_values.setdefault(ACTION_RETURN_FLAG, 0)

Compiler.analyze = _compiler_analyze_patch11  # type: ignore[method-assign]


def _compiler_compile_console_flag_loop_patch11(self: Compiler) -> None:
    assert self.action_argid is not None
    has_action_return = _sbg_action_entries_have_return(self.action_entries)

    hat = self.b.add_block("event_whenflagclicked", topLevel=True, x=536, y=455)
    forever = self.b.add_block("control_forever", parent=hat, inputs={})
    ask = self.b.add_block("sensing_askandwait", parent=forever, inputs={
        "QUESTION": [1, [10, ">"]]
    })
    answer = self.b.add_block("sensing_answer")

    echo = _builder_make_log_to_terminal(self.b, CallExpr("join", [Literal("> "), CallExpr("answer", [])]))
    call = self.b.add_block("procedures_call", inputs={}, mutation={
        "tagName": "mutation",
        "children": [],
        "proccode": "Action %s",
        "argumentids": json.dumps([self.action_argid]),
        "warp": "true",
    })
    self.b.blocks[call]["inputs"][self.action_argid] = [3, answer, [10, ""]]
    self.b.blocks[answer]["parent"] = call

    self.b.blocks[hat]["next"] = forever
    self.b.blocks[forever]["inputs"]["SUBSTACK"] = [2, ask]
    self.b.blocks[ask]["next"] = echo
    self.b.blocks[echo]["parent"] = ask
    self.b.blocks[echo]["next"] = call
    self.b.blocks[call]["parent"] = echo

    if has_action_return:
        cond = BinaryExpr(VarExpr(ACTION_RETURN_FLAG), "==", Literal(1))
        if_ret = self.b.add_block("control_if", parent=call, inputs={})
        self.b.blocks[if_ret]["inputs"]["CONDITION"] = self.b.expr_input(cond, if_ret)
        ret_log = _builder_make_log_to_terminal(self.b, CallExpr("join", [Literal("=> "), VarExpr(ACTION_RETURN_VAR)]))
        self.b.blocks[if_ret]["inputs"]["SUBSTACK"] = self.b.substack_input(ret_log)
        self.b.set_parent(ret_log, if_ret)
        self.b.blocks[call]["next"] = if_ret

Compiler.compile_console_flag_loop = _compiler_compile_console_flag_loop_patch11  # type: ignore[method-assign]


def _compiler_compile_console_action_definition_patch11(self: Compiler) -> None:
    assert self.action_argid is not None
    display_param = self.action_entries[0][0] if self.action_entries else "Input"
    has_action_return = _sbg_action_entries_have_return(self.action_entries)

    def_id = self.b.add_block("procedures_definition", topLevel=True, x=849, y=450)
    proto_id = self.b.uid()
    reporter_id = self.b.add_block(
        "argument_reporter_string_number",
        parent=proto_id,
        fields={"VALUE": [display_param, None]},
    )
    self.b.blocks[def_id]["inputs"] = {"custom_block": [2, proto_id]}
    self.b.blocks[proto_id] = {
        "opcode": "procedures_prototype",
        "next": None,
        "parent": def_id,
        "inputs": {self.action_argid: [2, reporter_id]},
        "fields": {},
        "shadow": False,
        "topLevel": False,
        "mutation": {
            "tagName": "mutation",
            "children": [],
            "proccode": "Action %s",
            "argumentids": json.dumps([self.action_argid]),
            "argumentnames": json.dumps([display_param]),
            "argumentdefaults": json.dumps([""]),
            "warp": "true",
        },
    }

    first: Optional[str] = None
    saved_params = dict(self.b.current_proc_params)
    saved_ret_var = getattr(self.b, "current_return_var", None)
    saved_ret_flag = getattr(self.b, "current_return_flag", None)

    if has_action_return:
        self.b.current_return_var = ACTION_RETURN_VAR
        self.b.current_return_flag = ACTION_RETURN_FLAG
        init_flag = _builder_make_set_var(self.b, ACTION_RETURN_FLAG, Literal(0))
        init_ret = _builder_make_set_var(self.b, ACTION_RETURN_VAR, Literal(""))
        first = self.b.chain(first, init_flag)
        first = self.b.chain(first, init_ret)
    else:
        self.b.current_return_var = None
        self.b.current_return_flag = None

    for param, body in self.action_entries:
        self.b.current_proc_params = {param: self.action_argid}
        part = self.b.compile_statement_chain(body)
        first = self.b.chain(first, part)

    self.b.current_proc_params = saved_params
    self.b.current_return_var = saved_ret_var
    self.b.current_return_flag = saved_ret_flag

    self.b.blocks[def_id]["next"] = first
    if first:
        self.b.blocks[first]["parent"] = def_id

Compiler.compile_console_action_definition = _compiler_compile_console_action_definition_patch11  # type: ignore[method-assign]


# Native runner mirrors Scratch terminal behavior: echo the submitted command and
# print Action(input)'s return value as `=> value` when the action returns.
_OLD_NO_ACTION_RETURN = object()
_old_runtime_run_action_patch11 = Runtime.run_action

def _runtime_run_action_patch11(self: Runtime, input_value: str = "") -> Any:
    self.answer_value = input_value
    self.last_action_returned = False
    self.last_action_return_value = None
    for ev in self.action_events:
        param = ev.value or "Input"
        old_present = param in self.vars
        old_value = self.vars.get(param)
        self.vars[param] = input_value
        try:
            self.exec_block(ev.body)
        except ReturnSignal as r:
            self.last_action_returned = True
            self.last_action_return_value = r.value
            if old_present:
                self.vars[param] = old_value
            else:
                self.vars.pop(param, None)
            return r.value
        finally:
            if old_present:
                self.vars[param] = old_value
            else:
                self.vars.pop(param, None)
    return _OLD_NO_ACTION_RETURN

Runtime.run_action = _runtime_run_action_patch11  # type: ignore[method-assign]


def _runtime_terminal_echo_and_result(self: Runtime, input_value: str, result: Any) -> None:
    self.call("log", ["> " + str(input_value)])
    if getattr(self, "last_action_returned", False):
        self.call("log", ["=> " + str(getattr(self, "last_action_return_value", ""))])


def _runtime_run_scratch_once_patch11(self: Runtime, input_value: str = "") -> None:
    self.prepare_scratch_console()
    if not self.action_events:
        raise RuntimeSBGError("nothing runnable: no Action(Input) body was prepared")
    self.call("log", ["> " + str(input_value)])
    self.run_action(input_value)
    if getattr(self, "last_action_returned", False):
        self.call("log", ["=> " + str(getattr(self, "last_action_return_value", ""))])

Runtime.run_scratch_once = _runtime_run_scratch_once_patch11  # type: ignore[method-assign]


def _runtime_run_scratch_terminal_patch11(self: Runtime, *, prompt: str = "sbg> ") -> None:
    self.prepare_scratch_console()
    if not self.action_events:
        raise RuntimeSBGError("nothing runnable: no Action(Input) body was prepared")
    print("StageBG native terminal. Type /exit or press Ctrl+D to quit.")
    while True:
        try:
            line = input(prompt)
        except EOFError:
            print()
            break
        if line in ("/exit", ":q", "quit", "exit"):
            break
        self.call("log", ["> " + str(line)])
        self.run_action(line)
        if getattr(self, "last_action_returned", False):
            self.call("log", ["=> " + str(getattr(self, "last_action_return_value", ""))])

Runtime.run_scratch_terminal = _runtime_run_scratch_terminal_patch11  # type: ignore[method-assign]


# =============================================================================
# Patch 12: bigger vanilla-Scratch standard surface, motion/looks/sensing,
# faster compiler returns, and vanilla-safe optimizations
# =============================================================================

# This patch intentionally stays inside vanilla Scratch opcodes. No TurboWarp-only
# extensions, custom JS, or VM hacks are emitted.

BUILTIN_EXPR_NAMES.update({
    # strings
    "letter", "containsText",
    # math ops supported by vanilla Scratch's operator_mathop
    "sin", "cos", "tan", "asin", "acos", "atan", "ln", "log10", "exp", "pow10",
    # sensing
    "mouseX", "mouseY", "mouseDown", "keyPressed", "current", "daysSince2000", "username", "loudness",
    "distanceTo", "touching",
    # sprite reporters
    "x", "y", "direction", "size", "costumeNumber", "costumeName", "backdropNumber", "backdropName",
})
BUILTIN_STMT_NAMES.update({
    # terminal/console
    "clearTerminal", "logMany",
    # sprite motion
    "setX", "setY", "changeX", "changeY", "goToXY", "goTo", "glideToXY",
    "move", "turnRight", "turnLeft", "pointDirection", "pointTo", "ifOnEdgeBounce", "setRotationStyle",
    # looks
    "say", "sayFor", "think", "thinkFor", "show", "hide", "setSize", "changeSize",
    "setCostume", "nextCostume", "setEffect", "changeEffect", "clearEffects",
    "layerFront", "layerBack", "goForwardLayers", "goBackwardLayers",
    # clones / control
    "createClone", "deleteThisClone", "stopAll", "stopThisScript", "stopOtherScripts",
})
BUILTIN_NAMES = BUILTIN_EXPR_NAMES | BUILTIN_STMT_NAMES

_SPRITE_ONLY_STMT = {
    "setX", "setY", "changeX", "changeY", "goToXY", "goTo", "glideToXY", "move", "turnRight", "turnLeft",
    "pointDirection", "pointTo", "ifOnEdgeBounce", "setRotationStyle", "say", "sayFor", "think", "thinkFor",
    "show", "hide", "setSize", "changeSize", "setCostume", "nextCostume", "setEffect", "changeEffect", "clearEffects",
    "layerFront", "layerBack", "goForwardLayers", "goBackwardLayers", "createClone", "deleteThisClone",
}
_SPRITE_ONLY_EXPR = {"x", "y", "direction", "size", "costumeNumber", "costumeName", "distanceTo", "touching"}


def _sbg_builder_target_kind(self: ScratchBuilder) -> str:
    return getattr(self, "target_kind", "stage")


def _sbg_require_sprite_target(self: ScratchBuilder, name: str) -> None:
    if _sbg_builder_target_kind(self) != "sprite":
        raise CompileError(f"{name}() is sprite-only in vanilla Scratch. Put this code inside `sprite Name {{ ... }}`.")


def _sbg_literal_string(expr: Any, what: str) -> str:
    if isinstance(expr, Literal):
        return str(expr.value)
    raise CompileError(f"{what} must be a string literal for vanilla Scratch output")


def _sbg_bool_expr_patch12(self: ScratchBuilder, expr: Any) -> bool:
    if _old_is_boolean_expr_patch12(self, expr):
        return True
    return isinstance(expr, CallExpr) and expr.callee in {"containsText", "mouseDown", "keyPressed", "touching"}

_old_is_boolean_expr_patch12 = ScratchBuilder.is_boolean_expr
ScratchBuilder.is_boolean_expr = _sbg_bool_expr_patch12  # type: ignore[method-assign]


def _sbg_constant_eval(expr: Any) -> Tuple[bool, Any]:
    """Small compile-time constant folder for vanilla-safe optimizations."""
    try:
        if isinstance(expr, Literal):
            return True, expr.value
        if isinstance(expr, UnaryExpr):
            ok, v = _sbg_constant_eval(expr.expr)
            if not ok: return False, None
            if expr.op == "-": return True, -float(v)
            if expr.op == "!": return True, not bool(v)
        if isinstance(expr, BinaryExpr):
            ok1, a = _sbg_constant_eval(expr.left)
            ok2, b = _sbg_constant_eval(expr.right)
            if not (ok1 and ok2): return False, None
            if expr.op == "+": return True, (str(a)+str(b)) if isinstance(a, str) or isinstance(b, str) else a + b
            if expr.op == "-": return True, float(a) - float(b)
            if expr.op == "*": return True, float(a) * float(b)
            if expr.op == "/": return True, float(a) / float(b)
            if expr.op == "%": return True, float(a) % float(b)
            if expr.op == "==": return True, a == b
            if expr.op == "!=": return True, a != b
            if expr.op == "<": return True, a < b
            if expr.op == "<=": return True, a <= b
            if expr.op == ">": return True, a > b
            if expr.op == ">=": return True, a >= b
            if expr.op == "&&": return True, bool(a) and bool(b)
            if expr.op == "||": return True, bool(a) or bool(b)
        if isinstance(expr, CallExpr):
            vals = []
            for a in expr.args:
                ok, v = _sbg_constant_eval(a)
                if not ok: return False, None
                vals.append(v)
            if expr.callee == "join": return True, "".join(str(v) for v in vals)
            if expr.callee == "len" and len(vals) == 1: return True, len(vals[0])
            if expr.callee == "letter" and len(vals) == 2: return True, str(vals[0])[max(0, int(vals[1])-1):max(0, int(vals[1]))]
            if expr.callee == "containsText" and len(vals) == 2: return True, str(vals[1]) in str(vals[0])
            if expr.callee == "round" and len(vals) == 1: return True, round(float(vals[0]))
            if expr.callee == "abs" and len(vals) == 1: return True, abs(float(vals[0]))
            if expr.callee == "floor" and len(vals) == 1: return True, math.floor(float(vals[0]))
            if expr.callee == "ceil" and len(vals) == 1: return True, math.ceil(float(vals[0]))
            if expr.callee == "sqrt" and len(vals) == 1: return True, math.sqrt(float(vals[0]))
            if expr.callee == "sin" and len(vals) == 1: return True, math.sin(math.radians(float(vals[0])))
            if expr.callee == "cos" and len(vals) == 1: return True, math.cos(math.radians(float(vals[0])))
            if expr.callee == "tan" and len(vals) == 1: return True, math.tan(math.radians(float(vals[0])))
            if expr.callee == "ln" and len(vals) == 1: return True, math.log(float(vals[0]))
            if expr.callee == "log10" and len(vals) == 1: return True, math.log10(float(vals[0]))
            if expr.callee == "exp" and len(vals) == 1: return True, math.exp(float(vals[0]))
            if expr.callee == "pow10" and len(vals) == 1: return True, 10 ** float(vals[0])
    except Exception:
        return False, None
    return False, None


_old_compile_expr_patch12 = ScratchBuilder.compile_expr

def _compile_expr_patch12(self: ScratchBuilder, expr: Any, parent: Optional[str] = None) -> str:
    ok, val = _sbg_constant_eval(expr)
    # Keep booleans as boolean reporter expressions because Scratch inputs expect
    # predicates in condition slots. Number/string constants can be primitive inputs.
    if ok and not isinstance(val, bool):
        return _old_compile_expr_patch12(self, Literal(val), parent)
    return _old_compile_expr_patch12(self, expr, parent)

ScratchBuilder.compile_expr = _compile_expr_patch12  # type: ignore[method-assign]


_old_compile_call_expr_patch12 = ScratchBuilder.compile_call_expr

def _compile_call_expr_patch12(self: ScratchBuilder, expr: CallExpr, parent: Optional[str]) -> str:
    name, a = expr.callee, expr.args
    if name == "letter":
        self.need_args(name, a, 2)
        bid = self.add_block("operator_letter_of", parent=parent, inputs={})
        self.blocks[bid]["inputs"]["STRING"] = self.expr_input(a[0], bid)
        self.blocks[bid]["inputs"]["LETTER"] = self.expr_input(a[1], bid)
        return bid
    if name == "containsText":
        self.need_args(name, a, 2)
        bid = self.add_block("operator_contains", parent=parent, inputs={})
        self.blocks[bid]["inputs"]["STRING1"] = self.expr_input(a[0], bid)
        self.blocks[bid]["inputs"]["STRING2"] = self.expr_input(a[1], bid)
        return bid
    if name in ("sin", "cos", "tan", "asin", "acos", "atan", "ln", "log10", "exp", "pow10"):
        self.need_args(name, a, 1)
        op = {"log10": "log", "exp": "e ^", "pow10": "10 ^"}.get(name, name)
        bid = self.add_block("operator_mathop", parent=parent, inputs={}, fields={"OPERATOR": [op, None]})
        self.blocks[bid]["inputs"]["NUM"] = self.expr_input(a[0], bid)
        return bid
    if name in ("mouseX", "mouseY", "mouseDown", "daysSince2000", "username", "loudness"):
        self.need_args(name, a, 0)
        return self.add_block({
            "mouseX": "sensing_mousex",
            "mouseY": "sensing_mousey",
            "mouseDown": "sensing_mousedown",
            "daysSince2000": "sensing_dayssince2000",
            "username": "sensing_username",
            "loudness": "sensing_loudness",
        }[name], parent=parent)
    if name == "keyPressed":
        self.need_args(name, a, 1)
        key = _sbg_literal_string(a[0], "keyPressed key")
        bid = self.add_block("sensing_keypressed", parent=parent, inputs={})
        menu = self.add_block("sensing_keyoptions", parent=bid, shadow=True, fields={"KEY_OPTION": [key, None]})
        self.blocks[bid]["inputs"]["KEY_OPTION"] = [1, menu]
        return bid
    if name == "current":
        self.need_args(name, a, 1)
        value = _sbg_literal_string(a[0], "current() menu")
        allowed = {
            "year": "YEAR", "month": "MONTH", "date": "DATE", "dayofweek": "DAYOFWEEK",
            "dayOfWeek": "DAYOFWEEK", "hour": "HOUR", "minute": "MINUTE", "second": "SECOND",
        }
        if value not in allowed:
            raise CompileError("current() expects one of: year, month, date, dayofweek, hour, minute, second")
        return self.add_block("sensing_current", parent=parent, fields={"CURRENTMENU": [allowed[value], None]})
    if name in ("x", "y", "direction", "size", "costumeNumber", "costumeName"):
        _sbg_require_sprite_target(self, name)
        self.need_args(name, a, 0)
        if name == "x": return self.add_block("motion_xposition", parent=parent)
        if name == "y": return self.add_block("motion_yposition", parent=parent)
        if name == "direction": return self.add_block("motion_direction", parent=parent)
        if name == "size": return self.add_block("looks_size", parent=parent)
        if name == "costumeNumber": return self.add_block("looks_costumenumbername", parent=parent, fields={"NUMBER_NAME": ["number", None]})
        if name == "costumeName": return self.add_block("looks_costumenumbername", parent=parent, fields={"NUMBER_NAME": ["name", None]})
    if name in ("backdropNumber", "backdropName"):
        self.need_args(name, a, 0)
        return self.add_block("looks_backdropnumbername", parent=parent, fields={"NUMBER_NAME": ["number" if name.endswith("Number") else "name", None]})
    if name == "distanceTo":
        _sbg_require_sprite_target(self, name)
        self.need_args(name, a, 1)
        obj = _sbg_literal_string(a[0], "distanceTo target")
        bid = self.add_block("sensing_distanceto", parent=parent, inputs={})
        menu = self.add_block("sensing_distancetomenu", parent=bid, shadow=True, fields={"DISTANCETOMENU": [obj, None]})
        self.blocks[bid]["inputs"]["DISTANCETOMENU"] = [1, menu]
        return bid
    if name == "touching":
        _sbg_require_sprite_target(self, name)
        self.need_args(name, a, 1)
        obj = _sbg_literal_string(a[0], "touching target")
        bid = self.add_block("sensing_touchingobject", parent=parent, inputs={})
        menu = self.add_block("sensing_touchingobjectmenu", parent=bid, shadow=True, fields={"TOUCHINGOBJECTMENU": [obj, None]})
        self.blocks[bid]["inputs"]["TOUCHINGOBJECTMENU"] = [1, menu]
        return bid
    return _old_compile_call_expr_patch12(self, expr, parent)

ScratchBuilder.compile_call_expr = _compile_call_expr_patch12  # type: ignore[method-assign]


_old_compile_call_stmt_patch12 = ScratchBuilder.compile_call_stmt

def _sbg_menu_block(self: ScratchBuilder, opcode: str, parent: str, field: str, value: str) -> str:
    return self.add_block(opcode, parent=parent, shadow=True, fields={field: [value, None]})


def _compile_call_stmt_patch12(self: ScratchBuilder, expr: CallExpr) -> Optional[str]:
    name, a = expr.callee, expr.args
    if name == "clearTerminal":
        self.need_args(name, a, 0)
        self.list_id(TERMINAL_LIST_NAME)
        return self.add_block("data_deletealloflist", fields={"LIST": [TERMINAL_LIST_NAME, self.list_id(TERMINAL_LIST_NAME)]})
    if name == "logMany":
        val = Literal("") if not a else self.join_many(a)
        return _old_compile_call_stmt_patch12(self, CallExpr("log", [val]))
    if name in _SPRITE_ONLY_STMT:
        _sbg_require_sprite_target(self, name)
    if name == "setX":
        self.need_args(name, a, 1); bid = self.add_block("motion_setx", inputs={}); self.blocks[bid]["inputs"]["X"] = self.expr_input(a[0], bid); return bid
    if name == "setY":
        self.need_args(name, a, 1); bid = self.add_block("motion_sety", inputs={}); self.blocks[bid]["inputs"]["Y"] = self.expr_input(a[0], bid); return bid
    if name == "changeX":
        self.need_args(name, a, 1); bid = self.add_block("motion_changexby", inputs={}); self.blocks[bid]["inputs"]["DX"] = self.expr_input(a[0], bid); return bid
    if name == "changeY":
        self.need_args(name, a, 1); bid = self.add_block("motion_changeyby", inputs={}); self.blocks[bid]["inputs"]["DY"] = self.expr_input(a[0], bid); return bid
    if name == "goToXY":
        self.need_args(name, a, 2); bid = self.add_block("motion_gotoxy", inputs={}); self.blocks[bid]["inputs"]["X"] = self.expr_input(a[0], bid); self.blocks[bid]["inputs"]["Y"] = self.expr_input(a[1], bid); return bid
    if name == "glideToXY":
        self.need_args(name, a, 3); bid = self.add_block("motion_glidesecstoxy", inputs={}); self.blocks[bid]["inputs"]["SECS"] = self.expr_input(a[0], bid); self.blocks[bid]["inputs"]["X"] = self.expr_input(a[1], bid); self.blocks[bid]["inputs"]["Y"] = self.expr_input(a[2], bid); return bid
    if name == "goTo":
        self.need_args(name, a, 1); target = _sbg_literal_string(a[0], "goTo target"); bid = self.add_block("motion_goto", inputs={}); menu = _sbg_menu_block(self, "motion_goto_menu", bid, "TO", target); self.blocks[bid]["inputs"]["TO"] = [1, menu]; return bid
    if name == "move":
        self.need_args(name, a, 1); bid = self.add_block("motion_movesteps", inputs={}); self.blocks[bid]["inputs"]["STEPS"] = self.expr_input(a[0], bid); return bid
    if name == "turnRight":
        self.need_args(name, a, 1); bid = self.add_block("motion_turnright", inputs={}); self.blocks[bid]["inputs"]["DEGREES"] = self.expr_input(a[0], bid); return bid
    if name == "turnLeft":
        self.need_args(name, a, 1); bid = self.add_block("motion_turnleft", inputs={}); self.blocks[bid]["inputs"]["DEGREES"] = self.expr_input(a[0], bid); return bid
    if name == "pointDirection":
        self.need_args(name, a, 1); bid = self.add_block("motion_pointindirection", inputs={}); self.blocks[bid]["inputs"]["DIRECTION"] = self.expr_input(a[0], bid); return bid
    if name == "pointTo":
        self.need_args(name, a, 1); target = _sbg_literal_string(a[0], "pointTo target"); bid = self.add_block("motion_pointtowards", inputs={}); menu = _sbg_menu_block(self, "motion_pointtowards_menu", bid, "TOWARDS", target); self.blocks[bid]["inputs"]["TOWARDS"] = [1, menu]; return bid
    if name == "ifOnEdgeBounce":
        self.need_args(name, a, 0); return self.add_block("motion_ifonedgebounce")
    if name == "setRotationStyle":
        self.need_args(name, a, 1); style = _sbg_literal_string(a[0], "rotation style"); return self.add_block("motion_setrotationstyle", fields={"STYLE": [style, None]})
    if name == "say":
        self.need_args(name, a, 1); bid = self.add_block("looks_say", inputs={}); self.blocks[bid]["inputs"]["MESSAGE"] = self.expr_input(a[0], bid); return bid
    if name == "sayFor":
        self.need_args(name, a, 2); bid = self.add_block("looks_sayforsecs", inputs={}); self.blocks[bid]["inputs"]["MESSAGE"] = self.expr_input(a[0], bid); self.blocks[bid]["inputs"]["SECS"] = self.expr_input(a[1], bid); return bid
    if name == "think":
        self.need_args(name, a, 1); bid = self.add_block("looks_think", inputs={}); self.blocks[bid]["inputs"]["MESSAGE"] = self.expr_input(a[0], bid); return bid
    if name == "thinkFor":
        self.need_args(name, a, 2); bid = self.add_block("looks_thinkforsecs", inputs={}); self.blocks[bid]["inputs"]["MESSAGE"] = self.expr_input(a[0], bid); self.blocks[bid]["inputs"]["SECS"] = self.expr_input(a[1], bid); return bid
    if name == "show":
        self.need_args(name, a, 0); return self.add_block("looks_show")
    if name == "hide":
        self.need_args(name, a, 0); return self.add_block("looks_hide")
    if name == "setSize":
        self.need_args(name, a, 1); bid = self.add_block("looks_setsizeto", inputs={}); self.blocks[bid]["inputs"]["SIZE"] = self.expr_input(a[0], bid); return bid
    if name == "changeSize":
        self.need_args(name, a, 1); bid = self.add_block("looks_changesizeby", inputs={}); self.blocks[bid]["inputs"]["CHANGE"] = self.expr_input(a[0], bid); return bid
    if name == "setCostume":
        self.need_args(name, a, 1); bid = self.add_block("looks_switchcostumeto", inputs={}); self.blocks[bid]["inputs"]["COSTUME"] = self.expr_input(a[0], bid); return bid
    if name == "nextCostume":
        self.need_args(name, a, 0); return self.add_block("looks_nextcostume")
    if name in ("setEffect", "changeEffect"):
        self.need_args(name, a, 2); effect = _sbg_literal_string(a[0], "effect name"); opcode = "looks_seteffectto" if name == "setEffect" else "looks_changeeffectby"; inp = "VALUE" if name == "setEffect" else "CHANGE"; bid = self.add_block(opcode, inputs={}, fields={"EFFECT": [effect, None]}); self.blocks[bid]["inputs"][inp] = self.expr_input(a[1], bid); return bid
    if name == "clearEffects":
        self.need_args(name, a, 0); return self.add_block("looks_cleargraphiceffects")
    if name == "layerFront":
        self.need_args(name, a, 0); return self.add_block("looks_gotofrontback", fields={"FRONT_BACK": ["front", None]})
    if name == "layerBack":
        self.need_args(name, a, 0); return self.add_block("looks_gotofrontback", fields={"FRONT_BACK": ["back", None]})
    if name == "goForwardLayers":
        self.need_args(name, a, 1); bid = self.add_block("looks_goforwardbackwardlayers", inputs={}, fields={"FORWARD_BACKWARD": ["forward", None]}); self.blocks[bid]["inputs"]["NUM"] = self.expr_input(a[0], bid); return bid
    if name == "goBackwardLayers":
        self.need_args(name, a, 1); bid = self.add_block("looks_goforwardbackwardlayers", inputs={}, fields={"FORWARD_BACKWARD": ["backward", None]}); self.blocks[bid]["inputs"]["NUM"] = self.expr_input(a[0], bid); return bid
    if name == "createClone":
        self.need_args(name, a, 1); target = _sbg_literal_string(a[0], "clone target"); bid = self.add_block("control_create_clone_of", inputs={}); menu = _sbg_menu_block(self, "control_create_clone_of_menu", bid, "CLONE_OPTION", target); self.blocks[bid]["inputs"]["CLONE_OPTION"] = [1, menu]; return bid
    if name == "deleteThisClone":
        self.need_args(name, a, 0); return self.add_block("control_delete_this_clone")
    if name in ("stopAll", "stopThisScript", "stopOtherScripts"):
        self.need_args(name, a, 0); opt = {"stopAll": "all", "stopThisScript": "this script", "stopOtherScripts": "other scripts in sprite"}[name]; return self.add_block("control_stop", fields={"STOP_OPTION": [opt, None]})
    return _old_compile_call_stmt_patch12(self, expr)

ScratchBuilder.compile_call_stmt = _compile_call_stmt_patch12  # type: ignore[method-assign]


# Faster return: in returning procs, repeat(n) becomes while(counter>0 && !returned),
# so a return inside a long repeat does not spend time running empty guarded iterations.
_old_compile_stmt_patch12 = ScratchBuilder.compile_stmt

def _compile_stmt_return_fast_patch12(self: ScratchBuilder, stmt: Any) -> Optional[str]:
    _builder_ensure_patch_state(self)
    if isinstance(stmt, RepeatStmt) and getattr(self, "current_return_flag", None):
        self.return_temp_counter += 1
        counter = f"__sbg_repeat_left_{self.return_temp_counter}"
        init = VarDecl(counter, stmt.count, True)
        body = list(stmt.body) + [AssignStmt(counter, "-=", Literal(1))]
        cond = BinaryExpr(BinaryExpr(VarExpr(counter), ">", Literal(0)), "&&", BinaryExpr(VarExpr(self.current_return_flag), "==", Literal(0)))
        return self.chain(self.compile_stmt(init), self.compile_stmt(WhileStmt(cond, body)))
    return _old_compile_stmt_patch12(self, stmt)

ScratchBuilder.compile_stmt = _compile_stmt_return_fast_patch12  # type: ignore[method-assign]


# Make all emitted SBG procedures warp-mode by default. This is still vanilla
# Scratch: it is the built-in "run without screen refresh" custom-block flag.
_old_compile_proc_definition_patch12 = ScratchBuilder.compile_proc_definition

def _compile_proc_definition_warp_patch12(self: ScratchBuilder, proc: ProcDecl) -> str:
    bid = _old_compile_proc_definition_patch12(self, proc)
    # Patch definition prototype mutation after the normal compiler builds it.
    for block in self.blocks.values():
        if block.get("opcode") == "procedures_prototype":
            mut = block.get("mutation", {})
            if isinstance(mut, dict) and mut.get("proccode", "").startswith(proc.name):
                mut["warp"] = "true"
    return bid

ScratchBuilder.compile_proc_definition = _compile_proc_definition_warp_patch12  # type: ignore[method-assign]

_old_compile_proc_call_patch12 = ScratchBuilder.compile_proc_call

def _compile_proc_call_warp_patch12(self: ScratchBuilder, name: str, args: List[Any]) -> str:
    bid = _old_compile_proc_call_patch12(self, name, args)
    if bid in self.blocks and "mutation" in self.blocks[bid]:
        self.blocks[bid]["mutation"]["warp"] = "true"
    return bid

ScratchBuilder.compile_proc_call = _compile_proc_call_warp_patch12  # type: ignore[method-assign]


# Mark Stage vs Sprite builders for vanilla target-specific diagnostics.
_old_compiler_init_patch12 = Compiler.__init__
def _compiler_init_patch12(self: Compiler, *args: Any, **kwargs: Any) -> None:
    _old_compiler_init_patch12(self, *args, **kwargs)
    self.b.target_kind = "stage"
Compiler.__init__ = _compiler_init_patch12  # type: ignore[method-assign]

_old_sprite_compiler_init_patch12 = SpriteTargetCompiler.__init__
def _sprite_compiler_init_patch12(self: SpriteTargetCompiler, *args: Any, **kwargs: Any) -> None:
    _old_sprite_compiler_init_patch12(self, *args, **kwargs)
    self.b.target_kind = "sprite"
SpriteTargetCompiler.__init__ = _sprite_compiler_init_patch12  # type: ignore[method-assign]


# Native runner state for sprite/motion APIs. This is headless but deterministic,
# so code using these functions can be smoke-tested before compiling to .sb3.
_old_runtime_call_patch12 = Runtime.call

def _runtime_state(self: Runtime) -> Dict[str, Any]:
    st = getattr(self, "_sbg_native_sprite_state", None)
    if st is None:
        st = {"x": 0.0, "y": 0.0, "direction": 90.0, "size": 100.0, "visible": True, "costume": 1, "backdrop": 1}
        self._sbg_native_sprite_state = st
    return st


def _runtime_call_patch12(self: Runtime, name: str, args: List[Any]) -> Any:
    st = _runtime_state(self)
    if name == "clearTerminal":
        self.lists.setdefault(TERMINAL_LIST_NAME, []).clear(); return None
    if name == "logMany":
        return self.call("log", ["".join(str(x) for x in args)])
    if name == "letter": return str(args[0])[max(0, int(args[1])-1):max(0, int(args[1]))]
    if name == "containsText": return str(args[1]) in str(args[0])
    if name in ("sin", "cos", "tan"):
        return getattr(math, name)(math.radians(float(args[0])))
    if name in ("asin", "acos", "atan"):
        return math.degrees(getattr(math, name)(float(args[0])))
    if name == "ln": return math.log(float(args[0]))
    if name == "log10": return math.log10(float(args[0]))
    if name == "exp": return math.exp(float(args[0]))
    if name == "pow10": return 10 ** float(args[0])
    if name == "setX": st["x"] = float(args[0]); return None
    if name == "setY": st["y"] = float(args[0]); return None
    if name == "changeX": st["x"] += float(args[0]); return None
    if name == "changeY": st["y"] += float(args[0]); return None
    if name == "goToXY": st["x"], st["y"] = float(args[0]), float(args[1]); return None
    if name == "move":
        steps = float(args[0]); rad = math.radians(90 - st["direction"]); st["x"] += math.cos(rad)*steps; st["y"] += math.sin(rad)*steps; return None
    if name == "turnRight": st["direction"] += float(args[0]); return None
    if name == "turnLeft": st["direction"] -= float(args[0]); return None
    if name == "pointDirection": st["direction"] = float(args[0]); return None
    if name in ("goTo", "glideToXY", "pointTo", "ifOnEdgeBounce", "setRotationStyle", "say", "sayFor", "think", "thinkFor", "setCostume", "nextCostume", "setEffect", "changeEffect", "clearEffects", "layerFront", "layerBack", "goForwardLayers", "goBackwardLayers", "createClone", "deleteThisClone", "stopThisScript", "stopOtherScripts"):
        # Visual/VM-only in headless native mode.
        return None
    if name == "show": st["visible"] = True; return None
    if name == "hide": st["visible"] = False; return None
    if name == "setSize": st["size"] = float(args[0]); return None
    if name == "changeSize": st["size"] += float(args[0]); return None
    if name == "stopAll": raise StopIteration("stopAll")
    if name == "x": return st["x"]
    if name == "y": return st["y"]
    if name == "direction": return st["direction"]
    if name == "size": return st["size"]
    if name in ("costumeNumber", "backdropNumber"): return st["costume" if name.startswith("costume") else "backdrop"]
    if name in ("costumeName", "backdropName"): return str(st["costume" if name.startswith("costume") else "backdrop"])
    if name == "mouseX": return 0
    if name == "mouseY": return 0
    if name == "mouseDown": return False
    if name == "keyPressed": return False
    if name == "current":
        import datetime as _dt
        now = _dt.datetime.now()
        m = str(args[0]); return {"year": now.year, "month": now.month, "date": now.day, "dayofweek": now.isoweekday(), "dayOfWeek": now.isoweekday(), "hour": now.hour, "minute": now.minute, "second": now.second}.get(m, 0)
    if name == "daysSince2000":
        import datetime as _dt
        return (_dt.datetime.now() - _dt.datetime(2000,1,1)).total_seconds()/86400
    if name == "username": return "native"
    if name == "loudness": return 0
    if name == "distanceTo": return math.sqrt(st["x"]*st["x"] + st["y"]*st["y"])
    if name == "touching": return False
    return _old_runtime_call_patch12(self, name, args)

Runtime.call = _runtime_call_patch12  # type: ignore[method-assign]



# Patch 12b: make new statement builtins visible even through patch9's lowering path.
_old_compile_stmt_patch12b = ScratchBuilder.compile_stmt

def _compile_stmt_patch12b(self: ScratchBuilder, stmt: Any) -> Optional[str]:
    if isinstance(stmt, ExprStmt) and isinstance(stmt.expr, CallExpr) and stmt.expr.callee in BUILTIN_STMT_NAMES:
        pre, lowered_args = _builder_lower_exprs(self, stmt.expr.args)
        core = self.compile_call_stmt(CallExpr(stmt.expr.callee, lowered_args))
        return self.chain(self.compile_statement_chain(pre), core)
    return _old_compile_stmt_patch12b(self, stmt)

ScratchBuilder.compile_stmt = _compile_stmt_patch12b  # type: ignore[method-assign]


# Patch 12c: resolve imports recursively inside stage/sprite/proc/event bodies.
def _sbg_resolve_body_recursive(self: ImportResolver, body: List[Any], current_file: Path) -> List[Any]:
    out: List[Any] = []
    for stmt in body:
        if isinstance(stmt, ImportDecl):
            try:
                imported = self.load_import(stmt.spec, current_file)
                out.extend(imported.body)
            except ImportSBGError as e:
                attach_location(e, stmt)
                raise
            continue
        if isinstance(stmt, TargetDecl):
            stmt.body = _sbg_resolve_body_recursive(self, stmt.body, current_file)
        elif isinstance(stmt, ProcDecl):
            stmt.body = _sbg_resolve_body_recursive(self, stmt.body, current_file)
        elif isinstance(stmt, EventDecl):
            stmt.body = _sbg_resolve_body_recursive(self, stmt.body, current_file)
        elif isinstance(stmt, BlockStmt):
            stmt.body = _sbg_resolve_body_recursive(self, stmt.body, current_file)
        elif isinstance(stmt, IfStmt):
            stmt.then_body = _sbg_resolve_body_recursive(self, stmt.then_body, current_file)
            if stmt.else_body is not None:
                stmt.else_body = _sbg_resolve_body_recursive(self, stmt.else_body, current_file)
        elif isinstance(stmt, (RepeatStmt, ForeverStmt, WhileStmt)):
            stmt.body = _sbg_resolve_body_recursive(self, stmt.body, current_file)
        elif isinstance(stmt, ForStmt):
            stmt.body = _sbg_resolve_body_recursive(self, stmt.body, current_file)
        out.append(stmt)
    return out

def _import_resolve_program_patch12(self: ImportResolver, program: Program, current_file: Path) -> Program:
    return Program(_sbg_resolve_body_recursive(self, program.body, current_file))

ImportResolver.resolve_program = _import_resolve_program_patch12  # type: ignore[method-assign]


# Patch 12d: allow the same library file to be imported into multiple targets.
# Scratch has separate block/workspace storage per target, so a library imported
# into Stage must also be imported into a sprite if the sprite wants those procs.
def _import_load_import_patch12(self: ImportResolver, spec: str, current_file: Path) -> Program:
    path = self.resolve_import_path(spec, current_file)
    if path in self.stack:
        chain = " -> ".join(str(p) for p in [*self.stack, path])
        raise ImportSBGError(f"circular import detected: {chain}")
    self.stack.append(path)
    try:
        text = path.read_text(encoding="utf-8")
        self.source_cache[str(path)] = text
        program = Parser(Lexer(text, str(path)).tokens(), str(path)).parse()
        return self.resolve_program(program, path)
    except OSError as e:
        raise ImportSBGError(str(e)) from e
    finally:
        if self.stack and self.stack[-1] == path:
            self.stack.pop()

ImportResolver.load_import = _import_load_import_patch12  # type: ignore[method-assign]

# =============================================================================
# Patch 13: professional stdlib surface, Pen extension, compile-time file embeds
# =============================================================================

# StageBG now treats Scratch as a VM target, not as a toy UI.  The compiler still
# emits vanilla Scratch 3.0 JSON only: no TurboWarp-only opcodes, no JS, no custom
# extensions except Scratch's official Pen extension.

EMBEDDED_FILE_LIST_NAMES = [
    "__sbg_file_names",
    "__sbg_file_texts",
    "__sbg_file_sizes",
    "__sbg_file_line_start",
    "__sbg_file_line_count",
    "__sbg_file_lines",
]
EMBEDDED_FILE_LIST_IDS = {
    "__sbg_file_names": "sbg_files_names_v1",
    "__sbg_file_texts": "sbg_files_texts_v1",
    "__sbg_file_sizes": "sbg_files_sizes_v1",
    "__sbg_file_line_start": "sbg_files_line_start_v1",
    "__sbg_file_line_count": "sbg_files_line_count_v1",
    "__sbg_file_lines": "sbg_files_lines_v1",
}

# Builtins added in patch13.  They are intentionally low-level VM bindings; the
# higher-level API lives in packages/std/*.sbg.
BUILTIN_EXPR_NAMES.update({
    "touchingColor", "colorTouchingColor",
})
BUILTIN_STMT_NAMES.update({
    # list/data/monitor control
    "clearList", "deleteAll", "showVariable", "hideVariable", "showList", "hideList",
    # clone/control aliases
    "stop", "createCloneOf", "setDragMode",
    # pen extension, vanilla Scratch official extension
    "penClear", "clearPen", "penEraseAll", "penDown", "penUp", "penStamp",
    "penSetColor", "penSetSize", "penChangeSize",
    "penSetParam", "penChangeParam", "penSetHue", "penChangeHue",
    "penSetSaturation", "penChangeSaturation", "penSetBrightness", "penChangeBrightness",
    "penSetTransparency", "penChangeTransparency",
})
BUILTIN_NAMES = BUILTIN_EXPR_NAMES | BUILTIN_STMT_NAMES

# Stable ids let sprite targets read Stage-embedded files through the same global
# list ids.  Without this, each sprite would generate empty sprite-local file lists.
_old_list_id_patch13 = ScratchBuilder.list_id

def _list_id_patch13(self: ScratchBuilder, name: str) -> str:
    if name in EMBEDDED_FILE_LIST_IDS:
        if name not in self.lists:
            self.lists[name] = EMBEDDED_FILE_LIST_IDS[name]
        return self.lists[name]
    return _old_list_id_patch13(self, name)

ScratchBuilder.list_id = _list_id_patch13  # type: ignore[method-assign]


def _sbg_hex_color(value: str) -> str:
    value = str(value).strip()
    if re.fullmatch(r"#[0-9A-Fa-f]{6}", value):
        return value.lower()
    if re.fullmatch(r"[0-9A-Fa-f]{6}", value):
        return "#" + value.lower()
    named = {
        "black": "#000000", "white": "#ffffff", "red": "#ff0000", "green": "#00ff00", "blue": "#0000ff",
        "yellow": "#ffff00", "cyan": "#00ffff", "magenta": "#ff00ff", "orange": "#ff8800",
        "purple": "#8844ff", "gray": "#808080", "grey": "#808080",
    }
    if value.lower() in named:
        return named[value.lower()]
    raise CompileError(f"invalid color {value!r}; use '#rrggbb' or a known color name")


def _sbg_color_input(self: ScratchBuilder, expr: Any, parent: str) -> Any:
    if isinstance(expr, Literal):
        return [1, [9, _sbg_hex_color(str(expr.value))]]
    # Scratch color sockets accept reporter blocks too, although the UI normally
    # shows a color picker shadow. This keeps dynamic colors possible.
    return self.expr_input(expr, parent)


def _sbg_var_or_list_name(expr: Any, what: str) -> str:
    if isinstance(expr, VarExpr):
        return expr.name
    if isinstance(expr, Literal):
        return str(expr.value)
    raise CompileError(f"{what} needs a variable/list name, e.g. {what}(score) or {what}(\"score\")")


def _sbg_pen_param(expr: Any) -> str:
    if not isinstance(expr, Literal):
        raise CompileError("pen color parameter must be a string literal")
    raw = str(expr.value).strip().lower()
    aliases = {
        "color": "color", "colour": "color", "hue": "color",
        "saturation": "saturation", "sat": "saturation",
        "brightness": "brightness", "bright": "brightness", "value": "brightness",
        "transparency": "transparency", "alpha": "transparency",
    }
    if raw not in aliases:
        raise CompileError("pen parameter must be color/hue, saturation, brightness or transparency")
    return aliases[raw]


_old_bool_expr_patch13 = ScratchBuilder.is_boolean_expr

def _bool_expr_patch13(self: ScratchBuilder, expr: Any) -> bool:
    return _old_bool_expr_patch13(self, expr) or (isinstance(expr, CallExpr) and expr.callee in {"touchingColor", "colorTouchingColor"})

ScratchBuilder.is_boolean_expr = _bool_expr_patch13  # type: ignore[method-assign]


_old_compile_call_expr_patch13 = ScratchBuilder.compile_call_expr

def _compile_call_expr_patch13(self: ScratchBuilder, expr: CallExpr, parent: Optional[str]) -> str:
    name, a = expr.callee, expr.args
    if name == "touchingColor":
        _sbg_require_sprite_target(self, name)
        self.need_args(name, a, 1)
        bid = self.add_block("sensing_touchingcolor", parent=parent, inputs={})
        self.blocks[bid]["inputs"]["COLOR"] = _sbg_color_input(self, a[0], bid)
        return bid
    if name == "colorTouchingColor":
        _sbg_require_sprite_target(self, name)
        self.need_args(name, a, 2)
        bid = self.add_block("sensing_coloristouchingcolor", parent=parent, inputs={})
        self.blocks[bid]["inputs"]["COLOR"] = _sbg_color_input(self, a[0], bid)
        self.blocks[bid]["inputs"]["COLOR2"] = _sbg_color_input(self, a[1], bid)
        return bid
    return _old_compile_call_expr_patch13(self, expr, parent)

ScratchBuilder.compile_call_expr = _compile_call_expr_patch13  # type: ignore[method-assign]


_old_compile_call_stmt_patch13 = ScratchBuilder.compile_call_stmt

def _compile_call_stmt_patch13(self: ScratchBuilder, expr: CallExpr) -> Optional[str]:
    name, a = expr.callee, expr.args

    if name in ("clearList", "deleteAll"):
        self.need_args(name, a, 1)
        lst = self.require_list_expr(a[0])
        return self.add_block("data_deletealloflist", fields={"LIST": [lst, self.list_id(lst)]})

    if name in ("showVariable", "hideVariable"):
        self.need_args(name, a, 1)
        var_name = _sbg_var_or_list_name(a[0], name)
        self.var_id(var_name)
        return self.add_block("data_showvariable" if name == "showVariable" else "data_hidevariable",
                              fields={"VARIABLE": [var_name, self.var_id(var_name)]})

    if name in ("showList", "hideList"):
        self.need_args(name, a, 1)
        list_name = _sbg_var_or_list_name(a[0], name)
        self.list_id(list_name)
        return self.add_block("data_showlist" if name == "showList" else "data_hidelist",
                              fields={"LIST": [list_name, self.list_id(list_name)]})

    if name == "stop":
        self.need_args(name, a, 1)
        mode = _sbg_literal_string(a[0], "stop mode").strip().lower()
        allowed = {
            "all": "all",
            "this": "this script",
            "this script": "this script",
            "other": "other scripts in sprite",
            "others": "other scripts in sprite",
            "other scripts": "other scripts in sprite",
            "other scripts in sprite": "other scripts in sprite",
        }
        if mode not in allowed:
            raise CompileError("stop() expects 'all', 'this script' or 'other scripts in sprite'")
        chosen = allowed[mode]
        # Scratch wants hasnext=false for all/this script, true for other scripts.
        return self.add_block("control_stop", fields={"STOP_OPTION": [chosen, None]}, mutation={
            "tagName": "mutation", "children": [], "hasnext": "true" if chosen == "other scripts in sprite" else "false"
        })

    if name == "createCloneOf":
        _sbg_require_sprite_target(self, name)
        self.need_args(name, a, 1)
        target = _sbg_literal_string(a[0], "createCloneOf target")
        bid = self.add_block("control_create_clone_of", inputs={})
        menu = self.add_block("control_create_clone_of_menu", parent=bid, shadow=True, fields={"CLONE_OPTION": [target, None]})
        self.blocks[bid]["inputs"]["CLONE_OPTION"] = [1, menu]
        return bid

    if name == "setDragMode":
        _sbg_require_sprite_target(self, name)
        self.need_args(name, a, 1)
        mode = _sbg_literal_string(a[0], "setDragMode mode").strip().lower()
        if mode in ("drag", "draggable", "true", "1"):
            mode = "draggable"
        elif mode in ("no", "not", "not draggable", "false", "0"):
            mode = "not draggable"
        else:
            raise CompileError("setDragMode() expects 'draggable' or 'not draggable'")
        return self.add_block("sensing_setdragmode", fields={"DRAG_MODE": [mode, None]})

    # Pen extension. pen_clear is VM-global; the actual drawing blocks are sprite-only.
    if name in ("penClear", "clearPen", "penEraseAll"):
        self.need_args(name, a, 0)
        return self.add_block("pen_clear")
    if name == "penDown":
        _sbg_require_sprite_target(self, name); self.need_args(name, a, 0); return self.add_block("pen_penDown")
    if name == "penUp":
        _sbg_require_sprite_target(self, name); self.need_args(name, a, 0); return self.add_block("pen_penUp")
    if name == "penStamp":
        _sbg_require_sprite_target(self, name); self.need_args(name, a, 0); return self.add_block("pen_stamp")
    if name == "penSetColor":
        _sbg_require_sprite_target(self, name); self.need_args(name, a, 1)
        bid = self.add_block("pen_setPenColorToColor", inputs={})
        self.blocks[bid]["inputs"]["COLOR"] = _sbg_color_input(self, a[0], bid)
        return bid
    if name == "penSetSize":
        _sbg_require_sprite_target(self, name); self.need_args(name, a, 1)
        bid = self.add_block("pen_setPenSizeTo", inputs={}); self.blocks[bid]["inputs"]["SIZE"] = self.expr_input(a[0], bid); return bid
    if name == "penChangeSize":
        _sbg_require_sprite_target(self, name); self.need_args(name, a, 1)
        bid = self.add_block("pen_changePenSizeBy", inputs={}); self.blocks[bid]["inputs"]["SIZE"] = self.expr_input(a[0], bid); return bid
    if name in ("penSetParam", "penChangeParam"):
        _sbg_require_sprite_target(self, name); self.need_args(name, a, 2)
        param = _sbg_pen_param(a[0])
        bid = self.add_block("pen_setPenColorParamTo" if name == "penSetParam" else "pen_changePenColorParamBy",
                             fields={"COLOR_PARAM": [param, None]}, inputs={})
        self.blocks[bid]["inputs"]["VALUE"] = self.expr_input(a[1], bid)
        return bid
    pen_aliases = {
        "penSetHue": ("penSetParam", "color"), "penChangeHue": ("penChangeParam", "color"),
        "penSetSaturation": ("penSetParam", "saturation"), "penChangeSaturation": ("penChangeParam", "saturation"),
        "penSetBrightness": ("penSetParam", "brightness"), "penChangeBrightness": ("penChangeParam", "brightness"),
        "penSetTransparency": ("penSetParam", "transparency"), "penChangeTransparency": ("penChangeParam", "transparency"),
    }
    if name in pen_aliases:
        _sbg_require_sprite_target(self, name); self.need_args(name, a, 1)
        base, param = pen_aliases[name]
        return _compile_call_stmt_patch13(self, CallExpr(base, [Literal(param), a[0]]))

    return _old_compile_call_stmt_patch13(self, expr)

ScratchBuilder.compile_call_stmt = _compile_call_stmt_patch13  # type: ignore[method-assign]


# Patch compiler expression walker to recognize new list builtins and file lists.
# This keeps `clearList(myList)` and file stdlib references as real lists, not variables.
_old_compiler_analyze_patch13 = Compiler.analyze

def _compiler_analyze_patch13(self: Compiler) -> None:
    _old_compiler_analyze_patch13(self)
    # Always make embedded file tables real Stage lists. This is necessary because
    # `len(__sbg_file_names)` must compile to data_lengthoflist even before any
    # `item(__sbg_file_names, i)` is encountered. Empty tables are cheap.
    for name in EMBEDDED_FILE_LIST_NAMES:
        self.b.list_id(name)
        self.init_lists.setdefault(name, [])

Compiler.analyze = _compiler_analyze_patch13  # type: ignore[method-assign]

_old_sprite_analyze_patch13 = SpriteTargetCompiler.analyze
def _sprite_analyze_patch13(self: SpriteTargetCompiler) -> None:
    _old_sprite_analyze_patch13(self)
    for name in EMBEDDED_FILE_LIST_NAMES:
        self.b.list_id(name)
        self.init_lists.setdefault(name, [])
SpriteTargetCompiler.analyze = _sprite_analyze_patch13  # type: ignore[method-assign]


_old_sprite_compile_target_patch13 = SpriteTargetCompiler.compile_target

def _sprite_compile_target_patch13(self: SpriteTargetCompiler) -> Dict[str, Any]:
    target = _old_sprite_compile_target_patch13(self)
    # Embedded file lists are Stage-global and share deterministic ids. Do not
    # create duplicate empty sprite-local lists with the same names.
    target["lists"] = {
        lid: pair for lid, pair in target.get("lists", {}).items()
        if pair and pair[0] not in EMBEDDED_FILE_LIST_NAMES
    }
    return target

SpriteTargetCompiler.compile_target = _sprite_compile_target_patch13  # type: ignore[method-assign]


_old_validate_patch13 = validate_scratch_project

def validate_scratch_project(project: Dict[str, Any]) -> None:  # type: ignore[no-redef]
    _old_validate_patch13(project)
    # Loading the official Pen extension is vanilla Scratch.  Ordinary sprite-local
    # variables/lists may coincidentally reuse ids across targets because Scratch
    # stores them per target. Only embedded file tables are required to be Stage-
    # global and must not appear as sprite-local duplicates.
    embedded_ids = set(EMBEDDED_FILE_LIST_IDS.values())
    stage_seen: set[str] = set()
    for target in project.get("targets", []):
        for lid, pair in (target.get("lists") or {}).items():
            if target.get("isStage") and lid in embedded_ids:
                stage_seen.add(lid)
            if (not target.get("isStage")) and lid in embedded_ids:
                raise CompileError(f"embedded file list {pair[0] if pair else lid!r} leaked into sprite-local lists")


def _project_ensure_patch13(project: Dict[str, Any]) -> Dict[str, Any]:
    # Always include Pen. It does not add files/assets; it only tells vanilla
    # Scratch to load the official extension when the project opens.
    exts = project.setdefault("extensions", [])
    if "pen" not in exts:
        exts.append("pen")
    project.setdefault("meta", {})["agent"] = f"StageBG/SBG {VERSION}"
    return project

_old_compiler_compile_patch13 = Compiler.compile

def _compiler_compile_patch13(self: Compiler) -> Dict[str, Any]:
    return _project_ensure_patch13(_old_compiler_compile_patch13(self))

Compiler.compile = _compiler_compile_patch13  # type: ignore[method-assign]


def _make_literal(value: Any) -> Literal:
    return Literal(value)


def _make_list_decl(name: str, values: List[Any]) -> ListDecl:
    return ListDecl(name, [_make_literal(v) for v in values])


def _parse_embed_ref(ref: str, base: Path) -> Tuple[Path, str]:
    # Accept either local=virtual or local:virtual.  local=virtual is safer for
    # paths that contain colons, but local:virtual is convenient on Linux.
    if "=" in ref:
        left, right = ref.split("=", 1)
        path = Path(left)
        virtual = right
    elif ":" in ref and not re.match(r"^[A-Za-z]:[\\/]", ref):
        left, right = ref.split(":", 1)
        path = Path(left)
        virtual = right
    else:
        path = Path(ref)
        virtual = path.name
    if not path.is_absolute():
        path = base / path
    virtual = virtual.strip().replace("\\", "/").lstrip("/")
    if not virtual:
        virtual = path.name
    return path.resolve(), virtual


def _collect_embedded_files(source_path: Union[str, Path], refs: Optional[List[str]] = None, dirs: Optional[List[str]] = None) -> List[Tuple[str, str]]:
    base = Path(source_path).resolve().parent if source_path else Path.cwd()
    files: List[Tuple[Path, str]] = []
    for ref in refs or []:
        p, name = _parse_embed_ref(ref, base)
        if not p.is_file():
            raise CompileError(f"embedded file does not exist or is not a file: {p}")
        files.append((p, name))
    for dref in dirs or []:
        dpath = Path(dref)
        if not dpath.is_absolute():
            dpath = base / dpath
        dpath = dpath.resolve()
        if not dpath.is_dir():
            raise CompileError(f"embedded directory does not exist: {dpath}")
        for p in sorted(x for x in dpath.rglob("*") if x.is_file()):
            rel = p.relative_to(dpath).as_posix()
            files.append((p, rel))

    seen: set[str] = set()
    result: List[Tuple[str, str]] = []
    for p, virtual in files:
        if virtual in seen:
            raise CompileError(f"duplicate embedded virtual file name {virtual!r}")
        seen.add(virtual)
        try:
            text = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            raise CompileError(f"cannot read embedded file {p}: {e}") from e
        # Scratch list items are strings. Keep \n in the text cell and also expose
        # a separate flat line table for efficient line access.
        result.append((virtual, text))
    return result


def _program_with_embedded_files(program: Program, source_path: Union[str, Path], *, embeds: Optional[List[str]] = None, embed_dirs: Optional[List[str]] = None) -> Program:
    embedded = _collect_embedded_files(source_path, embeds, embed_dirs)
    names: List[str] = []
    texts: List[str] = []
    sizes: List[int] = []
    starts: List[int] = []
    counts: List[int] = []
    all_lines: List[str] = []
    next_line = 1  # Scratch lists are 1-indexed
    for virtual, text in embedded:
        lines = text.splitlines()
        if text.endswith("\n"):
            # splitlines() intentionally drops the final blank line; keep line
            # based indexing readable by not adding a synthetic empty line.
            pass
        names.append(virtual)
        texts.append(text)
        sizes.append(len(text))
        starts.append(next_line)
        counts.append(len(lines))
        all_lines.extend(lines)
        next_line += len(lines)
    decls = [
        _make_list_decl("__sbg_file_names", names),
        _make_list_decl("__sbg_file_texts", texts),
        _make_list_decl("__sbg_file_sizes", sizes),
        _make_list_decl("__sbg_file_line_start", starts),
        _make_list_decl("__sbg_file_line_count", counts),
        _make_list_decl("__sbg_file_lines", all_lines),
    ]
    # Put file tables at the very front so libraries can use them immediately.
    return Program([*decls, *program.body])


_old_runtime_prepare_patch13 = Runtime.prepare_scratch_console

def _runtime_prepare_patch13(self: Runtime) -> None:
    _old_runtime_prepare_patch13(self)
    for name in EMBEDDED_FILE_LIST_NAMES:
        self.lists.setdefault(name, [])

Runtime.prepare_scratch_console = _runtime_prepare_patch13  # type: ignore[method-assign]


_old_runtime_call_patch13 = Runtime.call

def _runtime_call_patch13(self: Runtime, name: str, args: List[Any]) -> Any:
    st = _runtime_state(self)
    if name in ("clearList", "deleteAll"):
        self.get_list_arg(args[0], require_name=True).clear(); return None
    if name in ("showVariable", "hideVariable", "showList", "hideList", "setDragMode"):
        return None
    if name == "stop":
        mode = str(args[0]).lower() if args else "all"
        if "all" in mode: raise StopIteration("stop all")
        return None
    if name == "createCloneOf": return None
    if name in ("penClear", "clearPen", "penEraseAll", "penDown", "penUp", "penStamp", "penSetColor", "penSetSize", "penChangeSize", "penSetParam", "penChangeParam", "penSetHue", "penChangeHue", "penSetSaturation", "penChangeSaturation", "penSetBrightness", "penChangeBrightness", "penSetTransparency", "penChangeTransparency"):
        # Headless native mode cannot draw; keep deterministic no-op semantics so
        # code remains smoke-testable before compiling to Scratch.
        st.setdefault("pen", {})[name] = args
        return None
    if name == "touchingColor": return False
    if name == "colorTouchingColor": return False
    return _old_runtime_call_patch13(self, name, args)

Runtime.call = _runtime_call_patch13  # type: ignore[method-assign]


# Replacement CLI with file embedding flags.  It intentionally mirrors the old
# CLI but adds --embed and --embed-dir to both run and compile so native execution
# and Scratch execution see the same compile-time file tables.
def main(argv: Optional[List[str]] = None) -> int:  # type: ignore[no-redef]
    ap = argparse.ArgumentParser(prog="sbg", description="StageBG/SBG: professional text code -> vanilla Scratch .sb3 compiler")
    ap.add_argument("--version", action="version", version=f"SBG {VERSION}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    runp = sub.add_parser("run", help="run .sbg natively after verifying it can compile to Scratch")
    runp.add_argument("source")
    runp.add_argument("--fast", action="store_true", help="do not sleep on wait()")
    runp.add_argument("--input", default="", help="input value for one-shot Action(Input) execution")
    runp.add_argument("--terminal", action="store_true", help="start an interactive console loop that mirrors the generated Scratch terminal")
    runp.add_argument("--once", action="store_true", help="run exactly one Action(Input) invocation and exit; default unless --terminal is used")
    runp.add_argument("--embed", action="append", default=[], help="embed text file at compile/run time: path[:virtual/name] or path=virtual/name")
    runp.add_argument("--embed-dir", action="append", default=[], help="embed every text file from a directory recursively")

    comp = sub.add_parser("compile", help="compile .sbg source into a vanilla Scratch .sb3 project")
    comp.add_argument("source")
    comp.add_argument("output")
    comp.add_argument("--allow-library", action="store_true", help="allow compiling a file with no on action/on flag/top-level code")
    comp.add_argument("--no-verify", action="store_true", help="skip generated .sb3 structural verification")
    comp.add_argument("--embed", action="append", default=[], help="embed text file into Scratch lists: path[:virtual/name] or path=virtual/name")
    comp.add_argument("--embed-dir", action="append", default=[], help="embed every text file from a directory recursively")

    insp = sub.add_parser("inspect", help="inspect an .sb3 file and print JSON stats")
    insp.add_argument("sb3")

    unp = sub.add_parser("unpack", help="unzip an .sb3 project into a directory")
    unp.add_argument("sb3")
    unp.add_argument("out_dir")

    pkg = sub.add_parser("pkg", help="manage SBG libraries/packages")
    pkg_sub = pkg.add_subparsers(dest="pkg_cmd", required=True)
    pkg_init = pkg_sub.add_parser("init", help="create sbgpkg.json and sbg_modules/")
    pkg_init.add_argument("--name", default=None)
    pkg_install = pkg_sub.add_parser("install", help="install a package from .sbg file, folder, URL, zip URL or registry name")
    pkg_install.add_argument("source", help="local .sbg/folder, URL, or package name when --registry is used")
    pkg_install.add_argument("--name", default=None, help="override installed package name")
    pkg_install.add_argument("--registry", default=None, help="registry JSON path/URL for named packages")
    pkg_sub.add_parser("list", help="list installed packages")
    pkg_remove = pkg_sub.add_parser("remove", help="remove an installed package")
    pkg_remove.add_argument("name")

    args = ap.parse_args(argv)
    source_text = ""
    fallback_filename = "<source>"
    try:
        if args.cmd == "run":
            fallback_filename = args.source
            source_text = Path(args.source).read_text(encoding="utf-8")
            program = parse_source(source_text, args.source)
            program = _program_with_embedded_files(program, args.source, embeds=args.embed, embed_dirs=args.embed_dir)
            assert_scratch_compatible(program)
            rt = Runtime(program, fast=args.fast, filename=args.source, source_text=source_text)
            if args.terminal:
                rt.run_scratch_terminal()
            else:
                rt.run_scratch_once(args.input)
            return 0
        if args.cmd == "compile":
            fallback_filename = args.source
            source_text = Path(args.source).read_text(encoding="utf-8")
            program = parse_source(source_text, args.source)
            program = _program_with_embedded_files(program, args.source, embeds=args.embed, embed_dirs=args.embed_dir)
            project = Compiler(program, allow_library=args.allow_library).compile()
            write_sb3_project(project, args.output, verify=not args.no_verify)
            print(f"compiled: {args.output}")
            if args.allow_library:
                print("warning: compiled in --allow-library mode; Action(Input) may intentionally have no body")
            return 0
        if args.cmd == "inspect":
            print(json.dumps(inspect_sb3(args.sb3), ensure_ascii=False, indent=2))
            return 0
        if args.cmd == "unpack":
            unpack_sb3(args.sb3, args.out_dir)
            print(f"unpacked: {args.out_dir}")
            return 0
        if args.cmd == "pkg":
            root = Path.cwd()
            if args.pkg_cmd == "init":
                path = package_init(root, args.name)
                print(f"initialized: {path}")
                return 0
            if args.pkg_cmd == "install":
                result = install_from_source(args.source, root=root, name=args.name, registry=args.registry)
                print(f"installed: {result['name']} -> {result['path']} ({result['main']})")
                return 0
            if args.pkg_cmd == "list":
                rows = list_packages(root)
                if not rows:
                    print("no packages installed")
                else:
                    for row in rows:
                        status = "ok" if row["installed"] else "missing"
                        print(f"{row['name']}@{row['version']}  main={row['main']}  {status}")
                return 0
            if args.pkg_cmd == "remove":
                remove_package(root, args.name)
                print(f"removed: {args.name}")
                return 0
    except SBGError as e:
        print(format_diagnostic(e, source_text=source_text, fallback_filename=fallback_filename), file=sys.stderr)
        return 1
    except OSError as e:
        print(f"FileError: {e}", file=sys.stderr)
        return 1
    return 2



# =============================================================================
# Patch 14: default Turbo/Warp runtime + delta-time-safe standard loop helpers
# =============================================================================

VERSION = "0.9.0-patch24-keyboard"

# Vanilla Scratch cannot store the editor's Shift-click Turbo Mode inside .sb3.
# StageBG therefore treats "turbo" as a compiler/runtime policy:
#   - every generated custom block is warp=true by default;
#   - Action(Input) runs as a warp custom block;
#   - delta-time bookkeeping is done with direct Scratch variable blocks, not waits;
#   - native run defaults to the same zero-screen-refresh semantics, while wait()
#     remains real unless the user explicitly passes --fast.

SBG_NOW_VAR = "__sbg_now"
SBG_LAST_VAR = "__sbg_last"
SBG_RAW_DT_VAR = "__sbg_raw_dt"
SBG_DT_VAR = "__sbg_dt"
SBG_DT_SCALE_VAR = "__sbg_dt_scale"
SBG_DT_CAP_VAR = "__sbg_dt_cap"
SBG_FIXED_DT_VAR = "__sbg_fixed_dt"
SBG_FRAME_VAR = "__sbg_frame"
SBG_FPS_VAR = "__sbg_fps"
SBG_TURBO_VAR = "__sbg_turbo"

SBG_DELTA_VARS = {
    SBG_NOW_VAR: 0,
    SBG_LAST_VAR: 0,
    SBG_RAW_DT_VAR: 0,
    SBG_DT_VAR: 0,
    SBG_DT_SCALE_VAR: 1,
    SBG_DT_CAP_VAR: 0.25,
    SBG_FIXED_DT_VAR: 0,
    SBG_FRAME_VAR: 0,
    SBG_FPS_VAR: 0,
    SBG_TURBO_VAR: 1,
}

BUILTIN_EXPR_NAMES.update({
    "dt", "deltaTime", "rawDeltaTime", "fps", "frame", "timeSeconds", "isTurbo",
})
BUILTIN_STMT_NAMES.update({
    "tick", "frameStart", "updateDelta", "resetDelta", "setFixedDelta", "useRealDelta",
    "setDeltaScale", "setDeltaCap", "setTurbo", "turboOn", "turboOff",
})
BUILTIN_NAMES = BUILTIN_EXPR_NAMES | BUILTIN_STMT_NAMES


def _sbg_uses_delta_expr(expr: Any) -> bool:
    if isinstance(expr, CallExpr):
        if expr.callee in {"dt", "deltaTime", "rawDeltaTime", "fps", "frame", "timeSeconds"}:
            return True
        return any(_sbg_uses_delta_expr(a) for a in expr.args)
    if isinstance(expr, BinaryExpr):
        return _sbg_uses_delta_expr(expr.left) or _sbg_uses_delta_expr(expr.right)
    if isinstance(expr, UnaryExpr):
        return _sbg_uses_delta_expr(expr.expr)
    if isinstance(expr, ArrayExpr):
        return any(_sbg_uses_delta_expr(x) for x in expr.items)
    return False


def _sbg_uses_delta_stmt(stmt: Any) -> bool:
    if isinstance(stmt, VarDecl): return _sbg_uses_delta_expr(stmt.expr)
    if isinstance(stmt, ListDecl): return any(_sbg_uses_delta_expr(x) for x in stmt.items)
    if isinstance(stmt, AssignStmt): return _sbg_uses_delta_expr(stmt.expr)
    if isinstance(stmt, ExprStmt):
        return isinstance(stmt.expr, CallExpr) and (stmt.expr.callee in {"tick", "frameStart", "updateDelta"} or _sbg_uses_delta_expr(stmt.expr))
    if isinstance(stmt, IfStmt):
        return _sbg_uses_delta_expr(stmt.cond) or any(_sbg_uses_delta_stmt(s) for s in stmt.then_body) or any(_sbg_uses_delta_stmt(s) for s in (stmt.else_body or []))
    if isinstance(stmt, RepeatStmt): return _sbg_uses_delta_expr(stmt.count) or any(_sbg_uses_delta_stmt(s) for s in stmt.body)
    if isinstance(stmt, WhileStmt): return _sbg_uses_delta_expr(stmt.cond) or any(_sbg_uses_delta_stmt(s) for s in stmt.body)
    if isinstance(stmt, ForeverStmt): return any(_sbg_uses_delta_stmt(s) for s in stmt.body)
    if isinstance(stmt, ForStmt):
        return (stmt.init is not None and _sbg_uses_delta_stmt(stmt.init)) or (stmt.cond is not None and _sbg_uses_delta_expr(stmt.cond)) or (stmt.update is not None and _sbg_uses_delta_stmt(stmt.update)) or any(_sbg_uses_delta_stmt(s) for s in stmt.body)
    if isinstance(stmt, ReturnStmt): return stmt.expr is not None and _sbg_uses_delta_expr(stmt.expr)
    if isinstance(stmt, ProcDecl): return any(_sbg_uses_delta_stmt(s) for s in stmt.body)
    if isinstance(stmt, EventDecl): return any(_sbg_uses_delta_stmt(s) for s in stmt.body)
    return False


def _sbg_body_uses_delta(body: List[Any]) -> bool:
    return any(_sbg_uses_delta_stmt(s) for s in body)


def _sbg_set_var_block(self: ScratchBuilder, name: str, expr: Any) -> str:
    self.var_id(name)
    bid = self.add_block("data_setvariableto", fields={"VARIABLE": [name, self.var_id(name)]}, inputs={})
    self.blocks[bid]["inputs"]["VALUE"] = self.expr_input(expr, bid)
    return bid


def _sbg_if_block(self: ScratchBuilder, cond: Any, body_first: Optional[str]) -> str:
    bid = self.add_block("control_if", inputs={})
    self.blocks[bid]["inputs"]["CONDITION"] = self.expr_input(cond, bid)
    self.blocks[bid]["inputs"]["SUBSTACK"] = self.substack_input(body_first)
    self.set_parent(body_first, bid)
    return bid


def _sbg_if_else_block(self: ScratchBuilder, cond: Any, then_first: Optional[str], else_first: Optional[str]) -> str:
    bid = self.add_block("control_if_else", inputs={})
    self.blocks[bid]["inputs"]["CONDITION"] = self.expr_input(cond, bid)
    self.blocks[bid]["inputs"]["SUBSTACK"] = self.substack_input(then_first)
    self.blocks[bid]["inputs"]["SUBSTACK2"] = self.substack_input(else_first)
    self.set_parent(then_first, bid)
    self.set_parent(else_first, bid)
    return bid


def _sbg_compile_delta_reset(self: ScratchBuilder) -> Optional[str]:
    first: Optional[str] = None
    now_timer = CallExpr("timer", [])
    first = self.chain(first, _sbg_set_var_block(self, SBG_NOW_VAR, now_timer))
    first = self.chain(first, _sbg_set_var_block(self, SBG_LAST_VAR, VarExpr(SBG_NOW_VAR)))
    first = self.chain(first, _sbg_set_var_block(self, SBG_RAW_DT_VAR, Literal(0)))
    first = self.chain(first, _sbg_set_var_block(self, SBG_DT_VAR, Literal(0)))
    first = self.chain(first, _sbg_set_var_block(self, SBG_FPS_VAR, Literal(0)))
    first = self.chain(first, _sbg_set_var_block(self, SBG_FRAME_VAR, Literal(0)))
    first = self.chain(first, _sbg_set_var_block(self, SBG_TURBO_VAR, Literal(1)))
    return first


def _sbg_compile_delta_tick(self: ScratchBuilder) -> Optional[str]:
    first: Optional[str] = None
    # now = timer
    first = self.chain(first, _sbg_set_var_block(self, SBG_NOW_VAR, CallExpr("timer", [])))
    # raw_dt = now - last
    first = self.chain(first, _sbg_set_var_block(self, SBG_RAW_DT_VAR, BinaryExpr(VarExpr(SBG_NOW_VAR), "-", VarExpr(SBG_LAST_VAR))))
    # if raw_dt < 0: raw_dt = 0  (handles resetTimer() / project reload safely)
    first = self.chain(first, _sbg_if_block(self, BinaryExpr(VarExpr(SBG_RAW_DT_VAR), "<", Literal(0)), _sbg_set_var_block(self, SBG_RAW_DT_VAR, Literal(0))))
    # if dt_cap > 0 && raw_dt > dt_cap: raw_dt = dt_cap
    cap_cond = BinaryExpr(BinaryExpr(VarExpr(SBG_DT_CAP_VAR), ">", Literal(0)), "&&", BinaryExpr(VarExpr(SBG_RAW_DT_VAR), ">", VarExpr(SBG_DT_CAP_VAR)))
    first = self.chain(first, _sbg_if_block(self, cap_cond, _sbg_set_var_block(self, SBG_RAW_DT_VAR, VarExpr(SBG_DT_CAP_VAR))))
    # if fixed_dt > 0: raw_dt = fixed_dt
    fixed_cond = BinaryExpr(VarExpr(SBG_FIXED_DT_VAR), ">", Literal(0))
    first = self.chain(first, _sbg_if_block(self, fixed_cond, _sbg_set_var_block(self, SBG_RAW_DT_VAR, VarExpr(SBG_FIXED_DT_VAR))))
    # dt = raw_dt * scale
    first = self.chain(first, _sbg_set_var_block(self, SBG_DT_VAR, BinaryExpr(VarExpr(SBG_RAW_DT_VAR), "*", VarExpr(SBG_DT_SCALE_VAR))))
    # last = now
    first = self.chain(first, _sbg_set_var_block(self, SBG_LAST_VAR, VarExpr(SBG_NOW_VAR)))
    # frame += 1
    first = self.chain(first, _sbg_set_var_block(self, SBG_FRAME_VAR, BinaryExpr(VarExpr(SBG_FRAME_VAR), "+", Literal(1))))
    # fps = raw_dt > 0 ? 1/raw_dt : 0
    fps_then = _sbg_set_var_block(self, SBG_FPS_VAR, BinaryExpr(Literal(1), "/", VarExpr(SBG_RAW_DT_VAR)))
    fps_else = _sbg_set_var_block(self, SBG_FPS_VAR, Literal(0))
    first = self.chain(first, _sbg_if_else_block(self, BinaryExpr(VarExpr(SBG_RAW_DT_VAR), ">", Literal(0)), fps_then, fps_else))
    return first


_old_compile_call_expr_patch14 = ScratchBuilder.compile_call_expr

def _compile_call_expr_patch14(self: ScratchBuilder, expr: CallExpr, parent: Optional[str]) -> str:
    name, a = expr.callee, expr.args
    reporter_var = {
        "dt": SBG_DT_VAR,
        "deltaTime": SBG_DT_VAR,
        "rawDeltaTime": SBG_RAW_DT_VAR,
        "fps": SBG_FPS_VAR,
        "frame": SBG_FRAME_VAR,
        "isTurbo": SBG_TURBO_VAR,
    }.get(name)
    if reporter_var is not None:
        self.need_args(name, a, 0)
        self.var_id(reporter_var)
        return self.add_block("data_variable", parent=parent, fields={"VARIABLE": [reporter_var, self.var_id(reporter_var)]})
    if name == "timeSeconds":
        self.need_args(name, a, 0)
        return self.add_block("sensing_timer", parent=parent)
    return _old_compile_call_expr_patch14(self, expr, parent)

ScratchBuilder.compile_call_expr = _compile_call_expr_patch14  # type: ignore[method-assign]


_old_compile_call_stmt_patch14 = ScratchBuilder.compile_call_stmt

def _compile_call_stmt_patch14(self: ScratchBuilder, expr: CallExpr) -> Optional[str]:
    name, a = expr.callee, expr.args
    if name in ("tick", "frameStart", "updateDelta"):
        self.need_args(name, a, 0)
        return _sbg_compile_delta_tick(self)
    if name == "resetDelta":
        self.need_args(name, a, 0)
        return _sbg_compile_delta_reset(self)
    if name == "setFixedDelta":
        self.need_args(name, a, 1)
        return _sbg_set_var_block(self, SBG_FIXED_DT_VAR, a[0])
    if name == "useRealDelta":
        self.need_args(name, a, 0)
        return _sbg_set_var_block(self, SBG_FIXED_DT_VAR, Literal(0))
    if name == "setDeltaScale":
        self.need_args(name, a, 1)
        return _sbg_set_var_block(self, SBG_DT_SCALE_VAR, a[0])
    if name == "setDeltaCap":
        self.need_args(name, a, 1)
        return _sbg_set_var_block(self, SBG_DT_CAP_VAR, a[0])
    if name == "setTurbo":
        self.need_args(name, a, 1)
        return _sbg_set_var_block(self, SBG_TURBO_VAR, a[0])
    if name == "turboOn":
        self.need_args(name, a, 0)
        return _sbg_set_var_block(self, SBG_TURBO_VAR, Literal(1))
    if name == "turboOff":
        self.need_args(name, a, 0)
        return _sbg_set_var_block(self, SBG_TURBO_VAR, Literal(0))
    return _old_compile_call_stmt_patch14(self, expr)

ScratchBuilder.compile_call_stmt = _compile_call_stmt_patch14  # type: ignore[method-assign]


_old_compile_stmt_patch14 = ScratchBuilder.compile_stmt

def _compile_stmt_patch14(self: ScratchBuilder, stmt: Any) -> Optional[str]:
    # Delta-time-safe loops: if a loop body reads dt()/fps()/frame(), update once
    # per loop iteration. This is explicit in generated Scratch blocks and uses no wait.
    if isinstance(stmt, ForeverStmt) and _sbg_body_uses_delta(stmt.body):
        return _old_compile_stmt_patch14(self, ForeverStmt([ExprStmt(CallExpr("tick", [])), *stmt.body]))
    if isinstance(stmt, WhileStmt) and (_sbg_uses_delta_expr(stmt.cond) or _sbg_body_uses_delta(stmt.body)):
        return _old_compile_stmt_patch14(self, WhileStmt(stmt.cond, [ExprStmt(CallExpr("tick", [])), *stmt.body]))
    if isinstance(stmt, RepeatStmt) and _sbg_body_uses_delta(stmt.body):
        return _old_compile_stmt_patch14(self, RepeatStmt(stmt.count, [ExprStmt(CallExpr("tick", [])), *stmt.body]))
    if isinstance(stmt, ForStmt) and _sbg_body_uses_delta(stmt.body):
        return _old_compile_stmt_patch14(self, ForStmt(stmt.init, stmt.cond, stmt.update, [ExprStmt(CallExpr("tick", [])), *stmt.body]))
    return _old_compile_stmt_patch14(self, stmt)

ScratchBuilder.compile_stmt = _compile_stmt_patch14  # type: ignore[method-assign]


_old_compiler_analyze_patch14 = Compiler.analyze

def _compiler_analyze_patch14(self: Compiler) -> None:
    _old_compiler_analyze_patch14(self)
    # Allocate runtime variables even when dt() appears only inside lowered code.
    # They are ordinary Scratch variables, so this is vanilla and cheap.
    for name, value in SBG_DELTA_VARS.items():
        self.b.var_id(name)
        self.init_values.setdefault(name, value)

Compiler.analyze = _compiler_analyze_patch14  # type: ignore[method-assign]


_old_sprite_analyze_patch14 = SpriteTargetCompiler.analyze

def _sprite_analyze_patch14(self: SpriteTargetCompiler) -> None:
    _old_sprite_analyze_patch14(self)
    for name, value in SBG_DELTA_VARS.items():
        self.b.var_id(name)
        self.init_values.setdefault(name, value)

SpriteTargetCompiler.analyze = _sprite_analyze_patch14  # type: ignore[method-assign]


# Rebuild the terminal scripts with a real delta reset and an automatic frame tick
# at the start of Action(Input). This keeps native run and Scratch output aligned.
def _compiler_compile_console_flag_loop_patch14(self: Compiler) -> None:
    assert self.action_argid is not None
    has_action_return = _sbg_action_entries_have_return(self.action_entries)

    hat = self.b.add_block("event_whenflagclicked", topLevel=True, x=536, y=455)
    reset = _sbg_compile_delta_reset(self.b)
    forever = self.b.add_block("control_forever", inputs={})
    ask = self.b.add_block("sensing_askandwait", parent=forever, inputs={
        "QUESTION": [1, [10, ">"]]
    })
    answer = self.b.add_block("sensing_answer")

    echo = _builder_make_log_to_terminal(self.b, CallExpr("join", [Literal("> "), CallExpr("answer", [])]))
    call = self.b.add_block("procedures_call", inputs={}, mutation={
        "tagName": "mutation",
        "children": [],
        "proccode": "Action %s",
        "argumentids": json.dumps([self.action_argid]),
        "warp": "true",
    })
    self.b.blocks[call]["inputs"][self.action_argid] = [3, answer, [10, ""]]
    self.b.blocks[answer]["parent"] = call

    self.b.blocks[hat]["next"] = reset or forever
    if reset:
        # Append the forever loop after the reset chain.
        last = reset
        while self.b.blocks[last].get("next"):
            last = self.b.blocks[last]["next"]
        self.b.blocks[last]["next"] = forever
        self.b.blocks[forever]["parent"] = last
    else:
        self.b.blocks[forever]["parent"] = hat
    self.b.blocks[forever]["inputs"]["SUBSTACK"] = [2, ask]
    self.b.blocks[ask]["next"] = echo
    self.b.blocks[echo]["parent"] = ask
    self.b.blocks[echo]["next"] = call
    self.b.blocks[call]["parent"] = echo

    if has_action_return:
        cond = BinaryExpr(VarExpr(ACTION_RETURN_FLAG), "==", Literal(1))
        if_ret = self.b.add_block("control_if", parent=call, inputs={})
        self.b.blocks[if_ret]["inputs"]["CONDITION"] = self.b.expr_input(cond, if_ret)
        ret_log = _builder_make_log_to_terminal(self.b, CallExpr("join", [Literal("=> "), VarExpr(ACTION_RETURN_VAR)]))
        self.b.blocks[if_ret]["inputs"]["SUBSTACK"] = self.b.substack_input(ret_log)
        self.b.set_parent(ret_log, if_ret)
        self.b.blocks[call]["next"] = if_ret

Compiler.compile_console_flag_loop = _compiler_compile_console_flag_loop_patch14  # type: ignore[method-assign]


def _compiler_compile_console_action_definition_patch14(self: Compiler) -> None:
    assert self.action_argid is not None
    display_param = self.action_entries[0][0] if self.action_entries else "Input"
    has_action_return = _sbg_action_entries_have_return(self.action_entries)

    def_id = self.b.add_block("procedures_definition", topLevel=True, x=849, y=450)
    proto_id = self.b.uid()
    reporter_id = self.b.add_block(
        "argument_reporter_string_number",
        parent=proto_id,
        fields={"VALUE": [display_param, None]},
    )
    self.b.blocks[def_id]["inputs"] = {"custom_block": [2, proto_id]}
    self.b.blocks[proto_id] = {
        "opcode": "procedures_prototype",
        "next": None,
        "parent": def_id,
        "inputs": {self.action_argid: [2, reporter_id]},
        "fields": {},
        "shadow": False,
        "topLevel": False,
        "mutation": {
            "tagName": "mutation",
            "children": [],
            "proccode": "Action %s",
            "argumentids": json.dumps([self.action_argid]),
            "argumentnames": json.dumps([display_param]),
            "argumentdefaults": json.dumps([""]),
            "warp": "true",
        },
    }

    first: Optional[str] = _sbg_compile_delta_tick(self.b)
    saved_params = dict(self.b.current_proc_params)
    saved_ret_var = getattr(self.b, "current_return_var", None)
    saved_ret_flag = getattr(self.b, "current_return_flag", None)

    if has_action_return:
        self.b.current_return_var = ACTION_RETURN_VAR
        self.b.current_return_flag = ACTION_RETURN_FLAG
        first = self.b.chain(first, _builder_make_set_var(self.b, ACTION_RETURN_FLAG, Literal(0)))
        first = self.b.chain(first, _builder_make_set_var(self.b, ACTION_RETURN_VAR, Literal("")))
    else:
        self.b.current_return_var = None
        self.b.current_return_flag = None

    for param, body in self.action_entries:
        self.b.current_proc_params = {param: self.action_argid}
        part = self.b.compile_statement_chain(body)
        first = self.b.chain(first, part)

    self.b.current_proc_params = saved_params
    self.b.current_return_var = saved_ret_var
    self.b.current_return_flag = saved_ret_flag

    self.b.blocks[def_id]["next"] = first
    if first:
        self.b.blocks[first]["parent"] = def_id

Compiler.compile_console_action_definition = _compiler_compile_console_action_definition_patch14  # type: ignore[method-assign]


_old_sprite_compile_flag_patch14 = SpriteTargetCompiler.compile_flag_event

def _sprite_compile_flag_event_patch14(self: SpriteTargetCompiler, ev: EventDecl) -> None:
    # Sprite flag events get a cheap reset before user code. Loops using dt() are
    # handled by the compile_stmt loop injection above.
    hat = self.b.add_block("event_whenflagclicked", topLevel=True)
    reset = _sbg_compile_delta_reset(self.b)
    first = self.b.chain(reset, self.b.compile_statement_chain(ev.body))
    self.b.blocks[hat]["next"] = first
    if first:
        self.b.blocks[first]["parent"] = hat

SpriteTargetCompiler.compile_flag_event = _sprite_compile_flag_event_patch14  # type: ignore[method-assign]


_old_sprite_compile_message_patch14 = SpriteTargetCompiler.compile_message_event

def _sprite_compile_message_event_patch14(self: SpriteTargetCompiler, ev: EventDecl) -> None:
    msg = ev.value or ""
    hat = self.b.add_block("event_whenbroadcastreceived", topLevel=True,
                           fields={"BROADCAST_OPTION": [msg, self.b.broadcast_id(msg)]})
    tick = _sbg_compile_delta_tick(self.b)
    first = self.b.chain(tick, self.b.compile_statement_chain(ev.body))
    self.b.blocks[hat]["next"] = first
    if first:
        self.b.blocks[first]["parent"] = hat

SpriteTargetCompiler.compile_message_event = _sprite_compile_message_event_patch14  # type: ignore[method-assign]


# Native delta runtime ---------------------------------------------------------

def _runtime_ensure_delta_state(self: Runtime) -> None:
    for name, value in SBG_DELTA_VARS.items():
        self.vars.setdefault(name, value)
    if not hasattr(self, "_sbg_delta_last_monotonic"):
        self._sbg_delta_last_monotonic = time.monotonic()


def _runtime_reset_delta(self: Runtime) -> None:
    _runtime_ensure_delta_state(self)
    now = time.monotonic()
    self._sbg_delta_last_monotonic = now
    self.vars[SBG_NOW_VAR] = 0
    self.vars[SBG_LAST_VAR] = 0
    self.vars[SBG_RAW_DT_VAR] = 0
    self.vars[SBG_DT_VAR] = 0
    self.vars[SBG_FPS_VAR] = 0
    self.vars[SBG_FRAME_VAR] = 0
    self.vars[SBG_TURBO_VAR] = 1


def _runtime_update_delta(self: Runtime) -> float:
    _runtime_ensure_delta_state(self)
    now_abs = time.monotonic()
    last_abs = getattr(self, "_sbg_delta_last_monotonic", now_abs)
    raw = max(0.0, now_abs - last_abs)
    cap = float(self.vars.get(SBG_DT_CAP_VAR, 0.25) or 0)
    if cap > 0 and raw > cap:
        raw = cap
    fixed = float(self.vars.get(SBG_FIXED_DT_VAR, 0) or 0)
    if fixed > 0:
        raw = fixed
    scale = float(self.vars.get(SBG_DT_SCALE_VAR, 1) or 1)
    dt_val = raw * scale
    self._sbg_delta_last_monotonic = now_abs
    self.vars[SBG_NOW_VAR] = now_abs - self.timer_start
    self.vars[SBG_LAST_VAR] = self.vars[SBG_NOW_VAR]
    self.vars[SBG_RAW_DT_VAR] = raw
    self.vars[SBG_DT_VAR] = dt_val
    self.vars[SBG_FRAME_VAR] = float(self.vars.get(SBG_FRAME_VAR, 0) or 0) + 1
    self.vars[SBG_FPS_VAR] = (1 / raw) if raw > 0 else 0
    self.vars[SBG_TURBO_VAR] = 1
    return dt_val


_old_runtime_prepare_patch14 = Runtime.prepare_scratch_console

def _runtime_prepare_patch14(self: Runtime) -> None:
    _old_runtime_prepare_patch14(self)
    _runtime_reset_delta(self)

Runtime.prepare_scratch_console = _runtime_prepare_patch14  # type: ignore[method-assign]


_old_runtime_run_action_patch14 = Runtime.run_action

def _runtime_run_action_patch14(self: Runtime, input_value: str = "") -> Any:
    _runtime_update_delta(self)
    return _old_runtime_run_action_patch14(self, input_value)

Runtime.run_action = _runtime_run_action_patch14  # type: ignore[method-assign]


_old_runtime_call_patch14 = Runtime.call

def _runtime_call_patch14(self: Runtime, name: str, args: List[Any]) -> Any:
    _runtime_ensure_delta_state(self)
    if name in ("tick", "frameStart", "updateDelta"):
        return _runtime_update_delta(self)
    if name == "resetDelta":
        _runtime_reset_delta(self); return None
    if name in ("dt", "deltaTime"):
        return self.vars.get(SBG_DT_VAR, 0)
    if name == "rawDeltaTime":
        return self.vars.get(SBG_RAW_DT_VAR, 0)
    if name == "fps":
        return self.vars.get(SBG_FPS_VAR, 0)
    if name == "frame":
        return self.vars.get(SBG_FRAME_VAR, 0)
    if name == "timeSeconds":
        return time.monotonic() - self.timer_start
    if name == "isTurbo":
        return self.vars.get(SBG_TURBO_VAR, 1)
    if name == "setFixedDelta":
        self.vars[SBG_FIXED_DT_VAR] = float(args[0]); return None
    if name == "useRealDelta":
        self.vars[SBG_FIXED_DT_VAR] = 0; return None
    if name == "setDeltaScale":
        self.vars[SBG_DT_SCALE_VAR] = float(args[0]); return None
    if name == "setDeltaCap":
        self.vars[SBG_DT_CAP_VAR] = float(args[0]); return None
    if name == "setTurbo":
        self.vars[SBG_TURBO_VAR] = 1 if args and args[0] else 0; return None
    if name == "turboOn":
        self.vars[SBG_TURBO_VAR] = 1; return None
    if name == "turboOff":
        self.vars[SBG_TURBO_VAR] = 0; return None
    return _old_runtime_call_patch14(self, name, args)

Runtime.call = _runtime_call_patch14  # type: ignore[method-assign]


# Project-level turbo metadata + optional disabling of warp mutations.
def _sbg_project_set_warp(project: Dict[str, Any], enabled: bool = True) -> None:
    value = "true" if enabled else "false"
    for target in project.get("targets", []):
        for block in (target.get("blocks") or {}).values():
            if isinstance(block, dict):
                mut = block.get("mutation")
                if isinstance(mut, dict) and "warp" in mut:
                    mut["warp"] = value


_old_compiler_compile_patch14 = Compiler.compile

def _compiler_compile_patch14(self: Compiler) -> Dict[str, Any]:
    project = _old_compiler_compile_patch14(self)
    _sbg_project_set_warp(project, True)
    meta = project.setdefault("meta", {})
    meta["agent"] = f"StageBG/SBG {VERSION}"
    meta["stagebgTurbo"] = "warp-default"
    meta["stagebgDeltaTime"] = "timer-backed, capped, fixed-dt-capable"
    meta["stagebgVanillaScratch"] = True
    return project

Compiler.compile = _compiler_compile_patch14  # type: ignore[method-assign]


# Final patch14 CLI. Adds --no-turbo, but keeps turbo on by default.
def main(argv: Optional[List[str]] = None) -> int:  # type: ignore[no-redef]
    ap = argparse.ArgumentParser(prog="sbg", description="StageBG/SBG: professional text code -> vanilla Scratch .sb3 compiler")
    ap.add_argument("--version", action="version", version=f"SBG {VERSION}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    runp = sub.add_parser("run", help="run .sbg natively after verifying it can compile to Scratch")
    runp.add_argument("source")
    runp.add_argument("--fast", action="store_true", help="do not sleep on wait(); turbo mode itself stays on by default")
    runp.add_argument("--no-turbo", action="store_true", help="native compatibility flag: keep generated-style semantics but mark __sbg_turbo=0")
    runp.add_argument("--input", default="", help="input value for one-shot Action(Input) execution")
    runp.add_argument("--terminal", action="store_true", help="start an interactive console loop that mirrors the generated Scratch terminal")
    runp.add_argument("--once", action="store_true", help="run exactly one Action(Input) invocation and exit; default unless --terminal is used")
    runp.add_argument("--embed", action="append", default=[], help="embed text file at compile/run time: path[:virtual/name] or path=virtual/name")
    runp.add_argument("--embed-dir", action="append", default=[], help="embed every text file from a directory recursively")

    comp = sub.add_parser("compile", help="compile .sbg source into a vanilla Scratch .sb3 project")
    comp.add_argument("source")
    comp.add_argument("output")
    comp.add_argument("--allow-library", action="store_true", help="allow compiling a file with no on action/on flag/top-level code")
    comp.add_argument("--no-verify", action="store_true", help="skip generated .sb3 structural verification")
    comp.add_argument("--no-turbo", action="store_true", help="disable warp=true on generated custom blocks; default is turbo/warp on")
    comp.add_argument("--embed", action="append", default=[], help="embed text file into Scratch lists: path[:virtual/name] or path=virtual/name")
    comp.add_argument("--embed-dir", action="append", default=[], help="embed every text file from a directory recursively")

    insp = sub.add_parser("inspect", help="inspect an .sb3 file and print JSON stats")
    insp.add_argument("sb3")

    unp = sub.add_parser("unpack", help="unzip an .sb3 project into a directory")
    unp.add_argument("sb3")
    unp.add_argument("out_dir")

    pkg = sub.add_parser("pkg", help="manage SBG libraries/packages")
    pkg_sub = pkg.add_subparsers(dest="pkg_cmd", required=True)
    pkg_init = pkg_sub.add_parser("init", help="create sbgpkg.json and sbg_modules/")
    pkg_init.add_argument("--name", default=None)
    pkg_install = pkg_sub.add_parser("install", help="install a package from .sbg file, folder, URL, zip URL or registry name")
    pkg_install.add_argument("source", help="local .sbg/folder, URL, or package name when --registry is used")
    pkg_install.add_argument("--name", default=None, help="override installed package name")
    pkg_install.add_argument("--registry", default=None, help="registry JSON path/URL for named packages")
    pkg_sub.add_parser("list", help="list installed packages")
    pkg_remove = pkg_sub.add_parser("remove", help="remove an installed package")
    pkg_remove.add_argument("name")

    args = ap.parse_args(argv)
    source_text = ""
    fallback_filename = "<source>"
    try:
        if args.cmd == "run":
            fallback_filename = args.source
            source_text = Path(args.source).read_text(encoding="utf-8")
            program = parse_source(source_text, args.source)
            program = _program_with_embedded_files(program, args.source, embeds=args.embed, embed_dirs=args.embed_dir)
            assert_scratch_compatible(program)
            rt = Runtime(program, fast=args.fast, filename=args.source, source_text=source_text)
            if args.no_turbo:
                rt.vars[SBG_TURBO_VAR] = 0
            if args.terminal:
                rt.run_scratch_terminal()
            else:
                rt.run_scratch_once(args.input)
            return 0
        if args.cmd == "compile":
            fallback_filename = args.source
            source_text = Path(args.source).read_text(encoding="utf-8")
            program = parse_source(source_text, args.source)
            program = _program_with_embedded_files(program, args.source, embeds=args.embed, embed_dirs=args.embed_dir)
            project = Compiler(program, allow_library=args.allow_library).compile()
            if args.no_turbo:
                _sbg_project_set_warp(project, False)
                project.setdefault("meta", {})["stagebgTurbo"] = "disabled-by-cli"
            write_sb3_project(project, args.output, verify=not args.no_verify)
            print(f"compiled: {args.output}")
            if not args.no_turbo:
                print("turbo: on (vanilla Scratch warp custom blocks; no TurboWarp dependency)")
            if args.allow_library:
                print("warning: compiled in --allow-library mode; Action(Input) may intentionally have no body")
            return 0
        if args.cmd == "inspect":
            print(json.dumps(inspect_sb3(args.sb3), ensure_ascii=False, indent=2))
            return 0
        if args.cmd == "unpack":
            unpack_sb3(args.sb3, args.out_dir)
            print(f"unpacked: {args.out_dir}")
            return 0
        if args.cmd == "pkg":
            root = Path.cwd()
            if args.pkg_cmd == "init":
                path = package_init(root, args.name)
                print(f"initialized: {path}")
                return 0
            if args.pkg_cmd == "install":
                result = install_from_source(args.source, root=root, name=args.name, registry=args.registry)
                print(f"installed: {result['name']} -> {result['path']} ({result['main']})")
                return 0
            if args.pkg_cmd == "list":
                rows = list_packages(root)
                if not rows:
                    print("no packages installed")
                else:
                    for row in rows:
                        status = "ok" if row["installed"] else "missing"
                        print(f"{row['name']}@{row['version']}  main={row['main']}  {status}")
                return 0
            if args.pkg_cmd == "remove":
                remove_package(root, args.name)
                print(f"removed: {args.name}")
                return 0
    except SBGError as e:
        print(format_diagnostic(e, source_text=source_text, fallback_filename=fallback_filename), file=sys.stderr)
        return 1
    except OSError as e:
        print(f"FileError: {e}", file=sys.stderr)
        return 1
    return 2



# Patch14b: allow built-in/local package folders, so examples and real projects can
# use `import "std";` directly without first copying std into sbg_modules/.
_old_import_package_roots_patch14 = ImportResolver.package_roots

def _import_package_roots_patch14(self: ImportResolver, base: Path) -> List[Path]:
    roots = list(_old_import_package_roots_patch14(self, base))
    cursor = self._safe_resolve(base if base.is_dir() else base.parent)
    extra: List[Path] = []
    for parent in [cursor, *cursor.parents, Path.cwd()]:
        extra.append(parent / "packages")
    out: List[Path] = []
    seen: set[Path] = set()
    for root in [*roots, *extra]:
        r = self._safe_resolve(root)
        if r not in seen:
            out.append(r)
            seen.add(r)
    return out

ImportResolver.package_roots = _import_package_roots_patch14  # type: ignore[method-assign]



# =============================================================================
# Patch 15: adult stdlib surface + additional vanilla Scratch VM bindings
# =============================================================================

VERSION = "0.9.0-patch15-professional-stdlib"

# Extra builtins intentionally map to vanilla Scratch opcodes or official Scratch
# extensions (Pen/Music). No TurboWarp-only blocks are emitted.
BUILTIN_EXPR_NAMES.update({
    # coercion / list convenience
    "num", "text", "bool01", "listLen", "listGet", "listHas", "firstItem", "lastItem",
    # sound/music reporters
    "volume", "tempo",
})
BUILTIN_STMT_NAMES.update({
    # control helpers that normal languages expect
    "waitUntil",
    # list helpers that compile to multiple vanilla Scratch blocks
    "popTo", "shiftTo", "appendList", "copyList",
    # sound category
    "playSoundUntilDone", "setVolume", "changeVolume",
    # official Music extension
    "setTempo", "changeTempo", "playNote", "rest", "setInstrument", "playDrum",
})
BUILTIN_NAMES = BUILTIN_EXPR_NAMES | BUILTIN_STMT_NAMES

_LIST_FIRST_ARG_BUILTINS_PATCH15 = {
    "listLen", "listGet", "listHas", "firstItem", "lastItem", "popTo", "shiftTo", "appendList", "copyList"
}


def _sbg_output_var_name(expr: Any, what: str) -> str:
    if isinstance(expr, VarExpr):
        return expr.name
    if isinstance(expr, Literal):
        return str(expr.value)
    raise CompileError(f"{what} needs an output variable name, e.g. {what}(xs, \"out\")")


def _sbg_sound_menu_input(self: ScratchBuilder, bid: str, sound_name: str) -> None:
    menu = self.add_block("sound_sounds_menu", parent=bid, shadow=True, fields={"SOUND_MENU": [sound_name, None]})
    self.blocks[bid]["inputs"]["SOUND_MENU"] = [1, menu]


_old_compile_call_expr_patch15 = ScratchBuilder.compile_call_expr

def _compile_call_expr_patch15(self: ScratchBuilder, expr: CallExpr, parent: Optional[str]) -> str:
    name, a = expr.callee, expr.args
    if name == "num":
        self.need_args(name, a, 1)
        # Scratch numeric coercion: value + 0
        return self.compile_expr(BinaryExpr(a[0], "+", Literal(0)), parent)
    if name == "text":
        self.need_args(name, a, 1)
        return self.compile_expr(CallExpr("join", [Literal(""), a[0]]), parent)
    if name == "bool01":
        self.need_args(name, a, 1)
        return self.compile_expr(UnaryExpr("!", BinaryExpr(a[0], "==", Literal(0))), parent)
    if name == "listLen":
        self.need_args(name, a, 1)
        lst = self.require_list_expr(a[0])
        return self.add_block("data_lengthoflist", parent=parent, fields={"LIST": [lst, self.list_id(lst)]})
    if name == "listGet":
        self.need_args(name, a, 2)
        return _old_compile_call_expr_patch15(self, CallExpr("item", a), parent)
    if name == "listHas":
        self.need_args(name, a, 2)
        return _old_compile_call_expr_patch15(self, CallExpr("contains", a), parent)
    if name == "firstItem":
        self.need_args(name, a, 1)
        return _old_compile_call_expr_patch15(self, CallExpr("item", [a[0], Literal(1)]), parent)
    if name == "lastItem":
        self.need_args(name, a, 1)
        return _old_compile_call_expr_patch15(self, CallExpr("item", [a[0], CallExpr("len", [a[0]])]), parent)
    if name == "volume":
        self.need_args(name, a, 0)
        return self.add_block("sound_volume", parent=parent)
    if name == "tempo":
        self.need_args(name, a, 0)
        return self.add_block("music_getTempo", parent=parent)
    return _old_compile_call_expr_patch15(self, expr, parent)

ScratchBuilder.compile_call_expr = _compile_call_expr_patch15  # type: ignore[method-assign]


_old_bool_expr_patch15 = ScratchBuilder.is_boolean_expr

def _bool_expr_patch15(self: ScratchBuilder, expr: Any) -> bool:
    return _old_bool_expr_patch15(self, expr) or (isinstance(expr, CallExpr) and expr.callee in {"listHas", "bool01"})

ScratchBuilder.is_boolean_expr = _bool_expr_patch15  # type: ignore[method-assign]


_old_compile_call_stmt_patch15 = ScratchBuilder.compile_call_stmt

def _compile_call_stmt_patch15(self: ScratchBuilder, expr: CallExpr) -> Optional[str]:
    name, a = expr.callee, expr.args
    if name == "waitUntil":
        self.need_args(name, a, 1)
        bid = self.add_block("control_wait_until", inputs={})
        self.blocks[bid]["inputs"]["CONDITION"] = self.expr_input(a[0], bid)
        return bid

    if name in ("popTo", "shiftTo"):
        self.need_args(name, a, 2)
        lst = self.require_list_expr(a[0])
        out_name = _sbg_output_var_name(a[1], name)
        index_expr = Literal(1) if name == "shiftTo" else CallExpr("len", [VarExpr(lst)])
        set_bid = self.add_block("data_setvariableto", fields={"VARIABLE": [out_name, self.var_id(out_name)]}, inputs={})
        self.blocks[set_bid]["inputs"]["VALUE"] = self.expr_input(CallExpr("item", [VarExpr(lst), index_expr]), set_bid)
        del_bid = self.add_block("data_deleteoflist", fields={"LIST": [lst, self.list_id(lst)]}, inputs={})
        self.blocks[del_bid]["inputs"]["INDEX"] = self.expr_input(index_expr, del_bid)
        return self.chain(set_bid, del_bid)

    if name in ("appendList", "copyList"):
        self.need_args(name, a, 2)
        src = self.require_list_expr(a[0])
        dst = self.require_list_expr(a[1])
        tmp = f"__sbg_i_{self.uid('tmp')}"
        statements: List[Any] = []
        if name == "copyList":
            statements.append(ExprStmt(CallExpr("clearList", [VarExpr(dst)])))
        statements.append(AssignStmt(tmp, "=", Literal(1)))
        statements.append(WhileStmt(
            BinaryExpr(VarExpr(tmp), "<=", CallExpr("len", [VarExpr(src)])),
            [
                ExprStmt(CallExpr("push", [VarExpr(dst), CallExpr("item", [VarExpr(src), VarExpr(tmp)])])),
                AssignStmt(tmp, "+=", Literal(1)),
            ],
        ))
        self.var_id(tmp)
        return self.compile_statement_chain(statements)

    if name == "playSoundUntilDone":
        self.need_args(name, a, 1)
        sound_name = _sbg_literal_string(a[0], "playSoundUntilDone sound")
        bid = self.add_block("sound_playuntildone", inputs={})
        _sbg_sound_menu_input(self, bid, sound_name)
        return bid
    if name == "setVolume":
        self.need_args(name, a, 1)
        bid = self.add_block("sound_setvolumeto", inputs={})
        self.blocks[bid]["inputs"]["VOLUME"] = self.expr_input(a[0], bid)
        return bid
    if name == "changeVolume":
        self.need_args(name, a, 1)
        bid = self.add_block("sound_changevolumeby", inputs={})
        self.blocks[bid]["inputs"]["VOLUME"] = self.expr_input(a[0], bid)
        return bid
    if name == "setTempo":
        self.need_args(name, a, 1)
        bid = self.add_block("music_setTempo", inputs={})
        self.blocks[bid]["inputs"]["TEMPO"] = self.expr_input(a[0], bid)
        return bid
    if name == "changeTempo":
        self.need_args(name, a, 1)
        bid = self.add_block("music_changeTempo", inputs={})
        self.blocks[bid]["inputs"]["TEMPO"] = self.expr_input(a[0], bid)
        return bid
    if name == "playNote":
        self.need_args(name, a, 2)
        bid = self.add_block("music_playNoteForBeats", inputs={})
        self.blocks[bid]["inputs"]["NOTE"] = self.expr_input(a[0], bid)
        self.blocks[bid]["inputs"]["BEATS"] = self.expr_input(a[1], bid)
        return bid
    if name == "rest":
        self.need_args(name, a, 1)
        bid = self.add_block("music_restForBeats", inputs={})
        self.blocks[bid]["inputs"]["BEATS"] = self.expr_input(a[0], bid)
        return bid
    if name == "setInstrument":
        self.need_args(name, a, 1)
        bid = self.add_block("music_setInstrument", inputs={})
        self.blocks[bid]["inputs"]["INSTRUMENT"] = self.expr_input(a[0], bid)
        return bid
    if name == "playDrum":
        self.need_args(name, a, 2)
        bid = self.add_block("music_playDrumForBeats", inputs={})
        self.blocks[bid]["inputs"]["DRUM"] = self.expr_input(a[0], bid)
        self.blocks[bid]["inputs"]["BEATS"] = self.expr_input(a[1], bid)
        return bid

    return _old_compile_call_stmt_patch15(self, expr)

ScratchBuilder.compile_call_stmt = _compile_call_stmt_patch15  # type: ignore[method-assign]


_old_runtime_call_patch15 = Runtime.call

def _runtime_call_patch15(self: Runtime, name: str, args: List[Any]) -> Any:
    if name == "num": return float(args[0]) if args else 0
    if name == "text": return "" if not args else str(args[0])
    if name == "bool01": return 1 if (args and self.truthy(args[0])) else 0
    if name == "listLen": return len(self.get_list_arg(args[0]))
    if name == "listGet": return self.get_list_arg(args[0])[int(args[1]) - 1]
    if name == "listHas": return args[1] in self.get_list_arg(args[0])
    if name == "firstItem": return self.get_list_arg(args[0])[0]
    if name == "lastItem": return self.get_list_arg(args[0])[-1]
    if name == "waitUntil": return None
    if name in ("popTo", "shiftTo"):
        lst = self.get_list_arg(args[0], require_name=False)
        if not lst: return None
        value = lst.pop(0 if name == "shiftTo" else -1)
        if len(args) >= 2 and isinstance(args[1], str):
            self.vars[args[1]] = value
        return value
    if name == "appendList":
        self.get_list_arg(args[1]).extend(list(self.get_list_arg(args[0]))); return None
    if name == "copyList":
        dst = self.get_list_arg(args[1]); dst.clear(); dst.extend(list(self.get_list_arg(args[0]))); return None
    if name in ("playSoundUntilDone", "setVolume", "changeVolume", "setTempo", "changeTempo", "playNote", "rest", "setInstrument", "playDrum"):
        # Headless native runner: deterministic no-op for VM-side audio.
        return None
    if name == "volume": return 100
    if name == "tempo": return 60
    return _old_runtime_call_patch15(self, name, args)

Runtime.call = _runtime_call_patch15  # type: ignore[method-assign]


_old_project_ensure_patch15 = _project_ensure_patch13

def _project_ensure_patch15(project: Dict[str, Any]) -> Dict[str, Any]:
    project = _old_project_ensure_patch15(project)
    exts = project.setdefault("extensions", [])
    if "music" not in exts:
        exts.append("music")
    meta = project.setdefault("meta", {})
    meta["agent"] = f"StageBG/SBG {VERSION}"
    meta["stagebgScratchTurboNote"] = "Vanilla Scratch editor Turbo Mode is Shift+GreenFlag; StageBG also emits warp custom blocks."
    return project

# Existing patch14 compiler calls Compiler.compile -> _project_ensure_patch13 indirectly.
# Replace Compiler.compile once more to add patch15 metadata/extensions after patch14.
_old_compiler_compile_patch15 = Compiler.compile

def _compiler_compile_patch15(self: Compiler) -> Dict[str, Any]:
    return _project_ensure_patch15(_old_compiler_compile_patch15(self))

Compiler.compile = _compiler_compile_patch15  # type: ignore[method-assign]



# Patch 15b: source-level local variable lowering.
# Scratch variables are target-global, so real block/procedure-local `let` needs
# compiler-generated hidden names. This pass keeps SBG usable like a normal
# language: local `i`, `line`, `tmp`, etc. no longer collide across stdlib calls.

def _sbg_sanitize_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", name)


def _sbg_mangle_locals(program: Program) -> Program:
    counter = {"n": 0}

    def fresh(name: str) -> str:
        counter["n"] += 1
        return f"__loc_{counter['n']}_{_sbg_sanitize_name(name)}"

    def lookup(env_stack: List[Dict[str, str]], name: str) -> str:
        for env in reversed(env_stack):
            if name in env:
                return env[name]
        return name

    def expr(e: Any, env_stack: List[Dict[str, str]]) -> Any:
        if isinstance(e, VarExpr):
            e.name = lookup(env_stack, e.name)
        elif isinstance(e, BinaryExpr):
            e.left = expr(e.left, env_stack); e.right = expr(e.right, env_stack)
        elif isinstance(e, UnaryExpr):
            e.expr = expr(e.expr, env_stack)
        elif isinstance(e, CallExpr):
            if e.callee == "cin":
                # cin's targets are encoded as Literal(varname) rather than
                # VarExpr(varname), so they need explicit lookup/rename here;
                # otherwise cin keeps writing to the pre-mangling variable name
                # while every read of that local uses the mangled __loc_N_name.
                e.args = [
                    Literal(lookup(env_stack, a.value)) if isinstance(a, Literal) else expr(a, env_stack)
                    for a in e.args
                ]
            else:
                e.args = [expr(a, env_stack) for a in e.args]
        elif isinstance(e, ArrayExpr):
            e.items = [expr(x, env_stack) for x in e.items]
        return e

    def body(stmts: List[Any], env_stack: List[Dict[str, str]], *, top_level: bool = False) -> List[Any]:
        local_scope: Dict[str, str] = {}
        stack = env_stack if top_level else [*env_stack, local_scope]
        out: List[Any] = []
        for st in stmts:
            out.append(stmt(st, stack, local_scope if not top_level else None, top_level=top_level))
        return out

    def stmt(st: Any, env_stack: List[Dict[str, str]], current_scope: Optional[Dict[str, str]], *, top_level: bool = False) -> Any:
        if isinstance(st, VarDecl):
            st.expr = expr(st.expr, env_stack)
            if not top_level and current_scope is not None:
                new = fresh(st.name)
                current_scope[st.name] = new
                st.name = new
            return st
        if isinstance(st, ListDecl):
            st.items = [expr(x, env_stack) for x in st.items]
            if not top_level and current_scope is not None:
                new = fresh(st.name)
                current_scope[st.name] = new
                st.name = new
            return st
        if isinstance(st, AssignStmt):
            st.name = lookup(env_stack, st.name)
            st.expr = expr(st.expr, env_stack)
            return st
        if isinstance(st, ExprStmt):
            st.expr = expr(st.expr, env_stack); return st
        if isinstance(st, ReturnStmt):
            if st.expr is not None: st.expr = expr(st.expr, env_stack)
            return st
        if isinstance(st, IfStmt):
            st.cond = expr(st.cond, env_stack)
            st.then_body = body(st.then_body, env_stack)
            if st.else_body is not None:
                st.else_body = body(st.else_body, env_stack)
            return st
        if isinstance(st, RepeatStmt):
            st.count = expr(st.count, env_stack)
            st.body = body(st.body, env_stack)
            return st
        if isinstance(st, ForeverStmt):
            st.body = body(st.body, env_stack); return st
        if isinstance(st, WhileStmt):
            st.cond = expr(st.cond, env_stack)
            st.body = body(st.body, env_stack)
            return st
        if isinstance(st, ForStmt):
            loop_scope: Dict[str, str] = {}
            loop_stack = [*env_stack, loop_scope]
            if st.init is not None:
                st.init = stmt(st.init, loop_stack, loop_scope, top_level=False)
            if st.cond is not None:
                st.cond = expr(st.cond, loop_stack)
            if st.update is not None:
                st.update = stmt(st.update, loop_stack, loop_scope, top_level=False)
            st.body = body(st.body, loop_stack)
            return st
        if isinstance(st, ProcDecl):
            param_scope = {p: p for p in st.params}
            st.body = body(st.body, [*env_stack, param_scope])
            return st
        if isinstance(st, EventDecl):
            param_scope: Dict[str, str] = {}
            if st.kind == "action" and st.value:
                param_scope[st.value] = st.value
            st.body = body(st.body, [*env_stack, param_scope])
            return st
        if isinstance(st, TargetDecl):
            # Variables/lists directly under `stage {}` / `sprite {}` are target globals.
            st.body = body(st.body, env_stack, top_level=True)
            return st
        return st

    program.body = body(program.body, [], top_level=True)
    return program


_old_parse_source_patch15b = parse_source

def parse_source(text: str, filename: str = "<source>") -> Program:  # type: ignore[no-redef]
    return _sbg_mangle_locals(_old_parse_source_patch15b(text, filename))



# =============================================================================
# Patch 16: algorithmic stress-test upgrades
# =============================================================================
# This patch was added after testing StageBG on an olympiad-style shortest-path
# problem.  The missing pieces were not syntax sugar; they were real control-flow
# semantics: `break`/`continue` in vanilla Scratch output and correct loop
# conditions when the condition itself contains a value-returning procedure call.

VERSION = "0.9.0-patch16-algorithmic-controlflow"

BUILTIN_STMT_NAMES.update({
    "fillList", "resizeList", "swapItems", "setItem", "deleteLast", "deleteFirst",
})
BUILTIN_NAMES = BUILTIN_EXPR_NAMES | BUILTIN_STMT_NAMES


def _sbg_stmt_tree_contains(stmt: Any, typ: Any) -> bool:
    return any(isinstance(s, typ) for s in _sbg_walk_stmt_tree(stmt))


def _sbg_body_contains(body: List[Any], typ: Any) -> bool:
    return any(_sbg_stmt_tree_contains(s, typ) for s in body)


def _sbg_or_expr(items: List[Any]) -> Any:
    if not items:
        return Literal(False)
    out = items[0]
    for x in items[1:]:
        out = BinaryExpr(out, "||", x)
    return out


def _sbg_and_expr(items: List[Any]) -> Any:
    if not items:
        return Literal(True)
    out = items[0]
    for x in items[1:]:
        out = BinaryExpr(out, "&&", x)
    return out


def _sbg_ensure_flow_state(self: ScratchBuilder) -> None:
    _builder_ensure_patch_state(self)
    if not hasattr(self, "loop_flow_stack"):
        self.loop_flow_stack = []  # list[(break_var, continue_var)]
    if not hasattr(self, "loop_flow_counter"):
        self.loop_flow_counter = 0


def _sbg_new_loop_flow(self: ScratchBuilder) -> Tuple[str, str]:
    _sbg_ensure_flow_state(self)
    self.loop_flow_counter += 1
    b = f"__sbg_break_{self.loop_flow_counter}"
    c = f"__sbg_continue_{self.loop_flow_counter}"
    self.var_id(b); self.var_id(c)
    return b, c


def _sbg_current_loop_flow(self: ScratchBuilder) -> Optional[Tuple[str, str]]:
    _sbg_ensure_flow_state(self)
    stack = getattr(self, "loop_flow_stack", [])
    return stack[-1] if stack else None


def _sbg_flow_guard_expr(self: ScratchBuilder, *, include_continue: bool = True) -> Optional[Any]:
    _sbg_ensure_flow_state(self)
    conds: List[Any] = []
    ret = getattr(self, "current_return_flag", None)
    if ret:
        conds.append(BinaryExpr(VarExpr(ret), "==", Literal(0)))
    cur = _sbg_current_loop_flow(self)
    if cur:
        br, cont = cur
        conds.append(BinaryExpr(VarExpr(br), "==", Literal(0)))
        if include_continue:
            conds.append(BinaryExpr(VarExpr(cont), "==", Literal(0)))
    if not conds:
        return None
    return _sbg_and_expr(conds)


def _sbg_loop_stop_expr(self: ScratchBuilder, base_stop: Any, break_var: str) -> Any:
    stops = [base_stop, BinaryExpr(VarExpr(break_var), "==", Literal(1))]
    ret = getattr(self, "current_return_flag", None)
    if ret:
        stops.append(BinaryExpr(VarExpr(ret), "==", Literal(1)))
    return _sbg_or_expr(stops)


def _sbg_wrap_flow_guard(self: ScratchBuilder, body_first: Optional[str], *, include_continue: bool = True) -> Optional[str]:
    if not body_first:
        return body_first
    cond = _sbg_flow_guard_expr(self, include_continue=include_continue)
    if cond is None:
        return body_first
    bid = self.add_block("control_if", inputs={})
    self.blocks[bid]["inputs"]["CONDITION"] = self.expr_input(cond, bid)
    self.blocks[bid]["inputs"]["SUBSTACK"] = self.substack_input(body_first)
    self.set_parent(body_first, bid)
    return bid


def _sbg_compile_chain_with_guard(self: ScratchBuilder, body: List[Any], *, include_continue: bool = True) -> Optional[str]:
    first: Optional[str] = None
    for stmt in body:
        try:
            sid = self.compile_stmt(stmt)
            sid = _sbg_wrap_flow_guard(self, sid, include_continue=include_continue)
        except CompileError as e:
            attach_location(e, stmt)
            raise
        except Exception as e:
            err = CompileError(str(e))
            attach_location(err, stmt)
            raise err from e
        first = self.chain(first, sid)
    return first


# Replace patch9's guarder with a flow-aware one.  This keeps return guards, but
# also makes `break` and `continue` stop the rest of the current loop body.
def _compile_statement_chain_patch16(self: ScratchBuilder, body: List[Any]) -> Optional[str]:
    return _sbg_compile_chain_with_guard(self, body, include_continue=True)


ScratchBuilder.compile_statement_chain = _compile_statement_chain_patch16  # type: ignore[method-assign]


_old_compile_call_stmt_patch16 = ScratchBuilder.compile_call_stmt


def _compile_call_stmt_patch16(self: ScratchBuilder, expr: CallExpr) -> Optional[str]:
    name, a = expr.callee, expr.args

    if name in ("fillList", "resizeList"):
        self.need_args(name, a, 3)
        lst = self.require_list_expr(a[0])
        clear = self.add_block("data_deletealloflist", fields={"LIST": [lst, self.list_id(lst)]})
        add = self.add_block("data_addtolist", fields={"LIST": [lst, self.list_id(lst)]}, inputs={})
        self.blocks[add]["inputs"]["ITEM"] = self.expr_input(a[2], add)
        rep = self.add_block("control_repeat", inputs={})
        self.blocks[rep]["inputs"]["TIMES"] = self.expr_input(a[1], rep)
        self.blocks[rep]["inputs"]["SUBSTACK"] = self.substack_input(add)
        self.set_parent(add, rep)
        return self.chain(clear, rep)

    if name == "swapItems":
        self.need_args(name, a, 3)
        lst = self.require_list_expr(a[0])
        tmp = f"__sbg_swap_tmp_{self.uid('v')}"
        self.var_id(tmp)
        s1 = _builder_make_set_var(self, tmp, CallExpr("item", [VarExpr(lst), a[1]]))
        r1 = self.add_block("data_replaceitemoflist", fields={"LIST": [lst, self.list_id(lst)]}, inputs={})
        self.blocks[r1]["inputs"]["INDEX"] = self.expr_input(a[1], r1)
        self.blocks[r1]["inputs"]["ITEM"] = self.expr_input(CallExpr("item", [VarExpr(lst), a[2]]), r1)
        r2 = self.add_block("data_replaceitemoflist", fields={"LIST": [lst, self.list_id(lst)]}, inputs={})
        self.blocks[r2]["inputs"]["INDEX"] = self.expr_input(a[2], r2)
        self.blocks[r2]["inputs"]["ITEM"] = self.expr_input(VarExpr(tmp), r2)
        return self.chain(self.chain(s1, r1), r2)

    if name == "setItem":
        self.need_args(name, a, 3)
        lst = self.require_list_expr(a[0])
        bid = self.add_block("data_replaceitemoflist", fields={"LIST": [lst, self.list_id(lst)]}, inputs={})
        self.blocks[bid]["inputs"]["INDEX"] = self.expr_input(a[1], bid)
        self.blocks[bid]["inputs"]["ITEM"] = self.expr_input(a[2], bid)
        return bid

    if name in ("deleteLast", "deleteFirst"):
        self.need_args(name, a, 1)
        lst = self.require_list_expr(a[0])
        idx = Literal(1) if name == "deleteFirst" else CallExpr("len", [VarExpr(lst)])
        bid = self.add_block("data_deleteoflist", fields={"LIST": [lst, self.list_id(lst)]}, inputs={})
        self.blocks[bid]["inputs"]["INDEX"] = self.expr_input(idx, bid)
        return bid

    return _old_compile_call_stmt_patch16(self, expr)


ScratchBuilder.compile_call_stmt = _compile_call_stmt_patch16  # type: ignore[method-assign]


_old_compile_stmt_patch16 = ScratchBuilder.compile_stmt


def _compile_manual_statement_chain_no_continue(self: ScratchBuilder, body: List[Any]) -> Optional[str]:
    """Compile statements that must run after `continue` but not after return/break.

    Used for for-loop update expressions and for re-evaluating while/for
    conditions containing procedure calls.
    """
    first: Optional[str] = None
    for st in body:
        sid = self.compile_stmt(st)
        sid = _sbg_wrap_flow_guard(self, sid, include_continue=False)
        first = self.chain(first, sid)
    return first


def _compile_while_like_patch16(self: ScratchBuilder, cond_expr: Any, body: List[Any], *, update: Optional[Any] = None, init: Optional[Any] = None) -> Optional[str]:
    """Emit a vanilla Scratch repeat-until loop with real break/continue.

    If cond_expr contains value-returning procedure calls, _builder_lower_expr
    turns them into prelude command blocks.  Those prelude commands are emitted
    before the loop and after each iteration, so `while (hasNext())` behaves like
    a normal language rather than evaluating `hasNext()` once.
    """
    _sbg_ensure_flow_state(self)
    br, cont = _sbg_new_loop_flow(self)

    pre_cond, pure_cond = _builder_lower_expr(self, cond_expr)
    init_blocks = self.compile_stmt(init) if init is not None else None
    init_break = _builder_make_set_var(self, br, Literal(0))
    init_cont = _builder_make_set_var(self, cont, Literal(0))
    initial_condition_eval = self.compile_statement_chain(pre_cond)

    self.loop_flow_stack.append((br, cont))
    try:
        body_first = self.compile_statement_chain(body)
        update_first = self.compile_stmt(update) if update is not None else None
        update_first = _sbg_wrap_flow_guard(self, update_first, include_continue=False)
        post_condition_eval = _compile_manual_statement_chain_no_continue(self, pre_cond)
        clear_continue = _builder_make_set_var(self, cont, Literal(0))
    finally:
        self.loop_flow_stack.pop()

    sub = body_first
    sub = self.chain(sub, update_first)
    sub = self.chain(sub, post_condition_eval)
    sub = self.chain(sub, clear_continue)

    stop = _sbg_loop_stop_expr(self, UnaryExpr("!", pure_cond), br)
    loop = self.add_block("control_repeat_until", inputs={})
    self.blocks[loop]["inputs"]["CONDITION"] = self.expr_input(stop, loop)
    self.blocks[loop]["inputs"]["SUBSTACK"] = self.substack_input(sub)
    self.set_parent(sub, loop)

    first = init_blocks
    first = self.chain(first, init_break)
    first = self.chain(first, init_cont)
    first = self.chain(first, initial_condition_eval)
    first = self.chain(first, loop)
    return first


def _compile_stmt_patch16(self: ScratchBuilder, stmt: Any) -> Optional[str]:
    _sbg_ensure_flow_state(self)

    if isinstance(stmt, BreakStmt):
        cur = _sbg_current_loop_flow(self)
        if not cur:
            raise CompileError("break can only be used inside a loop")
        return _builder_make_set_var(self, cur[0], Literal(1))

    if isinstance(stmt, ContinueStmt):
        cur = _sbg_current_loop_flow(self)
        if not cur:
            raise CompileError("continue can only be used inside a loop")
        return _builder_make_set_var(self, cur[1], Literal(1))

    if isinstance(stmt, WhileStmt):
        return _compile_while_like_patch16(self, stmt.cond, stmt.body)

    if isinstance(stmt, ForStmt):
        cond = stmt.cond if stmt.cond is not None else Literal(True)
        return _compile_while_like_patch16(self, cond, stmt.body, update=stmt.update, init=stmt.init)

    if isinstance(stmt, RepeatStmt) and (_sbg_body_contains(stmt.body, BreakStmt) or _sbg_body_contains(stmt.body, ContinueStmt) or _sbg_body_contains(stmt.body, ReturnStmt)):
        self.return_temp_counter += 1
        limit = f"__sbg_repeat_limit_{self.return_temp_counter}"
        idx = f"__sbg_repeat_i_{self.return_temp_counter}"
        init_limit = VarDecl(limit, stmt.count, True)
        init_i = VarDecl(idx, Literal(0), True)
        update = AssignStmt(idx, "+=", Literal(1))
        cond = BinaryExpr(VarExpr(idx), "<", VarExpr(limit))
        return self.chain(self.compile_stmt(init_limit), self.chain(self.compile_stmt(init_i), _compile_while_like_patch16(self, cond, stmt.body, update=update)))

    if isinstance(stmt, ForeverStmt) and (_sbg_body_contains(stmt.body, BreakStmt) or _sbg_body_contains(stmt.body, ContinueStmt) or _sbg_body_contains(stmt.body, ReturnStmt)):
        return _compile_while_like_patch16(self, Literal(True), stmt.body)

    return _old_compile_stmt_patch16(self, stmt)


ScratchBuilder.compile_stmt = _compile_stmt_patch16  # type: ignore[method-assign]


_old_runtime_call_patch16 = Runtime.call


def _runtime_call_patch16(self: Runtime, name: str, args: List[Any]) -> Any:
    if name in ("fillList", "resizeList"):
        lst = self.get_list_arg(args[0], require_name=True)
        lst.clear()
        n = max(0, int(float(args[1])))
        for _ in range(n):
            lst.append(args[2])
        return None
    if name == "swapItems":
        lst = self.get_list_arg(args[0], require_name=True)
        i = int(float(args[1])) - 1
        j = int(float(args[2])) - 1
        lst[i], lst[j] = lst[j], lst[i]
        return None
    if name == "setItem":
        lst = self.get_list_arg(args[0], require_name=True)
        lst[int(float(args[1])) - 1] = args[2]
        return None
    if name == "deleteLast":
        lst = self.get_list_arg(args[0], require_name=True)
        if lst: lst.pop()
        return None
    if name == "deleteFirst":
        lst = self.get_list_arg(args[0], require_name=True)
        if lst: lst.pop(0)
        return None
    return _old_runtime_call_patch16(self, name, args)


Runtime.call = _runtime_call_patch16  # type: ignore[method-assign]


_old_project_ensure_patch16 = _project_ensure_patch15


def _project_ensure_patch16(project: Dict[str, Any]) -> Dict[str, Any]:
    project = _old_project_ensure_patch16(project)
    meta = project.setdefault("meta", {})
    meta["agent"] = f"StageBG/SBG {VERSION}"
    meta["stagebgPatch16"] = "break/continue lowering, loop-condition procedure-call reevaluation, algorithmic stdlib helpers"
    return project


_old_compiler_compile_patch16 = Compiler.compile


def _compiler_compile_patch16(self: Compiler) -> Dict[str, Any]:
    return _project_ensure_patch16(_old_compiler_compile_patch16(self))


Compiler.compile = _compiler_compile_patch16  # type: ignore[method-assign]


# Patch 16b: reachability-based procedure tree shaking.
# Importing `std` should not mean compiling every stdlib procedure into the
# Scratch workspace.  Adult-sized projects become unreadable and slow otherwise.

def _sbg_collect_calls_expr(expr: Any, out: set[str]) -> None:
    if isinstance(expr, CallExpr):
        out.add(expr.callee)
        for a in expr.args:
            _sbg_collect_calls_expr(a, out)
    elif isinstance(expr, BinaryExpr):
        _sbg_collect_calls_expr(expr.left, out); _sbg_collect_calls_expr(expr.right, out)
    elif isinstance(expr, UnaryExpr):
        _sbg_collect_calls_expr(expr.expr, out)
    elif isinstance(expr, ArrayExpr):
        for x in expr.items:
            _sbg_collect_calls_expr(x, out)


def _sbg_collect_calls_stmt(stmt: Any, out: set[str]) -> None:
    if isinstance(stmt, VarDecl):
        _sbg_collect_calls_expr(stmt.expr, out)
    elif isinstance(stmt, ListDecl):
        for x in stmt.items: _sbg_collect_calls_expr(x, out)
    elif isinstance(stmt, AssignStmt):
        _sbg_collect_calls_expr(stmt.expr, out)
    elif isinstance(stmt, ExprStmt):
        _sbg_collect_calls_expr(stmt.expr, out)
    elif isinstance(stmt, ReturnStmt) and stmt.expr is not None:
        _sbg_collect_calls_expr(stmt.expr, out)
    elif isinstance(stmt, IfStmt):
        _sbg_collect_calls_expr(stmt.cond, out)
        for s in stmt.then_body: _sbg_collect_calls_stmt(s, out)
        for s in stmt.else_body or []: _sbg_collect_calls_stmt(s, out)
    elif isinstance(stmt, RepeatStmt):
        _sbg_collect_calls_expr(stmt.count, out)
        for s in stmt.body: _sbg_collect_calls_stmt(s, out)
    elif isinstance(stmt, ForeverStmt):
        for s in stmt.body: _sbg_collect_calls_stmt(s, out)
    elif isinstance(stmt, WhileStmt):
        _sbg_collect_calls_expr(stmt.cond, out)
        for s in stmt.body: _sbg_collect_calls_stmt(s, out)
    elif isinstance(stmt, ForStmt):
        if stmt.init: _sbg_collect_calls_stmt(stmt.init, out)
        if stmt.cond: _sbg_collect_calls_expr(stmt.cond, out)
        if stmt.update: _sbg_collect_calls_stmt(stmt.update, out)
        for s in stmt.body: _sbg_collect_calls_stmt(s, out)
    elif isinstance(stmt, EventDecl):
        for s in stmt.body: _sbg_collect_calls_stmt(s, out)
    elif isinstance(stmt, ProcDecl):
        for s in stmt.body: _sbg_collect_calls_stmt(s, out)
    elif isinstance(stmt, TargetDecl):
        for s in stmt.body: _sbg_collect_calls_stmt(s, out)


def _sbg_reachable_proc_names(procs: Dict[str, ProcDecl], root_bodies: List[List[Any]]) -> set[str]:
    reachable: set[str] = set()
    work: List[str] = []
    roots: set[str] = set()
    for body in root_bodies:
        for st in body:
            _sbg_collect_calls_stmt(st, roots)
    for name in roots:
        if name in procs and name not in reachable:
            reachable.add(name); work.append(name)
    while work:
        name = work.pop()
        calls: set[str] = set()
        for st in procs[name].body:
            _sbg_collect_calls_stmt(st, calls)
        for c in calls:
            if c in procs and c not in reachable:
                reachable.add(c); work.append(c)
    return reachable


def _sbg_rebuild_proc_signatures(builder: ScratchBuilder, procs: Dict[str, ProcDecl]) -> None:
    signatures: Dict[str, Tuple[str, List[str]]] = {}
    for name, proc in procs.items():
        argids = [builder.uid("arg") for _ in proc.params]
        proccode = name + (" " + " ".join(["%s" for _ in proc.params]) if proc.params else "")
        signatures[name] = (proccode, argids)
    builder.proc_signatures = signatures  # type: ignore[attr-defined]


_old_compiler_analyze_patch16b = Compiler.analyze

def _compiler_analyze_patch16b(self: Compiler) -> None:
    _old_compiler_analyze_patch16b(self)
    if getattr(self, "allow_library", False):
        return
    roots: List[List[Any]] = [body for _param, body in getattr(self, "action_entries", [])]
    roots.extend(ev.body for ev in getattr(self, "message_events", []))
    keep = _sbg_reachable_proc_names(self.procs, roots)
    removed = len(self.procs) - len(keep)
    if removed > 0:
        self.procs = {name: proc for name, proc in self.procs.items() if name in keep}
        _sbg_rebuild_proc_signatures(self.b, self.procs)
        _register_return_vars(self.b, self.procs, self.init_values)
    self.treeshaken_procs_removed = removed

Compiler.analyze = _compiler_analyze_patch16b  # type: ignore[method-assign]


_old_sprite_analyze_patch16b = SpriteTargetCompiler.analyze

def _sprite_analyze_patch16b(self: SpriteTargetCompiler) -> None:
    _old_sprite_analyze_patch16b(self)
    roots: List[List[Any]] = [ev.body for ev in getattr(self, "flag_events", [])]
    roots.extend(ev.body for ev in getattr(self, "message_events", []))
    keep = _sbg_reachable_proc_names(self.procs, roots)
    if len(keep) < len(self.procs):
        self.procs = {name: proc for name, proc in self.procs.items() if name in keep}
        _sbg_rebuild_proc_signatures(self.b, self.procs)
        _register_return_vars(self.b, self.procs, self.init_values)

SpriteTargetCompiler.analyze = _sprite_analyze_patch16b  # type: ignore[method-assign]


# =============================================================================
# Patch 17: real language ergonomics + bits-like algorithm package support
# =============================================================================
# This patch is NOT about adding ready-made solutions such as Dijkstra.
# It adds the missing general-purpose language features that make algorithmic
# code feel like a normal adult language and still lower to vanilla Scratch:
#   - for (let x in list) { ... }
#   - for (let i, x in list) { ... }
#   - for (let i in range(start, end[, step])) { ... }       // half-open
#   - for (let i in rangeClosed(start, end[, step])) { ... } // inclusive
#   - i++; i--; in statements and for-loop updates
# These are compiled to ordinary Scratch variables, list item blocks and
# repeat-until loops. No TurboWarp-only VM opcodes are used.

VERSION = "0.9.0-patch17-language-stdlib"

# Lexer uses these globals dynamically, so updating them before main() is enough.
if "++" not in MULTI:
    MULTI.insert(0, "++")
if "--" not in MULTI:
    MULTI.insert(0, "--")
KEYWORDS.update({"in"})

_sbg_foreach_parse_counter = {"n": 0}


def _sbg_fresh_foreach_temp() -> str:
    _sbg_foreach_parse_counter["n"] += 1
    return f"__sbg_foreach_i_{_sbg_foreach_parse_counter['n']}"


def _sbg_copy_loc(dst: Any, src: Any) -> Any:
    for attr in ("filename", "line", "col"):
        if hasattr(src, attr):
            setattr(dst, attr, getattr(src, attr))
    return dst


def _sbg_lit_bool(v: bool) -> Literal:
    return Literal(bool(v))


def _sbg_make_for_range(start_token: Token, var_name: str, source: CallExpr, body: List[Any], *, declare: bool = True) -> ForStmt:
    args = source.args
    if source.callee in ("range", "rangeOpen"):
        inclusive = False
    elif source.callee in ("rangeClosed", "rangeInclusive"):
        inclusive = True
    else:
        raise ParseError("internal error: _sbg_make_for_range called with non-range expression")
    if len(args) == 1:
        start_expr, end_expr, step_expr = Literal(0), args[0], Literal(1)
    elif len(args) == 2:
        start_expr, end_expr, step_expr = args[0], args[1], Literal(1)
    elif len(args) == 3:
        start_expr, end_expr, step_expr = args[0], args[1], args[2]
    else:
        raise ParseError(f"{source.callee}() expects 1, 2 or 3 args")
    init: Any = VarDecl(var_name, start_expr, True) if declare else AssignStmt(var_name, "=", start_expr)
    # Supports dynamic negative step too:
    #   step >= 0 ? i < end : i > end      (half-open)
    #   step >= 0 ? i <= end : i >= end    (closed)
    pos_cond = BinaryExpr(VarExpr(var_name), "<=" if inclusive else "<", end_expr)
    neg_cond = BinaryExpr(VarExpr(var_name), ">=" if inclusive else ">", end_expr)
    cond = BinaryExpr(
        BinaryExpr(BinaryExpr(step_expr, ">=", Literal(0)), "&&", pos_cond),
        "||",
        BinaryExpr(BinaryExpr(step_expr, "<", Literal(0)), "&&", neg_cond),
    )
    update = AssignStmt(var_name, "+=", step_expr)
    out = ForStmt(init, cond, update, body)
    return _sbg_copy_loc(out, start_token)


def _sbg_make_for_each(start_token: Token, value_name: str, source: Any, body: List[Any], *, declare_value: bool = True, index_name: Optional[str] = None) -> ForStmt:
    # for (let i in range(...)) is syntax, not a real allocated list.
    if isinstance(source, CallExpr) and source.callee in {"range", "rangeOpen", "rangeClosed", "rangeInclusive"} and index_name is None:
        return _sbg_make_for_range(start_token, value_name, source, body, declare=declare_value)

    temp = _sbg_fresh_foreach_temp()
    init = VarDecl(temp, Literal(1), True)
    cond = BinaryExpr(VarExpr(temp), "<=", CallExpr("len", [source]))
    update = AssignStmt(temp, "+=", Literal(1))
    prefix: List[Any] = []
    if index_name is not None:
        prefix.append(VarDecl(index_name, VarExpr(temp), True))
    value_expr = CallExpr("item", [source, VarExpr(temp)])
    prefix.append(VarDecl(value_name, value_expr, True) if declare_value else AssignStmt(value_name, "=", value_expr))
    out = ForStmt(init, cond, update, [*prefix, *body])
    return _sbg_copy_loc(out, start_token)


_old_parse_statement_patch17 = Parser.parse_statement


def _parser_parse_statement_patch17(self: Parser) -> Any:
    start_token = self.peek()
    # i++; / i--;
    if self.peek().kind == "IDENT" and self.peek(1).value in ("++", "--"):
        name = self.advance().value
        op = self.advance().value
        self.expect(";")
        return self.loc(AssignStmt(name, "+=" if op == "++" else "-=", Literal(1)), start_token)
    return _old_parse_statement_patch17(self)


Parser.parse_statement = _parser_parse_statement_patch17  # type: ignore[method-assign]

_old_parse_for_patch17 = Parser.parse_for


def _parser_parse_for_patch17(self: Parser, start_token: Token) -> ForStmt:
    saved = self.i
    self.expect("(")

    # for (let x in xs) / for (let i, x in xs)
    if self.match_kw("let") or self.match_kw("var"):
        first_name = self.expect_ident()
        if self.match(","):
            second_name = self.expect_ident()
            if self.match_kw("in"):
                source = self.parse_expr()
                self.expect(")")
                return _sbg_make_for_each(start_token, second_name, source, self.parse_block(), declare_value=True, index_name=first_name)
            self.i = saved
            return _old_parse_for_patch17(self, start_token)
        if self.match_kw("in"):
            source = self.parse_expr()
            self.expect(")")
            return _sbg_make_for_each(start_token, first_name, source, self.parse_block(), declare_value=True)
        # Not for-in; parse the enhanced C-style loop manually from the saved point.
        self.i = saved

    # for (x in xs) assigns to an existing/global variable x each iteration.
    else:
        if self.peek().kind == "IDENT" and self.peek(1).kind == "KW" and self.peek(1).value == "in":
            value_name = self.advance().value
            self.advance()  # in
            source = self.parse_expr()
            self.expect(")")
            return _sbg_make_for_each(start_token, value_name, source, self.parse_block(), declare_value=False)
        self.i = saved

    # Enhanced original C-style parser, with i++ / i-- in init/update.
    self.expect("(")
    init: Optional[Any] = None
    if not self.at(";"):
        if self.match_kw("let") or self.match_kw("var"):
            name = self.expect_ident()
            expr = Literal(0)
            if self.match("="):
                expr = self.parse_expr()
            init = VarDecl(name, expr, True)
        elif self.peek().kind == "IDENT" and self.peek(1).value in ("++", "--"):
            name = self.advance().value
            op = self.advance().value
            init = AssignStmt(name, "+=" if op == "++" else "-=", Literal(1))
        elif self.peek().kind == "IDENT" and self.peek(1).value in ("=", "+=", "-=", "*=", "/=", "%="):
            name = self.advance().value
            op = self.advance().value
            init = AssignStmt(name, op, self.parse_expr())
        else:
            init = ExprStmt(self.parse_expr())
    self.expect(";")
    cond = None if self.at(";") else self.parse_expr()
    self.expect(";")
    update: Optional[Any] = None
    if not self.at(")"):
        if self.peek().kind == "IDENT" and self.peek(1).value in ("++", "--"):
            name = self.advance().value
            op = self.advance().value
            update = AssignStmt(name, "+=" if op == "++" else "-=", Literal(1))
        elif self.peek().kind == "IDENT" and self.peek(1).value in ("=", "+=", "-=", "*=", "/=", "%="):
            name = self.advance().value
            op = self.advance().value
            update = AssignStmt(name, op, self.parse_expr())
        else:
            update = ExprStmt(self.parse_expr())
    self.expect(")")
    body = self.parse_block()
    return self.loc(ForStmt(init, cond, update, body), start_token)


Parser.parse_for = _parser_parse_for_patch17  # type: ignore[method-assign]

# Compiler/runtime builtins for common generic algorithms that need a concrete
# Scratch list name. These are deliberately not implemented as list-parameter
# procedures, because vanilla Scratch custom blocks cannot receive list refs.
BUILTIN_EXPR_NAMES.update({
    "rangeLen",
})
BUILTIN_STMT_NAMES.update({
    "reverseList", "sortAsc", "sortDesc", "lowerBoundTo", "upperBoundTo", "binarySearchTo",
})
BUILTIN_NAMES = BUILTIN_EXPR_NAMES | BUILTIN_STMT_NAMES

_old_compile_call_expr_patch17 = ScratchBuilder.compile_call_expr


def _compile_call_expr_patch17(self: ScratchBuilder, expr: CallExpr, parent: Optional[str]) -> str:
    name, a = expr.callee, expr.args
    if name == "rangeLen":
        self.need_args(name, a, 2)
        # max(0, end - start), using Scratch arithmetic. For step-aware loops use for-in range syntax.
        return self.compile_expr(CallExpr("max", [Literal(0), BinaryExpr(a[1], "-", a[0])]), parent)
    return _old_compile_call_expr_patch17(self, expr, parent)


ScratchBuilder.compile_call_expr = _compile_call_expr_patch17  # type: ignore[method-assign]

_old_compile_call_stmt_patch17 = ScratchBuilder.compile_call_stmt


def _sbg_compile_sort_list(self: ScratchBuilder, lst: str, *, descending: bool = False) -> Optional[str]:
    # Insertion sort over a concrete Scratch list. Small but reliable in vanilla Scratch.
    suffix = self.uid("sort")
    i = f"__sbg_sort_i_{suffix}"
    j = f"__sbg_sort_j_{suffix}"
    key = f"__sbg_sort_key_{suffix}"
    self.var_id(i); self.var_id(j); self.var_id(key)
    cmp_op = "<" if descending else ">"
    body: List[Any] = [
        VarDecl(i, Literal(2), True),
        WhileStmt(BinaryExpr(VarExpr(i), "<=", CallExpr("len", [VarExpr(lst)])), [
            VarDecl(key, CallExpr("item", [VarExpr(lst), VarExpr(i)]), True),
            VarDecl(j, BinaryExpr(VarExpr(i), "-", Literal(1)), True),
            WhileStmt(BinaryExpr(BinaryExpr(VarExpr(j), ">=", Literal(1)), "&&", BinaryExpr(CallExpr("item", [VarExpr(lst), VarExpr(j)]), cmp_op, VarExpr(key))), [
                ExprStmt(CallExpr("setItem", [VarExpr(lst), BinaryExpr(VarExpr(j), "+", Literal(1)), CallExpr("item", [VarExpr(lst), VarExpr(j)])])),
                AssignStmt(j, "-=", Literal(1)),
            ]),
            ExprStmt(CallExpr("setItem", [VarExpr(lst), BinaryExpr(VarExpr(j), "+", Literal(1)), VarExpr(key)])),
            AssignStmt(i, "+=", Literal(1)),
        ]),
    ]
    return self.compile_statement_chain(body)


def _sbg_compile_bound_to(self: ScratchBuilder, lst: str, value_expr: Any, out_name: str, *, upper: bool = False) -> Optional[str]:
    suffix = self.uid("bound")
    lo = f"__sbg_bound_lo_{suffix}"
    hi = f"__sbg_bound_hi_{suffix}"
    mid = f"__sbg_bound_mid_{suffix}"
    self.var_id(lo); self.var_id(hi); self.var_id(mid); self.var_id(out_name)
    # lower_bound: first idx where item >= value. upper_bound: first idx where item > value.
    if upper:
        move_right_cond = BinaryExpr(CallExpr("item", [VarExpr(lst), VarExpr(mid)]), "<=", value_expr)
    else:
        move_right_cond = BinaryExpr(CallExpr("item", [VarExpr(lst), VarExpr(mid)]), "<", value_expr)
    body: List[Any] = [
        VarDecl(lo, Literal(1), True),
        VarDecl(hi, BinaryExpr(CallExpr("len", [VarExpr(lst)]), "+", Literal(1)), True),
        WhileStmt(BinaryExpr(VarExpr(lo), "<", VarExpr(hi)), [
            VarDecl(mid, CallExpr("floor", [BinaryExpr(BinaryExpr(VarExpr(lo), "+", VarExpr(hi)), "/", Literal(2))]), True),
            IfStmt(move_right_cond,
                   [AssignStmt(lo, "=", BinaryExpr(VarExpr(mid), "+", Literal(1)))],
                   [AssignStmt(hi, "=", VarExpr(mid))]),
        ]),
        AssignStmt(out_name, "=", VarExpr(lo)),
    ]
    return self.compile_statement_chain(body)


def _compile_call_stmt_patch17(self: ScratchBuilder, expr: CallExpr) -> Optional[str]:
    name, a = expr.callee, expr.args
    if name == "reverseList":
        self.need_args(name, a, 1)
        lst = self.require_list_expr(a[0])
        suffix = self.uid("rev")
        i = f"__sbg_rev_i_{suffix}"
        j = f"__sbg_rev_j_{suffix}"
        self.var_id(i); self.var_id(j)
        return self.compile_statement_chain([
            VarDecl(i, Literal(1), True),
            VarDecl(j, CallExpr("len", [VarExpr(lst)]), True),
            WhileStmt(BinaryExpr(VarExpr(i), "<", VarExpr(j)), [
                ExprStmt(CallExpr("swapItems", [VarExpr(lst), VarExpr(i), VarExpr(j)])),
                AssignStmt(i, "+=", Literal(1)),
                AssignStmt(j, "-=", Literal(1)),
            ]),
        ])
    if name in ("sortAsc", "sortDesc"):
        self.need_args(name, a, 1)
        lst = self.require_list_expr(a[0])
        return _sbg_compile_sort_list(self, lst, descending=(name == "sortDesc"))
    if name in ("lowerBoundTo", "upperBoundTo"):
        self.need_args(name, a, 3)
        lst = self.require_list_expr(a[0])
        out_name = _sbg_output_var_name(a[2], name)
        return _sbg_compile_bound_to(self, lst, a[1], out_name, upper=(name == "upperBoundTo"))
    if name == "binarySearchTo":
        self.need_args(name, a, 3)
        lst = self.require_list_expr(a[0])
        out_name = _sbg_output_var_name(a[2], name)
        suffix = self.uid("bs")
        pos = f"__sbg_bs_pos_{suffix}"
        self.var_id(pos); self.var_id(out_name)
        lower = _sbg_compile_bound_to(self, lst, a[1], pos, upper=False)
        check = IfStmt(
            BinaryExpr(BinaryExpr(VarExpr(pos), "<=", CallExpr("len", [VarExpr(lst)])), "&&", BinaryExpr(CallExpr("item", [VarExpr(lst), VarExpr(pos)]), "==", a[1])),
            [AssignStmt(out_name, "=", Literal(1))],
            [AssignStmt(out_name, "=", Literal(0))],
        )
        return self.chain(lower, self.compile_stmt(check))
    return _old_compile_call_stmt_patch17(self, expr)


ScratchBuilder.compile_call_stmt = _compile_call_stmt_patch17  # type: ignore[method-assign]

_old_runtime_call_patch17 = Runtime.call


def _runtime_call_patch17(self: Runtime, name: str, args: List[Any]) -> Any:
    if name == "rangeLen":
        return max(0, int(float(args[1])) - int(float(args[0])))
    if name == "reverseList":
        self.get_list_arg(args[0], require_name=False).reverse(); return None
    if name in ("sortAsc", "sortDesc"):
        lst = self.get_list_arg(args[0], require_name=False)
        lst.sort(reverse=(name == "sortDesc")); return None
    if name in ("lowerBoundTo", "upperBoundTo"):
        lst = self.get_list_arg(args[0], require_name=False)
        value = args[1]
        lo, hi = 0, len(lst)
        upper = name == "upperBoundTo"
        while lo < hi:
            mid = (lo + hi) // 2
            if (lst[mid] <= value) if upper else (lst[mid] < value):
                lo = mid + 1
            else:
                hi = mid
        if len(args) >= 3 and isinstance(args[2], str):
            self.vars[args[2]] = lo + 1
        return lo + 1
    if name == "binarySearchTo":
        lst = self.get_list_arg(args[0], require_name=False)
        ok = 1 if args[1] in lst else 0
        if len(args) >= 3 and isinstance(args[2], str):
            self.vars[args[2]] = ok
        return ok
    return _old_runtime_call_patch17(self, name, args)


Runtime.call = _runtime_call_patch17  # type: ignore[method-assign]

# Tree-shaking must see calls inside the lowered C-style ForStmt bodies. Patch16
# already handles ForStmt, so no new collector is needed because patch17 lowers
# for-in/range at parse time.

_old_project_ensure_patch17 = _project_ensure_patch16


def _project_ensure_patch17(project: Dict[str, Any]) -> Dict[str, Any]:
    project = _old_project_ensure_patch17(project)
    meta = project.setdefault("meta", {})
    meta["agent"] = f"StageBG/SBG {VERSION}"
    meta["stagebgPatch17"] = "for-in/range syntax, ++/--, generic list algorithms, bits-like package"
    meta["stagebgTurboMode"] = "Vanilla Scratch editor supports real Turbo Mode by Shift+GreenFlag; StageBG also emits warp custom blocks by default."
    return project


_old_compiler_compile_patch17 = Compiler.compile


def _compiler_compile_patch17(self: Compiler) -> Dict[str, Any]:
    return _project_ensure_patch17(_old_compiler_compile_patch17(self))


Compiler.compile = _compiler_compile_patch17  # type: ignore[method-assign]



# =============================================================================
# Patch 18: C++-style professional surface and missing standard containers
# =============================================================================
# Goal of this patch: StageBG should feel like a compact programming language for
# adults, not like a renamed Scratch worksheet.  This layer deliberately adds
# C++-inspired syntax and library names, then lowers them to vanilla Scratch
# blocks/lists/variables.
#
# Important design rule:
#   We do NOT add solved tasks such as Dijkstra as builtins.
#   We add the generic language/library pieces that were painful while porting
#   olympiad-style C++ solutions: typed declarations, vector-like list API,
#   C++ range-for, STL-like algorithms, priority_queue/DSU/Fenwick packages.

VERSION = "0.9.0-patch18-cpp-surface"

# Lexer additions.  These globals are read dynamically by Lexer.tokens().
SINGLE.add(":")
KEYWORDS.update({
    "auto", "int", "long", "double", "float", "string", "bool", "char",
    "vector", "include", "namespace", "using", "std",
})
if "::" not in MULTI:
    MULTI.insert(0, "::")
# Keep bool as an ordinary identifier too, because std/core.sbg defines proc bool(value).
KEYWORDS.discard("bool")

_CPP_TYPE_KWS = {"auto", "int", "long", "double", "float", "string", "bool", "char"}


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


_old_lexer_init_patch18 = Lexer.__init__

def _lexer_init_patch18(self: Lexer, text: str, filename: str = "<source>"):
    return _old_lexer_init_patch18(self, _sbg_preprocess_cpp_surface(text), filename)

Lexer.__init__ = _lexer_init_patch18  # type: ignore[method-assign]


def _parser_parse_initializer_items_patch18(self: Parser) -> List[Any]:
    items: List[Any] = []
    if not self.at("}"):
        while True:
            items.append(self.parse_expr())
            if not self.match(","):
                break
    self.expect("}")
    return items


def _parser_skip_template_patch18(self: Parser) -> None:
    """Skip <...> after vector/int/etc.  The compiler is dynamically typed;
    this only accepts familiar C++ syntax such as vector<int>."""
    if not self.match("<"):
        return
    depth = 1
    while depth > 0:
        if self.peek().kind == "EOF":
            raise self.error("unterminated template/type annotation")
        if self.match("<"):
            depth += 1
        elif self.match(">"):
            depth -= 1
        else:
            self.advance()


def _parser_parse_typed_decl_patch18(self: Parser, start_token: Token, first_type: str) -> Any:
    # vector<int> a = {1,2,3};    -> list a = [1,2,3]
    # int n = 0; / auto x = f();  -> let n = 0 / let x = f()
    if first_type == "vector":
        _parser_skip_template_patch18(self)
        name = self.expect_ident()
        items: List[Any] = []
        if self.match("="):
            if self.match("{"):
                items = _parser_parse_initializer_items_patch18(self)
            elif self.match("["):
                if not self.at("]"):
                    while True:
                        items.append(self.parse_expr())
                        if not self.match(","):
                            break
                self.expect("]")
            else:
                raise self.error("vector initialization expects {...} or [...]")
        self.expect(";")
        return self.loc(ListDecl(name, items), start_token)

    # int/long/double/string/bool/auto/char.  Optional extra `long` in `long long`.
    if first_type == "long" and self.peek().kind == "KW" and self.peek().value == "long":
        self.advance()
    _parser_skip_template_patch18(self)
    name = self.expect_ident()
    expr: Any = Literal(0)
    if first_type in {"string", "char"}:
        expr = Literal("")
    elif first_type == "bool":
        expr = Literal(False)
    if self.match("="):
        expr = self.parse_expr()
    self.expect(";")
    return self.loc(VarDecl(name, expr, True), start_token)


_old_parse_statement_patch18_base = Parser.parse_statement

def _parser_parse_statement_patch18(self: Parser) -> Any:
    start_token = self.peek()
    # Fix patch17 standalone i++/i-- bug and keep it C++-style.
    if self.peek().kind == "IDENT" and self.peek(1).value in ("++", "--"):
        name = self.advance().value
        op = self.advance().value
        self.expect(";")
        return self.loc(AssignStmt(name, "+=" if op == "++" else "-=", Literal(1)), start_token)
    if self.peek().value in _CPP_TYPE_KWS | {"vector"} and self.peek().kind in {"KW", "IDENT"}:
        typ = self.advance().value
        return _parser_parse_typed_decl_patch18(self, start_token, typ)
    return _old_parse_statement_patch18_base(self)

Parser.parse_statement = _parser_parse_statement_patch18  # type: ignore[method-assign]


def _parser_parse_for_init_or_update_patch18(self: Parser, *, terminators: set[str]) -> Optional[Any]:
    if self.peek().value in terminators:
        return None
    if self.peek().value in _CPP_TYPE_KWS and self.peek().kind in {"KW", "IDENT"}:
        typ = self.advance().value
        if typ == "long" and self.peek().kind == "KW" and self.peek().value == "long":
            self.advance()
        _parser_skip_template_patch18(self)
        name = self.expect_ident()
        expr: Any = Literal(0 if typ != "string" else "")
        if self.match("="):
            expr = self.parse_expr()
        return VarDecl(name, expr, True)
    if self.peek().kind == "IDENT" and self.peek(1).value in ("++", "--"):
        name = self.advance().value
        op = self.advance().value
        return AssignStmt(name, "+=" if op == "++" else "-=", Literal(1))
    if self.peek().kind == "IDENT" and self.peek(1).value in ("=", "+=", "-=", "*=", "/=", "%="):
        name = self.advance().value
        op = self.advance().value
        return AssignStmt(name, op, self.parse_expr())
    return ExprStmt(self.parse_expr())


_old_parse_for_patch18_base = Parser.parse_for

def _parser_parse_for_patch18(self: Parser, start_token: Token) -> ForStmt:
    saved = self.i
    self.expect("(")

    # C++ range-for: for (auto x : xs) / for (int x : xs)
    if self.peek().value in _CPP_TYPE_KWS | {"vector"} and self.peek().kind in {"KW", "IDENT"}:
        typ = self.advance().value
        if typ == "vector":
            _parser_skip_template_patch18(self)
        elif typ == "long" and self.peek().kind == "KW" and self.peek().value == "long":
            self.advance()
        name = self.expect_ident()
        if self.match(":"):
            source = self.parse_expr()
            self.expect(")")
            return _sbg_make_for_each(start_token, name, source, self.parse_block(), declare_value=True)
        self.i = saved

    # C++ style for (int i = 0; i < n; i++) plus fixed ++/--.
    self.expect("(")
    init = _parser_parse_for_init_or_update_patch18(self, terminators={";"})
    self.expect(";")
    cond = None if self.at(";") else self.parse_expr()
    self.expect(";")
    update = _parser_parse_for_init_or_update_patch18(self, terminators={")"})
    self.expect(")")
    body = self.parse_block()
    return self.loc(ForStmt(init, cond, update, body), start_token)

Parser.parse_for = _parser_parse_for_patch18  # type: ignore[method-assign]


# ---- C++/STL-like builtin names lowered to vanilla Scratch ------------------

BUILTIN_EXPR_NAMES.update({
    "size", "empty", "front", "back", "at",
    "str", "to_string", "stoi", "stod",
    "lower_bound", "upper_bound", "binary_search",
})
BUILTIN_STMT_NAMES.update({
    "push_back", "pop_back", "pop_front", "clear", "erase", "insert_at",
    "assign", "resize", "fill", "swap_items", "sort", "sort_desc", "reverse",
    "lower_bound_to", "upper_bound_to", "binary_search_to",
})
BUILTIN_NAMES = BUILTIN_EXPR_NAMES | BUILTIN_STMT_NAMES


def _sbg_is_list_var_for_builder(self: ScratchBuilder, expr: Any) -> bool:
    return isinstance(expr, VarExpr) and expr.name in getattr(self, "lists", {})


_old_builder_compile_call_expr_patch18 = ScratchBuilder.compile_call_expr

def _builder_compile_call_expr_patch18(self: ScratchBuilder, expr: CallExpr, parent: Optional[str]) -> str:
    name, a = expr.callee, expr.args
    if name == "size":
        self.need_args(name, a, 1)
        return self.compile_call_expr(CallExpr("len", a), parent)
    if name == "empty":
        self.need_args(name, a, 1)
        return self.compile_expr(BinaryExpr(CallExpr("len", a), "==", Literal(0)), parent)
    if name == "front":
        self.need_args(name, a, 1)
        return self.compile_call_expr(CallExpr("item", [a[0], Literal(1)]), parent)
    if name == "back":
        self.need_args(name, a, 1)
        return self.compile_call_expr(CallExpr("item", [a[0], CallExpr("len", [a[0]])]), parent)
    if name == "at":
        self.need_args(name, a, 2)
        return self.compile_call_expr(CallExpr("item", [a[0], a[1]]), parent)
    if name in {"str", "to_string"}:
        self.need_args(name, a, 1)
        # Scratch coerces to text naturally. join("", x) forces string semantics.
        return self.compile_call_expr(CallExpr("join", [Literal(""), a[0]]), parent)
    if name in {"stoi", "stod"}:
        self.need_args(name, a, 1)
        # Scratch numeric slots coerce text to number.  x + 0 forces numeric semantics.
        return self.compile_expr(BinaryExpr(a[0], "+", Literal(0)), parent)
    # lower_bound/upper_bound/binary_search are handled in expression-lowering
    # because they need command blocks before a reporter value can be used.
    return _old_builder_compile_call_expr_patch18(self, expr, parent)

ScratchBuilder.compile_call_expr = _builder_compile_call_expr_patch18  # type: ignore[method-assign]


_old_builder_is_boolean_expr_patch18 = ScratchBuilder.is_boolean_expr

def _builder_is_boolean_expr_patch18(self: ScratchBuilder, expr: Any) -> bool:
    if isinstance(expr, CallExpr) and expr.callee in {"empty", "binary_search"}:
        return True
    return _old_builder_is_boolean_expr_patch18(self, expr)

ScratchBuilder.is_boolean_expr = _builder_is_boolean_expr_patch18  # type: ignore[method-assign]


_old_compile_call_stmt_patch18 = ScratchBuilder.compile_call_stmt

def _builder_compile_call_stmt_patch18(self: ScratchBuilder, expr: CallExpr) -> Optional[str]:
    name, a = expr.callee, expr.args
    # Vector/list aliases named like C++/STL, but lowered to the existing vanilla
    # Scratch list builtins.
    alias = {
        "push_back": "push",
        "erase": "delete",
        "insert_at": "insert",
        "clear": "clearList",
        "resize": "resizeList",
        "fill": "fillList",
        "swap_items": "swapItems",
        "reverse": "reverseList",
        "sort": "sortAsc",
        "sort_desc": "sortDesc",
        "lower_bound_to": "lowerBoundTo",
        "upper_bound_to": "upperBoundTo",
        "binary_search_to": "binarySearchTo",
    }
    if name in alias:
        return self.compile_call_stmt(CallExpr(alias[name], a))
    if name == "pop_back":
        self.need_args(name, a, 1)
        return self.compile_call_stmt(CallExpr("deleteLast", a))
    if name == "pop_front":
        self.need_args(name, a, 1)
        return self.compile_call_stmt(CallExpr("deleteFirst", a))
    if name == "assign":
        self.need_args(name, a, 3)
        return self.compile_statement_chain([
            ExprStmt(CallExpr("clearList", [a[0]])),
            ExprStmt(CallExpr("resizeList", [a[0], a[1], a[2]])),
        ])
    return _old_compile_call_stmt_patch18(self, expr)

ScratchBuilder.compile_call_stmt = _builder_compile_call_stmt_patch18  # type: ignore[method-assign]


# Patch expression lowering so lower_bound(v,x) can be used naturally in `let p = ...`.
# It still compiles to command blocks + a hidden temporary variable before the
# surrounding statement, which is the only vanilla Scratch-compatible way to run
# a loop and then feed a value into a reporter slot.
_old_builder_lower_expr_patch18 = _builder_lower_expr

def _builder_lower_expr_patch18(self: ScratchBuilder, expr: Any) -> Tuple[List[Any], Any]:
    if isinstance(expr, CallExpr) and expr.callee in {"lower_bound", "upper_bound", "binary_search"}:
        if len(expr.args) != 2:
            raise CompileError(f"{expr.callee}() expects 2 args: list, value")
        self.return_temp_counter = getattr(self, "return_temp_counter", 0) + 1
        temp = f"__sbg_{expr.callee}_{self.return_temp_counter}"
        self.var_id(temp)
        stmt_name = {"lower_bound":"lower_bound_to", "upper_bound":"upper_bound_to", "binary_search":"binary_search_to"}[expr.callee]
        pre_a, lowered_args = _builder_lower_exprs(self, expr.args)
        return [*pre_a, ExprStmt(CallExpr(stmt_name, [lowered_args[0], lowered_args[1], VarExpr(temp)]))], VarExpr(temp)
    return _old_builder_lower_expr_patch18(self, expr)

# Replace the global function used by patched compile_stmt.
_builder_lower_expr = _builder_lower_expr_patch18  # type: ignore[assignment]


_old_runtime_call_patch18 = Runtime.call

def _runtime_call_patch18(self: Runtime, name: str, args: List[Any]) -> Any:
    if name in {"size", "len"} and len(args) == 1:
        return len(args[0])
    if name == "empty":
        return 1 if len(args[0]) == 0 else 0
    if name == "front":
        return self.get_list_arg(args[0])[0]
    if name == "back":
        return self.get_list_arg(args[0])[-1]
    if name == "at":
        return self.get_list_arg(args[0])[int(args[1]) - 1]
    if name in {"str", "to_string"}:
        return str(args[0])
    if name in {"stoi", "stod"}:
        try:
            x = float(args[0])
            if name == "stoi":
                return int(x)
            return x
        except Exception:
            return 0
    if name == "push_back":
        return self.call("push", args)
    if name == "pop_back":
        lst = self.get_list_arg(args[0], require_name=True)
        return lst.pop() if lst else ""
    if name == "pop_front":
        lst = self.get_list_arg(args[0], require_name=True)
        return lst.pop(0) if lst else ""
    if name == "clear":
        self.get_list_arg(args[0], require_name=True).clear(); return None
    if name == "erase":
        return self.call("delete", args)
    if name == "insert_at":
        return self.call("insert", args)
    if name == "assign":
        lst = self.get_list_arg(args[0], require_name=True)
        lst[:] = [args[2] for _ in range(max(0, int(args[1])))]
        return None
    if name == "resize":
        lst = self.get_list_arg(args[0], require_name=True)
        n = max(0, int(args[1])); val = args[2] if len(args) >= 3 else 0
        while len(lst) > n: lst.pop()
        while len(lst) < n: lst.append(val)
        return None
    if name == "fill":
        lst = self.get_list_arg(args[0], require_name=True)
        for i in range(len(lst)): lst[i] = args[1]
        return None
    if name == "swap_items":
        lst = self.get_list_arg(args[0], require_name=True)
        i = int(args[1]) - 1; j = int(args[2]) - 1
        lst[i], lst[j] = lst[j], lst[i]
        return None
    if name == "sort":
        self.get_list_arg(args[0], require_name=True).sort(); return None
    if name == "sort_desc":
        self.get_list_arg(args[0], require_name=True).sort(reverse=True); return None
    if name == "reverse":
        self.get_list_arg(args[0], require_name=True).reverse(); return None
    if name in {"lower_bound", "upper_bound", "binary_search"}:
        lst = self.get_list_arg(args[0])
        value = args[1]
        if name == "binary_search":
            return 1 if value in lst else 0
        lo, hi = 0, len(lst)
        upper = name == "upper_bound"
        while lo < hi:
            mid = (lo + hi) // 2
            if (lst[mid] <= value) if upper else (lst[mid] < value): lo = mid + 1
            else: hi = mid
        return lo + 1
    if name in {"lower_bound_to", "upper_bound_to", "binary_search_to"}:
        old = {"lower_bound_to":"lowerBoundTo", "upper_bound_to":"upperBoundTo", "binary_search_to":"binarySearchTo"}[name]
        return self.call(old, args)
    return _old_runtime_call_patch18(self, name, args)

Runtime.call = _runtime_call_patch18  # type: ignore[method-assign]


_old_project_ensure_patch18 = _project_ensure_patch17

def _project_ensure_patch18(project: Dict[str, Any]) -> Dict[str, Any]:
    project = _old_project_ensure_patch18(project)
    meta = project.setdefault("meta", {})
    meta["agent"] = f"StageBG/SBG {VERSION}"
    meta["stagebgPatch18"] = "C++-style syntax (#include, typed declarations, vector/list aliases, STL-like algorithms)."
    return project

_old_compiler_compile_patch18 = Compiler.compile

def _compiler_compile_patch18(self: Compiler) -> Dict[str, Any]:
    return _project_ensure_patch18(_old_compiler_compile_patch18(self))

Compiler.compile = _compiler_compile_patch18  # type: ignore[method-assign]



# ---- Patch 18b: compatibility fixes for patch17 syntax and sprite size() -----

_prev_parse_for_patch18 = Parser.parse_for

def _parser_parse_for_patch18b(self: Parser, start_token: Token) -> ForStmt:
    # Keep existing StageBG syntax: for (let x in xs), for (let i in range(...)).
    saved = self.i
    try:
        self.expect("(")
        if self.peek().kind == "KW" and self.peek().value in {"let", "var"}:
            self.i = saved
            return _old_parse_for_patch18_base(self, start_token)
    except Exception:
        pass
    self.i = saved
    return _prev_parse_for_patch18(self, start_token)

Parser.parse_for = _parser_parse_for_patch18b  # type: ignore[method-assign]

_prev_compile_call_expr_patch18 = ScratchBuilder.compile_call_expr

def _builder_compile_call_expr_patch18b(self: ScratchBuilder, expr: CallExpr, parent: Optional[str]) -> str:
    # size() with no args remains the sprite-size reporter from patch12.
    # size(xs) is C++ vector/string size.
    if expr.callee == "size" and len(expr.args) == 0:
        return _old_builder_compile_call_expr_patch18(self, expr, parent)
    return _prev_compile_call_expr_patch18(self, expr, parent)

ScratchBuilder.compile_call_expr = _builder_compile_call_expr_patch18b  # type: ignore[method-assign]

_prev_runtime_call_patch18 = Runtime.call

def _runtime_call_patch18b(self: Runtime, name: str, args: List[Any]) -> Any:
    if name == "size" and len(args) == 0:
        return _old_runtime_call_patch18(self, name, args)
    return _prev_runtime_call_patch18(self, name, args)

Runtime.call = _runtime_call_patch18b  # type: ignore[method-assign]



# =============================================================================
# Patch 19: dot-method surface for professional APIs
# =============================================================================
# Public APIs should look like normal programming interfaces:
#   v.push_back(x), v.sort(), v.size()
#   pq.push(priority, value), pq.top(), pq.pop()
#   dsu.unite(a, b), fw.sum(i), files.read("config.txt"), pen.down()
# The compiler lowers these method calls to the existing vanilla-Scratch-safe
# functions/procedures.  The underscore names remain only as ABI/internal aliases.

VERSION = "0.9.0-patch19-dot-methods"
KEYWORDS.update({"priority_queue", "max_priority_queue", "stack", "queue", "deque", "fenwick", "bit", "dsu"})

_SBG_OBJECT_TYPE_NAMES_PATCH19 = {"priority_queue", "max_priority_queue", "stack", "queue", "deque", "fenwick", "bit", "dsu"}


def _sbg_method_copy_loc_patch19(dst: Any, src: Any) -> Any:
    for attr in ("filename", "line", "col"):
        if hasattr(src, attr):
            setattr(dst, attr, getattr(src, attr))
    return dst


def _sbg_call_patch19(name: str, args: List[Any], loc: Any) -> CallExpr:
    return _sbg_method_copy_loc_patch19(CallExpr(name, args), loc)


def _sbg_method_lower_patch19(receiver: Any, method: str, args: List[Any], parser: Optional[Parser] = None) -> CallExpr:
    """Lower obj.method(args...) into an ordinary CallExpr.

    Two families are supported:
    1. Real list/vector methods: receiver is passed as first argument.
    2. Scratch-compatible singleton containers such as pq/dsu/fw/files/pen, where
       the receiver name selects a hidden global implementation.
    """
    rname = receiver.name if isinstance(receiver, VarExpr) else ""
    rtype = ""
    if parser is not None:
        rtype = getattr(parser, "sbg_object_types", {}).get(rname, "")

    # vector/list/string-ish methods. These are the normal public names; they
    # lower to existing builtins/std functions that already compile to vanilla blocks.
    list_methods = {
        "push": "push", "add": "push", "push_back": "push_back",
        "pop": "pop_back", "pop_back": "pop_back", "pop_front": "pop_front",
        "clear": "clear", "erase": "erase", "insert": "insert_at", "insert_at": "insert_at",
        "set": "setItem", "set_at": "setItem", "replace": "setItem",
        "resize": "resize", "assign": "assign", "fill": "fill",
        "swap": "swap_items", "swap_items": "swap_items",
        "sort": "sort", "sort_desc": "sort_desc", "reverse": "reverse",
        "size": "size", "len": "size", "empty": "empty",
        "front": "front", "back": "back", "at": "at", "get": "at",
        "contains": "contains", "lower_bound": "lower_bound", "upper_bound": "upper_bound", "binary_search": "binary_search",
    }

    # Special singleton containers. These names intentionally read like object APIs,
    # but compile to global Scratch lists because Scratch has no real object/list refs.
    pq_methods = {
        "clear": "pq_clear", "size": "pq_size", "empty": "pq_empty",
        "push": "pq_push", "pop": "pq_pop", "top": "pq_top", "top_key": "pq_top_key",
        "popped": "pq_popped", "popped_key": "pq_popped_key", "error": "pq_error_value",
    }
    maxpq_methods = {
        "clear": "maxpq_clear", "size": "maxpq_size", "empty": "maxpq_empty",
        "push": "maxpq_push", "pop": "maxpq_pop", "top": "maxpq_top", "top_key": "maxpq_top_key",
        "popped": "maxpq_popped", "popped_key": "maxpq_popped_key", "error": "maxpq_error_value",
    }
    dsu_methods = {
        "init": "make_set", "make_set": "make_set", "find": "find_set", "find_set": "find_set",
        "unite": "unite", "union": "unite", "same": "same", "size": "comp_size", "component_size": "comp_size",
    }
    fw_methods = {
        "init": "fw_init", "add": "fw_add", "sum": "fw_sum", "range": "fw_range", "range_sum": "fw_range",
    }
    dq_methods = {
        "clear": "dqClear", "size": "dqSize", "empty": "dqEmpty",
        "push_back": "dqPushBack", "push_front": "dqPushFront",
        "front": "dqFront", "back": "dqBack", "pop_front": "dqPopFront", "pop_back": "dqPopBack",
        "push": "dqPushBack", "pop": "dqPopFront",
    }
    st_methods = {"clear":"stClear", "push":"stPush", "top":"stTop", "pop":"stPop", "empty":"stEmpty", "size":"stSize"}
    qu_methods = {"clear":"quClear", "push":"quPush", "front":"quFront", "pop":"quPop", "empty":"quEmpty", "size":"quSize"}
    file_methods = {
        "count": "fileCount", "name": "fileName", "exists": "fileExists", "open": "fileOpen",
        "read": "fileReadAll", "read_all": "fileReadAll", "size": "fileSize", "lines": "fileLines",
        "line": "fileLine", "read_line": "fileReadLine", "contains": "fileContains",
        "dump": "fileDump", "debug": "fileDebugList", "list": "fileDebugList",
    }
    pen_methods = {
        "reset": "penReset", "use": "penUse", "clear": "penClear", "erase": "penClear",
        "down": "penDown", "up": "penUp", "stamp": "penStamp",
        "color": "penSetColor", "set_color": "penSetColor", "size": "penSetSize", "set_size": "penSetSize",
        "change_size": "penChangeSize", "param": "penSetParam", "change_param": "penChangeParam",
        "hue": "penSetHue", "change_hue": "penChangeHue", "saturation": "penSetSaturation",
        "change_saturation": "penChangeSaturation", "brightness": "penSetBrightness",
        "change_brightness": "penChangeBrightness", "transparency": "penSetTransparency",
        "change_transparency": "penChangeTransparency", "line": "penLine", "rect": "penRect",
        "filled_rect": "penFilledRect", "circle": "penCircle", "filled_circle": "penFilledCircle",
        "grid": "penGrid", "axes": "penAxes", "point": "penPoint", "points_clear": "penPointsClear",
        "polyline": "penPolylineFromPoints", "goto_draw": "penGotoDraw",
    }
    console_methods = {"log":"log", "info":"logInfo", "warn":"logWarn", "error":"logError", "sep":"logSeparator", "header":"logHeader", "clear":"clearTerminal"}
    sprite_methods = {
        "set_x":"setX", "setX":"setX", "set_y":"setY", "setY":"setY", "change_x":"changeX", "changeX":"changeX",
        "change_y":"changeY", "changeY":"changeY", "goto":"goToXY", "go_to":"goToXY", "goToXY":"goToXY",
        "x":"x", "y":"y", "move":"move", "turn_right":"turnRight", "turnRight":"turnRight",
        "turn_left":"turnLeft", "turnLeft":"turnLeft", "direction":"direction", "set_direction":"setDirection", "setDirection":"setDirection",
        "show":"show", "hide":"hide", "size":"size", "set_size":"setSize", "setSize":"setSize",
    }

    singleton: Optional[Dict[str, str]] = None
    if rname == "pq" or rtype == "priority_queue": singleton = pq_methods
    elif rname == "maxpq" or rtype == "max_priority_queue": singleton = maxpq_methods
    elif rname == "dsu" or rtype == "dsu": singleton = dsu_methods
    elif rname in {"fw", "bit", "fenwick"} or rtype in {"fenwick", "bit"}: singleton = fw_methods
    elif rname in {"dq", "deque"} or rtype == "deque": singleton = dq_methods
    elif rname in {"st", "stack"} or rtype == "stack": singleton = st_methods
    elif rname in {"qu", "queue"} or rtype == "queue": singleton = qu_methods
    elif rname in {"file", "files", "fs"}: singleton = file_methods
    elif rname == "pen": singleton = pen_methods
    elif rname == "console": singleton = console_methods
    elif rname in {"this", "sprite"}: singleton = sprite_methods

    if singleton and method in singleton:
        return _sbg_call_patch19(singleton[method], args, receiver)

    if method in list_methods:
        return _sbg_call_patch19(list_methods[method], [receiver, *args], receiver)

    # Final fallback: obj.foo(a,b) -> foo(obj,a,b).  This is useful for userland
    # libraries that deliberately write free functions but want method syntax.
    return _sbg_call_patch19(method, [receiver, *args], receiver)


_old_parser_init_patch19 = Parser.__init__

def _parser_init_patch19(self: Parser, tokens: List[Token], filename: str = "<source>"):
    _old_parser_init_patch19(self, tokens, filename)
    self.sbg_object_types: Dict[str, str] = {}

Parser.__init__ = _parser_init_patch19  # type: ignore[method-assign]


_old_parse_statement_patch19 = Parser.parse_statement

def _parser_parse_statement_patch19(self: Parser) -> Any:
    start_token = self.peek()
    if self.peek().value in _SBG_OBJECT_TYPE_NAMES_PATCH19 and self.peek().kind in {"KW", "IDENT"}:
        typ = self.advance().value
        _parser_skip_template_patch18(self)
        name = self.expect_ident()
        # Optional empty constructor syntax: priority_queue pq(); / stack st;
        if self.match("("):
            self.expect(")")
        self.expect(";")
        self.sbg_object_types[name] = typ
        # It is a compile-time handle to Scratch-global storage.  A harmless hidden
        # variable keeps native runtime happy if the name is ever logged/debugged.
        return self.loc(VarDecl(name, Literal(0), True), start_token)
    return _old_parse_statement_patch19(self)

Parser.parse_statement = _parser_parse_statement_patch19  # type: ignore[method-assign]


_old_parse_postfix_patch19 = Parser.parse_postfix

def _parser_parse_postfix_patch19(self: Parser) -> Any:
    expr = self.parse_primary()
    while True:
        if self.match("("):
            if not isinstance(expr, VarExpr):
                raise self.error("only named function calls are supported")
            args: List[Any] = []
            if not self.at(")"):
                while True:
                    args.append(self.parse_expr())
                    if not self.match(","):
                        break
            self.expect(")")
            expr = self.loc(CallExpr(expr.name, args), expr)
            continue
        if self.match("."):
            dot_token = self.toks[self.i - 1]
            if self.peek().kind not in {"IDENT", "KW"}:
                raise self.error("expected method name after '.'")
            method = self.advance().value
            args: List[Any] = []
            if self.match("("):
                if not self.at(")"):
                    while True:
                        args.append(self.parse_expr())
                        if not self.match(","):
                            break
                self.expect(")")
            else:
                raise self.error("method access needs a call, e.g. v.size()")
            expr = self.loc(_sbg_method_lower_patch19(expr, method, args, self), dot_token)
            continue
        break
    return expr

Parser.parse_postfix = _parser_parse_postfix_patch19  # type: ignore[method-assign]


# Extra public aliases that method syntax lowers to.  They are intentionally
# compact/professional, not educational names.
BUILTIN_EXPR_NAMES.update({"pq_error_value", "maxpq_error_value"})
BUILTIN_NAMES = BUILTIN_EXPR_NAMES | BUILTIN_STMT_NAMES

_old_runtime_call_patch19 = Runtime.call

def _runtime_call_patch19(self: Runtime, name: str, args: List[Any]) -> Any:
    if name == "pq_error_value":
        return self.vars.get("pq_error", "")
    if name == "maxpq_error_value":
        return self.vars.get("maxpq_error", "")
    return _old_runtime_call_patch19(self, name, args)

Runtime.call = _runtime_call_patch19  # type: ignore[method-assign]

_old_project_ensure_patch19 = _project_ensure_patch18

def _project_ensure_patch19(project: Dict[str, Any]) -> Dict[str, Any]:
    project = _old_project_ensure_patch19(project)
    meta = project.setdefault("meta", {})
    meta["agent"] = f"StageBG/SBG {VERSION}"
    meta["stagebgPatch19"] = "Dot-method API surface: v.sort(), pq.push(), dsu.unite(), fw.sum(), files.read(), pen.down()."
    return project

_old_compiler_compile_patch19 = Compiler.compile

def _compiler_compile_patch19(self: Compiler) -> Dict[str, Any]:
    return _project_ensure_patch19(_old_compiler_compile_patch19(self))

Compiler.compile = _compiler_compile_patch19  # type: ignore[method-assign]



# =============================================================================
# Patch 20: C++ compatibility layer + Scratch comments/layout
# =============================================================================
# This layer was driven by porting real C++-style code to SBG.
# It adds the C++ constructs that are not Scratch concepts at all but are needed
# by adult/professional code: typed functions, typed parameters, const typed
# declarations, struct declarations, field/index syntax, stream-like cout/cin,
# math/random aliases, and preservation of source comments as Scratch comments.

VERSION = "0.9.0-patch20-cpp-compat-comments-layout"

for _sym in ("++", "--", "<<", ">>"):
    if _sym not in MULTI:
        MULTI.insert(0, _sym)
KEYWORDS.update({"struct", "void", "static"})
_CPP_TYPE_KWS.update({"void"})


def _sbg_cpp_preprocess_patch20(text: str) -> str:
    # Keep the patch18 include preprocessor, then normalize std:: names.  This is
    # deliberately not a C++ preprocessor; it is a surface-syntax adapter.
    text = _sbg_preprocess_cpp_surface(text)
    text = text.replace("std::", "")
    return text


def _sbg_extract_comments_patch20(text: str) -> List[Dict[str, Any]]:
    """Collect // and /* */ comments for Scratch workspace comments."""
    comments: List[Dict[str, Any]] = []
    i = 0
    line = 1
    col = 1
    n = len(text)
    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if ch == "\n":
            line += 1; col = 1; i += 1; continue
        if ch == "/" and nxt == "/":
            start_line, start_col = line, col
            i += 2; col += 2
            s = ""
            while i < n and text[i] != "\n":
                s += text[i]; i += 1; col += 1
            s = s.strip()
            if s:
                comments.append({"line": start_line, "col": start_col, "text": s})
            continue
        if ch == "/" and nxt == "*":
            start_line, start_col = line, col
            i += 2; col += 2
            s = ""
            while i < n:
                if i + 1 < n and text[i] == "*" and text[i + 1] == "/":
                    i += 2; col += 2
                    break
                if text[i] == "\n":
                    s += "\n"; i += 1; line += 1; col = 1
                else:
                    s += text[i]; i += 1; col += 1
            s = s.strip()
            if s:
                comments.append({"line": start_line, "col": start_col, "text": s})
            continue
        i += 1; col += 1
    return comments


# Patch the Lexer init again.  Patch18 already preprocesses, so call the original
# raw initializer instead of nesting the include processor twice.
def _lexer_init_patch20(self: Lexer, text: str, filename: str = "<source>"):
    return _old_lexer_init_patch18(self, _sbg_cpp_preprocess_patch20(text), filename)

Lexer.__init__ = _lexer_init_patch20  # type: ignore[method-assign]


def _parser_skip_cpp_type_patch20(self: Parser) -> str:
    """Read and discard a C++-like type annotation; return a compact name."""
    parts: List[str] = []
    if self.match_kw("const"):
        parts.append("const")
    if self.match_kw("static"):
        parts.append("static")
    # Accept std:: prefixes if a user disabled preprocessing somehow.
    if self.peek().value == "std" and self.peek(1).value == "::":
        self.advance(); self.advance()
    if self.peek().kind not in {"IDENT", "KW"}:
        raise self.error(f"expected type name, got {self.peek().value!r}")
    typ = self.advance().value
    parts.append(typ)
    if typ == "long" and self.peek().value == "long":
        parts.append(self.advance().value)
    _parser_skip_template_patch18(self)
    # Ignore pointer/reference markers for now; Scratch has no address model.
    while self.peek().value in {"*", "&"}:
        parts.append(self.advance().value)
    return " ".join(parts)


def _parser_try_cpp_type_start_patch20(self: Parser) -> bool:
    v = self.peek().value
    if v in {"const", "static", "void", "vector", "auto", "int", "long", "double", "float", "string", "char"}:
        return True
    if v == "bool":
        return True
    # User-defined struct names are identifiers followed by another identifier or function name.
    return self.peek().kind == "IDENT" and self.peek(1).kind == "IDENT"


def _parser_parse_typed_params_patch20(self: Parser) -> List[str]:
    params: List[str] = []
    self.expect("(")
    if not self.at(")"):
        while True:
            # Allow untyped legacy params and typed C++ params.
            if self.peek().value == "std" and self.peek(1).value == "::":
                self.advance(); self.advance()
            if self.peek().value in {"const", "static", "void", "vector", "auto", "int", "long", "double", "float", "string", "char", "bool"} or (self.peek().kind == "IDENT" and self.peek(1).kind in {"IDENT", "KW"}):
                # If this is actually just `x` followed by comma/), don't skip it as type.
                if not (self.peek().kind == "IDENT" and self.peek(1).value in {",", ")"}):
                    _parser_skip_cpp_type_patch20(self)
            # Optional reference/pointer markers after type.
            while self.peek().value in {"*", "&"}:
                self.advance()
            name = self.expect_ident()
            params.append(name)
            if not self.match(","):
                break
    self.expect(")")
    return params


def _parser_parse_cpp_struct_patch20(self: Parser, start_token: Token) -> Any:
    name = self.expect_ident()
    fields: List[Tuple[str, str]] = []
    self.expect("{")
    while not self.at("}"):
        if self.peek().kind == "EOF":
            raise self.error("unterminated struct declaration")
        typ = _parser_skip_cpp_type_patch20(self)
        fname = self.expect_ident()
        # Ignore optional array brackets in field declarations for now.
        if self.match("["):
            while not self.at("]"):
                self.advance()
            self.expect("]")
        self.expect(";")
        fields.append((fname, typ))
    self.expect("}")
    self.expect(";")
    if not hasattr(self, "sbg_structs"):
        self.sbg_structs = {}
    self.sbg_structs[name] = fields
    node = VarDecl(f"__struct_{name}", Literal(0), False)
    return self.loc(node, start_token)


def _parser_parse_cpp_initializer_patch20(self: Parser) -> Any:
    if self.match("{"):
        items: List[Any] = []
        if not self.at("}"):
            while True:
                items.append(_parser_parse_cpp_initializer_patch20(self) if self.at("{") else self.parse_expr())
                if not self.match(","):
                    break
        self.expect("}")
        return ArrayExpr(items)
    return self.parse_expr()


def _parser_parse_cpp_decl_or_func_patch20(self: Parser, start_token: Token) -> Any:
    typ = _parser_skip_cpp_type_patch20(self)
    # Constructor declarations like `uniform_real_distribution<double> dis(-1.0, 1.0);`
    # are treated as ordinary variables with optional constructor metadata.
    name = self.expect_ident()
    if self.at("("):
        # Function definition when followed by a block after params.
        saved = self.i
        try:
            params = _parser_parse_typed_params_patch20(self)
            if self.at("{"):
                body = self.parse_block()
                if name == "main":
                    return self.loc(EventDecl("action", "input", body), start_token)
                return self.loc(ProcDecl(name, params, body), start_token)
            # Otherwise this was a constructor-style variable declaration.
            self.i = saved
        except ParseError:
            self.i = saved
        self.expect("(")
        ctor_args: List[Any] = []
        if not self.at(")"):
            while True:
                ctor_args.append(self.parse_expr())
                if not self.match(","):
                    break
        self.expect(")")
        self.expect(";")
        if "vector" in typ:
            # vector<T> v(n) => empty list plus resize at runtime/compile when used explicitly.
            return self.loc(ListDecl(name, []), start_token)
        # Random/distribution objects become tiny scalar handles; the compat runtime ignores them.
        return self.loc(VarDecl(name, Literal(0), True), start_token)

    expr: Any = Literal(0)
    if any(t in typ for t in ("string", "char")):
        expr = Literal("")
    elif "bool" in typ:
        expr = Literal(False)
    is_vector = "vector" in typ
    if self.match("="):
        init = _parser_parse_cpp_initializer_patch20(self)
        if is_vector:
            if isinstance(init, ArrayExpr):
                self.expect(";")
                return self.loc(ListDecl(name, init.items), start_token)
            # Assignment from another vector is value assignment in native mode; in Scratch
            # it is represented as a variable unless the user mutates it as a concrete list.
            expr = init
        else:
            expr = init
    self.expect(";")
    if is_vector:
        return self.loc(ListDecl(name, []), start_token)
    return self.loc(VarDecl(name, expr, True), start_token)


@dataclass
class LValueAssignStmt:
    op: str
    target: Any
    expr: Any


def _sbg_field_ref(obj: Any, field: str) -> CallExpr:
    return CallExpr("__field_ref", [obj, Literal(field)])


def _sbg_index_ref(obj: Any, index: Any) -> CallExpr:
    return CallExpr("__index0_ref", [obj, index])


def _parser_parse_cout_patch20(self: Parser, start_token: Token) -> Any:
    self.advance()  # cout
    parts: List[Any] = []
    while self.match("<<"):
        if self.peek().kind == "STRING":
            parts.append(self.parse_expr())
        elif self.peek().kind == "IDENT" and self.peek().value in {"endl"}:
            self.advance(); parts.append(Literal("\n"))
        else:
            parts.append(self.parse_expr())
    self.expect(";")
    if not parts:
        parts = [Literal("")]
    return self.loc(ExprStmt(CallExpr("cout", parts)), start_token)


def _parser_parse_cin_patch20(self: Parser, start_token: Token) -> Any:
    self.advance()  # cin
    names: List[Any] = []
    while self.match(">>"):
        if self.peek().kind != "IDENT":
            raise self.error("cin target must be an identifier")
        names.append(Literal(self.advance().value))
    self.expect(";")
    return self.loc(ExprStmt(CallExpr("cin", names)), start_token)


_old_parse_top_or_stmt_patch20 = Parser.parse_top_or_stmt

def _parser_parse_top_or_stmt_patch20(self: Parser) -> Any:
    start_token = self.peek()
    if self.match_kw("struct"):
        return _parser_parse_cpp_struct_patch20(self, start_token)
    if _parser_try_cpp_type_start_patch20(self):
        # Avoid stealing ordinary expression statements like `foo();`.
        saved = self.i
        try:
            return _parser_parse_cpp_decl_or_func_patch20(self, start_token)
        except ParseError:
            self.i = saved
    return _old_parse_top_or_stmt_patch20(self)

Parser.parse_top_or_stmt = _parser_parse_top_or_stmt_patch20  # type: ignore[method-assign]


_old_parse_statement_patch20 = Parser.parse_statement

def _parser_parse_statement_patch20(self: Parser) -> Any:
    start_token = self.peek()
    if self.peek().value == "cout":
        return _parser_parse_cout_patch20(self, start_token)
    if self.peek().value == "cin":
        return _parser_parse_cin_patch20(self, start_token)
    if self.peek().kind == "IDENT" and self.peek(1).value in ("++", "--"):
        name = self.advance().value; op = self.advance().value; self.expect(";")
        return self.loc(AssignStmt(name, "+=" if op == "++" else "-=", Literal(1)), start_token)
    if self.match_kw("struct"):
        return _parser_parse_cpp_struct_patch20(self, start_token)
    if _parser_try_cpp_type_start_patch20(self):
        saved = self.i
        try:
            return _parser_parse_cpp_decl_or_func_patch20(self, start_token)
        except ParseError:
            self.i = saved
    # General lvalue assignment: a[i] = x; obj.field += x;
    saved = self.i
    try:
        lhs = self.parse_expr()
        if self.peek().value in ("=", "+=", "-=", "*=", "/=", "%="):
            op = self.advance().value
            rhs = self.parse_expr()
            self.expect(";")
            if isinstance(lhs, VarExpr):
                return self.loc(AssignStmt(lhs.name, op, rhs), start_token)
            return self.loc(LValueAssignStmt(op, lhs, rhs), start_token)
        self.expect(";")
        return self.loc(ExprStmt(lhs), start_token)
    except ParseError:
        self.i = saved
    return _old_parse_statement_patch20(self)

Parser.parse_statement = _parser_parse_statement_patch20  # type: ignore[method-assign]


_old_parse_for_patch20 = Parser.parse_for

def _parser_parse_for_patch20(self: Parser, start_token: Token) -> ForStmt:
    saved = self.i
    self.expect("(")
    # for(double x : v) / for(Struct item : row)
    if _parser_try_cpp_type_start_patch20(self):
        try:
            _parser_skip_cpp_type_patch20(self)
            name = self.expect_ident()
            if self.match(":"):
                source = self.parse_expr()
                self.expect(")")
                return _sbg_make_for_each(start_token, name, source, self.parse_block(), declare_value=True)
        except ParseError:
            pass
    self.i = saved
    return _old_parse_for_patch20(self, start_token)

Parser.parse_for = _parser_parse_for_patch20  # type: ignore[method-assign]


_old_parse_postfix_patch20 = Parser.parse_postfix

def _parser_parse_postfix_patch20(self: Parser) -> Any:
    expr = self.parse_primary()
    while True:
        if self.match("("):
            if not isinstance(expr, VarExpr):
                raise self.error("only named function calls are supported")
            args: List[Any] = []
            if not self.at(")"):
                while True:
                    args.append(self.parse_expr())
                    if not self.match(","):
                        break
            self.expect(")")
            expr = self.loc(CallExpr(expr.name, args), expr)
            continue
        if self.match("["):
            tok = self.toks[self.i - 1]
            idx = self.parse_expr()
            self.expect("]")
            expr = self.loc(_sbg_index_ref(expr, idx), tok)
            continue
        if self.match("."):
            dot_token = self.toks[self.i - 1]
            if self.peek().kind not in {"IDENT", "KW"}:
                raise self.error("expected field/method name after '.'")
            field_or_method = self.advance().value
            if self.match("("):
                args: List[Any] = []
                if not self.at(")"):
                    while True:
                        args.append(self.parse_expr())
                        if not self.match(","):
                            break
                self.expect(")")
                expr = self.loc(_sbg_method_lower_patch19(expr, field_or_method, args, self), dot_token)
            else:
                expr = self.loc(_sbg_field_ref(expr, field_or_method), dot_token)
            continue
        break
    return expr

Parser.parse_postfix = _parser_parse_postfix_patch20  # type: ignore[method-assign]


def _sbg_ref_static_name_patch20(expr: Any) -> Optional[str]:
    """Return a Scratch variable/list name for a statically-known lvalue."""
    if isinstance(expr, VarExpr):
        return expr.name
    if isinstance(expr, CallExpr) and expr.callee == "__field_ref" and len(expr.args) == 2 and isinstance(expr.args[1], Literal):
        base = _sbg_ref_static_name_patch20(expr.args[0])
        if base is not None:
            return f"{base}.{expr.args[1].value}"
    return None


# Runtime support for C++ refs, zero-based indexing, cout/cin, math/random.
_old_runtime_eval_patch20 = Runtime._eval

def _runtime_eval_patch20(self: Runtime, expr: Any) -> Any:
    if isinstance(expr, CallExpr) and expr.callee == "__field_ref":
        obj = self.eval(expr.args[0]); field = str(self.eval(expr.args[1]))
        if isinstance(obj, dict):
            return obj.get(field, 0)
        name = _sbg_ref_static_name_patch20(expr)
        if name and name in self.vars:
            return self.vars[name]
        if name and name in self.lists:
            return self.lists[name]
        return 0
    if isinstance(expr, CallExpr) and expr.callee == "__index0_ref":
        obj = self.eval(expr.args[0]); idx = int(float(self.eval(expr.args[1])))
        if isinstance(obj, str):
            return _sbg_vec_at0_runtime_patch20(obj, idx)
        return obj[idx]
    return _old_runtime_eval_patch20(self, expr)

Runtime._eval = _runtime_eval_patch20  # type: ignore[method-assign]


def _sbg_vec_tokens_runtime_patch20(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(x) for x in value]
    text = str(value).strip()
    if not text:
        return []
    # Scratch list reporter joins items with spaces. Also accept CSV/semicolon.
    if "\x1f" in text:
        return [x for x in text.split("\x1f") if x != ""]
    if "," in text:
        return [x.strip() for x in text.split(",") if x.strip()]
    return [x for x in text.split() if x]


def _sbg_num_or_text_patch20(x: str) -> Any:
    try:
        f = float(x)
        return int(f) if f.is_integer() else f
    except Exception:
        return x


def _sbg_vec_at0_runtime_patch20(value: Any, idx: int) -> Any:
    vals = _sbg_vec_tokens_runtime_patch20(value)
    if idx < 0 or idx >= len(vals):
        return 0
    return _sbg_num_or_text_patch20(vals[idx])


_old_exec_stmt_patch20 = Runtime._exec_stmt

def _runtime_exec_stmt_patch20(self: Runtime, stmt: Any) -> None:
    if isinstance(stmt, LValueAssignStmt):
        rhs = self.eval(stmt.expr)
        def apply(old: Any) -> Any:
            if stmt.op == "=": return rhs
            if stmt.op == "+=": return old + rhs
            if stmt.op == "-=": return old - rhs
            if stmt.op == "*=": return old * rhs
            if stmt.op == "/=": return old / rhs
            if stmt.op == "%=": return old % rhs
            return rhs
        if isinstance(stmt.target, CallExpr) and stmt.target.callee == "__field_ref":
            name = _sbg_ref_static_name_patch20(stmt.target)
            if name is None:
                raise RuntimeSBGError("dynamic field assignment is not supported yet")
            self.vars[name] = apply(self.vars.get(name, 0))
            return None
        if isinstance(stmt.target, CallExpr) and stmt.target.callee == "__index0_ref":
            obj_expr, idx_expr = stmt.target.args
            idx = int(float(self.eval(idx_expr)))
            name = _sbg_ref_static_name_patch20(obj_expr)
            if name and name in self.lists:
                lst = self.lists[name]
                lst[idx] = apply(lst[idx])
                return None
            obj = self.eval(obj_expr)
            if isinstance(obj, list):
                obj[idx] = apply(obj[idx])
                return None
            raise RuntimeSBGError("index assignment needs a mutable vector/list")
        raise RuntimeSBGError("unsupported assignment target")
    return _old_exec_stmt_patch20(self, stmt)

Runtime._exec_stmt = _runtime_exec_stmt_patch20  # type: ignore[method-assign]


_old_runtime_call_patch20 = Runtime.call

def _runtime_call_patch20(self: Runtime, name: str, args: List[Any]) -> Any:
    if name in {"cout", "print"}:
        text = "".join(str(a) for a in args)
        # C++ cout may contain newlines; mirror it as separate terminal rows.
        rows = text.split("\n")
        for row in rows:
            if row != "":
                self.call("log", [row])
        return None
    if name == "println":
        return self.call("log", ["".join(str(a) for a in args)])
    if name == "cin":
        # Native runner asks from stdin. Scratch compiler emits ask-and-wait.
        for target in args:
            val = input(">> ")
            try:
                val = float(val)
                if val.is_integer(): val = int(val)
            except Exception:
                pass
            self.vars[str(target)] = val
        return None
    if name == "at0":
        return _sbg_vec_at0_runtime_patch20(args[0], int(float(args[1])))
    if name == "vec_size":
        return len(_sbg_vec_tokens_runtime_patch20(args[0]))
    if name == "pow":
        return math.pow(float(args[0]), float(args[1]))
    if name == "exp": return math.exp(float(args[0]))
    if name == "ln": return math.log(float(args[0]))
    if name == "log10": return math.log10(float(args[0]))
    if name == "sin": return math.sin(float(args[0]))
    if name == "cos": return math.cos(float(args[0]))
    if name == "tan": return math.tan(float(args[0]))
    if name in {"randuble", "rand_double", "random_double"}:
        lo = float(args[0]) if len(args) >= 1 else -1.0
        hi = float(args[1]) if len(args) >= 2 else 1.0
        return random.uniform(lo, hi)
    if name in {"dis", "uniform_real"}:
        return random.uniform(-1.0, 1.0)
    if name == "__new_struct":
        return {"__type": str(args[0]) if args else "struct"}
    return _old_runtime_call_patch20(self, name, args)

Runtime.call = _runtime_call_patch20  # type: ignore[method-assign]


# Scratch compile support for field/index refs and C++ I/O/math aliases.
BUILTIN_EXPR_NAMES.update({"at0", "vec_size", "pow", "exp", "ln", "log10", "sin", "cos", "tan", "randuble", "rand_double", "random_double", "__field_ref", "__index0_ref"})
BUILTIN_STMT_NAMES.update({"cout", "print", "println", "cin"})
BUILTIN_NAMES = BUILTIN_EXPR_NAMES | BUILTIN_STMT_NAMES

_old_builder_compile_expr_patch20 = ScratchBuilder._compile_expr

def _builder_compile_expr_patch20(self: ScratchBuilder, expr: Any, parent: Optional[str] = None) -> str:
    if isinstance(expr, CallExpr) and expr.callee == "__field_ref":
        name = _sbg_ref_static_name_patch20(expr)
        if name is None:
            raise CompileError("dynamic field access cannot be compiled to vanilla Scratch")
        if name in self.lists:
            return self.add_block("data_listcontents", parent=parent, fields={"LIST": [name, self.list_id(name)]})
        return self.add_block("data_variable", parent=parent, fields={"VARIABLE": [name, self.var_id(name)]})
    if isinstance(expr, CallExpr) and expr.callee == "__index0_ref":
        obj, idx = expr.args
        name = _sbg_ref_static_name_patch20(obj)
        if name is not None:
            # Statically-known Scratch list: use real list item with 0->1 index conversion.
            self.list_id(name)
            bid = self.add_block("data_itemoflist", parent=parent, fields={"LIST": [name, self.list_id(name)]}, inputs={})
            self.blocks[bid]["inputs"]["INDEX"] = self.expr_input(BinaryExpr(idx, "+", Literal(1)), bid)
            return bid
        # Dynamic/list-parameter vector: use std/bits vec_at0 return proc.
        return self.compile_call_expr(CallExpr("at0", [obj, idx]), parent)
    return _old_builder_compile_expr_patch20(self, expr, parent)

ScratchBuilder._compile_expr = _builder_compile_expr_patch20  # type: ignore[method-assign]

_old_builder_require_list_patch20 = ScratchBuilder.require_list_expr

def _builder_require_list_expr_patch20(self: ScratchBuilder, expr: Any) -> str:
    name = _sbg_ref_static_name_patch20(expr)
    if name is not None:
        self.list_id(name)
        return name
    return _old_builder_require_list_patch20(self, expr)

ScratchBuilder.require_list_expr = _builder_require_list_expr_patch20  # type: ignore[method-assign]

_old_builder_compile_call_expr_patch20 = ScratchBuilder.compile_call_expr

def _builder_compile_call_expr_patch20(self: ScratchBuilder, expr: CallExpr, parent: Optional[str]) -> str:
    name, a = expr.callee, expr.args
    if name == "at0":
        self.need_args(name, a, 2)
        # Static list fast path.
        lname = _sbg_ref_static_name_patch20(a[0])
        if lname is not None:
            self.list_id(lname)
            bid = self.add_block("data_itemoflist", parent=parent, fields={"LIST": [lname, self.list_id(lname)]}, inputs={})
            self.blocks[bid]["inputs"]["INDEX"] = self.expr_input(BinaryExpr(a[1], "+", Literal(1)), bid)
            return bid
        raise CompileError("at0() for dynamic vector parameters needs import \"bits\" so vec_at0 can be lowered as a return procedure")
    if name == "vec_size":
        self.need_args(name, a, 1)
        lname = _sbg_ref_static_name_patch20(a[0])
        if lname is not None:
            self.list_id(lname)
            return self.add_block("data_lengthoflist", parent=parent, fields={"LIST": [lname, self.list_id(lname)]})
        return self.compile_call_expr(CallExpr("len", a), parent)
    if name == "pow":
        self.need_args(name, a, 2)
        # C++ code often uses pow(e, x). Vanilla Scratch has native e^ reporter.
        first = a[0]
        if (isinstance(first, VarExpr) and first.name == "e") or (isinstance(first, Literal) and abs(float(first.value) - math.e) < 0.01):
            bid = self.add_block("operator_mathop", parent=parent, inputs={}, fields={"OPERATOR": ["e ^", None]})
            self.blocks[bid]["inputs"]["NUM"] = self.expr_input(a[1], bid)
            return bid
        raise CompileError("general pow(base, exp) is not a primitive reporter in vanilla Scratch; use powi() for integer powers or pow(e, x) for exp")
    if name in {"exp", "ln", "log10", "sin", "cos", "tan"}:
        self.need_args(name, a, 1)
        op = {"exp":"e ^", "ln":"ln", "log10":"log", "sin":"sin", "cos":"cos", "tan":"tan"}[name]
        bid = self.add_block("operator_mathop", parent=parent, inputs={}, fields={"OPERATOR": [op, None]})
        self.blocks[bid]["inputs"]["NUM"] = self.expr_input(a[0], bid)
        return bid
    if name in {"randuble", "rand_double", "random_double"}:
        lo = a[0] if len(a) >= 1 else Literal(-1)
        hi = a[1] if len(a) >= 2 else Literal(1)
        return self.compile_call_expr(CallExpr("random", [lo, hi]), parent)
    return _old_builder_compile_call_expr_patch20(self, expr, parent)

ScratchBuilder.compile_call_expr = _builder_compile_call_expr_patch20  # type: ignore[method-assign]

_old_builder_compile_call_stmt_patch20 = ScratchBuilder.compile_call_stmt

def _builder_compile_call_stmt_patch20(self: ScratchBuilder, expr: CallExpr) -> Optional[str]:
    name, a = expr.callee, expr.args
    if name in {"cout", "print", "println"}:
        val = Literal("") if not a else a[0] if len(a) == 1 else self.join_many(a)
        return self.compile_call_stmt(CallExpr("log", [val]))
    if name == "cin":
        first: Optional[str] = None
        for target in a:
            if not isinstance(target, Literal):
                raise CompileError("cin target metadata must be literal")
            varname = str(target.value)
            ask = self.add_block("sensing_askandwait", inputs={"QUESTION": self.literal_input(">>")})
            setb = self.add_block("data_setvariableto", fields={"VARIABLE": [varname, self.var_id(varname)]}, inputs={})
            ans = self.add_block("sensing_answer", parent=setb)
            self.blocks[setb]["inputs"]["VALUE"] = [1, ans]
            first = self.chain(first, self.chain(ask, setb))
        return first
    return _old_builder_compile_call_stmt_patch20(self, expr)

ScratchBuilder.compile_call_stmt = _builder_compile_call_stmt_patch20  # type: ignore[method-assign]

_old_compile_stmt_patch20 = ScratchBuilder.compile_stmt

def _builder_compile_stmt_patch20(self: ScratchBuilder, stmt: Any) -> Optional[str]:
    if isinstance(stmt, LValueAssignStmt):
        # Compile direct list/field writes when the lvalue is statically known.
        if isinstance(stmt.target, CallExpr) and stmt.target.callee == "__field_ref":
            name = _sbg_ref_static_name_patch20(stmt.target)
            if name is None:
                raise CompileError("dynamic field assignment cannot be compiled to vanilla Scratch")
            old = VarExpr(name)
            expr = stmt.expr
            if stmt.op == "+=": expr = BinaryExpr(old, "+", stmt.expr)
            elif stmt.op == "-=": expr = BinaryExpr(old, "-", stmt.expr)
            elif stmt.op == "*=": expr = BinaryExpr(old, "*", stmt.expr)
            elif stmt.op == "/=": expr = BinaryExpr(old, "/", stmt.expr)
            elif stmt.op == "%=": expr = BinaryExpr(old, "%", stmt.expr)
            bid = self.add_block("data_setvariableto", fields={"VARIABLE": [name, self.var_id(name)]}, inputs={})
            self.blocks[bid]["inputs"]["VALUE"] = self.expr_input(expr, bid)
            return bid
        if isinstance(stmt.target, CallExpr) and stmt.target.callee == "__index0_ref":
            obj, idx = stmt.target.args
            name = _sbg_ref_static_name_patch20(obj)
            if name is None:
                raise CompileError("dynamic index assignment cannot be compiled to vanilla Scratch")
            self.list_id(name)
            old = CallExpr("at0", [obj, idx])
            expr = stmt.expr
            if stmt.op == "+=": expr = BinaryExpr(old, "+", stmt.expr)
            elif stmt.op == "-=": expr = BinaryExpr(old, "-", stmt.expr)
            elif stmt.op == "*=": expr = BinaryExpr(old, "*", stmt.expr)
            elif stmt.op == "/=": expr = BinaryExpr(old, "/", stmt.expr)
            elif stmt.op == "%=": expr = BinaryExpr(old, "%", stmt.expr)
            bid = self.add_block("data_replaceitemoflist", fields={"LIST": [name, self.list_id(name)]}, inputs={})
            self.blocks[bid]["inputs"]["INDEX"] = self.expr_input(BinaryExpr(idx, "+", Literal(1)), bid)
            self.blocks[bid]["inputs"]["ITEM"] = self.expr_input(expr, bid)
            return bid
        raise CompileError("unsupported lvalue assignment for Scratch output")
    return _old_compile_stmt_patch20(self, stmt)

ScratchBuilder.compile_stmt = _builder_compile_stmt_patch20  # type: ignore[method-assign]


# Comment preservation + layout ------------------------------------------------
_old_parse_source_patch20 = parse_source

def parse_source(text: str, filename: str = "<source>") -> Program:  # type: ignore[no-redef]
    program = _old_parse_source_patch20(text, filename)
    setattr(program, "sbg_source_comments", _sbg_extract_comments_patch20(text))
    return program


def _sbg_layout_project_patch20(project: Dict[str, Any], comments: List[Dict[str, Any]]) -> Dict[str, Any]:
    for target in project.get("targets", []):
        if not isinstance(target, dict):
            continue
        blocks = target.get("blocks", {})
        # Stable, column-based layout for readable Scratch workspaces.
        lanes = {
            "event_whenflagclicked": (40, 40),
            "procedures_definition": (420, 40),
            "event_whenbroadcastreceived": (820, 40),
        }
        counts: Dict[str, int] = {k: 0 for k in lanes}
        other_i = 0
        for bid, block in blocks.items():
            if not isinstance(block, dict) or not block.get("topLevel"):
                continue
            op = block.get("opcode")
            if op in lanes:
                x, y0 = lanes[op]
                block["x"] = x
                block["y"] = y0 + counts[op] * 260
                counts[op] += 1
            else:
                block["x"] = 1220
                block["y"] = 40 + other_i * 220
                other_i += 1
        if target.get("isStage") and comments:
            cm: Dict[str, Any] = target.setdefault("comments", {})
            # Put comments in a separate left column, preserving source order.
            for i, c in enumerate(comments[:80]):
                cid = f"sbg_comment_{i+1:04d}"
                cm[cid] = {
                    "blockId": None,
                    "x": 40,
                    "y": 980 + i * 95,
                    "width": 360,
                    "height": 80,
                    "minimized": False,
                    "text": f"L{c.get('line', '?')}: {c.get('text', '')}",
                }
    meta = project.setdefault("meta", {})
    meta["stagebgBlockLayout"] = "patch20 column layout: green flag, procedures, broadcasts, comments"
    return project

_old_compiler_compile_patch20 = Compiler.compile

def _compiler_compile_patch20(self: Compiler) -> Dict[str, Any]:
    project = _old_compiler_compile_patch20(self)
    comments = list(getattr(self.program, "sbg_source_comments", []) or [])
    project = _sbg_layout_project_patch20(project, comments)
    meta = project.setdefault("meta", {})
    meta["agent"] = f"StageBG/SBG {VERSION}"
    meta["stagebgPatch20"] = "C++ typed funcs/params, struct surface, field/index syntax, cout/cin, math/random aliases, Scratch comments and block layout."
    return project

Compiler.compile = _compiler_compile_patch20  # type: ignore[method-assign]



# Patch20b: make zero-based vector indexing safe for procedure parameters.
# When a vector/list is passed to a Scratch custom block, the VM can only pass the
# list reporter text, not the list reference.  `bits/cpp_compat.sbg` provides at0()
# that parses that text.  Static lists still compile to real list item blocks.
_old_builder_lower_expr_patch20b = _builder_lower_expr

def _builder_lower_expr_patch20b(self: ScratchBuilder, expr: Any) -> Tuple[List[Any], Any]:
    if isinstance(expr, CallExpr) and expr.callee == "__index0_ref" and len(expr.args) == 2:
        obj, idx = expr.args
        # Local/procedure parameter: lower to value-return helper at0(obj, idx).
        if isinstance(obj, VarExpr) and obj.name in getattr(self, "current_proc_params", {}):
            return _old_builder_lower_expr_patch20b(self, CallExpr("at0", [obj, idx]))
    return _old_builder_lower_expr_patch20b(self, expr)

_builder_lower_expr = _builder_lower_expr_patch20b  # type: ignore[assignment]

_prev_builder_compile_expr_patch20b = ScratchBuilder._compile_expr

def _builder_compile_expr_patch20b(self: ScratchBuilder, expr: Any, parent: Optional[str] = None) -> str:
    if isinstance(expr, CallExpr) and expr.callee == "__index0_ref" and len(expr.args) == 2:
        obj, idx = expr.args
        if isinstance(obj, VarExpr) and obj.name in getattr(self, "current_proc_params", {}):
            raise CompileError("internal lowering error: dynamic vector index reached reporter compiler; import \"bits\" and use value lowering")
    return _prev_builder_compile_expr_patch20b(self, expr, parent)

ScratchBuilder._compile_expr = _builder_compile_expr_patch20b  # type: ignore[method-assign]



# Patch20c: preserve parse metadata (comments) when file-embedding wraps Program.
_old_program_with_embedded_files_patch20c = _program_with_embedded_files

def _program_with_embedded_files_patch20c(program: Program, source_path: Union[str, Path], *, embeds: Optional[List[str]] = None, embed_dirs: Optional[List[str]] = None) -> Program:
    wrapped = _old_program_with_embedded_files_patch20c(program, source_path, embeds=embeds, embed_dirs=embed_dirs)
    for key, value in getattr(program, "__dict__", {}).items():
        if key != "body":
            setattr(wrapped, key, value)
    return wrapped

_program_with_embedded_files = _program_with_embedded_files_patch20c  # type: ignore[assignment]



# Patch20d: nested C++ templates like vector<vector<int>> when lexer has >>.
def _parser_skip_template_patch18(self: Parser) -> None:  # type: ignore[no-redef]
    if not self.match("<"):
        return
    depth = 1
    while depth > 0:
        if self.peek().kind == "EOF":
            raise self.error("unterminated template/type annotation")
        if self.peek().value == "<":
            self.advance(); depth += 1
        elif self.peek().value == ">>":
            self.advance(); depth -= 2
        elif self.peek().value == ">":
            self.advance(); depth -= 1
        else:
            self.advance()
    if depth < 0:
        # A single >> may close the template and leave one > logically consumed;
        # that is fine for type annotations because we discard the whole type.
        depth = 0



# Patch20e: user/stdlib template types in declarations, e.g. uniform_real_distribution<double> dis(...);
def _parser_try_cpp_type_start_patch20(self: Parser) -> bool:  # type: ignore[no-redef]
    v = self.peek().value
    if v in {"const", "static", "void", "vector", "auto", "int", "long", "double", "float", "string", "char", "bool"}:
        return True
    if self.peek().kind == "IDENT" and self.peek(1).value in {"<", "IDENT"}:
        return True
    return self.peek().kind == "IDENT" and self.peek(1).kind == "IDENT"


# =============================================================================
# Patch 21: real C++-like structs + flattened nested vectors
# =============================================================================
# This patch moves struct/vector support away from the old "surface only" mode.
# Native run uses real Python dict/list values. Scratch compile receives flattened
# representations wherever vanilla Scratch can express them safely. Unsupported
# C++ object-memory patterns now produce explicit diagnostics instead of silently
# degrading into meaningless variables.

VERSION = "0.9.0-patch21-cpp-struct-flat"
KEYWORDS.update({"struct", "void", "static", "const"})
KEYWORDS.discard("bool")
for _sym in ("++", "--", "<<", ">>", "::"):
    if _sym not in MULTI:
        MULTI.insert(0, _sym)
SINGLE.update({":"})

@dataclass
class StructDecl:
    name: str
    fields: List[Tuple[str, str]]  # (type, field_name)

@dataclass
class StructVarDecl:
    typ: str
    name: str

@dataclass
class NestedVectorDecl:
    name: str
    typ: str
    rows: List[Any]

# Global registry is intentionally process-local.  The compiler also attaches the
# data to Program, but method lowering happens during parse, before Program exists.
_SBG_STRUCT_DEFS21: Dict[str, List[Tuple[str, str]]] = {}
_SBG_FLAT_VECTOR_TYPES21: Dict[str, str] = {}


def _sbg_norm_type21(t: str) -> str:
    return re.sub(r"\s+", "", t.replace("std::", "")).strip()


def _sbg_vector_inner21(t: str) -> Optional[str]:
    t = _sbg_norm_type21(t)
    if not t.startswith("vector<") or not t.endswith(">"):
        return None
    inner = t[len("vector<"):-1]
    return inner


def _sbg_is_vector21(t: str) -> bool:
    return _sbg_vector_inner21(t) is not None


def _sbg_is_nested_vector21(t: str) -> bool:
    inner = _sbg_vector_inner21(t)
    return inner is not None and _sbg_vector_inner21(inner) is not None


def _sbg_is_vector_of_struct21(t: str) -> Optional[str]:
    inner = _sbg_vector_inner21(t)
    if inner is None:
        return None
    inner = _sbg_norm_type21(inner)
    return inner if inner in _SBG_STRUCT_DEFS21 else None


def _sbg_is_nested_vector_of_struct21(t: str) -> Optional[str]:
    inner = _sbg_vector_inner21(t)
    if inner is None:
        return None
    return _sbg_is_vector_of_struct21(inner)


def _parser_read_template_type21(self: Parser) -> str:
    """Read a C++ type, preserving templates like vector<vector<Edge>>."""
    parts: List[str] = []
    while self.peek().value in {"const", "static"}:
        parts.append(self.advance().value)
    if self.peek().value == "std" and self.peek(1).value == "::":
        self.advance(); self.advance()
    if self.peek().kind not in {"IDENT", "KW"}:
        raise self.error(f"expected type name, got {self.peek().value!r}")
    base = self.advance().value
    parts.append(base)
    if base == "long" and self.peek().value == "long":
        parts.append(self.advance().value)
    # Template part.  Lexer may emit >> as one token, so account for it.
    if self.peek().value == "<":
        depth = 0
        while True:
            tok = self.advance()
            v = tok.value
            parts.append(v)
            if v == "<":
                depth += 1
            elif v == ">":
                depth -= 1
            elif v == ">>":
                depth -= 2
                # Keep exact C++ spelling, no need to split token.
            if depth <= 0:
                break
            if self.peek().kind == "EOF":
                raise self.error("unterminated template type")
    while self.peek().value in {"*", "&"}:
        parts.append(self.advance().value)
    return "".join(parts)


def _parser_skip_cpp_type_patch20(self: Parser) -> str:  # type: ignore[no-redef]
    return _parser_read_template_type21(self)


def _parser_try_cpp_type_start_patch20(self: Parser) -> bool:  # type: ignore[no-redef]
    v = self.peek().value
    if v in {"const", "static", "void", "vector", "auto", "int", "long", "double", "float", "string", "char", "bool"}:
        return True
    if v in _SBG_STRUCT_DEFS21:
        return True
    if self.peek().kind == "IDENT" and self.peek(1).kind == "IDENT":
        return True
    if self.peek().kind == "IDENT" and self.peek(1).value == "<":
        return True
    return False


def _parser_parse_cpp_struct_patch20(self: Parser, start_token: Token) -> Any:  # type: ignore[no-redef]
    name = self.expect_ident()
    self.expect("{")
    fields: List[Tuple[str, str]] = []
    while not self.at("}"):
        if self.peek().kind == "EOF":
            raise self.error("unterminated struct body")
        typ = _parser_read_template_type21(self)
        # Allow multiple field declarations separated by commas: int a, b;
        while True:
            fname = self.expect_ident()
            # Arrays are treated as vector-like fields in the surface parser.
            if self.match("["):
                while not self.at("]"):
                    self.advance()
                self.expect("]")
            fields.append((_sbg_norm_type21(typ), fname))
            if not self.match(","):
                break
        self.expect(";")
    self.expect("}")
    self.expect(";")
    _SBG_STRUCT_DEFS21[name] = fields
    if not hasattr(self, "sbg_structs"):
        self.sbg_structs = {}
    self.sbg_structs[name] = fields
    return self.loc(StructDecl(name, fields), start_token)


def _parser_parse_typed_params_patch20(self: Parser) -> List[str]:  # type: ignore[no-redef]
    params: List[str] = []
    param_types: Dict[str, str] = {}
    self.expect("(")
    if not self.at(")"):
        while True:
            # Untyped legacy parameter: foo(x, y)
            if self.peek().kind == "IDENT" and self.peek(1).value in {",", ")"}:
                pname = self.advance().value
                ptype = "auto"
            else:
                ptype = _sbg_norm_type21(_parser_read_template_type21(self))
                pname = self.expect_ident()
            params.append(pname)
            param_types[pname] = ptype
            if not self.match(","):
                break
    self.expect(")")
    self._sbg_last_param_types21 = param_types
    return params


def _sbg_default_for_type21(typ: str) -> Any:
    typ = _sbg_norm_type21(typ)
    if typ in {"string", "char"}:
        return Literal("")
    if typ == "bool":
        return Literal(False)
    return Literal(0)


def _parser_parse_cpp_initializer_patch20(self: Parser) -> Any:  # type: ignore[no-redef]
    if self.match("{"):
        items: List[Any] = []
        if not self.at("}"):
            while True:
                items.append(_parser_parse_cpp_initializer_patch20(self) if self.at("{") else self.parse_expr())
                if not self.match(","):
                    break
        self.expect("}")
        return ArrayExpr(items)
    return self.parse_expr()


def _parser_parse_cpp_decl_or_func_patch20(self: Parser, start_token: Token) -> Any:  # type: ignore[no-redef]
    typ = _sbg_norm_type21(_parser_read_template_type21(self))
    name = self.expect_ident()

    # Function definition or constructor-style variable declaration.
    if self.at("("):
        saved = self.i
        try:
            params = _parser_parse_typed_params_patch20(self)
            ptypes = getattr(self, "_sbg_last_param_types21", {})
            if self.at("{"):
                body = self.parse_block()
                if name == "main":
                    ev = self.loc(EventDecl("action", "input", body), start_token)
                    setattr(ev, "return_type", typ)
                    setattr(ev, "param_types", ptypes)
                    setattr(ev, "sbg_is_cpp_main", True)
                    return ev
                proc = self.loc(ProcDecl(name, params, body), start_token)
                setattr(proc, "return_type", typ)
                setattr(proc, "param_types", ptypes)
                return proc
            self.i = saved
        except ParseError:
            self.i = saved
        # C++ object constructor expression: T x(args...);
        self.expect("(")
        ctor_args: List[Any] = []
        if not self.at(")"):
            while True:
                ctor_args.append(self.parse_expr())
                if not self.match(","):
                    break
        self.expect(")")
        self.expect(";")
        if _sbg_is_vector21(typ):
            if _sbg_is_nested_vector21(typ):
                globals().setdefault("_SBG_NESTED_VECTOR_NAMES21", set()).add(name)
            if _sbg_is_nested_vector21(typ):
                st_name = _sbg_is_nested_vector_of_struct21(typ)
                if st_name:
                    _SBG_FLAT_VECTOR_TYPES21[name] = st_name
                node = NestedVectorDecl(name, typ, [])
                setattr(node, "ctor_args", ctor_args)
                return self.loc(node, start_token)
            node = ListDecl(name, [])
            setattr(node, "sbg_type", typ)
            setattr(node, "ctor_args", ctor_args)
            return self.loc(node, start_token)
        if typ in _SBG_STRUCT_DEFS21:
            return self.loc(StructVarDecl(typ, name), start_token)
        return self.loc(VarDecl(name, Literal(0), True), start_token)

    # Plain declaration.
    init: Any = _sbg_default_for_type21(typ)
    if self.match("="):
        init = _parser_parse_cpp_initializer_patch20(self)
    self.expect(";")

    if typ in _SBG_STRUCT_DEFS21:
        # `Edge e;` or `Edge e = other;`. Copy construction is represented
        # as a struct variable declaration plus optional assignment handled later.
        return self.loc(StructVarDecl(typ, name), start_token)

    if _sbg_is_vector21(typ):
        if _sbg_is_nested_vector21(typ):
            globals().setdefault("_SBG_NESTED_VECTOR_NAMES21", set()).add(name)
            if _sbg_is_nested_vector21(typ):
                globals().setdefault("_SBG_NESTED_VECTOR_NAMES21", set()).add(name)
            if _sbg_is_nested_vector_of_struct21(typ):
                _SBG_FLAT_VECTOR_TYPES21[name] = _sbg_is_nested_vector_of_struct21(typ) or ""
            rows: List[Any] = []
            if isinstance(init, ArrayExpr):
                rows = init.items
            node = NestedVectorDecl(name, typ, rows)
            return self.loc(node, start_token)
        if isinstance(init, ArrayExpr):
            node = ListDecl(name, init.items)
            setattr(node, "sbg_type", typ)
            return self.loc(node, start_token)
        node = ListDecl(name, [])
        setattr(node, "sbg_type", typ)
        return self.loc(node, start_token)

    node = VarDecl(name, init, True)
    setattr(node, "sbg_type", typ)
    return self.loc(node, start_token)


# Helpers for flattening during parse/lowering ------------------------------------------------
def _sbg_encode_row21(row: Any) -> Literal:
    # Scratch list reporters separate items with spaces.  Use spaces for easiest
    # compatibility with at0()/vec_size() in bits/cpp_compat.sbg.
    if isinstance(row, ArrayExpr):
        parts: List[str] = []
        for x in row.items:
            if isinstance(x, Literal):
                parts.append(str(x.value))
            else:
                # Non-literal row elements are not representable as initial Scratch
                # list values.  Keep a 0 placeholder and require runtime fill.
                parts.append("0")
        return Literal(" ".join(parts))
    if isinstance(row, Literal):
        return Literal(str(row.value))
    return Literal("")


def _sbg_expand_struct_var21(stmt: StructVarDecl) -> List[Any]:
    out: List[Any] = [VarDecl(stmt.name, Literal(0), True)]
    for ftyp, fname in _SBG_STRUCT_DEFS21.get(stmt.typ, []):
        full = f"{stmt.name}.{fname}"
        if _sbg_is_vector21(ftyp):
            out.append(ListDecl(full, []))
        else:
            out.append(VarDecl(full, _sbg_default_for_type21(ftyp), True))
    return out


def _sbg_expand_nested_vector21(stmt: NestedVectorDecl) -> List[Any]:
    # vector<vector<T>> becomes a list of encoded rows.  vector<vector<Struct>>
    # also gets flat field tables for Scratch-compatible record storage.
    rows = [_sbg_encode_row21(r) for r in stmt.rows]
    out: List[Any] = [ListDecl(stmt.name, rows)]
    struct_name = _sbg_is_nested_vector_of_struct21(stmt.typ)
    if struct_name:
        _SBG_FLAT_VECTOR_TYPES21[stmt.name] = struct_name
        out.append(ListDecl(f"{stmt.name}.__row_size", []))
        for ftyp, fname in _SBG_STRUCT_DEFS21.get(struct_name, []):
            out.append(ListDecl(f"{stmt.name}.{fname}", []))
    return out


def _sbg_lower_structs_body21(body: List[Any]) -> List[Any]:
    out: List[Any] = []
    for stmt in body:
        if isinstance(stmt, StructDecl):
            _SBG_STRUCT_DEFS21[stmt.name] = stmt.fields
            continue
        if isinstance(stmt, StructVarDecl):
            out.extend(_sbg_expand_struct_var21(stmt)); continue
        if isinstance(stmt, NestedVectorDecl):
            out.extend(_sbg_expand_nested_vector21(stmt)); continue
        if isinstance(stmt, ProcDecl):
            stmt.body = _sbg_lower_structs_body21(stmt.body)
        elif isinstance(stmt, EventDecl):
            stmt.body = _sbg_lower_structs_body21(stmt.body)
        elif isinstance(stmt, IfStmt):
            stmt.then_body = _sbg_lower_structs_body21(stmt.then_body)
            if stmt.else_body is not None:
                stmt.else_body = _sbg_lower_structs_body21(stmt.else_body)
        elif isinstance(stmt, (RepeatStmt, ForeverStmt, WhileStmt)):
            stmt.body = _sbg_lower_structs_body21(stmt.body)
        elif isinstance(stmt, ForStmt):
            if stmt.body:
                stmt.body = _sbg_lower_structs_body21(stmt.body)
        out.append(stmt)
    return out


_old_parse_source_patch21 = parse_source

def parse_source(text: str, filename: str = "<source>") -> Program:  # type: ignore[no-redef]
    program = _old_parse_source_patch21(text, filename)
    # Attach structs discovered by the parser and lower declarations to concrete
    # Scratch-representable variables/lists.
    setattr(program, "sbg_struct_defs", dict(_SBG_STRUCT_DEFS21))
    program.body = _sbg_lower_structs_body21(program.body)
    meta = getattr(program, "__dict__", {})
    meta["sbg_flat_vector_types"] = dict(_SBG_FLAT_VECTOR_TYPES21)
    return program


# Runtime support -----------------------------------------------------------------------------
_old_runtime_exec_stmt_patch21 = Runtime._exec_stmt

def _runtime_exec_stmt_patch21(self: Runtime, stmt: Any) -> None:
    if isinstance(stmt, StructDecl):
        _SBG_STRUCT_DEFS21[stmt.name] = stmt.fields
        return None
    if isinstance(stmt, StructVarDecl):
        obj: Dict[str, Any] = {"__type": stmt.typ}
        for ftyp, fname in _SBG_STRUCT_DEFS21.get(stmt.typ, []):
            obj[fname] = [] if _sbg_is_vector21(ftyp) else self.eval(_sbg_default_for_type21(ftyp))
        self.vars[stmt.name] = obj
        return None
    if isinstance(stmt, NestedVectorDecl):
        self.vars[stmt.name] = [[self.eval(x) for x in r.items] if isinstance(r, ArrayExpr) else [] for r in stmt.rows]
        self.lists[stmt.name] = [" ".join(str(self.eval(x)) for x in r.items) if isinstance(r, ArrayExpr) else "" for r in stmt.rows]
        return None
    if isinstance(stmt, LValueAssignStmt):
        rhs = self.eval(stmt.expr)
        def apply(old: Any) -> Any:
            if stmt.op == "=": return rhs
            if stmt.op == "+=": return old + rhs
            if stmt.op == "-=": return old - rhs
            if stmt.op == "*=": return old * rhs
            if stmt.op == "/=": return old / rhs
            if stmt.op == "%=": return old % rhs
            return rhs
        # Dict field assignment: n.b = x; n.w = vector;
        if isinstance(stmt.target, CallExpr) and stmt.target.callee == "__field_ref":
            base_expr, field_expr = stmt.target.args
            field = str(self.eval(field_expr))
            # Static flattened Scratch-compatible name first.
            static_name = _sbg_ref_static_name_patch20(stmt.target)
            if static_name and static_name in self.lists:
                dst = self.lists[static_name]
                if stmt.op != "=":
                    raise RuntimeSBGError("list field only supports '=' assignment")
                dst.clear()
                if isinstance(rhs, list): dst.extend(rhs)
                else: dst.extend(_sbg_vec_tokens_runtime_patch20(rhs))
                return None
            if static_name and static_name in self.vars:
                self.vars[static_name] = apply(self.vars.get(static_name, 0)); return None
            obj = self.eval(base_expr)
            if isinstance(obj, dict):
                old = obj.get(field, [] if isinstance(rhs, list) else 0)
                obj[field] = apply(old)
                return None
        if isinstance(stmt.target, CallExpr) and stmt.target.callee == "__index0_ref":
            obj_expr, idx_expr = stmt.target.args
            idx = int(float(self.eval(idx_expr)))
            obj = self.eval(obj_expr)
            if isinstance(obj, list):
                obj[idx] = apply(obj[idx])
                return None
        return _old_runtime_exec_stmt_patch21(self, stmt)
    return _old_runtime_exec_stmt_patch21(self, stmt)

Runtime._exec_stmt = _runtime_exec_stmt_patch21  # type: ignore[method-assign]

_old_runtime_eval_patch21 = Runtime._eval

def _runtime_eval_patch21(self: Runtime, expr: Any) -> Any:
    if isinstance(expr, CallExpr) and expr.callee == "__field_ref":
        base_expr, field_expr = expr.args
        field = str(self.eval(field_expr))
        # Static flattened variable/list, e.g. n.b / n.w.
        static_name = _sbg_ref_static_name_patch20(expr)
        if static_name and static_name in self.vars:
            return self.vars[static_name]
        if static_name and static_name in self.lists:
            return self.lists[static_name]
        obj = self.eval(base_expr)
        if isinstance(obj, dict):
            return obj.get(field, 0)
    if isinstance(expr, CallExpr) and expr.callee == "__index0_ref":
        obj = self.eval(expr.args[0])
        idx = int(float(self.eval(expr.args[1])))
        if isinstance(obj, str):
            return _sbg_vec_at0_runtime_patch20(obj, idx)
        if isinstance(obj, list):
            return obj[idx]
    return _old_runtime_eval_patch21(self, expr)

Runtime._eval = _runtime_eval_patch21  # type: ignore[method-assign]

_old_runtime_call_patch21 = Runtime.call

def _runtime_call_patch21(self: Runtime, name: str, args: List[Any]) -> Any:
    # Vector methods should work on actual list values, not only named Scratch lists.
    if name in {"push", "push_back"}:
        if isinstance(args[0], list):
            val = args[1]
            if isinstance(val, dict): val = dict(val)
            elif isinstance(val, list): val = list(val)
            args[0].append(val); return None
    if name in {"clear"}:
        if isinstance(args[0], list): args[0].clear(); return None
    if name in {"resize"}:
        if isinstance(args[0], list):
            n = max(0, int(float(args[1]))); val = args[2] if len(args) >= 3 else []
            while len(args[0]) > n: args[0].pop()
            while len(args[0]) < n: args[0].append([] if isinstance(val, list) else val)
            return None
    if name in {"size", "len"} and len(args) == 1:
        return len(args[0])
    if name == "at0" and len(args) == 2:
        if isinstance(args[0], list): return args[0][int(float(args[1]))]
        return _sbg_vec_at0_runtime_patch20(args[0], int(float(args[1])))
    return _old_runtime_call_patch21(self, name, args)

Runtime.call = _runtime_call_patch21  # type: ignore[method-assign]


# Method lowering / expression lowering -----------------------------------------------------------------
_old_sbg_method_lower_patch19 = _sbg_method_lower_patch19

def _sbg_method_lower_patch19(receiver: Any, method: str, args: List[Any], parser: Optional[Parser] = None) -> CallExpr:  # type: ignore[no-redef]
    # C++ vector row: matrix[i].size(), matrix[i].push_back(x)
    if isinstance(receiver, CallExpr) and receiver.callee == "__index0_ref" and len(receiver.args) == 2:
        base, row = receiver.args
        if isinstance(base, VarExpr) and base.name in _SBG_FLAT_VECTOR_TYPES21:
            if method in {"push", "push_back"}:
                return _sbg_call_patch19("__flat_struct_push", [Literal(base.name), row, *args], receiver)
            if method == "size":
                return _sbg_call_patch19("__flat_struct_row_size", [Literal(base.name), row], receiver)
    if method == "size" and isinstance(receiver, CallExpr) and receiver.callee == "__index0_ref":
        return _sbg_call_patch19("vec_size", [receiver], receiver)
    return _old_sbg_method_lower_patch19(receiver, method, args, parser)

# Reinstall parse_postfix because the older function captures _sbg_method_lower_patch19 by global name.
Parser.parse_postfix = _parser_parse_postfix_patch20  # type: ignore[method-assign]

_old_builder_lower_expr_patch21 = _builder_lower_expr

def _builder_lower_expr_patch21(self: ScratchBuilder, expr: Any) -> Tuple[List[Any], Any]:
    # Dynamic row.size() must use vec_size(row), not Scratch string length.
    if isinstance(expr, CallExpr) and expr.callee == "size" and len(expr.args) == 1:
        arg = expr.args[0]
        if not (isinstance(arg, VarExpr) and arg.name in getattr(self, "lists", {})):
            return _old_builder_lower_expr_patch21(self, CallExpr("vec_size", [arg]))
    return _old_builder_lower_expr_patch21(self, expr)

_builder_lower_expr = _builder_lower_expr_patch21  # type: ignore[assignment]

# Compile-time support for struct/list field assignment and nested row literals.
_old_builder_compile_stmt_patch21 = ScratchBuilder.compile_stmt

def _sbg_compile_copy_value_to_static_list21(self: ScratchBuilder, dst: str, src_expr: Any) -> Optional[str]:
    """dst = src for a concrete Scratch list.  If src is a list, copy items; if
    src is a row string/parameter, split by spaces/commas/semicolons."""
    self.list_id(dst)
    # Static list-to-list fast path.
    src_name = _sbg_ref_static_name_patch20(src_expr)
    if src_name and src_name in self.lists:
        return self.compile_call_stmt(CallExpr("copyList", [VarExpr(src_name), VarExpr(dst)]))
    i = f"__sbg_vec_i_{self.uid('tmp')}"
    tok = f"__sbg_vec_tok_{self.uid('tmp')}"
    ch = f"__sbg_vec_ch_{self.uid('tmp')}"
    self.var_id(i); self.var_id(tok); self.var_id(ch)
    # clear dst; token=""; i=1; while(i <= len(src)+1) { ch = ...; ... }
    delim = BinaryExpr(BinaryExpr(BinaryExpr(VarExpr(ch), "==", Literal(" ")), "||", BinaryExpr(VarExpr(ch), "==", Literal(","))), "||", BinaryExpr(VarExpr(ch), "==", Literal(";")))
    statements: List[Any] = [
        ExprStmt(CallExpr("clearList", [VarExpr(dst)])),
        AssignStmt(tok, "=", Literal("")),
        AssignStmt(i, "=", Literal(1)),
        WhileStmt(BinaryExpr(VarExpr(i), "<=", BinaryExpr(CallExpr("len", [src_expr]), "+", Literal(1))), [
            IfStmt(BinaryExpr(VarExpr(i), "<=", CallExpr("len", [src_expr])), [AssignStmt(ch, "=", CallExpr("letter", [src_expr, VarExpr(i)]))], [AssignStmt(ch, "=", Literal(" "))]),
            IfStmt(delim,
                [IfStmt(BinaryExpr(CallExpr("len", [VarExpr(tok)]), ">", Literal(0)), [ExprStmt(CallExpr("push", [VarExpr(dst), BinaryExpr(VarExpr(tok), "+", Literal(0))])), AssignStmt(tok, "=", Literal(""))], None)],
                [AssignStmt(tok, "=", CallExpr("join", [VarExpr(tok), VarExpr(ch)]))]
            ),
            AssignStmt(i, "+=", Literal(1)),
        ]),
    ]
    return self.compile_statement_chain(statements)


def _builder_compile_stmt_patch21(self: ScratchBuilder, stmt: Any) -> Optional[str]:
    if isinstance(stmt, StructDecl):
        return None
    if isinstance(stmt, StructVarDecl):
        return None
    if isinstance(stmt, NestedVectorDecl):
        return None
    if isinstance(stmt, LValueAssignStmt) and isinstance(stmt.target, CallExpr) and stmt.target.callee == "__field_ref":
        name = _sbg_ref_static_name_patch20(stmt.target)
        if name and name in self.lists:
            if stmt.op != "=":
                raise CompileError("list/vector field assignment only supports '='; use field.push_back()/field.set() for mutation")
            return _sbg_compile_copy_value_to_static_list21(self, name, stmt.expr)
    return _old_builder_compile_stmt_patch21(self, stmt)

ScratchBuilder.compile_stmt = _builder_compile_stmt_patch21  # type: ignore[method-assign]

_old_builder_compile_call_expr_patch21 = ScratchBuilder.compile_call_expr

def _builder_compile_call_expr_patch21(self: ScratchBuilder, expr: CallExpr, parent: Optional[str]) -> str:
    name, a = expr.callee, expr.args
    if name == "size" and len(a) == 1:
        if isinstance(a[0], VarExpr) and a[0].name in self.lists:
            return self.add_block("data_lengthoflist", parent=parent, fields={"LIST": [a[0].name, self.list_id(a[0].name)]})
        # If this reaches block generation it was not lowered; fail loudly.
        raise CompileError("dynamic .size() needs import <bits/stdc++.h> / vec_size lowering")
    return _old_builder_compile_call_expr_patch21(self, expr, parent)

ScratchBuilder.compile_call_expr = _builder_compile_call_expr_patch21  # type: ignore[method-assign]

# Program/project metadata ----------------------------------------------------------------------
_old_compiler_compile_patch21 = Compiler.compile

def _compiler_compile_patch21(self: Compiler) -> Dict[str, Any]:
    project = _old_compiler_compile_patch21(self)
    meta = project.setdefault("meta", {})
    meta["agent"] = f"StageBG/SBG {VERSION}"
    meta["stagebgPatch21"] = "C++-like struct declarations, native struct values, vector<vector<T>> row-string flattening, static struct-field list assignment, and explicit diagnostics for unflattenable C++ object-memory patterns."
    return project

Compiler.compile = _compiler_compile_patch21  # type: ignore[method-assign]


# Patch21b: lower dynamic nested indexing before reporter generation.
_prev_builder_lower_expr_patch21b = _builder_lower_expr

def _builder_lower_expr_patch21b(self: ScratchBuilder, expr: Any) -> Tuple[List[Any], Any]:
    if isinstance(expr, CallExpr) and expr.callee == "__index0_ref" and len(expr.args) == 2:
        obj, idx = expr.args
        if _sbg_ref_static_name_patch20(obj) is None:
            return _prev_builder_lower_expr_patch21b(self, CallExpr("at0", [obj, idx]))
    return _prev_builder_lower_expr_patch21b(self, expr)

_builder_lower_expr = _builder_lower_expr_patch21b  # type: ignore[assignment]


# Patch21c: tree-shaker must see helper procs introduced during expression lowering.
_prev_sbg_collect_calls_expr_patch21c = _sbg_collect_calls_expr

def _sbg_collect_calls_expr(expr: Any, out: set[str]) -> None:  # type: ignore[no-redef]
    if isinstance(expr, CallExpr):
        if expr.callee == "__index0_ref" and len(expr.args) == 2 and _sbg_ref_static_name_patch20(expr.args[0]) is None:
            out.add("at0")
        if expr.callee in {"vec_size", "at0"}:
            out.add(expr.callee)
    _prev_sbg_collect_calls_expr_patch21c(expr, out)


# Patch21d: flat vector< vector<Struct> > method support.
_prev_sbg_method_lower_patch21d = _sbg_method_lower_patch19

def _sbg_method_lower_patch19(receiver: Any, method: str, args: List[Any], parser: Optional[Parser] = None) -> CallExpr:  # type: ignore[no-redef]
    if isinstance(receiver, VarExpr) and receiver.name in _SBG_FLAT_VECTOR_TYPES21 and method == "resize":
        return _sbg_call_patch19("__flat_struct_resize_outer", [Literal(receiver.name), *args], receiver)
    return _prev_sbg_method_lower_patch21d(receiver, method, args, parser)

Parser.parse_postfix = _parser_parse_postfix_patch20  # type: ignore[method-assign]

_prev_runtime_call_patch21d = Runtime.call

def _runtime_call_patch21d(self: Runtime, name: str, args: List[Any]) -> Any:
    if name == "__flat_struct_resize_outer":
        base = str(args[0]); n = max(0, int(float(args[1])))
        rows = self.vars.setdefault(base, [])
        if not isinstance(rows, list):
            rows = []; self.vars[base] = rows
        while len(rows) > n: rows.pop()
        while len(rows) < n: rows.append([])
        self.lists[base] = ["" for _ in range(n)]
        self.lists[f"{base}.__row_size"] = [len(r) if isinstance(r, list) else 0 for r in rows]
        return None
    if name == "__flat_struct_push":
        base = str(args[0]); row = int(float(args[1])); value = args[2]
        rows = self.vars.setdefault(base, [])
        while len(rows) <= row: rows.append([])
        if isinstance(value, dict): value = dict(value)
        rows[row].append(value)
        self.lists[base] = [" ".join(str(i) for i in range(len(r))) for r in rows]
        self.lists[f"{base}.__row_size"] = [len(r) for r in rows]
        return None
    if name == "__flat_struct_row_size":
        base = str(args[0]); row = int(float(args[1]))
        rows = self.vars.get(base, [])
        return len(rows[row]) if isinstance(rows, list) and 0 <= row < len(rows) else 0
    return _prev_runtime_call_patch21d(self, name, args)

Runtime.call = _runtime_call_patch21d  # type: ignore[method-assign]

_prev_builder_compile_call_stmt_patch21d = ScratchBuilder.compile_call_stmt

def _builder_compile_call_stmt_patch21d(self: ScratchBuilder, expr: CallExpr) -> Optional[str]:
    if expr.callee == "__flat_struct_resize_outer":
        if len(expr.args) < 2 or not isinstance(expr.args[0], Literal):
            raise CompileError("flat struct resize needs static base name")
        base = str(expr.args[0].value)
        n = expr.args[1]
        return self.compile_statement_chain([
            ExprStmt(CallExpr("resizeList", [VarExpr(base), n, Literal("")])),
            ExprStmt(CallExpr("resizeList", [VarExpr(f"{base}.__row_size"), n, Literal(0)])),
        ])
    if expr.callee == "__flat_struct_push":
        raise CompileError("vector<vector<struct>>.push_back(struct) needs full record-copy lowering; native run supports it, Scratch compile currently requires flat arrays directly")
    return _prev_builder_compile_call_stmt_patch21d(self, expr)

ScratchBuilder.compile_call_stmt = _builder_compile_call_stmt_patch21d  # type: ignore[method-assign]


# Patch21e: intercept flat-struct command calls at stmt level before older wrappers.
_prev_builder_compile_stmt_patch21e = ScratchBuilder.compile_stmt

def _builder_compile_stmt_patch21e(self: ScratchBuilder, stmt: Any) -> Optional[str]:
    if isinstance(stmt, ExprStmt) and isinstance(stmt.expr, CallExpr) and stmt.expr.callee in {"__flat_struct_resize_outer", "__flat_struct_push"}:
        return _builder_compile_call_stmt_patch21d(self, stmt.expr)
    return _prev_builder_compile_stmt_patch21e(self, stmt)

ScratchBuilder.compile_stmt = _builder_compile_stmt_patch21e  # type: ignore[method-assign]


# Patch21f: item(row_string, i) and len(row_string) lowering for foreach over encoded rows.
_prev_builder_lower_expr_patch21f = _builder_lower_expr

def _builder_lower_expr_patch21f(self: ScratchBuilder, expr: Any) -> Tuple[List[Any], Any]:
    if isinstance(expr, CallExpr) and expr.callee == "item" and len(expr.args) == 2:
        src, idx1 = expr.args
        if not (isinstance(src, VarExpr) and src.name in getattr(self, "lists", {})):
            return _prev_builder_lower_expr_patch21f(self, CallExpr("at0", [src, BinaryExpr(idx1, "-", Literal(1))]))
    return _prev_builder_lower_expr_patch21f(self, expr)

_builder_lower_expr = _builder_lower_expr_patch21f  # type: ignore[assignment]

_prev_sbg_collect_calls_expr_patch21f = _sbg_collect_calls_expr

def _sbg_collect_calls_expr(expr: Any, out: set[str]) -> None:  # type: ignore[no-redef]
    if isinstance(expr, CallExpr) and expr.callee == "item" and len(expr.args) == 2:
        if not (isinstance(expr.args[0], VarExpr)):
            out.add("at0")
    _prev_sbg_collect_calls_expr_patch21f(expr, out)


# Patch21g: compile flattened vector<vector<struct>> as record tables for common C++ code.
_SBG_STRUCT_DEFAULT_BASE21: Dict[str, str] = {}
# Populate from declarations parsed so far.
for _base, _st in list(_SBG_FLAT_VECTOR_TYPES21.items()):
    if _st and _st not in _SBG_STRUCT_DEFAULT_BASE21:
        _SBG_STRUCT_DEFAULT_BASE21[_st] = _base

# Keep this updated when parse_source/lowering discovers new flat vectors.
_prev_sbg_expand_nested_vector21_g = _sbg_expand_nested_vector21

def _sbg_expand_nested_vector21(stmt: NestedVectorDecl) -> List[Any]:  # type: ignore[no-redef]
    out = _prev_sbg_expand_nested_vector21_g(stmt)
    st = _sbg_is_nested_vector_of_struct21(stmt.typ)
    if st:
        _SBG_STRUCT_DEFAULT_BASE21.setdefault(st, stmt.name)
    return out

_prev_compile_proc_definition_patch21g = ScratchBuilder.compile_proc_definition

def _builder_compile_proc_definition_patch21g(self: ScratchBuilder, proc: ProcDecl) -> str:
    old_types = getattr(self, "current_proc_param_types", {})
    self.current_proc_param_types = getattr(proc, "param_types", {}) or {}
    try:
        return _prev_compile_proc_definition_patch21g(self, proc)
    finally:
        self.current_proc_param_types = old_types

ScratchBuilder.compile_proc_definition = _builder_compile_proc_definition_patch21g  # type: ignore[method-assign]

_prev_builder_compile_expr_patch21g = ScratchBuilder._compile_expr

def _builder_compile_expr_patch21g(self: ScratchBuilder, expr: Any, parent: Optional[str] = None) -> str:
    # Struct parameter field: in Scratch a struct argument may be a flat row index.
    # field access is lowered to the corresponding flat field table.
    if isinstance(expr, CallExpr) and expr.callee == "__field_ref" and len(expr.args) == 2:
        base_expr, field_expr = expr.args
        if isinstance(base_expr, VarExpr) and isinstance(field_expr, Literal):
            ptypes = getattr(self, "current_proc_param_types", {}) or {}
            st = ptypes.get(base_expr.name)
            if st in _SBG_STRUCT_DEFAULT_BASE21:
                base = _SBG_STRUCT_DEFAULT_BASE21[st]
                lname = f"{base}.{field_expr.value}"
                self.list_id(lname)
                bid = self.add_block("data_itemoflist", parent=parent, fields={"LIST": [lname, self.list_id(lname)]}, inputs={})
                self.blocks[bid]["inputs"]["INDEX"] = self.expr_input(BinaryExpr(base_expr, "+", Literal(1)), bid)
                return bid
    return _prev_builder_compile_expr_patch21g(self, expr, parent)

ScratchBuilder._compile_expr = _builder_compile_expr_patch21g  # type: ignore[method-assign]


def _sbg_compile_list_to_string21(self: ScratchBuilder, src_list: str, out_var: str) -> Optional[str]:
    i = f"__sbg_enc_i_{self.uid('tmp')}"
    self.var_id(i); self.var_id(out_var)
    return self.compile_statement_chain([
        AssignStmt(out_var, "=", Literal("")),
        AssignStmt(i, "=", Literal(1)),
        WhileStmt(BinaryExpr(VarExpr(i), "<=", CallExpr("len", [VarExpr(src_list)])), [
            IfStmt(BinaryExpr(VarExpr(i), ">", Literal(1)), [AssignStmt(out_var, "=", CallExpr("join", [VarExpr(out_var), Literal(" ")]))], None),
            AssignStmt(out_var, "=", CallExpr("join", [VarExpr(out_var), CallExpr("item", [VarExpr(src_list), VarExpr(i)])])),
            AssignStmt(i, "+=", Literal(1)),
        ]),
    ])

# Override previous flat push compiler with real record-table push for static struct variables.
def _builder_compile_call_stmt_patch21d(self: ScratchBuilder, expr: CallExpr) -> Optional[str]:  # type: ignore[no-redef]
    if expr.callee == "__flat_struct_resize_outer":
        if len(expr.args) < 2 or not isinstance(expr.args[0], Literal):
            raise CompileError("flat struct resize needs static base name")
        base = str(expr.args[0].value); n = expr.args[1]
        return self.compile_statement_chain([
            ExprStmt(CallExpr("resizeList", [VarExpr(base), n, Literal("")])),
            ExprStmt(CallExpr("resizeList", [VarExpr(f"{base}.__row_size"), n, Literal(0)])),
        ])
    if expr.callee == "__flat_struct_push":
        if len(expr.args) != 3 or not isinstance(expr.args[0], Literal) or not isinstance(expr.args[2], VarExpr):
            raise CompileError("flat struct push currently needs vector[row].push_back(staticStructVar)")
        base = str(expr.args[0].value); row = expr.args[1]; obj = expr.args[2].name
        st = _SBG_FLAT_VECTOR_TYPES21.get(base)
        if not st:
            raise CompileError(f"unknown flat struct vector {base!r}")
        fields = _SBG_STRUCT_DEFS21.get(st, [])
        if not fields:
            raise CompileError(f"unknown struct type {st!r}")
        flat_idx = f"__sbg_flat_idx_{self.uid('tmp')}"
        old_row = f"__sbg_old_row_{self.uid('tmp')}"
        enc = f"__sbg_enc_{self.uid('tmp')}"
        self.var_id(flat_idx); self.var_id(old_row); self.var_id(enc)
        first_field_name = fields[0][1]
        stmts: List[Any] = [
            AssignStmt(flat_idx, "=", CallExpr("len", [VarExpr(f"{base}.{first_field_name}")])),
            AssignStmt(old_row, "=", CallExpr("item", [VarExpr(base), BinaryExpr(row, "+", Literal(1))])),
            IfStmt(BinaryExpr(CallExpr("len", [VarExpr(old_row)]), ">", Literal(0)),
                [AssignStmt(old_row, "=", CallExpr("join", [CallExpr("join", [VarExpr(old_row), Literal(" ")]), VarExpr(flat_idx)]))],
                [AssignStmt(old_row, "=", CallExpr("join", [Literal(""), VarExpr(flat_idx)]))]
            ),
            ExprStmt(CallExpr("setItem", [VarExpr(base), BinaryExpr(row, "+", Literal(1)), VarExpr(old_row)])),
            ExprStmt(CallExpr("setItem", [VarExpr(f"{base}.__row_size"), BinaryExpr(row, "+", Literal(1)), BinaryExpr(CallExpr("item", [VarExpr(f"{base}.__row_size"), BinaryExpr(row, "+", Literal(1))]), "+", Literal(1))])),
        ]
        for ftyp, fname in fields:
            src_static = f"{obj}.{fname}"
            dst = f"{base}.{fname}"
            if _sbg_is_vector21(ftyp):
                # encode src vector list to one Scratch list item string
                stmts.append(ExprStmt(CallExpr("__encode_list_to_var", [Literal(src_static), Literal(enc)])))
                stmts.append(ExprStmt(CallExpr("push", [VarExpr(dst), VarExpr(enc)])))
            else:
                stmts.append(ExprStmt(CallExpr("push", [VarExpr(dst), VarExpr(src_static)])))
        # Compile, with a private pseudo statement for encoding.
        first: Optional[str] = None
        for stt in stmts:
            if isinstance(stt, ExprStmt) and isinstance(stt.expr, CallExpr) and stt.expr.callee == "__encode_list_to_var":
                src = str(stt.expr.args[0].value); outv = str(stt.expr.args[1].value)
                bid = _sbg_compile_list_to_string21(self, src, outv)
            else:
                bid = self.compile_stmt(stt)
            first = self.chain(first, bid)
        return first
    return _prev_builder_compile_call_stmt_patch21d(self, expr)

ScratchBuilder.compile_call_stmt = _builder_compile_call_stmt_patch21d  # type: ignore[method-assign]

# Reinstall statement intercept to use the overridden function above.
def _builder_compile_stmt_patch21e(self: ScratchBuilder, stmt: Any) -> Optional[str]:  # type: ignore[no-redef]
    if isinstance(stmt, ExprStmt) and isinstance(stmt.expr, CallExpr) and stmt.expr.callee in {"__flat_struct_resize_outer", "__flat_struct_push"}:
        return _builder_compile_call_stmt_patch21d(self, stmt.expr)
    return _prev_builder_compile_stmt_patch21e(self, stmt)

ScratchBuilder.compile_stmt = _builder_compile_stmt_patch21e  # type: ignore[method-assign]


# Patch21h: recover references to C++ local names in newer AST nodes that older
# local-name mangling did not know about (notably LValueAssignStmt).
def _sbg_unique_mangled_name21(names: Iterable[str], original: str) -> Optional[str]:
    suffix = "_" + _sbg_sanitize_name(original)
    found = [n for n in names if n.endswith(suffix) and n.startswith("__loc_")]
    return found[0] if len(found) == 1 else None

_prev_runtime_eval_patch21h = Runtime._eval

def _runtime_eval_patch21h(self: Runtime, expr: Any) -> Any:
    if isinstance(expr, VarExpr):
        if expr.name not in self.vars and expr.name not in self.lists:
            alt = _sbg_unique_mangled_name21([*self.vars.keys(), *self.lists.keys()], expr.name)
            if alt:
                return self.vars[alt] if alt in self.vars else self.lists[alt]
    return _prev_runtime_eval_patch21h(self, expr)

Runtime._eval = _runtime_eval_patch21h  # type: ignore[method-assign]

_prev_builder_compile_expr_patch21h = ScratchBuilder._compile_expr

def _builder_compile_expr_patch21h(self: ScratchBuilder, expr: Any, parent: Optional[str] = None) -> str:
    if isinstance(expr, VarExpr) and expr.name not in self.variables and expr.name not in self.lists and expr.name not in getattr(self, "current_proc_params", {}):
        alt = _sbg_unique_mangled_name21([*self.variables.keys(), *self.lists.keys()], expr.name)
        if alt:
            expr = VarExpr(alt)
    return _prev_builder_compile_expr_patch21h(self, expr, parent)

ScratchBuilder._compile_expr = _builder_compile_expr_patch21h  # type: ignore[method-assign]

_prev_builder_require_list_expr_patch21h = ScratchBuilder.require_list_expr

def _builder_require_list_expr_patch21h(self: ScratchBuilder, expr: Any) -> str:
    if isinstance(expr, VarExpr) and expr.name not in self.lists:
        alt = _sbg_unique_mangled_name21(self.lists.keys(), expr.name)
        if alt:
            return alt
    return _prev_builder_require_list_expr_patch21h(self, expr)

ScratchBuilder.require_list_expr = _builder_require_list_expr_patch21h  # type: ignore[method-assign]


# Patch21i: compile flat row size reporter directly to list item.
_prev_builder_compile_expr_patch21i = ScratchBuilder._compile_expr

def _builder_compile_expr_patch21i(self: ScratchBuilder, expr: Any, parent: Optional[str] = None) -> str:
    if isinstance(expr, CallExpr) and expr.callee == "__flat_struct_row_size" and len(expr.args) == 2 and isinstance(expr.args[0], Literal):
        base = str(expr.args[0].value)
        lname = f"{base}.__row_size"
        self.list_id(lname)
        bid = self.add_block("data_itemoflist", parent=parent, fields={"LIST": [lname, self.list_id(lname)]}, inputs={})
        self.blocks[bid]["inputs"]["INDEX"] = self.expr_input(BinaryExpr(expr.args[1], "+", Literal(1)), bid)
        return bid
    return _prev_builder_compile_expr_patch21i(self, expr, parent)

ScratchBuilder._compile_expr = _builder_compile_expr_patch21i  # type: ignore[method-assign]


# Patch21j: mutable vector<vector<T>> rows (non-struct) as encoded row strings.
_SBG_NESTED_VECTOR_NAMES21: set[str] = set()

_prev_sbg_expand_nested_vector21_j = _sbg_expand_nested_vector21

def _sbg_expand_nested_vector21(stmt: NestedVectorDecl) -> List[Any]:  # type: ignore[no-redef]
    _SBG_NESTED_VECTOR_NAMES21.add(stmt.name)
    out = _prev_sbg_expand_nested_vector21_j(stmt)
    ctor_args = list(getattr(stmt, "ctor_args", []) or [])
    if ctor_args:
        out.append(ExprStmt(CallExpr("__nested_resize_outer", [Literal(stmt.name), ctor_args[0]])))
    return out

_prev_sbg_method_lower_patch21j = _sbg_method_lower_patch19

def _sbg_method_lower_patch19(receiver: Any, method: str, args: List[Any], parser: Optional[Parser] = None) -> CallExpr:  # type: ignore[no-redef]
    if isinstance(receiver, VarExpr) and receiver.name in _SBG_NESTED_VECTOR_NAMES21 and method == "resize":
        return _sbg_call_patch19("__nested_resize_outer", [Literal(receiver.name), *args], receiver)
    if isinstance(receiver, CallExpr) and receiver.callee == "__index0_ref" and len(receiver.args) == 2:
        base, row = receiver.args
        if isinstance(base, VarExpr) and base.name in _SBG_NESTED_VECTOR_NAMES21:
            if method in {"push", "push_back"}:
                return _sbg_call_patch19("__nested_row_push", [Literal(base.name), row, *args], receiver)
            if method == "clear":
                return _sbg_call_patch19("__nested_row_clear", [Literal(base.name), row], receiver)
            if method == "size":
                return _sbg_call_patch19("vec_size", [receiver], receiver)
    return _prev_sbg_method_lower_patch21j(receiver, method, args, parser)

Parser.parse_postfix = _parser_parse_postfix_patch20  # type: ignore[method-assign]

_prev_runtime_call_patch21j = Runtime.call

def _runtime_call_patch21j(self: Runtime, name: str, args: List[Any]) -> Any:
    if name == "__nested_resize_outer":
        base = str(args[0]); n = max(0, int(float(args[1])))
        rows = self.vars.setdefault(base, [])
        if not isinstance(rows, list): rows = []; self.vars[base] = rows
        while len(rows) > n: rows.pop()
        while len(rows) < n: rows.append([])
        self.lists[base] = [" ".join(str(x) for x in r) if isinstance(r, list) else str(r) for r in rows]
        return None
    if name == "__nested_row_push":
        base = str(args[0]); row = int(float(args[1])); val = args[2]
        rows = self.vars.setdefault(base, [])
        while len(rows) <= row: rows.append([])
        if not isinstance(rows[row], list): rows[row] = _sbg_vec_tokens_runtime_patch20(rows[row])
        rows[row].append(val)
        self.lists[base] = [" ".join(str(x) for x in r) if isinstance(r, list) else str(r) for r in rows]
        return None
    if name == "__nested_row_clear":
        base = str(args[0]); row = int(float(args[1])); rows = self.vars.setdefault(base, [])
        while len(rows) <= row: rows.append([])
        rows[row] = []
        self.lists[base] = [" ".join(str(x) for x in r) if isinstance(r, list) else str(r) for r in rows]
        return None
    return _prev_runtime_call_patch21j(self, name, args)

Runtime.call = _runtime_call_patch21j  # type: ignore[method-assign]

_prev_builder_compile_call_stmt_patch21j = ScratchBuilder.compile_call_stmt

def _builder_compile_call_stmt_patch21j(self: ScratchBuilder, expr: CallExpr) -> Optional[str]:
    if expr.callee == "__nested_resize_outer":
        base = str(expr.args[0].value); n = expr.args[1]
        return self.compile_call_stmt(CallExpr("resizeList", [VarExpr(base), n, Literal("")]))
    if expr.callee == "__nested_row_clear":
        base = str(expr.args[0].value); row = expr.args[1]
        return self.compile_call_stmt(CallExpr("setItem", [VarExpr(base), BinaryExpr(row, "+", Literal(1)), Literal("")]))
    if expr.callee == "__nested_row_push":
        base = str(expr.args[0].value); row = expr.args[1]; val = expr.args[2]
        oldv = f"__sbg_row_old_{self.uid('tmp')}"; newv = f"__sbg_row_new_{self.uid('tmp')}"
        self.var_id(oldv); self.var_id(newv)
        stmts = [
            AssignStmt(oldv, "=", CallExpr("item", [VarExpr(base), BinaryExpr(row, "+", Literal(1))])),
            IfStmt(BinaryExpr(CallExpr("len", [VarExpr(oldv)]), ">", Literal(0)),
                [AssignStmt(newv, "=", CallExpr("join", [CallExpr("join", [VarExpr(oldv), Literal(" ")]), val]))],
                [AssignStmt(newv, "=", CallExpr("join", [Literal(""), val]))]
            ),
            ExprStmt(CallExpr("setItem", [VarExpr(base), BinaryExpr(row, "+", Literal(1)), VarExpr(newv)])),
        ]
        return self.compile_statement_chain(stmts)
    return _prev_builder_compile_call_stmt_patch21j(self, expr)

ScratchBuilder.compile_call_stmt = _builder_compile_call_stmt_patch21j  # type: ignore[method-assign]

_prev_builder_compile_stmt_patch21j = ScratchBuilder.compile_stmt

def _builder_compile_stmt_patch21j(self: ScratchBuilder, stmt: Any) -> Optional[str]:
    if isinstance(stmt, ExprStmt) and isinstance(stmt.expr, CallExpr) and stmt.expr.callee in {"__nested_resize_outer", "__nested_row_push", "__nested_row_clear"}:
        return _builder_compile_call_stmt_patch21j(self, stmt.expr)
    return _prev_builder_compile_stmt_patch21j(self, stmt)

ScratchBuilder.compile_stmt = _builder_compile_stmt_patch21j  # type: ignore[method-assign]

_prev_collect_expr_patch21j = _sbg_collect_calls_expr

def _sbg_collect_calls_expr(expr: Any, out: set[str]) -> None:  # type: ignore[no-redef]
    if isinstance(expr, CallExpr) and expr.callee in {"__nested_resize_outer", "__nested_row_push", "__nested_row_clear"}:
        out.add("vec_size"); out.add("at0")
    _prev_collect_expr_patch21j(expr, out)


# Patch21k: dynamic flattened struct field access/update for generic nested structs.
def _sbg_match_flat_struct_elem21(expr: Any) -> Optional[Tuple[str, Any, Any]]:
    if isinstance(expr, CallExpr) and expr.callee == "__index0_ref" and len(expr.args) == 2:
        row_expr, pos = expr.args
        if isinstance(row_expr, CallExpr) and row_expr.callee == "__index0_ref" and len(row_expr.args) == 2:
            base, row = row_expr.args
            if isinstance(base, VarExpr) and base.name in _SBG_FLAT_VECTOR_TYPES21:
                return base.name, row, pos
    return None


def _sbg_match_flat_struct_field21(expr: Any) -> Optional[Tuple[str, Any, Any, str]]:
    if isinstance(expr, CallExpr) and expr.callee == "__field_ref" and len(expr.args) == 2 and isinstance(expr.args[1], Literal):
        m = _sbg_match_flat_struct_elem21(expr.args[0])
        if m:
            base, row, pos = m
            return base, row, pos, str(expr.args[1].value)
    return None


def _sbg_match_flat_struct_vec_item21(expr: Any) -> Optional[Tuple[str, Any, Any, str, Any]]:
    if isinstance(expr, CallExpr) and expr.callee == "__index0_ref" and len(expr.args) == 2:
        m = _sbg_match_flat_struct_field21(expr.args[0])
        if m:
            base, row, pos, field = m
            return base, row, pos, field, expr.args[1]
    return None


def _sbg_flat_index_stmts21(base: str, row: Any, pos: Any, out: str) -> List[Any]:
    r = f"__sbg_flat_r_{out}"
    return [
        AssignStmt(out, "=", Literal(0)),
        AssignStmt(r, "=", Literal(0)),
        WhileStmt(BinaryExpr(VarExpr(r), "<", row), [
            AssignStmt(out, "+=", CallExpr("item", [VarExpr(f"{base}.__row_size"), BinaryExpr(VarExpr(r), "+", Literal(1))])),
            AssignStmt(r, "+=", Literal(1)),
        ]),
        AssignStmt(out, "+=", pos),
    ]

_prev_builder_lower_expr_patch21k = _builder_lower_expr

def _builder_lower_expr_patch21k(self: ScratchBuilder, expr: Any) -> Tuple[List[Any], Any]:
    mv = _sbg_match_flat_struct_vec_item21(expr)
    if mv:
        base, row, pos, field, j = mv
        idx = f"__sbg_flat_idx_{self.uid('tmp')}"
        vec = f"__sbg_flat_vec_{self.uid('tmp')}"
        tmp = f"__sbg_flat_val_{self.uid('tmp')}"
        self.var_id(idx); self.var_id(vec); self.var_id(tmp)
        stmts = _sbg_flat_index_stmts21(base, row, pos, idx)
        stmts.append(AssignStmt(vec, "=", CallExpr("item", [VarExpr(f"{base}.{field}"), BinaryExpr(VarExpr(idx), "+", Literal(1))])))
        stmts.append(VarDecl(tmp, CallExpr("at0", [VarExpr(vec), j]), True))
        return stmts, VarExpr(tmp)
    mf = _sbg_match_flat_struct_field21(expr)
    if mf:
        base, row, pos, field = mf
        idx = f"__sbg_flat_idx_{self.uid('tmp')}"
        tmp = f"__sbg_flat_val_{self.uid('tmp')}"
        self.var_id(idx); self.var_id(tmp)
        stmts = _sbg_flat_index_stmts21(base, row, pos, idx)
        stmts.append(VarDecl(tmp, CallExpr("item", [VarExpr(f"{base}.{field}"), BinaryExpr(VarExpr(idx), "+", Literal(1))]), True))
        return stmts, VarExpr(tmp)
    return _prev_builder_lower_expr_patch21k(self, expr)

_builder_lower_expr = _builder_lower_expr_patch21k  # type: ignore[assignment]

_prev_builder_compile_stmt_patch21k = ScratchBuilder.compile_stmt

def _builder_compile_stmt_patch21k(self: ScratchBuilder, stmt: Any) -> Optional[str]:
    if isinstance(stmt, LValueAssignStmt):
        mv = _sbg_match_flat_struct_vec_item21(stmt.target)
        mf = _sbg_match_flat_struct_field21(stmt.target)
        if mv:
            base, row, pos, field, j = mv
            idx = f"__sbg_flat_idx_{self.uid('tmp')}"
            vec = f"__sbg_flat_vec_{self.uid('tmp')}"
            enc = f"__sbg_flat_enc_{self.uid('tmp')}"
            tmp_list = f"__sbg_flat_tmp_list_{self.uid('tmp')}"
            self.var_id(idx); self.var_id(vec); self.var_id(enc); self.list_id(tmp_list)
            old_item = CallExpr("at0", [VarExpr(vec), j])
            new_item = stmt.expr if stmt.op == "=" else BinaryExpr(old_item, stmt.op[0], stmt.expr)
            stmts: List[Any] = _sbg_flat_index_stmts21(base, row, pos, idx)
            stmts += [
                AssignStmt(vec, "=", CallExpr("item", [VarExpr(f"{base}.{field}"), BinaryExpr(VarExpr(idx), "+", Literal(1))])),
                ExprStmt(CallExpr("__copy_value_to_list", [Literal(tmp_list), VarExpr(vec)])),
                ExprStmt(CallExpr("setItem", [VarExpr(tmp_list), BinaryExpr(j, "+", Literal(1)), new_item])),
                ExprStmt(CallExpr("__encode_list_to_var", [Literal(tmp_list), Literal(enc)])),
                ExprStmt(CallExpr("setItem", [VarExpr(f"{base}.{field}"), BinaryExpr(VarExpr(idx), "+", Literal(1)), VarExpr(enc)])),
            ]
            first: Optional[str] = None
            for stt in stmts:
                if isinstance(stt, ExprStmt) and isinstance(stt.expr, CallExpr) and stt.expr.callee == "__copy_value_to_list":
                    dst = str(stt.expr.args[0].value); src = stt.expr.args[1]
                    bid = _sbg_compile_copy_value_to_static_list21(self, dst, src)
                elif isinstance(stt, ExprStmt) and isinstance(stt.expr, CallExpr) and stt.expr.callee == "__encode_list_to_var":
                    src = str(stt.expr.args[0].value); outv = str(stt.expr.args[1].value)
                    bid = _sbg_compile_list_to_string21(self, src, outv)
                else:
                    bid = self.compile_stmt(stt)
                first = self.chain(first, bid)
            return first
        if mf:
            base, row, pos, field = mf
            idx = f"__sbg_flat_idx_{self.uid('tmp')}"
            self.var_id(idx)
            old_item = CallExpr("item", [VarExpr(f"{base}.{field}"), BinaryExpr(VarExpr(idx), "+", Literal(1))])
            new_item = stmt.expr if stmt.op == "=" else BinaryExpr(old_item, stmt.op[0], stmt.expr)
            stmts = _sbg_flat_index_stmts21(base, row, pos, idx)
            stmts.append(ExprStmt(CallExpr("setItem", [VarExpr(f"{base}.{field}"), BinaryExpr(VarExpr(idx), "+", Literal(1)), new_item])))
            return self.compile_statement_chain(stmts)
    return _prev_builder_compile_stmt_patch21k(self, stmt)

ScratchBuilder.compile_stmt = _builder_compile_stmt_patch21k  # type: ignore[method-assign]

_prev_collect_expr_patch21k = _sbg_collect_calls_expr

def _sbg_collect_calls_expr(expr: Any, out: set[str]) -> None:  # type: ignore[no-redef]
    if _sbg_match_flat_struct_vec_item21(expr): out.add("at0")
    _prev_collect_expr_patch21k(expr, out)


# Patch21l: C++ vector assignment: vector<T> a; a = otherVectorOrRow;
_prev_runtime_exec_stmt_patch21l = Runtime._exec_stmt

def _runtime_exec_stmt_patch21l(self: Runtime, stmt: Any) -> None:
    if isinstance(stmt, AssignStmt):
        name = stmt.name
        if name not in self.lists:
            alt = _sbg_unique_mangled_name21(self.lists.keys(), name)
            if alt: name = alt
        if name in self.lists and stmt.op == "=":
            rhs = self.eval(stmt.expr)
            self.lists[name].clear()
            if isinstance(rhs, list): self.lists[name].extend(rhs)
            else: self.lists[name].extend(_sbg_vec_tokens_runtime_patch20(rhs))
            return None
    return _prev_runtime_exec_stmt_patch21l(self, stmt)

Runtime._exec_stmt = _runtime_exec_stmt_patch21l  # type: ignore[method-assign]

_prev_builder_compile_stmt_patch21l = ScratchBuilder.compile_stmt

def _builder_compile_stmt_patch21l(self: ScratchBuilder, stmt: Any) -> Optional[str]:
    if isinstance(stmt, AssignStmt):
        name = stmt.name
        if name not in self.lists:
            alt = _sbg_unique_mangled_name21(self.lists.keys(), name)
            if alt: name = alt
        if name in self.lists and stmt.op == "=":
            return _sbg_compile_copy_value_to_static_list21(self, name, stmt.expr)
    return _prev_builder_compile_stmt_patch21l(self, stmt)

ScratchBuilder.compile_stmt = _builder_compile_stmt_patch21l  # type: ignore[method-assign]


# Patch21m: flat struct vectors take precedence over generic nested-vector lowering.
_prev_sbg_method_lower_patch21m = _sbg_method_lower_patch19

def _sbg_method_lower_patch19(receiver: Any, method: str, args: List[Any], parser: Optional[Parser] = None) -> CallExpr:  # type: ignore[no-redef]
    if isinstance(receiver, VarExpr) and receiver.name in _SBG_FLAT_VECTOR_TYPES21 and method == "resize":
        return _sbg_call_patch19("__flat_struct_resize_outer", [Literal(receiver.name), *args], receiver)
    if isinstance(receiver, CallExpr) and receiver.callee == "__index0_ref" and len(receiver.args) == 2:
        base, row = receiver.args
        if isinstance(base, VarExpr) and base.name in _SBG_FLAT_VECTOR_TYPES21:
            if method in {"push", "push_back"}:
                return _sbg_call_patch19("__flat_struct_push", [Literal(base.name), row, *args], receiver)
            if method == "size":
                return _sbg_call_patch19("__flat_struct_row_size", [Literal(base.name), row], receiver)
    return _prev_sbg_method_lower_patch21m(receiver, method, args, parser)

Parser.parse_postfix = _parser_parse_postfix_patch20  # type: ignore[method-assign]

_prev_runtime_eval_patch21m = Runtime._eval

def _runtime_eval_patch21m(self: Runtime, expr: Any) -> Any:
    if isinstance(expr, VarExpr) and expr.name in _SBG_FLAT_VECTOR_TYPES21 and expr.name in self.lists:
        return self.lists[expr.name]
    if isinstance(expr, CallExpr) and expr.callee == "__field_ref" and len(expr.args) == 2:
        obj = self.eval(expr.args[0]); field = str(self.eval(expr.args[1]))
        if isinstance(obj, (int, float)):
            idx = int(float(obj))
            for _st, base in _SBG_STRUCT_DEFAULT_BASE21.items():
                lname = f"{base}.{field}"
                if lname in self.lists and 0 <= idx < len(self.lists[lname]):
                    val = self.lists[lname][idx]
                    # Vector fields are stored as encoded row strings; native code wants a list.
                    ftyp = None
                    for t, f in _SBG_STRUCT_DEFS21.get(_st, []):
                        if f == field: ftyp = t; break
                    if ftyp and _sbg_is_vector21(ftyp):
                        return [_sbg_num_or_text_patch20(x) for x in _sbg_vec_tokens_runtime_patch20(val)]
                    return val
    return _prev_runtime_eval_patch21m(self, expr)

Runtime._eval = _runtime_eval_patch21m  # type: ignore[method-assign]


# Patch21n: native foreach over encoded row strings.
_prev_runtime_call_patch21n = Runtime.call

def _runtime_call_patch21n(self: Runtime, name: str, args: List[Any]) -> Any:
    if name in {"len", "size"} and len(args) == 1 and isinstance(args[0], str):
        if any(sep in args[0] for sep in [" ", ",", ";", "\x1f"]):
            return len(_sbg_vec_tokens_runtime_patch20(args[0]))
    if name == "item" and len(args) == 2 and isinstance(args[0], str) and args[0] not in self.lists:
        return _sbg_vec_at0_runtime_patch20(args[0], int(float(args[1])) - 1)
    return _prev_runtime_call_patch21n(self, name, args)

Runtime.call = _runtime_call_patch21n  # type: ignore[method-assign]


# Patch21o: native flat struct push copies actual struct fields into flat record tables.
# Earlier native mode stored only row handles; Scratch compile already lowers this to
# record-table pushes.  This keeps native run and compiled Scratch semantics aligned
# for code such as graph[layer].push_back(e); graph[layer][i].cost.
_prev_runtime_eval_patch21o = Runtime._eval

def _sbg_encode_runtime_vec21(value: Any) -> str:
    if isinstance(value, str):
        # Already encoded row string, unless it is an ordinary scalar string.
        return value
    if isinstance(value, list):
        return " ".join(str(x) for x in value)
    return str(value)

def _runtime_eval_patch21o(self: Runtime, expr: Any) -> Any:
    if isinstance(expr, CallExpr) and expr.callee == "__flat_struct_resize_outer" and len(expr.args) >= 2:
        base = str(self.eval(expr.args[0]))
        n = max(0, int(float(self.eval(expr.args[1]))))
        self.lists[base] = (self.lists.get(base, []) + [""] * n)[:n]
        self.lists[f"{base}.__row_size"] = (self.lists.get(f"{base}.__row_size", []) + [0] * n)[:n]
        # Also keep legacy native rows for code paths that still use self.vars.
        rows = self.vars.setdefault(base, [])
        if not isinstance(rows, list):
            rows = []; self.vars[base] = rows
        while len(rows) > n: rows.pop()
        while len(rows) < n: rows.append([])
        return None

    if isinstance(expr, CallExpr) and expr.callee == "__flat_struct_push" and len(expr.args) == 3:
        base = str(self.eval(expr.args[0]))
        row = max(0, int(float(self.eval(expr.args[1]))))
        st = _SBG_FLAT_VECTOR_TYPES21.get(base)
        fields = _SBG_STRUCT_DEFS21.get(st or "", [])
        if not st or not fields:
            # Fall back to older behavior if this is not a known flattened struct vector.
            return _prev_runtime_eval_patch21o(self, expr)

        # Ensure row metadata exists.
        while len(self.lists.setdefault(base, [])) <= row:
            self.lists[base].append("")
        while len(self.lists.setdefault(f"{base}.__row_size", [])) <= row:
            self.lists[f"{base}.__row_size"].append(0)

        first_field = fields[0][1]
        flat_idx = len(self.lists.setdefault(f"{base}.{first_field}", []))
        row_text = str(self.lists[base][row])
        self.lists[base][row] = (row_text + (" " if row_text else "") + str(flat_idx))
        self.lists[f"{base}.__row_size"][row] = int(float(self.lists[f"{base}.__row_size"][row] or 0)) + 1

        val_expr = expr.args[2]
        val_obj = None
        # Only evaluate the value if needed; for static struct variables we copy
        # from n.field / n.vectorField, which is exactly how the compiler lowers it.
        if not isinstance(val_expr, VarExpr):
            val_obj = self.eval(val_expr)

        for ftyp, fname in fields:
            dst = f"{base}.{fname}"
            self.lists.setdefault(dst, [])
            copied = 0
            if isinstance(val_expr, VarExpr):
                src = f"{val_expr.name}.{fname}"
                alt_src = src
                # Locals may be name-mangled; recover the single matching local field.
                if src not in self.vars and src not in self.lists:
                    suffix = "." + fname
                    candidates = [n for n in list(self.vars.keys()) + list(self.lists.keys()) if n.endswith(suffix) and n.split('.')[-2].endswith('_' + _sbg_sanitize_name(val_expr.name))]
                    if len(candidates) == 1:
                        alt_src = candidates[0]
                if _sbg_is_vector21(ftyp):
                    if alt_src in self.lists:
                        self.lists[dst].append(_sbg_encode_runtime_vec21(self.lists[alt_src])); copied = 1
                    elif alt_src in self.vars:
                        self.lists[dst].append(_sbg_encode_runtime_vec21(self.vars[alt_src])); copied = 1
                else:
                    if alt_src in self.vars:
                        self.lists[dst].append(self.vars[alt_src]); copied = 1
                    elif alt_src in self.lists:
                        self.lists[dst].append(_sbg_encode_runtime_vec21(self.lists[alt_src])); copied = 1
            if not copied:
                if isinstance(val_obj, dict):
                    raw = val_obj.get(fname, [] if _sbg_is_vector21(ftyp) else 0)
                    self.lists[dst].append(_sbg_encode_runtime_vec21(raw) if _sbg_is_vector21(ftyp) else raw)
                else:
                    self.lists[dst].append("" if _sbg_is_vector21(ftyp) else 0)

        # Legacy mirror for older native paths.
        rows = self.vars.setdefault(base, [])
        if isinstance(rows, list):
            while len(rows) <= row: rows.append([])
            rows[row].append(flat_idx)
        return None

    return _prev_runtime_eval_patch21o(self, expr)

Runtime._eval = _runtime_eval_patch21o  # type: ignore[method-assign]


# Patch21p: native fallback for un-mangled C++ local names in large functions.
# Older local lowering keeps all locals as Scratch globals, so several variables
# called `layer`/`i` can exist after different loops. For native execution pick
# the newest executed __loc_N_name when an un-mangled VarExpr escaped lowering.
def _sbg_latest_mangled_name21(names: Iterable[str], original: str) -> Optional[str]:
    suffix = "_" + _sbg_sanitize_name(original)
    best: Optional[Tuple[int, str]] = None
    for n in names:
        if not (n.startswith("__loc_") and n.endswith(suffix)):
            continue
        try:
            num = int(n.split("_", 3)[2])
        except Exception:
            num = -1
        if best is None or num > best[0]:
            best = (num, n)
    return best[1] if best else None

_prev_runtime_eval_patch21p = Runtime._eval

def _runtime_eval_patch21p(self: Runtime, expr: Any) -> Any:
    if isinstance(expr, VarExpr) and expr.name not in self.vars and expr.name not in self.lists:
        alt = _sbg_latest_mangled_name21([*self.vars.keys(), *self.lists.keys()], expr.name)
        if alt:
            return self.vars[alt] if alt in self.vars else self.lists[alt]
    return _prev_runtime_eval_patch21p(self, expr)

Runtime._eval = _runtime_eval_patch21p  # type: ignore[method-assign]



# Patch22: generic nested vector item assignment for C++-style code.
# Handles expressions like mat[row][col] = x and mat[row][col] += x by lowering
# a row-string list item to a temporary list, mutating it, encoding it back, and
# replacing the original row. This is generic vector<vector<T>> support, not a
# domain-specific special case.
def _sbg_match_nested_vector_item22(expr: Any) -> Optional[Tuple[str, Any, Any]]:
    if isinstance(expr, CallExpr) and expr.callee == "__index0_ref" and len(expr.args) == 2:
        row_expr, col = expr.args
        if isinstance(row_expr, CallExpr) and row_expr.callee == "__index0_ref" and len(row_expr.args) == 2:
            base, row = row_expr.args
            if isinstance(base, VarExpr) and base.name in globals().get("_SBG_NESTED_VECTOR_NAMES21", set()) and base.name not in globals().get("_SBG_FLAT_VECTOR_TYPES21", {}):
                return base.name, row, col
    return None

_prev_builder_compile_stmt_patch22_nested = ScratchBuilder.compile_stmt

def _builder_compile_stmt_patch22_nested(self: ScratchBuilder, stmt: Any) -> Optional[str]:
    if isinstance(stmt, LValueAssignStmt):
        m = _sbg_match_nested_vector_item22(stmt.target)
        if m:
            base, row, col = m
            tmp_row = f"__sbg_nested_row_{self.uid('tmp')}"
            tmp_enc = f"__sbg_nested_enc_{self.uid('tmp')}"
            tmp_list = f"__sbg_nested_tmp_{self.uid('tmp')}"
            self.var_id(tmp_row); self.var_id(tmp_enc); self.list_id(tmp_list)
            old_item = CallExpr("at0", [VarExpr(tmp_row), col])
            new_item = stmt.expr if stmt.op == "=" else BinaryExpr(old_item, stmt.op[0], stmt.expr)
            stmts: List[Any] = [
                AssignStmt(tmp_row, "=", CallExpr("item", [VarExpr(base), BinaryExpr(row, "+", Literal(1))])),
                ExprStmt(CallExpr("__copy_value_to_list", [Literal(tmp_list), VarExpr(tmp_row)])),
                ExprStmt(CallExpr("setItem", [VarExpr(tmp_list), BinaryExpr(col, "+", Literal(1)), new_item])),
                ExprStmt(CallExpr("__encode_list_to_var", [Literal(tmp_list), Literal(tmp_enc)])),
                ExprStmt(CallExpr("setItem", [VarExpr(base), BinaryExpr(row, "+", Literal(1)), VarExpr(tmp_enc)])),
            ]
            first: Optional[str] = None
            for stt in stmts:
                if isinstance(stt, ExprStmt) and isinstance(stt.expr, CallExpr) and stt.expr.callee == "__copy_value_to_list":
                    dst = str(stt.expr.args[0].value); src = stt.expr.args[1]
                    bid = _sbg_compile_copy_value_to_static_list21(self, dst, src)
                elif isinstance(stt, ExprStmt) and isinstance(stt.expr, CallExpr) and stt.expr.callee == "__encode_list_to_var":
                    src = str(stt.expr.args[0].value); outv = str(stt.expr.args[1].value)
                    bid = _sbg_compile_list_to_string21(self, src, outv)
                else:
                    bid = self.compile_stmt(stt)
                first = self.chain(first, bid)
            return first
    return _prev_builder_compile_stmt_patch22_nested(self, stmt)

ScratchBuilder.compile_stmt = _builder_compile_stmt_patch22_nested  # type: ignore[method-assign]

_prev_runtime_exec_stmt_patch22_nested = Runtime._exec_stmt

def _runtime_exec_stmt_patch22_nested(self: Runtime, stmt: Any) -> None:
    if isinstance(stmt, LValueAssignStmt):
        m = _sbg_match_nested_vector_item22(stmt.target)
        if m:
            base, row_expr, col_expr = m
            row = int(float(self.eval(row_expr)))
            col = int(float(self.eval(col_expr)))
            rows = self.lists.setdefault(base, [])
            while len(rows) <= row:
                rows.append("")
            items = _sbg_vec_tokens_runtime_patch20(rows[row])
            while len(items) <= col:
                items.append("0")
            old = _sbg_num_or_text_patch20(items[col])
            rhs = self.eval(stmt.expr)
            if stmt.op == "=": new = rhs
            elif stmt.op == "+=": new = old + rhs
            elif stmt.op == "-=": new = old - rhs
            elif stmt.op == "*=": new = old * rhs
            elif stmt.op == "/=": new = old / rhs
            elif stmt.op == "%=": new = old % rhs
            else: new = rhs
            items[col] = str(new)
            rows[row] = " ".join(str(x) for x in items)
            return None
    return _prev_runtime_exec_stmt_patch22_nested(self, stmt)

Runtime._exec_stmt = _runtime_exec_stmt_patch22_nested  # type: ignore[method-assign]



# Patch22b: keep native vector<vector<T>> row-list mirror in sync for mat[i][j] assignments.
_prev_runtime_exec_stmt_patch22b_nested = Runtime._exec_stmt

def _runtime_exec_stmt_patch22b_nested(self: Runtime, stmt: Any) -> None:
    if isinstance(stmt, LValueAssignStmt):
        m = _sbg_match_nested_vector_item22(stmt.target)
        if m:
            base, row_expr, col_expr = m
            row = int(float(self.eval(row_expr)))
            col = int(float(self.eval(col_expr)))
            rows_obj = self.vars.setdefault(base, [])
            if not isinstance(rows_obj, list):
                rows_obj = []
                self.vars[base] = rows_obj
            while len(rows_obj) <= row:
                rows_obj.append([])
            if not isinstance(rows_obj[row], list):
                rows_obj[row] = [_sbg_num_or_text_patch20(x) for x in _sbg_vec_tokens_runtime_patch20(rows_obj[row])]
            while len(rows_obj[row]) <= col:
                rows_obj[row].append(0)
            old = rows_obj[row][col]
            rhs = self.eval(stmt.expr)
            if stmt.op == "=": new = rhs
            elif stmt.op == "+=": new = old + rhs
            elif stmt.op == "-=": new = old - rhs
            elif stmt.op == "*=": new = old * rhs
            elif stmt.op == "/=": new = old / rhs
            elif stmt.op == "%=": new = old % rhs
            else: new = rhs
            rows_obj[row][col] = new
            self.lists[base] = [" ".join(str(x) for x in r) if isinstance(r, list) else str(r) for r in rows_obj]
            return None
    return _prev_runtime_exec_stmt_patch22b_nested(self, stmt)

Runtime._exec_stmt = _runtime_exec_stmt_patch22b_nested  # type: ignore[method-assign]



# Patch22c: native dynamic flattened struct field assignment, generic graph[row][i].field += x.
_prev_runtime_exec_stmt_patch22c_flat = Runtime._exec_stmt

def _runtime_exec_stmt_patch22c_flat(self: Runtime, stmt: Any) -> None:
    if isinstance(stmt, LValueAssignStmt):
        mf = _sbg_match_flat_struct_field21(stmt.target)
        mv = _sbg_match_flat_struct_vec_item21(stmt.target)
        if mf and not mv:
            base, row_expr, pos_expr, field = mf
            row = int(float(self.eval(row_expr)))
            pos = int(float(self.eval(pos_expr)))
            sizes = self.lists.setdefault(f"{base}.__row_size", [])
            idx = 0
            for r in range(max(0, row)):
                idx += int(float(sizes[r] if r < len(sizes) and sizes[r] != "" else 0))
            idx += pos
            arr = self.lists.setdefault(f"{base}.{field}", [])
            while len(arr) <= idx:
                arr.append(0)
            old = arr[idx]
            rhs = self.eval(stmt.expr)
            if stmt.op == "=": new = rhs
            elif stmt.op == "+=": new = old + rhs
            elif stmt.op == "-=": new = old - rhs
            elif stmt.op == "*=": new = old * rhs
            elif stmt.op == "/=": new = old / rhs
            elif stmt.op == "%=": new = old % rhs
            else: new = rhs
            arr[idx] = new
            return None
    return _prev_runtime_exec_stmt_patch22c_flat(self, stmt)

Runtime._exec_stmt = _runtime_exec_stmt_patch22c_flat  # type: ignore[method-assign]


# =============================================================================
# Patch 23: dynamic terminal and input-prompt visibility
# =============================================================================
# Vanilla Scratch cannot close an active ask-and-wait bubble in the middle of the
# current ask.  The compiler therefore gates the *next* prompt with a global flag,
# and exposes terminal.show()/hide() plus terminal.showPrompt()/hidePrompt().

VERSION = "0.9.0-patch24-keyboard"

TERMINAL_VISIBLE_VAR = "__sbg_terminal_visible"
TERMINAL_INPUT_ENABLED_VAR = "__sbg_terminal_input_enabled"
TERMINAL_VISIBLE_VAR_ID = "sbg:terminal:visible"
TERMINAL_INPUT_ENABLED_VAR_ID = "sbg:terminal:input_enabled"
TERMINAL_HIDDEN_WAIT_SECONDS = 0.05

_TERMINAL_VISIBILITY_NAMES = {
    "showTerminal", "hideTerminal", "toggleTerminal",
    "showInputPrompt", "hideInputPrompt", "enableInputPrompt", "disableInputPrompt",
    "enableTerminalInput", "disableTerminalInput", "setInputPromptVisible", "setTerminalInputEnabled",
    "showTerminalAndPrompt", "hideTerminalAndPrompt", "terminalVisible", "terminalPromptVisible",
}
BUILTIN_STMT_NAMES.update(_TERMINAL_VISIBILITY_NAMES)
BUILTIN_EXPR_NAMES.update({"terminalVisible", "terminalPromptVisible"})
BUILTIN_NAMES.update(_TERMINAL_VISIBILITY_NAMES)

_prev_var_id_patch23 = ScratchBuilder.var_id

def _scratchbuilder_var_id_patch23(self: ScratchBuilder, name: str) -> str:
    if name == TERMINAL_VISIBLE_VAR:
        self.variables.setdefault(name, TERMINAL_VISIBLE_VAR_ID)
        return TERMINAL_VISIBLE_VAR_ID
    if name == TERMINAL_INPUT_ENABLED_VAR:
        self.variables.setdefault(name, TERMINAL_INPUT_ENABLED_VAR_ID)
        return TERMINAL_INPUT_ENABLED_VAR_ID
    return _prev_var_id_patch23(self, name)

ScratchBuilder.var_id = _scratchbuilder_var_id_patch23  # type: ignore[method-assign]


def _sbg_terminal_defaults_patch23(compiler: Any) -> None:
    compiler.b.var_id(TERMINAL_VISIBLE_VAR)
    compiler.b.var_id(TERMINAL_INPUT_ENABLED_VAR)
    compiler.init_values.setdefault(TERMINAL_VISIBLE_VAR, 1)
    compiler.init_values.setdefault(TERMINAL_INPUT_ENABLED_VAR, 1)

_prev_compiler_analyze_patch23 = Compiler.analyze

def _compiler_analyze_patch23(self: Compiler) -> None:
    _prev_compiler_analyze_patch23(self)
    _sbg_terminal_defaults_patch23(self)

Compiler.analyze = _compiler_analyze_patch23  # type: ignore[method-assign]


def _sbg_make_terminal_visible_setter_patch23(b: ScratchBuilder, visible: bool) -> str:
    b.list_id(TERMINAL_LIST_NAME)
    set_flag = _builder_make_set_var(b, TERMINAL_VISIBLE_VAR, Literal(1 if visible else 0))
    vis = b.add_block(
        "data_showlist" if visible else "data_hidelist",
        fields={"LIST": [TERMINAL_LIST_NAME, b.list_id(TERMINAL_LIST_NAME)]},
    )
    b.blocks[set_flag]["next"] = vis
    b.blocks[vis]["parent"] = set_flag
    return set_flag


def _sbg_make_prompt_visible_setter_patch23(b: ScratchBuilder, enabled: bool) -> str:
    return _builder_make_set_var(b, TERMINAL_INPUT_ENABLED_VAR, Literal(1 if enabled else 0))


def _sbg_make_terminal_show_all_patch23(b: ScratchBuilder) -> str:
    first = _sbg_make_terminal_visible_setter_patch23(b, True)
    second = _sbg_make_prompt_visible_setter_patch23(b, True)
    b.chain(first, second)
    return first


def _sbg_make_terminal_hide_all_patch23(b: ScratchBuilder) -> str:
    first = _sbg_make_terminal_visible_setter_patch23(b, False)
    second = _sbg_make_prompt_visible_setter_patch23(b, False)
    b.chain(first, second)
    return first


def _sbg_make_prompt_ifelse_loop_body_patch23(self: Compiler, ask_chain_first: str, wait_chain_first: str) -> str:
    cond = BinaryExpr(VarExpr(TERMINAL_INPUT_ENABLED_VAR), "==", Literal(1))
    bid = self.b.add_block("control_if_else", inputs={})
    self.b.blocks[bid]["inputs"]["CONDITION"] = self.b.expr_input(cond, bid)
    self.b.blocks[bid]["inputs"]["SUBSTACK"] = self.b.substack_input(ask_chain_first)
    self.b.blocks[bid]["inputs"]["SUBSTACK2"] = self.b.substack_input(wait_chain_first)
    self.b.set_parent(ask_chain_first, bid)
    self.b.set_parent(wait_chain_first, bid)
    return bid


def _compiler_compile_console_flag_loop_patch23(self: Compiler) -> None:
    assert self.action_argid is not None
    _sbg_terminal_defaults_patch23(self)
    has_action_return = _sbg_action_entries_have_return(self.action_entries)

    hat = self.b.add_block("event_whenflagclicked", topLevel=True, x=536, y=455)
    reset = _sbg_compile_delta_reset(self.b)
    forever = self.b.add_block("control_forever", inputs={})

    ask = self.b.add_block("sensing_askandwait", inputs={"QUESTION": [1, [10, ">"]]})
    answer = self.b.add_block("sensing_answer")
    echo = _builder_make_log_to_terminal(self.b, CallExpr("join", [Literal("> "), CallExpr("answer", [])]))
    call = self.b.add_block("procedures_call", inputs={}, mutation={
        "tagName": "mutation",
        "children": [],
        "proccode": "Action %s",
        "argumentids": json.dumps([self.action_argid]),
        "warp": "true",
    })
    self.b.blocks[call]["inputs"][self.action_argid] = [3, answer, [10, ""]]
    self.b.blocks[answer]["parent"] = call
    self.b.blocks[ask]["next"] = echo
    self.b.blocks[echo]["parent"] = ask
    self.b.blocks[echo]["next"] = call
    self.b.blocks[call]["parent"] = echo

    ask_chain_first = ask
    if has_action_return:
        cond = BinaryExpr(VarExpr(ACTION_RETURN_FLAG), "==", Literal(1))
        if_ret = self.b.add_block("control_if", parent=call, inputs={})
        self.b.blocks[if_ret]["inputs"]["CONDITION"] = self.b.expr_input(cond, if_ret)
        ret_log = _builder_make_log_to_terminal(self.b, CallExpr("join", [Literal("=> "), VarExpr(ACTION_RETURN_VAR)]))
        self.b.blocks[if_ret]["inputs"]["SUBSTACK"] = self.b.substack_input(ret_log)
        self.b.set_parent(ret_log, if_ret)
        self.b.blocks[call]["next"] = if_ret

    wait_block = self.b.add_block("control_wait", inputs={})
    self.b.blocks[wait_block]["inputs"]["DURATION"] = self.b.expr_input(Literal(TERMINAL_HIDDEN_WAIT_SECONDS), wait_block)
    gate = _sbg_make_prompt_ifelse_loop_body_patch23(self, ask_chain_first, wait_block)
    self.b.blocks[gate]["parent"] = forever

    self.b.blocks[hat]["next"] = reset or forever
    if reset:
        last = reset
        while self.b.blocks[last].get("next"):
            last = self.b.blocks[last]["next"]
        self.b.blocks[last]["next"] = forever
        self.b.blocks[forever]["parent"] = last
    else:
        self.b.blocks[forever]["parent"] = hat
    self.b.blocks[forever]["inputs"]["SUBSTACK"] = [2, gate]

def _compiler_compile_console_flag_loop_single_main_patch25(self: Compiler) -> None:
    """Green flag runs main() exactly once, no outer REPL ask(">") loop.

    Vanilla Scratch's `ask and wait` block already only shows the input box
    while that specific block is waiting for an answer, and hides it the
    instant it's answered. The only reason the input box used to appear to
    be "always open" for plain main()-style programs was the wrapping
    `forever { ask(">") ... }` console loop, which immediately reopened a
    new (empty) prompt right after main() finished. Skipping that outer loop
    for single-main programs means the input box now only ever appears for
    real `cin >> x;` calls inside the program.
    """
    assert self.action_argid is not None
    _sbg_terminal_defaults_patch23(self)
    has_action_return = _sbg_action_entries_have_return(self.action_entries)

    hat = self.b.add_block("event_whenflagclicked", topLevel=True, x=536, y=455)
    reset = _sbg_compile_delta_reset(self.b)
    call = self.b.add_block("procedures_call", inputs={}, mutation={
        "tagName": "mutation",
        "children": [],
        "proccode": "Action %s",
        "argumentids": json.dumps([self.action_argid]),
        "warp": "true",
    })
    self.b.blocks[call]["inputs"][self.action_argid] = [1, [10, ""]]

    if has_action_return:
        cond = BinaryExpr(VarExpr(ACTION_RETURN_FLAG), "==", Literal(1))
        if_ret = self.b.add_block("control_if", parent=call, inputs={})
        self.b.blocks[if_ret]["inputs"]["CONDITION"] = self.b.expr_input(cond, if_ret)
        ret_log = _builder_make_log_to_terminal(self.b, CallExpr("join", [Literal("=> "), VarExpr(ACTION_RETURN_VAR)]))
        self.b.blocks[if_ret]["inputs"]["SUBSTACK"] = self.b.substack_input(ret_log)
        self.b.set_parent(ret_log, if_ret)
        self.b.blocks[call]["next"] = if_ret

    self.b.blocks[hat]["next"] = reset or call
    if reset:
        last = reset
        while self.b.blocks[last].get("next"):
            last = self.b.blocks[last]["next"]
        self.b.blocks[last]["next"] = call
        self.b.blocks[call]["parent"] = last
    else:
        self.b.blocks[call]["parent"] = hat


_old_compiler_compile_console_flag_loop_patch25 = Compiler.compile_console_flag_loop

def _compiler_compile_console_flag_loop_patch25(self: Compiler) -> None:
    if getattr(self, "single_cpp_main_body", None) is not None:
        return _compiler_compile_console_flag_loop_single_main_patch25(self)
    return _old_compiler_compile_console_flag_loop_patch25(self)

Compiler.compile_console_flag_loop = _compiler_compile_console_flag_loop_patch25  # type: ignore[method-assign]


_prev_compile_call_stmt_patch23 = ScratchBuilder.compile_call_stmt

def _compile_call_stmt_patch23(self: ScratchBuilder, expr: CallExpr) -> Optional[str]:
    name, a = expr.callee, expr.args
    if name == "showTerminal":
        self.need_args(name, a, 0)
        return _sbg_make_terminal_visible_setter_patch23(self, True)
    if name == "hideTerminal":
        self.need_args(name, a, 0)
        return _sbg_make_terminal_visible_setter_patch23(self, False)
    if name in {"showInputPrompt", "enableInputPrompt", "enableTerminalInput"}:
        self.need_args(name, a, 0)
        return _sbg_make_prompt_visible_setter_patch23(self, True)
    if name in {"hideInputPrompt", "disableInputPrompt", "disableTerminalInput"}:
        self.need_args(name, a, 0)
        return _sbg_make_prompt_visible_setter_patch23(self, False)
    if name in {"setInputPromptVisible", "setTerminalInputEnabled"}:
        self.need_args(name, a, 1)
        cond = BinaryExpr(a[0], "!=", Literal(0))
        then_first = _sbg_make_prompt_visible_setter_patch23(self, True)
        else_first = _sbg_make_prompt_visible_setter_patch23(self, False)
        return _sbg_if_else_block(self, cond, then_first, else_first)
    if name == "showTerminalAndPrompt":
        self.need_args(name, a, 0)
        return _sbg_make_terminal_show_all_patch23(self)
    if name == "hideTerminalAndPrompt":
        self.need_args(name, a, 0)
        return _sbg_make_terminal_hide_all_patch23(self)
    if name == "toggleTerminal":
        self.need_args(name, a, 0)
        cond = BinaryExpr(VarExpr(TERMINAL_VISIBLE_VAR), "==", Literal(1))
        then_first = _sbg_make_terminal_visible_setter_patch23(self, False)
        else_first = _sbg_make_terminal_visible_setter_patch23(self, True)
        return _sbg_if_else_block(self, cond, then_first, else_first)
    return _prev_compile_call_stmt_patch23(self, expr)

ScratchBuilder.compile_call_stmt = _compile_call_stmt_patch23  # type: ignore[method-assign]


_prev_compile_call_expr_patch23 = ScratchBuilder.compile_call_expr

def _compile_call_expr_patch23(self: ScratchBuilder, expr: CallExpr, parent: Optional[str]) -> str:
    if expr.callee == "terminalVisible":
        self.need_args(expr.callee, expr.args, 0)
        return self.compile_expr(VarExpr(TERMINAL_VISIBLE_VAR), parent)
    if expr.callee == "terminalPromptVisible":
        self.need_args(expr.callee, expr.args, 0)
        return self.compile_expr(VarExpr(TERMINAL_INPUT_ENABLED_VAR), parent)
    return _prev_compile_call_expr_patch23(self, expr, parent)

ScratchBuilder.compile_call_expr = _compile_call_expr_patch23  # type: ignore[method-assign]


_prev_runtime_call_patch23 = Runtime.call

def _runtime_call_patch23(self: Runtime, name: str, args: List[Any]) -> Any:
    if not hasattr(self, "_sbg_terminal_visible"):
        self._sbg_terminal_visible = True
    if not hasattr(self, "_sbg_terminal_input_enabled"):
        self._sbg_terminal_input_enabled = True
    if name == "showTerminal": self._sbg_terminal_visible = True; self.vars[TERMINAL_VISIBLE_VAR] = 1; return None
    if name == "hideTerminal": self._sbg_terminal_visible = False; self.vars[TERMINAL_VISIBLE_VAR] = 0; return None
    if name in {"showInputPrompt", "enableInputPrompt", "enableTerminalInput"}: self._sbg_terminal_input_enabled = True; self.vars[TERMINAL_INPUT_ENABLED_VAR] = 1; return None
    if name in {"hideInputPrompt", "disableInputPrompt", "disableTerminalInput"}: self._sbg_terminal_input_enabled = False; self.vars[TERMINAL_INPUT_ENABLED_VAR] = 0; return None
    if name in {"setInputPromptVisible", "setTerminalInputEnabled"}:
        enabled = bool(args and float(args[0]) != 0)
        self._sbg_terminal_input_enabled = enabled; self.vars[TERMINAL_INPUT_ENABLED_VAR] = 1 if enabled else 0; return None
    if name == "showTerminalAndPrompt":
        self.call("showTerminal", []); self.call("showInputPrompt", []); return None
    if name == "hideTerminalAndPrompt":
        self.call("hideTerminal", []); self.call("hideInputPrompt", []); return None
    if name == "toggleTerminal":
        if getattr(self, "_sbg_terminal_visible", True): self.call("hideTerminal", [])
        else: self.call("showTerminal", [])
        return None
    if name == "terminalVisible": return 1 if getattr(self, "_sbg_terminal_visible", True) else 0
    if name == "terminalPromptVisible": return 1 if getattr(self, "_sbg_terminal_input_enabled", True) else 0
    return _prev_runtime_call_patch23(self, name, args)

Runtime.call = _runtime_call_patch23  # type: ignore[method-assign]


_prev_sbg_method_lower_patch23 = _sbg_method_lower_patch19

def _sbg_method_lower_patch19(receiver: Any, method: str, args: List[Any], parser: Optional[Parser] = None) -> CallExpr:  # type: ignore[no-redef]
    rname = receiver.name if isinstance(receiver, VarExpr) else ""
    if rname in {"console", "terminal"}:
        mapping = {
            "log": "log", "info": "logInfo", "warn": "logWarn", "error": "logError",
            "sep": "logSeparator", "header": "logHeader", "clear": "clearTerminal",
            "show": "showTerminal", "hide": "hideTerminal", "toggle": "toggleTerminal",
            "show_prompt": "showInputPrompt", "showPrompt": "showInputPrompt", "enable_prompt": "showInputPrompt",
            "enablePrompt": "showInputPrompt", "enable_input": "enableTerminalInput", "enableInput": "enableTerminalInput",
            "hide_prompt": "hideInputPrompt", "hidePrompt": "hideInputPrompt", "disable_prompt": "hideInputPrompt",
            "disablePrompt": "hideInputPrompt", "disable_input": "disableTerminalInput", "disableInput": "disableTerminalInput",
            "input": "setTerminalInputEnabled", "set_input": "setTerminalInputEnabled", "setInput": "setTerminalInputEnabled",
            "show_all": "showTerminalAndPrompt", "showAll": "showTerminalAndPrompt",
            "hide_all": "hideTerminalAndPrompt", "hideAll": "hideTerminalAndPrompt",
            "visible": "terminalVisible", "is_visible": "terminalVisible", "isVisible": "terminalVisible",
            "prompt_visible": "terminalPromptVisible", "promptVisible": "terminalPromptVisible",
            "input_enabled": "terminalPromptVisible", "inputEnabled": "terminalPromptVisible",
        }
        if method in mapping:
            return _sbg_call_patch19(mapping[method], args, receiver)
    return _prev_sbg_method_lower_patch23(receiver, method, args, parser)

Parser.parse_postfix = _parser_parse_postfix_patch20  # type: ignore[method-assign]


_prev_runtime_run_scratch_terminal_patch23 = Runtime.run_scratch_terminal

def _runtime_run_scratch_terminal_patch23(self: Runtime, *, prompt: str = "sbg> ") -> None:
    self.prepare_scratch_console()
    if not self.action_events:
        raise RuntimeSBGError("nothing runnable: no Action(Input) body was prepared")
    print("StageBG native terminal. Type /exit or press Ctrl+D to quit.")
    while True:
        if not getattr(self, "_sbg_terminal_input_enabled", True):
            print("[input prompt hidden; native terminal stopped]")
            break
        try:
            line = input(prompt)
        except EOFError:
            print()
            break
        if line in ("/exit", ":q", "quit", "exit"):
            break
        self.call("log", ["> " + str(line)])
        self.run_action(line)
        if getattr(self, "last_action_returned", False):
            self.call("log", ["=> " + str(getattr(self, "last_action_return_value", ""))])

Runtime.run_scratch_terminal = _runtime_run_scratch_terminal_patch23  # type: ignore[method-assign]


_prev_project_ensure_patch23 = _project_ensure_patch17 if "_project_ensure_patch17" in globals() else (lambda project: project)

def _project_ensure_patch23(project: Dict[str, Any]) -> Dict[str, Any]:
    project = _prev_project_ensure_patch23(project)
    meta = project.setdefault("meta", {})
    meta["agent"] = f"StageBG/SBG {VERSION}"
    meta["stagebgPatch23"] = "Dynamic terminal monitor and input-prompt visibility; terminal.show()/hide()/hidePrompt()/showPrompt()."
    return project

_prev_compiler_compile_patch23 = Compiler.compile

def _compiler_compile_patch23(self: Compiler) -> Dict[str, Any]:
    return _project_ensure_patch23(_prev_compiler_compile_patch23(self))

Compiler.compile = _compiler_compile_patch23  # type: ignore[method-assign]



# =============================================================================
# Patch 24: keyboard input/events for vanilla Scratch
# =============================================================================

VERSION = "0.9.0-patch24-keyboard"

# Vanilla Scratch key names accepted by the key hat/sensing menu. Single letters,
# digits and ordinary printable characters are also passed through unchanged.
_SBG_KEY_ALIASES = {
    "space": "space", "spacja": "space",
    "any": "any", "dowolny": "any",
    "up": "up arrow", "up_arrow": "up arrow", "arrow_up": "up arrow", "up arrow": "up arrow",
    "down": "down arrow", "down_arrow": "down arrow", "arrow_down": "down arrow", "down arrow": "down arrow",
    "left": "left arrow", "left_arrow": "left arrow", "arrow_left": "left arrow", "left arrow": "left arrow",
    "right": "right arrow", "right_arrow": "right arrow", "arrow_right": "right arrow", "right arrow": "right arrow",
    "enter": "enter", "return": "enter",
}

def _sbg_normalize_key_name_patch24(value: Any) -> str:
    key = str(value)
    return _SBG_KEY_ALIASES.get(key, _SBG_KEY_ALIASES.get(key.lower(), key))

_prev_parse_event_patch24 = Parser.parse_event

def _parse_event_patch24(self: Parser, start_token: Token) -> EventDecl:
    # on key "space" { ... }
    # on key("space") { ... }
    # on key any { ... }
    if self.peek().value == "key":
        self.advance()
        if self.match("("):
            if self.peek().kind == "STRING":
                value = self.advance().value
            elif self.peek().kind in ("IDENT", "KW"):
                value = self.advance().value
            else:
                raise self.error("expected key name, e.g. on key(\"space\")")
            self.expect(")")
        else:
            if self.peek().kind == "STRING":
                value = self.advance().value
            elif self.peek().kind in ("IDENT", "KW"):
                value = self.advance().value
            else:
                raise self.error("expected key name after `on key`, e.g. on key \"space\"")
        return self.loc(EventDecl("key", _sbg_normalize_key_name_patch24(value), self.parse_block()), start_token)
    return _prev_parse_event_patch24(self, start_token)

Parser.parse_event = _parse_event_patch24  # type: ignore[method-assign]

# `keyboard.pressed("space")`, `keys.down("left")`, etc. lower to the existing
# vanilla sensing reporter `keyPressed("...")`.
_prev_sbg_method_lower_patch24 = _sbg_method_lower_patch19

def _sbg_method_lower_patch19(receiver: Any, method: str, args: List[Any], parser: Optional[Parser] = None) -> CallExpr:  # type: ignore[no-redef]
    rname = receiver.name if isinstance(receiver, VarExpr) else ""
    if rname in {"keyboard", "keys", "key"}:
        if method in {"pressed", "down", "isPressed", "is_down", "isDown"}:
            return _sbg_call_patch19("keyPressed", args, receiver)
    return _prev_sbg_method_lower_patch24(receiver, method, args, parser)

Parser.parse_postfix = _parser_parse_postfix_patch20  # type: ignore[method-assign]

# Add event collection/compilation to the Stage compiler without rewriting all older patches.
_prev_compiler_init_patch24 = Compiler.__init__

def _compiler_init_patch24(self: Compiler, *args: Any, **kwargs: Any) -> None:
    _prev_compiler_init_patch24(self, *args, **kwargs)
    self.key_events: List[EventDecl] = []

Compiler.__init__ = _compiler_init_patch24  # type: ignore[method-assign]

_prev_compiler_analyze_patch24 = Compiler.analyze

def _compiler_analyze_patch24(self: Compiler) -> None:
    # Older analyze() ignores `on key`; temporarily remove key events so it does
    # not treat a keyboard-only project as a blank library, then add them back.
    key_events = [stmt for stmt in self.program.body if isinstance(stmt, EventDecl) and stmt.kind == "key"]
    if key_events:
        filtered = Program([stmt for stmt in self.program.body if not (isinstance(stmt, EventDecl) and stmt.kind == "key")])
        old_program = self.program
        old_allow = self.allow_library
        self.program = filtered
        self.allow_library = True
        try:
            _prev_compiler_analyze_patch24(self)
        finally:
            self.program = old_program
            self.allow_library = old_allow
        self.key_events = key_events
        # Walk key event bodies once, so variables/lists/procedure calls used only
        # from a key handler still get ids and return helpers.
        for ev in key_events:
            for stmt in ev.body:
                try:
                    self.b.compile_stmt(stmt)  # preflight catches obvious unsupported code
                except Exception:
                    # Do not keep preflight blocks; actual compile below emits real blocks.
                    self.b.blocks.clear()
                    raise
                self.b.blocks.clear()
        return
    _prev_compiler_analyze_patch24(self)

Compiler.analyze = _compiler_analyze_patch24  # type: ignore[method-assign]


def _compile_key_event_patch24(builder: ScratchBuilder, ev: EventDecl, *, x: int = 80, y: int = 700) -> None:
    key = _sbg_normalize_key_name_patch24(ev.value or "any")
    hat = builder.add_block("event_whenkeypressed", topLevel=True, x=x, y=y, fields={"KEY_OPTION": [key, None]})
    first = builder.compile_statement_chain(ev.body)
    builder.blocks[hat]["next"] = first
    if first:
        builder.blocks[first]["parent"] = hat

_prev_compiler_compile_patch24 = Compiler.compile

def _compiler_compile_patch24(self: Compiler) -> Dict[str, Any]:
    project = _prev_compiler_compile_patch24(self)
    # If the active compile path was the normal Stage compiler, key events are in self.
    if getattr(self, "key_events", None):
        for idx, ev in enumerate(self.key_events):
            _compile_key_event_patch24(self.b, ev, x=80, y=700 + idx * 260)
        # Re-export mutated Stage blocks after adding key hats.
        if project.get("targets"):
            project["targets"][0]["blocks"] = self.b.blocks
    project.setdefault("meta", {})["agent"] = f"StageBG/SBG {VERSION}"
    project.setdefault("meta", {})["stagebgPatch24"] = "Keyboard support: on key, keyPressed(), keyboard.pressed()."
    return project

Compiler.compile = _compiler_compile_patch24  # type: ignore[method-assign]

# Sprite-local key hats.
_prev_sprite_init_patch24 = SpriteTargetCompiler.__init__

def _sprite_init_patch24(self: SpriteTargetCompiler, *args: Any, **kwargs: Any) -> None:
    _prev_sprite_init_patch24(self, *args, **kwargs)
    self.key_events: List[EventDecl] = []

SpriteTargetCompiler.__init__ = _sprite_init_patch24  # type: ignore[method-assign]

_prev_sprite_analyze_patch24 = SpriteTargetCompiler.analyze

def _sprite_analyze_patch24(self: SpriteTargetCompiler) -> None:
    key_events = [stmt for stmt in self.body if isinstance(stmt, EventDecl) and stmt.kind == "key"]
    if key_events:
        old_body = self.body
        self.body = [stmt for stmt in self.body if not (isinstance(stmt, EventDecl) and stmt.kind == "key")]
        try:
            _prev_sprite_analyze_patch24(self)
        finally:
            self.body = old_body
        self.key_events = key_events
        return
    _prev_sprite_analyze_patch24(self)

SpriteTargetCompiler.analyze = _sprite_analyze_patch24  # type: ignore[method-assign]

_prev_sprite_compile_target_patch24 = SpriteTargetCompiler.compile_target

def _sprite_compile_target_patch24(self: SpriteTargetCompiler) -> Dict[str, Any]:
    target = _prev_sprite_compile_target_patch24(self)
    if getattr(self, "key_events", None):
        for idx, ev in enumerate(self.key_events):
            _compile_key_event_patch24(self.b, ev, x=80, y=700 + idx * 260)
        target["blocks"] = self.b.blocks
    return target

SpriteTargetCompiler.compile_target = _sprite_compile_target_patch24  # type: ignore[method-assign]

# Native runner is headless, but allow tests to simulate keys through an env var:
# SBG_KEYS="space,left arrow,a" python3 sbg_patch24.py run file.sbg --input go
_prev_runtime_call_patch24 = Runtime.call

def _runtime_call_patch24(self: Runtime, name: str, args: List[Any]) -> Any:
    if name == "keyPressed":
        key = _sbg_normalize_key_name_patch24(args[0] if args else "any")
        raw = os.environ.get("SBG_KEYS", "")
        pressed = {_sbg_normalize_key_name_patch24(x.strip()) for x in raw.split(",") if x.strip()}
        return bool(key == "any" and pressed) or key in pressed
    return _prev_runtime_call_patch24(self, name, args)

Runtime.call = _runtime_call_patch24  # type: ignore[method-assign]

if __name__ == "__main__":
    raise SystemExit(main())
