from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple, Union

from . import globals as _g
from .errors import CompileError, attach_location
from .ast import (
    Token, Lexer, Program, ImportDecl, VarDecl, ListDecl, EventDecl, ProcDecl,
    BlockStmt, IfStmt, RepeatStmt, ForeverStmt, WhileStmt, ForStmt,
    ReturnStmt, BreakStmt, ContinueStmt, AssignStmt, ExprStmt,
    Literal, VarExpr, BinaryExpr, UnaryExpr, CallExpr, ArrayExpr,
)
from .scratch import ScratchBuilder
from .globals import ACTION_PROC_NAME, BACKDROP_SVG, CIN_BUFFER_LIST_NAME, TERMINAL_LIST_ID, TERMINAL_LIST_NAME

class Compiler:
    def __init__(self, program: Program, *, allow_library: bool = False):
        self.program = program
        self.allow_library = allow_library
        self.b = ScratchBuilder()
        # Use the uploaded Empty Console.sb3 layout: a visible fullscreen list monitor
        # named "Terminal" plus a green-flag prompt loop that calls Action(Input).
        self.b.lists[TERMINAL_LIST_NAME] = TERMINAL_LIST_ID
        self.init_values: Dict[str, Any] = {}
        self.init_lists: Dict[str, List[Any]] = {TERMINAL_LIST_NAME: []}
        self.procs: Dict[str, ProcDecl] = {}
        self.message_events: List[EventDecl] = []
        self.action_entries: List[Tuple[str, List[Any]]] = []
        self.action_param_names: set[str] = {"Input"}
        self.action_argid: Optional[str] = None
        self.const_variables: set[str] = set()  # CRITICAL FIX: pkt 9 - track const variables

    def compile_error(self, message: str, node: Any = None) -> CompileError:
        err = CompileError(message)
        if node is not None:
            attach_location(err, node)
        return err

    @staticmethod
    def has_duplicate(values: List[str]) -> Optional[str]:
        seen: set[str] = set()
        for value in values:
            if value in seen:
                return value
            seen.add(value)
        return None

    def analyze(self) -> None:
        """Collect globals, procedures, events and Scratch ids before block emission."""
        loose: List[Any] = []
        flag_entries: List[Tuple[str, List[Any]]] = []
        self._sbg_main_candidate: Optional[List[Any]] = None
        self._sbg_saw_non_main_entry = False

        globals_seen: Dict[str, Any] = {}

        # First pass: decide what becomes the console Action(Input) body and fail
        # on source patterns that previously produced confusing/blank-looking sb3s.
        for stmt in self.program.body:
            if isinstance(stmt, VarDecl):
                if stmt.name == TERMINAL_LIST_NAME:
                    raise self.compile_error(f"{TERMINAL_LIST_NAME!r} is reserved for the fullscreen terminal list", stmt)
                if stmt.name in globals_seen:
                    raise self.compile_error(f"duplicate global name {stmt.name!r}", stmt)
                globals_seen[stmt.name] = stmt
                # CRITICAL FIX: pkt 9 - track const variables (mutable=False)
                if not stmt.mutable:
                    self.const_variables.add(stmt.name)
                self.b.var_id(stmt.name)
                self.init_values[stmt.name] = self.literal_value_or_zero(stmt.expr)
            elif isinstance(stmt, ListDecl):
                if stmt.name == TERMINAL_LIST_NAME:
                    raise self.compile_error(f"{TERMINAL_LIST_NAME!r} is reserved; use log(...) / clearTerminal() instead of redeclaring it", stmt)
                if stmt.name in globals_seen:
                    raise self.compile_error(f"duplicate global name {stmt.name!r}", stmt)
                globals_seen[stmt.name] = stmt
                self.b.list_id(stmt.name)
                self.init_lists[stmt.name] = [self.literal_value_or_zero(x) for x in stmt.items]
            elif isinstance(stmt, ProcDecl):
                dup_param = self.has_duplicate(stmt.params)
                if dup_param:
                    raise self.compile_error(f"duplicate parameter {dup_param!r} in {stmt.name}()", stmt)
                if stmt.name == ACTION_PROC_NAME:
                    if len(stmt.params) > 1:
                        raise self.compile_error("Action() may have at most one parameter because the console passes one input string", stmt)
                    param = stmt.params[0] if stmt.params else "Input"
                    self.action_entries.append((param, stmt.body))
                    self.action_param_names.add(param)
                    self._sbg_saw_non_main_entry = True
                else:
                    if stmt.name in self.procs:
                        raise self.compile_error(f"duplicate procedure {stmt.name}()", stmt)
                    self.procs[stmt.name] = stmt
            elif isinstance(stmt, EventDecl):
                if stmt.kind == "action":
                    param = stmt.value or "Input"
                    self.action_entries.append((param, stmt.body))
                    self.action_param_names.add(param)
                    if getattr(stmt, "sbg_is_cpp_main", False) and self._sbg_main_candidate is None:
                        self._sbg_main_candidate = stmt.body
                    else:
                        self._sbg_saw_non_main_entry = True
                elif stmt.kind == "flag":
                    # In console projects the real green-flag script is the terminal prompt loop.
                    # Old SBG code using `on flag { ... }` is treated as Action(Input) body.
                    flag_entries.append(("Input", stmt.body))
                    self._sbg_saw_non_main_entry = True
                elif stmt.kind == "message":
                    if stmt.value is not None:
                        self.b.broadcast_id(stmt.value)
                    self.message_events.append(stmt)
            else:
                loose.append(stmt)

        had_explicit_entrypoint = bool(self.action_entries) or bool(flag_entries)

        if not self.action_entries:
            self.action_entries.extend(flag_entries)
        if loose:
            if not had_explicit_entrypoint:
                first_loose = loose[0]
                raise self.compile_error(
                    "no entry point found: top-level statements must be wrapped in "
                    "int main() { ... }, on action(input) { ... }, on flag { ... }, "
                    "or proc Action(input) { ... }. (This usually means main()'s "
                    "braces are missing or were commented out.)",
                    first_loose,
                )
            self.action_entries.append(("Input", loose))
            self._sbg_saw_non_main_entry = True

        self.single_cpp_main_body: Optional[List[Any]] = (
            self._sbg_main_candidate
            if (self._sbg_main_candidate is not None
                and not self._sbg_saw_non_main_entry
                and len(self.action_entries) == 1)
            else None
        )

        if not self.allow_library and not self.action_entries:
            first = self.program.body[0] if self.program.body else None
            raise self.compile_error(
                "nothing runnable was compiled: this file looks like a library. "
                "Add `on action(input) { ... }`, add loose top-level statements, "
                "or compile a main.sbg that imports this file. Use `--allow-library` only when you intentionally want a library-only sb3.",
                first,
            )

        def walk_stmt(stmt: Any, local_params: Optional[set[str]] = None) -> None:
            local_params = local_params or set()
            if isinstance(stmt, VarDecl):
                self.b.var_id(stmt.name)
                walk_expr(stmt.expr, local_params)
            elif isinstance(stmt, ListDecl):
                self.b.list_id(stmt.name)
                for x in stmt.items:
                    walk_expr(x, local_params)
            elif isinstance(stmt, AssignStmt):
                if stmt.name not in local_params and stmt.name not in self.action_param_names:
                    self.b.var_id(stmt.name)
                walk_expr(stmt.expr, local_params)
            elif isinstance(stmt, ProcDecl):
                proc_params = set(stmt.params)
                for s in stmt.body:
                    walk_stmt(s, proc_params)
            elif isinstance(stmt, EventDecl):
                event_params = set()
                if stmt.kind == "action":
                    event_params.add(stmt.value or "Input")
                for s in stmt.body:
                    walk_stmt(s, event_params)
            elif isinstance(stmt, IfStmt):
                walk_expr(stmt.cond, local_params)
                for s in stmt.then_body:
                    walk_stmt(s, local_params)
                for s in (stmt.else_body or []):
                    walk_stmt(s, local_params)
            elif isinstance(stmt, RepeatStmt):
                walk_expr(stmt.count, local_params)
                for s in stmt.body:
                    walk_stmt(s, local_params)
            elif isinstance(stmt, ForeverStmt):
                for s in stmt.body:
                    walk_stmt(s, local_params)
            elif isinstance(stmt, WhileStmt):
                walk_expr(stmt.cond, local_params)
                for s in stmt.body:
                    walk_stmt(s, local_params)
            elif isinstance(stmt, ForStmt):
                if stmt.init:
                    walk_stmt(stmt.init, local_params)
                if stmt.cond:
                    walk_expr(stmt.cond, local_params)
                if stmt.update:
                    walk_stmt(stmt.update, local_params)
                for s in stmt.body:
                    walk_stmt(s, local_params)
            elif isinstance(stmt, ExprStmt):
                walk_expr(stmt.expr, local_params)
            elif isinstance(stmt, ReturnStmt) and stmt.expr:
                walk_expr(stmt.expr, local_params)

        def walk_expr(expr: Any, local_params: Optional[set[str]] = None) -> None:
            local_params = local_params or set()
            if isinstance(expr, VarExpr):
                # Procedure/action arguments are Scratch argument reporters, not Stage variables.
                if expr.name in local_params or expr.name in self.action_param_names:
                    return
                if expr.name not in self.b.lists:
                    self.b.var_id(expr.name)
            elif isinstance(expr, BinaryExpr):
                walk_expr(expr.left, local_params)
                walk_expr(expr.right, local_params)
            elif isinstance(expr, UnaryExpr):
                walk_expr(expr.expr, local_params)
            elif isinstance(expr, CallExpr):
                if expr.callee in ("broadcast", "broadcastAndWait") and expr.args and isinstance(expr.args[0], Literal):
                    self.b.broadcast_id(str(expr.args[0].value))
                if expr.args and isinstance(expr.args[0], VarExpr):
                    first_name = expr.args[0].name
                    if first_name in local_params:
                        # Do not promote procedure parameters to lists. Scratch cannot
                        # pass list references through custom-block arguments; the real
                        # diagnostic is emitted during block generation.
                        pass
                    elif expr.callee in ("push", "insert", "delete", "replace", "contains", "item"):
                        self.b.list_id(first_name)
                        self.b.variables.pop(first_name, None)
                    elif expr.callee == "len" and first_name in self.b.lists:
                        self.b.variables.pop(first_name, None)
                if expr.callee == "log":
                    self.b.list_id(TERMINAL_LIST_NAME)
                for a in expr.args:
                    walk_expr(a, local_params)
            elif isinstance(expr, ArrayExpr):
                for x in expr.items:
                    walk_expr(x, local_params)

        for proc in self.procs.values():
            walk_stmt(proc)
        for ev in self.message_events:
            walk_stmt(ev)
        for param, body in self.action_entries:
            for stmt in body:
                walk_stmt(stmt, {param})

        signatures: Dict[str, Tuple[str, List[str]]] = {}
        for name, proc in self.procs.items():
            argids = [self.b.uid("arg") for _ in proc.params]
            proccode = name + (" " + " ".join(["%s" for _ in proc.params]) if proc.params else "")
            signatures[name] = (proccode, argids)
        for proc in self.procs.values():
            for param in proc.params:
                if param not in self.init_values:
                    self.b.variables.pop(param, None)
        for param in self.action_param_names:
            if param not in self.init_values:
                self.b.variables.pop(param, None)
        self.b.proc_signatures = signatures  # type: ignore[attr-defined]

    def literal_value_or_zero(self, expr: Any) -> Any:
        if isinstance(expr, Literal):
            return expr.value
        if isinstance(expr, ArrayExpr):
            return [self.literal_value_or_zero(x) for x in expr.items]
        return 0

    def compile(self) -> Dict[str, Any]:
        self.analyze()
        # CRITICAL FIX: pkt 9 - pass const_variables to ScratchBuilder
        self.b.const_variables = self.const_variables
        self.action_argid = self.b.uid("arg")

        # User procedures first so Action(Input) and message handlers can call them.
        for proc in self.procs.values():
            self.b.compile_proc_definition(proc)

        self.compile_console_flag_loop()
        self.compile_console_action_definition()
        for ev in self.message_events:
            self.compile_message_event(ev)

        asset_id = hashlib.md5(BACKDROP_SVG.encode("utf-8")).hexdigest()
        costume = {
            "name": "backdrop1",
            "dataFormat": "svg",
            "assetId": asset_id,
            "md5ext": asset_id + ".svg",
            "rotationCenterX": 240,
            "rotationCenterY": 180,
        }
        variables_obj = {vid: [name, self.init_values.get(name, 0)] for name, vid in self.b.variables.items()}
        lists_obj = {lid: [name, self.init_lists.get(name, [])] for name, lid in self.b.lists.items()}
        broadcasts_obj = {bid: name for name, bid in self.b.broadcasts.items()}
        return {
            "targets": [
                {
                    "isStage": True,
                    "name": "Stage",
                    "variables": variables_obj,
                    "lists": lists_obj,
                    "broadcasts": broadcasts_obj,
                    "blocks": self.b.blocks,
                    "comments": {},
                    "currentCostume": 0,
                    "costumes": [costume],
                    "sounds": [],
                    "volume": 100,
                    "layerOrder": 0,
                    "tempo": 60,
                    "videoTransparency": 50,
                    "videoState": "on",
                    "textToSpeechLanguage": None,
                }
            ],
            "monitors": [
                {
                    "id": TERMINAL_LIST_ID,
                    "mode": "list",
                    "opcode": "data_listcontents",
                    "params": {"LIST": TERMINAL_LIST_NAME},
                    "spriteName": None,
                    "value": self.init_lists.get(TERMINAL_LIST_NAME, []),
                    "width": 479,
                    "height": 307,
                    "x": 0,
                    "y": 0,
                    "visible": True,
                }
            ],
            "extensions": [],
            "meta": {
                "semver": "3.0.0",
                "vm": "14.0.0",
                "agent": f"StageBG/SBG {_g.VERSION}",
            },
        }

    def _compile_cin_buffer_clear(self) -> Optional[str]:
        """Clear the std/io.sbg leftover token buffer at green flag.

        Scratch does not reset lists between runs, so without this the tokens
        left over from a previous run's `cin >>` (the C++ stdin buffer) would
        be consumed by the next run. Emitted only when the program actually
        declares the buffer (i.e. uses std input).
        """
        if CIN_BUFFER_LIST_NAME not in self.b.lists:
            return None
        return self.b.add_block(
            "data_deletealloflist",
            fields={"LIST": [CIN_BUFFER_LIST_NAME, self.b.list_id(CIN_BUFFER_LIST_NAME)]},
        )

    def compile_console_flag_loop(self) -> None:
        assert self.action_argid is not None
        hat = self.b.add_block("event_whenflagclicked", topLevel=True, x=536, y=455)
        clear = self._compile_cin_buffer_clear()
        forever = self.b.add_block("control_forever", parent=hat, inputs={})
        ask = self.b.add_block("sensing_askandwait", parent=forever, inputs={
            "QUESTION": [1, [10, ""]]
        })
        call = self.b.add_block("procedures_call", parent=ask, inputs={}, mutation={
            "tagName": "mutation",
            "children": [],
            "proccode": "Action %s",
            "argumentids": json.dumps([self.action_argid]),
            "warp": "true",
        })
        answer = self.b.add_block("sensing_answer", parent=call)
        self.b.blocks[hat]["next"] = clear or forever
        if clear:
            self.b.blocks[clear]["parent"] = hat
            self.b.blocks[clear]["next"] = forever
            self.b.blocks[forever]["parent"] = clear
        self.b.blocks[forever]["inputs"]["SUBSTACK"] = [2, ask]
        self.b.blocks[ask]["next"] = call
        self.b.blocks[call]["inputs"][self.action_argid] = [3, answer, [10, ""]]

    def compile_console_action_definition(self) -> None:
        assert self.action_argid is not None
        display_param = self.action_entries[0][0] if self.action_entries else "Input"
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
        for param, body in self.action_entries:
            self.b.current_proc_params = {param: self.action_argid}
            part = self.b.compile_statement_chain(body)
            first = self.b.chain(first, part)
        self.b.current_proc_params = saved_params
        self.b.blocks[def_id]["next"] = first
        if first:
            self.b.blocks[first]["parent"] = def_id

    def compile_message_event(self, ev: EventDecl) -> None:
        msg = ev.value or ""
        hat = self.b.add_block("event_whenbroadcastreceived", topLevel=True,
                               fields={"BROADCAST_OPTION": [msg, self.b.broadcast_id(msg)]})
        first = self.b.compile_statement_chain(ev.body)
        self.b.blocks[hat]["next"] = first
        if first:
            self.b.blocks[first]["parent"] = hat

