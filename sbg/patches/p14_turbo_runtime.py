# =============================================================================
# Patch 14: default Turbo/Warp runtime + delta-time-safe standard loop helpers
# =============================================================================

VERSION = "0.9.0-patch24-keyboard"

# Vanilla Scratch cannot store the editor's Shift-click Turbo Mode inside .sb3.
# StageBG therefore treats "turbo" as a compiler/runtime policy:
#   - every generated custom block is warp=true by default;
#   - Action(Input) runs as a warp custom block;
#   - delta-time bookkeeping is done with direct Scratch variable blocks, not waits;
#   - native run defaults to the same zero-screen-refresh semantics, while wait()
#     remains real unless the user explicitly passes --fast.

SBG_NOW_VAR = "__sbg_now"
SBG_LAST_VAR = "__sbg_last"
SBG_RAW_DT_VAR = "__sbg_raw_dt"
SBG_DT_VAR = "__sbg_dt"
SBG_DT_SCALE_VAR = "__sbg_dt_scale"
SBG_DT_CAP_VAR = "__sbg_dt_cap"
SBG_FIXED_DT_VAR = "__sbg_fixed_dt"
SBG_FRAME_VAR = "__sbg_frame"
SBG_FPS_VAR = "__sbg_fps"
SBG_TURBO_VAR = "__sbg_turbo"

SBG_DELTA_VARS = {
    SBG_NOW_VAR: 0,
    SBG_LAST_VAR: 0,
    SBG_RAW_DT_VAR: 0,
    SBG_DT_VAR: 0,
    SBG_DT_SCALE_VAR: 1,
    SBG_DT_CAP_VAR: 0.25,
    SBG_FIXED_DT_VAR: 0,
    SBG_FRAME_VAR: 0,
    SBG_FPS_VAR: 0,
    SBG_TURBO_VAR: 1,
}

BUILTIN_EXPR_NAMES.update({
    "dt", "deltaTime", "rawDeltaTime", "fps", "frame", "timeSeconds", "isTurbo",
})
BUILTIN_STMT_NAMES.update({
    "tick", "frameStart", "updateDelta", "resetDelta", "setFixedDelta", "useRealDelta",
    "setDeltaScale", "setDeltaCap", "setTurbo", "turboOn", "turboOff",
})
BUILTIN_NAMES = BUILTIN_EXPR_NAMES | BUILTIN_STMT_NAMES


def _sbg_uses_delta_expr(expr: Any) -> bool:
    if isinstance(expr, CallExpr):
        if expr.callee in {"dt", "deltaTime", "rawDeltaTime", "fps", "frame", "timeSeconds"}:
            return True
        return any(_sbg_uses_delta_expr(a) for a in expr.args)
    if isinstance(expr, BinaryExpr):
        return _sbg_uses_delta_expr(expr.left) or _sbg_uses_delta_expr(expr.right)
    if isinstance(expr, UnaryExpr):
        return _sbg_uses_delta_expr(expr.expr)
    if isinstance(expr, ArrayExpr):
        return any(_sbg_uses_delta_expr(x) for x in expr.items)
    return False


def _sbg_uses_delta_stmt(stmt: Any) -> bool:
    if isinstance(stmt, VarDecl): return _sbg_uses_delta_expr(stmt.expr)
    if isinstance(stmt, ListDecl): return any(_sbg_uses_delta_expr(x) for x in stmt.items)
    if isinstance(stmt, AssignStmt): return _sbg_uses_delta_expr(stmt.expr)
    if isinstance(stmt, ExprStmt):
        return isinstance(stmt.expr, CallExpr) and (stmt.expr.callee in {"tick", "frameStart", "updateDelta"} or _sbg_uses_delta_expr(stmt.expr))
    if isinstance(stmt, IfStmt):
        return _sbg_uses_delta_expr(stmt.cond) or any(_sbg_uses_delta_stmt(s) for s in stmt.then_body) or any(_sbg_uses_delta_stmt(s) for s in (stmt.else_body or []))
    if isinstance(stmt, RepeatStmt): return _sbg_uses_delta_expr(stmt.count) or any(_sbg_uses_delta_stmt(s) for s in stmt.body)
    if isinstance(stmt, WhileStmt): return _sbg_uses_delta_expr(stmt.cond) or any(_sbg_uses_delta_stmt(s) for s in stmt.body)
    if isinstance(stmt, ForeverStmt): return any(_sbg_uses_delta_stmt(s) for s in stmt.body)
    if isinstance(stmt, ForStmt):
        return (stmt.init is not None and _sbg_uses_delta_stmt(stmt.init)) or (stmt.cond is not None and _sbg_uses_delta_expr(stmt.cond)) or (stmt.update is not None and _sbg_uses_delta_stmt(stmt.update)) or any(_sbg_uses_delta_stmt(s) for s in stmt.body)
    if isinstance(stmt, ReturnStmt): return stmt.expr is not None and _sbg_uses_delta_expr(stmt.expr)
    if isinstance(stmt, ProcDecl): return any(_sbg_uses_delta_stmt(s) for s in stmt.body)
    if isinstance(stmt, EventDecl): return any(_sbg_uses_delta_stmt(s) for s in stmt.body)
    return False


