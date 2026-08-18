# =============================================================================
# Patch 19: dot-method surface for professional APIs
# =============================================================================
# Public APIs should look like normal programming interfaces:
#   v.push_back(x), v.sort(), v.size()
#   pq.push(priority, value), pq.top(), pq.pop()
#   dsu.unite(a, b), fw.sum(i), files.read("config.txt"), pen.down()
# The compiler lowers these method calls to the existing vanilla-Scratch-safe
# functions/procedures.  The underscore names remain only as ABI/internal aliases.

VERSION = "0.9.0-patch19-dot-methods"
KEYWORDS.update({"priority_queue", "max_priority_queue", "stack", "queue", "deque", "fenwick", "bit", "dsu"})

_SBG_OBJECT_TYPE_NAMES_PATCH19 = {"priority_queue", "max_priority_queue", "stack", "queue", "deque", "fenwick", "bit", "dsu"}


def _sbg_method_copy_loc_patch19(dst: Any, src: Any) -> Any:
    for attr in ("filename", "line", "col"):
        if hasattr(src, attr):
            setattr(dst, attr, getattr(src, attr))
    return dst


def _sbg_call_patch19(name: str, args: List[Any], loc: Any) -> CallExpr:
    return _sbg_method_copy_loc_patch19(CallExpr(name, args), loc)