def _input_block_refs(value: Any) -> Iterable[str]:
    """Yield block ids referenced from a Scratch input value."""
    if isinstance(value, list) and value:
        # Scratch input shapes are usually [1, primitive-or-block], [2, block],
        # or [3, block, primitive-shadow]. Primitive shadows like [10, "text"]
        # are nested lists, not ids.
        if isinstance(value[0], int):
            for item in value[1:]:
                if isinstance(item, str):
                    yield item
                elif isinstance(item, list) and item and isinstance(item[0], int) and item[0] in (1, 2, 3):
                    yield from _input_block_refs(item)
    elif isinstance(value, dict):
        for sub in value.values():
            yield from _input_block_refs(sub)

def validate_scratch_project(project: Dict[str, Any]) -> None:
    """Catch invalid/blank-looking .sb3 output before the user opens Scratch."""
    targets = project.get("targets")
    if not isinstance(targets, list) or not targets:
        raise CompileError("generated Scratch project has no targets")
    stage_targets = [t for t in targets if isinstance(t, dict) and t.get("isStage")]
    if len(stage_targets) != 1:
        raise CompileError("generated Scratch project must contain exactly one Stage target")
    stage = stage_targets[0]
    blocks = stage.get("blocks")
    if not isinstance(blocks, dict) or not blocks:
        raise CompileError("generated Scratch project contains no blocks; refusing to write an empty sb3")

    opcodes = [b.get("opcode") for b in blocks.values() if isinstance(b, dict)]
    if "event_whenflagclicked" not in opcodes:
        raise CompileError("generated Scratch project has no green-flag entrypoint")
    if "procedures_definition" not in opcodes:
        raise CompileError("generated Scratch project has no Action(Input) procedure")

    action_found = False
    for block in blocks.values():
        if not isinstance(block, dict):
            continue
        if block.get("opcode") == "procedures_prototype":
            mutation = block.get("mutation") or {}
            if mutation.get("proccode") == "Action %s":
                action_found = True
                break
    if not action_found:
        raise CompileError("generated Scratch project is missing the Action(Input) prototype")

    if TERMINAL_LIST_ID not in stage.get("lists", {}):
        raise CompileError("generated Scratch project is missing the Terminal list")
    monitor_ok = False
    for monitor in project.get("monitors", []):
        if isinstance(monitor, dict) and monitor.get("id") == TERMINAL_LIST_ID and monitor.get("visible") is True:
            monitor_ok = True
            break
    if not monitor_ok:
        raise CompileError("generated Scratch project is missing the visible fullscreen Terminal monitor")

    for bid, block in blocks.items():
        if not isinstance(block, dict):
            raise CompileError(f"generated Scratch block {bid!r} is not an object")
        nxt = block.get("next")
        if nxt is not None and nxt not in blocks:
            raise CompileError(f"generated Scratch block {bid!r} points to missing next block {nxt!r}")
        parent = block.get("parent")
        if parent is not None and parent not in blocks:
            raise CompileError(f"generated Scratch block {bid!r} points to missing parent block {parent!r}")
        for ref in _input_block_refs(block.get("inputs", {})):
            if ref not in blocks:
                raise CompileError(f"generated Scratch block {bid!r} has an input pointing to missing block {ref!r}")

