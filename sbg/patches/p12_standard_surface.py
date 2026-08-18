# =============================================================================
# Patch 12: bigger vanilla-Scratch standard surface, motion/looks/sensing,
# faster compiler returns, and vanilla-safe optimizations
# =============================================================================

# This patch intentionally stays inside vanilla Scratch opcodes. No TurboWarp-only
# extensions, custom JS, or VM hacks are emitted.

BUILTIN_EXPR_NAMES.update({
    # strings
    "letter", "containsText",
    # math ops supported by vanilla Scratch's operator_mathop
    "sin", "cos", "tan", "asin", "acos", "atan", "ln", "log10", "exp", "pow10",
    # sensing
    "mouseX", "mouseY", "mouseDown", "keyPressed", "current", "daysSince2000", "username", "loudness",
    "distanceTo", "touching",
    # sprite reporters
    "x", "y", "direction", "size", "costumeNumber", "costumeName", "backdropNumber", "backdropName",
})
BUILTIN_STMT_NAMES.update({
    # terminal/console
    "clearTerminal", "logMany",
    # sprite motion
    "setX", "setY", "changeX", "changeY", "goToXY", "goTo", "glideToXY",
    "move", "turnRight", "turnLeft", "pointDirection", "pointTo", "ifOnEdgeBounce", "setRotationStyle",
    # looks
    "say", "sayFor", "think", "thinkFor", "show", "hide", "setSize", "changeSize",
    "setCostume", "nextCostume", "setEffect", "changeEffect", "clearEffects",
    "layerFront", "layerBack", "goForwardLayers", "goBackwardLayers",
    # clones / control
    "createClone", "deleteThisClone", "stopAll", "stopThisScript", "stopOtherScripts",
})
BUILTIN_NAMES = BUILTIN_EXPR_NAMES | BUILTIN_STMT_NAMES

_SPRITE_ONLY_STMT = {
    "setX", "setY", "changeX", "changeY", "goToXY", "goTo", "glideToXY", "move", "turnRight", "turnLeft",
    "pointDirection", "pointTo", "ifOnEdgeBounce", "setRotationStyle", "say", "sayFor", "think", "thinkFor",
    "show", "hide", "setSize", "changeSize", "setCostume", "nextCostume", "setEffect", "changeEffect", "clearEffects",
    "layerFront", "layerBack", "goForwardLayers", "goBackwardLayers", "createClone", "deleteThisClone",
}
_SPRITE_ONLY_EXPR = {"x", "y", "direction", "size", "costumeNumber", "costumeName", "distanceTo", "touching"}


def _sbg_builder_target_kind(self: ScratchBuilder) -> str:
    return getattr(self, "target_kind", "stage")


def _sbg_require_sprite_target(self: ScratchBuilder, name: str) -> None:
    if _sbg_builder_target_kind(self) != "sprite":
        raise CompileError(f"{name}() is sprite-only in vanilla Scratch. Put this code inside `sprite Name {{ ... }}`.")


def _sbg_literal_string(expr: Any, what: str) -> str:
    if isinstance(expr, Literal):
        return str(expr.value)
    raise CompileError(f"{what} must be a string literal for vanilla Scratch output")


def _sbg_bool_expr_patch12(self: ScratchBuilder, expr: Any) -> bool:
    if _old_is_boolean_expr_patch12(self, expr):
        return True
    return isinstance(expr, CallExpr) and expr.callee in {"containsText", "mouseDown", "keyPressed", "touching"}

_old_is_boolean_expr_patch12 = ScratchBuilder.is_boolean_expr
ScratchBuilder.is_boolean_expr = _sbg_bool_expr_patch12  # type: ignore[method-assign]


