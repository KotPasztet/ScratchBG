# =============================================================================
# Patch 13: professional stdlib surface, Pen extension, compile-time file embeds
# =============================================================================

# StageBG now treats Scratch as a VM target, not as a toy UI.  The compiler still
# emits vanilla Scratch 3.0 JSON only: no TurboWarp-only opcodes, no JS, no custom
# extensions except Scratch's official Pen extension.

EMBEDDED_FILE_LIST_NAMES = [
    "__sbg_file_names",
    "__sbg_file_texts",
    "__sbg_file_sizes",
    "__sbg_file_line_start",
    "__sbg_file_line_count",
    "__sbg_file_lines",
]
EMBEDDED_FILE_LIST_IDS = {
    "__sbg_file_names": "sbg_files_names_v1",
    "__sbg_file_texts": "sbg_files_texts_v1",
    "__sbg_file_sizes": "sbg_files_sizes_v1",
    "__sbg_file_line_start": "sbg_files_line_start_v1",
    "__sbg_file_line_count": "sbg_files_line_count_v1",
    "__sbg_file_lines": "sbg_files_lines_v1",
}

# Builtins added in patch13.  They are intentionally low-level VM bindings; the
# higher-level API lives in packages/std/*.sbg.
BUILTIN_EXPR_NAMES.update({
    "touchingColor", "colorTouchingColor",
})
BUILTIN_STMT_NAMES.update({
    # list/data/monitor control
    "clearList", "deleteAll", "showVariable", "hideVariable", "showList", "hideList",
    # clone/control aliases
    "stop", "createCloneOf", "setDragMode",
    # pen extension, vanilla Scratch official extension
    "penClear", "clearPen", "penEraseAll", "penDown", "penUp", "penStamp",
    "penSetColor", "penSetSize", "penChangeSize",
    "penSetParam", "penChangeParam", "penSetHue", "penChangeHue",
    "penSetSaturation", "penChangeSaturation", "penSetBrightness", "penChangeBrightness",
    "penSetTransparency", "penChangeTransparency",
})
BUILTIN_NAMES = BUILTIN_EXPR_NAMES | BUILTIN_STMT_NAMES

# Stable ids let sprite targets read Stage-embedded files through the same global
# list ids.  Without this, each sprite would generate empty sprite-local file lists.
_old_list_id_patch13 = ScratchBuilder.list_id

def _list_id_patch13(self: ScratchBuilder, name: str) -> str:
    if name in EMBEDDED_FILE_LIST_IDS:
        if name not in self.lists:
            self.lists[name] = EMBEDDED_FILE_LIST_IDS[name]
        return self.lists[name]
    return _old_list_id_patch13(self, name)

ScratchBuilder.list_id = _list_id_patch13  # type: ignore[method-assign]


def _sbg_hex_color(value: str) -> str:
    value = str(value).strip()
    if re.fullmatch(r"#[0-9A-Fa-f]{6}", value):
        return value.lower()
    if re.fullmatch(r"[0-9A-Fa-f]{6}", value):
        return "#" + value.lower()
    named = {
        "black": "#000000", "white": "#ffffff", "red": "#ff0000", "green": "#00ff00", "blue": "#0000ff",
        "yellow": "#ffff00", "cyan": "#00ffff", "magenta": "#ff00ff", "orange": "#ff8800",
        "purple": "#8844ff", "gray": "#808080", "grey": "#808080",
    }
    if value.lower() in named:
        return named[value.lower()]
    raise CompileError(f"invalid color {value!r}; use '#rrggbb' or a known color name")


def _sbg_color_input(self: ScratchBuilder, expr: Any, parent: str) -> Any:
    if isinstance(expr, Literal):
        return [1, [9, _sbg_hex_color(str(expr.value))]]
    # Scratch color sockets accept reporter blocks too, although the UI normally
    # shows a color picker shadow. This keeps dynamic colors possible.
    return self.expr_input(expr, parent)


def _sbg_var_or_list_name(expr: Any, what: str) -> str:
    if isinstance(expr, VarExpr):
        return expr.name
    if isinstance(expr, Literal):
        return str(expr.value)
    raise CompileError(f"{what} needs a variable/list name, e.g. {what}(score) or {what}(\"score\")")


