"""Code generators for `std::`-namespace algorithms that operate on a *concrete,
statically-known* Scratch list (sortAsc/sortDesc, lowerBoundTo/upperBoundTo).

Why this lives here and not as real `.sbg` source, like the rest of
`packages/std/`
-----------------------------------------------------------------------------
Vanilla Scratch custom blocks (the compile target for `proc`) cannot receive a
list as a parameter -- there is no "pass this list by reference" mechanism in
the platform. Verified experimentally: compiling a `.sbg` proc that takes a
list *name* as a string parameter and calls `item(name, i)` on it compiles
without error but silently returns the wrong value at runtime (the literal
name string, not the list contents) -- Scratch's list-reporter blocks bind to
a concrete list ID at compile time, not a name resolved at runtime.

So these algorithms are generated as `.sbg` AST nodes (WhileStmt, IfStmt, ...)
*per call site*, with the concrete list name substituted in directly -- the
functional equivalent of writing the same `.sbg` loop by hand at every call
site, done once here instead. This is not a stopgap; it's the only vanilla-
Scratch-compatible way to implement a generic list algorithm today.

This module used to be inline in `sbg/_patches.py`. Moving it here is step one
of retiring `_patches.py`: this file is real, "no active edits" library code,
not a patch layer -- new call sites should call these functions directly
instead of adding another patch layer on top.
"""
from __future__ import annotations

from typing import Any, Optional

from .ast import (
    AssignStmt,
    BinaryExpr,
    CallExpr,
    ExprStmt,
    IfStmt,
    Literal,
    VarDecl,
    VarExpr,
    WhileStmt,
)


def compile_sort_list(builder: Any, lst: str, *, descending: bool = False) -> Optional[str]:
    """Insertion sort over a concrete Scratch list. Small but reliable in
    vanilla Scratch (no native sort block exists)."""
    suffix = builder.uid("sort")
    i = f"__sbg_sort_i_{suffix}"
    j = f"__sbg_sort_j_{suffix}"
    key = f"__sbg_sort_key_{suffix}"
    builder.var_id(i)
    builder.var_id(j)
    builder.var_id(key)
    cmp_op = "<" if descending else ">"
    body: list[Any] = [
        VarDecl(i, Literal(2), True),
        WhileStmt(BinaryExpr(VarExpr(i), "<=", CallExpr("len", [VarExpr(lst)])), [
            VarDecl(key, CallExpr("item", [VarExpr(lst), VarExpr(i)]), True),
            VarDecl(j, BinaryExpr(VarExpr(i), "-", Literal(1)), True),
            WhileStmt(
                BinaryExpr(
                    BinaryExpr(VarExpr(j), ">=", Literal(1)),
                    "&&",
                    BinaryExpr(CallExpr("item", [VarExpr(lst), VarExpr(j)]), cmp_op, VarExpr(key)),
                ),
                [
                    ExprStmt(CallExpr("setItem", [VarExpr(lst), BinaryExpr(VarExpr(j), "+", Literal(1)), CallExpr("item", [VarExpr(lst), VarExpr(j)])])),
                    AssignStmt(j, "-=", Literal(1)),
                ],
            ),
            ExprStmt(CallExpr("setItem", [VarExpr(lst), BinaryExpr(VarExpr(j), "+", Literal(1)), VarExpr(key)])),
            AssignStmt(i, "+=", Literal(1)),
        ]),
    ]
    return builder.compile_statement_chain(body)


def compile_bound_to(builder: Any, lst: str, value_expr: Any, out_name: str, *, upper: bool = False) -> Optional[str]:
    """Binary search over a concrete, already-sorted Scratch list.
    `lower_bound`: first index where item >= value.
    `upper_bound`: first index where item > value.
    """
    suffix = builder.uid("bound")
    lo = f"__sbg_bound_lo_{suffix}"
    hi = f"__sbg_bound_hi_{suffix}"
    mid = f"__sbg_bound_mid_{suffix}"
    builder.var_id(lo)
    builder.var_id(hi)
    builder.var_id(mid)
    builder.var_id(out_name)
    if upper:
        move_right_cond = BinaryExpr(CallExpr("item", [VarExpr(lst), VarExpr(mid)]), "<=", value_expr)
    else:
        move_right_cond = BinaryExpr(CallExpr("item", [VarExpr(lst), VarExpr(mid)]), "<", value_expr)
    body: list[Any] = [
        VarDecl(lo, Literal(1), True),
        VarDecl(hi, BinaryExpr(CallExpr("len", [VarExpr(lst)]), "+", Literal(1)), True),
        WhileStmt(BinaryExpr(VarExpr(lo), "<", VarExpr(hi)), [
            VarDecl(mid, CallExpr("floor", [BinaryExpr(BinaryExpr(VarExpr(lo), "+", VarExpr(hi)), "/", Literal(2))]), True),
            IfStmt(
                move_right_cond,
                [AssignStmt(lo, "=", BinaryExpr(VarExpr(mid), "+", Literal(1)))],
                [AssignStmt(hi, "=", VarExpr(mid))],
            ),
        ]),
        AssignStmt(out_name, "=", VarExpr(lo)),
    ]
    return builder.compile_statement_chain(body)
