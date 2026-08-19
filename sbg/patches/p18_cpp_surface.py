# =============================================================================
# Patch 18: C++-style professional surface and missing standard containers
# =============================================================================
# Goal of this patch: StageBG should feel like a compact programming language for
# adults, not like a renamed Scratch worksheet.  This layer deliberately adds
# C++-inspired syntax and library names, then lowers them to vanilla Scratch
# blocks/lists/variables.
#
# Important design rule:
#   We do NOT add solved tasks such as Dijkstra as builtins.
#   We add the generic language/library pieces that were painful while porting
#   olympiad-style C++ solutions: typed declarations, vector-like list API,
#   C++ range-for, STL-like algorithms, priority_queue/DSU/Fenwick packages.

VERSION = "0.9.0-patch18-cpp-surface"

# Lexer additions.  These globals are read dynamically by Lexer.tokens().
SINGLE.add(":")
KEYWORDS.update({
    "auto", "int", "long", "double", "float", "string", "bool", "char",
    "vector", "include", "namespace", "using", "std",
})
if "::" not in MULTI:
    MULTI.insert(0, "::")
# Keep bool as an ordinary identifier too, because std/core.sbg defines proc bool(value).
KEYWORDS.discard("bool")

_CPP_TYPE_KWS = {"auto", "int", "long", "double", "float", "string", "bool", "char"}


def _sbg_preprocess_cpp_surface(text: str) -> str:
    """Tiny compile-time preprocessor for C++-style include ergonomics.

    Supported:
        #include <bits/stdc++.h>  -> import "bits";
        #include <std>            -> import "std";
        #include "lib/foo.sbg"    -> import "lib/foo.sbg";
        using namespace std;      -> ignored

    It is intentionally small; this is not a C++ preprocessor.
    """
    out: List[str] = []
    # Strip a UTF-8 BOM if present: it is not whitespace, so without this the
    # `^\s*#` include regex would miss line 1 and `#include <bits/stdc++.h>`
    # would silently not become `import "bits";` (breaking the iostream gate).
    text = text.lstrip("\ufeff")
    include_re = re.compile(r"^\s*#\s*include\s*([<\"])([^>\"]+)[>\"]\s*$")
    using_re = re.compile(r"^\s*using\s+namespace\s+std\s*;\s*$")
    for line in text.splitlines():
        m = include_re.match(line)
        if m:
            quote, spec = m.group(1), m.group(2).strip()
            if spec == "bits/stdc++.h":
                out.append('import "bits";')
            elif spec in {"std", "std.sbg"}:
                out.append('import "std";')
            elif spec in {"bits", "bits.sbg"}:
                out.append('import "bits";')
            else:
                # Quoted include keeps path semantics.  Angle include becomes package import.
                if quote == '"':
                    out.append(f'import "{spec}";')
                else:
                    pkg = spec[:-4] if spec.endswith('.sbg') else spec
                    out.append(f'import "{pkg}";')
            continue
        if using_re.match(line):
            out.append("// using namespace std; ignored by StageBG")
            continue
        out.append(line)
    return "\n".join(out)


_old_lexer_init_patch18 = Lexer.__init__

def _lexer_init_patch18(self: Lexer, text: str, filename: str = "<source>"):
    return _old_lexer_init_patch18(self, _sbg_preprocess_cpp_surface(text), filename)

Lexer.__init__ = _lexer_init_patch18  # type: ignore[method-assign]


def _parser_parse_initializer_items_patch18(self: Parser) -> List[Any]:
    items: List[Any] = []
    if not self.at("}"):
        while True:
            items.append(self.parse_expr())
            if not self.match(","):
                break
    self.expect("}")
    return items


def _parser_skip_template_patch18(self: Parser) -> None:
    """Skip <...> after vector/int/etc.  The compiler is dynamically typed;
    this only accepts familiar C++ syntax such as vector<int>."""
    if not self.match("<"):
        return
    depth = 1
    while depth > 0:
        if self.peek().kind == "EOF":
            raise self.error("unterminated template/type annotation")
        if self.match("<"):
            depth += 1
        elif self.match(">"):
            depth -= 1
        else:
            self.advance()


