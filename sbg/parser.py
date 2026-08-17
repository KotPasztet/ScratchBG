from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import globals as _g
from .errors import ImportSBGError, ParseError, attach_location
from .ast import (
    Token, Lexer, Program, ImportDecl, VarDecl, ListDecl, EventDecl, ProcDecl,
    BlockStmt, IfStmt, RepeatStmt, ForeverStmt, WhileStmt, ForStmt,
    ReturnStmt, BreakStmt, ContinueStmt, AssignStmt, ExprStmt,
    Literal, VarExpr, BinaryExpr, UnaryExpr, CallExpr, ArrayExpr,
)
from .globals import PACKAGE_MANIFEST, SBG_MODULES_DIR

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

_g.parse_source = parse_source