def _sbg_constant_eval(expr: Any) -> Tuple[bool, Any]:
    """Small compile-time constant folder for vanilla-safe optimizations."""
    try:
        if isinstance(expr, Literal):
            return True, expr.value
        if isinstance(expr, UnaryExpr):
            ok, v = _sbg_constant_eval(expr.expr)
            if not ok: return False, None
            if expr.op == "-": return True, -float(v)
            if expr.op == "!": return True, not bool(v)
        if isinstance(expr, BinaryExpr):
            ok1, a = _sbg_constant_eval(expr.left)
            ok2, b = _sbg_constant_eval(expr.right)
            if not (ok1 and ok2): return False, None
            if expr.op == "+": return True, (str(a)+str(b)) if isinstance(a, str) or isinstance(b, str) else a + b
            if expr.op == "-": return True, float(a) - float(b)
            if expr.op == "*": return True, float(a) * float(b)
            if expr.op == "/": return True, float(a) / float(b)
            if expr.op == "%": return True, float(a) % float(b)
            if expr.op == "==": return True, a == b
            if expr.op == "!=": return True, a != b
            if expr.op == "<": return True, a < b
            if expr.op == "<=": return True, a <= b
            if expr.op == ">": return True, a > b
            if expr.op == ">=": return True, a >= b
            if expr.op == "&&": return True, bool(a) and bool(b)
            if expr.op == "||": return True, bool(a) or bool(b)
        if isinstance(expr, CallExpr):
            vals = []
            for a in expr.args:
                ok, v = _sbg_constant_eval(a)
                if not ok: return False, None
                vals.append(v)
            if expr.callee == "join": return True, "".join(str(v) for v in vals)
            if expr.callee == "len" and len(vals) == 1: return True, len(vals[0])
            if expr.callee == "letter" and len(vals) == 2: return True, str(vals[0])[max(0, int(vals[1])-1):max(0, int(vals[1]))]
            if expr.callee == "containsText" and len(vals) == 2: return True, str(vals[1]) in str(vals[0])
            if expr.callee == "round" and len(vals) == 1: return True, round(float(vals[0]))
            if expr.callee == "abs" and len(vals) == 1: return True, abs(float(vals[0]))
            if expr.callee == "floor" and len(vals) == 1: return True, math.floor(float(vals[0]))
            if expr.callee == "ceil" and len(vals) == 1: return True, math.ceil(float(vals[0]))
            if expr.callee == "sqrt" and len(vals) == 1: return True, math.sqrt(float(vals[0]))
            if expr.callee == "sin" and len(vals) == 1: return True, math.sin(math.radians(float(vals[0])))
            if expr.callee == "cos" and len(vals) == 1: return True, math.cos(math.radians(float(vals[0])))
            if expr.callee == "tan" and len(vals) == 1: return True, math.tan(math.radians(float(vals[0])))
            if expr.callee == "ln" and len(vals) == 1: return True, math.log(float(vals[0]))
            if expr.callee == "log10" and len(vals) == 1: return True, math.log10(float(vals[0]))
            if expr.callee == "exp" and len(vals) == 1: return True, math.exp(float(vals[0]))
            if expr.callee == "pow10" and len(vals) == 1: return True, 10 ** float(vals[0])
    except Exception:
        return False, None
    return False, None


_old_compile_expr_patch12 = ScratchBuilder.compile_expr

def _compile_expr_patch12(self: ScratchBuilder, expr: Any, parent: Optional[str] = None) -> str:
    ok, val = _sbg_constant_eval(expr)
    # Keep booleans as boolean reporter expressions because Scratch inputs expect
    # predicates in condition slots. Number/string constants can be primitive inputs.
    if ok and not isinstance(val, bool):
        return _old_compile_expr_patch12(self, Literal(val), parent)
    return _old_compile_expr_patch12(self, expr, parent)

ScratchBuilder.compile_expr = _compile_expr_patch12  # type: ignore[method-assign]


_old_compile_call_expr_patch12 = ScratchBuilder.compile_call_expr

