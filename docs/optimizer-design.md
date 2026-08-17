# StageBG Optimizer Design — IR, Passes, and CLI

**Status:** design document for the IR + optimizer implementation work.
**Audience:** the engineer implementing the `sbg/` optimizer package.
**Source of truth:** the committed `sbg.py` snapshot at `HEAD` (analyzed as `/tmp/sbg_snapshot.py`, 10,180 lines). All line numbers below refer to that snapshot. The live working tree is being split into a package by a parallel effort and MUST NOT be assumed stable while reading this document; re-derive from the snapshot if a line reference drifts.

**Hard requirement (compatibility anchor):** `-O0` (the default) must produce a **byte-identical `.sb3`** to today's compiler. That is: unzipping `project.json` from `sbg compile -O0 foo.sbg` and from today's `sbg compile foo.sbg` must yield identical bytes, for every program, including patch-order effects (metadata, warp mutations, terminal monitor id). Any deviation is a regression.

---

## 1. The pipeline today (what we are optimizing against)

The current compile path is, in order (snapshot line refs):

1. **Parse** — `parse_source(text, filename)` (final wrapper at line 8500) calls, in reverse patch order:
   - base `parse_source` → `ImportResolver().parse_entry(text, filename)` (lines 861–862),
   - `_sbg_mangle_locals` (patch15b, lines 5617–5739) which renames locals to `__loc_{n}_{sanitized}`,
   - the struct/vector **lowering wrapper** (patch21, line 8500) which sets `program.sbg_struct_defs` and rewrites `program.body = _sbg_lower_structs_body21(program.body)` (line 8505).
2. **Embedded files** — `_program_with_embedded_files(program, source_path, embeds=..., embed_dirs=...)` (line 4522) prepends six `ListDecl`s (`__sbg_file_names`, `__sbg_file_texts`, `__sbg_file_sizes`, `__sbg_file_line_start`, `__sbg_file_line_count`, `__sbg_file_lines`) as a `Program([*decls, *program.body])` (line 4553).
3. **Compile** — `Compiler(program, allow_library=...).compile()` (class at lines 1758–2073). `compile()` (final wrap at line 9991) runs `analyze()` then emits blocks via `ScratchBuilder` (lines 1284–1757), builds the console flag/action entry points, and returns a project dict whose `targets[0]` carries `variables`/`lists`/`broadcasts`/`blocks` plus `monitors` (the Terminal list monitor, id `,(0/{jAb*2vBd56rlG@1`, lines 1266–1282).
4. **Warp post-process** — `_sbg_project_set_warp(project, True/False)` (line 5203) is already called inside `Compiler.compile` (patch14, line 5217) and re-invoked by the CLI when `--no-turbo` is passed (line 5297).
5. **Write** — `write_sb3_project(project, output_path, verify=True)` (lines 2145–2252) writes `project.json` + the backdrop SVG.

The optimizer inserts itself **between step 2 and step 3**: after embedded-file prep and after struct/vector lowering, but before Scratch block emission. This placement is forced by the data we must not reimplement: struct fields have already been flattened to `name.field` scalars/lists, nested vectors already flattened to row-encoded lists + `.field` flat tables, and locals already mangled. The IR consumes the *lowered* AST.

### Key lowered-AST shapes the IR must carry

These are the shapes produced by patch21 lowering and consumed by `ScratchBuilder`/`Compiler`; the IR lowering rules must preserve them exactly at `-O0`.

- **AST node dataclasses** (lines 265–375): `Program(body)`, `ImportDecl`, `VarDecl(name, expr, mutable=True)`, `ListDecl(name, items)`, `EventDecl(kind, value, body)`, `ProcDecl(name, params, body, warp=False)`, `BlockStmt`, `IfStmt(cond, then_body, else_body)`, `RepeatStmt(count, body)`, `ForeverStmt(body)`, `WhileStmt(cond, body)`, `ForStmt(init, cond, update, body)`, `ReturnStmt(expr)`, `BreakStmt`, `ContinueStmt`, `AssignStmt(name, op, expr)`, `ExprStmt(expr)`, `Literal(value)`, `VarExpr(name)`, `BinaryExpr(left, op, right)`, `UnaryExpr(op, expr)`, `CallExpr(callee, args)`, `ArrayExpr(items)`.
- **Struct lowering** (patch21): `StructDecl(name, fields: List[Tuple[str,str]])` is consumed (not emitted) by `_sbg_lower_structs_body21` (line 8471); `_sbg_expand_struct_var21` (line 8446) turns `StructVarDecl(typ, name)` into `[VarDecl(name), VarDecl/ListDecl(f"{name}.{field}") ...]`; `_sbg_expand_nested_vector21` (line 8457) turns `NestedVectorDecl(name, typ, rows)` into `[ListDecl(name, encoded_rows), ListDecl(f"{name}.__row_size", []), ListDecl(f"{name}.{field}", []) ...]`. Registry: `_SBG_STRUCT_DEFS21` (line 8172), `_SBG_FLAT_VECTOR_TYPES21` (line 8173, maps base list name → struct name for `vector<vector<Struct>>`).
- **Flat-struct ABI helpers** emitted/consumed later: `__flat_struct_resize_outer(base, n)` (compiled at lines 8799–8808 to `resizeList` of `base` + `base.__row_size`), `__flat_struct_push(base, row, value)` (runtime native only; Scratch compile raises at line 8810 — a known gap), `__flat_struct_row_size(base, row)` (native eval lines 8789–8792), and runtime native eval in `_runtime_eval_patch21o` (lines 9367–9478).
- **Row-encoding ABI** (`vector<vector<T>>`): rows are **string-encoded, space-separated**; `_sbg_encode_row21` (line 8428) joins literal row elements with `" "`. Accessors for dynamic rows are `at0(vec, idx)` and `vec_size(vec)` defined in `packages/bits/cpp_compat.sbg` — both are **O(tokens) string-scanners** (loop over `letter(vec, i)` and `join`). `item(row_string, i)` over an encoded row is rewritten to `at0(row, i-1)` (patch21f, lines 8830–8835). These two helpers are the primary strength-reduction target (Section 4).
- **`Compiler.compile` block/console glue**: `compile_console_flag_loop` (patch23 REPL at line 9733; patch25 single-main fast path at line 9785), `compile_console_action_definition` (patch11, line 3492), `_sbg_compile_delta_reset`, and terminal visibility helpers (`_sbg_make_terminal_visible_setter_patch23`, line 9692). The optimizer must never reorder these entry-point blocks; they are the frame skeleton.
- **Tree-shaking already exists**: patch16b (`_sbg_reachable_proc_names`, lines 6090–6201) BFS-removes unreachable procs unless `--allow-library`. The IR pass list must treat this as already-applied input, not re-implement it at `-O0`.