def _sbg_body_uses_delta(body: List[Any]) -> bool:
    return any(_sbg_uses_delta_stmt(s) for s in body)


def _sbg_set_var_block(self: ScratchBuilder, name: str, expr: Any) -> str:
    self.var_id(name)
    bid = self.add_block("data_setvariableto", fields={"VARIABLE": [name, self.var_id(name)]}, inputs={})
    self.blocks[bid]["inputs"]["VALUE"] = self.expr_input(expr, bid)
    return bid


def _sbg_if_block(self: ScratchBuilder, cond: Any, body_first: Optional[str]) -> str:
    bid = self.add_block("control_if", inputs={})
    self.blocks[bid]["inputs"]["CONDITION"] = self.expr_input(cond, bid)
    self.blocks[bid]["inputs"]["SUBSTACK"] = self.substack_input(body_first)
    self.set_parent(body_first, bid)
    return bid


def _sbg_if_else_block(self: ScratchBuilder, cond: Any, then_first: Optional[str], else_first: Optional[str]) -> str:
    bid = self.add_block("control_if_else", inputs={})
    self.blocks[bid]["inputs"]["CONDITION"] = self.expr_input(cond, bid)
    self.blocks[bid]["inputs"]["SUBSTACK"] = self.substack_input(then_first)
    self.blocks[bid]["inputs"]["SUBSTACK2"] = self.substack_input(else_first)
    self.set_parent(then_first, bid)
    self.set_parent(else_first, bid)
    return bid


def _sbg_compile_delta_reset(self: ScratchBuilder) -> Optional[str]:
    first: Optional[str] = None
    now_timer = CallExpr("timer", [])
    first = self.chain(first, _sbg_set_var_block(self, SBG_NOW_VAR, now_timer))
    first = self.chain(first, _sbg_set_var_block(self, SBG_LAST_VAR, VarExpr(SBG_NOW_VAR)))
    first = self.chain(first, _sbg_set_var_block(self, SBG_RAW_DT_VAR, Literal(0)))
    first = self.chain(first, _sbg_set_var_block(self, SBG_DT_VAR, Literal(0)))
    first = self.chain(first, _sbg_set_var_block(self, SBG_FPS_VAR, Literal(0)))
    first = self.chain(first, _sbg_set_var_block(self, SBG_FRAME_VAR, Literal(0)))
    first = self.chain(first, _sbg_set_var_block(self, SBG_TURBO_VAR, Literal(1)))
    return first


def _sbg_compile_delta_tick(self: ScratchBuilder) -> Optional[str]:
    first: Optional[str] = None
    # now = timer
    first = self.chain(first, _sbg_set_var_block(self, SBG_NOW_VAR, CallExpr("timer", [])))
    # raw_dt = now - last
    first = self.chain(first, _sbg_set_var_block(self, SBG_RAW_DT_VAR, BinaryExpr(VarExpr(SBG_NOW_VAR), "-", VarExpr(SBG_LAST_VAR))))
    # if raw_dt < 0: raw_dt = 0  (handles resetTimer() / project reload safely)
    first = self.chain(first, _sbg_if_block(self, BinaryExpr(VarExpr(SBG_RAW_DT_VAR), "<", Literal(0)), _sbg_set_var_block(self, SBG_RAW_DT_VAR, Literal(0))))
    # if dt_cap > 0 && raw_dt > dt_cap: raw_dt = dt_cap
    cap_cond = BinaryExpr(BinaryExpr(VarExpr(SBG_DT_CAP_VAR), ">", Literal(0)), "&&", BinaryExpr(VarExpr(SBG_RAW_DT_VAR), ">", VarExpr(SBG_DT_CAP_VAR)))
    first = self.chain(first, _sbg_if_block(self, cap_cond, _sbg_set_var_block(self, SBG_RAW_DT_VAR, VarExpr(SBG_DT_CAP_VAR))))
    # if fixed_dt > 0: raw_dt = fixed_dt
    fixed_cond = BinaryExpr(VarExpr(SBG_FIXED_DT_VAR), ">", Literal(0))
    first = self.chain(first, _sbg_if_block(self, fixed_cond, _sbg_set_var_block(self, SBG_RAW_DT_VAR, VarExpr(SBG_FIXED_DT_VAR))))
    # dt = raw_dt * scale
    first = self.chain(first, _sbg_set_var_block(self, SBG_DT_VAR, BinaryExpr(VarExpr(SBG_RAW_DT_VAR), "*", VarExpr(SBG_DT_SCALE_VAR))))
    # last = now
    first = self.chain(first, _sbg_set_var_block(self, SBG_LAST_VAR, VarExpr(SBG_NOW_VAR)))
    # frame += 1
    first = self.chain(first, _sbg_set_var_block(self, SBG_FRAME_VAR, BinaryExpr(VarExpr(SBG_FRAME_VAR), "+", Literal(1))))
    # fps = raw_dt > 0 ? 1/raw_dt : 0
    fps_then = _sbg_set_var_block(self, SBG_FPS_VAR, BinaryExpr(Literal(1), "/", VarExpr(SBG_RAW_DT_VAR)))
    fps_else = _sbg_set_var_block(self, SBG_FPS_VAR, Literal(0))
    first = self.chain(first, _sbg_if_else_block(self, BinaryExpr(VarExpr(SBG_RAW_DT_VAR), ">", Literal(0)), fps_then, fps_else))
    return first


