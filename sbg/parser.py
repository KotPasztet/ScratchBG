from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import globals as _g
from .errors import ImportSBGError, ParseError, attach_location
from .ast import (
    Token, Lexer, Program, ImportDecl, VarDecl, ListDecl, EventDecl, ProcDecl,
    TargetDecl, BlockStmt, IfStmt, RepeatStmt, ForeverStmt, WhileStmt, ForStmt,
    ReturnStmt, BreakStmt, ContinueStmt, AssignStmt, ExprStmt,
    Literal, VarExpr, BinaryExpr, UnaryExpr, CallExpr, ArrayExpr,
    LValueAssignStmt, StructDecl, StructVarDecl, NestedVectorDecl,
)
from .globals import PACKAGE_MANIFEST, SBG_MODULES_DIR

# Copied verbatim from _patches.py (patch17/18/19/21). Registry-per-Program
# migration for the struct/vector state is tracked separately (Phase 1 step 3);
# these remain process-global for now, exactly as in the original monkeypatch
# chain, to keep this step a pure code-motion copy.
_sbg_foreach_parse_counter = {"n": 0}
_CPP_TYPE_KWS = {"auto", "int", "long", "double", "float", "string", "bool", "char"}
_SBG_OBJECT_TYPE_NAMES_PATCH19 = {"priority_queue", "max_priority_queue", "stack", "queue", "deque", "fenwick", "bit", "dsu"}
_SBG_STRUCT_DEFS21: Dict[str, List[Tuple[str, str]]] = {}
_SBG_FLAT_VECTOR_TYPES21: Dict[str, str] = {}
# struct name -> the flat-vector base name it defaults to (first-seen wins).
# Consumed at runtime (see runtime.py) to resolve `__field_ref` on a bare
# struct index without an explicit base-vector name in scope.
_SBG_STRUCT_DEFAULT_BASE21: Dict[str, str] = {}
_SBG_NESTED_VECTOR_NAMES21: set = set()

# =============================================================================
# Parser
# =============================================================================

_SBG_KEY_ALIASES = {
    "space": "space", "spacja": "space",
    "any": "any", "dowolny": "any",
    "up": "up arrow", "up_arrow": "up arrow", "arrow_up": "up arrow", "up arrow": "up arrow",
    "down": "down arrow", "down_arrow": "down arrow", "arrow_down": "down arrow", "down arrow": "down arrow",
    "left": "left arrow", "left_arrow": "left arrow", "arrow_left": "left arrow", "left arrow": "left arrow",
    "right": "right arrow", "right_arrow": "right arrow", "arrow_right": "right arrow", "right arrow": "right arrow",
    "enter": "enter", "return": "enter",
}

def _sbg_normalize_key_name_patch24(value: Any) -> str:
    key = str(value)
    return _SBG_KEY_ALIASES.get(key, _SBG_KEY_ALIASES.get(key.lower(), key))

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

def _sbg_copy_loc(dst: Any, src: Any) -> Any:
    for attr in ("filename", "line", "col"):
        if hasattr(src, attr):
            setattr(dst, attr, getattr(src, attr))
    return dst

def _sbg_fresh_foreach_temp() -> str:
    _sbg_foreach_parse_counter["n"] += 1
    return f"__sbg_foreach_i_{_sbg_foreach_parse_counter['n']}"

def _sbg_field_ref(obj: Any, field: str) -> CallExpr:
    return CallExpr("__field_ref", [obj, Literal(field)])

def _sbg_method_lower_patch19_v0(receiver: Any, method: str, args: List[Any], parser: Optional[Parser] = None) -> CallExpr:
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

def _sbg_method_lower_patch19_v1(receiver: Any, method: str, args: List[Any], parser: Optional[Parser] = None) -> CallExpr:  # type: ignore[no-redef]
    # C++ vector row: matrix[i].size(), matrix[i].push_back(x)
    if isinstance(receiver, CallExpr) and receiver.callee == "__index0_ref" and len(receiver.args) == 2:
        base, row = receiver.args
        if isinstance(base, VarExpr) and base.name in _SBG_FLAT_VECTOR_TYPES21:
            if method in {"push", "push_back"}:
                return _sbg_call_patch19("__flat_struct_push", [Literal(base.name), row, *args], receiver)
            if method == "size":
                return _sbg_call_patch19("__flat_struct_row_size", [Literal(base.name), row], receiver)
    if method == "size" and isinstance(receiver, CallExpr) and receiver.callee == "__index0_ref":
        return _sbg_call_patch19("vec_size", [receiver], receiver)
    return _sbg_method_lower_patch19_v0(receiver, method, args, parser)

def _sbg_method_lower_patch19_v2(receiver: Any, method: str, args: List[Any], parser: Optional[Parser] = None) -> CallExpr:  # type: ignore[no-redef]
    if isinstance(receiver, VarExpr) and receiver.name in _SBG_FLAT_VECTOR_TYPES21 and method == "resize":
        return _sbg_call_patch19("__flat_struct_resize_outer", [Literal(receiver.name), *args], receiver)
    return _sbg_method_lower_patch19_v1(receiver, method, args, parser)

def _sbg_method_lower_patch19_v3(receiver: Any, method: str, args: List[Any], parser: Optional[Parser] = None) -> CallExpr:  # type: ignore[no-redef]
    if isinstance(receiver, VarExpr) and receiver.name in _SBG_NESTED_VECTOR_NAMES21 and method == "resize":
        return _sbg_call_patch19("__nested_resize_outer", [Literal(receiver.name), *args], receiver)
    if isinstance(receiver, CallExpr) and receiver.callee == "__index0_ref" and len(receiver.args) == 2:
        base, row = receiver.args
        if isinstance(base, VarExpr) and base.name in _SBG_NESTED_VECTOR_NAMES21:
            if method in {"push", "push_back"}:
                return _sbg_call_patch19("__nested_row_push", [Literal(base.name), row, *args], receiver)
            if method == "clear":
                return _sbg_call_patch19("__nested_row_clear", [Literal(base.name), row], receiver)
            if method == "size":
                return _sbg_call_patch19("vec_size", [receiver], receiver)
    return _sbg_method_lower_patch19_v2(receiver, method, args, parser)

