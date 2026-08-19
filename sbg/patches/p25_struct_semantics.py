# Patch 25: struct value semantics for plain (non-nested) vector<Struct> and
# struct copies (BUGS_REPORT #1 and #7).
#
# A `vector<Item> items;` declared at any scope is represented in Scratch as a
# SoA (struct-of-arrays): one hidden list `items.<field>` per struct field, plus
# the original `items` list which only tracks the element count (a marker 0 is
# pushed per element so `items.size()` keeps working).
#
# Lowerings performed AFTER the whole patch chain has parsed the program
# (names are final at that point, including p15 mangling of vector names --
# struct var names are never mangled because the mangler does not know
# StructVarDecl, which keeps references consistent):
#   * `items.at(i).f`      -> item(i, items.f)              (at() is 1-based)
#   * `items[i].f`         -> item(i + 1, items.f)           ([] is 0-based)
#   * `items.at(i).f = x`  -> setItem(items.f, i, x)
#   * `Item b = a;`        -> per-field `b.f = a.f`
#   * `Item b = items.at(i)` -> per-field `b.f = item(i, items.f)`
#   * `b = items.at(i);`   -> per-field copies (struct-var assignment)
#   * `items.push_back(a)` -> per-field `push_back(items.f, a.f)` + marker
#
# All emitted nodes are ordinary builtins (item/setItem/push_back/AssignStmt)
# already handled by both the native Runtime and the Scratch builder, so no
# new runtime or compiler support is required.

_SBG_VECSTRUCTS_P25: Dict[str, str] = {}    # vector name -> struct type name
_SBG_STRUCTVARS_P25: Dict[str, str] = {}    # struct var name -> struct type name


def _sbg_match_at_pattern25(e: Any) -> Optional[Tuple[str, Any, bool]]:
    """Match `vec.at(idx)` / `vec[idx]` where vec is a plain vector<Struct>."""
    if not (isinstance(e, CallExpr) and len(e.args) == 2 and isinstance(e.args[0], VarExpr)):
        return None
    v = e.args[0].name
    if v not in _SBG_VECSTRUCTS_P25 or v in _SBG_FLAT_VECTOR_TYPES21:
        return None
    if e.callee == "at":
        return (v, e.args[1], False)          # at() is 1-based
    if e.callee == "__index0_ref":
        return (v, e.args[1], True)           # [] is 0-based
    return None


def _sbg_vecstruct_field_read25(v: str, field: str, idx: Any, zero_based: bool) -> CallExpr:
    i = BinaryExpr(idx, "+", Literal(1)) if zero_based else idx
    return CallExpr("item", [VarExpr(f"{v}.{field}"), i])


def _sbg_vecstruct_index_expr25(idx: Any, zero_based: bool) -> Any:
    return BinaryExpr(idx, "+", Literal(1)) if zero_based else idx


# --- hook 1: record struct vars + emit copy semantics during p21a lowering ----

_old_expand_struct_var25 = _sbg_expand_struct_var21


def _sbg_expand_struct_var21(stmt: StructVarDecl) -> List[Any]:  # type: ignore[no-redef]
    _SBG_STRUCTVARS_P25[stmt.name] = stmt.typ
    out = _old_expand_struct_var25(stmt)
    init = getattr(stmt, "init", None)
    if init is None:
        return out
    fields = _SBG_STRUCT_DEFS21.get(stmt.typ, [])
    if isinstance(init, VarExpr) and init.name in _SBG_STRUCTVARS_P25:
        # BUGS_REPORT #1: `Item b = a;` -> field-by-field copy.
        for _ftyp, fn in fields:
            out.append(AssignStmt(f"{stmt.name}.{fn}", "=", VarExpr(f"{init.name}.{fn}")))
    elif isinstance(init, CallExpr):
        m = _sbg_match_at_pattern25(init)
        if m:
            v, idx, zb = m
            # `Item b = items.at(i);` -> per-field read from the SoA lists.
            for _ftyp, fn in fields:
                out.append(AssignStmt(f"{stmt.name}.{fn}", "=", _sbg_vecstruct_field_read25(v, fn, idx, zb)))
    return out