def _parser_parse_one_typed_declarator_patch18(self: Parser, start_token: Token, first_type: str, name: str) -> Any:
    """Parse the (optional) initializer for a single declarator name and
    build the matching AST node.  Does not consume the trailing `,`/`;`.
    """
    if first_type == "vector":
        items: List[Any] = []
        if self.match("="):
            if self.match("{"):
                items = _parser_parse_initializer_items_patch18(self)
            elif self.match("["):
                if not self.at("]"):
                    while True:
                        items.append(self.parse_expr())
                        if not self.match(","):
                            break
                self.expect("]")
            else:
                raise self.error("vector initialization expects {...} or [...]")
        return self.loc(ListDecl(name, items), start_token)

    declared_type = first_type
    expr: Any = Literal(0)
    if first_type in {"string", "char"}:
        expr = Literal("")
    elif first_type == "bool":
        expr = Literal(False)
    if self.match("="):
        expr = self.parse_expr()
    return self.loc(VarDecl(name, expr, True), start_token)


def _parser_parse_typed_decl_patch18(self: Parser, start_token: Token, first_type: str) -> Any:
    # vector<int> a = {1,2,3};    -> list a = [1,2,3]
    # int n = 0; / auto x = f();  -> let n = 0 / let x = f()
    # int/long/double/string/bool/auto/char.  Optional extra `long` in `long long`.
    declared_type = first_type
    if first_type == "long" and self.peek().kind == "KW" and self.peek().value == "long":
        self.advance()
        declared_type = "long long"

    # Comma-separated declarator list: `int a, b;`, `int a = 1, b, c = 3;`,
    # `vector<int> v, w;`, etc.  Each declarator gets its own template-skip
    # (so `vector<int> a, vector<int> b`-style repeats of the type aren't
    # required -- plain `vector<int> a, b;` works) and its own initializer.
    decls: List[Any] = []
    while True:
        _parser_skip_template_patch18(self)
        name = self.expect_ident()
        decls.append(_parser_parse_one_typed_declarator_patch18(self, start_token, declared_type, name))
        if self.match(","):
            continue
        break
    self.expect(";")

    if len(decls) == 1:
        return decls[0]
    return self.loc(BlockStmt(decls), start_token)


_old_parse_statement_patch18_base = Parser.parse_statement

def _parser_parse_statement_patch18(self: Parser) -> Any:
    start_token = self.peek()
    # Fix patch17 standalone i++/i-- bug and keep it C++-style.
    if self.peek().kind == "IDENT" and self.peek(1).value in ("++", "--"):
        name = self.advance().value
        op = self.advance().value
        self.expect(";")
        return self.loc(AssignStmt(name, "+=" if op == "++" else "-=", Literal(1)), start_token)
    if self.peek().value in _CPP_TYPE_KWS | {"vector"} and self.peek().kind in {"KW", "IDENT"}:
        typ = self.advance().value
        return _parser_parse_typed_decl_patch18(self, start_token, typ)
    return _old_parse_statement_patch18_base(self)

Parser.parse_statement = _parser_parse_statement_patch18  # type: ignore[method-assign]


def _parser_parse_for_init_or_update_patch18(self: Parser, *, terminators: set[str]) -> Optional[Any]:
    if self.peek().value in terminators:
        return None
    if self.peek().value in _CPP_TYPE_KWS and self.peek().kind in {"KW", "IDENT"}:
        typ = self.advance().value
        if typ == "long" and self.peek().kind == "KW" and self.peek().value == "long":
            self.advance()
        _parser_skip_template_patch18(self)
        name = self.expect_ident()
        expr: Any = Literal(0 if typ != "string" else "")
        if self.match("="):
            expr = self.parse_expr()
        return VarDecl(name, expr, True)
    if self.peek().kind == "IDENT" and self.peek(1).value in ("++", "--"):
        name = self.advance().value
        op = self.advance().value
        return AssignStmt(name, "+=" if op == "++" else "-=", Literal(1))
    if self.peek().kind == "IDENT" and self.peek(1).value in ("=", "+=", "-=", "*=", "/=", "%="):
        name = self.advance().value
        op = self.advance().value
        return AssignStmt(name, op, self.parse_expr())
    return ExprStmt(self.parse_expr())


_old_parse_for_patch18_base = Parser.parse_for