_old_compile_call_expr_patch14 = ScratchBuilder.compile_call_expr

def _compile_call_expr_patch14(self: ScratchBuilder, expr: CallExpr, parent: Optional[str]) -> str:
    name, a = expr.callee, expr.args
    reporter_var = {
        "dt": SBG_DT_VAR,
        "deltaTime": SBG_DT_VAR,
        "rawDeltaTime": SBG_RAW_DT_VAR,
        "fps": SBG_FPS_VAR,
        "frame": SBG_FRAME_VAR,
        "isTurbo": SBG_TURBO_VAR,
    }.get(name)
    if reporter_var is not None:
        self.need_args(name, a, 0)
        self.var_id(reporter_var)
        return self.add_block("data_variable", parent=parent, fields={"VARIABLE": [reporter_var, self.var_id(reporter_var)]})
    if name == "timeSeconds":
        self.need_args(name, a, 0)
        return self.add_block("sensing_timer", parent=parent)
    return _old_compile_call_expr_patch14(self, expr, parent)

ScratchBuilder.compile_call_expr = _compile_call_expr_patch14  # type: ignore[method-assign]


_old_compile_call_stmt_patch14 = ScratchBuilder.compile_call_stmt

def _compile_call_stmt_patch14(self: ScratchBuilder, expr: CallExpr) -> Optional[str]:
    name, a = expr.callee, expr.args
    if name in ("tick", "frameStart", "updateDelta"):
        self.need_args(name, a, 0)
        return _sbg_compile_delta_tick(self)
    if name == "resetDelta":
        self.need_args(name, a, 0)
        return _sbg_compile_delta_reset(self)
    if name == "setFixedDelta":
        self.need_args(name, a, 1)
        return _sbg_set_var_block(self, SBG_FIXED_DT_VAR, a[0])
    if name == "useRealDelta":
        self.need_args(name, a, 0)
        return _sbg_set_var_block(self, SBG_FIXED_DT_VAR, Literal(0))
    if name == "setDeltaScale":
        self.need_args(name, a, 1)
        return _sbg_set_var_block(self, SBG_DT_SCALE_VAR, a[0])
    if name == "setDeltaCap":
        self.need_args(name, a, 1)
        return _sbg_set_var_block(self, SBG_DT_CAP_VAR, a[0])
    if name == "setTurbo":
        self.need_args(name, a, 1)
        return _sbg_set_var_block(self, SBG_TURBO_VAR, a[0])
    if name == "turboOn":
        self.need_args(name, a, 0)
        return _sbg_set_var_block(self, SBG_TURBO_VAR, Literal(1))
    if name == "turboOff":
        self.need_args(name, a, 0)
        return _sbg_set_var_block(self, SBG_TURBO_VAR, Literal(0))
    return _old_compile_call_stmt_patch14(self, expr)

ScratchBuilder.compile_call_stmt = _compile_call_stmt_patch14  # type: ignore[method-assign]


_old_compile_stmt_patch14 = ScratchBuilder.compile_stmt

