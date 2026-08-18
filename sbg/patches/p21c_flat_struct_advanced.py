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
    if name in {"len", "size"} and len(args) == 1:
        # CRITICAL FIX: For len()/size(), always count characters for strings
        # NEVER use heuristic tokenization, as strings like "a b" or "a,b" will be
        # incorrectly split. Strings are atomic values; only vector<T> deserialization
        # should use tokenization, and those come as lists, not strings.
        if isinstance(args[0], str):
            return len(args[0])
        elif isinstance(args[0], list):
            return len(args[0])
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