def _sbg_pen_param(expr: Any) -> str:
    if not isinstance(expr, Literal):
        raise CompileError("pen color parameter must be a string literal")
    raw = str(expr.value).strip().lower()
    aliases = {
        "color": "color", "colour": "color", "hue": "color",
        "saturation": "saturation", "sat": "saturation",
        "brightness": "brightness", "bright": "brightness", "value": "brightness",
        "transparency": "transparency", "alpha": "transparency",
    }
    if raw not in aliases:
        raise CompileError("pen parameter must be color/hue, saturation, brightness or transparency")
    return aliases[raw]


_old_bool_expr_patch13 = ScratchBuilder.is_boolean_expr

def _bool_expr_patch13(self: ScratchBuilder, expr: Any) -> bool:
    return _old_bool_expr_patch13(self, expr) or (isinstance(expr, CallExpr) and expr.callee in {"touchingColor", "colorTouchingColor"})

ScratchBuilder.is_boolean_expr = _bool_expr_patch13  # type: ignore[method-assign]


_old_compile_call_expr_patch13 = ScratchBuilder.compile_call_expr

def _compile_call_expr_patch13(self: ScratchBuilder, expr: CallExpr, parent: Optional[str]) -> str:
    name, a = expr.callee, expr.args
    if name == "touchingColor":
        _sbg_require_sprite_target(self, name)
        self.need_args(name, a, 1)
        bid = self.add_block("sensing_touchingcolor", parent=parent, inputs={})
        self.blocks[bid]["inputs"]["COLOR"] = _sbg_color_input(self, a[0], bid)
        return bid
    if name == "colorTouchingColor":
        _sbg_require_sprite_target(self, name)
        self.need_args(name, a, 2)
        bid = self.add_block("sensing_coloristouchingcolor", parent=parent, inputs={})
        self.blocks[bid]["inputs"]["COLOR"] = _sbg_color_input(self, a[0], bid)
        self.blocks[bid]["inputs"]["COLOR2"] = _sbg_color_input(self, a[1], bid)
        return bid
    return _old_compile_call_expr_patch13(self, expr, parent)

ScratchBuilder.compile_call_expr = _compile_call_expr_patch13  # type: ignore[method-assign]


_old_compile_call_stmt_patch13 = ScratchBuilder.compile_call_stmt

