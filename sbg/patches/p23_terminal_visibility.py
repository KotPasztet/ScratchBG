# =============================================================================
# Patch 23: dynamic terminal and input-prompt visibility
# =============================================================================
# Vanilla Scratch cannot close an active ask-and-wait bubble in the middle of the
# current ask.  The compiler therefore gates the *next* prompt with a global flag,
# and exposes terminal.show()/hide() plus terminal.showPrompt()/hidePrompt().

VERSION = "0.9.0-patch24-keyboard"

TERMINAL_VISIBLE_VAR = "__sbg_terminal_visible"
TERMINAL_INPUT_ENABLED_VAR = "__sbg_terminal_input_enabled"
TERMINAL_VISIBLE_VAR_ID = "sbg:terminal:visible"
TERMINAL_INPUT_ENABLED_VAR_ID = "sbg:terminal:input_enabled"
TERMINAL_HIDDEN_WAIT_SECONDS = 0.05

_TERMINAL_VISIBILITY_NAMES = {
    "showTerminal", "hideTerminal", "toggleTerminal",
    "showInputPrompt", "hideInputPrompt", "enableInputPrompt", "disableInputPrompt",
    "enableTerminalInput", "disableTerminalInput", "setInputPromptVisible", "setTerminalInputEnabled",
    "showTerminalAndPrompt", "hideTerminalAndPrompt", "terminalVisible", "terminalPromptVisible",
}
BUILTIN_STMT_NAMES.update(_TERMINAL_VISIBILITY_NAMES)
BUILTIN_EXPR_NAMES.update({"terminalVisible", "terminalPromptVisible"})
BUILTIN_NAMES.update(_TERMINAL_VISIBILITY_NAMES)

_prev_var_id_patch23 = ScratchBuilder.var_id

def _scratchbuilder_var_id_patch23(self: ScratchBuilder, name: str) -> str:
    if name == TERMINAL_VISIBLE_VAR:
        self.variables.setdefault(name, TERMINAL_VISIBLE_VAR_ID)
        return TERMINAL_VISIBLE_VAR_ID
    if name == TERMINAL_INPUT_ENABLED_VAR:
        self.variables.setdefault(name, TERMINAL_INPUT_ENABLED_VAR_ID)
        return TERMINAL_INPUT_ENABLED_VAR_ID
    return _prev_var_id_patch23(self, name)

ScratchBuilder.var_id = _scratchbuilder_var_id_patch23  # type: ignore[method-assign]


def _sbg_terminal_defaults_patch23(compiler: Any) -> None:
    compiler.b.var_id(TERMINAL_VISIBLE_VAR)
    compiler.b.var_id(TERMINAL_INPUT_ENABLED_VAR)
    compiler.init_values.setdefault(TERMINAL_VISIBLE_VAR, 1)
    compiler.init_values.setdefault(TERMINAL_INPUT_ENABLED_VAR, 1)

_prev_compiler_analyze_patch23 = Compiler.analyze

def _compiler_analyze_patch23(self: Compiler) -> None:
    _prev_compiler_analyze_patch23(self)
    _sbg_terminal_defaults_patch23(self)

Compiler.analyze = _compiler_analyze_patch23  # type: ignore[method-assign]


def _sbg_make_terminal_visible_setter_patch23(b: ScratchBuilder, visible: bool) -> str:
    b.list_id(TERMINAL_LIST_NAME)
    set_flag = _builder_make_set_var(b, TERMINAL_VISIBLE_VAR, Literal(1 if visible else 0))
    vis = b.add_block(
        "data_showlist" if visible else "data_hidelist",
        fields={"LIST": [TERMINAL_LIST_NAME, b.list_id(TERMINAL_LIST_NAME)]},
    )
    b.blocks[set_flag]["next"] = vis
    b.blocks[vis]["parent"] = set_flag
    return set_flag


def _sbg_make_prompt_visible_setter_patch23(b: ScratchBuilder, enabled: bool) -> str:
    return _builder_make_set_var(b, TERMINAL_INPUT_ENABLED_VAR, Literal(1 if enabled else 0))