def _sbg_method_lower_patch19_v4(receiver: Any, method: str, args: List[Any], parser: Optional[Parser] = None) -> CallExpr:  # type: ignore[no-redef]
    if isinstance(receiver, VarExpr) and receiver.name in _SBG_FLAT_VECTOR_TYPES21 and method == "resize":
        return _sbg_call_patch19("__flat_struct_resize_outer", [Literal(receiver.name), *args], receiver)
    if isinstance(receiver, CallExpr) and receiver.callee == "__index0_ref" and len(receiver.args) == 2:
        base, row = receiver.args
        if isinstance(base, VarExpr) and base.name in _SBG_FLAT_VECTOR_TYPES21:
            if method in {"push", "push_back"}:
                return _sbg_call_patch19("__flat_struct_push", [Literal(base.name), row, *args], receiver)
            if method == "size":
                return _sbg_call_patch19("__flat_struct_row_size", [Literal(base.name), row], receiver)
    return _sbg_method_lower_patch19_v3(receiver, method, args, parser)

def _sbg_method_lower_patch19_v5(receiver: Any, method: str, args: List[Any], parser: Optional[Parser] = None) -> CallExpr:  # type: ignore[no-redef]
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
    return _sbg_method_lower_patch19_v4(receiver, method, args, parser)

def _sbg_method_lower_patch19_v6(receiver: Any, method: str, args: List[Any], parser: Optional[Parser] = None) -> CallExpr:  # type: ignore[no-redef]
    rname = receiver.name if isinstance(receiver, VarExpr) else ""
    if rname in {"keyboard", "keys", "key"}:
        if method in {"pressed", "down", "isPressed", "is_down", "isDown"}:
            return _sbg_call_patch19("keyPressed", args, receiver)
    return _sbg_method_lower_patch19_v5(receiver, method, args, parser)

# Public entry point: late-binding in the original file meant every
# bare call to _sbg_method_lower_patch19 resolved to this final layer.
_sbg_method_lower_patch19 = _sbg_method_lower_patch19_v6

def _sbg_call_patch19(name: str, args: List[Any], loc: Any) -> CallExpr:
    return _sbg_method_copy_loc_patch19(CallExpr(name, args), loc)

def _sbg_method_copy_loc_patch19(dst: Any, src: Any) -> Any:
    for attr in ("filename", "line", "col"):
        if hasattr(src, attr):
            setattr(dst, attr, getattr(src, attr))
    return dst

def _sbg_index_ref(obj: Any, index: Any) -> CallExpr:
    return CallExpr("__index0_ref", [obj, index])

def _sbg_norm_type21(t: str) -> str:
    return re.sub(r"\s+", "", t.replace("std::", "")).strip()

def _sbg_default_for_type21(typ: str) -> Any:
    typ = _sbg_norm_type21(typ)
    if typ in {"string", "char"}:
        return Literal("")
    if typ == "bool":
        return Literal(False)
    return Literal(0)

def _sbg_is_nested_vector21(t: str) -> bool:
    inner = _sbg_vector_inner21(t)
    return inner is not None and _sbg_vector_inner21(inner) is not None

def _sbg_vector_inner21(t: str) -> Optional[str]:
    t = _sbg_norm_type21(t)
    if not t.startswith("vector<") or not t.endswith(">"):
        return None
    inner = t[len("vector<"):-1]
    return inner

def _sbg_is_vector21(t: str) -> bool:
    return _sbg_vector_inner21(t) is not None

def _sbg_is_nested_vector_of_struct21(t: str) -> Optional[str]:
    inner = _sbg_vector_inner21(t)
    if inner is None:
        return None
    return _sbg_is_vector_of_struct21(inner)

def _sbg_is_vector_of_struct21(t: str) -> Optional[str]:
    inner = _sbg_vector_inner21(t)
    if inner is None:
        return None
    inner = _sbg_norm_type21(inner)
    return inner if inner in _SBG_STRUCT_DEFS21 else None


