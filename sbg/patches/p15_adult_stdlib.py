# =============================================================================
# Patch 15: adult stdlib surface + additional vanilla Scratch VM bindings
# =============================================================================

VERSION = "0.9.0-patch15-professional-stdlib"

# Extra builtins intentionally map to vanilla Scratch opcodes or official Scratch
# extensions (Pen/Music). No TurboWarp-only blocks are emitted.
BUILTIN_EXPR_NAMES.update({
    # coercion / list convenience
    "text", "listLen", "listGet", "listHas", "firstItem", "lastItem",
    # "num" removed from here -- real `.sbg proc` in packages/std/core.sbg now
    # (same str/to_string treatment, see kontekst.md). "bool01" also removed
    # (migrated to packages/std/core.sbg) now that _builder_lower_expr tags
    # lowered temp-var reporters as boolean-shaped when the original call was
    # boolean-shaped -- see the was_boolean/_sbg_bool_shaped block near the
    # top of this file. Before that fix, is_boolean_expr special-cased
    # "bool01" here because a plain `.sbg proc` gets lowered to a temp-var
    # reporter before is_boolean_expr ever sees the call, which
    # produces a wrongly-shaped (non-boolean) block input. Verified this
    # experimentally in this session -- do not migrate bool01 without first
    # fixing is_boolean_expr/lowering to track "this temp var was a
    # boolean-returning call".
    # sound/music reporters
    "volume", "tempo",
})
BUILTIN_STMT_NAMES.update({
    # control helpers that normal languages expect
    "waitUntil",
    # list helpers that compile to multiple vanilla Scratch blocks
    "popTo", "shiftTo", "appendList", "copyList",
    # sound category
    "playSoundUntilDone", "setVolume", "changeVolume",
    # official Music extension
    "setTempo", "changeTempo", "playNote", "rest", "setInstrument", "playDrum",
})
BUILTIN_NAMES = BUILTIN_EXPR_NAMES | BUILTIN_STMT_NAMES

_LIST_FIRST_ARG_BUILTINS_PATCH15 = {
    "listLen", "listGet", "listHas", "firstItem", "lastItem", "popTo", "shiftTo", "appendList", "copyList"
}


def _sbg_output_var_name(expr: Any, what: str) -> str:
    if isinstance(expr, VarExpr):
        return expr.name
    if isinstance(expr, Literal):
        return str(expr.value)
    raise CompileError(f"{what} needs an output variable name, e.g. {what}(xs, \"out\")")


def _sbg_sound_menu_input(self: ScratchBuilder, bid: str, sound_name: str) -> None:
    menu = self.add_block("sound_sounds_menu", parent=bid, shadow=True, fields={"SOUND_MENU": [sound_name, None]})
    self.blocks[bid]["inputs"]["SOUND_MENU"] = [1, menu]


_old_compile_call_expr_patch15 = ScratchBuilder.compile_call_expr

def _compile_call_expr_patch15(self: ScratchBuilder, expr: CallExpr, parent: Optional[str]) -> str:
    name, a = expr.callee, expr.args
    # "num" removed -- real `.sbg proc` in packages/std/core.sbg now.
    if name == "text":
        self.need_args(name, a, 1)
        return self.compile_expr(CallExpr("join", [Literal(""), a[0]]), parent)
    # "bool01" removed -- real `.sbg proc` in packages/std/core.sbg now.
    if name == "listLen":
        self.need_args(name, a, 1)
        lst = self.require_list_expr(a[0])
        return self.add_block("data_lengthoflist", parent=parent, fields={"LIST": [lst, self.list_id(lst)]})
    if name == "listGet":
        self.need_args(name, a, 2)
        return _old_compile_call_expr_patch15(self, CallExpr("item", a), parent)
    if name == "listHas":
        self.need_args(name, a, 2)
        return _old_compile_call_expr_patch15(self, CallExpr("contains", a), parent)
    if name == "firstItem":
        self.need_args(name, a, 1)
        return _old_compile_call_expr_patch15(self, CallExpr("item", [a[0], Literal(1)]), parent)
    if name == "lastItem":
        self.need_args(name, a, 1)
        return _old_compile_call_expr_patch15(self, CallExpr("item", [a[0], CallExpr("len", [a[0]])]), parent)
    if name == "volume":
        self.need_args(name, a, 0)
        return self.add_block("sound_volume", parent=parent)
    if name == "tempo":
        self.need_args(name, a, 0)
        return self.add_block("music_getTempo", parent=parent)
    return _old_compile_call_expr_patch15(self, expr, parent)

ScratchBuilder.compile_call_expr = _compile_call_expr_patch15  # type: ignore[method-assign]


