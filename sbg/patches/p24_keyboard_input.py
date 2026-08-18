# =============================================================================
# Patch 24: keyboard input/events for vanilla Scratch
# =============================================================================

VERSION = "0.9.0-patch24-keyboard"

# Vanilla Scratch key names accepted by the key hat/sensing menu. Single letters,
# digits and ordinary printable characters are also passed through unchanged.
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

_prev_parse_event_patch24 = Parser.parse_event

def _parse_event_patch24(self: Parser, start_token: Token) -> EventDecl:
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
    return _prev_parse_event_patch24(self, start_token)

Parser.parse_event = _parse_event_patch24  # type: ignore[method-assign]

# `keyboard.pressed("space")`, `keys.down("left")`, etc. lower to the existing
# vanilla sensing reporter `keyPressed("...")`.
_prev_sbg_method_lower_patch24 = _sbg_method_lower_patch19

def _sbg_method_lower_patch19(receiver: Any, method: str, args: List[Any], parser: Optional[Parser] = None) -> CallExpr:  # type: ignore[no-redef]
    rname = receiver.name if isinstance(receiver, VarExpr) else ""
    if rname in {"keyboard", "keys", "key"}:
        if method in {"pressed", "down", "isPressed", "is_down", "isDown"}:
            return _sbg_call_patch19("keyPressed", args, receiver)
    return _prev_sbg_method_lower_patch24(receiver, method, args, parser)

Parser.parse_postfix = _parser_parse_postfix_patch20  # type: ignore[method-assign]

# Add event collection/compilation to the Stage compiler without rewriting all older patches.
_prev_compiler_init_patch24 = Compiler.__init__

def _compiler_init_patch24(self: Compiler, *args: Any, **kwargs: Any) -> None:
    _prev_compiler_init_patch24(self, *args, **kwargs)
    self.key_events: List[EventDecl] = []

Compiler.__init__ = _compiler_init_patch24  # type: ignore[method-assign]

_prev_compiler_analyze_patch24 = Compiler.analyze

def _compiler_analyze_patch24(self: Compiler) -> None:
    # Older analyze() ignores `on key`; temporarily remove key events so it does
    # not treat a keyboard-only project as a blank library, then add them back.
    key_events = [stmt for stmt in self.program.body if isinstance(stmt, EventDecl) and stmt.kind == "key"]
    if key_events:
        filtered = Program([stmt for stmt in self.program.body if not (isinstance(stmt, EventDecl) and stmt.kind == "key")])
        old_program = self.program
        old_allow = self.allow_library
        self.program = filtered
        self.allow_library = True
        try:
            _prev_compiler_analyze_patch24(self)
        finally:
            self.program = old_program
            self.allow_library = old_allow
        self.key_events = key_events
        # Walk key event bodies once, so variables/lists/procedure calls used only
        # from a key handler still get ids and return helpers.
        for ev in key_events:
            for stmt in ev.body:
                try:
                    self.b.compile_stmt(stmt)  # preflight catches obvious unsupported code
                except Exception:
                    # Do not keep preflight blocks; actual compile below emits real blocks.
                    self.b.blocks.clear()
                    raise
                self.b.blocks.clear()
        return
    _prev_compiler_analyze_patch24(self)

Compiler.analyze = _compiler_analyze_patch24  # type: ignore[method-assign]


def _compile_key_event_patch24(builder: ScratchBuilder, ev: EventDecl, *, x: int = 80, y: int = 700) -> None:
    key = _sbg_normalize_key_name_patch24(ev.value or "any")
    hat = builder.add_block("event_whenkeypressed", topLevel=True, x=x, y=y, fields={"KEY_OPTION": [key, None]})
    first = builder.compile_statement_chain(ev.body)
    builder.blocks[hat]["next"] = first
    if first:
        builder.blocks[first]["parent"] = hat

_prev_compiler_compile_patch24 = Compiler.compile

def _compiler_compile_patch24(self: Compiler) -> Dict[str, Any]:
    project = _prev_compiler_compile_patch24(self)
    # If the active compile path was the normal Stage compiler, key events are in self.
    if getattr(self, "key_events", None):
        for idx, ev in enumerate(self.key_events):
            _compile_key_event_patch24(self.b, ev, x=80, y=700 + idx * 260)
        # Re-export mutated Stage blocks after adding key hats.
        if project.get("targets"):
            project["targets"][0]["blocks"] = self.b.blocks
    project.setdefault("meta", {})["agent"] = f"StageBG/SBG {VERSION}"
    project.setdefault("meta", {})["stagebgPatch24"] = "Keyboard support: on key, keyPressed(), keyboard.pressed()."
    return project

Compiler.compile = _compiler_compile_patch24  # type: ignore[method-assign]

# Sprite-local key hats.
_prev_sprite_init_patch24 = SpriteTargetCompiler.__init__

def _sprite_init_patch24(self: SpriteTargetCompiler, *args: Any, **kwargs: Any) -> None:
    _prev_sprite_init_patch24(self, *args, **kwargs)
    self.key_events: List[EventDecl] = []

SpriteTargetCompiler.__init__ = _sprite_init_patch24  # type: ignore[method-assign]

_prev_sprite_analyze_patch24 = SpriteTargetCompiler.analyze

def _sprite_analyze_patch24(self: SpriteTargetCompiler) -> None:
    key_events = [stmt for stmt in self.body if isinstance(stmt, EventDecl) and stmt.kind == "key"]
    if key_events:
        old_body = self.body
        self.body = [stmt for stmt in self.body if not (isinstance(stmt, EventDecl) and stmt.kind == "key")]
        try:
            _prev_sprite_analyze_patch24(self)
        finally:
            self.body = old_body
        self.key_events = key_events
        return
    _prev_sprite_analyze_patch24(self)

SpriteTargetCompiler.analyze = _sprite_analyze_patch24  # type: ignore[method-assign]

_prev_sprite_compile_target_patch24 = SpriteTargetCompiler.compile_target

def _sprite_compile_target_patch24(self: SpriteTargetCompiler) -> Dict[str, Any]:
    target = _prev_sprite_compile_target_patch24(self)
    if getattr(self, "key_events", None):
        for idx, ev in enumerate(self.key_events):
            _compile_key_event_patch24(self.b, ev, x=80, y=700 + idx * 260)
        target["blocks"] = self.b.blocks
    return target

SpriteTargetCompiler.compile_target = _sprite_compile_target_patch24  # type: ignore[method-assign]

# Native runner is headless, but allow tests to simulate keys through an env var:
# SBG_KEYS="space,left arrow,a" python3 sbg_patch24.py run file.sbg --input go
_prev_runtime_call_patch24 = Runtime.call

def _runtime_call_patch24(self: Runtime, name: str, args: List[Any]) -> Any:
    if name == "keyPressed":
        key = _sbg_normalize_key_name_patch24(args[0] if args else "any")
        raw = os.environ.get("SBG_KEYS", "")
        pressed = {_sbg_normalize_key_name_patch24(x.strip()) for x in raw.split(",") if x.strip()}
        return bool(key == "any" and pressed) or key in pressed
    return _prev_runtime_call_patch24(self, name, args)

Runtime.call = _runtime_call_patch24  # type: ignore[method-assign]

_g.parse_source = parse_source
_g.validate_scratch_project = validate_scratch_project
_g.VERSION = VERSION
