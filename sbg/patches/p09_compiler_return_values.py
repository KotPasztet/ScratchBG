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
            temp_var = VarExpr(temp_name)
            # Preserve "boolean-shaped" info across lowering: a user/package
            # .sbg proc that is recognized as boolean by is_boolean_expr on the
            # ORIGINAL (pre-lowering) CallExpr must still produce a [2, ...]
            # (boolean) CONDITION input after being lowered to a temp-var
            # reporter, or it silently degrades to [1, ...] (plain value) and
            # produces an unimportable/incorrect .sb3 for callers like
            # `if (someBoolProc(x)) {...}`. See kontekst.md ("bool01" section)
            # for the experiment that found this. We tag the temp VarExpr so
            # is_boolean_expr can still recognize it after lowering.
            try:
                was_boolean = self.is_boolean_expr(expr)
            except Exception:
                was_boolean = False
            if was_boolean:
                temp_var._sbg_bool_shaped = True  # type: ignore[attr-defined]
            return prelude, temp_var
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