class Parser:
    def _base___init__(self, tokens: List[Token], filename: str = "<source>"):
        self.toks = tokens
        self.i = 0
        self.filename = filename

    def peek(self, n: int = 0) -> Token:
        j = self.i + n
        return self.toks[j] if j < len(self.toks) else self.toks[-1]

    def at(self, value: str) -> bool:
        return self.peek().value == value

    def kind(self, kind: str, value: Optional[str] = None) -> bool:
        t = self.peek()
        return t.kind == kind and (value is None or t.value == value)

    def advance(self) -> Token:
        t = self.peek()
        self.i += 1
        return t

    def match(self, value: str) -> bool:
        if self.peek().value == value:
            self.advance()
            return True
        return False

    def match_kw(self, value: str) -> bool:
        if self.kind("KW", value):
            self.advance()
            return True
        return False

    def expect(self, value: str) -> Token:
        if not self.match(value):
            raise self.error(f"expected {value!r}, got {self.peek().value!r}")
        return self.toks[self.i - 1]

    def expect_ident(self) -> str:
        if self.peek().kind != "IDENT":
            raise self.error(f"expected identifier, got {self.peek().value!r}")
        return self.advance().value

    def error(self, msg: str) -> ParseError:
        t = self.peek()
        return ParseError(f"{self.filename}:{t.line}:{t.col}: {msg}")

    def loc(self, node: Any, token: Any) -> Any:
        setattr(node, "filename", getattr(token, "filename", self.filename))
        setattr(node, "line", getattr(token, "line", 1))
        setattr(node, "col", getattr(token, "col", 1))
        return node

    def parse(self) -> Program:
        body: List[Any] = []
        while self.peek().kind != "EOF":
            body.append(self.parse_top_or_stmt())
        return Program(body)

    def _base_parse_top_or_stmt(self) -> Any:
        if self.match_kw("import") or self.match_kw("use"):
            return self.parse_import(self.toks[self.i - 1])
        if self.match_kw("on"):
            return self.parse_event(self.toks[self.i - 1])
        if self.match_kw("proc") or self.match_kw("fn"):
            return self.parse_proc(self.toks[self.i - 1])
        return self.parse_statement()

    def parse_import(self, start_token: Token) -> ImportDecl:
        spec = self.expect_string_value()
        self.expect(";")
        return self.loc(ImportDecl(spec), start_token)

    def _base_parse_event(self, start_token: Token) -> EventDecl:
        if self.match_kw("flag") or self.match_kw("start"):
            return self.loc(EventDecl("flag", None, self.parse_block()), start_token)
        if self.match_kw("action"):
            # Console entrypoint. The generated .sb3 asks for terminal input, then calls
            # Scratch procedure: Action(input).
            param = "Input"
            if self.match("("):
                param = self.expect_ident()
                self.expect(")")
            return self.loc(EventDecl("action", param, self.parse_block()), start_token)
        if self.match_kw("message"):
            value: Optional[str]
            if self.match("("):
                value = self.expect_string_value()
                self.expect(")")
            else:
                value = self.expect_string_value()
            return self.loc(EventDecl("message", value, self.parse_block()), start_token)
        raise self.error("expected event type: flag/start/action/message")

    def parse_proc(self, start_token: Token) -> ProcDecl:
        name = self.expect_ident()
        self.expect("(")
        params: List[str] = []
        if not self.at(")"):
            while True:
                params.append(self.expect_ident())
                if not self.match(","):
                    break
        self.expect(")")
        return self.loc(ProcDecl(name, params, self.parse_block()), start_token)

    def parse_block(self) -> List[Any]:
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

    def expect_string_value(self) -> str:
        if self.peek().kind != "STRING":
            raise self.error(f"expected string, got {self.peek().value!r}")
        return self.advance().value

    def _base_parse_statement(self) -> Any:
        start_token = self.peek()
        if self.match_kw("let"):
            name = self.expect_ident()
            expr = Literal(0)
            if self.match("="):
                expr = self.parse_expr()
            self.expect(";")
            return self.loc(VarDecl(name, expr, True), start_token)
        if self.match_kw("var"):
            name = self.expect_ident()
            expr = Literal(0)
            if self.match("="):
                expr = self.parse_expr()
            self.expect(";")
            return self.loc(VarDecl(name, expr, True), start_token)
        if self.match_kw("const"):
            name = self.expect_ident()
            self.expect("=")
            expr = self.parse_expr()
            self.expect(";")
            return self.loc(VarDecl(name, expr, False), start_token)
        if self.match_kw("list"):
            name = self.expect_ident()
            items: List[Any] = []
            if self.match("="):
                arr = self.parse_expr()
                if not isinstance(arr, ArrayExpr):
                    raise self.error("list declaration needs an array literal, e.g. list xs = [1,2,3];")
                items = arr.items
            self.expect(";")
            return self.loc(ListDecl(name, items), start_token)
        if self.match_kw("if"):
            self.expect("(")
            cond = self.parse_expr()
            self.expect(")")
            then_body = self.parse_block()
            else_body: Optional[List[Any]] = None
            if self.match_kw("else"):
                # C-style `else if (...) { ... }` chains: parse the nested if
                # recursively instead of requiring `{` directly after `else`.
                if self.kind("KW", "if"):
                    else_body = [self.parse_statement()]
                else:
                    else_body = self.parse_block()
            return self.loc(IfStmt(cond, then_body, else_body), start_token)
        if self.match_kw("repeat"):
            self.expect("(")
            count = self.parse_expr()
            self.expect(")")
            return self.loc(RepeatStmt(count, self.parse_block()), start_token)
        if self.match_kw("forever"):
            return self.loc(ForeverStmt(self.parse_block()), start_token)
        if self.match_kw("while"):
            self.expect("(")
            cond = self.parse_expr()
            self.expect(")")
            return self.loc(WhileStmt(cond, self.parse_block()), start_token)
        if self.match_kw("for"):
            return self.parse_for(start_token)
        if self.match_kw("return"):
            expr = None if self.at(";") else self.parse_expr()
            self.expect(";")
            return self.loc(ReturnStmt(expr), start_token)
        if self.match_kw("break"):
            self.expect(";")
            return self.loc(BreakStmt(), start_token)
        if self.match_kw("continue"):
            self.expect(";")
            return self.loc(ContinueStmt(), start_token)
        if self.peek().kind == "IDENT" and self.peek(1).value in ("=", "+=", "-=", "*=", "/=", "%="):
            name = self.advance().value
            op = self.advance().value
            expr = self.parse_expr()
            self.expect(";")
            return self.loc(AssignStmt(name, op, expr), start_token)
        expr = self.parse_expr()
        self.expect(";")
        return self.loc(ExprStmt(expr), start_token)

    def _base_parse_for(self, start_token: Token) -> ForStmt:
        self.expect("(")
        init: Optional[Any] = None
        if not self.at(";"):
            if self.match_kw("let") or self.match_kw("var"):
                name = self.expect_ident()
                expr = Literal(0)
                if self.match("="):
                    expr = self.parse_expr()
                init = VarDecl(name, expr, True)
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
            if self.peek().kind == "IDENT" and self.peek(1).value in ("=", "+=", "-=", "*=", "/=", "%="):
                name = self.advance().value
                op = self.advance().value
                update = AssignStmt(name, op, self.parse_expr())
            else:
                update = ExprStmt(self.parse_expr())
        self.expect(")")
        body = self.parse_block()
        return self.loc(ForStmt(init, cond, update, body), start_token)

    # Pratt parser
    PRECEDENCE = {
        "||": 1,
        "&&": 2,
        "==": 3, "!=": 3,
        "<": 4, "<=": 4, ">": 4, ">=": 4,
        "+": 5, "-": 5,
        "*": 6, "/": 6, "%": 6,
    }

    def parse_expr(self, min_prec: int = 1) -> Any:
        left = self.parse_unary()
        while True:
            op = self.peek().value
            prec = self.PRECEDENCE.get(op)
            if prec is None or prec < min_prec:
                break
            self.advance()
            right = self.parse_expr(prec + 1)
            left = self.loc(BinaryExpr(left, op, right), left)
        return left

    def parse_unary(self) -> Any:
        # NOTE: the kind check is required -- a *string literal* like "-"
        # or "!" has the same .value as these operators, and treating it as
        # unary broke expressions like `if (c == "-")`.
        if self.peek().kind == "SYM" and self.peek().value in ("!", "-"):
            token = self.advance()
            op = token.value
            return self.loc(UnaryExpr(op, self.parse_unary()), token)
        return self.parse_postfix()

    def parse_primary(self) -> Any:
        t = self.peek()
        if t.kind == "NUMBER":
            self.advance()
            if "." in t.value:
                return self.loc(Literal(float(t.value)), t)
            return self.loc(Literal(int(t.value)), t)
        if t.kind == "STRING":
            self.advance()
            return self.loc(Literal(t.value), t)
        if self.match_kw("true"):
            return self.loc(Literal(True), self.toks[self.i - 1])
        if self.match_kw("false"):
            return self.loc(Literal(False), self.toks[self.i - 1])
        if self.match_kw("null"):
            return self.loc(Literal(None), self.toks[self.i - 1])
        if t.kind == "IDENT":
            tok = self.advance()
            name = tok.value
            # `scratch::name` / `std::name` -- explicit builtin namespace prefix.
            # Purely cosmetic/documentary: it resolves to the same builtin as
            # the bare name, so existing (unprefixed) callers keep working.
            if self.peek().value == "::" and name in ("scratch", "std"):
                self.advance()  # consume '::'
                if self.peek().kind not in ("IDENT", "KW"):
                    raise self.error(f"expected a name after '{name}::'")
                name_tok = self.advance()
                return self.loc(VarExpr(name_tok.value), tok)
            return self.loc(VarExpr(name), tok)
        if self.match("("):
            expr = self.parse_expr()
            self.expect(")")
            return expr
        if self.match("["):
            items: List[Any] = []
            if not self.at("]"):
                while True:
                    items.append(self.parse_expr())
                    if not self.match(","):
                        break
            self.expect("]")
            return self.loc(ArrayExpr(items), t)
        raise self.error(f"expected expression, got {t.value!r}")
    def _parse_event_patch24(self, start_token: Token) -> EventDecl:
        # on key "space" { ... }
        # on key("space") { ... }
        # on key any { ... }
        if self.peek().value == "key":
            self.advance()
            if self.match("("):
                if self.peek().kind == "STRING":
                    value = self.advance().value
                elif self.peek().kind in ("IDENT", "KW"):
                    value = self.advance().value
                else:
                    raise self.error("expected key name, e.g. on key(\"space\")")
                self.expect(")")
            else:
                if self.peek().kind == "STRING":
                    value = self.advance().value
                elif self.peek().kind in ("IDENT", "KW"):
                    value = self.advance().value
                else:
                    raise self.error("expected key name after `on key`, e.g. on key \"space\"")
            return self.loc(EventDecl("key", _sbg_normalize_key_name_patch24(value), self.parse_block()), start_token)
        return self._base_parse_event(start_token)

    def _parser_init_patch19(self, tokens: List[Token], filename: str = "<source>"):
        self._base___init__(tokens, filename)
        self.sbg_object_types: Dict[str, str] = {}

    def _parser_parse_cin_patch20(self, start_token: Token) -> Any:
        self.advance()  # cin
        stmts: List[Any] = []
        while self.match(">>"):
            if self.peek().kind != "IDENT":
                raise self.error("cin target must be an identifier")
            stmts.append(AssignStmt(self.advance().value, "=", CallExpr("cin_get", [])))
        self.expect(";")
        return self.loc(BlockStmt(stmts), start_token)

    def _parser_parse_cout_patch20(self, start_token: Token) -> Any:
        self.advance()  # cout
        parts: List[Any] = []
        while self.match("<<"):
            if self.peek().kind == "STRING":
                parts.append(self.parse_expr())
            elif self.peek().kind == "IDENT" and self.peek().value in {"endl"}:
                self.advance(); parts.append(Literal("\n"))
            else:
                parts.append(self.parse_expr())
        self.expect(";")
        if not parts:
            parts = [Literal("")]
        return self.loc(ExprStmt(CallExpr("cout", parts)), start_token)

    def _parser_parse_cpp_decl_or_func_patch20(self, start_token: Token) -> Any:  # type: ignore[no-redef]
        typ = _sbg_norm_type21(self._parser_read_template_type21())
        name = self.expect_ident()

        # Function definition or constructor-style variable declaration.
        if self.at("("):
            saved = self.i
            try:
                params = self._parser_parse_typed_params_patch20()
                ptypes = getattr(self, "_sbg_last_param_types21", {})
                if self.at("{"):
                    body = self.parse_block()
                    if name == "main":
                        ev = self.loc(EventDecl("action", "input", body), start_token)
                        setattr(ev, "return_type", typ)
                        setattr(ev, "param_types", ptypes)
                        setattr(ev, "sbg_is_cpp_main", True)
                        return ev
                    proc = self.loc(ProcDecl(name, params, body), start_token)
                    setattr(proc, "return_type", typ)
                    setattr(proc, "param_types", ptypes)
                    return proc
                self.i = saved
            except ParseError:
                self.i = saved
            # C++ object constructor expression: T x(args...);
            self.expect("(")
            ctor_args: List[Any] = []
            if not self.at(")"):
                while True:
                    ctor_args.append(self.parse_expr())
                    if not self.match(","):
                        break
            self.expect(")")
            self.expect(";")
            if _sbg_is_vector21(typ):
                if _sbg_is_nested_vector21(typ):
                    globals().setdefault("_SBG_NESTED_VECTOR_NAMES21", set()).add(name)
                if _sbg_is_nested_vector21(typ):
                    st_name = _sbg_is_nested_vector_of_struct21(typ)
                    if st_name:
                        _SBG_FLAT_VECTOR_TYPES21[name] = st_name
                        _SBG_STRUCT_DEFAULT_BASE21.setdefault(st_name, name)
                    node = NestedVectorDecl(name, typ, [])
                    setattr(node, "ctor_args", ctor_args)
                    return self.loc(node, start_token)
                node = ListDecl(name, [])
                setattr(node, "sbg_type", typ)
                setattr(node, "ctor_args", ctor_args)
                return self.loc(node, start_token)
            if typ in _SBG_STRUCT_DEFS21:
                return self.loc(StructVarDecl(typ, name), start_token)
            return self.loc(VarDecl(name, Literal(0), True), start_token)

        # Plain declaration.
        init: Any = _sbg_default_for_type21(typ)
        has_real_init = False
        if self.match("="):
            init = self._parser_parse_cpp_initializer_patch20()
            has_real_init = True
        self.expect(";")

        if typ in _SBG_STRUCT_DEFS21:
            # `Edge e;` or `Edge e = other;` -- the latter carries `init` so that
            # lowering can emit a field-by-field copy (see _sbg_expand_struct_var21).
            return self.loc(StructVarDecl(typ, name, init if has_real_init else None), start_token)

        if _sbg_is_vector21(typ):
            if _sbg_is_nested_vector21(typ):
                globals().setdefault("_SBG_NESTED_VECTOR_NAMES21", set()).add(name)
                if _sbg_is_nested_vector21(typ):
                    globals().setdefault("_SBG_NESTED_VECTOR_NAMES21", set()).add(name)
                if _sbg_is_nested_vector_of_struct21(typ):
                    _st_name21 = _sbg_is_nested_vector_of_struct21(typ) or ""
                    _SBG_FLAT_VECTOR_TYPES21[name] = _st_name21
                    if _st_name21:
                        _SBG_STRUCT_DEFAULT_BASE21.setdefault(_st_name21, name)
                rows: List[Any] = []
                if isinstance(init, ArrayExpr):
                    rows = init.items
                node = NestedVectorDecl(name, typ, rows)
                return self.loc(node, start_token)
            if isinstance(init, ArrayExpr):
                node = ListDecl(name, init.items)
                setattr(node, "sbg_type", typ)
                return self.loc(node, start_token)
            node = ListDecl(name, [])
            setattr(node, "sbg_type", typ)
            # BUGS_REPORT #6: keep the non-array initializer (e.g. a proc call)
            # so the post-parse type check can diagnose mismatches instead of
            # silently dropping it.
            if has_real_init:
                setattr(node, "sbg_init", init)
            return self.loc(node, start_token)

        node = VarDecl(name, init, True)
        setattr(node, "sbg_type", typ)
        return self.loc(node, start_token)

    def _parser_parse_cpp_initializer_patch20(self) -> Any:  # type: ignore[no-redef]
        if self.match("{"):
            items: List[Any] = []
            if not self.at("}"):
                while True:
                    items.append(self._parser_parse_cpp_initializer_patch20() if self.at("{") else self.parse_expr())
                    if not self.match(","):
                        break
            self.expect("}")
            return ArrayExpr(items)
        return self.parse_expr()

    def _parser_parse_cpp_struct_patch20(self, start_token: Token) -> Any:  # type: ignore[no-redef]
        name = self.expect_ident()
        self.expect("{")
        fields: List[Tuple[str, str]] = []
        while not self.at("}"):
            if self.peek().kind == "EOF":
                raise self.error("unterminated struct body")
            typ = self._parser_read_template_type21()
            # Allow multiple field declarations separated by commas: int a, b;
            while True:
                fname = self.expect_ident()
                # Arrays are treated as vector-like fields in the surface parser.
                if self.match("["):
                    while not self.at("]"):
                        self.advance()
                    self.expect("]")
                fields.append((_sbg_norm_type21(typ), fname))
                if not self.match(","):
                    break
            self.expect(";")
        self.expect("}")
        self.expect(";")
        _SBG_STRUCT_DEFS21[name] = fields
        if not hasattr(self, "sbg_structs"):
            self.sbg_structs = {}
        self.sbg_structs[name] = fields
        return self.loc(StructDecl(name, fields), start_token)

    def _parser_parse_for_init_or_update_patch18(self, *, terminators: set[str]) -> Optional[Any]:
        if self.peek().value in terminators:
            return None
        if self.peek().value in _CPP_TYPE_KWS and self.peek().kind in {"KW", "IDENT"}:
            typ = self.advance().value
            if typ == "long" and self.peek().kind == "KW" and self.peek().value == "long":
                self.advance()
            self._parser_skip_template_patch18()
            name = self.expect_ident()
            expr: Any = Literal(0 if typ != "string" else "")
            if self.match("="):
                expr = self.parse_expr()
            return VarDecl(name, expr, True)
        if self.peek().kind == "IDENT" and self.peek(1).value in ("++", "--"):
            name = self.advance().value
            op = self.advance().value
            return AssignStmt(name, "+=" if op == "++" else "-=", Literal(1))
        if self.peek().kind == "IDENT" and self.peek(1).value in ("=", "+=", "-=", "*=", "/=", "%="):
            name = self.advance().value
            op = self.advance().value
            return AssignStmt(name, op, self.parse_expr())
        return ExprStmt(self.parse_expr())

    def _parser_parse_for_patch17(self, start_token: Token) -> ForStmt:
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
                return self._base_parse_for(start_token)
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

    def _parser_parse_for_patch18(self, start_token: Token) -> ForStmt:
        saved = self.i
        self.expect("(")

        # C++ range-for: for (auto x : xs) / for (int x : xs)
        if self.peek().value in _CPP_TYPE_KWS | {"vector"} and self.peek().kind in {"KW", "IDENT"}:
            typ = self.advance().value
            if typ == "vector":
                self._parser_skip_template_patch18()
            elif typ == "long" and self.peek().kind == "KW" and self.peek().value == "long":
                self.advance()
            name = self.expect_ident()
            if self.match(":"):
                source = self.parse_expr()
                self.expect(")")
                return _sbg_make_for_each(start_token, name, source, self.parse_block(), declare_value=True)
            self.i = saved

        # C++ style for (int i = 0; i < n; i++) plus fixed ++/--.
        self.expect("(")
        init = self._parser_parse_for_init_or_update_patch18(terminators={";"})
        self.expect(";")
        cond = None if self.at(";") else self.parse_expr()
        self.expect(";")
        update = self._parser_parse_for_init_or_update_patch18(terminators={")"})
        self.expect(")")
        body = self.parse_block()
        return self.loc(ForStmt(init, cond, update, body), start_token)

    def _parser_parse_for_patch18b(self, start_token: Token) -> ForStmt:
        # Keep existing StageBG syntax: for (let x in xs), for (let i in range(...)).
        saved = self.i
        try:
            self.expect("(")
            if self.peek().kind == "KW" and self.peek().value in {"let", "var"}:
                self.i = saved
                return self._parser_parse_for_patch17(start_token)
        except Exception:
            pass
        self.i = saved
        return self._parser_parse_for_patch18(start_token)

    def _parser_parse_for_patch20(self, start_token: Token) -> ForStmt:
        saved = self.i
        self.expect("(")
        # for(double x : v) / for(Struct item : row)
        if self._parser_try_cpp_type_start_patch20():
            try:
                self._parser_skip_cpp_type_patch20()
                name = self.expect_ident()
                if self.match(":"):
                    source = self.parse_expr()
                    self.expect(")")
                    return _sbg_make_for_each(start_token, name, source, self.parse_block(), declare_value=True)
            except ParseError:
                pass
        self.i = saved
        return self._parser_parse_for_patch18b(start_token)

    def _parser_parse_initializer_items_patch18(self) -> List[Any]:
        items: List[Any] = []
        if not self.at("}"):
            while True:
                items.append(self.parse_expr())
                if not self.match(","):
                    break
        self.expect("}")
        return items

    def _parser_parse_postfix_patch20(self) -> Any:
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
            if self.match("["):
                tok = self.toks[self.i - 1]
                idx = self.parse_expr()
                self.expect("]")
                expr = self.loc(_sbg_index_ref(expr, idx), tok)
                continue
            if self.match("."):
                dot_token = self.toks[self.i - 1]
                if self.peek().kind not in {"IDENT", "KW"}:
                    raise self.error("expected field/method name after '.'")
                field_or_method = self.advance().value
                if self.match("("):
                    args: List[Any] = []
                    if not self.at(")"):
                        while True:
                            args.append(self.parse_expr())
                            if not self.match(","):
                                break
                    self.expect(")")
                    expr = self.loc(_sbg_method_lower_patch19(expr, field_or_method, args, self), dot_token)
                else:
                    expr = self.loc(_sbg_field_ref(expr, field_or_method), dot_token)
                continue
            break
        return expr

    def _parser_parse_statement_patch17(self) -> Any:
        start_token = self.peek()
        # i++; / i--;
        if self.peek().kind == "IDENT" and self.peek(1).value in ("++", "--"):
            name = self.advance().value
            op = self.advance().value
            self.expect(";")
            return self.loc(AssignStmt(name, "+=" if op == "++" else "-=", Literal(1)), start_token)
        return self._base_parse_statement()

    def _parser_parse_statement_patch18(self) -> Any:
        start_token = self.peek()
        # Fix patch17 standalone i++/i-- bug and keep it C++-style.
        if self.peek().kind == "IDENT" and self.peek(1).value in ("++", "--"):
            name = self.advance().value
            op = self.advance().value
            self.expect(";")
            return self.loc(AssignStmt(name, "+=" if op == "++" else "-=", Literal(1)), start_token)
        if self.peek().value in _CPP_TYPE_KWS | {"vector"} and self.peek().kind in {"KW", "IDENT"}:
            typ = self.advance().value
            return self._parser_parse_typed_decl_patch18(start_token, typ)
        return self._parser_parse_statement_patch17()

    def _parser_parse_statement_patch19(self) -> Any:
        start_token = self.peek()
        if self.peek().value in _SBG_OBJECT_TYPE_NAMES_PATCH19 and self.peek().kind in {"KW", "IDENT"}:
            typ = self.advance().value
            self._parser_skip_template_patch18()
            name = self.expect_ident()
            # Optional empty constructor syntax: priority_queue pq(); / stack st;
            if self.match("("):
                self.expect(")")
            self.expect(";")
            self.sbg_object_types[name] = typ
            # It is a compile-time handle to Scratch-global storage.  A harmless hidden
            # variable keeps native runtime happy if the name is ever logged/debugged.
            return self.loc(VarDecl(name, Literal(0), True), start_token)
        return self._parser_parse_statement_patch18()

    def _parser_parse_statement_patch20(self) -> Any:
        start_token = self.peek()
        if self.peek().value == "cout":
            return self._parser_parse_cout_patch20(start_token)
        if self.peek().value == "cin":
            return self._parser_parse_cin_patch20(start_token)
        if self.peek().kind == "IDENT" and self.peek(1).value in ("++", "--"):
            name = self.advance().value; op = self.advance().value; self.expect(";")
            return self.loc(AssignStmt(name, "+=" if op == "++" else "-=", Literal(1)), start_token)
        if self.match_kw("struct"):
            return self._parser_parse_cpp_struct_patch20(start_token)
        if self._parser_try_cpp_type_start_patch20():
            saved = self.i
            try:
                return self._parser_parse_cpp_decl_or_func_patch20(start_token)
            except ParseError:
                self.i = saved
        # General lvalue assignment: a[i] = x; obj.field += x;
        saved = self.i
        try:
            lhs = self.parse_expr()
            if self.peek().value in ("=", "+=", "-=", "*=", "/=", "%="):
                op = self.advance().value
                rhs = self.parse_expr()
                self.expect(";")
                if isinstance(lhs, VarExpr):
                    return self.loc(AssignStmt(lhs.name, op, rhs), start_token)
                return self.loc(LValueAssignStmt(op, lhs, rhs), start_token)
            self.expect(";")
            return self.loc(ExprStmt(lhs), start_token)
        except ParseError:
            self.i = saved
        return self._parser_parse_statement_patch19()

    def _parser_parse_target_decl(self, start_token: Token, kind: str) -> TargetDecl:
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

    def _parser_parse_top_or_stmt_patch20(self) -> Any:
        start_token = self.peek()
        if self.match_kw("struct"):
            return self._parser_parse_cpp_struct_patch20(start_token)
        if self._parser_try_cpp_type_start_patch20():
            # Avoid stealing ordinary expression statements like `foo();`.
            saved = self.i
            try:
                return self._parser_parse_cpp_decl_or_func_patch20(start_token)
            except ParseError:
                self.i = saved
        return self._parser_parse_top_or_stmt_patch9()

    def _parser_parse_top_or_stmt_patch9(self) -> Any:
        if self.match_kw("sprite"):
            return self._parser_parse_target_decl(self.toks[self.i - 1], "sprite")
        if self.match_kw("stage"):
            return self._parser_parse_target_decl(self.toks[self.i - 1], "stage")
        return self._base_parse_top_or_stmt()

    def _parser_parse_typed_decl_patch18(self, start_token: Token, first_type: str) -> Any:
        # vector<int> a = {1,2,3};    -> list a = [1,2,3]
        # int n = 0; / auto x = f();  -> let n = 0 / let x = f()
        if first_type == "vector":
            self._parser_skip_template_patch18()
            name = self.expect_ident()
            items: List[Any] = []
            if self.match("="):
                if self.match("{"):
                    items = self._parser_parse_initializer_items_patch18()
                elif self.match("["):
                    if not self.at("]"):
                        while True:
                            items.append(self.parse_expr())
                            if not self.match(","):
                                break
                    self.expect("]")
                else:
                    raise self.error("vector initialization expects {...} or [...]")
            self.expect(";")
            return self.loc(ListDecl(name, items), start_token)

        # int/long/double/string/bool/auto/char.  Optional extra `long` in `long long`.
        if first_type == "long" and self.peek().kind == "KW" and self.peek().value == "long":
            self.advance()
        self._parser_skip_template_patch18()
        name = self.expect_ident()
        expr: Any = Literal(0)
        if first_type in {"string", "char"}:
            expr = Literal("")
        elif first_type == "bool":
            expr = Literal(False)
        if self.match("="):
            expr = self.parse_expr()
        self.expect(";")
        return self.loc(VarDecl(name, expr, True), start_token)

    def _parser_parse_typed_params_patch20(self) -> List[str]:  # type: ignore[no-redef]
        params: List[str] = []
        param_types: Dict[str, str] = {}
        self.expect("(")
        if not self.at(")"):
            while True:
                # Untyped legacy parameter: foo(x, y)
                if self.peek().kind == "IDENT" and self.peek(1).value in {",", ")"}:
                    pname = self.advance().value
                    ptype = "auto"
                else:
                    ptype = _sbg_norm_type21(self._parser_read_template_type21())
                    pname = self.expect_ident()
                params.append(pname)
                param_types[pname] = ptype
                if not self.match(","):
                    break
        self.expect(")")
        self._sbg_last_param_types21 = param_types
        return params

    def _parser_read_template_type21(self) -> str:
        """Read a C++ type, preserving templates like vector<vector<Edge>>."""
        parts: List[str] = []
        while self.peek().value in {"const", "static"}:
            parts.append(self.advance().value)
        if self.peek().value == "std" and self.peek(1).value == "::":
            self.advance(); self.advance()
        if self.peek().kind not in {"IDENT", "KW"}:
            raise self.error(f"expected type name, got {self.peek().value!r}")
        base = self.advance().value
        parts.append(base)
        if base == "long" and self.peek().value == "long":
            parts.append(self.advance().value)
        # Template part.  Lexer may emit >> as one token, so account for it.
        if self.peek().value == "<":
            depth = 0
            while True:
                tok = self.advance()
                v = tok.value
                parts.append(v)
                if v == "<":
                    depth += 1
                elif v == ">":
                    depth -= 1
                elif v == ">>":
                    depth -= 2
                    # Keep exact C++ spelling, no need to split token.
                if depth <= 0:
                    break
                if self.peek().kind == "EOF":
                    raise self.error("unterminated template type")
        while self.peek().value in {"*", "&"}:
            parts.append(self.advance().value)
        return "".join(parts)

    def _parser_skip_cpp_type_patch20(self) -> str:  # type: ignore[no-redef]
        return self._parser_read_template_type21()

    def _parser_skip_template_patch18(self) -> None:  # type: ignore[no-redef]
        if not self.match("<"):
            return
        depth = 1
        while depth > 0:
            if self.peek().kind == "EOF":
                raise self.error("unterminated template/type annotation")
            if self.peek().value == "<":
                self.advance(); depth += 1
            elif self.peek().value == ">>":
                self.advance(); depth -= 2
            elif self.peek().value == ">":
                self.advance(); depth -= 1
            else:
                self.advance()
        if depth < 0:
            # A single >> may close the template and leave one > logically consumed;
            # that is fine for type annotations because we discard the whole type.
            depth = 0

    def _parser_try_cpp_type_start_patch20(self) -> bool:  # type: ignore[no-redef]
        v = self.peek().value
        if v in {"const", "static", "void", "vector", "auto", "int", "long", "double", "float", "string", "char", "bool"}:
            return True
        if v in _SBG_STRUCT_DEFS21:
            return True
        if self.peek().kind == "IDENT" and self.peek(1).kind == "IDENT":
            return True
        if self.peek().kind == "IDENT" and self.peek(1).value == "<":
            return True
        return False

    def __init__(self, tokens: List[Token], filename: str = "<source>"):
        return self._parser_init_patch19(tokens, filename)

    def parse_top_or_stmt(self) -> Any:
        return self._parser_parse_top_or_stmt_patch20()

    def parse_event(self, start_token: Token) -> EventDecl:
        return self._parse_event_patch24(start_token)

    def parse_statement(self) -> Any:
        return self._parser_parse_statement_patch20()

    def parse_for(self, start_token: Token) -> ForStmt:
        return self._parser_parse_for_patch20(start_token)

    def parse_postfix(self) -> Any:
        return self._parser_parse_postfix_patch20()

