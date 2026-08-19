from typing import Any

VERSION = "0.9.0-patch24-keyboard"

KEYWORDS = {
    "let", "var", "const", "list", "import", "use", "on", "flag", "start", "message", "action",
    "proc", "fn", "if", "else", "repeat", "forever", "while", "for",
    "return", "break", "continue", "true", "false", "null",
    # accumulated by later language extensions (sprite/stage targets, C++-style
    # subset, struct/vector containers, etc.) -- frozen here, no longer mutated
    # at import time.
    "sprite", "stage", "in",
    "struct", "void", "static", "const",
    "priority_queue", "max_priority_queue", "stack", "queue", "deque", "fenwick", "bit", "dsu",
    "auto", "bool", "char", "double", "float", "int", "long", "string", "vector",
    "include", "namespace", "std", "using",
}
KEYWORDS.discard("bool")  # patch18/patch-cpp-surface explicitly removed `bool` as a keyword

MULTI = ["++", "--", "::", ">>", "<<", "==", "!=", "<=", ">=", "&&", "||", "+=", "-=", "*=", "/=", "%="]
SINGLE = set("{}()[];,.+-*/%<>!=:")

SBG_MODULES_DIR = "sbg_modules"
PACKAGE_MANIFEST = "sbgpkg.json"

BACKDROP_SVG = '''<svg version="1.1" width="2" height="2" viewBox="-1 -1 2 2" xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink">
  <!-- Exported by Scratch - http://scratch.mit.edu/ -->
</svg>'''

TERMINAL_LIST_NAME = "Terminal"
TERMINAL_LIST_ID = ",(0/{jAb*2vBd56rlG@1"
ACTION_PROC_NAME = "Action"
# std/io.sbg keeps leftover `cin >>` tokens in this list between reads
# (C++ stdin semantics). Scratch never resets lists on flag click, so the
# green-flag script must clear it to give every run a fresh input buffer.
CIN_BUFFER_LIST_NAME = "cin_buffor"

# Builtins that are emitted as real Scratch blocks. Procedure names may shadow
# them only in statement position; expression-position returns are not available
# for Scratch custom blocks, so diagnostics try to catch confusing cases early.
BUILTIN_EXPR_NAMES = {
    "answer", "random", "round", "abs", "floor", "ceil", "sqrt",
    "join", "len", "item", "contains", "timer",
    # accumulated by later patches (motion/sensing/pen/console/keyboard/C++
    # subset/struct-vector builtins) -- frozen here, no longer mutated at
    # import time.
    "x", "y", "direction", "size", "costumeName", "costumeNumber", "backdropName",
    "backdropNumber", "volume", "tempo", "current", "mouseX", "mouseY", "mouseDown",
    "keyPressed", "loudness", "timeSeconds", "daysSince2000", "username",
    "touching", "touchingColor", "colorTouchingColor", "distanceTo",
    "isTurbo", "dt", "deltaTime", "rawDeltaTime", "fps", "frame",
    "listGet", "listHas", "listLen", "firstItem", "lastItem",
    "terminalVisible", "terminalPromptVisible",
    "at0", "vec_size", "pow", "exp", "ln", "log10", "sin", "cos", "tan",
    "randuble", "rand_double", "random_double", "__field_ref", "__index0_ref",
    "pq_error_value", "maxpq_error_value",
    "at", "back", "front", "empty", "num", "str", "bool01", "text", "letter",
    "acos", "asin", "atan", "binary_search", "lower_bound", "upper_bound",
    "pow10", "stod", "stoi", "to_string", "containsText", "rangeLen",
}
BUILTIN_STMT_NAMES = {
    "log", "wait", "ask", "broadcast", "broadcastAndWait", "resetTimer",
    "push", "insert", "delete", "replace", "setBackdrop", "nextBackdrop",
    "playSound", "stopAllSounds",
    # accumulated by later patches (motion/looks/sound/pen/control/console/
    # keyboard/C++ subset/struct-vector builtins) -- frozen here, no longer
    # mutated at import time.
    "move", "turnLeft", "turnRight", "goTo", "goToXY", "glideToXY", "pointDirection",
    "pointTo", "changeX", "setX", "changeY", "setY", "ifOnEdgeBounce", "setRotationStyle",
    "say", "sayFor", "think", "thinkFor", "show", "hide", "setCostume", "nextCostume",
    "changeSize", "setSize", "changeEffect", "setEffect", "clearEffects",
    "goForwardLayers", "goBackwardLayers", "layerFront", "layerBack",
    "playSoundUntilDone", "stopAllSounds", "changeVolume", "setVolume",
    "playNote", "playDrum", "setInstrument", "changeTempo", "setTempo", "rest",
    "penDown", "penUp", "penClear", "penStamp", "penSetColor", "penChangeHue",
    "penSetHue", "penChangeSaturation", "penSetSaturation", "penChangeBrightness",
    "penSetBrightness", "penChangeTransparency", "penSetTransparency",
    "penChangeSize", "penSetSize", "penChangeParam", "penSetParam", "penEraseAll",
    "clearPen", "erase",
    "createClone", "createCloneOf", "deleteThisClone", "stop", "stopAll",
    "stopThisScript", "stopOtherScripts",
    "hideVariable", "showVariable", "hideList", "showList",
    "clearList", "deleteAll", "deleteFirst", "deleteLast", "appendList",
    "insert_at", "setItem", "copyList", "fillList", "resizeList", "reverseList",
    "sortAsc", "sortDesc", "sort", "sort_desc", "reverse", "resize", "fill",
    "clear", "swapItems", "swap_items", "assign",
    "push_back", "pop_back", "pop_front", "shiftTo", "popTo", "front", "back",
    "print", "println", "cout", "cin", "logMany",
    "showTerminal", "hideTerminal", "toggleTerminal", "showInputPrompt",
    "hideInputPrompt", "enableInputPrompt", "disableInputPrompt",
    "enableTerminalInput", "disableTerminalInput", "setInputPromptVisible",
    "setTerminalInputEnabled", "showTerminalAndPrompt", "hideTerminalAndPrompt",
    "terminalVisible", "terminalPromptVisible", "clearTerminal",
    "binarySearchTo", "binary_search_to", "lowerBoundTo", "lower_bound_to",
    "upperBoundTo", "upper_bound_to",
    "waitUntil", "tick", "frameStart", "updateDelta", "resetDelta",
    "setFixedDelta", "setDeltaCap", "setDeltaScale", "useRealDelta",
    "setDragMode", "setTurbo", "turboOn", "turboOff",
}
# NOTE: `BUILTIN_NAMES` intentionally stays equal to the *original*
# `BUILTIN_EXPR_NAMES | BUILTIN_STMT_NAMES` from before the later builtin
# waves above (patches only ever grew EXPR/STMT and re-pointed a *local*
# `BUILTIN_NAMES` name inside the old `_patches.py` module -- they never
# mutated this module's set object in place). Later builtin names above are
# therefore deliberately absent from BUILTIN_NAMES; this reproduces that
# behavior byte-for-byte rather than "fixing" it.
BUILTIN_NAMES = {
    "answer", "random", "round", "abs", "floor", "ceil", "sqrt",
    "join", "len", "item", "contains", "timer",
    "log", "wait", "ask", "broadcast", "broadcastAndWait", "resetTimer",
    "push", "insert", "delete", "replace", "setBackdrop", "nextBackdrop",
    "playSound", "stopAllSounds",
}