def _sbg_method_lower_patch19(receiver: Any, method: str, args: List[Any], parser: Optional[Parser] = None) -> CallExpr:
    """Lower obj.method(args...) into an ordinary CallExpr.

    Two families are supported:
    1. Real list/vector methods: receiver is passed as first argument.
    2. Scratch-compatible singleton containers such as pq/dsu/fw/files/pen, where
       the receiver name selects a hidden global implementation.
    """
    rname = receiver.name if isinstance(receiver, VarExpr) else ""
    rtype = ""
    if parser is not None:
        rtype = getattr(parser, "sbg_object_types", {}).get(rname, "")

    # vector/list/string-ish methods. These are the normal public names; they
    # lower to existing builtins/std functions that already compile to vanilla blocks.
    list_methods = {
        "push": "push", "add": "push", "push_back": "push_back",
        "pop": "pop_back", "pop_back": "pop_back", "pop_front": "pop_front",
        "clear": "clear", "erase": "erase", "insert": "insert_at", "insert_at": "insert_at",
        "set": "setItem", "set_at": "setItem", "replace": "setItem",
        "resize": "resize", "assign": "assign", "fill": "fill",
        "swap": "swap_items", "swap_items": "swap_items",
        "sort": "sort", "sort_desc": "sort_desc", "reverse": "reverse",
        "size": "size", "len": "size", "empty": "empty",
        "front": "front", "back": "back", "at": "at", "get": "at",
        "contains": "contains", "lower_bound": "lower_bound", "upper_bound": "upper_bound", "binary_search": "binary_search",
    }

    # Special singleton containers. These names intentionally read like object APIs,
    # but compile to global Scratch lists because Scratch has no real object/list refs.
    pq_methods = {
        "clear": "pq_clear", "size": "pq_size", "empty": "pq_empty",
        "push": "pq_push", "pop": "pq_pop", "top": "pq_top", "top_key": "pq_top_key",
        "popped": "pq_popped", "popped_key": "pq_popped_key", "error": "pq_error_value",
    }
    maxpq_methods = {
        "clear": "maxpq_clear", "size": "maxpq_size", "empty": "maxpq_empty",
        "push": "maxpq_push", "pop": "maxpq_pop", "top": "maxpq_top", "top_key": "maxpq_top_key",
        "popped": "maxpq_popped", "popped_key": "maxpq_popped_key", "error": "maxpq_error_value",
    }
    dsu_methods = {
        "init": "make_set", "make_set": "make_set", "find": "find_set", "find_set": "find_set",
        "unite": "unite", "union": "unite", "same": "same", "size": "comp_size", "component_size": "comp_size",
    }
    fw_methods = {
        "init": "fw_init", "add": "fw_add", "sum": "fw_sum", "range": "fw_range", "range_sum": "fw_range",
    }
    dq_methods = {
        "clear": "dqClear", "size": "dqSize", "empty": "dqEmpty",
        "push_back": "dqPushBack", "push_front": "dqPushFront",
        "front": "dqFront", "back": "dqBack", "pop_front": "dqPopFront", "pop_back": "dqPopBack",
        "push": "dqPushBack", "pop": "dqPopFront",
    }
    st_methods = {"clear":"stClear", "push":"stPush", "top":"stTop", "pop":"stPop", "empty":"stEmpty", "size":"stSize"}
    qu_methods = {"clear":"quClear", "push":"quPush", "front":"quFront", "pop":"quPop", "empty":"quEmpty", "size":"quSize"}
    file_methods = {
        "count": "fileCount", "name": "fileName", "exists": "fileExists", "open": "fileOpen",
        "read": "fileReadAll", "read_all": "fileReadAll", "size": "fileSize", "lines": "fileLines",
        "line": "fileLine", "read_line": "fileReadLine", "contains": "fileContains",
        "dump": "fileDump", "debug": "fileDebugList", "list": "fileDebugList",
    }
    pen_methods = {
        "reset": "penReset", "use": "penUse", "clear": "penClear", "erase": "penClear",
        "down": "penDown", "up": "penUp", "stamp": "penStamp",
        "color": "penSetColor", "set_color": "penSetColor", "size": "penSetSize", "set_size": "penSetSize",
        "change_size": "penChangeSize", "param": "penSetParam", "change_param": "penChangeParam",
        "hue": "penSetHue", "change_hue": "penChangeHue", "saturation": "penSetSaturation",
        "change_saturation": "penChangeSaturation", "brightness": "penSetBrightness",
        "change_brightness": "penChangeBrightness", "transparency": "penSetTransparency",
        "change_transparency": "penChangeTransparency", "line": "penLine", "rect": "penRect",
        "filled_rect": "penFilledRect", "circle": "penCircle", "filled_circle": "penFilledCircle",
        "grid": "penGrid", "axes": "penAxes", "point": "penPoint", "points_clear": "penPointsClear",
        "polyline": "penPolylineFromPoints", "goto_draw": "penGotoDraw",
    }
    console_methods = {"log":"log", "info":"logInfo", "warn":"logWarn", "error":"logError", "sep":"logSeparator", "header":"logHeader", "clear":"clearTerminal"}
    sprite_methods = {
        "set_x":"setX", "setX":"setX", "set_y":"setY", "setY":"setY", "change_x":"changeX", "changeX":"changeX",
        "change_y":"changeY", "changeY":"changeY", "goto":"goToXY", "go_to":"goToXY", "goToXY":"goToXY",
        "x":"x", "y":"y", "move":"move", "turn_right":"turnRight", "turnRight":"turnRight",
        "turn_left":"turnLeft", "turnLeft":"turnLeft", "direction":"direction", "set_direction":"setDirection", "setDirection":"setDirection",
        "show":"show", "hide":"hide", "size":"size", "set_size":"setSize", "setSize":"setSize",
    }

    singleton: Optional[Dict[str, str]] = None
    if rname == "pq" or rtype == "priority_queue": singleton = pq_methods
    elif rname == "maxpq" or rtype == "max_priority_queue": singleton = maxpq_methods
    elif rname == "dsu" or rtype == "dsu": singleton = dsu_methods
    elif rname in {"fw", "bit", "fenwick"} or rtype in {"fenwick", "bit"}: singleton = fw_methods
    elif rname in {"dq", "deque"} or rtype == "deque": singleton = dq_methods
    elif rname in {"st", "stack"} or rtype == "stack": singleton = st_methods
    elif rname in {"qu", "queue"} or rtype == "queue": singleton = qu_methods
    elif rname in {"file", "files", "fs"}: singleton = file_methods
    elif rname == "pen": singleton = pen_methods
    elif rname == "console": singleton = console_methods
    elif rname in {"this", "sprite"}: singleton = sprite_methods

    if singleton and method in singleton:
        return _sbg_call_patch19(singleton[method], args, receiver)

    if method in list_methods:
        return _sbg_call_patch19(list_methods[method], [receiver, *args], receiver)

    # Final fallback: obj.foo(a,b) -> foo(obj,a,b).  This is useful for userland
    # libraries that deliberately write free functions but want method syntax.
    return _sbg_call_patch19(method, [receiver, *args], receiver)