def _sbg_make_terminal_show_all_patch23(b: ScratchBuilder) -> str:
    first = _sbg_make_terminal_visible_setter_patch23(b, True)
    second = _sbg_make_prompt_visible_setter_patch23(b, True)
    b.chain(first, second)
    return first


def _sbg_make_terminal_hide_all_patch23(b: ScratchBuilder) -> str:
    first = _sbg_make_terminal_visible_setter_patch23(b, False)
    second = _sbg_make_prompt_visible_setter_patch23(b, False)
    b.chain(first, second)
    return first


def _sbg_make_prompt_ifelse_loop_body_patch23(self: Compiler, ask_chain_first: str, wait_chain_first: str) -> str:
    cond = BinaryExpr(VarExpr(TERMINAL_INPUT_ENABLED_VAR), "==", Literal(1))
    bid = self.b.add_block("control_if_else", inputs={})
    self.b.blocks[bid]["inputs"]["CONDITION"] = self.b.expr_input(cond, bid)
    self.b.blocks[bid]["inputs"]["SUBSTACK"] = self.b.substack_input(ask_chain_first)
    self.b.blocks[bid]["inputs"]["SUBSTACK2"] = self.b.substack_input(wait_chain_first)
    self.b.set_parent(ask_chain_first, bid)
    self.b.set_parent(wait_chain_first, bid)
    return bid


def _compiler_compile_console_flag_loop_patch23(self: Compiler) -> None:
    assert self.action_argid is not None
    _sbg_terminal_defaults_patch23(self)
    has_action_return = _sbg_action_entries_have_return(self.action_entries)

    hat = self.b.add_block("event_whenflagclicked", topLevel=True, x=536, y=455)
    reset = _sbg_compile_delta_reset(self.b)
    forever = self.b.add_block("control_forever", inputs={})

    ask = self.b.add_block("sensing_askandwait", inputs={"QUESTION": [1, [10, ">"]]})
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
    self.b.blocks[ask]["next"] = echo
    self.b.blocks[echo]["parent"] = ask
    self.b.blocks[echo]["next"] = call
    self.b.blocks[call]["parent"] = echo

    ask_chain_first = ask
    if has_action_return:
        cond = BinaryExpr(VarExpr(ACTION_RETURN_FLAG), "==", Literal(1))
        if_ret = self.b.add_block("control_if", parent=call, inputs={})
        self.b.blocks[if_ret]["inputs"]["CONDITION"] = self.b.expr_input(cond, if_ret)
        ret_log = _builder_make_log_to_terminal(self.b, CallExpr("join", [Literal("=> "), VarExpr(ACTION_RETURN_VAR)]))
        self.b.blocks[if_ret]["inputs"]["SUBSTACK"] = self.b.substack_input(ret_log)
        self.b.set_parent(ret_log, if_ret)
        self.b.blocks[call]["next"] = if_ret

    wait_block = self.b.add_block("control_wait", inputs={})
    self.b.blocks[wait_block]["inputs"]["DURATION"] = self.b.expr_input(Literal(TERMINAL_HIDDEN_WAIT_SECONDS), wait_block)
    gate = _sbg_make_prompt_ifelse_loop_body_patch23(self, ask_chain_first, wait_block)
    self.b.blocks[gate]["parent"] = forever

    self.b.blocks[hat]["next"] = reset or forever
    if reset:
        last = reset
        while self.b.blocks[last].get("next"):
            last = self.b.blocks[last]["next"]
        self.b.blocks[last]["next"] = forever
        self.b.blocks[forever]["parent"] = last
    else:
        self.b.blocks[forever]["parent"] = hat
    self.b.blocks[forever]["inputs"]["SUBSTACK"] = [2, gate]