def _parser_parse_for_patch18(self: Parser, start_token: Token) -> ForStmt:
    saved = self.i
    self.expect("(")

    # C++ range-for: for (auto x : xs) / for (int x : xs)
    if self.peek().value in _CPP_TYPE_KWS | {"vector"} and self.peek().kind in {"KW", "IDENT"}:
        typ = self.advance().value
        if typ == "vector":
            _parser_skip_template_patch18(self)
        elif typ == "long" and self.peek().kind == "KW" and self.peek().value == "long":
            self.advance()
        name = self.expect_ident()
        if self.match(":"):
            source = self.parse_expr()
            self.expect(")")
            return _sbg_make_for_each(start_token, name, source, self.parse_block(), declare_value=True)
        self.i = saved

    # C++ style for (int i = 0; i < n; i++) plus fixed ++/--.
    self.expect("(")
    init = _parser_parse_for_init_or_update_patch18(self, terminators={";"})
    self.expect(";")
    cond = None if self.at(";") else self.parse_expr()
    self.expect(";")
    update = _parser_parse_for_init_or_update_patch18(self, terminators={")"})
    self.expect(")")
    body = self.parse_block()
    return self.loc(ForStmt(init, cond, update, body), start_token)

Parser.parse_for = _parser_parse_for_patch18  # type: ignore[method-assign]


# ---- C++/STL-like builtin names lowered to vanilla Scratch ------------------

BUILTIN_EXPR_NAMES.update({
    "size", "empty", "front", "back", "at",
    # "stoi"/"stod" removed from here -- real `.sbg proc`s in
    # packages/std/core.sbg now (same treatment as str/to_string/num).
    "lower_bound", "upper_bound", "binary_search",
})
# NOTE: "str"/"to_string" deliberately NOT added here anymore. They are real
# `.sbg proc`s in packages/std/strings.sbg now (str() was already there;
# to_string() added as an alias). Before this change they were BOTH a native
# alias here AND a duplicate `.sbg proc` -- compile_call_expr (this file) and
# Runtime.call disagreed on which implementation actually ran (compiler
# lowered proc calls first and used the `.sbg` version; the interpreter
# checked native names first and used this native version). See kontekst.md
# "Sesja kontynuacyjna" for the empirical trace. Removing the native branch
# makes both paths consistently use the `.sbg` proc, and makes `std::str`/
# `std::to_string` require `import "std";` like a real stdlib call, instead
# of silently working without any import.
BUILTIN_STMT_NAMES.update({
    "push_back", "pop_back", "pop_front", "clear", "erase", "insert_at",
    "assign", "resize", "fill", "swap_items", "sort", "sort_desc", "reverse",
    "lower_bound_to", "upper_bound_to", "binary_search_to",
})
BUILTIN_NAMES = BUILTIN_EXPR_NAMES | BUILTIN_STMT_NAMES


def _sbg_is_list_var_for_builder(self: ScratchBuilder, expr: Any) -> bool:
    return isinstance(expr, VarExpr) and expr.name in getattr(self, "lists", {})


_old_builder_compile_call_expr_patch18 = ScratchBuilder.compile_call_expr

def _builder_compile_call_expr_patch18(self: ScratchBuilder, expr: CallExpr, parent: Optional[str]) -> str:
    name, a = expr.callee, expr.args
    if name == "size":
        self.need_args(name, a, 1)
        return self.compile_call_expr(CallExpr("len", a), parent)
    if name == "empty":
        self.need_args(name, a, 1)
        return self.compile_expr(BinaryExpr(CallExpr("len", a), "==", Literal(0)), parent)
    if name == "front":
        self.need_args(name, a, 1)
        return self.compile_call_expr(CallExpr("item", [a[0], Literal(1)]), parent)
    if name == "back":
        self.need_args(name, a, 1)
        return self.compile_call_expr(CallExpr("item", [a[0], CallExpr("len", [a[0]])]), parent)
    if name == "at":
        self.need_args(name, a, 2)
        return self.compile_call_expr(CallExpr("item", [a[0], a[1]]), parent)
    # str/to_string/stoi/stod: no longer handled here -- real `.sbg proc`s in
    # packages/std/strings.sbg and packages/std/core.sbg (see notes above
    # BUILTIN_EXPR_NAMES.update).
    # lower_bound/upper_bound/binary_search are handled in expression-lowering
    # because they need command blocks before a reporter value can be used.
    return _old_builder_compile_call_expr_patch18(self, expr, parent)

ScratchBuilder.compile_call_expr = _builder_compile_call_expr_patch18  # type: ignore[method-assign]


_old_builder_is_boolean_expr_patch18 = ScratchBuilder.is_boolean_expr

