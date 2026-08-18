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