def _compiler_compile_console_flag_loop_single_main_patch25(self: Compiler) -> None:
    """Green flag runs main() exactly once, no outer REPL ask(">") loop.

    Vanilla Scratch's `ask and wait` block already only shows the input box
    while that specific block is waiting for an answer, and hides it the
    instant it's answered. The only reason the input box used to appear to
    be "always open" for plain main()-style programs was the wrapping
    `forever { ask(">") ... }` console loop, which immediately reopened a
    new (empty) prompt right after main() finished. Skipping that outer loop
    for single-main programs means the input box now only ever appears for
    real `cin >> x;` calls inside the program.
    """
    assert self.action_argid is not None
    _sbg_terminal_defaults_patch23(self)
    has_action_return = _sbg_action_entries_have_return(self.action_entries)

    hat = self.b.add_block("event_whenflagclicked", topLevel=True, x=536, y=455)
    reset = _sbg_compile_delta_reset(self.b)
    call = self.b.add_block("procedures_call", inputs={}, mutation={
        "tagName": "mutation",
        "children": [],
        "proccode": "Action %s",
        "argumentids": json.dumps([self.action_argid]),
        "warp": "true",
    })
    self.b.blocks[call]["inputs"][self.action_argid] = [1, [10, ""]]

    if has_action_return:
        cond = BinaryExpr(VarExpr(ACTION_RETURN_FLAG), "==", Literal(1))
        if_ret = self.b.add_block("control_if", parent=call, inputs={})
        self.b.blocks[if_ret]["inputs"]["CONDITION"] = self.b.expr_input(cond, if_ret)
        ret_log = _builder_make_log_to_terminal(self.b, CallExpr("join", [Literal("=> "), VarExpr(ACTION_RETURN_VAR)]))
        self.b.blocks[if_ret]["inputs"]["SUBSTACK"] = self.b.substack_input(ret_log)
        self.b.set_parent(ret_log, if_ret)
        self.b.blocks[call]["next"] = if_ret

    self.b.blocks[hat]["next"] = reset or call
    if reset:
        last = reset
        while self.b.blocks[last].get("next"):
            last = self.b.blocks[last]["next"]
        self.b.blocks[last]["next"] = call
        self.b.blocks[call]["parent"] = last
    else:
        self.b.blocks[call]["parent"] = hat


_old_compiler_compile_console_flag_loop_patch25 = Compiler.compile_console_flag_loop

def _compiler_compile_console_flag_loop_patch25(self: Compiler) -> None:
    if getattr(self, "single_cpp_main_body", None) is not None:
        return _compiler_compile_console_flag_loop_single_main_patch25(self)
    return _old_compiler_compile_console_flag_loop_patch25(self)

Compiler.compile_console_flag_loop = _compiler_compile_console_flag_loop_patch25  # type: ignore[method-assign]


_prev_compile_call_stmt_patch23 = ScratchBuilder.compile_call_stmt

def _compile_call_stmt_patch23(self: ScratchBuilder, expr: CallExpr) -> Optional[str]:
    name, a = expr.callee, expr.args
    if name == "showTerminal":
        self.need_args(name, a, 0)
        return _sbg_make_terminal_visible_setter_patch23(self, True)
    if name == "hideTerminal":
        self.need_args(name, a, 0)
        return _sbg_make_terminal_visible_setter_patch23(self, False)
    if name in {"showInputPrompt", "enableInputPrompt", "enableTerminalInput"}:
        self.need_args(name, a, 0)
        return _sbg_make_prompt_visible_setter_patch23(self, True)
    if name in {"hideInputPrompt", "disableInputPrompt", "disableTerminalInput"}:
        self.need_args(name, a, 0)
        return _sbg_make_prompt_visible_setter_patch23(self, False)
    if name in {"setInputPromptVisible", "setTerminalInputEnabled"}:
        self.need_args(name, a, 1)
        cond = BinaryExpr(a[0], "!=", Literal(0))
        then_first = _sbg_make_prompt_visible_setter_patch23(self, True)
        else_first = _sbg_make_prompt_visible_setter_patch23(self, False)
        return _sbg_if_else_block(self, cond, then_first, else_first)
    if name == "showTerminalAndPrompt":
        self.need_args(name, a, 0)
        return _sbg_make_terminal_show_all_patch23(self)
    if name == "hideTerminalAndPrompt":
        self.need_args(name, a, 0)
        return _sbg_make_terminal_hide_all_patch23(self)
    if name == "toggleTerminal":
        self.need_args(name, a, 0)
        cond = BinaryExpr(VarExpr(TERMINAL_VISIBLE_VAR), "==", Literal(1))
        then_first = _sbg_make_terminal_visible_setter_patch23(self, False)
        else_first = _sbg_make_terminal_visible_setter_patch23(self, True)
        return _sbg_if_else_block(self, cond, then_first, else_first)
    return _prev_compile_call_stmt_patch23(self, expr)