def _compile_stmt_patch14(self: ScratchBuilder, stmt: Any) -> Optional[str]:
    # Delta-time-safe loops: if a loop body reads dt()/fps()/frame(), update once
    # per loop iteration. This is explicit in generated Scratch blocks and uses no wait.
    if isinstance(stmt, ForeverStmt) and _sbg_body_uses_delta(stmt.body):
        return _old_compile_stmt_patch14(self, ForeverStmt([ExprStmt(CallExpr("tick", [])), *stmt.body]))
    if isinstance(stmt, WhileStmt) and (_sbg_uses_delta_expr(stmt.cond) or _sbg_body_uses_delta(stmt.body)):
        return _old_compile_stmt_patch14(self, WhileStmt(stmt.cond, [ExprStmt(CallExpr("tick", [])), *stmt.body]))
    if isinstance(stmt, RepeatStmt) and _sbg_body_uses_delta(stmt.body):
        return _old_compile_stmt_patch14(self, RepeatStmt(stmt.count, [ExprStmt(CallExpr("tick", [])), *stmt.body]))
    if isinstance(stmt, ForStmt) and _sbg_body_uses_delta(stmt.body):
        return _old_compile_stmt_patch14(self, ForStmt(stmt.init, stmt.cond, stmt.update, [ExprStmt(CallExpr("tick", [])), *stmt.body]))
    return _old_compile_stmt_patch14(self, stmt)

ScratchBuilder.compile_stmt = _compile_stmt_patch14  # type: ignore[method-assign]


_old_compiler_analyze_patch14 = Compiler.analyze

def _compiler_analyze_patch14(self: Compiler) -> None:
    _old_compiler_analyze_patch14(self)
    # Allocate runtime variables even when dt() appears only inside lowered code.
    # They are ordinary Scratch variables, so this is vanilla and cheap.
    for name, value in SBG_DELTA_VARS.items():
        self.b.var_id(name)
        self.init_values.setdefault(name, value)

Compiler.analyze = _compiler_analyze_patch14  # type: ignore[method-assign]


_old_sprite_analyze_patch14 = SpriteTargetCompiler.analyze

def _sprite_analyze_patch14(self: SpriteTargetCompiler) -> None:
    _old_sprite_analyze_patch14(self)
    for name, value in SBG_DELTA_VARS.items():
        self.b.var_id(name)
        self.init_values.setdefault(name, value)

SpriteTargetCompiler.analyze = _sprite_analyze_patch14  # type: ignore[method-assign]


# Rebuild the terminal scripts with a real delta reset and an automatic frame tick
# at the start of Action(Input). This keeps native run and Scratch output aligned.
def _compiler_compile_console_flag_loop_patch14(self: Compiler) -> None:
    assert self.action_argid is not None
    has_action_return = _sbg_action_entries_have_return(self.action_entries)

    hat = self.b.add_block("event_whenflagclicked", topLevel=True, x=536, y=455)
    reset = _sbg_compile_delta_reset(self.b)
    forever = self.b.add_block("control_forever", inputs={})
    ask = self.b.add_block("sensing_askandwait", parent=forever, inputs={
        "QUESTION": [1, [10, ">"]]
    })
    answer = self.b.add_block("sensing_answer")

    echo = _builder_make_log_to_terminal(self.b, CallExpr("join", [Literal("> "), CallExpr("answer", [])]))
    call = self.b.add_block("procedures_call", inputs={}, mutation={
        "tagName": "mutation",
        "children": [],
        "proccode": "Action %s",
        "argumentids": json.dumps([self.action_argid]),
        "warp": "true",
    })
    self.b.blocks[call]["inputs"][self.action_argid] = [3, answer, [10, ""]]
    self.b.blocks[answer]["parent"] = call

    self.b.blocks[hat]["next"] = reset or forever
    if reset:
        # Append the forever loop after the reset chain.
        last = reset
        while self.b.blocks[last].get("next"):
            last = self.b.blocks[last]["next"]
        self.b.blocks[last]["next"] = forever
        self.b.blocks[forever]["parent"] = last
    else:
        self.b.blocks[forever]["parent"] = hat
    self.b.blocks[forever]["inputs"]["SUBSTACK"] = [2, ask]
    self.b.blocks[ask]["next"] = echo
    self.b.blocks[echo]["parent"] = ask
    self.b.blocks[echo]["next"] = call
    self.b.blocks[call]["parent"] = echo

    if has_action_return:
        cond = BinaryExpr(VarExpr(ACTION_RETURN_FLAG), "==", Literal(1))
        if_ret = self.b.add_block("control_if", parent=call, inputs={})
        self.b.blocks[if_ret]["inputs"]["CONDITION"] = self.b.expr_input(cond, if_ret)
        ret_log = _builder_make_log_to_terminal(self.b, CallExpr("join", [Literal("=> "), VarExpr(ACTION_RETURN_VAR)]))
        self.b.blocks[if_ret]["inputs"]["SUBSTACK"] = self.b.substack_input(ret_log)
        self.b.set_parent(ret_log, if_ret)
        self.b.blocks[call]["next"] = if_ret