def _compile_call_expr_patch12(self: ScratchBuilder, expr: CallExpr, parent: Optional[str]) -> str:
    name, a = expr.callee, expr.args
    if name == "letter":
        self.need_args(name, a, 2)
        bid = self.add_block("operator_letter_of", parent=parent, inputs={})
        self.blocks[bid]["inputs"]["STRING"] = self.expr_input(a[0], bid)
        self.blocks[bid]["inputs"]["LETTER"] = self.expr_input(a[1], bid)
        return bid
    if name == "containsText":
        self.need_args(name, a, 2)
        bid = self.add_block("operator_contains", parent=parent, inputs={})
        self.blocks[bid]["inputs"]["STRING1"] = self.expr_input(a[0], bid)
        self.blocks[bid]["inputs"]["STRING2"] = self.expr_input(a[1], bid)
        return bid
    if name in ("sin", "cos", "tan", "asin", "acos", "atan", "ln", "log10", "exp", "pow10"):
        self.need_args(name, a, 1)
        op = {"log10": "log", "exp": "e ^", "pow10": "10 ^"}.get(name, name)
        bid = self.add_block("operator_mathop", parent=parent, inputs={}, fields={"OPERATOR": [op, None]})
        self.blocks[bid]["inputs"]["NUM"] = self.expr_input(a[0], bid)
        return bid
    if name in ("mouseX", "mouseY", "mouseDown", "daysSince2000", "username", "loudness"):
        self.need_args(name, a, 0)
        return self.add_block({
            "mouseX": "sensing_mousex",
            "mouseY": "sensing_mousey",
            "mouseDown": "sensing_mousedown",
            "daysSince2000": "sensing_dayssince2000",
            "username": "sensing_username",
            "loudness": "sensing_loudness",
        }[name], parent=parent)
    if name == "keyPressed":
        self.need_args(name, a, 1)
        key = _sbg_literal_string(a[0], "keyPressed key")
        bid = self.add_block("sensing_keypressed", parent=parent, inputs={})
        menu = self.add_block("sensing_keyoptions", parent=bid, shadow=True, fields={"KEY_OPTION": [key, None]})
        self.blocks[bid]["inputs"]["KEY_OPTION"] = [1, menu]
        return bid
    if name == "current":
        self.need_args(name, a, 1)
        value = _sbg_literal_string(a[0], "current() menu")
        allowed = {
            "year": "YEAR", "month": "MONTH", "date": "DATE", "dayofweek": "DAYOFWEEK",
            "dayOfWeek": "DAYOFWEEK", "hour": "HOUR", "minute": "MINUTE", "second": "SECOND",
        }
        if value not in allowed:
            raise CompileError("current() expects one of: year, month, date, dayofweek, hour, minute, second")
        return self.add_block("sensing_current", parent=parent, fields={"CURRENTMENU": [allowed[value], None]})
    if name in ("x", "y", "direction", "size", "costumeNumber", "costumeName"):
        _sbg_require_sprite_target(self, name)
        self.need_args(name, a, 0)
        if name == "x": return self.add_block("motion_xposition", parent=parent)
        if name == "y": return self.add_block("motion_yposition", parent=parent)
        if name == "direction": return self.add_block("motion_direction", parent=parent)
        if name == "size": return self.add_block("looks_size", parent=parent)
        if name == "costumeNumber": return self.add_block("looks_costumenumbername", parent=parent, fields={"NUMBER_NAME": ["number", None]})
        if name == "costumeName": return self.add_block("looks_costumenumbername", parent=parent, fields={"NUMBER_NAME": ["name", None]})
    if name in ("backdropNumber", "backdropName"):
        self.need_args(name, a, 0)
        return self.add_block("looks_backdropnumbername", parent=parent, fields={"NUMBER_NAME": ["number" if name.endswith("Number") else "name", None]})
    if name == "distanceTo":
        _sbg_require_sprite_target(self, name)
        self.need_args(name, a, 1)
        obj = _sbg_literal_string(a[0], "distanceTo target")
        bid = self.add_block("sensing_distanceto", parent=parent, inputs={})
        menu = self.add_block("sensing_distancetomenu", parent=bid, shadow=True, fields={"DISTANCETOMENU": [obj, None]})
        self.blocks[bid]["inputs"]["DISTANCETOMENU"] = [1, menu]
        return bid
    if name == "touching":
        _sbg_require_sprite_target(self, name)
        self.need_args(name, a, 1)
        obj = _sbg_literal_string(a[0], "touching target")
        bid = self.add_block("sensing_touchingobject", parent=parent, inputs={})
        menu = self.add_block("sensing_touchingobjectmenu", parent=bid, shadow=True, fields={"TOUCHINGOBJECTMENU": [obj, None]})
        self.blocks[bid]["inputs"]["TOUCHINGOBJECTMENU"] = [1, menu]
        return bid
    return _old_compile_call_expr_patch12(self, expr, parent)

