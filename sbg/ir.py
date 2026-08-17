"""StageBG intermediate representation (IR).

The IR sits between the lowered AST (post ``parse_source`` + embedded-file prep +
patch21 struct/vector lowering) and Scratch block emission.  It is a *structured,
statement-linearized* form (no SSA, no CFG) as described in
``docs/optimizer-design.md`` section 2.

The contract that matters most is the ``-O0`` invariant: ``-O0`` never enters this
module.  ``build_ir`` / ``lower_ir`` therefore only have to be *semantics
preserving* — a conservative optimizer is the whole safety story, because any
node a pass cannot prove safe is carried through unchanged via :class:`IRRaw`.

Two extra statement nodes beyond the design doc's table exist for completeness:
:class:`IRBreak` / :class:`IRContinue` (the base Scratch builder cannot represent
``break``/``continue``, so these are carried opaquely by default at ``-O1``).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union

from . import globals as _g


# ---------------------------------------------------------------------------
# IR node definitions (frozen dataclass tree; every node carries `src`)
# ---------------------------------------------------------------------------

Src = Optional[Tuple[Any, Any, Any]]  # (filename, line, col)


def _src(node: Any) -> Src:
    """Extract the (filename, line, col) source location from an AST node."""
    if node is None or not hasattr(node, "__dict__"):
        return None
    d = node.__dict__
    return (
        d.get("filename", "<source>"),
        d.get("line", 1),
        d.get("col", 1),
    )


@dataclass(frozen=True)
class IRConst:
    value: Any
    src: Src = None


@dataclass(frozen=True)
class IRVar:
    name: str
    src: Src = None


@dataclass(frozen=True)
class IRItem:
    name: str
    index: "IRValue"
    src: Src = None


@dataclass(frozen=True)
class IRListLen:
    name: str
    src: Src = None


@dataclass(frozen=True)
class IRBinary:
    op: str
    left: "IRValue"
    right: "IRValue"
    src: Src = None


@dataclass(frozen=True)
class IRUnary:
    op: str
    operand: "IRValue"
    src: Src = None


@dataclass(frozen=True)
class IRCall:
    callee: str
    args: Tuple["IRValue", ...]
    kind: str  # "builtin" | "proc" | "helper"
    src: Src = None


# Value (reporter) union.
IRValue = Union[IRConst, IRVar, IRItem, IRListLen, IRBinary, IRUnary, IRCall]


@dataclass(frozen=True)
class IRAssign:
    target: IRValue
    value: IRValue
    decl: bool = False          # True => body-level `VarDecl` (lower back to VarDecl)
    attrs: Dict[str, Any] = field(default_factory=dict)
    src: Src = None


@dataclass(frozen=True)
class IRChange:
    target: IRValue
    delta: IRValue
    src: Src = None


@dataclass(frozen=True)
class IRIf:
    cond: IRValue
    then: Tuple["IRStmt", ...]
    else_: Optional[Tuple["IRStmt", ...]]  # None => no else; () => empty else
    src: Src = None


@dataclass(frozen=True)
class IRRepeat:
    count: IRValue
    body: Tuple["IRStmt", ...]
    src: Src = None


@dataclass(frozen=True)
class IRForever:
    body: Tuple["IRStmt", ...]
    src: Src = None


@dataclass(frozen=True)
class IRWhile:
    cond: IRValue
    body: Tuple["IRStmt", ...]
    src: Src = None


@dataclass(frozen=True)
class IRReturn:
    value: Optional[IRValue]
    src: Src = None


@dataclass(frozen=True)
class IRDeleteList:
    name: str
    src: Src = None


@dataclass(frozen=True)
class IRAppend:
    name: str
    value: IRValue
    src: Src = None


@dataclass(frozen=True)
class IRExpr:
    value: IRValue
    src: Src = None


@dataclass(frozen=True)
class IRBreak:
    src: Src = None


@dataclass(frozen=True)
class IRContinue:
    src: Src = None


@dataclass(frozen=True)
class IRRaw:
    """Opaque escape hatch: carries an AST node through unchanged.

    Used for every node the IR does not fully model (TargetDecl, ImportDecl,
    LValueAssignStmt, ArrayExpr, StructDecl/StructVarDecl/NestedVectorDecl
    remnants, and anything the base builder treats specially).
    """
    node: Any
    src: Src = None


# Statement (command) union.
IRStmt = Union[
    IRAssign, IRChange, IRIf, IRRepeat, IRForever, IRWhile,
    IRReturn, IRDeleteList, IRAppend, IRExpr, IRBreak, IRContinue, IRRaw,
]


@dataclass(frozen=True)
class IRGlobal:
    kind: str  # "var" | "list"
    name: str
    init: Optional[IRValue] = None           # var
    items: Tuple[IRValue, ...] = ()          # list
    attrs: Dict[str, Any] = field(default_factory=dict)
    src: Src = None


@dataclass(frozen=True)
class IRProc:
    name: str
    params: Tuple[str, ...]
    body: Tuple[IRStmt, ...]
    warp: bool = False
    returns: bool = False
    is_action: bool = False
    attrs: Dict[str, Any] = field(default_factory=dict)
    src: Src = None


@dataclass(frozen=True)
class IREvent:
    kind: str  # "flag" | "message" | "action"
    value: Optional[str]
    body: Tuple[IRStmt, ...]
    attrs: Dict[str, Any] = field(default_factory=dict)
    src: Src = None


# Top-level item union: the ordered contents of a target.
IRTopItem = Union[IRGlobal, IRProc, IREvent, IRRaw]


@dataclass(frozen=True)
class IRTarget:
    name: str
    is_stage: bool
    items: Tuple[IRTopItem, ...] = ()
    src: Src = None

    @property
    def globals(self) -> List[IRGlobal]:
        return [x for x in self.items if isinstance(x, IRGlobal)]

    @property
    def procs(self) -> List[IRProc]:
        return [x for x in self.items if isinstance(x, IRProc)]

    @property
    def events(self) -> List[IREvent]:
        return [x for x in self.items if isinstance(x, IREvent)]


@dataclass(frozen=True)
class IRModule:
    targets: Tuple[IRTarget, ...] = ()
    attrs: Dict[str, Any] = field(default_factory=dict)

    @property
    def stage(self) -> IRTarget:
        return self.targets[0] if self.targets else IRTarget("Stage", True)


# ---------------------------------------------------------------------------
# Helper classification
# ---------------------------------------------------------------------------

# Helpers are the mangle/ABI calls the passes may specialize.  ``__``-prefixed
# names are compiler-generated (``__field_ref``, ``__index0_ref``,
# ``__flat_struct_*``, ``__nested_*``); ``at0``/``vec_size`` are the token
# scanners from ``bits/cpp_compat.sbg``.
_HELPER_NAMES = {"at0", "vec_size"}


def classify_call(callee: str) -> str:
    if callee.startswith("__") or callee in _HELPER_NAMES:
        return "helper"
    if callee in _g.BUILTIN_EXPR_NAMES or callee in _g.BUILTIN_STMT_NAMES:
        return "builtin"
    return "proc"


def is_impure_builtin(callee: str) -> bool:
    return callee in {
        "answer", "random", "timer", "resetTimer", "ask", "broadcast",
        "broadcastAndWait", "resetDelta", "dt", "deltaTime", "fps", "frame",
        "timeSeconds", "cin",
    }


# ---------------------------------------------------------------------------
# Extra-attribute preservation
# ---------------------------------------------------------------------------

def _extra(node: Any, *fields: str) -> Dict[str, Any]:
    """Capture every attribute of an AST node except the listed dataclass
    fields and the source-location triple (which is re-attached from ``src``)."""
    if node is None or not hasattr(node, "__dict__"):
        return {}
    skip = set(fields) | {"filename", "line", "col"}
    return {k: v for k, v in node.__dict__.items() if k not in skip}


def _apply_attrs(node: Any, attrs: Dict[str, Any], src: Src) -> Any:
    if attrs:
        for k, v in attrs.items():
            setattr(node, k, v)
    if src is not None:
        node.filename, node.line, node.col = src  # type: ignore[attr-defined]
    return node


# ---------------------------------------------------------------------------
# build_ir: lowered AST -> IR
# ---------------------------------------------------------------------------

def build_ir(program: Any) -> IRModule:
    """Convert a lowered AST :class:`Program` into an :class:`IRModule`."""
    from .ast import (  # noqa: F401  (deferred so this module stays import-order safe)
        AssignStmt, BinaryExpr, BreakStmt, CallExpr, ContinueStmt, EventDecl,
        ExprStmt, ForStmt, ForeverStmt, IfStmt, ListDecl, Literal, ProcDecl,
        RepeatStmt, ReturnStmt, UnaryExpr, VarDecl, VarExpr, WhileStmt,
    )

    def expr(e: Any) -> IRValue:
        if isinstance(e, Literal):
            return IRConst(e.value, _src(e))
        if isinstance(e, VarExpr):
            return IRVar(e.name, _src(e))
        if isinstance(e, BinaryExpr):
            return IRBinary(e.op, expr(e.left), expr(e.right), _src(e))
        if isinstance(e, UnaryExpr):
            return IRUnary(e.op, expr(e.expr), _src(e))
        if isinstance(e, CallExpr):
            return IRCall(
                e.callee,
                tuple(expr(a) for a in e.args),
                classify_call(e.callee),
                _src(e),
            )
        # ArrayExpr and any unknown reporter travel opaquely.
        return IRRaw(e, _src(e))

    def stmt(s: Any) -> IRStmt:
        if isinstance(s, VarDecl):
            return IRAssign(
                IRVar(s.name, _src(s)), expr(s.expr), decl=True,
                attrs=_extra(s, "name", "expr", "mutable"), src=_src(s),
            )
        if isinstance(s, AssignStmt):
            if s.op == "=":
                return IRAssign(IRVar(s.name, _src(s)), expr(s.expr), decl=False, src=_src(s))
            if s.op == "+=":
                return IRChange(IRVar(s.name, _src(s)), expr(s.expr), _src(s))
            op = s.op[0]  # "-=" -> "-", "*=" -> "*", "/=" -> "/", "%=" -> "%"
            return IRAssign(
                IRVar(s.name, _src(s)),
                IRBinary(op, IRVar(s.name, _src(s)), expr(s.expr), _src(s)),
                decl=False, src=_src(s),
            )
        if isinstance(s, IfStmt):
            return IRIf(
                expr(s.cond),
                body(s.then_body),
                None if s.else_body is None else body(s.else_body),
                _src(s),
            )
        if isinstance(s, RepeatStmt):
            return IRRepeat(expr(s.count), body(s.body), _src(s))
        if isinstance(s, ForeverStmt):
            return IRForever(body(s.body), _src(s))
        if isinstance(s, WhileStmt):
            return IRWhile(expr(s.cond), body(s.body), _src(s))
        if isinstance(s, ReturnStmt):
            return IRReturn(expr(s.expr) if s.expr is not None else None, _src(s))
        if isinstance(s, BreakStmt):
            return IRBreak(_src(s))
        if isinstance(s, ContinueStmt):
            return IRContinue(_src(s))
        if isinstance(s, ExprStmt):
            if isinstance(s.expr, CallExpr) and s.expr.callee == "log":
                a = s.expr.args
                if not a:
                    val: IRValue = IRConst("", _src(s))
                elif len(a) == 1:
                    val = expr(a[0])
                else:
                    val = expr(a[0])
                    for nxt in a[1:]:
                        val = IRCall("join", (val, expr(nxt)), "builtin", _src(s))
                return IRAppend(_g.TERMINAL_LIST_NAME, val, _src(s))
            return IRExpr(expr(s.expr), _src(s))
        # Everything else (LValueAssignStmt, StructDecl, ArrayExpr stmts, ...)
        # travels opaquely.
        return IRRaw(s, _src(s))

    def body(stmts: Iterable[Any]) -> Tuple[IRStmt, ...]:
        from .ast import BlockStmt

        out: List[IRStmt] = []
        for s in stmts:
            if isinstance(s, ListDecl):
                out.append(IRDeleteList(s.name, _src(s)))
                for item in s.items:
                    out.append(IRAppend(s.name, expr(item), _src(s)))
            elif isinstance(s, ForStmt):
                if s.init is not None:
                    out.append(stmt(s.init))
                sub = list(s.body)
                if s.update is not None:
                    sub.append(s.update)
                cond: IRValue = expr(s.cond) if s.cond is not None else IRConst(True, _src(s))
                out.append(IRWhile(cond, body(sub), _src(s)))
            elif isinstance(s, BlockStmt):
                out.extend(body(s.body))
            else:
                out.append(stmt(s))
        return tuple(out)

    def top(items: Iterable[Any]) -> Tuple[IRTopItem, ...]:
        out: List[IRTopItem] = []
        for s in items:
            if isinstance(s, VarDecl):
                out.append(IRGlobal(
                    "var", s.name, init=expr(s.expr),
                    attrs=_extra(s, "name", "expr", "mutable"), src=_src(s),
                ))
            elif isinstance(s, ListDecl):
                out.append(IRGlobal(
                    "list", s.name, items=tuple(expr(x) for x in s.items),
                    attrs=_extra(s, "name", "items"), src=_src(s),
                ))
            elif isinstance(s, ProcDecl):
                out.append(IRProc(
                    s.name, tuple(s.params), body(s.body), bool(s.warp),
                    returns=_body_has_return(s.body),
                    is_action=(s.name == _g.ACTION_PROC_NAME),
                    attrs=_extra(s, "name", "params", "body", "warp"), src=_src(s),
                ))
            elif isinstance(s, EventDecl):
                out.append(IREvent(
                    s.kind, s.value, body(s.body),
                    attrs=_extra(s, "kind", "value", "body"), src=_src(s),
                ))
            else:
                # TargetDecl / ImportDecl / loose top-level statements stay opaque.
                out.append(IRRaw(s, _src(s)))
        return tuple(out)

    attrs = {k: v for k, v in getattr(program, "__dict__", {}).items() if k != "body"}
    stage = IRTarget("Stage", True, top(program.body))
    return IRModule((stage,), attrs)


def _body_has_return(stmts: Iterable[Any]) -> bool:
    from .ast import ReturnStmt
    for s in stmts:
        if isinstance(s, ReturnStmt):
            return True
    return False


# ---------------------------------------------------------------------------
# lower_ir: IR -> lowered AST
# ---------------------------------------------------------------------------

def lower_ir(module: IRModule) -> Any:
    """Reconstruct a lowered AST :class:`Program` from an :class:`IRModule`."""
    from .ast import (
        AssignStmt, BinaryExpr, BreakStmt, CallExpr, ContinueStmt, EventDecl,
        ExprStmt, IfStmt, ListDecl, Literal, ProcDecl, Program, RepeatStmt,
        ReturnStmt, UnaryExpr, VarDecl, VarExpr, WhileStmt,
    )

    def expr(v: IRValue) -> Any:
        if isinstance(v, IRConst):
            return Literal(v.value)
        if isinstance(v, IRVar):
            return VarExpr(v.name)
        if isinstance(v, IRItem):
            return CallExpr("item", [VarExpr(v.name), expr(v.index)])
        if isinstance(v, IRListLen):
            return CallExpr("len", [VarExpr(v.name)])
        if isinstance(v, IRBinary):
            if v.op == "join":
                return CallExpr("join", [expr(v.left), expr(v.right)])
            return BinaryExpr(expr(v.left), v.op, expr(v.right))
        if isinstance(v, IRUnary):
            return UnaryExpr(v.op, expr(v.operand))
        if isinstance(v, IRCall):
            return CallExpr(v.callee, [expr(a) for a in v.args])
        if isinstance(v, IRRaw):
            return v.node
        raise TypeError(f"cannot lower IR value {v!r}")

    def stmt(s: IRStmt) -> Any:
        if isinstance(s, IRRaw):
            return s.node
        if isinstance(s, IRAssign):
            if s.decl:
                return _apply_attrs(VarDecl(s.target.name, expr(s.value), True), s.attrs, s.src)
            return AssignStmt(s.target.name, "=", expr(s.value))
        if isinstance(s, IRChange):
            return AssignStmt(s.target.name, "+=", expr(s.delta))
        if isinstance(s, IRIf):
            return IfStmt(
                expr(s.cond), body(s.then),
                None if s.else_ is None else body(s.else_),
            )
        if isinstance(s, IRRepeat):
            return RepeatStmt(expr(s.count), body(s.body))
        if isinstance(s, IRForever):
            return ForeverStmt(body(s.body))
        if isinstance(s, IRWhile):
            return WhileStmt(expr(s.cond), body(s.body))
        if isinstance(s, IRReturn):
            return ReturnStmt(expr(s.value) if s.value is not None else None)
        if isinstance(s, IRBreak):
            return BreakStmt()
        if isinstance(s, IRContinue):
            return ContinueStmt()
        if isinstance(s, IRDeleteList):
            return ListDecl(s.name, [])
        if isinstance(s, IRAppend):
            if s.name == _g.TERMINAL_LIST_NAME:
                return ExprStmt(CallExpr("log", [expr(s.value)]))
            return ExprStmt(CallExpr("push", [VarExpr(s.name), expr(s.value)]))
        if isinstance(s, IRExpr):
            return ExprStmt(expr(s.value))
        raise TypeError(f"cannot lower IR statement {s!r}")

    def body(stmts: Iterable[IRStmt]) -> List[Any]:
        out: List[Any] = []
        it = iter(stmts)
        for s in it:
            if isinstance(s, IRDeleteList):
                # Collapse `IRDeleteList(L); IRAppend(L, x)*` back to `ListDecl(L, items)`.
                items: List[Any] = []
                for nxt in it:
                    if isinstance(nxt, IRAppend) and nxt.name == s.name:
                        items.append(expr(nxt.value))
                    else:
                        out.append(ListDecl(s.name, items))
                        out.append(stmt(nxt))
                        break
                else:
                    out.append(ListDecl(s.name, items))
            else:
                out.append(stmt(s))
        return out

    program = Program([])
    for item in module.stage.items:
        if isinstance(item, IRGlobal):
            if item.kind == "list":
                program.body.append(_apply_attrs(
                    ListDecl(item.name, [expr(x) for x in item.items]),
                    item.attrs, item.src,
                ))
            else:
                program.body.append(_apply_attrs(
                    VarDecl(item.name, expr(item.init) if item.init is not None else Literal(0), True),
                    item.attrs, item.src,
                ))
        elif isinstance(item, IRProc):
            program.body.append(_apply_attrs(
                ProcDecl(item.name, list(item.params), body(item.body), item.warp),
                item.attrs, item.src,
            ))
        elif isinstance(item, IREvent):
            program.body.append(_apply_attrs(
                EventDecl(item.kind, item.value, body(item.body)),
                item.attrs, item.src,
            ))
        elif isinstance(item, IRRaw):
            program.body.append(item.node)
        else:
            raise TypeError(f"cannot lower top-level IR item {item!r}")
    if module.attrs:
        for k, v in module.attrs.items():
            setattr(program, k, v)
    return program