class ImportResolver:
    """Resolve `import "...";` declarations into a single StageBG program.

    Supported forms:
      import "./relative/file.sbg";
      import "../lib";          // resolves .sbg, main.sbg or index.sbg
      import "pkg:package";     // resolves sbg_modules/package/main.sbg
      import "package";         // package fallback when no relative file exists
    """
    def __init__(self):
        self.seen: set[Path] = set()
        self.stack: List[Path] = []
        self.source_cache: Dict[str, str] = {}

    def parse_entry(self, text: str, filename: str) -> Program:
        filename_path = self._safe_resolve(Path(filename)) if filename and filename != "<source>" else None
        if filename_path:
            self.stack.append(filename_path)
            self.source_cache[str(filename_path)] = text
            try:
                program = Parser(Lexer(text, str(filename_path)).tokens(), str(filename_path)).parse()
                program = self.resolve_program(program, filename_path)
                self.seen.add(filename_path)
                return program
            finally:
                if self.stack and self.stack[-1] == filename_path:
                    self.stack.pop()
        program = Parser(Lexer(text, filename).tokens(), filename).parse()
        return self.resolve_program(program, Path.cwd() / "<source>.sbg")

    def _resolve_body_recursive(self, body: List[Any], current_file: Path) -> List[Any]:
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
                stmt.body = self._resolve_body_recursive(stmt.body, current_file)
            elif isinstance(stmt, ProcDecl):
                stmt.body = self._resolve_body_recursive(stmt.body, current_file)
            elif isinstance(stmt, EventDecl):
                stmt.body = self._resolve_body_recursive(stmt.body, current_file)
            elif isinstance(stmt, BlockStmt):
                stmt.body = self._resolve_body_recursive(stmt.body, current_file)
            elif isinstance(stmt, IfStmt):
                stmt.then_body = self._resolve_body_recursive(stmt.then_body, current_file)
                if stmt.else_body is not None:
                    stmt.else_body = self._resolve_body_recursive(stmt.else_body, current_file)
            elif isinstance(stmt, (RepeatStmt, ForeverStmt, WhileStmt)):
                stmt.body = self._resolve_body_recursive(stmt.body, current_file)
            elif isinstance(stmt, ForStmt):
                stmt.body = self._resolve_body_recursive(stmt.body, current_file)
            out.append(stmt)
        return out

    def resolve_program(self, program: Program, current_file: Path) -> Program:
        return Program(self._resolve_body_recursive(program.body, current_file))

    def load_import(self, spec: str, current_file: Path) -> Program:
        path = self.resolve_import_path(spec, current_file)
        if path in self.stack:
            chain = " -> ".join(str(p) for p in [*self.stack, path])
            raise ImportSBGError(f"circular import detected: {chain}")
        if path in self.seen:
            # Dedup: diamond imports (A imports B and C, both import D) must
            # splice D only once, otherwise duplicate proc/list declarations
            # leak into the compiled program.
            return Program([])
        self.stack.append(path)
        try:
            text = path.read_text(encoding="utf-8")
            self.source_cache[str(path)] = text
            program = Parser(Lexer(text, str(path)).tokens(), str(path)).parse()
            resolved = self.resolve_program(program, path)
            self.seen.add(path)
            return resolved
        except OSError as e:
            raise ImportSBGError(str(e)) from e
        finally:
            if self.stack and self.stack[-1] == path:
                self.stack.pop()

    def resolve_import_path(self, spec: str, current_file: Path) -> Path:
        base = current_file.parent if current_file.name != "<source>.sbg" else Path.cwd()
        searched: List[Path] = []

        if spec.startswith("pkg:"):
            found = self.resolve_package(spec[4:], base, searched)
            if found:
                return found
            raise ImportSBGError(self.import_not_found_message(spec, searched))

        raw = Path(spec)
        file_candidates: List[Path] = []
        if raw.is_absolute():
            file_candidates.extend(self.expand_module_candidates(raw))
        else:
            file_candidates.extend(self.expand_module_candidates(base / raw))
        for cand in file_candidates:
            searched.append(cand)
            if cand.is_file():
                return self._safe_resolve(cand)

        # Bare imports can be package names, e.g. import "arrays";
        if not spec.startswith(".") and not spec.startswith("/"):
            found = self.resolve_package(spec, base, searched)
            if found:
                return found

        raise ImportSBGError(self.import_not_found_message(spec, searched))

    def expand_module_candidates(self, path: Path) -> List[Path]:
        candidates = [path]
        if path.suffix != ".sbg":
            candidates.append(path.with_suffix(".sbg"))
            candidates.append(path / "main.sbg")
            candidates.append(path / "index.sbg")
        elif path.suffix == ".sbg":
            candidates.append(path / "main.sbg")
        return [self._safe_resolve(c) for c in candidates]

    def resolve_package(self, spec: str, base: Path, searched: List[Path]) -> Optional[Path]:
        parts = [p for p in spec.split("/") if p]
        if not parts:
            return None
        pkg = parts[0]
        sub = Path(*parts[1:]) if len(parts) > 1 else None
        for modules in self.package_roots(base):
            pkg_dir = modules / pkg
            direct = modules / f"{pkg}.sbg"
            for cand in self.expand_module_candidates(direct):
                searched.append(cand)
                if cand.is_file() and sub is None:
                    return self._safe_resolve(cand)
            searched.append(pkg_dir)
            if not pkg_dir.exists():
                continue
            if sub is not None:
                for cand in self.expand_module_candidates(pkg_dir / sub):
                    searched.append(cand)
                    if cand.is_file():
                        return self._safe_resolve(cand)
            manifest = pkg_dir / PACKAGE_MANIFEST
            main_name = "main.sbg"
            if manifest.is_file():
                try:
                    meta = json.loads(manifest.read_text(encoding="utf-8"))
                    main_name = str(meta.get("main") or main_name)
                except Exception:
                    main_name = "main.sbg"
            for cand in self.expand_module_candidates(pkg_dir / main_name):
                searched.append(cand)
                if cand.is_file():
                    return self._safe_resolve(cand)
            for fallback in (pkg_dir / "main.sbg", pkg_dir / "index.sbg"):
                searched.append(fallback)
                if fallback.is_file():
                    return self._safe_resolve(fallback)
        return None

    def _package_roots_base(self, base: Path) -> List[Path]:
        roots: List[Path] = []
        cursor = self._safe_resolve(base if base.is_dir() else base.parent)
        for parent in [cursor, *cursor.parents]:
            roots.append(parent / SBG_MODULES_DIR)
            if (parent / PACKAGE_MANIFEST).is_file():
                break
        cwd_root = Path.cwd() / SBG_MODULES_DIR
        if cwd_root not in roots:
            roots.append(cwd_root)
        return roots

    def package_roots(self, base: Path) -> List[Path]:
        # Patch14b: allow built-in/local package folders, so examples and real
        # projects can use `import "std";` directly without first copying std
        # into sbg_modules/.
        roots = list(self._package_roots_base(base))
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

    def import_not_found_message(self, spec: str, searched: List[Path]) -> str:
        shown = "\n".join(f"    {p}" for p in searched[:12])
        extra = "" if len(searched) <= 12 else f"\n    ... and {len(searched) - 12} more"
        return f"cannot resolve import {spec!r}. Searched:\n{shown}{extra}"

    @staticmethod
    def _safe_resolve(path: Path) -> Path:
        try:
            return path.resolve()
        except OSError:
            return path.absolute()