---

## 2. IR design

### 2.1 Placement and the `-O0` invariant

The IR is a **structured, statement-linearized intermediate form** between the lowered AST (Section 1) and Scratch opcode emission. Pipeline:

```
parse_source ─▶ _program_with_embedded_files ─▶ [lowered AST]
                                                      │
        ┌─────────────────────────────────────────────┤
        │ -O0 (default): Compiler(program).compile()  │  ← byte-identical to today
        │                                             │
        │ -O1/-O2/-O3:                                 │
        │   ir = build_ir(program)                     │
        │   ir = run_passes(ir, level)                 │
        │   lowered = lower_ir(ir)   # back to lowered AST shape
        │   project = emit_scratch(lowered, ...)       │
        └─────────────────────────────────────────────┘
                      │
          _sbg_project_set_warp(project, ...) ─▶ write_sb3_project(...)
```

**Invariant strategy (recommended, avoids the classic "IR must round-trip byte-exactly" trap):** `-O0` never enters the IR. The `compile_project(source, level, ...)` hook (Section 5) short-circuits `level == 0` straight to `Compiler(program).compile()` — the exact existing code path. The IR only needs to be *semantics-preserving*, not byte-preserving, because it is only exercised at `-O1+`. This guarantees the `-O0 == today` contract with zero risk from IR construction bugs. If a future maintainer instead wants one unified IR path, the acceptance test in Section 6 makes byte-equality at `-O0` a hard gate, so either strategy is safe.

### 2.2 IR node types

The IR is a frozen-dataclass tree. Every node carries `src` (source location for diagnostics). Value nodes (reporters) and statement nodes (command chains) are distinct; this mirrors Scratch's fundamental reporter/command split, so no pass can accidentally build a reporter where Scratch needs a command.

**Module / declaration nodes**

| Node | Fields | Meaning |
|---|---|---|
| `IRModule` | `targets: List[IRTarget]` | whole program (multi-target) |
| `IRTarget` | `name: str`, `is_stage: bool`, `globals: List[IRGlobal]`, `procs: List[IRProc]`, `events: List[IREvent]` | Stage or sprite |
| `IRGlobal` | `kind: Literal["var","list"]`, `name: str`, `init: Optional[IRValue]` (var) / `List[IRValue]` (list) | lowered `VarDecl`/`ListDecl`; carries the terminal/file-table special lists |
| `IRProc` | `name: str`, `params: List[str]`, `body: List[IRStmt]`, `warp: bool`, `returns: bool`, `is_action: bool` | lowered `ProcDecl` |
| `IREvent` | `kind: Literal["flag","message","action"]`, `value: Optional[str]`, `body: List[IRStmt]` | lowered `EventDecl` |

**Value (reporter) nodes**

| Node | Fields | Meaning |
|---|---|---|
| `IRConst` | `value: Any` | `Literal` |
| `IRVar` | `name: str` | scalar read (global/local/param); compiles to `data_variable` reporter |
| `IRItem` | `name: str`, `index: IRValue` | `item N of list` reporter (`data_itemoflist`) |
| `IRListLen` | `name: str` | `data_lengthoflist` (compiles to length-of-list, not string `len`) |
| `IRBinary` | `op: str`, `left: IRValue`, `right: IRValue` | `operator_*` / `operator_join` |
| `IRUnary` | `op: str`, `operand: IRValue` | `operator_not` |
| `IRCall` | `callee: str`, `args: List[IRValue]`, `kind: Literal["builtin","proc","helper"]` | `kind="helper"` marks the mangle/ABI calls (`at0`, `vec_size`, `__flat_struct_*`) so passes can specialize them; `kind="builtin"` marks impure builtins (`answer`, `random`, `timer`, `resetTimer`, `ask`, `broadcast`, `wait`) so constant-folding never touches them |

