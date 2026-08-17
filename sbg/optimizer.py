"""StageBG optimization pipeline (the -O1/-O2/-O3 pass driver).

This module sits between the lowered AST (post ``parse_source`` + embedded-file
prep + patch21 struct/vector lowering) and Scratch block emission.  ``-O0``
NEVER enters this module: :func:`compile_project` short-circuits ``level == 0``
straight to ``Compiler(program).compile()``, the exact existing code path.  The
IR therefore only ever has to be *semantics-preserving*, never byte-identical.

Design reference: ``docs/optimizer-design.md`` (sections 3 and 4 in particular).

Conventions
-----------
* Every pass is a pure function ``(IRModule, Facts) -> IRModule`` — except
  :func:`warp_turbo_placement` which returns ``(IRModule, meta_dict)``.
  :func:`run_passes` threads the ``(module, meta)`` pair and accepts both
  shapes.
* Every pass that cannot PROVE its precondition returns its input UNCHANGED.
  A conservative optimizer is the whole safety story here.
* The tree is a set of frozen dataclasses; passes rebuild via
  ``dataclasses.replace`` (which preserves ``src``/``attrs``) or by
  constructing fresh nodes.
* ``._patches`` / ``.runtime`` are NEVER imported at module top level
  (circular-import / heavyweight).  The tiny runtime semantics needed for
  constant folding are reproduced inline below.

``_patches.py`` does ``from .optimizer import *``, so the public API here is
:func:`compile_project`, :func:`compile_project_from_program`,
:func:`run_passes`, :func:`build_ir` and :func:`lower_ir` (plus the 13 pass
functions).
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Dict, Optional, Tuple

from . import globals as _g
from .ir import (
    build_ir,
    lower_ir,
    is_impure_builtin,
    IRAppend,
    IRAssign,
    IRBinary,
    IRBreak,
    IRCall,
    IRChange,
    IRConst,
    IRContinue,
    IRDeleteList,
    IREvent,
    IRExpr,
    IRForever,
    IRGlobal,
    IRIf,
    IRItem,
    IRListLen,
    IRModule,
    IRProc,
    IRRaw,
    IRRepeat,
    IRReturn,
    IRTarget,
    IRUnary,
    IRVar,
    IRWhile,
)

# ---------------------------------------------------------------------------
# Exact runtime semantics (reproduced inline — see _patches.py
# `_sbg_vec_tokens_runtime_patch20` and runtime.py `Runtime.num/truthy/eval`).
# ---------------------------------------------------------------------------


def _num(v: Any) -> float:
    """Reproduce ``Runtime.num``: bools coerce to 1/0, everything else float."""
    if v is True:
        return 1
    if v is False:
        return 0
    return float(v)


def _truthy(v: Any) -> bool:
    return bool(v)


def _fold_binary(op: str, a: Any, b: Any) -> Optional[Any]:
    """Constant-fold one binary op.  Returns ``None`` to mean "don't fold"."""
    try:
        if op == "+":
            if isinstance(a, str) or isinstance(b, str):
                return str(a) + str(b)
            return a + b
        if op == "-":
            return _num(a) - _num(b)
        if op == "*":
            return _num(a) * _num(b)
        if op == "/":
            den = _num(b)
            if den == 0:
                return None
            return _num(a) / den
        if op == "%":
            den = _num(b)
            if den == 0:
                return None
            return _num(a) % den
        if op == "==":
            return a == b
        if op == "!=":
            return a != b
        if op == "<":
            return a < b
        if op == "<=":
            return a <= b
        if op == ">":
            return a > b
        if op == ">=":
            return a >= b
        if op == "&&":
            return _truthy(a) and _truthy(b)
        if op == "||":
            return _truthy(a) or _truthy(b)
    except Exception:
        return None
    return None


def _fold_unary(op: str, v: Any) -> Optional[Any]:
    """Constant-fold one unary op.  Returns ``None`` to mean "don't fold"."""
    try:
        if op == "-":
            return -_num(v)
        if op == "!":
            return not _truthy(v)
    except Exception:
        return None
    return None


def _tokens(value: Any):
    """Reproduce ``_sbg_vec_tokens_runtime_patch20`` exactly."""
    if isinstance(value, list):
        return [str(x) for x in value]
    text = str(value).strip()
    if not text:
        return []
    if "\x1f" in text:
        return [x for x in text.split("\x1f") if x != ""]
    if "," in text:
        return [x.strip() for x in text.split(",") if x.strip()]
    return [x for x in text.split() if x]


def _num_or_text(x: Any) -> Any:
    """Reproduce ``_sbg_num_or_text_patch20``."""
    try:
        f = float(x)
        return int(f) if f.is_integer() else f
    except (ValueError, TypeError):
        return x


def _at0(value: Any, idx: int) -> Any:
    """Reproduce ``_sbg_vec_at0_runtime_patch20`` (idx is 0-based)."""
    vals = _tokens(value)
    if idx < 0 or idx >= len(vals):
        return 0
    return _num_or_text(vals[idx])


def _is_proc_call(v: Any) -> bool:
    return isinstance(v, IRCall) and v.kind == "proc"


def _is_impure_call(v: Any) -> bool:
    return isinstance(v, IRCall) and v.kind != "proc" and is_impure_builtin(v.callee)


def _has_side_effect_value(v: Any) -> bool:
    """True if evaluating ``v`` may mutate an observable scalar.

    Conservative: any user proc call, any impure builtin, or an opaque
    ``IRRaw`` anywhere in the value tree counts.  Recurses through args so a
    proc call nested as an argument (e.g. ``log(getInput())``) is caught, not
    just a top-level proc call.
    """
    if isinstance(v, IRCall):
        if v.kind == "proc" or (v.kind != "proc" and is_impure_builtin(v.callee)):
            return True
        return any(_has_side_effect_value(a) for a in v.args)
    if isinstance(v, IRRaw):
        return True
    if isinstance(v, IRBinary):
        return _has_side_effect_value(v.left) or _has_side_effect_value(v.right)
    if isinstance(v, IRUnary):
        return _has_side_effect_value(v.operand)
    if isinstance(v, IRItem):
        return _has_side_effect_value(v.index)
    return False


def _has_side_effect_stmt(stmts: Tuple) -> bool:
    """True if executing the statement tuple may mutate an observable scalar
    (a side-effecting value anywhere, or an opaque ``IRRaw`` statement)."""
    for s in stmts:
        if isinstance(s, IRRaw):
            return True
        if isinstance(s, IRAssign):
            if _has_side_effect_value(s.value):
                return True
        elif isinstance(s, IRChange):
            if _has_side_effect_value(s.delta):
                return True
        elif isinstance(s, IRIf):
            if _has_side_effect_value(s.cond) or _has_side_effect_stmt(s.then):
                return True
            if s.else_ is not None and _has_side_effect_stmt(s.else_):
                return True
        elif isinstance(s, IRRepeat):
            if _has_side_effect_value(s.count) or _has_side_effect_stmt(s.body):
                return True
        elif isinstance(s, IRForever):
            if _has_side_effect_stmt(s.body):
                return True
        elif isinstance(s, IRWhile):
            if _has_side_effect_value(s.cond) or _has_side_effect_stmt(s.body):
                return True
        elif isinstance(s, IRReturn):
            if s.value is not None and _has_side_effect_value(s.value):
                return True
        elif isinstance(s, IRAppend):
            if _has_side_effect_value(s.value):
                return True
        elif isinstance(s, IRExpr):
            if _has_side_effect_value(s.value):
                return True
    return False


def _assigned_var_names(stmts: Tuple) -> Optional[set]:
    """Names of scalar vars assigned/changed in ``stmts``, recursing through
    nested if/repeat/forever/while bodies.

    Returns ``None`` when an opaque ``IRRaw`` makes the set unknowable (the
    caller then falls back to dropping all constant knowledge).
    """
    names: set = set()
    for s in stmts:
        if isinstance(s, (IRAssign, IRChange)):
            t = s.target
            if isinstance(t, IRVar):
                names.add(t.name)
        elif isinstance(s, IRIf):
            sub = _assigned_var_names(s.then)
            if sub is None:
                return None
            names |= sub
            if s.else_ is not None:
                sub = _assigned_var_names(s.else_)
                if sub is None:
                    return None
                names |= sub
        elif isinstance(s, (IRRepeat, IRWhile, IRForever)):
            sub = _assigned_var_names(s.body)
            if sub is None:
                return None
            names |= sub
        elif isinstance(s, IRRaw):
            return None
    return names


def _pop_env(stmts: Tuple, env: Dict[str, IRConst]) -> None:
    """Drop constant knowledge that may be invalidated by executing ``stmts``.

    Conservative: any side-effecting value in the tuple (proc/impure call or
    opaque ``IRRaw``) could mutate *any* scalar, so the whole env is dropped.
    Otherwise only the vars assigned within the tuple are dropped.
    """
    if _has_side_effect_stmt(stmts):
        env.clear()
        return
    mods = _assigned_var_names(stmts)
    if mods is None:
        env.clear()
    else:
        for n in mods:
            env.pop(n, None)


def _loop_env(body: Tuple, env: Dict[str, IRConst]) -> Dict[str, IRConst]:
    """Env to fold a loop body (and, for a while, its condition) with.

    Loop bodies may run zero or many times and may redefine their assigned
    vars, so the pre-loop constant for any body-assigned var is stale.  If the
    body is opaque (``IRRaw``) or contains a side-effecting value that could
    mutate *any* var, fold from a blank env instead.
    """
    mods = _assigned_var_names(body)
    if mods is None or _has_side_effect_stmt(body):
        return {}
    return {n: c for n, c in env.items() if n not in mods}


# ---------------------------------------------------------------------------
# Facts (global/local symbol tables + purity), computed once by collect-info.
# ---------------------------------------------------------------------------

_IMPURE_FOR_WARP = {"ask", "wait", "broadcastAndWait", "resetTimer", "random"}


@dataclass
class Facts:
    var_names: set
    list_names: set
    referenced: set
    has_raw: bool
    pure_procs: set
    proc_defs: dict


def _walk_value(v: Any, cb) -> None:
    """Depth-first walk over a value tree, calling ``cb`` on every node."""
    cb(v)
    if isinstance(v, IRItem):
        _walk_value(v.index, cb)
    elif isinstance(v, IRBinary):
        _walk_value(v.left, cb)
        _walk_value(v.right, cb)
    elif isinstance(v, IRUnary):
        _walk_value(v.operand, cb)
    elif isinstance(v, IRCall):
        for a in v.args:
            _walk_value(a, cb)


def collect_info(module: IRModule) -> Facts:
    """Walk every target/item recursively and gather conservative facts.

    * ``var_names``: global ``kind=="var"`` names, proc params, ``decl=True``
      assignment targets.
    * ``list_names``: global ``kind=="list"`` names, ``IRDeleteList`` and
      ``IRAppend`` names.
    * ``referenced``: every ``IRVar``/``IRItem``/``IRListLen`` name, every
      assign/change target name, every delete/append name, AND every string
      ``IRConst`` passed as an argument to any ``IRCall`` (this catches
      ``showVariable("x")`` / ``showList("x")`` observable references).
    * ``has_raw``: True if ANY opaque ``IRRaw`` node appears anywhere.
    * ``pure_procs``/``proc_defs``: proc purity computed transitively.
    """
    var_names: set = set()
    list_names: set = set()
    referenced: set = set()
    has_raw = False
    proc_defs: dict = {}

    def note_value(v: Any) -> None:
        nonlocal has_raw
        if isinstance(v, IRRaw):
            has_raw = True
        elif isinstance(v, IRVar):
            referenced.add(v.name)
        elif isinstance(v, IRItem):
            referenced.add(v.name)
        elif isinstance(v, IRListLen):
            referenced.add(v.name)
        elif isinstance(v, IRCall):
            for a in v.args:
                if isinstance(a, IRConst) and isinstance(a.value, str):
                    referenced.add(a.value)

    def note_stmt(s: Any) -> None:
        nonlocal has_raw
        if isinstance(s, IRRaw):
            has_raw = True
        elif isinstance(s, IRAssign):
            tgt = s.target
            if isinstance(tgt, IRVar):
                referenced.add(tgt.name)
                if s.decl:
                    var_names.add(tgt.name)
            elif isinstance(tgt, IRItem):
                referenced.add(tgt.name)
                _walk_value(tgt.index, note_value)
            _walk_value(s.value, note_value)
        elif isinstance(s, IRChange):
            tgt = s.target
            if isinstance(tgt, IRVar):
                referenced.add(tgt.name)
            elif isinstance(tgt, IRItem):
                referenced.add(tgt.name)
                _walk_value(tgt.index, note_value)
            _walk_value(s.delta, note_value)
        elif isinstance(s, IRIf):
            _walk_value(s.cond, note_value)
            for x in s.then:
                note_stmt(x)
            if s.else_:
                for x in s.else_:
                    note_stmt(x)
        elif isinstance(s, IRRepeat):
            _walk_value(s.count, note_value)
            for x in s.body:
                note_stmt(x)
        elif isinstance(s, IRForever):
            for x in s.body:
                note_stmt(x)
        elif isinstance(s, IRWhile):
            _walk_value(s.cond, note_value)
            for x in s.body:
                note_stmt(x)
        elif isinstance(s, IRReturn):
            if s.value is not None:
                _walk_value(s.value, note_value)
        elif isinstance(s, IRDeleteList):
            list_names.add(s.name)
            referenced.add(s.name)
        elif isinstance(s, IRAppend):
            list_names.add(s.name)
            referenced.add(s.name)
            _walk_value(s.value, note_value)
        elif isinstance(s, IRExpr):
            _walk_value(s.value, note_value)
        # IRBreak / IRContinue carry no values.

    for target in module.targets:
        for item in target.items:
            if isinstance(item, IRRaw):
                has_raw = True
            elif isinstance(item, IRGlobal):
                if item.kind == "var":
                    var_names.add(item.name)
                    if item.init is not None:
                        _walk_value(item.init, note_value)
                else:  # "list"
                    list_names.add(item.name)
                    for x in item.items:
                        _walk_value(x, note_value)
            elif isinstance(item, IRProc):
                proc_defs[item.name] = item
                for p in item.params:
                    var_names.add(p)
                for s in item.body:
                    note_stmt(s)
            elif isinstance(item, IREvent):
                for s in item.body:
                    note_stmt(s)

    pure_procs = _compute_pure_procs(proc_defs)
    return Facts(var_names, list_names, referenced, has_raw, pure_procs, proc_defs)


def _reaches(calls: Dict[str, set], start: str, target: str) -> bool:
    """True if ``start`` can reach ``target`` by a call path of length >= 1."""
    seen = set()
    stack = list(calls.get(start, ()))
    while stack:
        n = stack.pop()
        if n == target:
            return True
        if n in seen:
            continue
        seen.add(n)
        stack.extend(calls.get(n, ()))
    return False


def _compute_pure_procs(proc_defs: dict) -> set:
    """Compute transitive purity over the proc call graph.

    A proc is impure if its body contains an ``IRCall`` with a builtin/helper
    callee in ``_IMPURE_FOR_WARP``, or if it (transitively) calls an impure
    proc, or if it calls a proc that is not defined (conservative), or if it is
    part of a call cycle (self/cycles default impure).
    """
    direct = set()
    unknown = set()
    calls: Dict[str, set] = {name: set() for name in proc_defs}

    def scan_value(v: Any, owner: str) -> None:
        if isinstance(v, IRCall):
            if v.kind == "proc":
                calls[owner].add(v.callee)
                if v.callee not in proc_defs:
                    unknown.add(owner)
            elif v.kind in ("builtin", "helper") and v.callee in _IMPURE_FOR_WARP:
                direct.add(owner)

    def scan_stmt(s: Any, owner: str) -> None:
        if isinstance(s, IRRaw):
            return
        if isinstance(s, IRAssign):
            _walk_value(s.value, lambda v: scan_value(v, owner))
            if isinstance(s.target, IRItem):
                _walk_value(s.target.index, lambda v: scan_value(v, owner))
        elif isinstance(s, IRChange):
            _walk_value(s.delta, lambda v: scan_value(v, owner))
            if isinstance(s.target, IRItem):
                _walk_value(s.target.index, lambda v: scan_value(v, owner))
        elif isinstance(s, IRIf):
            _walk_value(s.cond, lambda v: scan_value(v, owner))
            for x in s.then:
                scan_stmt(x, owner)
            if s.else_:
                for x in s.else_:
                    scan_stmt(x, owner)
        elif isinstance(s, IRRepeat):
            _walk_value(s.count, lambda v: scan_value(v, owner))
            for x in s.body:
                scan_stmt(x, owner)
        elif isinstance(s, IRForever):
            for x in s.body:
                scan_stmt(x, owner)
        elif isinstance(s, IRWhile):
            _walk_value(s.cond, lambda v: scan_value(v, owner))
            for x in s.body:
                scan_stmt(x, owner)
        elif isinstance(s, IRReturn):
            if s.value is not None:
                _walk_value(s.value, lambda v: scan_value(v, owner))
        elif isinstance(s, IRAppend):
            _walk_value(s.value, lambda v: scan_value(v, owner))
        elif isinstance(s, IRExpr):
            _walk_value(s.value, lambda v: scan_value(v, owner))
        # IRDeleteList / IRBreak / IRContinue carry no calls.

    for name, proc in proc_defs.items():
        for s in proc.body:
            scan_stmt(s, name)

    impure = set(direct) | set(unknown)
    changed = True
    while changed:
        changed = False
        for name, cs in calls.items():
            if name in impure:
                continue
            if any(c in impure for c in cs):
                impure.add(name)
                changed = True

    # Self/cycles default impure (conservative).
    for name in proc_defs:
        if name not in impure and _reaches(calls, name, name):
            impure.add(name)

    return set(proc_defs) - impure


# ---------------------------------------------------------------------------
# Generic tree-rewrite machinery (shared by the "map a function over every
# value" passes).  The tree is frozen, so rewrites are done by constructing new
# nodes; identity (`is`) checks let us short-circuit when nothing changed.
# ---------------------------------------------------------------------------


def _map_value(v: Any, fn) -> Any:
    """Bottom-up map ``fn`` over a value tree (children first, then the node)."""
    if isinstance(v, IRConst) or isinstance(v, IRVar) or isinstance(v, IRListLen) or isinstance(v, IRRaw):
        return fn(v)
    if isinstance(v, IRItem):
        idx = _map_value(v.index, fn)
        nv = v if idx is v.index else IRItem(v.name, idx, v.src)
        return fn(nv)
    if isinstance(v, IRBinary):
        left = _map_value(v.left, fn)
        right = _map_value(v.right, fn)
        nv = v if (left is v.left and right is v.right) else IRBinary(v.op, left, right, v.src)
        return fn(nv)
    if isinstance(v, IRUnary):
        operand = _map_value(v.operand, fn)
        nv = v if operand is v.operand else IRUnary(v.op, operand, v.src)
        return fn(nv)
    if isinstance(v, IRCall):
        args = tuple(_map_value(a, fn) for a in v.args)
        nv = v if all(a is b for a, b in zip(args, v.args)) else IRCall(v.callee, args, v.kind, v.src)
        return fn(nv)
    return fn(v)


def _map_stmt(s: Any, fn) -> Any:
    if isinstance(s, IRAssign):
        tgt = s.target
        if isinstance(tgt, IRItem):
            tgt = _map_value(tgt, fn)
        val = _map_value(s.value, fn)
        if tgt is s.target and val is s.value:
            return s
        return replace(s, target=tgt, value=val)
    if isinstance(s, IRChange):
        tgt = s.target
        if isinstance(tgt, IRItem):
            tgt = _map_value(tgt, fn)
        delta = _map_value(s.delta, fn)
        if tgt is s.target and delta is s.delta:
            return s
        return replace(s, target=tgt, delta=delta)
    if isinstance(s, IRIf):
        cond = _map_value(s.cond, fn)
        then = _map_body(s.then, fn)
        else_ = None if s.else_ is None else _map_body(s.else_, fn)
        if cond is s.cond and then is s.then and else_ is s.else_:
            return s
        return replace(s, cond=cond, then=then, else_=else_)
    if isinstance(s, IRRepeat):
        count = _map_value(s.count, fn)
        body = _map_body(s.body, fn)
        if count is s.count and body is s.body:
            return s
        return replace(s, count=count, body=body)
    if isinstance(s, IRForever):
        body = _map_body(s.body, fn)
        if body is s.body:
            return s
        return replace(s, body=body)
    if isinstance(s, IRWhile):
        cond = _map_value(s.cond, fn)
        body = _map_body(s.body, fn)
        if cond is s.cond and body is s.body:
            return s
        return replace(s, cond=cond, body=body)
    if isinstance(s, IRReturn):
        val = None if s.value is None else _map_value(s.value, fn)
        if val is s.value:
            return s
        return replace(s, value=val)
    if isinstance(s, IRAppend):
        val = _map_value(s.value, fn)
        if val is s.value:
            return s
        return replace(s, value=val)
    if isinstance(s, IRExpr):
        val = _map_value(s.value, fn)
        if val is s.value:
            return s
        return replace(s, value=val)
    return s  # IRDeleteList / IRBreak / IRContinue / IRRaw: no values


def _map_body(stmts: Tuple, fn) -> Tuple:
    out = []
    changed = False
    for s in stmts:
        ns = _map_stmt(s, fn)
        out.append(ns)
        if ns is not s:
            changed = True
    return tuple(out) if changed else stmts


def _map_item(item: Any, fn) -> Any:
    if isinstance(item, IRGlobal):
        if item.kind == "var":
            init = None if item.init is None else _map_value(item.init, fn)
            return item if init is item.init else replace(item, init=init)
        its = tuple(_map_value(x, fn) for x in item.items)
        return item if all(a is b for a, b in zip(its, item.items)) else replace(item, items=its)
    if isinstance(item, IRProc):
        body = _map_body(item.body, fn)
        return item if body is item.body else replace(item, body=body)
    if isinstance(item, IREvent):
        body = _map_body(item.body, fn)
        return item if body is item.body else replace(item, body=body)
    return item


def _rebuild_module(module: IRModule, item_fn) -> IRModule:
    """Rebuild a module by mapping each top-level item; ``None`` drops an item.

    ``module.attrs`` and the target order are preserved; an unchanged module is
    returned as the identical object.
    """
    targets = []
    changed = False
    for t in module.targets:
        items = []
        tchanged = False
        for item in t.items:
            ni = item_fn(item)
            if ni is None:
                tchanged = True
                continue
            items.append(ni)
            if ni is not item:
                tchanged = True
        nt = t if not tchanged else replace(t, items=tuple(items))
        targets.append(nt)
        if nt is not t:
            changed = True
    return module if not changed else IRModule(tuple(targets), module.attrs)


def _map_module(module: IRModule, fn) -> IRModule:
    return _rebuild_module(module, lambda it: _map_item(it, fn))


# ---------------------------------------------------------------------------
# The 13 passes.
# ---------------------------------------------------------------------------


def constant_fold(module: IRModule, facts: Facts) -> IRModule:
    """Constant folding + straight-line forward propagation (-O1).

    * Folds ``IRBinary``/``IRUnary`` over ``IRConst`` operands using the exact
      runtime semantics (see :func:`_fold_binary` / :func:`_fold_unary`).
    * Propagates scalar constants forward inside each linear statement body via
      an env ``{var_name: IRConst}``.  Loop/if arm bodies get a copy and never
      merge back.  Impure builtins and proc calls are env barriers.
    * Never folds anything whose callee is impure.
    """
    def fold_expr(v: Any, env: Dict[str, IRConst]) -> Any:
        if isinstance(v, IRVar):
            c = env.get(v.name)
            return c if c is not None else v
        if isinstance(v, IRBinary):
            left = fold_expr(v.left, env)
            right = fold_expr(v.right, env)
            nv = v
            if left is not v.left or right is not v.right:
                nv = IRBinary(v.op, left, right, v.src)
            if isinstance(left, IRConst) and isinstance(right, IRConst):
                r = _fold_binary(v.op, left.value, right.value)
                if r is not None:
                    return IRConst(r, v.src)
            return nv
        if isinstance(v, IRUnary):
            operand = fold_expr(v.operand, env)
            nv = v if operand is v.operand else IRUnary(v.op, operand, v.src)
            if isinstance(operand, IRConst):
                r = _fold_unary(v.op, operand.value)
                if r is not None:
                    return IRConst(r, v.src)
            return nv
        if isinstance(v, IRItem):
            idx = fold_expr(v.index, env)
            return v if idx is v.index else IRItem(v.name, idx, v.src)
        if isinstance(v, IRCall):
            # Never fold anything whose callee is impure.
            if v.kind != "proc" and is_impure_builtin(v.callee):
                return v
            args = tuple(fold_expr(a, env) for a in v.args)
            return v if all(a is b for a, b in zip(args, v.args)) else IRCall(v.callee, args, v.kind, v.src)
        return v  # IRConst / IRListLen / IRRaw

    def fold_body(stmts: Tuple, env: Dict[str, IRConst]) -> Tuple:
        out = []
        for s in stmts:
            if isinstance(s, IRAssign):
                val = fold_expr(s.value, env)
                nv = s if val is s.value else replace(s, value=val)
                if isinstance(s.target, IRVar):
                    n = s.target.name
                    if isinstance(val, IRConst):
                        env[n] = val
                    else:
                        env.pop(n, None)
                if _has_side_effect_value(val):
                    env.clear()
                out.append(nv)
            elif isinstance(s, IRChange):
                delta = fold_expr(s.delta, env)
                nv = s if delta is s.delta else replace(s, delta=delta)
                if isinstance(s.target, IRVar):
                    env.pop(s.target.name, None)
                if _has_side_effect_value(delta):
                    env.clear()
                out.append(nv)
            elif isinstance(s, IRIf):
                # The condition is evaluated exactly once; fold it with the
                # current env unless it has side effects (which could mutate
                # any var, so fold the arms from a blank env too and drop all
                # knowledge afterwards).
                cond_se = _has_side_effect_value(s.cond)
                cond = fold_expr(s.cond, {} if cond_se else env)
                arm_env = {} if cond_se else env.copy()
                then = fold_body(s.then, arm_env)
                else_ = None if s.else_ is None else fold_body(s.else_, arm_env)
                if cond_se:
                    env.clear()
                else:
                    # Vars assigned in either arm may differ from the pre-if
                    # env; drop them (or everything if an arm was opaque).
                    _pop_env(s.then, env)
                    if s.else_ is not None:
                        _pop_env(s.else_, env)
                if cond is s.cond and then is s.then and else_ is s.else_:
                    out.append(s)
                else:
                    out.append(replace(s, cond=cond, then=then, else_=else_))
            elif isinstance(s, IRRepeat):
                # `count` is evaluated exactly once, before the loop.  A
                # side-effecting count could mutate any var — fold blank and
                # drop all knowledge afterwards.
                count_se = _has_side_effect_value(s.count)
                count = fold_expr(s.count, {} if count_se else env)
                body = fold_body(s.body, _loop_env(s.body, env))
                if count_se:
                    env.clear()
                else:
                    # Loop body may run zero times, so after it only the
                    # *pre-loop* env minus body-assigned vars is guaranteed.
                    _pop_env(s.body, env)
                out.append(replace(s, count=count, body=body) if (count is not s.count or body is not s.body) else s)
            elif isinstance(s, IRForever):
                body = fold_body(s.body, _loop_env(s.body, env))
                # Unreachable afterwards (infinite loop); drop all knowledge.
                env.clear()
                out.append(replace(s, body=body) if body is not s.body else s)
            elif isinstance(s, IRWhile):
                # The condition is evaluated each iteration.  Vars assigned in
                # the body are unknown at entry and may take any value, so the
                # cond is only folded for vars NOT touched by the body — and
                # only when the body is side-effect free (else blank env).
                loop_env = _loop_env(s.body, env)
                cond_se = _has_side_effect_value(s.cond)
                cond = fold_expr(s.cond, {} if cond_se else loop_env)
                body = fold_body(s.body, loop_env)
                if cond_se:
                    env.clear()
                else:
                    _pop_env(s.body, env)
                out.append(replace(s, cond=cond, body=body) if (cond is not s.cond or body is not s.body) else s)
            elif isinstance(s, IRReturn):
                if s.value is None:
                    out.append(s)
                else:
                    val = fold_expr(s.value, env)
                    out.append(replace(s, value=val) if val is not s.value else s)
            elif isinstance(s, IRAppend):
                val = fold_expr(s.value, env)
                out.append(replace(s, value=val) if val is not s.value else s)
                # List appends do not touch scalars, but a side-effecting value
                # (proc call / impure builtin / IRRaw) may mutate globals.
                if _has_side_effect_value(val):
                    env.clear()
            elif isinstance(s, IRDeleteList):
                out.append(s)
            elif isinstance(s, IRExpr):
                val = fold_expr(s.value, env)
                out.append(replace(s, value=val) if val is not s.value else s)
                if _has_side_effect_value(val):
                    env.clear()
            elif isinstance(s, IRRaw):
                # Opaque statement: may mutate scalars.  Conservative barrier.
                env.clear()
                out.append(s)
            else:  # IRBreak / IRContinue
                out.append(s)
        return tuple(out)

    def item_fn(item: Any) -> Any:
        if isinstance(item, IRGlobal):
            if item.kind == "var":
                if item.init is None:
                    return item
                init = fold_expr(item.init, {})
                return item if init is item.init else replace(item, init=init)
            its = tuple(fold_expr(x, {}) for x in item.items)
            return item if all(a is b for a, b in zip(its, item.items)) else replace(item, items=its)
        if isinstance(item, IRProc):
            body = fold_body(item.body, {})
            return item if body is item.body else replace(item, body=body)
        if isinstance(item, IREvent):
            body = fold_body(item.body, {})
            return item if body is item.body else replace(item, body=body)
        return item

    return _rebuild_module(module, item_fn)


def specialize_vec_helpers(module: IRModule, facts: Facts) -> IRModule:
    """Strength-reduce the O(tokens) ``vec_size``/``at0`` string-scanners (-O1).

    * ``vec_size(IRConst(row))`` -> ``IRConst(len(_tokens(row)))`` when ``row``
      is a str with no ``;``/``\x1f`` separators.
    * ``at0(IRConst(row), IRConst(idx))`` -> ``IRConst(_at0(row, idx))`` when
      ``row`` is a str with no ``;``/``\x1f`` separators and ``idx`` is a real
      (non-bool) int.

    No other helper / ``__flat_struct_*`` call is touched.
    """
    def fn(v: Any) -> Any:
        if isinstance(v, IRCall):
            if v.callee == "vec_size" and len(v.args) == 1:
                a = v.args[0]
                if (
                    isinstance(a, IRConst)
                    and isinstance(a.value, str)
                    and ";" not in a.value
                    and "\x1f" not in a.value
                ):
                    return IRConst(len(_tokens(a.value)), v.src)
            elif v.callee == "at0" and len(v.args) == 2:
                row, idx = v.args
                if (
                    isinstance(row, IRConst)
                    and isinstance(row.value, str)
                    and ";" not in row.value
                    and "\x1f" not in row.value
                    and isinstance(idx, IRConst)
                    and isinstance(idx.value, int)
                    and not isinstance(idx.value, bool)
                ):
                    return IRConst(_at0(row.value, idx.value), v.src)
        return v

    return _map_module(module, fn)


def merge_join_literals(module: IRModule, facts: Facts) -> IRModule:
    """Fuse all-constant ``join`` trees into a single concatenated constant (-O1).

    Applies to any ``IRCall`` with ``callee == "join"`` (any kind).  Recursively
    folds each arg first; if after folding every arg is an ``IRConst`` the whole
    call becomes ``IRConst("".join(str(a.value) ...))``.  Only the all-constants
    case is required; mixed trees are skipped.
    """
    def fn(v: Any) -> Any:
        if isinstance(v, IRCall) and v.callee == "join":
            if all(isinstance(a, IRConst) for a in v.args):
                return IRConst("".join(str(a.value) for a in v.args), v.src)
        return v

    return _map_module(module, fn)


def dce(module: IRModule, facts: Facts) -> IRModule:
    """Minimal, conservative dead-code elimination (-O1, rerun at O2/O3).

    Only three transformations, applied recursively over statement bodies:
    * drop every statement AFTER an ``IRReturn``/``IRBreak``/``IRContinue`` in
      the same linear body (unreachable);
    * ``IRIf`` with an ``IRConst`` condition: inline the taken arm (or nothing);
    * ``IRWhile`` with an ``IRConst`` falsy cond, and ``IRRepeat`` with an
      ``IRConst`` count <= 0, are dropped entirely.

    No liveness-based assignment removal; ``IRAppend``/``IRDeleteList``/``IRExpr``
    are never removed.
    """
    def simplify_body(stmts: Tuple) -> Tuple:
        out = []
        for s in stmts:
            if isinstance(s, IRIf):
                then = simplify_body(s.then)
                else_ = None if s.else_ is None else simplify_body(s.else_)
                ns = s if (then is s.then and else_ is s.else_) else replace(s, then=then, else_=else_)
                if isinstance(ns.cond, IRConst):
                    if _truthy(ns.cond.value):
                        out.extend(then)
                    elif else_:
                        out.extend(else_)
                    continue
                out.append(ns)
            elif isinstance(s, IRWhile):
                body = simplify_body(s.body)
                ns = s if body is s.body else replace(s, body=body)
                if isinstance(ns.cond, IRConst) and not _truthy(ns.cond.value):
                    continue  # while(false) never runs
                out.append(ns)
            elif isinstance(s, IRRepeat):
                body = simplify_body(s.body)
                ns = s if body is s.body else replace(s, body=body)
                if isinstance(ns.count, IRConst):
                    try:
                        if _num(ns.count.value) <= 0:
                            continue  # repeat(<=0) never runs
                    except Exception:
                        pass
                out.append(ns)
            elif isinstance(s, IRForever):
                body = simplify_body(s.body)
                out.append(replace(s, body=body) if body is not s.body else s)
            else:
                out.append(s)
                if isinstance(s, IRReturn) or isinstance(s, IRBreak) or isinstance(s, IRContinue):
                    break  # everything after is unreachable
        return tuple(out)

    def item_fn(item: Any) -> Any:
        if isinstance(item, IRProc):
            body = simplify_body(item.body)
            return item if body is item.body else replace(item, body=body)
        if isinstance(item, IREvent):
            body = simplify_body(item.body)
            return item if body is item.body else replace(item, body=body)
        return item

    return _rebuild_module(module, item_fn)


def sroa_struct_scalars(module: IRModule, facts: Facts) -> IRModule:
    # NO-OP (deliberate).  patch21 already flattens struct fields into
    # `name.field` scalars, so the classic SROA "split a struct into scalars"
    # step is pre-done.  The remaining "drop dead fields" work needs struct
    # copy-construction liveness (`y = x` reads every field of `x` even without
    # an explicit IRVar("x.f") read), which collect_info does not yet model —
    # so this pass is disabled rather than risk deleting live fields.
    return module


def dead_list_variable_elim(module: IRModule, facts: Facts) -> IRModule:
    """Remove never-referenced top-level globals (-O2, conservative).

    Removes only ``IRGlobal`` items (from each target) whose ``name`` is NOT in
    ``facts.referenced``, NOT in the keep-set (``Terminal`` and any ``__``-
    prefixed name, e.g. the ``__sbg_file_*`` tables), and only when the whole
    program contains no opaque ``IRRaw`` node (``facts.has_raw``).  Nothing else
    is touched — no procs/events/statements are removed.
    """
    if facts.has_raw:
        return module
    keep = {_g.TERMINAL_LIST_NAME}

    def is_keep(name: str) -> bool:
        return name in keep or name.startswith("__")

    def item_fn(item: Any) -> Any:
        if isinstance(item, IRGlobal) and item.name not in facts.referenced and not is_keep(item.name):
            return None  # dropped by _rebuild_module
        return item

    return _rebuild_module(module, item_fn)


def inline_small_procs(module: IRModule, facts: Facts) -> IRModule:
    # NO-OP (deliberate).  Inlining requires a block-count budget, formal/arg
    # substitution, return-temp rebinding for returning procs, a "never increase
    # blocks" net-reduction check, and must skip `__sbg_*`/`__flat_struct_*`
    # procs (design §4.7).  None of that is implemented yet; leaving calls in
    # place is always correct.
    return module


def loop_invariant_hoist(module: IRModule, facts: Facts) -> IRModule:
    # NO-OP (deliberate).  Hoisting requires per-loop liveness/invariance
    # analysis and must never move anything derived from `ask`/`answer`/
    # `timer`/`random`/`dt`/`fps`/`frame`/`timeSeconds`, nor anything that
    # mutates a list/variable used inside the loop (design §4.8).  Not
    # implemented; returning unchanged is correct.
    return module


def flat_list_fusion(module: IRModule, facts: Facts) -> IRModule:
    # NO-OP (deliberate).  patch21d already compiles `__flat_struct_push` to
    # direct field appends, and row-size elision needs a whole-program "list is
    # never resized" proof (design §4.9).  Not implemented; returning unchanged
    # is correct.
    return module


def vector_scalar_promotion(module: IRModule, facts: Facts) -> IRModule:
    # NO-OP (deliberate).  Promoting a list to scalars needs a provable
    # fixed-length bound and all-reads-in-bounds analysis (design §4.10), and an
    # out-of-bounds `push_back` would be a runtime semantic change.  Not
    # implemented; returning unchanged is correct.
    return module


def warp_turbo_placement(module: IRModule, facts: Facts) -> Tuple[IRModule, Dict[str, Any]]:
    """Metadata-only pass (-O3).  Returns ``(module, meta)``.

    Warp is already default-on for every custom block (patch12/14); this pass
    only *reports* which procs are pure vs interactive.  It never changes any
    ``IRProc.warp`` flag.
    """
    proc_names = set(facts.proc_defs.keys())
    meta = {
        "stagebgWarpAnalysis": {
            "pureProcs": sorted(facts.pure_procs & proc_names),
            "impureProcs": sorted(proc_names - facts.pure_procs),
        }
    }
    return module, meta


def terminal_output_minimize(module: IRModule, facts: Facts) -> IRModule:
    # Gated NO-OP (deliberate).  Merging consecutive `IRAppend("Terminal", ...)`
    # into a single batched join changes Terminal list item granularity — an
    # observable output shape — so it is intentionally disabled even at -O3.
    # The `--opt-terminal-batch` flag is accepted by run_passes but this pass is
    # not implemented, so Terminal appends are never merged.
    return module


# ---------------------------------------------------------------------------
# Pass driver
# ---------------------------------------------------------------------------


def run_passes(ir: IRModule, level: int, terminal_batch: bool = False) -> Tuple[IRModule, Dict[str, Any]]:
    """Apply the pass list for ``level`` and return ``(module, meta)``.

    Pass lists (design §3):
        O1: constant-fold → specialize-vec-helpers → merge-join-literals → dce
        O2: O1 → sroa-struct-scalars → dead-list-variable-elim →
            inline-small-procs → loop-invariant-hoist → dce (rerun)
        O3: O2 → flat-list-fusion → vector-scalar-promotion →
            warp-turbo-placement → terminal-output-minimize → dce (rerun)

    Every pass has signature ``(module, facts) -> module`` except
    :func:`warp_turbo_placement`, which returns ``(module, meta)``; both shapes
    are threaded here.  ``terminal_batch`` is accepted for CLI compatibility but
    the terminal-output-minimize pass is not implemented.
    """
    if level <= 0:
        return ir, {}
    facts = collect_info(ir)
    module = ir
    meta: Dict[str, Any] = {}

    passes = []
    if level >= 1:
        passes.extend([constant_fold, specialize_vec_helpers, merge_join_literals, dce])
    if level >= 2:
        passes.extend([sroa_struct_scalars, dead_list_variable_elim,
                       inline_small_procs, loop_invariant_hoist, dce])
    if level >= 3:
        passes.extend([flat_list_fusion, vector_scalar_promotion,
                       warp_turbo_placement, terminal_output_minimize, dce])

    for p in passes:
        result = p(module, facts)
        if isinstance(result, tuple):
            module, extra = result
            if extra:
                meta.update(extra)
        else:
            module = result

    return module, meta


# ---------------------------------------------------------------------------
# Public compile entry points.
#
# `_patches` is imported lazily INSIDE these functions to avoid a circular
# import: sbg/_patches.py does `from .optimizer import *` at module top level,
# so optimizer.py must never import _patches at module top level.
# ---------------------------------------------------------------------------


def compile_project(
    source: str,
    level: int = 0,
    *,
    filename: str = "<source>",
    embeds=None,
    embed_dirs=None,
    allow_library: bool = False,
    no_turbo: bool = False,
    terminal_batch: bool = False,
    verify: bool = True,
) -> dict:
    """Compile ``source`` text into a project dict (pre-``write_sb3_project``).

    ``-O0`` short-circuits to the exact existing ``Compiler(...).compile()``
    path (byte-identical to today).  ``-O1+`` routes through build_ir → passes
    → lower_ir and merges pass metadata into ``project["meta"]``.  ``verify`` is
    accepted for API compatibility but unused here (the caller passes it to
    ``write_sb3_project``).
    """
    from . import _patches as P

    program = P.parse_source(source, filename)
    program = P._program_with_embedded_files(program, filename, embeds=embeds, embed_dirs=embed_dirs)
    project = _compile_program(program, level, P, allow_library=allow_library, terminal_batch=terminal_batch)
    if no_turbo:
        P._sbg_project_set_warp(project, False)
    return project


def compile_project_from_program(
    program,
    level: int = 0,
    *,
    allow_library: bool = False,
    no_turbo: bool = False,
    terminal_batch: bool = False,
) -> dict:
    """Like :func:`compile_project` but takes an already-parsed + embedded-files
    ``program``.  This is what the CLI ``compile``/``run`` paths call."""
    from . import _patches as P

    project = _compile_program(program, level, P, allow_library=allow_library, terminal_batch=terminal_batch)
    if no_turbo:
        P._sbg_project_set_warp(project, False)
    return project


def _compile_program(program, level: int, P, *, allow_library: bool, terminal_batch: bool) -> dict:
    """Shared steps 4-6 of the two public entry points."""
    if level == 0:
        return P.Compiler(program, allow_library=allow_library).compile()
    ir = build_ir(program)
    ir, meta = run_passes(ir, level, terminal_batch=terminal_batch)
    lowered = lower_ir(ir)
    project = P.Compiler(lowered, allow_library=allow_library).compile()
    if meta:
        project.setdefault("meta", {}).update(meta)
    return project
