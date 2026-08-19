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
    _has_real_init21 = False
    if self.match("="):
        init = _parser_parse_cpp_initializer_patch20(self)
        _has_real_init21 = True
    self.expect(";")

    if typ in _SBG_STRUCT_DEFS21:
        # `Edge e;` or `Edge e = other;`. Copy construction is represented
        # as a struct variable declaration plus per-field assignments emitted
        # by patch 25 (see _sbg_expand_struct_var21).
        return self.loc(StructVarDecl(typ, name, init if _has_real_init21 else None), start_token)

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
        # BUGS_REPORT #6: keep the non-array initializer (e.g. a proc call)
        # so the post-parse type check can diagnose mismatches instead of
        # silently dropping it.
        if _has_real_init21:
            setattr(node, "sbg_init", init)
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