ScratchBuilder.compile_call_expr = _compile_call_expr_patch12  # type: ignore[method-assign]


_old_compile_call_stmt_patch12 = ScratchBuilder.compile_call_stmt

def _sbg_menu_block(self: ScratchBuilder, opcode: str, parent: str, field: str, value: str) -> str:
    return self.add_block(opcode, parent=parent, shadow=True, fields={field: [value, None]})


def _compile_call_stmt_patch12(self: ScratchBuilder, expr: CallExpr) -> Optional[str]:
    name, a = expr.callee, expr.args
    if name == "clearTerminal":
        self.need_args(name, a, 0)
        self.list_id(TERMINAL_LIST_NAME)
        return self.add_block("data_deletealloflist", fields={"LIST": [TERMINAL_LIST_NAME, self.list_id(TERMINAL_LIST_NAME)]})
    if name == "logMany":
        val = Literal("") if not a else self.join_many(a)
        return _old_compile_call_stmt_patch12(self, CallExpr("log", [val]))
    if name in _SPRITE_ONLY_STMT:
        _sbg_require_sprite_target(self, name)
    if name == "setX":
        self.need_args(name, a, 1); bid = self.add_block("motion_setx", inputs={}); self.blocks[bid]["inputs"]["X"] = self.expr_input(a[0], bid); return bid
    if name == "setY":
        self.need_args(name, a, 1); bid = self.add_block("motion_sety", inputs={}); self.blocks[bid]["inputs"]["Y"] = self.expr_input(a[0], bid); return bid
    if name == "changeX":
        self.need_args(name, a, 1); bid = self.add_block("motion_changexby", inputs={}); self.blocks[bid]["inputs"]["DX"] = self.expr_input(a[0], bid); return bid
    if name == "changeY":
        self.need_args(name, a, 1); bid = self.add_block("motion_changeyby", inputs={}); self.blocks[bid]["inputs"]["DY"] = self.expr_input(a[0], bid); return bid
    if name == "goToXY":
        self.need_args(name, a, 2); bid = self.add_block("motion_gotoxy", inputs={}); self.blocks[bid]["inputs"]["X"] = self.expr_input(a[0], bid); self.blocks[bid]["inputs"]["Y"] = self.expr_input(a[1], bid); return bid
    if name == "glideToXY":
        self.need_args(name, a, 3); bid = self.add_block("motion_glidesecstoxy", inputs={}); self.blocks[bid]["inputs"]["SECS"] = self.expr_input(a[0], bid); self.blocks[bid]["inputs"]["X"] = self.expr_input(a[1], bid); self.blocks[bid]["inputs"]["Y"] = self.expr_input(a[2], bid); return bid
    if name == "goTo":
        self.need_args(name, a, 1); target = _sbg_literal_string(a[0], "goTo target"); bid = self.add_block("motion_goto", inputs={}); menu = _sbg_menu_block(self, "motion_goto_menu", bid, "TO", target); self.blocks[bid]["inputs"]["TO"] = [1, menu]; return bid
    if name == "move":
        self.need_args(name, a, 1); bid = self.add_block("motion_movesteps", inputs={}); self.blocks[bid]["inputs"]["STEPS"] = self.expr_input(a[0], bid); return bid
    if name == "turnRight":
        self.need_args(name, a, 1); bid = self.add_block("motion_turnright", inputs={}); self.blocks[bid]["inputs"]["DEGREES"] = self.expr_input(a[0], bid); return bid
    if name == "turnLeft":
        self.need_args(name, a, 1); bid = self.add_block("motion_turnleft", inputs={}); self.blocks[bid]["inputs"]["DEGREES"] = self.expr_input(a[0], bid); return bid
    if name == "pointDirection":
        self.need_args(name, a, 1); bid = self.add_block("motion_pointindirection", inputs={}); self.blocks[bid]["inputs"]["DIRECTION"] = self.expr_input(a[0], bid); return bid
    if name == "pointTo":
        self.need_args(name, a, 1); target = _sbg_literal_string(a[0], "pointTo target"); bid = self.add_block("motion_pointtowards", inputs={}); menu = _sbg_menu_block(self, "motion_pointtowards_menu", bid, "TOWARDS", target); self.blocks[bid]["inputs"]["TOWARDS"] = [1, menu]; return bid
    if name == "ifOnEdgeBounce":
        self.need_args(name, a, 0); return self.add_block("motion_ifonedgebounce")
    if name == "setRotationStyle":
        self.need_args(name, a, 1); style = _sbg_literal_string(a[0], "rotation style"); return self.add_block("motion_setrotationstyle", fields={"STYLE": [style, None]})
    if name == "say":
        self.need_args(name, a, 1); bid = self.add_block("looks_say", inputs={}); self.blocks[bid]["inputs"]["MESSAGE"] = self.expr_input(a[0], bid); return bid
    if name == "sayFor":
        self.need_args(name, a, 2); bid = self.add_block("looks_sayforsecs", inputs={}); self.blocks[bid]["inputs"]["MESSAGE"] = self.expr_input(a[0], bid); self.blocks[bid]["inputs"]["SECS"] = self.expr_input(a[1], bid); return bid
    if name == "think":
        self.need_args(name, a, 1); bid = self.add_block("looks_think", inputs={}); self.blocks[bid]["inputs"]["MESSAGE"] = self.expr_input(a[0], bid); return bid
    if name == "thinkFor":
        self.need_args(name, a, 2); bid = self.add_block("looks_thinkforsecs", inputs={}); self.blocks[bid]["inputs"]["MESSAGE"] = self.expr_input(a[0], bid); self.blocks[bid]["inputs"]["SECS"] = self.expr_input(a[1], bid); return bid
    if name == "show":
        self.need_args(name, a, 0); return self.add_block("looks_show")
    if name == "hide":
        self.need_args(name, a, 0); return self.add_block("looks_hide")
    if name == "setSize":
        self.need_args(name, a, 1); bid = self.add_block("looks_setsizeto", inputs={}); self.blocks[bid]["inputs"]["SIZE"] = self.expr_input(a[0], bid); return bid
    if name == "changeSize":
        self.need_args(name, a, 1); bid = self.add_block("looks_changesizeby", inputs={}); self.blocks[bid]["inputs"]["CHANGE"] = self.expr_input(a[0], bid); return bid
    if name == "setCostume":
        self.need_args(name, a, 1); bid = self.add_block("looks_switchcostumeto", inputs={}); self.blocks[bid]["inputs"]["COSTUME"] = self.expr_input(a[0], bid); return bid
    if name == "nextCostume":
        self.need_args(name, a, 0); return self.add_block("looks_nextcostume")
    if name in ("setEffect", "changeEffect"):
        self.need_args(name, a, 2); effect = _sbg_literal_string(a[0], "effect name"); opcode = "looks_seteffectto" if name == "setEffect" else "looks_changeeffectby"; inp = "VALUE" if name == "setEffect" else "CHANGE"; bid = self.add_block(opcode, inputs={}, fields={"EFFECT": [effect, None]}); self.blocks[bid]["inputs"][inp] = self.expr_input(a[1], bid); return bid
    if name == "clearEffects":
        self.need_args(name, a, 0); return self.add_block("looks_cleargraphiceffects")
    if name == "layerFront":
        self.need_args(name, a, 0); return self.add_block("looks_gotofrontback", fields={"FRONT_BACK": ["front", None]})
    if name == "layerBack":
        self.need_args(name, a, 0); return self.add_block("looks_gotofrontback", fields={"FRONT_BACK": ["back", None]})
    if name == "goForwardLayers":
        self.need_args(name, a, 1); bid = self.add_block("looks_goforwardbackwardlayers", inputs={}, fields={"FORWARD_BACKWARD": ["forward", None]}); self.blocks[bid]["inputs"]["NUM"] = self.expr_input(a[0], bid); return bid
    if name == "goBackwardLayers":
        self.need_args(name, a, 1); bid = self.add_block("looks_goforwardbackwardlayers", inputs={}, fields={"FORWARD_BACKWARD": ["backward", None]}); self.blocks[bid]["inputs"]["NUM"] = self.expr_input(a[0], bid); return bid
    if name == "createClone":
        self.need_args(name, a, 1); target = _sbg_literal_string(a[0], "clone target"); bid = self.add_block("control_create_clone_of", inputs={}); menu = _sbg_menu_block(self, "control_create_clone_of_menu", bid, "CLONE_OPTION", target); self.blocks[bid]["inputs"]["CLONE_OPTION"] = [1, menu]; return bid
    if name == "deleteThisClone":
        self.need_args(name, a, 0); return self.add_block("control_delete_this_clone")
    if name in ("stopAll", "stopThisScript", "stopOtherScripts"):
        self.need_args(name, a, 0); opt = {"stopAll": "all", "stopThisScript": "this script", "stopOtherScripts": "other scripts in sprite"}[name]; return self.add_block("control_stop", fields={"STOP_OPTION": [opt, None]})
    return _old_compile_call_stmt_patch12(self, expr)

