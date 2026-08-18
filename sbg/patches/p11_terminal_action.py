# =============================================================================
# Patch 11: terminal echo + Action(input) return values
# =============================================================================

ACTION_RETURN_VAR = "__sbg_ret_Action"
ACTION_RETURN_FLAG = "__sbg_returning_Action"


def _sbg_action_entries_have_return(entries: List[Tuple[str, List[Any]]]) -> bool:
    return any(_sbg_body_has_return(body) for _param, body in entries)


def _builder_make_log_to_terminal(self: ScratchBuilder, expr: Any) -> str:
    self.list_id(TERMINAL_LIST_NAME)
    bid = self.add_block(
        "data_addtolist",
        fields={"LIST": [TERMINAL_LIST_NAME, self.list_id(TERMINAL_LIST_NAME)]},
        inputs={},
    )
    self.blocks[bid]["inputs"]["ITEM"] = self.expr_input(expr, bid)
    return bid


_old_compiler_analyze_patch11 = Compiler.analyze

def _compiler_analyze_patch11(self: Compiler) -> None:
    _old_compiler_analyze_patch11(self)
    if _sbg_action_entries_have_return(self.action_entries):
        self.b.var_id(ACTION_RETURN_VAR)
        self.b.var_id(ACTION_RETURN_FLAG)
        self.init_values.setdefault(ACTION_RETURN_VAR, "")
        self.init_values.setdefault(ACTION_RETURN_FLAG, 0)

Compiler.analyze = _compiler_analyze_patch11  # type: ignore[method-assign]


def _compiler_compile_console_flag_loop_patch11(self: Compiler) -> None:
    assert self.action_argid is not None
    has_action_return = _sbg_action_entries_have_return(self.action_entries)

    hat = self.b.add_block("event_whenflagclicked", topLevel=True, x=536, y=455)
    forever = self.b.add_block("control_forever", parent=hat, inputs={})
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

    self.b.blocks[hat]["next"] = forever
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

Compiler.compile_console_flag_loop = _compiler_compile_console_flag_loop_patch11  # type: ignore[method-assign]


def _compiler_compile_console_action_definition_patch11(self: Compiler) -> None:
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

    first: Optional[str] = None
    saved_params = dict(self.b.current_proc_params)
    saved_ret_var = getattr(self.b, "current_return_var", None)
    saved_ret_flag = getattr(self.b, "current_return_flag", None)

    if has_action_return:
        self.b.current_return_var = ACTION_RETURN_VAR
        self.b.current_return_flag = ACTION_RETURN_FLAG
        init_flag = _builder_make_set_var(self.b, ACTION_RETURN_FLAG, Literal(0))
        init_ret = _builder_make_set_var(self.b, ACTION_RETURN_VAR, Literal(""))
        first = self.b.chain(first, init_flag)
        first = self.b.chain(first, init_ret)
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

Compiler.compile_console_action_definition = _compiler_compile_console_action_definition_patch11  # type: ignore[method-assign]


# Native runner mirrors Scratch terminal behavior: echo the submitted command and
# print Action(input)'s return value as `=> value` when the action returns.
_OLD_NO_ACTION_RETURN = object()
_old_runtime_run_action_patch11 = Runtime.run_action

def _runtime_run_action_patch11(self: Runtime, input_value: str = "") -> Any:
    self.answer_value = input_value
    self.last_action_returned = False
    self.last_action_return_value = None
    for ev in self.action_events:
        param = ev.value or "Input"
        old_present = param in self.vars
        old_value = self.vars.get(param)
        self.vars[param] = input_value
        try:
            self.exec_block(ev.body)
        except ReturnSignal as r:
            self.last_action_returned = True
            self.last_action_return_value = r.value
            if old_present:
                self.vars[param] = old_value
            else:
                self.vars.pop(param, None)
            return r.value
        finally:
            if old_present:
                self.vars[param] = old_value
            else:
                self.vars.pop(param, None)
    return _OLD_NO_ACTION_RETURN

Runtime.run_action = _runtime_run_action_patch11  # type: ignore[method-assign]


def _runtime_terminal_echo_and_result(self: Runtime, input_value: str, result: Any) -> None:
    self.call("log", ["> " + str(input_value)])
    if getattr(self, "last_action_returned", False):
        self.call("log", ["=> " + str(getattr(self, "last_action_return_value", ""))])


def _runtime_run_scratch_once_patch11(self: Runtime, input_value: str = "") -> None:
    self.prepare_scratch_console()
    if not self.action_events:
        raise RuntimeSBGError("nothing runnable: no Action(Input) body was prepared")
    self.call("log", ["> " + str(input_value)])
    self.run_action(input_value)
    if getattr(self, "last_action_returned", False):
        self.call("log", ["=> " + str(getattr(self, "last_action_return_value", ""))])

Runtime.run_scratch_once = _runtime_run_scratch_once_patch11  # type: ignore[method-assign]


def _runtime_run_scratch_terminal_patch11(self: Runtime, *, prompt: str = "sbg> ") -> None:
    self.prepare_scratch_console()
    if not self.action_events:
        raise RuntimeSBGError("nothing runnable: no Action(Input) body was prepared")
    print("StageBG native terminal. Type /exit or press Ctrl+D to quit.")
    while True:
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

Runtime.run_scratch_terminal = _runtime_run_scratch_terminal_patch11  # type: ignore[method-assign]