**Statement (command) nodes**

| Node | Fields | Meaning |
|---|---|---|
| `IRAssign` | `target: Union[IRVar, IRItem]`, `value: IRValue` | `data_setvariableto` / `data_replaceitemoflist` |
| `IRChange` | `target: Union[IRVar, IRItem]`, `delta: IRValue` | `data_changevariableby` |
| `IRIf` | `cond: IRValue`, `then: List[IRStmt]`, `else_: List[IRStmt]` | `control_if`/`control_if_else` |
| `IRRepeat` | `count: IRValue`, `body: List[IRStmt]` | `control_repeat` |
| `IRForever` | `body: List[IRStmt]` | `control_forever` |
| `IRWhile` | `cond: IRValue`, `body: List[IRStmt]` | `control_repeat_until <not cond>` |
| `IRReturn` | `value: Optional[IRValue]` | return-temp + flag set (see `__sbg_ret_*` / `__sbg_returning_*`, lines 3419–3420, 3112–3113) |
| `IRDeleteList` | `name: str` | `data_deletealloflist` (list init) |
| `IRAppend` | `name: str`, `value: IRValue` | `data_addtolist` (list init + `log`) |
| `IRExpr` | `value: IRValue` | statement-position reporter (void builtin calls) |

This is deliberately **not SSA** and **has no explicit CFG**. Scratch is a linear script: loops are nested `control_*` blocks, there is no arbitrary `goto`, and the compiler already linearizes expressions into temp variables (`__sbg_tmp_*`, line 2869; `__sbg_repeat_i_*`, line 6014). A structured IR keeps every pass a tree walk, which is far cheaper to verify than an SSA construction and matches the one-block-at-a-time cost model. Data-flow analyses (reaching defs, liveness, loop-invariance) run over the nested statement tree; they do not need a CFG for this language's subset.

### 2.3 IR ↔ AST lowering rules (concrete)

`build_ir` consumes the **post-patch21 lowered AST**, `lower_ir` must regenerate an AST that `ScratchBuilder.compile_stmt`/`compile_expr` (lines 1284–1757) accept unchanged. The rules below are the contract; they are a direct transcription of what the builder already does:

