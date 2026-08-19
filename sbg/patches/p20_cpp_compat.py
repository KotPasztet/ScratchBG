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


# Stream syntax (`cin >> x`, `cout << x`) is gated on the iostream/bits/std
# library being imported, exactly like `std::` names are gated on `import
# "std"`: without it the parser would lower `cin >> x` to a call of an
# unknown proc cin_get().  C++ discipline: using a stream without
# #include <iostream> is a compile error, not a silent auto-import.
_STREAM_USE_RE = re.compile(r"\b(cin)\b\s*>>|\b(cout)\b\s*<<")
_IOSTREAM_IMPORT_RE = re.compile(r'import\s+"(?:iostream|bits|std)(?:\.sbg)?"')


def _sbg_cpp_check_stream_import(text: str, filename: str) -> None:
    """Raise CompileError when stream syntax is used with no iostream import.

    `text` must already be include-preprocessed (so `#include <iostream>`
    shows up as `import "iostream";` and `#include <bits/stdc++.h>` as
    `import "bits";`).  Line comments are ignored so that docs mentioning
    `cin >>` do not trip the check; block comments and string literals are
    not analyzed (documented limitation).
    """
    if _IOSTREAM_IMPORT_RE.search(text):
        return
    for lineno, line in enumerate(text.splitlines(), 1):
        code = line.split("//", 1)[0]
        m = _STREAM_USE_RE.search(code)
        if m:
            which = m.group(1) or m.group(2)
            raise CompileError(
                f"{filename}:{lineno}:{m.start() + 1}: `{which}` used without "
                "`#include <iostream>` (or `<bits/stdc++.h>`): add "
                '`#include <iostream>` / `import "iostream";` at the top of the file'
            )


def _sbg_cpp_preprocess_patch20(text: str) -> str:
    # Keep the patch18 include preprocessor, then normalize std:: names.  This is
    # deliberately not a C++ preprocessor; it is a surface-syntax adapter.
    # NOTE: stream syntax is NOT auto-imported anymore; using `cin >>` /
    # `cout <<` without importing iostream/bits/std is a CompileError (see
    # _sbg_cpp_check_stream_import, wired in _lexer_init_patch20 below).
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
    processed = _sbg_cpp_preprocess_patch20(text)
    _sbg_cpp_check_stream_import(processed, filename)
    return _old_lexer_init_patch18(self, processed, filename)

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
    # `cin >> a >> b;` lowers to `a = cin_get(); b = cin_get();` — a BlockStmt
    # of plain assignments, so everything downstream (runtime, Scratch builder,
    # IR, mangler) sees only ordinary proc calls from the iostream package.
    self.advance()  # cin
    stmts: List[Any] = []
    while self.match(">>"):
        if self.peek().kind != "IDENT":
            raise self.error("cin target must be an identifier")
        stmts.append(AssignStmt(self.advance().value, "=", CallExpr("cin_get", [])))
    self.expect(";")
    return self.loc(BlockStmt(stmts), start_token)


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


def _sbg_format_number(x: Any) -> str:
    """Format number for display: hide .0 for integer values, matching Scratch behavior.
    CRITICAL FIX: pkt 5 - ensure 610.0 displays as '610', not '610.0'."""
    if isinstance(x, bool):
        return str(x)  # bool before float, since bool is a subclass of int
    if isinstance(x, (int, float)):
        if isinstance(x, float) and x.is_integer():
            return str(int(x))
        return str(x)
    return str(x)


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
        text = "".join(_sbg_format_number(a) for a in args)
        # C++ cout may contain newlines; mirror it as separate terminal rows.
        rows = text.split("\n")
        for row in rows:
            if row != "":
                self.call("log", [row])
        return None
    if name == "println":
        return self.call("log", ["".join(_sbg_format_number(a) for a in args)])
    # `cin` is no longer native: patch20 lowers `cin >> x` to
    # `x = cin_get()` from packages/iostream (stdlib getinput algorithm).
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
    # `cin` is no longer a native builder construct: patch20 lowers
    # `cin >> x` to `x = cin_get()` (packages/iostream on top of std getinput).
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


