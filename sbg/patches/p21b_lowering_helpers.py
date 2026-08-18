
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