1. `VarDecl(name, expr, mutable=True)` → `IRGlobal("var", name, expr)`. A `VarDecl` inside a procedure body → leading `IRAssign(IRVar(name), expr)` + registration of a local (name already mangled by `_sbg_mangle_locals`).
2. `ListDecl(name, items)` → `IRGlobal("list", name, items)`; its runtime init is a `IRDeleteList(name)` followed by one `IRAppend(name, item)` per item (this is exactly `ScratchBuilder.compile_stmt`'s `ListDecl` behavior).
3. `AssignStmt(name, op, expr)`:
   - `op == "="` → `IRAssign(IRVar(name), expr)`; if `name` is `x.f` of a flattened struct scalar → `IRAssign(IRVar("x.f"), expr)` (already a scalar post-lowering); if `name` is a nested-vector item `m[i][j]` → `IRAssign(IRItem(base, row_or_flat_index), expr)` per patch22 (`_sbg_match_nested_vector_item22`, lines 9487–9560).
   - `op == "+="`/`"-="`/`"*="`/`"/="` → `IRChange(target, delta)` (maps to `data_changevariableby`, and the builder's existing multiply/divide-by-change special case).
4. `IfStmt/RepeatStmt/ForeverStmt/WhileStmt/ForStmt` map 1:1 to `IRIf/IRRepeat/IRForever/IRWhile`; `ForStmt(init, cond, update, body)` lowers to `init` statements + `IRWhile(cond, [*body, *update])` (this is the existing `ForStmt` compile).
5. `ReturnStmt(expr)` → `IRReturn(expr)`; codegen sets the per-proc `__sbg_ret_{name}` and `__sbg_returning_{name}` vars (lines 3112–3113).
6. `CallExpr` classification: `callee in BUILTIN_EXPR_NAMES` (line 1266) and `BUILTIN_STMT_NAMES` (line 1275) → `kind="builtin"`; `at0`/`vec_size`/`__flat_struct_*` → `kind="helper"`; otherwise `kind="proc"`.
7. `ExprStmt(CallExpr("log", [v]))` → `IRAppend("Terminal", v)`; the Terminal list id is fixed (line 1267).
8. `BinaryExpr(..., "join")` chains (`join(a, join(b, ...))`) are preserved as nested `IRBinary("join", ...)` so the join-fusion pass can match them.

The `lower_ir` direction is the inverse and must reproduce the same field names/order the builder reads (`stmt.cond`, `stmt.then_body`, `stmt.else_body`, `stmt.body`, `stmt.count`, `stmt.init/cond/update`), plus `sbg_type`/`sbg_struct_defs`/`sbg_flat_vector_types` attributes that patch21 attaches (lines 8504–8507) and the `__flat_struct_*` ABI call shapes.

---

## 3. Pass list by level

| Level | Passes (in order) | Notes |
|---|---|---|
| **-O0** | *(none)* | short-circuit to `Compiler(program).compile()`; byte-identical to today |
| **-O1** | `collect-info` → `constant-fold` → `specialize-vec-helpers` → `merge-join-literals` → `dce` | safe, intra-procedure, side-effect-preserving |
| **-O2** | all -O1 → `sroa-struct-scalars` → `dead-list-variable-elim` → `inline-small-procs` → `loop-invariant-hoist` → `dce` (rerun) | structural memory + modest inlining |
| **-O3** | all -O2 → `flat-list-fusion` → `vector-scalar-promotion` → `warp-turbo-placement` → `terminal-output-minimize` → `dce` (rerun) | aggressive, Scratch-cost-targeted |

Ordering constraints (why this order):

- `collect-info` must run first: it builds the global/local symbol tables, classifies each name as var vs list, computes liveness and loop-invariance facts, and records which lists are the `Terminal`/`__sbg_file_*` special lists. Every later pass consumes it.
- `constant-fold` before `dce` so that branches with constant conditions can be simplified and their dead arms removed.
- `merge-join-literals` before `dce` so a folded join can reveal a dead store.
- `inline-small-procs` before `loop-invariant-hoist` (inlining exposes invariant code across the call boundary); `dce` rerun after inlining to remove now-dead call args.
- `dead-list-variable-elim` after inlining/hoisting (their outputs feed liveness); rerun `dce` last.
- `terminal-output-minimize` is always last among semantic passes (it reorders `IRAppend("Terminal", ...)`) and is the only pass gated behind an explicit behavior flag (Section 4.10, because it changes the Terminal list item granularity).

At every level, `run_passes` is `collect-info` plus the level's list, and **every pass must be a no-op that returns its input unchanged when it cannot prove its precondition** — a conservative optimizer is the whole safety story here because Scratch has no exception model, no aliasing, and no pointers (Section 7).

---

## 4. Aggressive -O3 for vanilla Scratch — pass catalog with cost model

### 4.0 Cost model (definition, used by every pass's budget check)

Compile-time cost of a generated project, computable from `project.json` (mirrors `inspect_sb3`, lines 2258–2276, plus list items and monitors):

```
cost(project) = 1 * blocks
              + 3 * variables
              + 5 * lists
              + 1 * sum(len(list.items))        # each init item is a data_addtolist block
              + 10 * monitors
```

Runtime cost, used by loop/terminal passes:

- per-frame cost of a `control_forever` body = `blocks_in_body` (executed every tick while running).
- `at0(vec, i)` / `vec_size(vec)` = O(tokens) Scratch blocks per access (string scan over `letter`+`join`+comparison in `bits/cpp_compat.sbg`).
- non-warp custom block = ~1 frame (30 fps) yield per call; warp custom block = atomic, near-free at VM level.

Every pass below states a measurable win against this model.

### 4.1 `constant-fold` (O1) — constant folding + propagation

- **Pattern:** `IRBinary(op, IRConst, IRConst)` for arithmetic/compare/logic ops; `IRUnary`; `IRVar`/`IRItem` whose only reaching definition is a constant `IRAssign` (SSA-lite within a basic block; no phi nodes because the IR is structured).
- **Transform:** replace with `IRConst(result)`.
- **Never fold (impure builtins):** `answer`, `random`, `timer`, `resetTimer`, `ask`, `broadcast`, `broadcastAndWait`, `resetDelta`, `dt`/`deltaTime`, `fps`, `frame`, `timeSeconds` (lines 1266–1282, 5163–5199). Any `IRCall` with `kind != "proc"` and these names is a folding barrier.
- **Cost win:** each folded reporter removes 1 block (the `operator_*` block) and its two input reporters; a folded `IRIf` removes the whole `control_if` stack.
- **Measurable:** `blocks` count decreases; `opcodes["operator_add"]` etc. drop in `inspect_sb3`.

### 4.2 `specialize-vec-helpers` (O1) — strength reduction of mangle helpers

Targets the O(tokens) `at0`/`vec_size` string-scanners (`packages/bits/cpp_compat.sbg`, lines 4–60) and the flat-struct ABI calls.

- **Pattern A:** `IRCall("vec_size", [IRConst(row_string)])` → `IRConst(token_count(row_string))` (count space/comma/semicolon tokens per the `vec_size` scanner).
- **Pattern B:** `IRCall("at0", [IRConst(row_string), IRConst(idx)])` → `IRConst(token)` (same scanner semantics; `idx` is 0-based inside `at0`, translated from 1-based `item`).
- **Pattern C:** `IRCall("__flat_struct_row_size", [IRConst(base), IRConst(row)])` where `base.__row_size` is initialized from a literal row count (from `NestedVectorDecl` initial rows) and the list is never mutated → `IRConst(len(row))`.
- **Pattern D:** `IRCall("at0", [IRVar(row), IRConst(0)])` where `row` is a flat `.field` scalar table of a `vector<vector<Struct>>` → `IRItem(row, IRConst(1))` (patch21 already routes flat-struct field reads through `__field_ref`/`__index0_ref`, line 7844; this generalizes it).
- **Preconditions:** the row/field list must be provably not `resizeList`ed/replaced after its init (no `IRCall("resizeList", ...)` reaching the read). If not provable, skip.
- **Cost win:** removes an entire `while` loop of `letter`/`join`/comparison blocks (tens of blocks) and replaces it with 1 constant reporter. This is the single largest per-access win in nested-vector code.
- **Measurable:** `opcodes["procedures_call"]` count drops; `csharp_nested_vector_*` examples see `blocks` fall by dozens.

### 4.3 `merge-join-literals` (O1) — join/string-concat fusion

- **Pattern:** nested `IRBinary("join", ...)` trees whose leaves are all `IRConst`, or a mixed tree where the adjacent constant leaves can be merged (`join(join(a,b),c)` with `a,b` const → `join(IRConst(a+b), c)`).
- **Transform:** replace with a single `IRConst(concatenated)`. This is *compile-time* fusion only — it never changes runtime list item granularity, so it is safe at O1.
- **Cost win:** `operator_join` chain of K joins collapses to 0 blocks (K reporter blocks removed).
- **Measurable:** `opcodes["operator_join"]` drops; string-heavy examples (`return.sbg`, `std_demo.sbg`'s `console.log(join(...))` call sites) shrink.

### 4.4 `dce` (O1, rerun at O2/O3) — dead code elimination

- **Pattern:** statements after `IRReturn`/`IRBreak`/`IRContinue` in the same `body` (unreachable); `IRAssign`/`IRChange`/`IRExpr` whose target/value is never subsequently read (liveness from `collect-info`) and whose evaluation is side-effect-free; dead `IRIf` arms when `cond` is constant (after `constant-fold`).
- **Transform:** remove the dead statements.
- **Never remove:** any `IRAppend("Terminal", ...)` (observable), `IRDeleteList`, `IRAppend` to any list (list mutation is a side effect), `IRExpr` whose `IRCall` is a builtin with side effects (`log`, `broadcast`, `wait`, `ask`, `push`, `insert`, `delete`, `replace`, `setBackdrop`, `playSound`, `stopAllSounds`).
- **Cost win:** removes dead blocks; dead assignments also enable `sroa`/`dead-list-variable-elim` downstream.
- **Measurable:** `blocks` decreases; no `variables`/`lists` count change yet (that is the next pass).

### 4.5 `sroa-struct-scalars` (O2) — Struct SROA

Struct fields are already flattened by patch21 to `name.field` scalars (`_sbg_expand_struct_var21`, line 8446), so the classic SROA "split a struct into scalars" step is pre-done. This pass removes the **dead fields** of that flattening.

- **Pattern:** a `IRGlobal("var", "x.f")` (or the struct root var `x`) that is never read, or only written.
- **Transform:** delete the field's `IRGlobal` and all writes to it. This is exactly `dce` lifted to the struct-field granularity, but it must respect the struct **copy-construction** shape: if `y = x;` copies the whole struct, `x.f` is read-by-copy even without an explicit `IRVar("x.f")` read — `collect-info` must mark every field of `x` live at a struct-to-struct assignment.
- **Cost win:** 3 units per removed variable (init block + monitor + storage) plus its write blocks.
- **Measurable:** `variables` count decreases in `cpp_struct_flat_generic_demo.sbg` if any `Edge` field is unused.

### 4.6 `dead-list-variable-elim` (O2)

- **Pattern:** `IRGlobal("list", L)` or `IRGlobal("var", v)` whose contents are never read by any `IRItem`/`IRListLen`/`IRVar` in any reachable `IRProc`/`IREvent`.
- **Transform:** delete the `IRGlobal` and its `IRDeleteList` + init `IRAppend`s.
- **Never remove:** `Terminal`, the six `__sbg_file_*` tables (line 4146), the `Action` return vars, any list the user exposes via `showList`/`showVariable` (those are observables), or any list that is read only by the *native runner* but not by Scratch (the two runners must stay in sync; see Section 6 rule).
- **Cost win:** 5 + N units per removed list (list + init items), 3 units per removed variable.
- **Measurable:** `lists`/`variables` counts drop; `sprites.sbg`/`files_*.sbg` are good candidates (file tables are the ones we must keep).

### 4.7 `inline-small-procs` (O2) — small-procedure inlining with block budget

- **Pattern:** `IRCall("p", args)` where `IRProc p` is a leaf (calls no other proc, or only builtins), non-recursive, `not p.is_action`, and `block_count(p.body) <= BUDGET` (default `BUDGET = 8`).
- **Transform:** substitute `p`'s body with formals replaced by the argument expressions; if `p.returns`, bind the return temp to a fresh local. Do **not** inline procs whose name starts with `__sbg_` (compiler-generated) or `__flat_struct_*`.
- **Cost win:** removes one `procedures_call` block (plus `procedures_definition` + `procedures_prototype` if the proc becomes unreachable, then `dce` drops the definition). Runtime also improves because a `procedures_call` in a hot loop is a VM boundary.
- **Budget check:** inline only if `post_inline_block_count <= pre_inline_block_count - 1` (net block reduction) — a pure size budget, never a size increase.
- **Measurable:** `opcodes["procedures_call"]` and `opcodes["procedures_definition"]` drop; `blocks` non-increasing.

### 4.8 `loop-invariant-hoist` (O2) — loop-invariant code motion

- **Pattern:** `IRAssign`/`IRChange`/`IRExpr` inside `IRForever`/`IRWhile`/`IRRepeat` whose operands are loop-invariant (per `collect-info`: no operand is defined inside the loop, and the call is pure).
- **Transform:** move the statement immediately before the loop, preserving order among hoisted statements.
- **Never hoist:** anything reading `answer`, `timer`, `random`, `dt`, `fps`, `frame`, `timeSeconds`, or any list/variable mutated inside the loop.
- **Cost win:** the moved blocks stop costing per frame; for a `forever` game loop this is the dominant runtime win.
- **Measurable:** `blocks` count is unchanged (same blocks, different nesting), so this pass is measured by **runtime** (native runner output/behavior identical, Section 6) plus a targeted assertion that a hoisted assignment appears at top level, not inside `forever`.

### 4.9 `flat-list-fusion` (O3) — SoA list fusion for `vector<vector<Struct>>`

- **Pattern:** the `N` parallel flat field lists `base.{f1}`, `base.{f2}`, ... plus `base.__row_size` emitted by `_sbg_expand_nested_vector21` (line 8457). In Scratch these are separate lists, so the "fusion" is not literal list merging (Scratch has no struct-of-array row type); it is **row-size elision + index demotion**.
- **Transform A (row-size elision):** if every `__flat_struct_row_size(base, r)` call can be answered by a compile-time constant or a single `data_lengthoflist(base.{f1})` (when all rows are the same length), delete the `base.__row_size` list and rewrite `__flat_struct_row_size` to `IRListLen("base.f1")`.
- **Transform B (index demotion):** replace `IRCall("__flat_struct_push", [base, row, value])` with direct `IRAppend("base.{f1}", ...)` etc. where the struct copy is fully static — this closes the known Scratch gap at line 8810 (`__flat_struct_push` currently raises on Scratch compile) by lowering it to N field appends.
- **Cost win:** `5 + rows` units per removed `.__row_size` list; per-push removal of a helper proc call.
- **Measurable:** `lists` count drops in `cpp_struct_flat_generic_demo.sbg`; `__flat_struct_push` no longer raises.

### 4.10 `vector-scalar-promotion` (O3)

- **Pattern:** `IRGlobal("list", v)` whose `len` is provably bounded by a small constant (default `<= 4`) and whose reads are all `IRItem(v, IRConst(i))` with `i` in bounds.
- **Transform:** replace with `4` scalar `IRGlobal("var", "v.0"... "v.3")` and rewrite `IRItem` to `IRVar`; drop the list + `data_deletealloflist` init.
- **Cost win:** `5 + len` units (list + items) → `3 * len` units (scalars), and every access becomes a variable reporter instead of a list reporter (list reporters render as monitors and are heavier in the VM).
- **Measurable:** `lists` → 0 for the promoted vector; `variables` + small N; `cpp_simple_subset_demo.sbg`'s `vector<int> v = {5,1,4,2,3}` is NOT eligible (len 5 > 4) — the doc sets the budget explicitly so tests can pick a 4-element example.

### 4.11 `warp-turbo-placement` (O3)

Warp is already default-on for every custom block (patch12, lines 3937–3975; patch14, line 5217). The O3 work is placement, not enablement.

- **Transform:** classify each `IRProc` as *pure* (no `ask`/`wait`/`broadcastAndWait`/`resetTimer`/`random` in its transitive body) vs *interactive*. Leave warp on for pure procs. For interactive procs, do NOT change anything (Section 7: warp atomicity is a correctness property, not an optimization target). The only O3 action is to **keep** warp on pure procs and add a `meta.stagebgWarpAnalysis` note listing which procs are pure — this gives the front-end a factual basis for `--no-turbo` decisions and prevents regressions where a future change accidentally de-warps a hot pure proc.
- **Cost win:** 0 blocks; protects the existing atomic-fast behavior.
- **Measurable:** `project.meta.stagebgWarpAnalysis` populated; `_sbg_project_set_warp` output unchanged vs O0.

### 4.12 `terminal-output-minimize` (O3, **opt-in flag**)

The one pass that changes observable output shape (Terminal list item granularity). Because Section 6's acceptance rule is "native stdout equality vs O0", this pass is **off by default even at O3** and enabled only by `--opt-terminal-batch` (or `-O3` plus an explicit env/flag; the CLI section picks one).

- **Pattern:** consecutive `IRAppend("Terminal", x1); IRAppend("Terminal", x2); ...` in the same basic block (especially inside a loop).
- **Transform:** merge into `IRAppend("Terminal", join(x1, "\n", x2, ...))` using a sentinel separator, **and** the native runner's `Runtime.call("log", ...)` / terminal printer is taught to split that exact sentinel into the same number of lines the unbatched program would have printed. This keeps stdout byte-equal while reducing Scratch list-append blocks and Terminal monitor churn.
- **Cost win:** `1 block + 1 list item` per merged log.
- **Measurable:** `opcodes["data_addtolist"]` drops; native `run -O3 --opt-terminal-batch` stdout equals `run -O0` stdout (the acceptance gate).

---

## 5. CLI wiring

Add an optimization-level flag to **both** `compile` and `run`, default `-O0`, plus one behavior flag for the batching pass. The argparse surface lives in the final `main()` (lines 5229–5343). Proposed:

- `comp.add_argument("-O", "--opt-level", type=int, choices=[0,1,2,3], default=0, dest="opt_level", help="optimization level 0-3 (0 = byte-identical to today)")`
- `runp.add_argument(... same ...)`
- `comp.add_argument("--opt-terminal-batch", action="store_true", help="enable terminal-output batching (O3 only; changes Terminal list item granularity)")`

**Layout-agnostic hook** (the parallel package-splitting effort will place it; this is the API it must expose, decoupled from file layout):

```python
def compile_project(
    source: str,                       # full source text
    level: int = 0,
    *,
    filename: str = "<source>",
    embeds: Optional[List[str]] = None,
    embed_dirs: Optional[List[str]] = None,
    allow_library: bool = False,
    no_turbo: bool = False,
    terminal_batch: bool = False,
    verify: bool = True,
) -> Dict:   # the project dict, pre-write_sb3_project
```

Behavior (mirrors the current `main()` compile path, lines 5290–5299):

1. `program = parse_source(source, filename)`
2. `program = _program_with_embedded_files(program, filename, embeds=embeds, embed_dirs=embed_dirs)` — `filename` is used only as the base dir for embed path resolution.
3. If `level == 0` or the IR module is not yet wired: `project = Compiler(program, allow_library=allow_library).compile()` (today's exact path).
4. Else: `ir = build_ir(program); ir = run_passes(ir, level, terminal_batch=terminal_batch); project = emit_scratch(lower_ir(ir), allow_library=allow_library)`.
5. `if no_turbo: _sbg_project_set_warp(project, False)`.
6. Return `project`; callers write with `write_sb3_project(project, output_path, verify=verify)`.

The `run` subcommand keeps its existing "compile-then-run" guarantee (`assert_scratch_compatible`, line 2541) and simply passes `level` through, so `sbg run -O3` verifies the O3 project compiles before executing natively.

---

## 6. Test & verification plan

### 6.1 Metric definition (precise)

All metrics are read from the generated `project.json` (equivalently via `inspect_sb3`, lines 2258–2276, extended with the two fields noted):

- `blocks` = `sum(len(t["blocks"]) for t in targets)`
- `variables` = `sum(len(t["variables"]) for t in targets)`
- `lists` = `sum(len(t["lists"]) for t in targets)`
- `monitors` = `len(project["monitors"])`
- `list_items` = `sum(len(v) for t in targets for v in t["lists"].values())` (init-item volume; requires reading list init values, not in `inspect_sb3` today — extend it)
- `terminal_volume` = runtime: number of lines printed by `sbg run --once --input "..."` (native stdout line count).
- `opcode_histogram` = `inspect_sb3(...)["opcodes"]`.

**Regression rule:** for each example, `blocks(O3) <= blocks(O1) <= blocks(O0)`, `lists(O3) <= lists(O0)`, `variables(O3) <= variables(O0)`, `monitors(O3) == monitors(O0)` (never add monitors). Any pass that increases `blocks` must be proven neutral or backed out (the inline budget, Section 4.7, already forbids increases).

### 6.2 `-O0` byte-identity gate

For every example: `unzip -p out-O0.sb3 project.json` == `unzip -p out-baseline.sb3 project.json` byte-for-byte (baseline compiled with today's unmodified compiler). This is the first test in CI.

### 6.3 Example → feature matrix (which examples exercise what)

| Example | Exercises | Expected direction at O3 |
|---|---|---|
| `cpp_struct_flat_generic_demo.sbg` | `struct Edge`, `vector<vector<Edge>>`, `.push_back`, flat SoA tables | `lists` drops (row-size elision / field dead-list), `blocks` drops (helper specialization) |
| `cpp_nested_vector_demo.sbg` | `vector<vector<double>>` literal init, `[i][j]` reads, `.size()` | `at0`/`vec_size` on literal rows folded → `blocks` drops |
| `cpp_nested_vector_generic_demo.sbg` | `vector<vector<int>>` dynamic rows, `push_back`, item write `+=` | helper specialization + item lowering; `blocks` drops |
| `cpp_simple_subset_demo.sbg` | `vector<int>` literal, `push_back`, range-`for`, priority_queue, `while` | inline tiny procs + DCE + const-fold; `blocks` drops |
| `dot_methods_demo.sbg` | dot-method lowering (`v.size()`, `pq.push`) | const-fold + inline; `blocks` drops |
| `foreach_range_demo.sbg` | `for (auto x : xs)`, indexed `xs.at(i)`, `range(1,6)` | loop-invariant hoist; `blocks` non-increasing, runtime faster |
| `std_demo.sbg` / `pro_std_demo.sbg` | std helpers, `console.log(join(...))` | join fusion; `operator_join` drops |
| `bits_demo.sbg` | `pq.push`/`while (!pq.empty())` | inline + DCE; `procedures_call` drops |
| `return.sbg` | Action return values | must be **unchanged** in terminal output; `-O3` stdout == `-O0` stdout |
| `files_demo.sbg` / `files_kv_demo.sbg` | embedded-file `__sbg_file_*` tables | dead-list elim must **NOT** drop the file tables; assert `lists >= 6` |
| `sprites.sbg` | multi-target, broadcasts, sprite logs | per-target optimization; `monitors` unchanged; broadcast ids preserved |
| `terminal_visibility_demo.sbg` | `terminal.show()/hide()`, prompt toggles | warp/prompt glue preserved; `-O3` stdout == `-O0` |

### 6.4 Output-equality rule for `-O3` native run

For every example and for a fixed `--input`, `sbg run -O3 example.sbg --once --input "X"` must print **identical stdout** to `sbg run -O0 example.sbg --once --input "X"`. The only exception is `--opt-terminal-batch`, which is allowed to reorder Terminal items *only if* the native runner's batching mirror (Section 4.12) reproduces the same stdout; if the mirror is not implemented, the flag must refuse to run.

### 6.5 Manual smoke list

1. `sbg compile -O0 examples/return.sbg /tmp/o0.sb3` then `sbg inspect /tmp/o0.sb3` — sanity-check the metric path.
2. `sbg compile -O3 examples/cpp_struct_flat_generic_demo.sbg /tmp/o3.sb3` — confirm `lists` < baseline and no `__flat_struct_push` compile error.
3. `sbg run -O3 examples/cpp_nested_vector_generic_demo.sbg --once` — stdout equality vs `-O0`.
4. `sbg run -O3 examples/terminal_visibility_demo.sbg --terminal` — interactive prompt toggles still behave (hide/show).
5. Open `/tmp/o3.sb3` in the Scratch editor: confirm it loads, the green flag runs, the Terminal monitor shows the same lines as `-O0`, and warp custom blocks still run atomically.

---

## 7. Risks & non-goals

**What we will NOT optimize (correctness boundaries):**

- **Warp atomicity.** Never merge, reorder, or split blocks across a `procedures_call` boundary in a way that changes whether a block is inside a warp custom block. Warp changes when the VM yields; moving `ask and wait` / `wait` / `broadcast and wait` into or out of a warp proc changes observable timing (patch12, lines 3937–3975; `--no-turbo`, line 5297). Interactive procs are never inlined, hoisted across, or re-warped.
- **`ask and wait` / `answer` ordering.** The console loop is `forever { ask(">") → log echo → Action(answer) → ... }` (patch23, line 9733; single-main fast path, line 9785). Any pass that reorders a `sensing_answer` read relative to an `ask` changes the returned value. `answer`, `ask`, and the Action call are hard ordering barriers.
- **Key sensing / keyboard events.** Patch24 keyboard hats and `_SBG_KEY_ALIASES` (lines 9996+) are input-timing-sensitive; never hoist/fold anything derived from key state.
- **Timing / delta time.** `timer`, `resetTimer`, `dt`, `fps`, `frame`, `timeSeconds` (lines 4716–4725, 5163–5199) are all impure; never fold or hoist.
- **Terminal list identity.** `Terminal` (name + fixed monitor id `,(0/{jAb*2vBd56rlG@1`) is observable and must survive dead-list elimination and fusion untouched.
- **Broadcast message ids.** Broadcast name → id mapping must stay identical across levels so sprite code and Stage code agree (shared `broadcasts` dict, lines 3138–3140, 3382–3385).

**Primary risk to the `-O0` contract:** the `build_ir → lower_ir` round-trip diverging from `Compiler.compile` in field order, temp-variable numbering (`__sbg_tmp_*`, `__sbg_repeat_i_*`), or list-init order. Mitigation: the `-O0` short-circuit in `compile_project` (Section 5) keeps the IR out of the `-O0` path entirely, so `-O0` cannot regress; and the byte-identity gate (Section 6.2) catches any future attempt to route `-O0` through the IR.

**Secondary risks and mitigations:**

- **`__flat_struct_push` Scratch gap** (line 8810): `-O3`'s `flat-list-fusion` lowers it to field appends. Until that pass is implemented, `-O1`/`-O2` must not touch programs using it (the native runner already supports it, lines 9367–9478).
- **Struct copy-construction liveness** (Section 4.5): a struct-to-struct assignment reads all fields without explicit `IRVar` reads; `collect-info` must model this or `sroa-struct-scalars` will delete live fields. Verification: add a `y = x; use(y.f)` test before enabling the pass.
- **Inlining budget growth**: guarded by the never-increase-block rule (Section 4.7).
- **`vector-scalar-promotion` bound**: out-of-bounds `push_back` on a promoted vector would be a runtime semantic change; the pass requires provable read-only, fixed-length access, else it must skip (default `len <= 4`, verified by an explicit bounds analysis).

**Non-goals (explicitly out of scope):** SSA construction, whole-program value numbering, cross-target (sprite-to-stage) constant propagation, register allocation, anything that changes `monitors` or the Terminal monitor id, and any optimization that relies on TurboWarp-specific extensions — the output must remain vanilla Scratch 3.
