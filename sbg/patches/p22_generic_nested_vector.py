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