Compiler.compile_console_flag_loop = _compiler_compile_console_flag_loop_patch14  # type: ignore[method-assign]


def _compiler_compile_console_action_definition_patch14(self: Compiler) -> None:
    assert self.action_argid is not None
    display_param = self.action_entries[0][0] if self.action_entries else "Input"
    has_action_return = _sbg_action_entries_have_return(self.action_entries)

    def_id = self.b.add_block("procedures_definition", topLevel=True, x=849, y=450)
    proto_id = self.b.uid()
    reporter_id = self.b.add_block(
        "argument_reporter_string_number",
        parent=proto_id,
        fields={"VALUE": [display_param, None]},
    )
    self.b.blocks[def_id]["inputs"] = {"custom_block": [2, proto_id]}
    self.b.blocks[proto_id] = {
        "opcode": "procedures_prototype",
        "next": None,
        "parent": def_id,
        "inputs": {self.action_argid: [2, reporter_id]},
        "fields": {},
        "shadow": False,
        "topLevel": False,
        "mutation": {
            "tagName": "mutation",
            "children": [],
            "proccode": "Action %s",
            "argumentids": json.dumps([self.action_argid]),
            "argumentnames": json.dumps([display_param]),
            "argumentdefaults": json.dumps([""]),
            "warp": "true",
        },
    }

    first: Optional[str] = _sbg_compile_delta_tick(self.b)
    saved_params = dict(self.b.current_proc_params)
    saved_ret_var = getattr(self.b, "current_return_var", None)
    saved_ret_flag = getattr(self.b, "current_return_flag", None)

    if has_action_return:
        self.b.current_return_var = ACTION_RETURN_VAR
        self.b.current_return_flag = ACTION_RETURN_FLAG
        first = self.b.chain(first, _builder_make_set_var(self.b, ACTION_RETURN_FLAG, Literal(0)))
        first = self.b.chain(first, _builder_make_set_var(self.b, ACTION_RETURN_VAR, Literal("")))
    else:
        self.b.current_return_var = None
        self.b.current_return_flag = None

    for param, body in self.action_entries:
        self.b.current_proc_params = {param: self.action_argid}
        part = self.b.compile_statement_chain(body)
        first = self.b.chain(first, part)

    self.b.current_proc_params = saved_params
    self.b.current_return_var = saved_ret_var
    self.b.current_return_flag = saved_ret_flag

    self.b.blocks[def_id]["next"] = first
    if first:
        self.b.blocks[first]["parent"] = def_id

Compiler.compile_console_action_definition = _compiler_compile_console_action_definition_patch14  # type: ignore[method-assign]


_old_sprite_compile_flag_patch14 = SpriteTargetCompiler.compile_flag_event

def _sprite_compile_flag_event_patch14(self: SpriteTargetCompiler, ev: EventDecl) -> None:
    # Sprite flag events get a cheap reset before user code. Loops using dt() are
    # handled by the compile_stmt loop injection above.
    hat = self.b.add_block("event_whenflagclicked", topLevel=True)
    reset = _sbg_compile_delta_reset(self.b)
    first = self.b.chain(reset, self.b.compile_statement_chain(ev.body))
    self.b.blocks[hat]["next"] = first
    if first:
        self.b.blocks[first]["parent"] = hat

SpriteTargetCompiler.compile_flag_event = _sprite_compile_flag_event_patch14  # type: ignore[method-assign]


_old_sprite_compile_message_patch14 = SpriteTargetCompiler.compile_message_event

def _sprite_compile_message_event_patch14(self: SpriteTargetCompiler, ev: EventDecl) -> None:
    msg = ev.value or ""
    hat = self.b.add_block("event_whenbroadcastreceived", topLevel=True,
                           fields={"BROADCAST_OPTION": [msg, self.b.broadcast_id(msg)]})
    tick = _sbg_compile_delta_tick(self.b)
    first = self.b.chain(tick, self.b.compile_statement_chain(ev.body))
    self.b.blocks[hat]["next"] = first
    if first:
        self.b.blocks[first]["parent"] = hat

SpriteTargetCompiler.compile_message_event = _sprite_compile_message_event_patch14  # type: ignore[method-assign]


# Native delta runtime ---------------------------------------------------------