# Classification used by the optional `scratch::name` / `std::name` call
# prefix (see Parser.parse_primary). This is documentary/tooling metadata,
# not enforced at parse time -- either prefix (or none) works for any
# builtin, so a name missing from these sets (or listed "wrong") never
# breaks compilation.
SCRATCH_BUILTIN_NAMES = {
    # backed 1:1 by a real Scratch block (motion/looks/sound/pen/sensing/
    # control/variable&list ops, terminal I/O).
    "answer", "log", "wait", "ask", "broadcast", "broadcastAndWait", "resetTimer",
    "push", "insert", "delete", "replace", "setBackdrop", "nextBackdrop",
    "playSound", "stopAllSounds", "x", "y", "direction", "size", "costumeName",
    "costumeNumber", "backdropName", "backdropNumber", "volume", "tempo",
    "mouseX", "mouseY", "mouseDown", "keyPressed", "loudness", "timeSeconds",
    "daysSince2000", "username", "touching", "touchingColor", "colorTouchingColor",
    "distanceTo", "isTurbo", "dt", "deltaTime", "rawDeltaTime", "fps", "frame",
    "listGet", "listHas", "listLen", "terminalVisible", "terminalPromptVisible",
    "move", "turnLeft", "turnRight", "goTo", "goToXY", "glideToXY", "pointDirection",
    "pointTo", "changeX", "setX", "changeY", "setY", "ifOnEdgeBounce", "setRotationStyle",
    "say", "sayFor", "think", "thinkFor", "show", "hide", "setCostume", "nextCostume",
    "changeSize", "setSize", "changeEffect", "setEffect", "clearEffects",
    "goForwardLayers", "goBackwardLayers", "layerFront", "layerBack",
    "playSoundUntilDone", "changeVolume", "setVolume", "playNote", "playDrum",
    "setInstrument", "changeTempo", "setTempo", "penDown", "penUp", "penClear",
    "penStamp", "penSetColor", "penChangeHue", "penSetHue", "penChangeSaturation",
    "penSetSaturation", "penChangeBrightness", "penSetBrightness",
    "penChangeTransparency", "penSetTransparency", "penChangeSize", "penSetSize",
    "penChangeParam", "penSetParam", "penEraseAll", "createClone", "createCloneOf",
    "deleteThisClone", "stop", "stopAll", "stopThisScript", "stopOtherScripts",
    "hideVariable", "showVariable", "hideList", "showList", "clearList",
    "deleteAll", "showTerminal", "hideTerminal", "toggleTerminal", "showInputPrompt",
    "hideInputPrompt", "enableInputPrompt", "disableInputPrompt", "enableTerminalInput",
    "disableTerminalInput", "setInputPromptVisible", "setTerminalInputEnabled",
    "showTerminalAndPrompt", "hideTerminalAndPrompt", "clearTerminal",
    "waitUntil", "tick", "frameStart", "updateDelta", "resetDelta", "setFixedDelta",
    "setDeltaCap", "setDeltaScale", "useRealDelta", "setDragMode", "setTurbo",
    "turboOn", "turboOff",
}
STD_BUILTIN_NAMES = {
    # C++-stdlib-flavored helpers (math/algorithm/container/string/io), not
    # backed by a dedicated native Scratch block.
    "random", "round", "abs", "floor", "ceil", "sqrt", "join", "len", "item",
    "contains", "timer", "pow", "exp", "ln", "log10", "sin", "cos", "tan",
    "acos", "asin", "atan", "pow10", "rand_double", "random_double", "randuble",
    "stod", "stoi", "to_string", "num", "str", "bool01", "text", "letter",
    "containsText", "rangeLen", "at", "at0", "back", "front", "empty",
    "vec_size", "firstItem", "lastItem", "binary_search", "lower_bound",
    "upper_bound", "binarySearchTo", "binary_search_to", "lowerBoundTo",
    "lower_bound_to", "upperBoundTo", "upper_bound_to", "pq_error_value",
    "maxpq_error_value", "__field_ref", "__index0_ref",
    "appendList", "deleteFirst", "deleteLast", "insert_at", "setItem",
    "copyList", "fillList", "resizeList", "reverseList", "sortAsc", "sortDesc",
    "sort", "sort_desc", "reverse", "resize", "fill", "clear", "swapItems",
    "swap_items", "assign", "push_back", "pop_back", "pop_front", "shiftTo",
    "popTo", "print", "println", "cout", "cin", "logMany",
}


