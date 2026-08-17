from __future__ import annotations

import math
import random
import time
from typing import Any, Dict, List

from .errors import RuntimeSBGError, attach_location
from .ast import (
    Token, Lexer, Program, ImportDecl, VarDecl, ListDecl, EventDecl, ProcDecl,
    BlockStmt, IfStmt, RepeatStmt, ForeverStmt, WhileStmt, ForStmt,
    ReturnStmt, BreakStmt, ContinueStmt, AssignStmt, ExprStmt,
    Literal, VarExpr, BinaryExpr, UnaryExpr, CallExpr, ArrayExpr,
)
from .globals import ACTION_PROC_NAME

# =============================================================================
# Interpreter
# =============================================================================

class ReturnSignal(Exception):
    def __init__(self, value: Any): self.value = value
class BreakSignal(Exception): pass
class ContinueSignal(Exception): pass

class Runtime:
    def __init__(self, program: Program, *, fast: bool = False, filename: str = "<source>", source_text: str = ""):
        self.program = program
        self.filename = filename
        self.source_text = source_text
        self.vars: Dict[str, Any] = {}
        self.lists: Dict[str, List[Any]] = {}
        self.procs: Dict[str, ProcDecl] = {}
        self.flag_events: List[EventDecl] = []
        self.action_events: List[EventDecl] = []
        self.message_events: Dict[str, List[EventDecl]] = {}
        self.answer_value = ""
        self.fast = fast
        self.timer_start = time.monotonic()
        self.output: List[str] = []

    def prepare(self) -> None:
        for stmt in self.program.body:
            if isinstance(stmt, VarDecl):
                self.vars[stmt.name] = self.eval(stmt.expr)
            elif isinstance(stmt, ListDecl):
                self.lists[stmt.name] = [self.eval(x) for x in stmt.items]
            elif isinstance(stmt, ProcDecl):
                self.procs[stmt.name] = stmt
            elif isinstance(stmt, EventDecl):
                if stmt.kind == "flag":
                    self.flag_events.append(stmt)
                elif stmt.kind == "action":
                    self.action_events.append(stmt)
                else:
                    self.message_events.setdefault(stmt.value or "", []).append(stmt)
            else:
                # top-level loose code becomes init/flag-like code
                self.exec_stmt(stmt)

    def run_flag(self, input_value: str = "") -> None:
        self.prepare()
        if self.flag_events:
            for ev in self.flag_events:
                self.exec_block(ev.body)
        else:
            self.run_action(input_value)

    def run_action(self, input_value: str = "") -> None:
        self.answer_value = input_value
        for ev in self.action_events:
            param = ev.value or "Input"
            old_present = param in self.vars
            old_value = self.vars.get(param)
            self.vars[param] = input_value
            try:
                self.exec_block(ev.body)
            finally:
                if old_present:
                    self.vars[param] = old_value
                else:
                    self.vars.pop(param, None)

    def run_message(self, msg: str) -> None:
        for ev in self.message_events.get(msg, []):
            self.exec_block(ev.body)

    def prepare_scratch_console(self) -> None:
        """Prepare runtime using the same entrypoint model as compiled Scratch.

        The generated .sb3 owns the green-flag script and repeatedly calls
        Action(Input). Therefore native execution must not interpret top-level
        code as a separate Python-only startup phase. Instead it builds the same
        Action(Input) body that the compiler builds:

        - global let/list declarations initialise Stage variables/lists once,
        - `on action(input)` and `proc Action(input)` become console handlers,
        - `on flag` is treated as Action(Input) only when no explicit action
          handler exists,
        - loose top-level statements are appended to Action(Input).
        """
        self.vars.clear()
        self.lists.clear()
        self.procs.clear()
        self.flag_events.clear()
        self.action_events.clear()
        self.message_events.clear()
        self.output.clear()
        self.timer_start = time.monotonic()

        loose: List[Any] = []
        flag_entries: List[EventDecl] = []
        for stmt in self.program.body:
            if isinstance(stmt, VarDecl):
                self.vars[stmt.name] = self.eval(stmt.expr)
            elif isinstance(stmt, ListDecl):
                self.lists[stmt.name] = [self.eval(x) for x in stmt.items]
            elif isinstance(stmt, ProcDecl):
                if stmt.name == ACTION_PROC_NAME:
                    param = stmt.params[0] if stmt.params else "Input"
                    self.action_events.append(EventDecl("action", param, stmt.body))
                else:
                    self.procs[stmt.name] = stmt
            elif isinstance(stmt, EventDecl):
                if stmt.kind == "action":
                    self.action_events.append(stmt)
                elif stmt.kind == "flag":
                    flag_entries.append(EventDecl("action", "Input", stmt.body))
                else:
                    self.message_events.setdefault(stmt.value or "", []).append(stmt)
            else:
                loose.append(stmt)

        if not self.action_events:
            self.action_events.extend(flag_entries)
        if loose:
            self.action_events.append(EventDecl("action", "Input", loose))

    def run_scratch_once(self, input_value: str = "") -> None:
        """Run one native Action(Input) invocation after Scratch-compatible init."""
        self.prepare_scratch_console()
        if not self.action_events:
            raise RuntimeSBGError("nothing runnable: no Action(Input) body was prepared")
        self.run_action(input_value)

    def run_scratch_terminal(self, *, prompt: str = "sbg> ") -> None:
        """Run an interactive native terminal that mirrors the generated .sb3 loop."""
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
            self.run_action(line)

    def exec_block(self, body: List[Any]) -> None:
        for stmt in body:
            self.exec_stmt(stmt)

    def exec_stmt(self, stmt: Any) -> None:
        try:
            return self._exec_stmt(stmt)
        except RuntimeSBGError as e:
            attach_location(e, stmt)
            raise
        except (TypeError, ValueError, ZeroDivisionError, IndexError) as e:
            err = RuntimeSBGError(str(e))
            attach_location(err, stmt)
            raise err from e

    def _exec_stmt(self, stmt: Any) -> None:
        if isinstance(stmt, VarDecl):
            self.vars[stmt.name] = self.eval(stmt.expr)
        elif isinstance(stmt, ListDecl):
            self.lists[stmt.name] = [self.eval(x) for x in stmt.items]
        elif isinstance(stmt, ProcDecl):
            self.procs[stmt.name] = stmt
        elif isinstance(stmt, AssignStmt):
            val = self.eval(stmt.expr)
            old = self.vars.get(stmt.name, 0)
            if stmt.op == "=": self.vars[stmt.name] = val
            elif stmt.op == "+=": self.vars[stmt.name] = old + val
            elif stmt.op == "-=": self.vars[stmt.name] = old - val
            elif stmt.op == "*=": self.vars[stmt.name] = old * val
            elif stmt.op == "/=": self.vars[stmt.name] = old / val
            elif stmt.op == "%=": self.vars[stmt.name] = old % val
        elif isinstance(stmt, ExprStmt):
            self.eval(stmt.expr)
        elif isinstance(stmt, IfStmt):
            if self.truthy(self.eval(stmt.cond)):
                self.exec_block(stmt.then_body)
            elif stmt.else_body is not None:
                self.exec_block(stmt.else_body)
        elif isinstance(stmt, RepeatStmt):
            n = int(self.num(self.eval(stmt.count)))
            for _ in range(max(0, n)):
                try:
                    self.exec_block(stmt.body)
                except ContinueSignal:
                    continue
                except BreakSignal:
                    break
        elif isinstance(stmt, ForeverStmt):
            # The runner protects you from accidental infinite terminal loops.
            for _ in range(1000000):
                try:
                    self.exec_block(stmt.body)
                except ContinueSignal:
                    continue
                except BreakSignal:
                    break
        elif isinstance(stmt, WhileStmt):
            guard = 0
            while self.truthy(self.eval(stmt.cond)):
                guard += 1
                if guard > 1000000:
                    raise RuntimeSBGError("while loop safety limit hit")
                try:
                    self.exec_block(stmt.body)
                except ContinueSignal:
                    continue
                except BreakSignal:
                    break
        elif isinstance(stmt, ForStmt):
            if stmt.init: self.exec_stmt(stmt.init)
            guard = 0
            while True:
                if stmt.cond is not None and not self.truthy(self.eval(stmt.cond)):
                    break
                guard += 1
                if guard > 1000000:
                    raise RuntimeSBGError("for loop safety limit hit")
                try:
                    self.exec_block(stmt.body)
                except ContinueSignal:
                    pass
                except BreakSignal:
                    break
                if stmt.update: self.exec_stmt(stmt.update)
        elif isinstance(stmt, ReturnStmt):
            raise ReturnSignal(None if stmt.expr is None else self.eval(stmt.expr))
        elif isinstance(stmt, BreakStmt):
            raise BreakSignal()
        elif isinstance(stmt, ContinueStmt):
            raise ContinueSignal()
        elif isinstance(stmt, EventDecl):
            pass
        else:
            raise RuntimeSBGError(f"unknown statement {stmt}")

    def eval(self, expr: Any) -> Any:
        try:
            return self._eval(expr)
        except RuntimeSBGError as e:
            attach_location(e, expr)
            raise
        except (TypeError, ValueError, ZeroDivisionError, IndexError) as e:
            err = RuntimeSBGError(str(e))
            attach_location(err, expr)
            raise err from e

    def _eval(self, expr: Any) -> Any:
        if isinstance(expr, Literal):
            return expr.value
        if isinstance(expr, VarExpr):
            if expr.name in self.vars:
                return self.vars[expr.name]
            if expr.name in self.lists:
                return self.lists[expr.name]
            raise RuntimeSBGError(f"unknown variable/list {expr.name!r}")
        if isinstance(expr, UnaryExpr):
            v = self.eval(expr.expr)
            if expr.op == "-": return -self.num(v)
            if expr.op == "!": return not self.truthy(v)
        if isinstance(expr, BinaryExpr):
            a = self.eval(expr.left)
            if expr.op == "&&":
                return self.truthy(a) and self.truthy(self.eval(expr.right))
            if expr.op == "||":
                return self.truthy(a) or self.truthy(self.eval(expr.right))
            b = self.eval(expr.right)
            if expr.op == "+":
                if isinstance(a, str) or isinstance(b, str): return str(a) + str(b)
                return a + b
            if expr.op == "-": return self.num(a) - self.num(b)
            if expr.op == "*": return self.num(a) * self.num(b)
            if expr.op == "/": return self.num(a) / self.num(b)
            if expr.op == "%": return self.num(a) % self.num(b)
            if expr.op == "==": return a == b
            if expr.op == "!=": return a != b
            if expr.op == "<": return a < b
            if expr.op == "<=": return a <= b
            if expr.op == ">": return a > b
            if expr.op == ">=": return a >= b
        if isinstance(expr, ArrayExpr):
            return [self.eval(x) for x in expr.items]
        if isinstance(expr, CallExpr):
            try:
                return self.call(expr.callee, [self.eval(x) for x in expr.args])
            except RuntimeSBGError as e:
                attach_location(e, expr)
                raise
        raise RuntimeSBGError(f"unknown expression {expr}")

    def call(self, name: str, args: List[Any]) -> Any:
        if name in self.procs:
            proc = self.procs[name]
            if len(args) != len(proc.params):
                raise RuntimeSBGError(f"{name} expects {len(proc.params)} args, got {len(args)}")
            saved = dict(self.vars)
            for p, a in zip(proc.params, args):
                self.vars[p] = a
            try:
                self.exec_block(proc.body)
            except ReturnSignal as r:
                self.vars = saved
                return r.value
            # preserve global mutations, but remove params
            for p in proc.params:
                self.vars.pop(p, None)
            for k, v in saved.items():
                if k not in self.vars:
                    self.vars[k] = v
            return None

        if name == "log":
            text = " ".join(str(a) for a in args)
            self.output.append(text)
            self.lists.setdefault("Terminal", []).append(text)
            print(text)
            return None
        if name == "wait":
            if not self.fast:
                time.sleep(float(args[0]) if args else 0)
            return None
        if name == "ask":
            question = str(args[0]) if args else ""
            self.answer_value = input(question + " ")
            return self.answer_value
        if name == "answer": return self.answer_value
        if name == "broadcast":
            self.run_message(str(args[0]))
            return None
        if name == "broadcastAndWait":
            self.run_message(str(args[0]))
            return None
        if name == "len":
            obj = args[0]
            return len(obj)
        if name == "item":
            lst = self.get_list_arg(args[0])
            idx = int(args[1]) - 1
            return lst[idx]
        if name == "push":
            lst = self.get_list_arg(args[0], require_name=True)
            lst.append(args[1])
            return None
        if name == "insert":
            lst = self.get_list_arg(args[0], require_name=True)
            lst.insert(max(0, int(args[1]) - 1), args[2])
            return None
        if name == "delete":
            lst = self.get_list_arg(args[0], require_name=True)
            del lst[int(args[1]) - 1]
            return None
        if name == "replace":
            lst = self.get_list_arg(args[0], require_name=True)
            lst[int(args[1]) - 1] = args[2]
            return None
        if name == "contains":
            return args[1] in self.get_list_arg(args[0])
        if name == "join":
            return "".join(str(a) for a in args)
        if name == "random": return random.uniform(float(args[0]), float(args[1]))
        if name == "round": return round(float(args[0]))
        if name == "floor": return math.floor(float(args[0]))
        if name == "ceil": return math.ceil(float(args[0]))
        if name == "sqrt": return math.sqrt(float(args[0]))
        if name == "abs": return abs(float(args[0]))
        if name == "min": return min(args)
        if name == "max": return max(args)
        if name == "timer": return time.monotonic() - self.timer_start
        if name == "resetTimer": self.timer_start = time.monotonic(); return None
        if name in ("setBackdrop", "nextBackdrop", "playSound", "stopAllSounds"):
            # Runner is headless; compiler emits real Scratch blocks for these.
            return None
        raise RuntimeSBGError(f"unknown function {name!r}")

    def get_list_arg(self, value: Any, require_name: bool = False) -> List[Any]:
        if isinstance(value, str) and value in self.lists:
            return self.lists[value]
        if isinstance(value, list):
            return value
        raise RuntimeSBGError("expected list value/name")

    @staticmethod
    def truthy(v: Any) -> bool:
        return bool(v)

    @staticmethod
    def num(v: Any) -> float:
        if isinstance(v, bool): return 1 if v else 0
        return float(v)