def verify_sb3_file(path: Union[str, Path]) -> None:
    path = Path(path)
    try:
        with zipfile.ZipFile(path) as z:
            if "project.json" not in z.namelist():
                raise CompileError("written sb3 has no project.json")
            project = json.loads(z.read("project.json"))
    except zipfile.BadZipFile as e:
        raise CompileError(f"written sb3 is not a valid zip file: {e}") from e
    except json.JSONDecodeError as e:
        raise CompileError(f"written project.json is invalid JSON: {e}") from e
    _g.validate_scratch_project(project)

def _collect_strings(node: Any, acc: set) -> None:
    """Collect every string literal anywhere inside a nested JSON structure."""
    if isinstance(node, str):
        acc.add(node)
    elif isinstance(node, dict):
        for value in node.values():
            _collect_strings(value, acc)
    elif isinstance(node, list):
        for item in node:
            _collect_strings(item, acc)


def prune_dead_variables_lists(project: Dict[str, Any]) -> Dict[str, Any]:
    """Remove declared variables/lists that no block, monitor, or name-string uses.

    Scratch references a variable/list either by ID (the ``fields.VARIABLE`` /
    ``fields.LIST`` value ``[name, id]`` emitted by every ``data_*`` block) or, in
    a few runtime/monitor cases, by NAME (e.g. the Terminal monitor's
    ``params.LIST``). Lowering declares many ``__sbg_*`` intermediates (struct /
    vector field tables, row metadata, return slots, temp scalars) that never end
    up referenced by any emitted block, so this final pass drops them.

    Safety rule: an entry survives if its ID OR its NAME appears as a string
    anywhere in any target's blocks or in the top-level monitors. Matching by
    NAME as well is deliberately conservative: variable/list ids are only unique
    per target (each target reuses ``var00001``...), and some helpers reference
    things by name string, so a name hit must never be allowed to drop a live
    entry. The Terminal console list is pinned explicitly regardless.
    """
    declared_var_ids: set = set()
    declared_list_ids: set = set()
    declared_var_names: set = set()
    declared_list_names: set = set()

    for target in project.get("targets", []):
        if not isinstance(target, dict):
            continue
        for vid, pair in (target.get("variables") or {}).items():
            declared_var_ids.add(vid)
            if isinstance(pair, list) and pair and isinstance(pair[0], str):
                declared_var_names.add(pair[0])
        for lid, pair in (target.get("lists") or {}).items():
            declared_list_ids.add(lid)
            if isinstance(pair, list) and pair and isinstance(pair[0], str):
                declared_list_names.add(pair[0])

    referenced: set = set()
    for target in project.get("targets", []):
        if isinstance(target, dict):
            _collect_strings(target.get("blocks"), referenced)
    _collect_strings(project.get("monitors"), referenced)

    live_var_ids = declared_var_ids & referenced
    live_list_ids = declared_list_ids & referenced
    live_var_names = declared_var_names & referenced
    live_list_names = declared_list_names & referenced

    # The terminal list is the console output; its monitor references it and
    # runtime logging appends to it. Never prune it.
    live_list_ids.add(TERMINAL_LIST_ID)
    live_list_names.add(TERMINAL_LIST_NAME)

    for target in project.get("targets", []):
        if not isinstance(target, dict):
            continue
        if isinstance(target.get("variables"), dict):
            target["variables"] = {
                vid: pair
                for vid, pair in target["variables"].items()
                if vid in live_var_ids
                or (isinstance(pair, list) and pair and isinstance(pair[0], str) and pair[0] in live_var_names)
            }
        if isinstance(target.get("lists"), dict):
            target["lists"] = {
                lid: pair
                for lid, pair in target["lists"].items()
                if lid in live_list_ids
                or (isinstance(pair, list) and pair and isinstance(pair[0], str) and pair[0] in live_list_names)
            }
    return project