ScratchBuilder.compile_call_stmt = _compile_call_stmt_patch12  # type: ignore[method-assign]


# Faster return: in returning procs, repeat(n) becomes while(counter>0 && !returned),
# so a return inside a long repeat does not spend time running empty guarded iterations.
_old_compile_stmt_patch12 = ScratchBuilder.compile_stmt

def _compile_stmt_return_fast_patch12(self: ScratchBuilder, stmt: Any) -> Optional[str]:
    _builder_ensure_patch_state(self)
    if isinstance(stmt, RepeatStmt) and getattr(self, "current_return_flag", None):
        self.return_temp_counter += 1
        counter = f"__sbg_repeat_left_{self.return_temp_counter}"
        init = VarDecl(counter, stmt.count, True)
        body = list(stmt.body) + [AssignStmt(counter, "-=", Literal(1))]
        cond = BinaryExpr(BinaryExpr(VarExpr(counter), ">", Literal(0)), "&&", BinaryExpr(VarExpr(self.current_return_flag), "==", Literal(0)))
        return self.chain(self.compile_stmt(init), self.compile_stmt(WhileStmt(cond, body)))
    return _old_compile_stmt_patch12(self, stmt)

ScratchBuilder.compile_stmt = _compile_stmt_return_fast_patch12  # type: ignore[method-assign]