def _builder_is_boolean_expr_patch18(self: ScratchBuilder, expr: Any) -> bool:
    if isinstance(expr, CallExpr) and expr.callee in {"empty", "binary_search"}:
        return True
    # A lowered user/package .sbg proc call (VarExpr temp) that was tagged as
    # boolean-shaped by _builder_lower_expr BEFORE lowering replaced the
    # CallExpr with a temp variable. Without this, migrating any
    # boolean-shaped builtin (bool01, containsText, ...) from native to a
    # real `.sbg proc` would silently degrade `if (fn(x)) {...}` from a
    # boolean CONDITION input ([2, ...]) to a plain value input ([1, ...]),
    # producing an incorrect/unimportable .sb3. See kontekst.md.
    if isinstance(expr, VarExpr) and getattr(expr, "_sbg_bool_shaped", False):
        return True
    return _old_builder_is_boolean_expr_patch18(self, expr)

ScratchBuilder.is_boolean_expr = _builder_is_boolean_expr_patch18  # type: ignore[method-assign]


_old_compile_call_stmt_patch18 = ScratchBuilder.compile_call_stmt

def _builder_compile_call_stmt_patch18(self: ScratchBuilder, expr: CallExpr) -> Optional[str]:
    name, a = expr.callee, expr.args
    # Vector/list aliases named like C++/STL, but lowered to the existing vanilla
    # Scratch list builtins.
    alias = {
        "push_back": "push",
        "erase": "delete",
        "insert_at": "insert",
        "clear": "clearList",
        "resize": "resizeList",
        "fill": "fillList",
        "swap_items": "swapItems",
        "reverse": "reverseList",
        "sort": "sortAsc",
        "sort_desc": "sortDesc",
        "lower_bound_to": "lowerBoundTo",
        "upper_bound_to": "upperBoundTo",
        "binary_search_to": "binarySearchTo",
    }
    if name in alias:
        return self.compile_call_stmt(CallExpr(alias[name], a))
    if name == "pop_back":
        self.need_args(name, a, 1)
        return self.compile_call_stmt(CallExpr("deleteLast", a))
    if name == "pop_front":
        self.need_args(name, a, 1)
        return self.compile_call_stmt(CallExpr("deleteFirst", a))
    if name == "assign":
        self.need_args(name, a, 3)
        return self.compile_statement_chain([
            ExprStmt(CallExpr("clearList", [a[0]])),
            ExprStmt(CallExpr("resizeList", [a[0], a[1], a[2]])),
        ])
    return _old_compile_call_stmt_patch18(self, expr)

ScratchBuilder.compile_call_stmt = _builder_compile_call_stmt_patch18  # type: ignore[method-assign]


# Patch expression lowering so lower_bound(v,x) can be used naturally in `let p = ...`.
# It still compiles to command blocks + a hidden temporary variable before the
# surrounding statement, which is the only vanilla Scratch-compatible way to run
# a loop and then feed a value into a reporter slot.
_old_builder_lower_expr_patch18 = _builder_lower_expr

def _builder_lower_expr_patch18(self: ScratchBuilder, expr: Any) -> Tuple[List[Any], Any]:
    if isinstance(expr, CallExpr) and expr.callee in {"lower_bound", "upper_bound", "binary_search"}:
        if len(expr.args) != 2:
            raise CompileError(f"{expr.callee}() expects 2 args: list, value")
        self.return_temp_counter = getattr(self, "return_temp_counter", 0) + 1
        temp = f"__sbg_{expr.callee}_{self.return_temp_counter}"
        self.var_id(temp)
        stmt_name = {"lower_bound":"lower_bound_to", "upper_bound":"upper_bound_to", "binary_search":"binary_search_to"}[expr.callee]
        pre_a, lowered_args = _builder_lower_exprs(self, expr.args)
        return [*pre_a, ExprStmt(CallExpr(stmt_name, [lowered_args[0], lowered_args[1], VarExpr(temp)]))], VarExpr(temp)
    return _old_builder_lower_expr_patch18(self, expr)

# Replace the global function used by patched compile_stmt.
_builder_lower_expr = _builder_lower_expr_patch18  # type: ignore[assignment]


_old_runtime_call_patch18 = Runtime.call