# --- hook 2: register plain vector<Struct> decls + create the SoA field lists --

_old_lower_structs_body25 = _sbg_lower_structs_body21


def _sbg_lower_structs_body21(body: List[Any]) -> List[Any]:  # type: ignore[no-redef]
    for st in body:
        if isinstance(st, ListDecl):
            t = getattr(st, "sbg_type", None)
            if t and _sbg_is_vector21(t) and not _sbg_is_nested_vector21(t):
                s = _sbg_is_vector_of_struct21(t)
                if s and st.name not in _SBG_FLAT_VECTOR_TYPES21:
                    _SBG_VECSTRUCTS_P25[st.name] = s
    out = _old_lower_structs_body25(body)
    # Insert one hidden list per struct field right after the vector decl.
    res: List[Any] = []
    for st in out:
        res.append(st)
        if isinstance(st, ListDecl) and st.name in _SBG_VECSTRUCTS_P25:
            s = _SBG_VECSTRUCTS_P25[st.name]
            for _ftyp, fn in _SBG_STRUCT_DEFS21.get(s, []):
                res.append(ListDecl(f"{st.name}.{fn}", []))
    return res

# --- hook 3: post-parse rewrite of remaining expression/statement patterns ----

def _sbg_rw_expr25(e: Any) -> Any:
    if e is None or isinstance(e, (Literal, VarExpr)):
        return e
    if isinstance(e, BinaryExpr):
        e.left = _sbg_rw_expr25(e.left)
        e.right = _sbg_rw_expr25(e.right)
        return e
    if isinstance(e, UnaryExpr):
        e.expr = _sbg_rw_expr25(e.expr)
        return e
    if isinstance(e, ArrayExpr):
        e.items = [_sbg_rw_expr25(x) for x in e.items]
        return e
    if isinstance(e, CallExpr):
        e.args = [_sbg_rw_expr25(a) for a in e.args]
        if e.callee == "__field_ref" and len(e.args) == 2 and isinstance(e.args[1], Literal):
            m = _sbg_match_at_pattern25(e.args[0])
            if m:
                v, idx, zb = m
                return _sbg_vecstruct_field_read25(v, str(e.args[1].value), idx, zb)
        return e
    return e


def _sbg_rw_body25(body: List[Any]) -> List[Any]:
    out: List[Any] = []
    for st in body:
        r = _sbg_rw_stmt25(st)
        if isinstance(r, list):
            out.extend(r)
        else:
            out.append(r)
    return out