# Make all emitted SBG procedures warp-mode by default. This is still vanilla
# Scratch: it is the built-in "run without screen refresh" custom-block flag.
_old_compile_proc_definition_patch12 = ScratchBuilder.compile_proc_definition

def _compile_proc_definition_warp_patch12(self: ScratchBuilder, proc: ProcDecl) -> str:
    bid = _old_compile_proc_definition_patch12(self, proc)
    # Patch definition prototype mutation after the normal compiler builds it.
    for block in self.blocks.values():
        if block.get("opcode") == "procedures_prototype":
            mut = block.get("mutation", {})
            if isinstance(mut, dict) and mut.get("proccode", "").startswith(proc.name):
                mut["warp"] = "true"
    return bid

ScratchBuilder.compile_proc_definition = _compile_proc_definition_warp_patch12  # type: ignore[method-assign]

_old_compile_proc_call_patch12 = ScratchBuilder.compile_proc_call

def _compile_proc_call_warp_patch12(self: ScratchBuilder, name: str, args: List[Any]) -> str:
    bid = _old_compile_proc_call_patch12(self, name, args)
    if bid in self.blocks and "mutation" in self.blocks[bid]:
        self.blocks[bid]["mutation"]["warp"] = "true"
    return bid

ScratchBuilder.compile_proc_call = _compile_proc_call_warp_patch12  # type: ignore[method-assign]


# Mark Stage vs Sprite builders for vanilla target-specific diagnostics.
_old_compiler_init_patch12 = Compiler.__init__
def _compiler_init_patch12(self: Compiler, *args: Any, **kwargs: Any) -> None:
    _old_compiler_init_patch12(self, *args, **kwargs)
    self.b.target_kind = "stage"
Compiler.__init__ = _compiler_init_patch12  # type: ignore[method-assign]

_old_sprite_compiler_init_patch12 = SpriteTargetCompiler.__init__
def _sprite_compiler_init_patch12(self: SpriteTargetCompiler, *args: Any, **kwargs: Any) -> None:
    _old_sprite_compiler_init_patch12(self, *args, **kwargs)
    self.b.target_kind = "sprite"