_old_bool_expr_patch15 = ScratchBuilder.is_boolean_expr

def _bool_expr_patch15(self: ScratchBuilder, expr: Any) -> bool:
    # NOTE: "bool01" stays here even though it is now a real `.sbg proc`
    # (packages/std/core.sbg), not native. is_boolean_expr is called on the
    # ORIGINAL CallExpr (by name) inside _builder_lower_expr BEFORE lowering
    # replaces it with a temp-var reporter; that call is what lets the
    # boolean-shaped tag propagate onto the lowered temp var (see the
    # was_boolean/_sbg_bool_shaped block near the top of this file). Removing
    # "bool01" from this name-based check would silently reintroduce the
    # exact CONDITION-shape bug this migration fixed.
    return _old_bool_expr_patch15(self, expr) or (isinstance(expr, CallExpr) and expr.callee in {"listHas", "bool01"})

ScratchBuilder.is_boolean_expr = _bool_expr_patch15  # type: ignore[method-assign]


_old_compile_call_stmt_patch15 = ScratchBuilder.compile_call_stmt

def _compile_call_stmt_patch15(self: ScratchBuilder, expr: CallExpr) -> Optional[str]:
    name, a = expr.callee, expr.args
    if name == "waitUntil":
        self.need_args(name, a, 1)
        bid = self.add_block("control_wait_until", inputs={})
        self.blocks[bid]["inputs"]["CONDITION"] = self.expr_input(a[0], bid)
        return bid

    if name in ("popTo", "shiftTo"):
        self.need_args(name, a, 2)
        lst = self.require_list_expr(a[0])
        out_name = _sbg_output_var_name(a[1], name)
        index_expr = Literal(1) if name == "shiftTo" else CallExpr("len", [VarExpr(lst)])
        set_bid = self.add_block("data_setvariableto", fields={"VARIABLE": [out_name, self.var_id(out_name)]}, inputs={})
        self.blocks[set_bid]["inputs"]["VALUE"] = self.expr_input(CallExpr("item", [VarExpr(lst), index_expr]), set_bid)
        del_bid = self.add_block("data_deleteoflist", fields={"LIST": [lst, self.list_id(lst)]}, inputs={})
        self.blocks[del_bid]["inputs"]["INDEX"] = self.expr_input(index_expr, del_bid)
        return self.chain(set_bid, del_bid)

    if name in ("appendList", "copyList"):
        self.need_args(name, a, 2)
        src = self.require_list_expr(a[0])
        dst = self.require_list_expr(a[1])
        tmp = f"__sbg_i_{self.uid('tmp')}"
        statements: List[Any] = []
        if name == "copyList":
            statements.append(ExprStmt(CallExpr("clearList", [VarExpr(dst)])))
        statements.append(AssignStmt(tmp, "=", Literal(1)))
        statements.append(WhileStmt(
            BinaryExpr(VarExpr(tmp), "<=", CallExpr("len", [VarExpr(src)])),
            [
                ExprStmt(CallExpr("push", [VarExpr(dst), CallExpr("item", [VarExpr(src), VarExpr(tmp)])])),
                AssignStmt(tmp, "+=", Literal(1)),
            ],
        ))
        self.var_id(tmp)
        return self.compile_statement_chain(statements)

    if name == "playSoundUntilDone":
        self.need_args(name, a, 1)
        sound_name = _sbg_literal_string(a[0], "playSoundUntilDone sound")
        bid = self.add_block("sound_playuntildone", inputs={})
        _sbg_sound_menu_input(self, bid, sound_name)
        return bid
    if name == "setVolume":
        self.need_args(name, a, 1)
        bid = self.add_block("sound_setvolumeto", inputs={})
        self.blocks[bid]["inputs"]["VOLUME"] = self.expr_input(a[0], bid)
        return bid
    if name == "changeVolume":
        self.need_args(name, a, 1)
        bid = self.add_block("sound_changevolumeby", inputs={})
        self.blocks[bid]["inputs"]["VOLUME"] = self.expr_input(a[0], bid)
        return bid
    if name == "setTempo":
        self.need_args(name, a, 1)
        bid = self.add_block("music_setTempo", inputs={})
        self.blocks[bid]["inputs"]["TEMPO"] = self.expr_input(a[0], bid)
        return bid
    if name == "changeTempo":
        self.need_args(name, a, 1)
        bid = self.add_block("music_changeTempo", inputs={})
        self.blocks[bid]["inputs"]["TEMPO"] = self.expr_input(a[0], bid)
        return bid
    if name == "playNote":
        self.need_args(name, a, 2)
        bid = self.add_block("music_playNoteForBeats", inputs={})
        self.blocks[bid]["inputs"]["NOTE"] = self.expr_input(a[0], bid)
        self.blocks[bid]["inputs"]["BEATS"] = self.expr_input(a[1], bid)
        return bid
    if name == "rest":
        self.need_args(name, a, 1)
        bid = self.add_block("music_restForBeats", inputs={})
        self.blocks[bid]["inputs"]["BEATS"] = self.expr_input(a[0], bid)
        return bid
    if name == "setInstrument":
        self.need_args(name, a, 1)
        bid = self.add_block("music_setInstrument", inputs={})
        self.blocks[bid]["inputs"]["INSTRUMENT"] = self.expr_input(a[0], bid)
        return bid
    if name == "playDrum":
        self.need_args(name, a, 2)
        bid = self.add_block("music_playDrumForBeats", inputs={})
        self.blocks[bid]["inputs"]["DRUM"] = self.expr_input(a[0], bid)
        self.blocks[bid]["inputs"]["BEATS"] = self.expr_input(a[1], bid)
        return bid

    return _old_compile_call_stmt_patch15(self, expr)

