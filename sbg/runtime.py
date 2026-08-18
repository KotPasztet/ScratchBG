from __future__ import annotations

import math
import os
import random
import re
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .errors import RuntimeSBGError, attach_location
from .ast import (
    Token, Lexer, Program, ImportDecl, VarDecl, ListDecl, EventDecl, ProcDecl,
    BlockStmt, IfStmt, RepeatStmt, ForeverStmt, WhileStmt, ForStmt,
    ReturnStmt, BreakStmt, ContinueStmt, AssignStmt, ExprStmt,
    Literal, VarExpr, BinaryExpr, UnaryExpr, CallExpr, ArrayExpr,
    TargetDecl, LValueAssignStmt, StructDecl, StructVarDecl, NestedVectorDecl,
)
from .globals import (
    ACTION_PROC_NAME, TERMINAL_LIST_NAME, EMBEDDED_FILE_LIST_NAMES,
    SBG_NOW_VAR, SBG_LAST_VAR, SBG_RAW_DT_VAR, SBG_DT_VAR, SBG_DT_SCALE_VAR,
    SBG_DT_CAP_VAR, SBG_FIXED_DT_VAR, SBG_FRAME_VAR, SBG_FPS_VAR, SBG_TURBO_VAR,
    SBG_DELTA_VARS, TERMINAL_VISIBLE_VAR, TERMINAL_INPUT_ENABLED_VAR,
    SBG_KEY_ALIASES, NO_ACTION_RETURN,
)
# Struct/vector type tables: populated during parsing (see parser.py), read
# here during native interpretation of struct/flat-vector expressions. These
# are the same dict *objects* the parser writes to -- sharing them (instead
# of each module owning an independent copy) preserves the original
# process-global-registry behavior. Wiring this through registry.ProgramRegistry
# per-Program is tracked as Phase 1 step 3 follow-up, not done yet.
from .parser import (
    _SBG_STRUCT_DEFS21, _SBG_FLAT_VECTOR_TYPES21, _SBG_STRUCT_DEFAULT_BASE21,
)

_OLD_NO_ACTION_RETURN = NO_ACTION_RETURN
_SBG_KEY_ALIASES = SBG_KEY_ALIASES

# =============================================================================
# Interpreter
# =============================================================================

class ReturnSignal(Exception):
    def __init__(self, value: Any): self.value = value
class BreakSignal(Exception): pass
class ContinueSignal(Exception): pass

def _sbg_sanitize_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", name)

def _sbg_encode_runtime_vec21(value: Any) -> str:
    if isinstance(value, str):
        # Already encoded row string, unless it is an ordinary scalar string.
        return value
    if isinstance(value, list):
        return " ".join(str(x) for x in value)
    return str(value)

def _sbg_is_vector21(t: str) -> bool:
    return _sbg_vector_inner21(t) is not None

def _sbg_vector_inner21(t: str) -> Optional[str]:
    t = _sbg_norm_type21(t)
    if not t.startswith("vector<") or not t.endswith(">"):
        return None
    inner = t[len("vector<"):-1]
    return inner

def _sbg_norm_type21(t: str) -> str:
    return re.sub(r"\s+", "", t.replace("std::", "")).strip()

def _sbg_vec_at0_runtime_patch20(value: Any, idx: int) -> Any:
    vals = _sbg_vec_tokens_runtime_patch20(value)
    if idx < 0 or idx >= len(vals):
        return 0
    return _sbg_num_or_text_patch20(vals[idx])

def _sbg_vec_tokens_runtime_patch20(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(x) for x in value]
    text = str(value).strip()
    if not text:
        return []
    # Scratch list reporter joins items with spaces. Also accept CSV/semicolon.
    if "\x1f" in text:
        return [x for x in text.split("\x1f") if x != ""]
    if "," in text:
        return [x.strip() for x in text.split(",") if x.strip()]
    return [x for x in text.split() if x]

def _sbg_num_or_text_patch20(x: str) -> Any:
    try:
        f = float(x)
        return int(f) if f.is_integer() else f
    except Exception:
        return x

def _sbg_format_number(x: Any) -> str:
    """Format number for display: hide .0 for integer values, matching Scratch behavior.
    CRITICAL FIX: pkt 5 - ensure 610.0 displays as '610', not '610.0'."""
    if isinstance(x, bool):
        return str(x)  # bool before float, since bool is a subclass of int
    if isinstance(x, (int, float)):
        if isinstance(x, float) and x.is_integer():
            return str(int(x))
        return str(x)
    return str(x)

def _sbg_ref_static_name_patch20(expr: Any) -> Optional[str]:
    """Return a Scratch variable/list name for a statically-known lvalue."""
    if isinstance(expr, VarExpr):
        return expr.name
    if isinstance(expr, CallExpr) and expr.callee == "__field_ref" and len(expr.args) == 2 and isinstance(expr.args[1], Literal):
        base = _sbg_ref_static_name_patch20(expr.args[0])
        if base is not None:
            return f"{base}.{expr.args[1].value}"
    return None

def _sbg_unique_mangled_name21(names: Iterable[str], original: str) -> Optional[str]:
    suffix = "_" + _sbg_sanitize_name(original)
    found = [n for n in names if n.endswith(suffix) and n.startswith("__loc_")]
    return found[0] if len(found) == 1 else None

def _sbg_latest_mangled_name21(names: Iterable[str], original: str) -> Optional[str]:
    suffix = "_" + _sbg_sanitize_name(original)
    best: Optional[Tuple[int, str]] = None
    for n in names:
        if not (n.startswith("__loc_") and n.endswith(suffix)):
            continue
        try:
            num = int(n.split("_", 3)[2])
        except Exception:
            num = -1
        if best is None or num > best[0]:
            best = (num, n)
    return best[1] if best else None