SpriteTargetCompiler.__init__ = _sprite_compiler_init_patch12  # type: ignore[method-assign]


# Native runner state for sprite/motion APIs. This is headless but deterministic,
# so code using these functions can be smoke-tested before compiling to .sb3.
_old_runtime_call_patch12 = Runtime.call

def _runtime_state(self: Runtime) -> Dict[str, Any]:
    st = getattr(self, "_sbg_native_sprite_state", None)
    if st is None:
        st = {"x": 0.0, "y": 0.0, "direction": 90.0, "size": 100.0, "visible": True, "costume": 1, "backdrop": 1}
        self._sbg_native_sprite_state = st
    return st


def _runtime_call_patch12(self: Runtime, name: str, args: List[Any]) -> Any:
    st = _runtime_state(self)
    if name == "clearTerminal":
        self.lists.setdefault(TERMINAL_LIST_NAME, []).clear(); return None
    if name == "logMany":
        return self.call("log", ["".join(str(x) for x in args)])
    if name == "letter": return str(args[0])[max(0, int(args[1])-1):max(0, int(args[1]))]
    if name == "containsText": return str(args[1]) in str(args[0])
    if name in ("sin", "cos", "tan"):
        return getattr(math, name)(math.radians(float(args[0])))
    if name in ("asin", "acos", "atan"):
        return math.degrees(getattr(math, name)(float(args[0])))
    if name == "ln": return math.log(float(args[0]))
    if name == "log10": return math.log10(float(args[0]))
    if name == "exp": return math.exp(float(args[0]))
    if name == "pow10": return 10 ** float(args[0])
    if name == "setX": st["x"] = float(args[0]); return None
    if name == "setY": st["y"] = float(args[0]); return None
    if name == "changeX": st["x"] += float(args[0]); return None
    if name == "changeY": st["y"] += float(args[0]); return None
    if name == "goToXY": st["x"], st["y"] = float(args[0]), float(args[1]); return None
    if name == "move":
        steps = float(args[0]); rad = math.radians(90 - st["direction"]); st["x"] += math.cos(rad)*steps; st["y"] += math.sin(rad)*steps; return None
    if name == "turnRight": st["direction"] += float(args[0]); return None
    if name == "turnLeft": st["direction"] -= float(args[0]); return None
    if name == "pointDirection": st["direction"] = float(args[0]); return None
    if name in ("goTo", "glideToXY", "pointTo", "ifOnEdgeBounce", "setRotationStyle", "say", "sayFor", "think", "thinkFor", "setCostume", "nextCostume", "setEffect", "changeEffect", "clearEffects", "layerFront", "layerBack", "goForwardLayers", "goBackwardLayers", "createClone", "deleteThisClone", "stopThisScript", "stopOtherScripts"):
        # Visual/VM-only in headless native mode.
        return None
    if name == "show": st["visible"] = True; return None
    if name == "hide": st["visible"] = False; return None
    if name == "setSize": st["size"] = float(args[0]); return None
    if name == "changeSize": st["size"] += float(args[0]); return None
    if name == "stopAll": raise StopIteration("stopAll")
    if name == "x": return st["x"]
    if name == "y": return st["y"]
    if name == "direction": return st["direction"]
    if name == "size": return st["size"]
    if name in ("costumeNumber", "backdropNumber"): return st["costume" if name.startswith("costume") else "backdrop"]
    if name in ("costumeName", "backdropName"): return str(st["costume" if name.startswith("costume") else "backdrop"])
    if name == "mouseX": return 0
    if name == "mouseY": return 0
    if name == "mouseDown": return False
    if name == "keyPressed": return False
    if name == "current":
        import datetime as _dt
        now = _dt.datetime.now()
        m = str(args[0]); return {"year": now.year, "month": now.month, "date": now.day, "dayofweek": now.isoweekday(), "dayOfWeek": now.isoweekday(), "hour": now.hour, "minute": now.minute, "second": now.second}.get(m, 0)
    if name == "daysSince2000":
        import datetime as _dt
        return (_dt.datetime.now() - _dt.datetime(2000,1,1)).total_seconds()/86400
    if name == "username": return "native"
    if name == "loudness": return 0
    if name == "distanceTo": return math.sqrt(st["x"]*st["x"] + st["y"]*st["y"])
    if name == "touching": return False
    return _old_runtime_call_patch12(self, name, args)