def _sbg_collect_proc_return_types(body: List[Any], out: Dict[str, str]) -> None:
    # BUGS_REPORT #6: gather {proc_name: return_type} for the declaration
    # type check. Only procs with an annotated return type participate.
    for stmt in body:
        if isinstance(stmt, ProcDecl):
            rt = getattr(stmt, "return_type", None)
            if rt:
                out[stmt.name] = _sbg_norm_type21(rt)
        if isinstance(stmt, TargetDecl):
            _sbg_collect_proc_return_types(stmt.body, out)
        elif isinstance(stmt, (EventDecl, ProcDecl, BlockStmt)):
            _sbg_collect_proc_return_types(stmt.body, out)
        elif isinstance(stmt, IfStmt):
            _sbg_collect_proc_return_types(stmt.then_body, out)
            if stmt.else_body is not None:
                _sbg_collect_proc_return_types(stmt.else_body, out)
        elif isinstance(stmt, (RepeatStmt, ForeverStmt, WhileStmt, ForStmt)):
            _sbg_collect_proc_return_types(stmt.body, out)


def _sbg_check_vector_init_types(body: List[Any], proc_types: Dict[str, str]) -> None:
    # BUGS_REPORT #6: `vector<T> x = someProc(...)` must not compile silently
    # when someProc is known to return a scalar (int/string/...).
    for stmt in body:
        if isinstance(stmt, ListDecl):
            init = getattr(stmt, "sbg_init", None)
            typ = getattr(stmt, "sbg_type", None)
            if typ and _sbg_is_vector21(typ) and isinstance(init, CallExpr):
                rt = proc_types.get(init.callee)
                if rt and not _sbg_is_vector21(rt):
                    raise ParseError(
                        f"{getattr(stmt, 'filename', '<source>')}:{getattr(stmt, 'line', 1)}:{getattr(stmt, 'col', 1)}: "
                        f"type mismatch: cannot assign proc '{init.callee}' returning '{rt}' "
                        f"to variable '{stmt.name}' of type '{typ}'"
                    )
        if isinstance(stmt, TargetDecl):
            _sbg_check_vector_init_types(stmt.body, proc_types)
        elif isinstance(stmt, (EventDecl, ProcDecl, BlockStmt)):
            _sbg_check_vector_init_types(stmt.body, proc_types)
        elif isinstance(stmt, IfStmt):
            _sbg_check_vector_init_types(stmt.then_body, proc_types)
            if stmt.else_body is not None:
                _sbg_check_vector_init_types(stmt.else_body, proc_types)
        elif isinstance(stmt, (RepeatStmt, ForeverStmt, WhileStmt, ForStmt)):
            _sbg_check_vector_init_types(stmt.body, proc_types)


def parse_source(text: str, filename: str = "<source>") -> Program:
    program = ImportResolver().parse_entry(text, filename)
    # BUGS_REPORT #6: post-parse type check for vector declarations whose
    # initializer is a call to a proc with a known scalar return type.
    proc_types: Dict[str, str] = {}
    _sbg_collect_proc_return_types(program.body, proc_types)
    _sbg_check_vector_init_types(program.body, proc_types)
    return program

_g.parse_source = parse_source