ScratchBuilder.compile_call_stmt = _compile_call_stmt_patch15  # type: ignore[method-assign]


_old_runtime_call_patch15 = Runtime.call

def _runtime_call_patch15(self: Runtime, name: str, args: List[Any]) -> Any:
    # "num" removed -- real `.sbg proc` in packages/std/core.sbg now.
    if name == "text": return "" if not args else str(args[0])
    # "bool01" removed -- real `.sbg proc` in packages/std/core.sbg now.
    if name == "listLen": return len(self.get_list_arg(args[0]))
    if name == "listGet": return self.get_list_arg(args[0])[int(args[1]) - 1]
    if name == "listHas": return args[1] in self.get_list_arg(args[0])
    if name == "firstItem": return self.get_list_arg(args[0])[0]
    if name == "lastItem": return self.get_list_arg(args[0])[-1]
    if name == "waitUntil": return None
    if name in ("popTo", "shiftTo"):
        lst = self.get_list_arg(args[0], require_name=False)
        if not lst: return None
        value = lst.pop(0 if name == "shiftTo" else -1)
        if len(args) >= 2 and isinstance(args[1], str):
            self.vars[args[1]] = value
        return value
    if name == "appendList":
        self.get_list_arg(args[1]).extend(list(self.get_list_arg(args[0]))); return None
    if name == "copyList":
        dst = self.get_list_arg(args[1]); dst.clear(); dst.extend(list(self.get_list_arg(args[0]))); return None
    if name in ("playSoundUntilDone", "setVolume", "changeVolume", "setTempo", "changeTempo", "playNote", "rest", "setInstrument", "playDrum"):
        # Headless native runner: deterministic no-op for VM-side audio.
        return None
    if name == "volume": return 100
    if name == "tempo": return 60
    return _old_runtime_call_patch15(self, name, args)

Runtime.call = _runtime_call_patch15  # type: ignore[method-assign]


_old_project_ensure_patch15 = _project_ensure_patch13

def _project_ensure_patch15(project: Dict[str, Any]) -> Dict[str, Any]:
    project = _old_project_ensure_patch15(project)
    exts = project.setdefault("extensions", [])
    if "music" not in exts:
        exts.append("music")
    meta = project.setdefault("meta", {})
    meta["agent"] = f"StageBG/SBG {VERSION}"
    meta["stagebgScratchTurboNote"] = "Vanilla Scratch editor Turbo Mode is Shift+GreenFlag; StageBG also emits warp custom blocks."
    return project

# Existing patch14 compiler calls Compiler.compile -> _project_ensure_patch13 indirectly.
# Replace Compiler.compile once more to add patch15 metadata/extensions after patch14.
_old_compiler_compile_patch15 = Compiler.compile

def _compiler_compile_patch15(self: Compiler) -> Dict[str, Any]:
    return _project_ensure_patch15(_old_compiler_compile_patch15(self))

Compiler.compile = _compiler_compile_patch15  # type: ignore[method-assign]



# Patch 15b: source-level local variable lowering.
# Scratch variables are target-global, so real block/procedure-local `let` needs
# compiler-generated hidden names. This pass keeps SBG usable like a normal
# language: local `i`, `line`, `tmp`, etc. no longer collide across stdlib calls.

def _sbg_sanitize_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", name)