# -----------------------------------------------------------------------
# Runtime/turbo delta-time bookkeeping variable names (Phase 1: moved out
# of _patches.py module scope; these are plain string constants, never
# mutated, so freezing them here is a pure move).
# -----------------------------------------------------------------------
SBG_NOW_VAR = "__sbg_now"
SBG_LAST_VAR = "__sbg_last"
SBG_RAW_DT_VAR = "__sbg_raw_dt"
SBG_DT_VAR = "__sbg_dt"
SBG_DT_SCALE_VAR = "__sbg_dt_scale"
SBG_DT_CAP_VAR = "__sbg_dt_cap"
SBG_FIXED_DT_VAR = "__sbg_fixed_dt"
SBG_FRAME_VAR = "__sbg_frame"
SBG_FPS_VAR = "__sbg_fps"
SBG_TURBO_VAR = "__sbg_turbo"

SBG_DELTA_VARS = {
    SBG_NOW_VAR: 0,
    SBG_LAST_VAR: 0,
    SBG_RAW_DT_VAR: 0,
    SBG_DT_VAR: 0,
    SBG_DT_SCALE_VAR: 1,
    SBG_DT_CAP_VAR: 0.25,
    SBG_FIXED_DT_VAR: 0,
    SBG_FRAME_VAR: 0,
    SBG_FPS_VAR: 0,
    SBG_TURBO_VAR: 1,
}

# -----------------------------------------------------------------------
# Embedded-file (files_demo) list names/ids.
# -----------------------------------------------------------------------
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

# -----------------------------------------------------------------------
# Terminal/console visibility variable names + ids.
# -----------------------------------------------------------------------
TERMINAL_VISIBLE_VAR = "__sbg_terminal_visible"
TERMINAL_INPUT_ENABLED_VAR = "__sbg_terminal_input_enabled"
TERMINAL_VISIBLE_VAR_ID = "sbg:terminal:visible"
TERMINAL_INPUT_ENABLED_VAR_ID = "sbg:terminal:input_enabled"

# -----------------------------------------------------------------------
# Vanilla Scratch key names accepted by the key hat/sensing menu.
# -----------------------------------------------------------------------
SBG_KEY_ALIASES = {
    "space": "space", "spacja": "space",
    "any": "any", "dowolny": "any",
    "up": "up arrow", "up_arrow": "up arrow", "arrow_up": "up arrow", "up arrow": "up arrow",
    "down": "down arrow", "down_arrow": "down arrow", "arrow_down": "down arrow", "down arrow": "down arrow",
    "left": "left arrow", "left_arrow": "left arrow", "arrow_left": "left arrow", "left arrow": "left arrow",
    "right": "right arrow", "right_arrow": "right arrow", "arrow_right": "right arrow", "right arrow": "right arrow",
    "enter": "enter", "return": "enter",
}


def normalize_key_name(value: Any) -> str:
    key = str(value)
    return SBG_KEY_ALIASES.get(key, SBG_KEY_ALIASES.get(key.lower(), key))


# Sentinel meaning "no explicit `return`", distinct from `return null`/None.
NO_ACTION_RETURN = object()

# Late-bound slots, kept for modules that import them lazily via `_g.<name>`
# instead of a normal top-level `from .parser import parse_source`. Set once,
# at the bottom of __init__.py, after parser.py/compiler.py are imported.
parse_source = None
validate_scratch_project = None