def _compile_call_stmt_patch13(self: ScratchBuilder, expr: CallExpr) -> Optional[str]:
    name, a = expr.callee, expr.args

    if name in ("clearList", "deleteAll"):
        self.need_args(name, a, 1)
        lst = self.require_list_expr(a[0])
        return self.add_block("data_deletealloflist", fields={"LIST": [lst, self.list_id(lst)]})

    if name in ("showVariable", "hideVariable"):
        self.need_args(name, a, 1)
        var_name = _sbg_var_or_list_name(a[0], name)
        self.var_id(var_name)
        return self.add_block("data_showvariable" if name == "showVariable" else "data_hidevariable",
                              fields={"VARIABLE": [var_name, self.var_id(var_name)]})

    if name in ("showList", "hideList"):
        self.need_args(name, a, 1)
        list_name = _sbg_var_or_list_name(a[0], name)
        self.list_id(list_name)
        return self.add_block("data_showlist" if name == "showList" else "data_hidelist",
                              fields={"LIST": [list_name, self.list_id(list_name)]})

    if name == "stop":
        self.need_args(name, a, 1)
        mode = _sbg_literal_string(a[0], "stop mode").strip().lower()
        allowed = {
            "all": "all",
            "this": "this script",
            "this script": "this script",
            "other": "other scripts in sprite",
            "others": "other scripts in sprite",
            "other scripts": "other scripts in sprite",
            "other scripts in sprite": "other scripts in sprite",
        }
        if mode not in allowed:
            raise CompileError("stop() expects 'all', 'this script' or 'other scripts in sprite'")
        chosen = allowed[mode]
        # Scratch wants hasnext=false for all/this script, true for other scripts.
        return self.add_block("control_stop", fields={"STOP_OPTION": [chosen, None]}, mutation={
            "tagName": "mutation", "children": [], "hasnext": "true" if chosen == "other scripts in sprite" else "false"
        })

    if name == "createCloneOf":
        _sbg_require_sprite_target(self, name)
        self.need_args(name, a, 1)
        target = _sbg_literal_string(a[0], "createCloneOf target")
        bid = self.add_block("control_create_clone_of", inputs={})
        menu = self.add_block("control_create_clone_of_menu", parent=bid, shadow=True, fields={"CLONE_OPTION": [target, None]})
        self.blocks[bid]["inputs"]["CLONE_OPTION"] = [1, menu]
        return bid

    if name == "setDragMode":
        _sbg_require_sprite_target(self, name)
        self.need_args(name, a, 1)
        mode = _sbg_literal_string(a[0], "setDragMode mode").strip().lower()
        if mode in ("drag", "draggable", "true", "1"):
            mode = "draggable"
        elif mode in ("no", "not", "not draggable", "false", "0"):
            mode = "not draggable"
        else:
            raise CompileError("setDragMode() expects 'draggable' or 'not draggable'")
        return self.add_block("sensing_setdragmode", fields={"DRAG_MODE": [mode, None]})

    # Pen extension. pen_clear is VM-global; the actual drawing blocks are sprite-only.
    if name in ("penClear", "clearPen", "penEraseAll"):
        self.need_args(name, a, 0)
        return self.add_block("pen_clear")
    if name == "penDown":
        _sbg_require_sprite_target(self, name); self.need_args(name, a, 0); return self.add_block("pen_penDown")
    if name == "penUp":
        _sbg_require_sprite_target(self, name); self.need_args(name, a, 0); return self.add_block("pen_penUp")
    if name == "penStamp":
        _sbg_require_sprite_target(self, name); self.need_args(name, a, 0); return self.add_block("pen_stamp")
    if name == "penSetColor":
        _sbg_require_sprite_target(self, name); self.need_args(name, a, 1)
        bid = self.add_block("pen_setPenColorToColor", inputs={})
        self.blocks[bid]["inputs"]["COLOR"] = _sbg_color_input(self, a[0], bid)
        return bid
    if name == "penSetSize":
        _sbg_require_sprite_target(self, name); self.need_args(name, a, 1)
        bid = self.add_block("pen_setPenSizeTo", inputs={}); self.blocks[bid]["inputs"]["SIZE"] = self.expr_input(a[0], bid); return bid
    if name == "penChangeSize":
        _sbg_require_sprite_target(self, name); self.need_args(name, a, 1)
        bid = self.add_block("pen_changePenSizeBy", inputs={}); self.blocks[bid]["inputs"]["SIZE"] = self.expr_input(a[0], bid); return bid
    if name in ("penSetParam", "penChangeParam"):
        _sbg_require_sprite_target(self, name); self.need_args(name, a, 2)
        param = _sbg_pen_param(a[0])
        bid = self.add_block("pen_setPenColorParamTo" if name == "penSetParam" else "pen_changePenColorParamBy",
                             fields={"COLOR_PARAM": [param, None]}, inputs={})
        self.blocks[bid]["inputs"]["VALUE"] = self.expr_input(a[1], bid)
        return bid
    pen_aliases = {
        "penSetHue": ("penSetParam", "color"), "penChangeHue": ("penChangeParam", "color"),
        "penSetSaturation": ("penSetParam", "saturation"), "penChangeSaturation": ("penChangeParam", "saturation"),
        "penSetBrightness": ("penSetParam", "brightness"), "penChangeBrightness": ("penChangeParam", "brightness"),
        "penSetTransparency": ("penSetParam", "transparency"), "penChangeTransparency": ("penChangeParam", "transparency"),
    }
    if name in pen_aliases:
        _sbg_require_sprite_target(self, name); self.need_args(name, a, 1)
        base, param = pen_aliases[name]
        return _compile_call_stmt_patch13(self, CallExpr(base, [Literal(param), a[0]]))

    return _old_compile_call_stmt_patch13(self, expr)

ScratchBuilder.compile_call_stmt = _compile_call_stmt_patch13  # type: ignore[method-assign]


# Patch compiler expression walker to recognize new list builtins and file lists.
# This keeps `clearList(myList)` and file stdlib references as real lists, not variables.
_old_compiler_analyze_patch13 = Compiler.analyze

def _compiler_analyze_patch13(self: Compiler) -> None:
    _old_compiler_analyze_patch13(self)
    # Always make embedded file tables real Stage lists. This is necessary because
    # `len(__sbg_file_names)` must compile to data_lengthoflist even before any
    # `item(__sbg_file_names, i)` is encountered. Empty tables are cheap.
    for name in EMBEDDED_FILE_LIST_NAMES:
        self.b.list_id(name)
        self.init_lists.setdefault(name, [])