def _sbg_mangle_locals(program: Program) -> Program:
    counter = {"n": 0}

    def fresh(name: str) -> str:
        counter["n"] += 1
        return f"__loc_{counter['n']}_{_sbg_sanitize_name(name)}"

    def lookup(env_stack: List[Dict[str, str]], name: str) -> str:
        for env in reversed(env_stack):
            if name in env:
                return env[name]
        return name

    def expr(e: Any, env_stack: List[Dict[str, str]]) -> Any:
        if isinstance(e, VarExpr):
            e.name = lookup(env_stack, e.name)
        elif isinstance(e, BinaryExpr):
            e.left = expr(e.left, env_stack); e.right = expr(e.right, env_stack)
        elif isinstance(e, UnaryExpr):
            e.expr = expr(e.expr, env_stack)
        elif isinstance(e, CallExpr):
            if e.callee == "cin":
                # cin's targets are encoded as Literal(varname) rather than
                # VarExpr(varname), so they need explicit lookup/rename here;
                # otherwise cin keeps writing to the pre-mangling variable name
                # while every read of that local uses the mangled __loc_N_name.
                e.args = [
                    Literal(lookup(env_stack, a.value)) if isinstance(a, Literal) else expr(a, env_stack)
                    for a in e.args
                ]
            else:
                e.args = [expr(a, env_stack) for a in e.args]
        elif isinstance(e, ArrayExpr):
            e.items = [expr(x, env_stack) for x in e.items]
        return e

    def body(stmts: List[Any], env_stack: List[Dict[str, str]], *, top_level: bool = False) -> List[Any]:
        local_scope: Dict[str, str] = {}
        stack = env_stack if top_level else [*env_stack, local_scope]
        out: List[Any] = []
        for st in stmts:
            out.append(stmt(st, stack, local_scope if not top_level else None, top_level=top_level))
        return out

    def stmt(st: Any, env_stack: List[Dict[str, str]], current_scope: Optional[Dict[str, str]], *, top_level: bool = False) -> Any:
        if isinstance(st, VarDecl):
            st.expr = expr(st.expr, env_stack)
            if not top_level and current_scope is not None:
                new = fresh(st.name)
                current_scope[st.name] = new
                st.name = new
            return st
        if isinstance(st, ListDecl):
            st.items = [expr(x, env_stack) for x in st.items]
            if not top_level and current_scope is not None:
                new = fresh(st.name)
                current_scope[st.name] = new
                st.name = new
            return st
        if isinstance(st, AssignStmt):
            st.name = lookup(env_stack, st.name)
            st.expr = expr(st.expr, env_stack)
            return st
        if isinstance(st, ExprStmt):
            st.expr = expr(st.expr, env_stack); return st
        if isinstance(st, ReturnStmt):
            if st.expr is not None: st.expr = expr(st.expr, env_stack)
            return st
        if isinstance(st, IfStmt):
            st.cond = expr(st.cond, env_stack)
            st.then_body = body(st.then_body, env_stack)
            if st.else_body is not None:
                st.else_body = body(st.else_body, env_stack)
            return st
        if isinstance(st, RepeatStmt):
            st.count = expr(st.count, env_stack)
            st.body = body(st.body, env_stack)
            return st
        if isinstance(st, ForeverStmt):
            st.body = body(st.body, env_stack); return st
        if isinstance(st, WhileStmt):
            st.cond = expr(st.cond, env_stack)
            st.body = body(st.body, env_stack)
            return st
        if isinstance(st, ForStmt):
            loop_scope: Dict[str, str] = {}
            loop_stack = [*env_stack, loop_scope]
            if st.init is not None:
                st.init = stmt(st.init, loop_stack, loop_scope, top_level=False)
            if st.cond is not None:
                st.cond = expr(st.cond, loop_stack)
            if st.update is not None:
                st.update = stmt(st.update, loop_stack, loop_scope, top_level=False)
            st.body = body(st.body, loop_stack)
            return st
        if isinstance(st, ProcDecl):
            param_scope = {p: p for p in st.params}
            st.body = body(st.body, [*env_stack, param_scope])
            return st
        if isinstance(st, EventDecl):
            param_scope: Dict[str, str] = {}
            if st.kind == "action" and st.value:
                param_scope[st.value] = st.value
            st.body = body(st.body, [*env_stack, param_scope])
            return st
        if isinstance(st, TargetDecl):
            # Variables/lists directly under `stage {}` / `sprite {}` are target globals.
            st.body = body(st.body, env_stack, top_level=True)
            return st
        return st

    program.body = body(program.body, [], top_level=True)
    return program


_old_parse_source_patch15b = parse_source

def parse_source(text: str, filename: str = "<source>") -> Program:  # type: ignore[no-redef]
    return _sbg_mangle_locals(_old_parse_source_patch15b(text, filename))