ScratchBuilder.compile_call_stmt = _compile_call_stmt_patch23  # type: ignore[method-assign]


_prev_compile_call_expr_patch23 = ScratchBuilder.compile_call_expr

def _compile_call_expr_patch23(self: ScratchBuilder, expr: CallExpr, parent: Optional[str]) -> str:
    if expr.callee == "terminalVisible":
        self.need_args(expr.callee, expr.args, 0)
        return self.compile_expr(VarExpr(TERMINAL_VISIBLE_VAR), parent)
    if expr.callee == "terminalPromptVisible":
        self.need_args(expr.callee, expr.args, 0)
        return self.compile_expr(VarExpr(TERMINAL_INPUT_ENABLED_VAR), parent)
    return _prev_compile_call_expr_patch23(self, expr, parent)

ScratchBuilder.compile_call_expr = _compile_call_expr_patch23  # type: ignore[method-assign]


_prev_runtime_call_patch23 = Runtime.call

def _runtime_call_patch23(self: Runtime, name: str, args: List[Any]) -> Any:
    if not hasattr(self, "_sbg_terminal_visible"):
        self._sbg_terminal_visible = True
    if not hasattr(self, "_sbg_terminal_input_enabled"):
        self._sbg_terminal_input_enabled = True
    if name == "showTerminal": self._sbg_terminal_visible = True; self.vars[TERMINAL_VISIBLE_VAR] = 1; return None
    if name == "hideTerminal": self._sbg_terminal_visible = False; self.vars[TERMINAL_VISIBLE_VAR] = 0; return None
    if name in {"showInputPrompt", "enableInputPrompt", "enableTerminalInput"}: self._sbg_terminal_input_enabled = True; self.vars[TERMINAL_INPUT_ENABLED_VAR] = 1; return None
    if name in {"hideInputPrompt", "disableInputPrompt", "disableTerminalInput"}: self._sbg_terminal_input_enabled = False; self.vars[TERMINAL_INPUT_ENABLED_VAR] = 0; return None
    if name in {"setInputPromptVisible", "setTerminalInputEnabled"}:
        enabled = bool(args and float(args[0]) != 0)
        self._sbg_terminal_input_enabled = enabled; self.vars[TERMINAL_INPUT_ENABLED_VAR] = 1 if enabled else 0; return None
    if name == "showTerminalAndPrompt":
        self.call("showTerminal", []); self.call("showInputPrompt", []); return None
    if name == "hideTerminalAndPrompt":
        self.call("hideTerminal", []); self.call("hideInputPrompt", []); return None
    if name == "toggleTerminal":
        if getattr(self, "_sbg_terminal_visible", True): self.call("hideTerminal", [])
        else: self.call("showTerminal", [])
        return None
    if name == "terminalVisible": return 1 if getattr(self, "_sbg_terminal_visible", True) else 0
    if name == "terminalPromptVisible": return 1 if getattr(self, "_sbg_terminal_input_enabled", True) else 0
    return _prev_runtime_call_patch23(self, name, args)

Runtime.call = _runtime_call_patch23  # type: ignore[method-assign]


_prev_sbg_method_lower_patch23 = _sbg_method_lower_patch19

