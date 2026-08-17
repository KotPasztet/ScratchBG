VERSION = "0.9.0-patch24-keyboard"

KEYWORDS = {
    "let", "var", "const", "list", "import", "use", "on", "flag", "start", "message", "action",
    "proc", "fn", "if", "else", "repeat", "forever", "while", "for",
    "return", "break", "continue", "true", "false", "null",
}

MULTI = ["==", "!=", "<=", ">=", "&&", "||", "+=", "-=", "*=", "/=", "%="]
SINGLE = set("{}()[];,.+-*/%<>!=")

SBG_MODULES_DIR = "sbg_modules"
PACKAGE_MANIFEST = "sbgpkg.json"

BACKDROP_SVG = '''<svg version="1.1" width="2" height="2" viewBox="-1 -1 2 2" xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink">
  <!-- Exported by Scratch - http://scratch.mit.edu/ -->
</svg>'''

TERMINAL_LIST_NAME = "Terminal"
TERMINAL_LIST_ID = ",(0/{jAb*2vBd56rlG@1"
ACTION_PROC_NAME = "Action"

# Builtins that are emitted as real Scratch blocks. Procedure names may shadow
# them only in statement position; expression-position returns are not available
# for Scratch custom blocks, so diagnostics try to catch confusing cases early.
BUILTIN_EXPR_NAMES = {
    "answer", "random", "round", "abs", "floor", "ceil", "sqrt",
    "join", "len", "item", "contains", "timer",
}
BUILTIN_STMT_NAMES = {
    "log", "wait", "ask", "broadcast", "broadcastAndWait", "resetTimer",
    "push", "insert", "delete", "replace", "setBackdrop", "nextBackdrop",
    "playSound", "stopAllSounds",
}
BUILTIN_NAMES = BUILTIN_EXPR_NAMES | BUILTIN_STMT_NAMES


# Late-bound slots. parser.py publishes `parse_source` (base) and
# compiler.py publishes `validate_scratch_project` (base); _patches.py
# overwrites both with the final patched versions at import time. Code
# that must always see the FINAL version reads them via `_g.<name>`.
parse_source = None
validate_scratch_project = None
