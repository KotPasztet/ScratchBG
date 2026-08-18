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
# ("rangeLen" doesn't actually touch a list -- it was misplaced in this group.
# Removed from here and migrated to a real `.sbg proc` (packages/std/math.sbg)
# in this session: its compile_call_expr branch below referenced a builtin
# named "max" that was never actually implemented anywhere in this codebase,
# so `rangeLen(...)` used in expression position had been silently broken for
# `sbg.py compile` this whole time -- not covered by regress.sh/examples, so
# nobody noticed. The runtime/interpreter branch (Runtime.call, below) used
# real Python max() and worked fine, so this was another compile/run
# behavioral split like str/to_string and num/stod/stoi earlier this session.)
BUILTIN_STMT_NAMES.update({
    "reverseList", "sortAsc", "sortDesc", "lowerBoundTo", "upperBoundTo", "binarySearchTo",
})
BUILTIN_NAMES = BUILTIN_EXPR_NAMES | BUILTIN_STMT_NAMES

_old_compile_call_expr_patch17 = ScratchBuilder.compile_call_expr


def _compile_call_expr_patch17(self: ScratchBuilder, expr: CallExpr, parent: Optional[str]) -> str:
    name, a = expr.callee, expr.args
    # "rangeLen" removed -- real `.sbg proc` in packages/std/math.sbg now
    # (was broken here anyway, see note above BUILTIN_STMT_NAMES.update).
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