Compiler.analyze = _compiler_analyze_patch13  # type: ignore[method-assign]

_old_sprite_analyze_patch13 = SpriteTargetCompiler.analyze
def _sprite_analyze_patch13(self: SpriteTargetCompiler) -> None:
    _old_sprite_analyze_patch13(self)
    for name in EMBEDDED_FILE_LIST_NAMES:
        self.b.list_id(name)
        self.init_lists.setdefault(name, [])
SpriteTargetCompiler.analyze = _sprite_analyze_patch13  # type: ignore[method-assign]


_old_sprite_compile_target_patch13 = SpriteTargetCompiler.compile_target

def _sprite_compile_target_patch13(self: SpriteTargetCompiler) -> Dict[str, Any]:
    target = _old_sprite_compile_target_patch13(self)
    # Embedded file lists are Stage-global and share deterministic ids. Do not
    # create duplicate empty sprite-local lists with the same names.
    target["lists"] = {
        lid: pair for lid, pair in target.get("lists", {}).items()
        if pair and pair[0] not in EMBEDDED_FILE_LIST_NAMES
    }
    return target

SpriteTargetCompiler.compile_target = _sprite_compile_target_patch13  # type: ignore[method-assign]


_old_validate_patch13 = validate_scratch_project

def validate_scratch_project(project: Dict[str, Any]) -> None:  # type: ignore[no-redef]
    _old_validate_patch13(project)
    # Loading the official Pen extension is vanilla Scratch.  Ordinary sprite-local
    # variables/lists may coincidentally reuse ids across targets because Scratch
    # stores them per target. Only embedded file tables are required to be Stage-
    # global and must not appear as sprite-local duplicates.
    embedded_ids = set(EMBEDDED_FILE_LIST_IDS.values())
    stage_seen: set[str] = set()
    for target in project.get("targets", []):
        for lid, pair in (target.get("lists") or {}).items():
            if target.get("isStage") and lid in embedded_ids:
                stage_seen.add(lid)
            if (not target.get("isStage")) and lid in embedded_ids:
                raise CompileError(f"embedded file list {pair[0] if pair else lid!r} leaked into sprite-local lists")


def _project_ensure_patch13(project: Dict[str, Any]) -> Dict[str, Any]:
    # Always include Pen. It does not add files/assets; it only tells vanilla
    # Scratch to load the official extension when the project opens.
    exts = project.setdefault("extensions", [])
    if "pen" not in exts:
        exts.append("pen")
    project.setdefault("meta", {})["agent"] = f"StageBG/SBG {VERSION}"
    return project

_old_compiler_compile_patch13 = Compiler.compile

def _compiler_compile_patch13(self: Compiler) -> Dict[str, Any]:
    return _project_ensure_patch13(_old_compiler_compile_patch13(self))

Compiler.compile = _compiler_compile_patch13  # type: ignore[method-assign]


def _make_literal(value: Any) -> Literal:
    return Literal(value)


def _make_list_decl(name: str, values: List[Any]) -> ListDecl:
    return ListDecl(name, [_make_literal(v) for v in values])


def _parse_embed_ref(ref: str, base: Path) -> Tuple[Path, str]:
    # Accept either local=virtual or local:virtual.  local=virtual is safer for
    # paths that contain colons, but local:virtual is convenient on Linux.
    if "=" in ref:
        left, right = ref.split("=", 1)
        path = Path(left)
        virtual = right
    elif ":" in ref and not re.match(r"^[A-Za-z]:[\\/]", ref):
        left, right = ref.split(":", 1)
        path = Path(left)
        virtual = right
    else:
        path = Path(ref)
        virtual = path.name
    if not path.is_absolute():
        path = base / path
    virtual = virtual.strip().replace("\\", "/").lstrip("/")
    if not virtual:
        virtual = path.name
    return path.resolve(), virtual