Runtime.call = _runtime_call_patch12  # type: ignore[method-assign]



# Patch 12b: make new statement builtins visible even through patch9's lowering path.
_old_compile_stmt_patch12b = ScratchBuilder.compile_stmt

def _compile_stmt_patch12b(self: ScratchBuilder, stmt: Any) -> Optional[str]:
    if isinstance(stmt, ExprStmt) and isinstance(stmt.expr, CallExpr) and stmt.expr.callee in BUILTIN_STMT_NAMES:
        pre, lowered_args = _builder_lower_exprs(self, stmt.expr.args)
        core = self.compile_call_stmt(CallExpr(stmt.expr.callee, lowered_args))
        return self.chain(self.compile_statement_chain(pre), core)
    return _old_compile_stmt_patch12b(self, stmt)

ScratchBuilder.compile_stmt = _compile_stmt_patch12b  # type: ignore[method-assign]


# Patch 12c: resolve imports recursively inside stage/sprite/proc/event bodies.
def _sbg_resolve_body_recursive(self: ImportResolver, body: List[Any], current_file: Path) -> List[Any]:
    out: List[Any] = []
    for stmt in body:
        if isinstance(stmt, ImportDecl):
            try:
                imported = self.load_import(stmt.spec, current_file)
                out.extend(imported.body)
            except ImportSBGError as e:
                attach_location(e, stmt)
                raise
            continue
        if isinstance(stmt, TargetDecl):
            stmt.body = _sbg_resolve_body_recursive(self, stmt.body, current_file)
        elif isinstance(stmt, ProcDecl):
            stmt.body = _sbg_resolve_body_recursive(self, stmt.body, current_file)
        elif isinstance(stmt, EventDecl):
            stmt.body = _sbg_resolve_body_recursive(self, stmt.body, current_file)
        elif isinstance(stmt, BlockStmt):
            stmt.body = _sbg_resolve_body_recursive(self, stmt.body, current_file)
        elif isinstance(stmt, IfStmt):
            stmt.then_body = _sbg_resolve_body_recursive(self, stmt.then_body, current_file)
            if stmt.else_body is not None:
                stmt.else_body = _sbg_resolve_body_recursive(self, stmt.else_body, current_file)
        elif isinstance(stmt, (RepeatStmt, ForeverStmt, WhileStmt)):
            stmt.body = _sbg_resolve_body_recursive(self, stmt.body, current_file)
        elif isinstance(stmt, ForStmt):
            stmt.body = _sbg_resolve_body_recursive(self, stmt.body, current_file)
        out.append(stmt)
    return out

def _import_resolve_program_patch12(self: ImportResolver, program: Program, current_file: Path) -> Program:
    return Program(_sbg_resolve_body_recursive(self, program.body, current_file))

ImportResolver.resolve_program = _import_resolve_program_patch12  # type: ignore[method-assign]


# Patch 12d: allow the same library file to be imported into multiple targets.
# Scratch has separate block/workspace storage per target, so a library imported
# into Stage must also be imported into a sprite if the sprite wants those procs.
def _import_load_import_patch12(self: ImportResolver, spec: str, current_file: Path) -> Program:
    path = self.resolve_import_path(spec, current_file)
    if path in self.stack:
        chain = " -> ".join(str(p) for p in [*self.stack, path])
        raise ImportSBGError(f"circular import detected: {chain}")
    self.stack.append(path)
    try:
        text = path.read_text(encoding="utf-8")
        self.source_cache[str(path)] = text
        program = Parser(Lexer(text, str(path)).tokens(), str(path)).parse()
        return self.resolve_program(program, path)
    except OSError as e:
        raise ImportSBGError(str(e)) from e
    finally:
        if self.stack and self.stack[-1] == path:
            self.stack.pop()

ImportResolver.load_import = _import_load_import_patch12  # type: ignore[method-assign]