def _sbg_method_lower_patch19(receiver: Any, method: str, args: List[Any], parser: Optional[Parser] = None) -> CallExpr:  # type: ignore[no-redef]
    rname = receiver.name if isinstance(receiver, VarExpr) else ""
    if rname in {"console", "terminal"}:
        mapping = {
            "log": "log", "info": "logInfo", "warn": "logWarn", "error": "logError",
            "sep": "logSeparator", "header": "logHeader", "clear": "clearTerminal",
            "show": "showTerminal", "hide": "hideTerminal", "toggle": "toggleTerminal",
            "show_prompt": "showInputPrompt", "showPrompt": "showInputPrompt", "enable_prompt": "showInputPrompt",
            "enablePrompt": "showInputPrompt", "enable_input": "enableTerminalInput", "enableInput": "enableTerminalInput",
            "hide_prompt": "hideInputPrompt", "hidePrompt": "hideInputPrompt", "disable_prompt": "hideInputPrompt",
            "disablePrompt": "hideInputPrompt", "disable_input": "disableTerminalInput", "disableInput": "disableTerminalInput",
            "input": "setTerminalInputEnabled", "set_input": "setTerminalInputEnabled", "setInput": "setTerminalInputEnabled",
            "show_all": "showTerminalAndPrompt", "showAll": "showTerminalAndPrompt",
            "hide_all": "hideTerminalAndPrompt", "hideAll": "hideTerminalAndPrompt",
            "visible": "terminalVisible", "is_visible": "terminalVisible", "isVisible": "terminalVisible",
            "prompt_visible": "terminalPromptVisible", "promptVisible": "terminalPromptVisible",
            "input_enabled": "terminalPromptVisible", "inputEnabled": "terminalPromptVisible",
        }
        if method in mapping:
            return _sbg_call_patch19(mapping[method], args, receiver)
    return _prev_sbg_method_lower_patch23(receiver, method, args, parser)

Parser.parse_postfix = _parser_parse_postfix_patch20  # type: ignore[method-assign]


_prev_runtime_run_scratch_terminal_patch23 = Runtime.run_scratch_terminal

def _runtime_run_scratch_terminal_patch23(self: Runtime, *, prompt: str = "sbg> ") -> None:
    self.prepare_scratch_console()
    if not self.action_events:
        raise RuntimeSBGError("nothing runnable: no Action(Input) body was prepared")
    print("StageBG native terminal. Type /exit or press Ctrl+D to quit.")
    while True:
        if not getattr(self, "_sbg_terminal_input_enabled", True):
            print("[input prompt hidden; native terminal stopped]")
            break
        try:
            line = input(prompt)
        except EOFError:
            print()
            break
        if line in ("/exit", ":q", "quit", "exit"):
            break
        self.call("log", ["> " + str(line)])
        self.run_action(line)
        if getattr(self, "last_action_returned", False):
            self.call("log", ["=> " + str(getattr(self, "last_action_return_value", ""))])

Runtime.run_scratch_terminal = _runtime_run_scratch_terminal_patch23  # type: ignore[method-assign]


_prev_project_ensure_patch23 = _project_ensure_patch17 if "_project_ensure_patch17" in globals() else (lambda project: project)

def _project_ensure_patch23(project: Dict[str, Any]) -> Dict[str, Any]:
    project = _prev_project_ensure_patch23(project)
    meta = project.setdefault("meta", {})
    meta["agent"] = f"StageBG/SBG {VERSION}"
    meta["stagebgPatch23"] = "Dynamic terminal monitor and input-prompt visibility; terminal.show()/hide()/hidePrompt()/showPrompt()."
    return project

_prev_compiler_compile_patch23 = Compiler.compile

def _compiler_compile_patch23(self: Compiler) -> Dict[str, Any]:
    return _project_ensure_patch23(_prev_compiler_compile_patch23(self))

Compiler.compile = _compiler_compile_patch23  # type: ignore[method-assign]