def _sbg_rw_stmt25(st: Any) -> Any:
    if isinstance(st, ExprStmt):
        st.expr = _sbg_rw_expr25(st.expr)
        e = st.expr
        if (isinstance(e, CallExpr) and e.callee in ("push_back", "push", "add")
                and len(e.args) == 2 and isinstance(e.args[0], VarExpr)
                and e.args[0].name in _SBG_VECSTRUCTS_P25
                and isinstance(e.args[1], VarExpr)
                and e.args[1].name in _SBG_STRUCTVARS_P25):
            v = e.args[0].name
            a = e.args[1].name
            fields = _SBG_STRUCT_DEFS21.get(_SBG_VECSTRUCTS_P25[v], [])
            seq: List[Any] = [
                ExprStmt(CallExpr("push_back", [VarExpr(f"{v}.{fn}"), VarExpr(f"{a}.{fn}")]))
                for _ftyp, fn in fields
            ]
            # marker keeps `items.size()` / `items.empty()` in sync
            seq.append(ExprStmt(CallExpr("push_back", [VarExpr(v), Literal(0)])))
            return seq
        return st
    if isinstance(st, VarDecl):
        st.expr = _sbg_rw_expr25(st.expr)
        return st
    if isinstance(st, ListDecl):
        st.items = [_sbg_rw_expr25(x) for x in st.items]
        return st
    if isinstance(st, AssignStmt):
        if st.name in _SBG_STRUCTVARS_P25 and st.op == "=":
            if isinstance(st.expr, VarExpr) and st.expr.name in _SBG_STRUCTVARS_P25:
                # BUGS_REPORT #1: `b = a;` after declaration -> field-by-field copy.
                return [
                    AssignStmt(f"{st.name}.{fn}", "=", VarExpr(f"{st.expr.name}.{fn}"))
                    for _ftyp, fn in _SBG_STRUCT_DEFS21.get(_SBG_STRUCTVARS_P25[st.name], [])
                ]
            if isinstance(st.expr, CallExpr):
                m = _sbg_match_at_pattern25(st.expr)
                if m:
                    v, idx, zb = m
                    return [
                        AssignStmt(f"{st.name}.{fn}", "=", _sbg_vecstruct_field_read25(v, fn, idx, zb))
                        for _ftyp, fn in _SBG_STRUCT_DEFS21.get(_SBG_VECSTRUCTS_P25[v], [])
                    ]
        st.expr = _sbg_rw_expr25(st.expr)
        return st
    if isinstance(st, LValueAssignStmt):
        t = st.target
        if (isinstance(t, CallExpr) and t.callee == "__field_ref"
                and len(t.args) == 2 and isinstance(t.args[1], Literal)):
            m = _sbg_match_at_pattern25(t.args[0])
            if m:
                v, idx, zb = m
                field = str(t.args[1].value)
                st.expr = _sbg_rw_expr25(st.expr)
                old = _sbg_vecstruct_field_read25(v, field, idx, zb)
                val = st.expr if st.op == "=" else BinaryExpr(old, st.op[:-1], st.expr)
                return ExprStmt(CallExpr(
                    "setItem",
                    [VarExpr(f"{v}.{field}"), _sbg_vecstruct_index_expr25(idx, zb), val],
                ))
        st.target = _sbg_rw_expr25(st.target)
        st.expr = _sbg_rw_expr25(st.expr)
        return st
    if isinstance(st, ReturnStmt):
        st.expr = _sbg_rw_expr25(st.expr)
        return st
    if isinstance(st, IfStmt):
        st.cond = _sbg_rw_expr25(st.cond)
        st.then_body = _sbg_rw_body25(st.then_body)
        if st.else_body is not None:
            st.else_body = _sbg_rw_body25(st.else_body)
        return st
    if isinstance(st, (RepeatStmt, ForeverStmt, WhileStmt)):
        if isinstance(st, RepeatStmt):
            st.count = _sbg_rw_expr25(st.count)
        if isinstance(st, WhileStmt):
            st.cond = _sbg_rw_expr25(st.cond)
        st.body = _sbg_rw_body25(st.body)
        return st
    if isinstance(st, ForStmt):
        st.init = _sbg_rw_stmt25(st.init) if st.init is not None else None
        st.cond = _sbg_rw_expr25(st.cond)
        st.update = _sbg_rw_stmt25(st.update) if st.update is not None else None
        st.body = _sbg_rw_body25(st.body)
        return st
    if isinstance(st, BlockStmt):
        st.body = _sbg_rw_body25(st.body)
        return st
    if isinstance(st, ProcDecl):
        st.body = _sbg_rw_body25(st.body)
        return st
    if isinstance(st, EventDecl):
        st.body = _sbg_rw_body25(st.body)
        return st
    if isinstance(st, TargetDecl):
        st.body = _sbg_rw_body25(st.body)
        return st
    return st


_old_parse_source_patch25 = parse_source


def parse_source(text: str, filename: str = "<source>") -> Program:  # type: ignore[no-redef]
    _SBG_VECSTRUCTS_P25.clear()
    _SBG_STRUCTVARS_P25.clear()
    program = _old_parse_source_patch25(text, filename)
    program.body = _sbg_rw_body25(program.body)
    return program


_g.parse_source = parse_source
