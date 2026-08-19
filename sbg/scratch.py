from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from .errors import CompileError, attach_location
from .ast import (
    Token, Lexer, Program, ImportDecl, VarDecl, ListDecl, EventDecl, ProcDecl,
    BlockStmt, IfStmt, RepeatStmt, ForeverStmt, WhileStmt, ForStmt,
    ReturnStmt, BreakStmt, ContinueStmt, AssignStmt, ExprStmt,
    Literal, VarExpr, BinaryExpr, UnaryExpr, CallExpr, ArrayExpr,
)
from .globals import TERMINAL_LIST_NAME

class ScratchBuilder:
    def __init__(self):
        self.blocks: Dict[str, Dict[str, Any]] = {}
        self.variables: Dict[str, str] = {}
        self.lists: Dict[str, str] = {}
        self.broadcasts: Dict[str, str] = {}
        self.counter = 0
        self.x = 40
        self.y = 40
        self.current_proc_params: Dict[str, str] = {}
        self.const_variables: set[str] = set()  # CRITICAL FIX: pkt 9 - track const variables

    def uid(self, prefix: str = "b") -> str:
        self.counter += 1
        return f"{prefix}{self.counter:05d}"

    def add_block(self, opcode: str, *, next: Optional[str] = None, parent: Optional[str] = None,
                  inputs: Optional[Dict[str, Any]] = None, fields: Optional[Dict[str, Any]] = None,
                  shadow: bool = False, topLevel: bool = False, x: Optional[int] = None, y: Optional[int] = None,
                  mutation: Optional[Dict[str, Any]] = None) -> str:
        bid = self.uid()
        obj: Dict[str, Any] = {
            "opcode": opcode,
            "next": next,
            "parent": parent,
            "inputs": inputs or {},
            "fields": fields or {},
            "shadow": shadow,
            "topLevel": topLevel,
        }
        if topLevel:
            obj["x"] = self.x if x is None else x
            obj["y"] = self.y if y is None else y
            self.y += 140
        if mutation is not None:
            obj["mutation"] = mutation
        self.blocks[bid] = obj
        return bid

    def set_parent(self, bid: Optional[str], parent: str) -> None:
        if bid and bid in self.blocks:
            self.blocks[bid]["parent"] = parent

    def chain(self, first: Optional[str], second: Optional[str]) -> Optional[str]:
        if not first:
            return second
        if not second:
            return first
        last = first
        while self.blocks[last].get("next"):
            last = self.blocks[last]["next"]
        self.blocks[last]["next"] = second
        self.blocks[second]["parent"] = last
        return first

    def var_id(self, name: str) -> str:
        if name not in self.variables:
            self.variables[name] = self.uid("var")
        return self.variables[name]

    def list_id(self, name: str) -> str:
        if name not in self.lists:
            self.lists[name] = self.uid("list")
        return self.lists[name]

    def broadcast_id(self, name: str) -> str:
        if name not in self.broadcasts:
            self.broadcasts[name] = self.uid("msg")
        return self.broadcasts[name]

    def literal_input(self, value: Any) -> Any:
        if isinstance(value, bool):
            # No boolean literal primitive in Scratch. Use expression block elsewhere.
            raise CompileError("internal: boolean literal must be compiled as block")
        if isinstance(value, (int, float)):
            return [1, [4, str(value)]]
        if value is None:
            return [1, [10, ""]]
        return [1, [10, str(value)]]

    def expr_input(self, expr: Any, parent: Optional[str] = None) -> Any:
        if isinstance(expr, Literal) and not isinstance(expr.value, bool):
            return self.literal_input(expr.value)
        bid = self.compile_expr(expr, parent=parent)
        return [2, bid] if self.is_boolean_expr(expr) else [1, bid]

    def condition_input(self, expr: Any, parent: Optional[str] = None) -> Any:
        """Input for a boolean (hexagonal) slot such as CONDITION or OPERAND.

        Scratch rejects round reporter blocks dropped into hexagonal fields, so
        a non-boolean expression is wrapped as `expr == 1`. This handles both
        numbers and booleans (1 == 1 is true, 0 == 1 is false) and mirrors the
        C-style truthiness the language promises. Boolean expressions are used
        as-is: sharp blocks are always accepted in round slots, never the
        other way around.
        """
        if self.is_boolean_expr(expr):
            return self.expr_input(expr, parent)
        return self.expr_input(BinaryExpr(expr, "==", Literal(1)), parent)

    def substack_input(self, first: Optional[str]) -> Any:
        return [2, first] if first else [1, None]

    # Builtins that compile to boolean (hexagonal) reporter blocks. They must
    # never be wrapped in `== 1` by condition_input(): in Scratch a boolean in
    # a round slot renders as the text "true"/"false", so `true == 1` is false.
    BOOLEAN_REPORTER_BUILTINS = {
        "contains", "listHas",
        "mouseDown", "keyPressed", "touching",
    }

    def is_boolean_expr(self, expr: Any) -> bool:
        if isinstance(expr, Literal) and isinstance(expr.value, bool): return True
        if isinstance(expr, UnaryExpr) and expr.op == "!": return True
        if isinstance(expr, BinaryExpr) and expr.op in ("==", "!=", "<", "<=", ">", ">=", "&&", "||"): return True
        if isinstance(expr, CallExpr) and expr.callee == "contains": return True
        if isinstance(expr, CallExpr) and expr.callee in self.BOOLEAN_REPORTER_BUILTINS: return True
        return False

    def compile_expr(self, expr: Any, parent: Optional[str] = None) -> str:
        try:
            return self._compile_expr(expr, parent=parent)
        except CompileError as e:
            attach_location(e, expr)
            raise
        except Exception as e:
            err = CompileError(str(e))
            attach_location(err, expr)
            raise err from e

    def _compile_expr(self, expr: Any, parent: Optional[str] = None) -> str:
        if isinstance(expr, Literal):
            if isinstance(expr.value, bool):
                a = Literal(1)
                b = Literal(1 if expr.value else 0)
                return self.compile_expr(BinaryExpr(a, "==", b), parent)
            # String/number literals generally appear as primitive inputs.
            bid = self.add_block("operator_join", parent=parent,
                                 inputs={"STRING1": self.literal_input(expr.value), "STRING2": self.literal_input("")})
            return bid
        if isinstance(expr, VarExpr):
            if expr.name in self.current_proc_params:
                return self.add_block("argument_reporter_string_number", parent=parent,
                                      fields={"VALUE": [expr.name, None]})
            if expr.name in self.lists:
                return self.add_block("data_listcontents", parent=parent,
                                      fields={"LIST": [expr.name, self.list_id(expr.name)]})
            return self.add_block("data_variable", parent=parent,
                                  fields={"VARIABLE": [expr.name, self.var_id(expr.name)]})
        if isinstance(expr, UnaryExpr):
            if expr.op == "!":
                bid = self.add_block("operator_not", parent=parent, inputs={})
                self.blocks[bid]["inputs"]["OPERAND"] = self.condition_input(expr.expr, bid)
                return bid
            if expr.op == "-":
                return self.compile_expr(BinaryExpr(Literal(0), "-", expr.expr), parent)
        if isinstance(expr, BinaryExpr):
            opmap = {
                "+": "operator_add", "-": "operator_subtract", "*": "operator_multiply", "/": "operator_divide",
                "%": "operator_mod", "==": "operator_equals", "<": "operator_lt", ">": "operator_gt",
                "&&": "operator_and", "||": "operator_or",
            }
            if expr.op == "!=":
                return self.compile_expr(UnaryExpr("!", BinaryExpr(expr.left, "==", expr.right)), parent)
            if expr.op == "<=":
                return self.compile_expr(UnaryExpr("!", BinaryExpr(expr.left, ">", expr.right)), parent)
            if expr.op == ">=":
                return self.compile_expr(UnaryExpr("!", BinaryExpr(expr.left, "<", expr.right)), parent)
            opcode = opmap[expr.op]
            bid = self.add_block(opcode, parent=parent, inputs={})
            if expr.op in ("&&", "||"):
                self.blocks[bid]["inputs"]["OPERAND1"] = self.condition_input(expr.left, bid)
                self.blocks[bid]["inputs"]["OPERAND2"] = self.condition_input(expr.right, bid)
            elif expr.op in ("<", ">", "=="):
                self.blocks[bid]["inputs"]["OPERAND1"] = self.expr_input(expr.left, bid)
                self.blocks[bid]["inputs"]["OPERAND2"] = self.expr_input(expr.right, bid)
            else:
                self.blocks[bid]["inputs"]["NUM1"] = self.expr_input(expr.left, bid)
                self.blocks[bid]["inputs"]["NUM2"] = self.expr_input(expr.right, bid)
            return bid
        if isinstance(expr, CallExpr):
            return self.compile_call_expr(expr, parent)
        raise CompileError(f"expression cannot be compiled to Scratch: {expr}")

    def compile_call_expr(self, expr: CallExpr, parent: Optional[str]) -> str:
        name = expr.callee
        a = expr.args
        if name == "answer":
            return self.add_block("sensing_answer", parent=parent)
        if name == "random":
            self.need_args(name, a, 2)
            bid = self.add_block("operator_random", parent=parent, inputs={})
            self.blocks[bid]["inputs"]["FROM"] = self.expr_input(a[0], bid)
            self.blocks[bid]["inputs"]["TO"] = self.expr_input(a[1], bid)
            return bid
        if name == "round":
            self.need_args(name, a, 1)
            bid = self.add_block("operator_round", parent=parent, inputs={})
            self.blocks[bid]["inputs"]["NUM"] = self.expr_input(a[0], bid)
            return bid
        if name in ("abs", "floor", "ceil", "sqrt"):
            self.need_args(name, a, 1)
            op = {"abs": "abs", "floor": "floor", "ceil": "ceiling", "sqrt": "sqrt"}[name]
            bid = self.add_block("operator_mathop", parent=parent, inputs={}, fields={"OPERATOR": [op, None]})
            self.blocks[bid]["inputs"]["NUM"] = self.expr_input(a[0], bid)
            return bid
        if name == "join":
            self.need_args(name, a, 2)
            bid = self.add_block("operator_join", parent=parent, inputs={})
            self.blocks[bid]["inputs"]["STRING1"] = self.expr_input(a[0], bid)
            self.blocks[bid]["inputs"]["STRING2"] = self.expr_input(a[1], bid)
            return bid
        if name == "len":
            self.need_args(name, a, 1)
            # Scratch has two different blocks:
            #   - length of list
            #   - length of string
            # Older SBG builds treated every VarExpr passed to len() as a list,
            # which silently turned procedure parameters like `text` into lists.
            # That could produce projects that looked empty/broken after import.
            if isinstance(a[0], VarExpr) and a[0].name in self.lists:
                lst_name = a[0].name
                return self.add_block("data_lengthoflist", parent=parent, fields={"LIST": [lst_name, self.list_id(lst_name)]})
            bid = self.add_block("operator_length", parent=parent, inputs={})
            self.blocks[bid]["inputs"]["STRING"] = self.expr_input(a[0], bid)
            return bid
        if name == "item":
            self.need_args(name, a, 2)
            lst_name = self.require_list_expr(a[0])
            bid = self.add_block("data_itemoflist", parent=parent, fields={"LIST": [lst_name, self.list_id(lst_name)]}, inputs={})
            self.blocks[bid]["inputs"]["INDEX"] = self.expr_input(a[1], bid)
            return bid
        if name == "contains":
            self.need_args(name, a, 2)
            lst_name = self.require_list_expr(a[0])
            bid = self.add_block("data_listcontainsitem", parent=parent, fields={"LIST": [lst_name, self.list_id(lst_name)]}, inputs={})
            self.blocks[bid]["inputs"]["ITEM"] = self.expr_input(a[1], bid)
            return bid
        if name == "timer":
            return self.add_block("sensing_timer", parent=parent)
        raise CompileError(f"function {name} cannot be used as an expression in Scratch output")

    def need_args(self, name: str, args: List[Any], n: int) -> None:
        if len(args) != n:
            raise CompileError(f"{name}() expects {n} args, got {len(args)}")

    def require_list_expr(self, expr: Any) -> str:
        if isinstance(expr, VarExpr):
            # Scratch custom blocks cannot receive a list as a real reference.
            # A parameter named `target_list` is only a string/number argument in
            # Scratch, not a dynamic list handle. Failing here is much better than
            # generating a project that imports as blank or behaves like it is blank.
            if expr.name in self.current_proc_params:
                raise CompileError(
                    f"Scratch output cannot use procedure parameter {expr.name!r} as a list reference. "
                    "Scratch custom blocks do not support list-reference parameters. "
                    "Use a concrete declared list name, or generate one wrapper proc per list."
                )
            if expr.name not in self.lists:
                raise CompileError(f"unknown list {expr.name!r}; declare it with `list {expr.name} = [];` before using list functions")
            self.list_id(expr.name)
            return expr.name
        raise CompileError("Scratch list functions need a plain list name, e.g. len(items), item(items, 1)")

    def compile_statement_chain(self, body: List[Any]) -> Optional[str]:
        first: Optional[str] = None
        for stmt in body:
            try:
                sid = self.compile_stmt(stmt)
            except CompileError as e:
                attach_location(e, stmt)
                raise
            except Exception as e:
                err = CompileError(str(e))
                attach_location(err, stmt)
                raise err from e
            first = self.chain(first, sid)
        return first

    def compile_stmt(self, stmt: Any) -> Optional[str]:
        if isinstance(stmt, VarDecl):
            self.var_id(stmt.name)
            bid = self.add_block("data_setvariableto", fields={"VARIABLE": [stmt.name, self.var_id(stmt.name)]}, inputs={})
            self.blocks[bid]["inputs"]["VALUE"] = self.expr_input(stmt.expr, bid)
            return bid
        if isinstance(stmt, ListDecl):
            self.list_id(stmt.name)
            # Project JSON initializes lists. Runtime reset is emulated by delete all + add items.
            first = self.add_block("data_deletealloflist", fields={"LIST": [stmt.name, self.list_id(stmt.name)]})
            chain = first
            for item in stmt.items:
                add = self.add_block("data_addtolist", fields={"LIST": [stmt.name, self.list_id(stmt.name)]}, inputs={})
                self.blocks[add]["inputs"]["ITEM"] = self.expr_input(item, add)
                chain = self.chain(chain, add) or chain
            return first
        if isinstance(stmt, AssignStmt):
            # CRITICAL FIX: pkt 9 - check if trying to assign to const variable
            if stmt.name in self.const_variables:
                raise CompileError(f"cannot assign to const variable {stmt.name!r}")
            self.var_id(stmt.name)
            if stmt.op == "+=":
                bid = self.add_block("data_changevariableby", fields={"VARIABLE": [stmt.name, self.var_id(stmt.name)]}, inputs={})
                self.blocks[bid]["inputs"]["VALUE"] = self.expr_input(stmt.expr, bid)
                return bid
            expr = stmt.expr
            if stmt.op == "-=": expr = BinaryExpr(VarExpr(stmt.name), "-", stmt.expr)
            elif stmt.op == "*=": expr = BinaryExpr(VarExpr(stmt.name), "*", stmt.expr)
            elif stmt.op == "/=": expr = BinaryExpr(VarExpr(stmt.name), "/", stmt.expr)
            elif stmt.op == "%=": expr = BinaryExpr(VarExpr(stmt.name), "%", stmt.expr)
            bid = self.add_block("data_setvariableto", fields={"VARIABLE": [stmt.name, self.var_id(stmt.name)]}, inputs={})
            self.blocks[bid]["inputs"]["VALUE"] = self.expr_input(expr, bid)
            return bid
        if isinstance(stmt, ExprStmt):
            if isinstance(stmt.expr, CallExpr):
                return self.compile_call_stmt(stmt.expr)
            raise CompileError("only function calls can be used as expression statements in Scratch output")
        if isinstance(stmt, IfStmt):
            if stmt.else_body is not None:
                then_first = self.compile_statement_chain(stmt.then_body)
                else_first = self.compile_statement_chain(stmt.else_body)
                bid = self.add_block("control_if_else", inputs={})
                self.blocks[bid]["inputs"]["CONDITION"] = self.condition_input(stmt.cond, bid)
                self.blocks[bid]["inputs"]["SUBSTACK"] = self.substack_input(then_first)
                self.blocks[bid]["inputs"]["SUBSTACK2"] = self.substack_input(else_first)
                self.set_parent(then_first, bid); self.set_parent(else_first, bid)
                return bid
            then_first = self.compile_statement_chain(stmt.then_body)
            bid = self.add_block("control_if", inputs={})
            self.blocks[bid]["inputs"]["CONDITION"] = self.condition_input(stmt.cond, bid)
            self.blocks[bid]["inputs"]["SUBSTACK"] = self.substack_input(then_first)
            self.set_parent(then_first, bid)
            return bid
        if isinstance(stmt, RepeatStmt):
            sub = self.compile_statement_chain(stmt.body)
            bid = self.add_block("control_repeat", inputs={})
            self.blocks[bid]["inputs"]["TIMES"] = self.expr_input(stmt.count, bid)
            self.blocks[bid]["inputs"]["SUBSTACK"] = self.substack_input(sub)
            self.set_parent(sub, bid)
            return bid
        if isinstance(stmt, ForeverStmt):
            sub = self.compile_statement_chain(stmt.body)
            bid = self.add_block("control_forever", inputs={"SUBSTACK": self.substack_input(sub)})
            self.set_parent(sub, bid)
            return bid
        if isinstance(stmt, WhileStmt):
            sub = self.compile_statement_chain(stmt.body)
            bid = self.add_block("control_repeat_until", inputs={})
            self.blocks[bid]["inputs"]["CONDITION"] = self.expr_input(UnaryExpr("!", stmt.cond), bid)
            self.blocks[bid]["inputs"]["SUBSTACK"] = self.substack_input(sub)
            self.set_parent(sub, bid)
            return bid
        if isinstance(stmt, ForStmt):
            # Compile for(init;cond;update){body} as init; repeat until !(cond){body;update}
            first = self.compile_stmt(stmt.init) if stmt.init else None
            sub_body = list(stmt.body)
            if stmt.update:
                sub_body.append(stmt.update)
            cond = stmt.cond if stmt.cond is not None else Literal(True)
            loop = self.compile_stmt(WhileStmt(cond, sub_body))
            return self.chain(first, loop)
        if isinstance(stmt, ReturnStmt):
            raise CompileError("Scratch procedures do not return values. Use output variables/lists instead of return.")
        if isinstance(stmt, (BreakStmt, ContinueStmt)):
            raise CompileError("break/continue cannot be represented safely in Scratch blocks")
        if isinstance(stmt, ProcDecl):
            return None
        if isinstance(stmt, EventDecl):
            return None
        raise CompileError(f"statement cannot be compiled: {stmt}")

    def compile_call_stmt(self, expr: CallExpr) -> Optional[str]:
        name, a = expr.callee, expr.args
        if name == "log":
            self.list_id(TERMINAL_LIST_NAME)
            val = Literal("") if not a else a[0] if len(a) == 1 else self.join_many(a)
            bid = self.add_block("data_addtolist", fields={"LIST": [TERMINAL_LIST_NAME, self.list_id(TERMINAL_LIST_NAME)]}, inputs={})
            self.blocks[bid]["inputs"]["ITEM"] = self.expr_input(val, bid)
            return bid
        if name == "wait":
            self.need_args(name, a, 1)
            bid = self.add_block("control_wait", inputs={})
            self.blocks[bid]["inputs"]["DURATION"] = self.expr_input(a[0], bid)
            return bid
        if name == "ask":
            self.need_args(name, a, 1)
            bid = self.add_block("sensing_askandwait", inputs={})
            self.blocks[bid]["inputs"]["QUESTION"] = self.expr_input(a[0], bid)
            return bid
        if name in ("broadcast", "broadcastAndWait"):
            self.need_args(name, a, 1)
            if not isinstance(a[0], Literal) or not isinstance(a[0].value, str):
                raise CompileError("broadcast() target must be a string literal for Scratch output")
            msg = a[0].value
            bid = self.add_block("event_broadcastandwait" if name == "broadcastAndWait" else "event_broadcast", inputs={})
            menu = self.add_block("event_broadcast_menu", parent=bid, shadow=True,
                                  fields={"BROADCAST_OPTION": [msg, self.broadcast_id(msg)]})
            self.blocks[bid]["inputs"]["BROADCAST_INPUT"] = [1, menu]
            return bid
        if name == "resetTimer":
            return self.add_block("sensing_resettimer")
        if name == "push":
            self.need_args(name, a, 2)
            lst = self.require_list_expr(a[0])
            bid = self.add_block("data_addtolist", fields={"LIST": [lst, self.list_id(lst)]}, inputs={})
            self.blocks[bid]["inputs"]["ITEM"] = self.expr_input(a[1], bid)
            return bid
        if name == "insert":
            self.need_args(name, a, 3)
            lst = self.require_list_expr(a[0])
            bid = self.add_block("data_insertatlist", fields={"LIST": [lst, self.list_id(lst)]}, inputs={})
            self.blocks[bid]["inputs"]["INDEX"] = self.expr_input(a[1], bid)
            self.blocks[bid]["inputs"]["ITEM"] = self.expr_input(a[2], bid)
            return bid
        if name == "delete":
            self.need_args(name, a, 2)
            lst = self.require_list_expr(a[0])
            bid = self.add_block("data_deleteoflist", fields={"LIST": [lst, self.list_id(lst)]}, inputs={})
            self.blocks[bid]["inputs"]["INDEX"] = self.expr_input(a[1], bid)
            return bid
        if name == "replace":
            self.need_args(name, a, 3)
            lst = self.require_list_expr(a[0])
            bid = self.add_block("data_replaceitemoflist", fields={"LIST": [lst, self.list_id(lst)]}, inputs={})
            self.blocks[bid]["inputs"]["INDEX"] = self.expr_input(a[1], bid)
            self.blocks[bid]["inputs"]["ITEM"] = self.expr_input(a[2], bid)
            return bid
        if name == "setBackdrop":
            self.need_args(name, a, 1)
            # The generated project has one backdrop; dynamic backdrop names work if user later adds them in Scratch.
            bid = self.add_block("looks_switchbackdropto", inputs={})
            self.blocks[bid]["inputs"]["BACKDROP"] = self.expr_input(a[0], bid)
            return bid
        if name == "nextBackdrop":
            return self.add_block("looks_nextbackdrop")
        if name == "playSound":
            self.need_args(name, a, 1)
            if not isinstance(a[0], Literal):
                raise CompileError("playSound() needs a string literal in Scratch output")
            return self.add_block("sound_play", fields={"SOUND_MENU": [str(a[0].value), None]})
        if name == "stopAllSounds":
            return self.add_block("sound_stopallsounds")
        # Procedure call
        return self.compile_proc_call(name, a)

    def join_many(self, args: List[Any]) -> Any:
        expr = args[0]
        for nxt in args[1:]:
            expr = CallExpr("join", [expr, nxt])
        return expr

    def compile_proc_call(self, name: str, args: List[Any]) -> str:
        proccode, argids = self.proc_signatures.get(name, (None, None))  # type: ignore[attr-defined]
        if proccode is None or argids is None:
            raise CompileError(f"unknown procedure {name}()")
        if len(args) != len(argids):
            raise CompileError(f"{name}() expects {len(argids)} args, got {len(args)}")
        bid = self.add_block("procedures_call", inputs={}, mutation={
            "tagName": "mutation", "children": [], "proccode": proccode,
            "argumentids": json.dumps(argids), "warp": "false"
        })
        for argid, arg in zip(argids, args):
            self.blocks[bid]["inputs"][argid] = self.expr_input(arg, bid)
        return bid

    def compile_proc_definition(self, proc: ProcDecl) -> str:
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
        saved = dict(self.current_proc_params)
        self.current_proc_params = {p: aid for p, aid in zip(proc.params, argids)}
        body_first = self.compile_statement_chain(proc.body)
        self.current_proc_params = saved
        self.blocks[def_id]["next"] = body_first
        if body_first:
            self.blocks[body_first]["parent"] = def_id
        return def_id