def _sbg_match_flat_struct_vec_item21(expr: Any) -> Optional[Tuple[str, Any, Any, str, Any]]:
    if isinstance(expr, CallExpr) and expr.callee == "__index0_ref" and len(expr.args) == 2:
        m = _sbg_match_flat_struct_field21(expr.args[0])
        if m:
            base, row, pos, field = m
            return base, row, pos, field, expr.args[1]
    return None

def _sbg_match_flat_struct_field21(expr: Any) -> Optional[Tuple[str, Any, Any, str]]:
    if isinstance(expr, CallExpr) and expr.callee == "__field_ref" and len(expr.args) == 2 and isinstance(expr.args[1], Literal):
        m = _sbg_match_flat_struct_elem21(expr.args[0])
        if m:
            base, row, pos = m
            return base, row, pos, str(expr.args[1].value)
    return None

def _sbg_match_flat_struct_elem21(expr: Any) -> Optional[Tuple[str, Any, Any]]:
    if isinstance(expr, CallExpr) and expr.callee == "__index0_ref" and len(expr.args) == 2:
        row_expr, pos = expr.args
        if isinstance(row_expr, CallExpr) and row_expr.callee == "__index0_ref" and len(row_expr.args) == 2:
            base, row = row_expr.args
            if isinstance(base, VarExpr) and base.name in _SBG_FLAT_VECTOR_TYPES21:
                return base.name, row, pos
    return None

def _sbg_match_nested_vector_item22(expr: Any) -> Optional[Tuple[str, Any, Any]]:
    if isinstance(expr, CallExpr) and expr.callee == "__index0_ref" and len(expr.args) == 2:
        row_expr, col = expr.args
        if isinstance(row_expr, CallExpr) and row_expr.callee == "__index0_ref" and len(row_expr.args) == 2:
            base, row = row_expr.args
            if isinstance(base, VarExpr) and base.name in globals().get("_SBG_NESTED_VECTOR_NAMES21", set()) and base.name not in globals().get("_SBG_FLAT_VECTOR_TYPES21", {}):
                return base.name, row, col
    return None

def _sbg_default_for_type21(typ: str) -> Any:
    typ = _sbg_norm_type21(typ)
    if typ in {"string", "char"}:
        return Literal("")
    if typ == "bool":
        return Literal(False)
    return Literal(0)