def write_sb3_project(project: Dict[str, Any], output_path: Union[str, Path], *, verify: bool = True) -> None:
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    prune_dead_variables_lists(project)
    if verify:
        _g.validate_scratch_project(project)
    asset_id = hashlib.md5(BACKDROP_SVG.encode("utf-8")).hexdigest()
    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("project.json", json.dumps(project, ensure_ascii=False, separators=(",", ":")))
        z.writestr(asset_id + ".svg", BACKDROP_SVG)
    if verify:
        try:
            verify_sb3_file(output_path)
        except Exception:
            # Do not leave a broken file that Scratch later opens as blank.
            try:
                output_path.unlink()
            except OSError:
                pass
            raise

def compile_to_sb3(source_path: Union[str, Path], output_path: Union[str, Path], *, allow_library: bool = False, verify: bool = True) -> None:
    source_path = Path(source_path)
    program = _g.parse_source(source_path.read_text(encoding="utf-8"), str(source_path))
    project = Compiler(program, allow_library=allow_library).compile()
    write_sb3_project(project, output_path, verify=verify)

# =============================================================================
# Tools
# =============================================================================

def inspect_sb3(path: Union[str, Path]) -> Dict[str, Any]:
    path = Path(path)
    with zipfile.ZipFile(path) as z:
        project = json.loads(z.read("project.json"))
    opcodes: Dict[str, int] = {}
    for target in project.get("targets", []):
        for block in target.get("blocks", {}).values():
            if isinstance(block, dict):
                op = block.get("opcode")
                if op:
                    opcodes[op] = opcodes.get(op, 0) + 1
    return {
        "targets": [t.get("name") for t in project.get("targets", [])],
        "stage_only": all(t.get("isStage") for t in project.get("targets", [])),
        "variables": sum(len(t.get("variables", {})) for t in project.get("targets", [])),
        "lists": sum(len(t.get("lists", {})) for t in project.get("targets", [])),
        "blocks": sum(len(t.get("blocks", {})) for t in project.get("targets", [])),
        "opcodes": dict(sorted(opcodes.items(), key=lambda kv: (-kv[1], kv[0]))),
    }

def unpack_sb3(path: Union[str, Path], out_dir: Union[str, Path]) -> None:
    out = Path(out_dir)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path) as z:
        z.extractall(out)

_g.validate_scratch_project = validate_scratch_project