def _runtime_call_patch18(self: Runtime, name: str, args: List[Any]) -> Any:
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
    # str/to_string: no longer native here -- real `.sbg proc`s in
    # packages/std/strings.sbg (see note above BUILTIN_EXPR_NAMES.update in
    # the compile_call_expr patch18 section). Runtime.call falls through to
    # `self.procs` lookup (patch9, earlier in this chain) for these names now.
    # str/to_string/stoi/stod: no longer native here -- real `.sbg proc`s in
    # packages/std/strings.sbg and packages/std/core.sbg (see notes above
    # BUILTIN_EXPR_NAMES.update in the compile_call_expr patch18 section).
    # Runtime.call falls through to `self.procs` lookup (patch9, earlier in
    # this chain) for these names now.
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
    return _old_runtime_call_patch18(self, name, args)

Runtime.call = _runtime_call_patch18  # type: ignore[method-assign]


_old_project_ensure_patch18 = _project_ensure_patch17

def _project_ensure_patch18(project: Dict[str, Any]) -> Dict[str, Any]:
    project = _old_project_ensure_patch18(project)
    meta = project.setdefault("meta", {})
    meta["agent"] = f"StageBG/SBG {VERSION}"
    meta["stagebgPatch18"] = "C++-style syntax (#include, typed declarations, vector/list aliases, STL-like algorithms)."
    return project

_old_compiler_compile_patch18 = Compiler.compile

def _compiler_compile_patch18(self: Compiler) -> Dict[str, Any]:
    return _project_ensure_patch18(_old_compiler_compile_patch18(self))

Compiler.compile = _compiler_compile_patch18  # type: ignore[method-assign]



# ---- Patch 18b: compatibility fixes for patch17 syntax and sprite size() -----

_prev_parse_for_patch18 = Parser.parse_for

def _parser_parse_for_patch18b(self: Parser, start_token: Token) -> ForStmt:
    # Keep existing StageBG syntax: for (let x in xs), for (let i in range(...)).
    saved = self.i
    try:
        self.expect("(")
        if self.peek().kind == "KW" and self.peek().value in {"let", "var"}:
            self.i = saved
            return _old_parse_for_patch18_base(self, start_token)
    except Exception:
        pass
    self.i = saved
    return _prev_parse_for_patch18(self, start_token)

Parser.parse_for = _parser_parse_for_patch18b  # type: ignore[method-assign]

_prev_compile_call_expr_patch18 = ScratchBuilder.compile_call_expr

def _builder_compile_call_expr_patch18b(self: ScratchBuilder, expr: CallExpr, parent: Optional[str]) -> str:
    # size() with no args remains the sprite-size reporter from patch12.
    # size(xs) is C++ vector/string size.
    if expr.callee == "size" and len(expr.args) == 0:
        return _old_builder_compile_call_expr_patch18(self, expr, parent)
    return _prev_compile_call_expr_patch18(self, expr, parent)

ScratchBuilder.compile_call_expr = _builder_compile_call_expr_patch18b  # type: ignore[method-assign]

_prev_runtime_call_patch18 = Runtime.call

def _runtime_call_patch18b(self: Runtime, name: str, args: List[Any]) -> Any:
    if name == "size" and len(args) == 0:
        return _old_runtime_call_patch18(self, name, args)
    return _prev_runtime_call_patch18(self, name, args)

Runtime.call = _runtime_call_patch18b  # type: ignore[method-assign]


# Patch 18c: compile BlockStmt (used by comma-separated declarators such as
# `int a, b;`) by simply chaining the compiled forms of its inner statements
# in place, exactly as if each had been written on its own line.
_prev_compile_stmt_patch18c = ScratchBuilder.compile_stmt

def _builder_compile_stmt_patch18c(self: ScratchBuilder, stmt: Any) -> Optional[str]:
    if isinstance(stmt, BlockStmt):
        return self.compile_statement_chain(stmt.body)
    return _prev_compile_stmt_patch18c(self, stmt)

ScratchBuilder.compile_stmt = _builder_compile_stmt_patch18c  # type: ignore[method-assign]

# Same BlockStmt handling for the native interpreter (used by `sbg run`
# to verify the program before compiling it to Scratch). Runtime._exec_stmt
# gets reassigned again later by patch22, so hook the innermost base impl
# instead of the current top of the chain.
_prev_exec_stmt_patch18c = Runtime._base__exec_stmt

def _runtime_exec_stmt_patch18c(self: Runtime, stmt: Any) -> None:
    if isinstance(stmt, BlockStmt):
        for s in stmt.body:
            self.exec_stmt(s)
        return
    return _prev_exec_stmt_patch18c(self, stmt)

Runtime._base__exec_stmt = _runtime_exec_stmt_patch18c  # type: ignore[method-assign]