_old_parser_init_patch19 = Parser.__init__

def _parser_init_patch19(self: Parser, tokens: List[Token], filename: str = "<source>"):
    _old_parser_init_patch19(self, tokens, filename)
    self.sbg_object_types: Dict[str, str] = {}

Parser.__init__ = _parser_init_patch19  # type: ignore[method-assign]


_old_parse_statement_patch19 = Parser.parse_statement

def _parser_parse_statement_patch19(self: Parser) -> Any:
    start_token = self.peek()
    if self.peek().value in _SBG_OBJECT_TYPE_NAMES_PATCH19 and self.peek().kind in {"KW", "IDENT"}:
        typ = self.advance().value
        _parser_skip_template_patch18(self)
        name = self.expect_ident()
        # Optional empty constructor syntax: priority_queue pq(); / stack st;
        if self.match("("):
            self.expect(")")
        self.expect(";")
        self.sbg_object_types[name] = typ
        # It is a compile-time handle to Scratch-global storage.  A harmless hidden
        # variable keeps native runtime happy if the name is ever logged/debugged.
        return self.loc(VarDecl(name, Literal(0), True, type=typ), start_token)
    return _old_parse_statement_patch19(self)

Parser.parse_statement = _parser_parse_statement_patch19  # type: ignore[method-assign]


_old_parse_postfix_patch19 = Parser.parse_postfix

def _parser_parse_postfix_patch19(self: Parser) -> Any:
    expr = self.parse_primary()
    while True:
        if self.match("("):
            if not isinstance(expr, VarExpr):
                raise self.error("only named function calls are supported")
            args: List[Any] = []
            if not self.at(")"):
                while True:
                    args.append(self.parse_expr())
                    if not self.match(","):
                        break
            self.expect(")")
            expr = self.loc(CallExpr(expr.name, args), expr)
            continue
        if self.match("."):
            dot_token = self.toks[self.i - 1]
            if self.peek().kind not in {"IDENT", "KW"}:
                raise self.error("expected method name after '.'")
            method = self.advance().value
            args: List[Any] = []
            if self.match("("):
                if not self.at(")"):
                    while True:
                        args.append(self.parse_expr())
                        if not self.match(","):
                            break
                self.expect(")")
            else:
                raise self.error("method access needs a call, e.g. v.size()")
            expr = self.loc(_sbg_method_lower_patch19(expr, method, args, self), dot_token)
            continue
        break
    return expr

Parser.parse_postfix = _parser_parse_postfix_patch19  # type: ignore[method-assign]


# Extra public aliases that method syntax lowers to.  They are intentionally
# compact/professional, not educational names.
BUILTIN_EXPR_NAMES.update({"pq_error_value", "maxpq_error_value"})
BUILTIN_NAMES = BUILTIN_EXPR_NAMES | BUILTIN_STMT_NAMES

_old_runtime_call_patch19 = Runtime.call

def _runtime_call_patch19(self: Runtime, name: str, args: List[Any]) -> Any:
    if name == "pq_error_value":
        return self.vars.get("pq_error", "")
    if name == "maxpq_error_value":
        return self.vars.get("maxpq_error", "")
    return _old_runtime_call_patch19(self, name, args)

Runtime.call = _runtime_call_patch19  # type: ignore[method-assign]

_old_project_ensure_patch19 = _project_ensure_patch18

def _project_ensure_patch19(project: Dict[str, Any]) -> Dict[str, Any]:
    project = _old_project_ensure_patch19(project)
    meta = project.setdefault("meta", {})
    meta["agent"] = f"StageBG/SBG {VERSION}"
    meta["stagebgPatch19"] = "Dot-method API surface: v.sort(), pq.push(), dsu.unite(), fw.sum(), files.read(), pen.down()."
    return project

_old_compiler_compile_patch19 = Compiler.compile

def _compiler_compile_patch19(self: Compiler) -> Dict[str, Any]:
    return _project_ensure_patch19(_old_compiler_compile_patch19(self))

Compiler.compile = _compiler_compile_patch19  # type: ignore[method-assign]