def _collect_embedded_files(source_path: Union[str, Path], refs: Optional[List[str]] = None, dirs: Optional[List[str]] = None) -> List[Tuple[str, str]]:
    base = Path(source_path).resolve().parent if source_path else Path.cwd()
    files: List[Tuple[Path, str]] = []
    for ref in refs or []:
        p, name = _parse_embed_ref(ref, base)
        if not p.is_file():
            raise CompileError(f"embedded file does not exist or is not a file: {p}")
        files.append((p, name))
    for dref in dirs or []:
        dpath = Path(dref)
        if not dpath.is_absolute():
            dpath = base / dpath
        dpath = dpath.resolve()
        if not dpath.is_dir():
            raise CompileError(f"embedded directory does not exist: {dpath}")
        for p in sorted(x for x in dpath.rglob("*") if x.is_file()):
            rel = p.relative_to(dpath).as_posix()
            files.append((p, rel))

    seen: set[str] = set()
    result: List[Tuple[str, str]] = []
    for p, virtual in files:
        if virtual in seen:
            raise CompileError(f"duplicate embedded virtual file name {virtual!r}")
        seen.add(virtual)
        try:
            text = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            raise CompileError(f"cannot read embedded file {p}: {e}") from e
        # Scratch list items are strings. Keep \n in the text cell and also expose
        # a separate flat line table for efficient line access.
        result.append((virtual, text))
    return result


def _program_with_embedded_files(program: Program, source_path: Union[str, Path], *, embeds: Optional[List[str]] = None, embed_dirs: Optional[List[str]] = None) -> Program:
    embedded = _collect_embedded_files(source_path, embeds, embed_dirs)
    names: List[str] = []
    texts: List[str] = []
    sizes: List[int] = []
    starts: List[int] = []
    counts: List[int] = []
    all_lines: List[str] = []
    next_line = 1  # Scratch lists are 1-indexed
    for virtual, text in embedded:
        lines = text.splitlines()
        if text.endswith("\n"):
            # splitlines() intentionally drops the final blank line; keep line
            # based indexing readable by not adding a synthetic empty line.
            pass
        names.append(virtual)
        texts.append(text)
        sizes.append(len(text))
        starts.append(next_line)
        counts.append(len(lines))
        all_lines.extend(lines)
        next_line += len(lines)
    decls = [
        _make_list_decl("__sbg_file_names", names),
        _make_list_decl("__sbg_file_texts", texts),
        _make_list_decl("__sbg_file_sizes", sizes),
        _make_list_decl("__sbg_file_line_start", starts),
        _make_list_decl("__sbg_file_line_count", counts),
        _make_list_decl("__sbg_file_lines", all_lines),
    ]
    # Put file tables at the very front so libraries can use them immediately.
    return Program([*decls, *program.body])


_old_runtime_prepare_patch13 = Runtime.prepare_scratch_console

def _runtime_prepare_patch13(self: Runtime) -> None:
    _old_runtime_prepare_patch13(self)
    for name in EMBEDDED_FILE_LIST_NAMES:
        self.lists.setdefault(name, [])

Runtime.prepare_scratch_console = _runtime_prepare_patch13  # type: ignore[method-assign]


_old_runtime_call_patch13 = Runtime.call

def _runtime_call_patch13(self: Runtime, name: str, args: List[Any]) -> Any:
    st = _runtime_state(self)
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
    return _old_runtime_call_patch13(self, name, args)

Runtime.call = _runtime_call_patch13  # type: ignore[method-assign]