def _runtime_ensure_delta_state(self: Runtime) -> None:
    for name, value in SBG_DELTA_VARS.items():
        self.vars.setdefault(name, value)
    if not hasattr(self, "_sbg_delta_last_monotonic"):
        self._sbg_delta_last_monotonic = time.monotonic()


def _runtime_reset_delta(self: Runtime) -> None:
    _runtime_ensure_delta_state(self)
    now = time.monotonic()
    self._sbg_delta_last_monotonic = now
    self.vars[SBG_NOW_VAR] = 0
    self.vars[SBG_LAST_VAR] = 0
    self.vars[SBG_RAW_DT_VAR] = 0
    self.vars[SBG_DT_VAR] = 0
    self.vars[SBG_FPS_VAR] = 0
    self.vars[SBG_FRAME_VAR] = 0
    self.vars[SBG_TURBO_VAR] = 1


def _runtime_update_delta(self: Runtime) -> float:
    _runtime_ensure_delta_state(self)
    now_abs = time.monotonic()
    last_abs = getattr(self, "_sbg_delta_last_monotonic", now_abs)
    raw = max(0.0, now_abs - last_abs)
    cap = float(self.vars.get(SBG_DT_CAP_VAR, 0.25) or 0)
    if cap > 0 and raw > cap:
        raw = cap
    fixed = float(self.vars.get(SBG_FIXED_DT_VAR, 0) or 0)
    if fixed > 0:
        raw = fixed
    scale = float(self.vars.get(SBG_DT_SCALE_VAR, 1) or 1)
    dt_val = raw * scale
    self._sbg_delta_last_monotonic = now_abs
    self.vars[SBG_NOW_VAR] = now_abs - self.timer_start
    self.vars[SBG_LAST_VAR] = self.vars[SBG_NOW_VAR]
    self.vars[SBG_RAW_DT_VAR] = raw
    self.vars[SBG_DT_VAR] = dt_val
    self.vars[SBG_FRAME_VAR] = float(self.vars.get(SBG_FRAME_VAR, 0) or 0) + 1
    self.vars[SBG_FPS_VAR] = (1 / raw) if raw > 0 else 0
    self.vars[SBG_TURBO_VAR] = 1
    return dt_val


_old_runtime_prepare_patch14 = Runtime.prepare_scratch_console

def _runtime_prepare_patch14(self: Runtime) -> None:
    _old_runtime_prepare_patch14(self)
    _runtime_reset_delta(self)

Runtime.prepare_scratch_console = _runtime_prepare_patch14  # type: ignore[method-assign]


_old_runtime_run_action_patch14 = Runtime.run_action

def _runtime_run_action_patch14(self: Runtime, input_value: str = "") -> Any:
    _runtime_update_delta(self)
    return _old_runtime_run_action_patch14(self, input_value)

Runtime.run_action = _runtime_run_action_patch14  # type: ignore[method-assign]


_old_runtime_call_patch14 = Runtime.call

def _runtime_call_patch14(self: Runtime, name: str, args: List[Any]) -> Any:
    _runtime_ensure_delta_state(self)
    if name in ("tick", "frameStart", "updateDelta"):
        return _runtime_update_delta(self)
    if name == "resetDelta":
        _runtime_reset_delta(self); return None
    if name in ("dt", "deltaTime"):
        return self.vars.get(SBG_DT_VAR, 0)
    if name == "rawDeltaTime":
        return self.vars.get(SBG_RAW_DT_VAR, 0)
    if name == "fps":
        return self.vars.get(SBG_FPS_VAR, 0)
    if name == "frame":
        return self.vars.get(SBG_FRAME_VAR, 0)
    if name == "timeSeconds":
        return time.monotonic() - self.timer_start
    if name == "isTurbo":
        return self.vars.get(SBG_TURBO_VAR, 1)
    if name == "setFixedDelta":
        self.vars[SBG_FIXED_DT_VAR] = float(args[0]); return None
    if name == "useRealDelta":
        self.vars[SBG_FIXED_DT_VAR] = 0; return None
    if name == "setDeltaScale":
        self.vars[SBG_DT_SCALE_VAR] = float(args[0]); return None
    if name == "setDeltaCap":
        self.vars[SBG_DT_CAP_VAR] = float(args[0]); return None
    if name == "setTurbo":
        self.vars[SBG_TURBO_VAR] = 1 if args and args[0] else 0; return None
    if name == "turboOn":
        self.vars[SBG_TURBO_VAR] = 1; return None
    if name == "turboOff":
        self.vars[SBG_TURBO_VAR] = 0; return None
    return _old_runtime_call_patch14(self, name, args)