def _sbg_normalize_key_name_patch24(value: Any) -> str:
    key = str(value)
    return _SBG_KEY_ALIASES.get(key, _SBG_KEY_ALIASES.get(key.lower(), key))

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
        return self._runtime_run_action_patch14(input_value)

    def run_message(self, msg: str) -> None:
        for ev in self.message_events.get(msg, []):
            self.exec_block(ev.body)

    def _base_prepare_scratch_console(self) -> None:
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

    def _base__exec_stmt(self, stmt: Any) -> None:
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
        # CRITICAL FIX: pkt 3 - errors should be handled gracefully in-place,
        # not converted to exceptions. Real Scratch doesn't crash on div/0 or index errors,
        # it just returns Infinity or empty string. Native runtime should match.

    def _base__eval(self, expr: Any) -> Any:
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
            if expr.op == "/":
                b_num = self.num(b)
                # CRITICAL FIX: pkt 3 - match Scratch behavior: division by zero returns Infinity, not crash
                if b_num == 0:
                    return float('inf') if self.num(a) >= 0 else float('-inf')
                return self.num(a) / b_num
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

    def _base_call(self, name: str, args: List[Any]) -> Any:
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
            text = " ".join(_sbg_format_number(a) for a in args)
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
            # CRITICAL FIX: pkt 3 - match Scratch behavior: out-of-bounds returns empty string/0
            if idx < 0 or idx >= len(lst):
                return 0  # or "" for string context; Scratch unifies both to empty/0
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
            return "".join(_sbg_format_number(a) for a in args)
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


    def _runtime_eval_patch21p(self, expr: Any) -> Any:
        if isinstance(expr, VarExpr) and expr.name not in self.vars and expr.name not in self.lists:
            alt = _sbg_latest_mangled_name21([*self.vars.keys(), *self.lists.keys()], expr.name)
            if alt:
                return self.vars[alt] if alt in self.vars else self.lists[alt]
        return self._runtime_eval_patch21o(expr)

    def _runtime_eval_patch21o(self, expr: Any) -> Any:
        if isinstance(expr, CallExpr) and expr.callee == "__flat_struct_resize_outer" and len(expr.args) >= 2:
            base = str(self.eval(expr.args[0]))
            n = max(0, int(float(self.eval(expr.args[1]))))
            self.lists[base] = (self.lists.get(base, []) + [""] * n)[:n]
            self.lists[f"{base}.__row_size"] = (self.lists.get(f"{base}.__row_size", []) + [0] * n)[:n]
            # Also keep legacy native rows for code paths that still use self.vars.
            rows = self.vars.setdefault(base, [])
            if not isinstance(rows, list):
                rows = []; self.vars[base] = rows
            while len(rows) > n: rows.pop()
            while len(rows) < n: rows.append([])
            return None

        if isinstance(expr, CallExpr) and expr.callee == "__flat_struct_push" and len(expr.args) == 3:
            base = str(self.eval(expr.args[0]))
            row = max(0, int(float(self.eval(expr.args[1]))))
            st = _SBG_FLAT_VECTOR_TYPES21.get(base)
            fields = _SBG_STRUCT_DEFS21.get(st or "", [])
            if not st or not fields:
                # Fall back to older behavior if this is not a known flattened struct vector.
                return self._runtime_eval_patch21m(expr)

            # Ensure row metadata exists.
            while len(self.lists.setdefault(base, [])) <= row:
                self.lists[base].append("")
            while len(self.lists.setdefault(f"{base}.__row_size", [])) <= row:
                self.lists[f"{base}.__row_size"].append(0)

            first_field = fields[0][1]
            flat_idx = len(self.lists.setdefault(f"{base}.{first_field}", []))
            row_text = str(self.lists[base][row])
            self.lists[base][row] = (row_text + (" " if row_text else "") + str(flat_idx))
            self.lists[f"{base}.__row_size"][row] = int(float(self.lists[f"{base}.__row_size"][row] or 0)) + 1

            val_expr = expr.args[2]
            val_obj = None
            # Only evaluate the value if needed; for static struct variables we copy
            # from n.field / n.vectorField, which is exactly how the compiler lowers it.
            if not isinstance(val_expr, VarExpr):
                val_obj = self.eval(val_expr)

            for ftyp, fname in fields:
                dst = f"{base}.{fname}"
                self.lists.setdefault(dst, [])
                copied = 0
                if isinstance(val_expr, VarExpr):
                    src = f"{val_expr.name}.{fname}"
                    alt_src = src
                    # Locals may be name-mangled; recover the single matching local field.
                    if src not in self.vars and src not in self.lists:
                        suffix = "." + fname
                        candidates = [n for n in list(self.vars.keys()) + list(self.lists.keys()) if n.endswith(suffix) and n.split('.')[-2].endswith('_' + _sbg_sanitize_name(val_expr.name))]
                        if len(candidates) == 1:
                            alt_src = candidates[0]
                    if _sbg_is_vector21(ftyp):
                        if alt_src in self.lists:
                            self.lists[dst].append(_sbg_encode_runtime_vec21(self.lists[alt_src])); copied = 1
                        elif alt_src in self.vars:
                            self.lists[dst].append(_sbg_encode_runtime_vec21(self.vars[alt_src])); copied = 1
                    else:
                        if alt_src in self.vars:
                            self.lists[dst].append(self.vars[alt_src]); copied = 1
                        elif alt_src in self.lists:
                            self.lists[dst].append(_sbg_encode_runtime_vec21(self.lists[alt_src])); copied = 1
                if not copied:
                    if isinstance(val_obj, dict):
                        raw = val_obj.get(fname, [] if _sbg_is_vector21(ftyp) else 0)
                        self.lists[dst].append(_sbg_encode_runtime_vec21(raw) if _sbg_is_vector21(ftyp) else raw)
                    else:
                        self.lists[dst].append("" if _sbg_is_vector21(ftyp) else 0)

            # Legacy mirror for older native paths.
            rows = self.vars.setdefault(base, [])
            if isinstance(rows, list):
                while len(rows) <= row: rows.append([])
                rows[row].append(flat_idx)
            return None

        return self._runtime_eval_patch21m(expr)

    def _runtime_eval_patch21m(self, expr: Any) -> Any:
        if isinstance(expr, VarExpr) and expr.name in _SBG_FLAT_VECTOR_TYPES21 and expr.name in self.lists:
            return self.lists[expr.name]
        if isinstance(expr, CallExpr) and expr.callee == "__field_ref" and len(expr.args) == 2:
            obj = self.eval(expr.args[0]); field = str(self.eval(expr.args[1]))
            if isinstance(obj, (int, float)):
                idx = int(float(obj))
                for _st, base in _SBG_STRUCT_DEFAULT_BASE21.items():
                    lname = f"{base}.{field}"
                    if lname in self.lists and 0 <= idx < len(self.lists[lname]):
                        val = self.lists[lname][idx]
                        # Vector fields are stored as encoded row strings; native code wants a list.
                        ftyp = None
                        for t, f in _SBG_STRUCT_DEFS21.get(_st, []):
                            if f == field: ftyp = t; break
                        if ftyp and _sbg_is_vector21(ftyp):
                            return [_sbg_num_or_text_patch20(x) for x in _sbg_vec_tokens_runtime_patch20(val)]
                        return val
        return self._runtime_eval_patch21h(expr)

    def _runtime_eval_patch21h(self, expr: Any) -> Any:
        if isinstance(expr, VarExpr):
            if expr.name not in self.vars and expr.name not in self.lists:
                alt = _sbg_unique_mangled_name21([*self.vars.keys(), *self.lists.keys()], expr.name)
                if alt:
                    return self.vars[alt] if alt in self.vars else self.lists[alt]
        return self._runtime_eval_patch21(expr)

    def _runtime_eval_patch21(self, expr: Any) -> Any:
        if isinstance(expr, CallExpr) and expr.callee == "__field_ref":
            base_expr, field_expr = expr.args
            field = str(self.eval(field_expr))
            # Static flattened variable/list, e.g. n.b / n.w.
            static_name = _sbg_ref_static_name_patch20(expr)
            if static_name and static_name in self.vars:
                return self.vars[static_name]
            if static_name and static_name in self.lists:
                return self.lists[static_name]
            obj = self.eval(base_expr)
            if isinstance(obj, dict):
                return obj.get(field, 0)
        if isinstance(expr, CallExpr) and expr.callee == "__index0_ref":
            obj = self.eval(expr.args[0])
            idx = int(float(self.eval(expr.args[1])))
            if isinstance(obj, str):
                return _sbg_vec_at0_runtime_patch20(obj, idx)
            if isinstance(obj, list):
                return obj[idx]
        return self._runtime_eval_patch20(expr)

    def _runtime_eval_patch20(self, expr: Any) -> Any:
        if isinstance(expr, CallExpr) and expr.callee == "__field_ref":
            obj = self.eval(expr.args[0]); field = str(self.eval(expr.args[1]))
            if isinstance(obj, dict):
                return obj.get(field, 0)
            name = _sbg_ref_static_name_patch20(expr)
            if name and name in self.vars:
                return self.vars[name]
            if name and name in self.lists:
                return self.lists[name]
            return 0
        if isinstance(expr, CallExpr) and expr.callee == "__index0_ref":
            obj = self.eval(expr.args[0]); idx = int(float(self.eval(expr.args[1])))
            if isinstance(obj, str):
                return _sbg_vec_at0_runtime_patch20(obj, idx)
            return obj[idx]
        return self._base__eval(expr)

    def _runtime_exec_stmt_patch22c_flat(self, stmt: Any) -> None:
        if isinstance(stmt, LValueAssignStmt):
            mf = _sbg_match_flat_struct_field21(stmt.target)
            mv = _sbg_match_flat_struct_vec_item21(stmt.target)
            if mf and not mv:
                base, row_expr, pos_expr, field = mf
                row = int(float(self.eval(row_expr)))
                pos = int(float(self.eval(pos_expr)))
                sizes = self.lists.setdefault(f"{base}.__row_size", [])
                idx = 0
                for r in range(max(0, row)):
                    idx += int(float(sizes[r] if r < len(sizes) and sizes[r] != "" else 0))
                idx += pos
                arr = self.lists.setdefault(f"{base}.{field}", [])
                while len(arr) <= idx:
                    arr.append(0)
                old = arr[idx]
                rhs = self.eval(stmt.expr)
                if stmt.op == "=": new = rhs
                elif stmt.op == "+=": new = old + rhs
                elif stmt.op == "-=": new = old - rhs
                elif stmt.op == "*=": new = old * rhs
                elif stmt.op == "/=": new = old / rhs
                elif stmt.op == "%=": new = old % rhs
                else: new = rhs
                arr[idx] = new
                return None
        return self._runtime_exec_stmt_patch22b_nested(stmt)

    def _runtime_exec_stmt_patch22b_nested(self, stmt: Any) -> None:
        if isinstance(stmt, LValueAssignStmt):
            m = _sbg_match_nested_vector_item22(stmt.target)
            if m:
                base, row_expr, col_expr = m
                row = int(float(self.eval(row_expr)))
                col = int(float(self.eval(col_expr)))
                rows_obj = self.vars.setdefault(base, [])
                if not isinstance(rows_obj, list):
                    rows_obj = []
                    self.vars[base] = rows_obj
                while len(rows_obj) <= row:
                    rows_obj.append([])
                if not isinstance(rows_obj[row], list):
                    rows_obj[row] = [_sbg_num_or_text_patch20(x) for x in _sbg_vec_tokens_runtime_patch20(rows_obj[row])]
                while len(rows_obj[row]) <= col:
                    rows_obj[row].append(0)
                old = rows_obj[row][col]
                rhs = self.eval(stmt.expr)
                if stmt.op == "=": new = rhs
                elif stmt.op == "+=": new = old + rhs
                elif stmt.op == "-=": new = old - rhs
                elif stmt.op == "*=": new = old * rhs
                elif stmt.op == "/=": new = old / rhs
                elif stmt.op == "%=": new = old % rhs
                else: new = rhs
                rows_obj[row][col] = new
                self.lists[base] = [" ".join(str(x) for x in r) if isinstance(r, list) else str(r) for r in rows_obj]
                return None
        return self._runtime_exec_stmt_patch22_nested(stmt)

    def _runtime_exec_stmt_patch22_nested(self, stmt: Any) -> None:
        if isinstance(stmt, LValueAssignStmt):
            m = _sbg_match_nested_vector_item22(stmt.target)
            if m:
                base, row_expr, col_expr = m
                row = int(float(self.eval(row_expr)))
                col = int(float(self.eval(col_expr)))
                rows = self.lists.setdefault(base, [])
                while len(rows) <= row:
                    rows.append("")
                items = _sbg_vec_tokens_runtime_patch20(rows[row])
                while len(items) <= col:
                    items.append("0")
                old = _sbg_num_or_text_patch20(items[col])
                rhs = self.eval(stmt.expr)
                if stmt.op == "=": new = rhs
                elif stmt.op == "+=": new = old + rhs
                elif stmt.op == "-=": new = old - rhs
                elif stmt.op == "*=": new = old * rhs
                elif stmt.op == "/=": new = old / rhs
                elif stmt.op == "%=": new = old % rhs
                else: new = rhs
                items[col] = str(new)
                rows[row] = " ".join(str(x) for x in items)
                return None
        return self._runtime_exec_stmt_patch21l(stmt)

    def _runtime_exec_stmt_patch21l(self, stmt: Any) -> None:
        if isinstance(stmt, AssignStmt):
            name = stmt.name
            if name not in self.lists:
                alt = _sbg_unique_mangled_name21(self.lists.keys(), name)
                if alt: name = alt
            if name in self.lists and stmt.op == "=":
                rhs = self.eval(stmt.expr)
                self.lists[name].clear()
                if isinstance(rhs, list): self.lists[name].extend(rhs)
                else: self.lists[name].extend(_sbg_vec_tokens_runtime_patch20(rhs))
                return None
        return self._runtime_exec_stmt_patch21(stmt)

    def _runtime_exec_stmt_patch21(self, stmt: Any) -> None:
        if isinstance(stmt, StructDecl):
            _SBG_STRUCT_DEFS21[stmt.name] = stmt.fields
            return None
        if isinstance(stmt, StructVarDecl):
            obj: Dict[str, Any] = {"__type": stmt.typ}
            for ftyp, fname in _SBG_STRUCT_DEFS21.get(stmt.typ, []):
                obj[fname] = [] if _sbg_is_vector21(ftyp) else self.eval(_sbg_default_for_type21(ftyp))
            self.vars[stmt.name] = obj
            return None
        if isinstance(stmt, NestedVectorDecl):
            self.vars[stmt.name] = [[self.eval(x) for x in r.items] if isinstance(r, ArrayExpr) else [] for r in stmt.rows]
            self.lists[stmt.name] = [" ".join(str(self.eval(x)) for x in r.items) if isinstance(r, ArrayExpr) else "" for r in stmt.rows]
            return None
        if isinstance(stmt, LValueAssignStmt):
            rhs = self.eval(stmt.expr)
            def apply(old: Any) -> Any:
                if stmt.op == "=": return rhs
                if stmt.op == "+=": return old + rhs
                if stmt.op == "-=": return old - rhs
                if stmt.op == "*=": return old * rhs
                if stmt.op == "/=": return old / rhs
                if stmt.op == "%=": return old % rhs
                return rhs
            # Dict field assignment: n.b = x; n.w = vector;
            if isinstance(stmt.target, CallExpr) and stmt.target.callee == "__field_ref":
                base_expr, field_expr = stmt.target.args
                field = str(self.eval(field_expr))
                # Static flattened Scratch-compatible name first.
                static_name = _sbg_ref_static_name_patch20(stmt.target)
                if static_name and static_name in self.lists:
                    dst = self.lists[static_name]
                    if stmt.op != "=":
                        raise RuntimeSBGError("list field only supports '=' assignment")
                    dst.clear()
                    if isinstance(rhs, list): dst.extend(rhs)
                    else: dst.extend(_sbg_vec_tokens_runtime_patch20(rhs))
                    return None
                if static_name and static_name in self.vars:
                    self.vars[static_name] = apply(self.vars.get(static_name, 0)); return None
                obj = self.eval(base_expr)
                if isinstance(obj, dict):
                    old = obj.get(field, [] if isinstance(rhs, list) else 0)
                    obj[field] = apply(old)
                    return None
            if isinstance(stmt.target, CallExpr) and stmt.target.callee == "__index0_ref":
                obj_expr, idx_expr = stmt.target.args
                idx = int(float(self.eval(idx_expr)))
                obj = self.eval(obj_expr)
                if isinstance(obj, list):
                    obj[idx] = apply(obj[idx])
                    return None
            return self._runtime_exec_stmt_patch20(stmt)
        return self._runtime_exec_stmt_patch20(stmt)

    def _runtime_exec_stmt_patch20(self, stmt: Any) -> None:
        if isinstance(stmt, LValueAssignStmt):
            rhs = self.eval(stmt.expr)
            def apply(old: Any) -> Any:
                if stmt.op == "=": return rhs
                if stmt.op == "+=": return old + rhs
                if stmt.op == "-=": return old - rhs
                if stmt.op == "*=": return old * rhs
                if stmt.op == "/=": return old / rhs
                if stmt.op == "%=": return old % rhs
                return rhs
            if isinstance(stmt.target, CallExpr) and stmt.target.callee == "__field_ref":
                name = _sbg_ref_static_name_patch20(stmt.target)
                if name is None:
                    raise RuntimeSBGError("dynamic field assignment is not supported yet")
                self.vars[name] = apply(self.vars.get(name, 0))
                return None
            if isinstance(stmt.target, CallExpr) and stmt.target.callee == "__index0_ref":
                obj_expr, idx_expr = stmt.target.args
                idx = int(float(self.eval(idx_expr)))
                name = _sbg_ref_static_name_patch20(obj_expr)
                if name and name in self.lists:
                    lst = self.lists[name]
                    lst[idx] = apply(lst[idx])
                    return None
                obj = self.eval(obj_expr)
                if isinstance(obj, list):
                    obj[idx] = apply(obj[idx])
                    return None
                raise RuntimeSBGError("index assignment needs a mutable vector/list")
            raise RuntimeSBGError("unsupported assignment target")
        return self._base__exec_stmt(stmt)

    def _runtime_call_patch24(self, name: str, args: List[Any]) -> Any:
        if name == "keyPressed":
            key = _sbg_normalize_key_name_patch24(args[0] if args else "any")
            raw = os.environ.get("SBG_KEYS", "")
            pressed = {_sbg_normalize_key_name_patch24(x.strip()) for x in raw.split(",") if x.strip()}
            return bool(key == "any" and pressed) or key in pressed
        return self._runtime_call_patch23(name, args)

    def _runtime_call_patch23(self, name: str, args: List[Any]) -> Any:
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
        return self._runtime_call_patch21n(name, args)

    def _runtime_call_patch21n(self, name: str, args: List[Any]) -> Any:
        if name in {"len", "size"} and len(args) == 1:
            # CRITICAL FIX: For len()/size(), always count characters for strings
            # NEVER use heuristic tokenization, as strings like "a b" or "a,b" will be
            # incorrectly split. Strings are atomic values; only vector<T> deserialization
            # should use tokenization, and those come as lists, not strings.
            if isinstance(args[0], str):
                return len(args[0])
            elif isinstance(args[0], list):
                return len(args[0])
        if name == "item" and len(args) == 2 and isinstance(args[0], str) and args[0] not in self.lists:
            return _sbg_vec_at0_runtime_patch20(args[0], int(float(args[1])) - 1)
        return self._runtime_call_patch21j(name, args)

    def _runtime_call_patch21j(self, name: str, args: List[Any]) -> Any:
        if name == "__nested_resize_outer":
            base = str(args[0]); n = max(0, int(float(args[1])))
            rows = self.vars.setdefault(base, [])
            if not isinstance(rows, list): rows = []; self.vars[base] = rows
            while len(rows) > n: rows.pop()
            while len(rows) < n: rows.append([])
            self.lists[base] = [" ".join(str(x) for x in r) if isinstance(r, list) else str(r) for r in rows]
            return None
        if name == "__nested_row_push":
            base = str(args[0]); row = int(float(args[1])); val = args[2]
            rows = self.vars.setdefault(base, [])
            while len(rows) <= row: rows.append([])
            if not isinstance(rows[row], list): rows[row] = _sbg_vec_tokens_runtime_patch20(rows[row])
            rows[row].append(val)
            self.lists[base] = [" ".join(str(x) for x in r) if isinstance(r, list) else str(r) for r in rows]
            return None
        if name == "__nested_row_clear":
            base = str(args[0]); row = int(float(args[1])); rows = self.vars.setdefault(base, [])
            while len(rows) <= row: rows.append([])
            rows[row] = []
            self.lists[base] = [" ".join(str(x) for x in r) if isinstance(r, list) else str(r) for r in rows]
            return None
        return self._runtime_call_patch21d(name, args)

    def _runtime_call_patch21d(self, name: str, args: List[Any]) -> Any:
        if name == "__flat_struct_resize_outer":
            base = str(args[0]); n = max(0, int(float(args[1])))
            rows = self.vars.setdefault(base, [])
            if not isinstance(rows, list):
                rows = []; self.vars[base] = rows
            while len(rows) > n: rows.pop()
            while len(rows) < n: rows.append([])
            self.lists[base] = ["" for _ in range(n)]
            self.lists[f"{base}.__row_size"] = [len(r) if isinstance(r, list) else 0 for r in rows]
            return None
        if name == "__flat_struct_push":
            base = str(args[0]); row = int(float(args[1])); value = args[2]
            rows = self.vars.setdefault(base, [])
            while len(rows) <= row: rows.append([])
            if isinstance(value, dict): value = dict(value)
            rows[row].append(value)
            self.lists[base] = [" ".join(str(i) for i in range(len(r))) for r in rows]
            self.lists[f"{base}.__row_size"] = [len(r) for r in rows]
            return None
        if name == "__flat_struct_row_size":
            base = str(args[0]); row = int(float(args[1]))
            rows = self.vars.get(base, [])
            return len(rows[row]) if isinstance(rows, list) and 0 <= row < len(rows) else 0
        return self._runtime_call_patch21(name, args)

    def _runtime_call_patch21(self, name: str, args: List[Any]) -> Any:
        # Vector methods should work on actual list values, not only named Scratch lists.
        if name in {"push", "push_back"}:
            if isinstance(args[0], list):
                val = args[1]
                if isinstance(val, dict): val = dict(val)
                elif isinstance(val, list): val = list(val)
                args[0].append(val); return None
        if name in {"clear"}:
            if isinstance(args[0], list): args[0].clear(); return None
        if name in {"resize"}:
            if isinstance(args[0], list):
                n = max(0, int(float(args[1]))); val = args[2] if len(args) >= 3 else []
                while len(args[0]) > n: args[0].pop()
                while len(args[0]) < n: args[0].append([] if isinstance(val, list) else val)
                return None
        if name in {"size", "len"} and len(args) == 1:
            return len(args[0])
        if name == "at0" and len(args) == 2:
            if isinstance(args[0], list): return args[0][int(float(args[1]))]
            return _sbg_vec_at0_runtime_patch20(args[0], int(float(args[1])))
        return self._runtime_call_patch20(name, args)

    def _runtime_call_patch20(self, name: str, args: List[Any]) -> Any:
        if name in {"cout", "print"}:
            text = "".join(_sbg_format_number(a) for a in args)
            # C++ cout may contain newlines; mirror it as separate terminal rows.
            rows = text.split("\n")
            for row in rows:
                if row != "":
                    self.call("log", [row])
            return None
        if name == "println":
            return self.call("log", ["".join(_sbg_format_number(a) for a in args)])
        if name == "cin":
            # Native runner asks from stdin. Scratch compiler emits ask-and-wait.
            for target in args:
                val = input(">> ")
                try:
                    val = float(val)
                    if val.is_integer(): val = int(val)
                except Exception:
                    pass
                self.vars[str(target)] = val
            return None
        if name == "at0":
            return _sbg_vec_at0_runtime_patch20(args[0], int(float(args[1])))
        if name == "vec_size":
            return len(_sbg_vec_tokens_runtime_patch20(args[0]))
        if name == "pow":
            return math.pow(float(args[0]), float(args[1]))
        if name == "exp": return math.exp(float(args[0]))
        if name == "ln": return math.log(float(args[0]))
        if name == "log10": return math.log10(float(args[0]))
        if name == "sin": return math.sin(float(args[0]))
        if name == "cos": return math.cos(float(args[0]))
        if name == "tan": return math.tan(float(args[0]))
        if name in {"randuble", "rand_double", "random_double"}:
            lo = float(args[0]) if len(args) >= 1 else -1.0
            hi = float(args[1]) if len(args) >= 2 else 1.0
            return random.uniform(lo, hi)
        if name in {"dis", "uniform_real"}:
            return random.uniform(-1.0, 1.0)
        if name == "__new_struct":
            return {"__type": str(args[0]) if args else "struct"}
        return self._runtime_call_patch19(name, args)

    def _runtime_call_patch19(self, name: str, args: List[Any]) -> Any:
        if name == "pq_error_value":
            return self.vars.get("pq_error", "")
        if name == "maxpq_error_value":
            return self.vars.get("maxpq_error", "")
        return self._runtime_call_patch18b(name, args)

    def _runtime_call_patch18b(self, name: str, args: List[Any]) -> Any:
        if name == "size" and len(args) == 0:
            return self._runtime_call_patch17(name, args)
        return self._runtime_call_patch18(name, args)

    def _runtime_call_patch18(self, name: str, args: List[Any]) -> Any:
        if name in {"size", "len"} and len(args) == 1:
            return len(args[0])
        if name == "empty":
            return 1 if len(args[0]) == 0 else 0
        if name == "front":
            return self.get_list_arg(args[0])[0]
        if name == "back":
            return self.get_list_arg(args[0])[-1]
        if name == "at":
            return self.get_list_arg(args[0])[int(args[1]) - 1]
        if name in {"str", "to_string"}:
            return str(args[0])
        if name in {"stoi", "stod"}:
            try:
                x = float(args[0])
                if name == "stoi":
                    return int(x)
                return x
            except Exception:
                return 0
        if name == "push_back":
            return self.call("push", args)
        if name == "pop_back":
            lst = self.get_list_arg(args[0], require_name=True)
            return lst.pop() if lst else ""
        if name == "pop_front":
            lst = self.get_list_arg(args[0], require_name=True)
            return lst.pop(0) if lst else ""
        if name == "clear":
            self.get_list_arg(args[0], require_name=True).clear(); return None
        if name == "erase":
            return self.call("delete", args)
        if name == "insert_at":
            return self.call("insert", args)
        if name == "assign":
            lst = self.get_list_arg(args[0], require_name=True)
            lst[:] = [args[2] for _ in range(max(0, int(args[1])))]
            return None
        if name == "resize":
            lst = self.get_list_arg(args[0], require_name=True)
            n = max(0, int(args[1])); val = args[2] if len(args) >= 3 else 0
            while len(lst) > n: lst.pop()
            while len(lst) < n: lst.append(val)
            return None
        if name == "fill":
            lst = self.get_list_arg(args[0], require_name=True)
            for i in range(len(lst)): lst[i] = args[1]
            return None
        if name == "swap_items":
            lst = self.get_list_arg(args[0], require_name=True)
            i = int(args[1]) - 1; j = int(args[2]) - 1
            lst[i], lst[j] = lst[j], lst[i]
            return None
        if name == "sort":
            self.get_list_arg(args[0], require_name=True).sort(); return None
        if name == "sort_desc":
            self.get_list_arg(args[0], require_name=True).sort(reverse=True); return None
        if name == "reverse":
            self.get_list_arg(args[0], require_name=True).reverse(); return None
        if name in {"lower_bound", "upper_bound", "binary_search"}:
            lst = self.get_list_arg(args[0])
            value = args[1]
            if name == "binary_search":
                return 1 if value in lst else 0
            lo, hi = 0, len(lst)
            upper = name == "upper_bound"
            while lo < hi:
                mid = (lo + hi) // 2
                if (lst[mid] <= value) if upper else (lst[mid] < value): lo = mid + 1
                else: hi = mid
            return lo + 1
        if name in {"lower_bound_to", "upper_bound_to", "binary_search_to"}:
            old = {"lower_bound_to":"lowerBoundTo", "upper_bound_to":"upperBoundTo", "binary_search_to":"binarySearchTo"}[name]
            return self.call(old, args)
        return self._runtime_call_patch17(name, args)

    def _runtime_call_patch17(self, name: str, args: List[Any]) -> Any:
        if name == "rangeLen":
            return max(0, int(float(args[1])) - int(float(args[0])))
        if name == "reverseList":
            self.get_list_arg(args[0], require_name=False).reverse(); return None
        if name in ("sortAsc", "sortDesc"):
            lst = self.get_list_arg(args[0], require_name=False)
            lst.sort(reverse=(name == "sortDesc")); return None
        if name in ("lowerBoundTo", "upperBoundTo"):
            lst = self.get_list_arg(args[0], require_name=False)
            value = args[1]
            lo, hi = 0, len(lst)
            upper = name == "upperBoundTo"
            while lo < hi:
                mid = (lo + hi) // 2
                if (lst[mid] <= value) if upper else (lst[mid] < value):
                    lo = mid + 1
                else:
                    hi = mid
            if len(args) >= 3 and isinstance(args[2], str):
                self.vars[args[2]] = lo + 1
            return lo + 1
        if name == "binarySearchTo":
            lst = self.get_list_arg(args[0], require_name=False)
            ok = 1 if args[1] in lst else 0
            if len(args) >= 3 and isinstance(args[2], str):
                self.vars[args[2]] = ok
            return ok
        return self._runtime_call_patch16(name, args)

    def _runtime_call_patch16(self, name: str, args: List[Any]) -> Any:
        if name in ("fillList", "resizeList"):
            lst = self.get_list_arg(args[0], require_name=True)
            lst.clear()
            n = max(0, int(float(args[1])))
            for _ in range(n):
                lst.append(args[2])
            return None
        if name == "swapItems":
            lst = self.get_list_arg(args[0], require_name=True)
            i = int(float(args[1])) - 1
            j = int(float(args[2])) - 1
            lst[i], lst[j] = lst[j], lst[i]
            return None
        if name == "setItem":
            lst = self.get_list_arg(args[0], require_name=True)
            lst[int(float(args[1])) - 1] = args[2]
            return None
        if name == "deleteLast":
            lst = self.get_list_arg(args[0], require_name=True)
            if lst: lst.pop()
            return None
        if name == "deleteFirst":
            lst = self.get_list_arg(args[0], require_name=True)
            if lst: lst.pop(0)
            return None
        return self._runtime_call_patch15(name, args)

    def _runtime_call_patch15(self, name: str, args: List[Any]) -> Any:
        if name == "num": return float(args[0]) if args else 0
        if name == "text": return "" if not args else str(args[0])
        if name == "bool01": return 1 if (args and self.truthy(args[0])) else 0
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
        return self._runtime_call_patch14(name, args)

    def _runtime_call_patch14(self, name: str, args: List[Any]) -> Any:
        self._runtime_ensure_delta_state()
        if name in ("tick", "frameStart", "updateDelta"):
            return self._runtime_update_delta()
        if name == "resetDelta":
            self._runtime_reset_delta(); return None
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
        return self._runtime_call_patch13(name, args)

    def _runtime_update_delta(self) -> float:
        self._runtime_ensure_delta_state()
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

    def _runtime_ensure_delta_state(self) -> None:
        for name, value in SBG_DELTA_VARS.items():
            self.vars.setdefault(name, value)
        if not hasattr(self, "_sbg_delta_last_monotonic"):
            self._sbg_delta_last_monotonic = time.monotonic()

    def _runtime_reset_delta(self) -> None:
        self._runtime_ensure_delta_state()
        now = time.monotonic()
        self._sbg_delta_last_monotonic = now
        self.vars[SBG_NOW_VAR] = 0
        self.vars[SBG_LAST_VAR] = 0
        self.vars[SBG_RAW_DT_VAR] = 0
        self.vars[SBG_DT_VAR] = 0
        self.vars[SBG_FPS_VAR] = 0
        self.vars[SBG_FRAME_VAR] = 0
        self.vars[SBG_TURBO_VAR] = 1

    def _runtime_call_patch13(self, name: str, args: List[Any]) -> Any:
        st = self._runtime_state()
        if name in ("clearList", "deleteAll"):
            self.get_list_arg(args[0], require_name=True).clear(); return None
        if name in ("showVariable", "hideVariable", "showList", "hideList", "setDragMode"):
            return None
        if name == "stop":
            mode = str(args[0]).lower() if args else "all"
            if "all" in mode: raise StopIteration("stop all")
            return None
        if name == "createCloneOf": return None
        if name in ("penClear", "clearPen", "penEraseAll", "penDown", "penUp", "penStamp", "penSetColor", "penSetSize", "penChangeSize", "penSetParam", "penChangeParam", "penSetHue", "penChangeHue", "penSetSaturation", "penChangeSaturation", "penSetBrightness", "penChangeBrightness", "penSetTransparency", "penChangeTransparency"):
            # Headless native mode cannot draw; keep deterministic no-op semantics so
            # code remains smoke-testable before compiling to Scratch.
            st.setdefault("pen", {})[name] = args
            return None
        if name == "touchingColor": return False
        if name == "colorTouchingColor": return False
        return self._runtime_call_patch12(name, args)

    def _runtime_state(self) -> Dict[str, Any]:
        st = getattr(self, "_sbg_native_sprite_state", None)
        if st is None:
            st = {"x": 0.0, "y": 0.0, "direction": 90.0, "size": 100.0, "visible": True, "costume": 1, "backdrop": 1}
            self._sbg_native_sprite_state = st
        return st

    def _runtime_call_patch12(self, name: str, args: List[Any]) -> Any:
        st = self._runtime_state()
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
        return self._runtime_call_patch9(name, args)

    def _runtime_call_patch9(self, name: str, args: List[Any]) -> Any:
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
        return self._base_call(name, args)

    def _runtime_prepare_patch14(self) -> None:
        self._runtime_prepare_patch13()
        self._runtime_reset_delta()

    def _runtime_prepare_patch13(self) -> None:
        self._runtime_prepare_scratch_console_patch9()
        for name in EMBEDDED_FILE_LIST_NAMES:
            self.lists.setdefault(name, [])

    def _runtime_prepare_scratch_console_patch9(self) -> None:
        old = self.program
        self.program = _flatten_targets_for_runtime(old)
        try:
            self._base_prepare_scratch_console()
        finally:
            self.program = old

    def _runtime_run_action_patch14(self, input_value: str = "") -> Any:
        self._runtime_update_delta()
        return self._runtime_run_action_patch11(input_value)

    def _runtime_run_action_patch11(self, input_value: str = "") -> Any:
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

    def _runtime_run_scratch_once_patch11(self, input_value: str = "") -> None:
        self.prepare_scratch_console()
        if not self.action_events:
            raise RuntimeSBGError("nothing runnable: no Action(Input) body was prepared")
        self.call("log", ["> " + str(input_value)])
        self.run_action(input_value)
        if getattr(self, "last_action_returned", False):
            self.call("log", ["=> " + str(getattr(self, "last_action_return_value", ""))])

    def _runtime_run_scratch_terminal_patch23(self, *, prompt: str = "sbg> ") -> None:
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

    def _eval(self, expr: Any) -> Any:
        return self._runtime_eval_patch21p(expr)

    def _exec_stmt(self, stmt: Any) -> None:
        return self._runtime_exec_stmt_patch22c_flat(stmt)

    def call(self, name: str, args: List[Any]) -> Any:
        return self._runtime_call_patch24(name, args)

    def prepare_scratch_console(self) -> None:
        return self._runtime_prepare_patch14()

    def run_scratch_once(self, input_value: str = "") -> None:
        return self._runtime_run_scratch_once_patch11(input_value)

    def run_scratch_terminal(self, *, prompt: str = "sbg> ") -> None:
        return self._runtime_run_scratch_terminal_patch23(prompt=prompt)