# Replacement CLI with file embedding flags.  It intentionally mirrors the old
# CLI but adds --embed and --embed-dir to both run and compile so native execution
# and Scratch execution see the same compile-time file tables.
def main(argv: Optional[List[str]] = None) -> int:  # type: ignore[no-redef]
    ap = argparse.ArgumentParser(prog="sbg", description="StageBG/SBG: professional text code -> vanilla Scratch .sb3 compiler")
    ap.add_argument("--version", action="version", version=f"SBG {VERSION}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    runp = sub.add_parser("run", help="run .sbg natively after verifying it can compile to Scratch")
    runp.add_argument("source")
    runp.add_argument("--fast", action="store_true", help="do not sleep on wait()")
    runp.add_argument("--input", default="", help="input value for one-shot Action(Input) execution")
    runp.add_argument("--terminal", action="store_true", help="start an interactive console loop that mirrors the generated Scratch terminal")
    runp.add_argument("--once", action="store_true", help="run exactly one Action(Input) invocation and exit; default unless --terminal is used")
    runp.add_argument("--embed", action="append", default=[], help="embed text file at compile/run time: path[:virtual/name] or path=virtual/name")
    runp.add_argument("--embed-dir", action="append", default=[], help="embed every text file from a directory recursively")

    comp = sub.add_parser("compile", help="compile .sbg source into a vanilla Scratch .sb3 project")
    comp.add_argument("source")
    comp.add_argument("output")
    comp.add_argument("--allow-library", action="store_true", help="allow compiling a file with no on action/on flag/top-level code")
    comp.add_argument("--no-verify", action="store_true", help="skip generated .sb3 structural verification")
    comp.add_argument("--embed", action="append", default=[], help="embed text file into Scratch lists: path[:virtual/name] or path=virtual/name")
    comp.add_argument("--embed-dir", action="append", default=[], help="embed every text file from a directory recursively")

    insp = sub.add_parser("inspect", help="inspect an .sb3 file and print JSON stats")
    insp.add_argument("sb3")

    unp = sub.add_parser("unpack", help="unzip an .sb3 project into a directory")
    unp.add_argument("sb3")
    unp.add_argument("out_dir")

    pkg = sub.add_parser("pkg", help="manage SBG libraries/packages")
    pkg_sub = pkg.add_subparsers(dest="pkg_cmd", required=True)
    pkg_init = pkg_sub.add_parser("init", help="create sbgpkg.json and sbg_modules/")
    pkg_init.add_argument("--name", default=None)
    pkg_install = pkg_sub.add_parser("install", help="install a package from .sbg file, folder, URL, zip URL or registry name")
    pkg_install.add_argument("source", help="local .sbg/folder, URL, or package name when --registry is used")
    pkg_install.add_argument("--name", default=None, help="override installed package name")
    pkg_install.add_argument("--registry", default=None, help="registry JSON path/URL for named packages")
    pkg_sub.add_parser("list", help="list installed packages")
    pkg_remove = pkg_sub.add_parser("remove", help="remove an installed package")
    pkg_remove.add_argument("name")

    args = ap.parse_args(argv)
    source_text = ""
    fallback_filename = "<source>"
    try:
        if args.cmd == "run":
            fallback_filename = args.source
            source_text = Path(args.source).read_text(encoding="utf-8")
            program = parse_source(source_text, args.source)
            program = _program_with_embedded_files(program, args.source, embeds=args.embed, embed_dirs=args.embed_dir)
            assert_scratch_compatible(program)
            rt = Runtime(program, fast=args.fast, filename=args.source, source_text=source_text)
            if args.terminal:
                rt.run_scratch_terminal()
            else:
                rt.run_scratch_once(args.input)
            return 0
        if args.cmd == "compile":
            fallback_filename = args.source
            source_text = Path(args.source).read_text(encoding="utf-8")
            program = parse_source(source_text, args.source)
            program = _program_with_embedded_files(program, args.source, embeds=args.embed, embed_dirs=args.embed_dir)
            project = Compiler(program, allow_library=args.allow_library).compile()
            write_sb3_project(project, args.output, verify=not args.no_verify)
            print(f"compiled: {args.output}")
            if args.allow_library:
                print("warning: compiled in --allow-library mode; Action(Input) may intentionally have no body")
            return 0
        if args.cmd == "inspect":
            print(json.dumps(inspect_sb3(args.sb3), ensure_ascii=False, indent=2))
            return 0
        if args.cmd == "unpack":
            unpack_sb3(args.sb3, args.out_dir)
            print(f"unpacked: {args.out_dir}")
            return 0
        if args.cmd == "pkg":
            root = Path.cwd()
            if args.pkg_cmd == "init":
                path = package_init(root, args.name)
                print(f"initialized: {path}")
                return 0
            if args.pkg_cmd == "install":
                result = install_from_source(args.source, root=root, name=args.name, registry=args.registry)
                print(f"installed: {result['name']} -> {result['path']} ({result['main']})")
                return 0
            if args.pkg_cmd == "list":
                rows = list_packages(root)
                if not rows:
                    print("no packages installed")
                else:
                    for row in rows:
                        status = "ok" if row["installed"] else "missing"
                        print(f"{row['name']}@{row['version']}  main={row['main']}  {status}")
                return 0
            if args.pkg_cmd == "remove":
                remove_package(root, args.name)
                print(f"removed: {args.name}")
                return 0
    except SBGError as e:
        print(format_diagnostic(e, source_text=source_text, fallback_filename=fallback_filename), file=sys.stderr)
        return 1
    except OSError as e:
        print(f"FileError: {e}", file=sys.stderr)
        return 1
    return 2