Runtime.call = _runtime_call_patch14  # type: ignore[method-assign]


# Project-level turbo metadata + optional disabling of warp mutations.
def _sbg_project_set_warp(project: Dict[str, Any], enabled: bool = True) -> None:
    value = "true" if enabled else "false"
    for target in project.get("targets", []):
        for block in (target.get("blocks") or {}).values():
            if isinstance(block, dict):
                mut = block.get("mutation")
                if isinstance(mut, dict) and "warp" in mut:
                    mut["warp"] = value


_old_compiler_compile_patch14 = Compiler.compile

def _compiler_compile_patch14(self: Compiler) -> Dict[str, Any]:
    project = _old_compiler_compile_patch14(self)
    _sbg_project_set_warp(project, True)
    meta = project.setdefault("meta", {})
    meta["agent"] = f"StageBG/SBG {VERSION}"
    meta["stagebgTurbo"] = "warp-default"
    meta["stagebgDeltaTime"] = "timer-backed, capped, fixed-dt-capable"
    meta["stagebgVanillaScratch"] = True
    return project

Compiler.compile = _compiler_compile_patch14  # type: ignore[method-assign]


# Final patch14 CLI. Adds --no-turbo, but keeps turbo on by default.
def main(argv: Optional[List[str]] = None) -> int:  # type: ignore[no-redef]
    ap = argparse.ArgumentParser(prog="sbg", description="StageBG/SBG: professional text code -> vanilla Scratch .sb3 compiler")
    ap.add_argument("--version", action="version", version=f"SBG {VERSION}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    runp = sub.add_parser("run", help="run .sbg natively after verifying it can compile to Scratch")
    runp.add_argument("source")
    runp.add_argument("--fast", action="store_true", help="do not sleep on wait(); turbo mode itself stays on by default")
    runp.add_argument("--no-turbo", action="store_true", help="native compatibility flag: keep generated-style semantics but mark __sbg_turbo=0")
    runp.add_argument("--input", default="", help="input value for one-shot Action(Input) execution")
    runp.add_argument("--terminal", action="store_true", help="start an interactive console loop that mirrors the generated Scratch terminal")
    runp.add_argument("--once", action="store_true", help="run exactly one Action(Input) invocation and exit; default unless --terminal is used")
    runp.add_argument("--embed", action="append", default=[], help="embed text file at compile/run time: path[:virtual/name] or path=virtual/name")
    runp.add_argument("--embed-dir", action="append", default=[], help="embed every text file from a directory recursively")
    runp.add_argument("-O", "--opt-level", type=int, choices=[0, 1, 2, 3], default=0, dest="opt_level", help="optimization level 0-3 (0 = byte-identical to today)")
    runp.add_argument("--opt-terminal-batch", action="store_true", help="enable terminal-output batching (reserved; currently a no-op)")

    comp = sub.add_parser("compile", help="compile .sbg source into a vanilla Scratch .sb3 project")
    comp.add_argument("source")
    comp.add_argument("output")
    comp.add_argument("--allow-library", action="store_true", help="allow compiling a file with no on action/on flag/top-level code")
    comp.add_argument("--no-verify", action="store_true", help="skip generated .sb3 structural verification")
    comp.add_argument("--no-turbo", action="store_true", help="disable warp=true on generated custom blocks; default is turbo/warp on")
    comp.add_argument("--embed", action="append", default=[], help="embed text file into Scratch lists: path[:virtual/name] or path=virtual/name")
    comp.add_argument("--embed-dir", action="append", default=[], help="embed every text file from a directory recursively")
    comp.add_argument("-O", "--opt-level", type=int, choices=[0, 1, 2, 3], default=0, dest="opt_level", help="optimization level 0-3 (0 = byte-identical to today)")
    comp.add_argument("--opt-terminal-batch", action="store_true", help="enable terminal-output batching (reserved; currently a no-op)")

    insp = sub.add_parser("inspect", help="inspect an .sb3 file and print JSON stats")
    insp.add_argument("sb3")

    unp = sub.add_parser("unpack", help="unzip an .sb3 project into a directory")
    unp.add_argument("sb3")
    unp.add_argument("out_dir")

    pkg = sub.add_parser("pkg", help="manage SBG libraries/packages")
    pkg_sub = pkg.add_subparsers(dest="pkg_cmd", required=True)
    pkg_init = pkg_sub.add_parser("init", help="create sbgpkg.json and sbg_modules/")
    pkg_init.add_argument("--name", default=None)
    pkg_install = pkg_sub.add_parser("install", help="install a package from .sbg file, folder, URL, zip URL or registry name")
    pkg_install.add_argument("source", help="local .sbg/folder, URL, or package name when --registry is used")
    pkg_install.add_argument("--name", default=None, help="override installed package name")
    pkg_install.add_argument("--registry", default=None, help="registry JSON path/URL for named packages")
    pkg_sub.add_parser("list", help="list installed packages")
    pkg_remove = pkg_sub.add_parser("remove", help="remove an installed package")
    pkg_remove.add_argument("name")

    args = ap.parse_args(argv)
    source_text = ""
    fallback_filename = "<source>"
    try:
        if args.cmd == "run":
            fallback_filename = args.source
            source_text = Path(args.source).read_text(encoding="utf-8")
            program = parse_source(source_text, args.source)
            program = _program_with_embedded_files(program, args.source, embeds=args.embed, embed_dirs=args.embed_dir)
            if args.opt_level == 0:
                assert_scratch_compatible(program)
            else:
                project = compile_project_from_program(program, level=args.opt_level, terminal_batch=args.opt_terminal_batch)
                validate_scratch_project(project)
            rt = Runtime(program, fast=args.fast, filename=args.source, source_text=source_text)
            if args.no_turbo:
                rt.vars[SBG_TURBO_VAR] = 0
            if args.terminal:
                rt.run_scratch_terminal()
            else:
                rt.run_scratch_once(args.input)
            return 0
        if args.cmd == "compile":
            fallback_filename = args.source
            source_text = Path(args.source).read_text(encoding="utf-8")
            program = parse_source(source_text, args.source)
            program = _program_with_embedded_files(program, args.source, embeds=args.embed, embed_dirs=args.embed_dir)
            project = compile_project_from_program(
                program, level=args.opt_level, allow_library=args.allow_library,
                no_turbo=args.no_turbo, terminal_batch=args.opt_terminal_batch,
            )
            if args.no_turbo:
                project.setdefault("meta", {})["stagebgTurbo"] = "disabled-by-cli"
            write_sb3_project(project, args.output, verify=not args.no_verify)
            print(f"compiled: {args.output}")
            if not args.no_turbo:
                print("turbo: on (vanilla Scratch warp custom blocks; no TurboWarp dependency)")
            if args.allow_library:
                print("warning: compiled in --allow-library mode; Action(Input) may intentionally have no body")
            return 0
        if args.cmd == "inspect":
            print(json.dumps(inspect_sb3(args.sb3), ensure_ascii=False, indent=2))
            return 0
        if args.cmd == "unpack":
            unpack_sb3(args.sb3, args.out_dir)
            print(f"unpacked: {args.out_dir}")
            return 0
        if args.cmd == "pkg":
            root = Path.cwd()
            if args.pkg_cmd == "init":
                path = package_init(root, args.name)
                print(f"initialized: {path}")
                return 0
            if args.pkg_cmd == "install":
                result = install_from_source(args.source, root=root, name=args.name, registry=args.registry)
                print(f"installed: {result['name']} -> {result['path']} ({result['main']})")
                return 0
            if args.pkg_cmd == "list":
                rows = list_packages(root)
                if not rows:
                    print("no packages installed")
                else:
                    for row in rows:
                        status = "ok" if row["installed"] else "missing"
                        print(f"{row['name']}@{row['version']}  main={row['main']}  {status}")
                return 0
            if args.pkg_cmd == "remove":
                remove_package(root, args.name)
                print(f"removed: {args.name}")
                return 0
    except SBGError as e:
        print(format_diagnostic(e, source_text=source_text, fallback_filename=fallback_filename), file=sys.stderr)
        return 1
    except OSError as e:
        print(f"FileError: {e}", file=sys.stderr)
        return 1
    return 2



# Patch14b: allow built-in/local package folders, so examples and real projects can
# use `import "std";` directly without first copying std into sbg_modules/.
_old_import_package_roots_patch14 = ImportResolver.package_roots

def _import_package_roots_patch14(self: ImportResolver, base: Path) -> List[Path]:
    roots = list(_old_import_package_roots_patch14(self, base))
    cursor = self._safe_resolve(base if base.is_dir() else base.parent)
    extra: List[Path] = []
    for parent in [cursor, *cursor.parents, Path.cwd()]:
        extra.append(parent / "packages")
    out: List[Path] = []
    seen: set[Path] = set()
    for root in [*roots, *extra]:
        r = self._safe_resolve(root)
        if r not in seen:
            out.append(r)
            seen.add(r)
    return out

ImportResolver.package_roots = _import_package_roots_patch14  # type: ignore[method-assign]



