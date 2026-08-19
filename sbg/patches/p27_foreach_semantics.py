# Patch 27: range-for semantics for strings (todo [6], completes for-each).
#
# `for (auto x : xs)` / `for (int k : xs)` over *lists* already worked (patch
# p17/p20 lowering to an index loop with `item(list, i)`). But iterating a
# *string* was broken: `item(s, i)` on a string goes through the flat-struct
# "encoded row" machinery (space-separated token access, see p21b/p21c), which
# is inconsistent with `len(s)` counting characters -- `for (char c : "abc")`
# produced garbage.
#
# Fix: p21a now records source-level variable types in `_SBG_VAR_TYPES21`
# (declarations + function parameters) at parse time. When the foreach source
# is a variable known to be `string`, this patch lowers the loop to
# `letter(s, i)` instead of `item(s, i)` -- character iteration, exactly like
# C++ range-for over std::string.
#
# Scope of the type registry: declaration order (a name is typed from its
# declaration/parameter onwards, which matches how source code is read).
# Non-VarExpr sources (e.g. `for (char c : f())`) keep the legacy item()
# lowering.

# C++ reference markers (`for (auto& x : xs)`, `for (const T& x : xs)`) need a
# bare `&` token; the type-skip helpers in p20 already discard it afterwards.
# Same trick p17 uses for MULTI: the Lexer reads SINGLE dynamically.
if "&" not in SINGLE:
    SINGLE.add("&")

_prev_make_for_each_patch27 = _sbg_make_for_each


def _sbg_foreach_is_string_source_patch27(source: Any) -> bool:
    if isinstance(source, Literal) and isinstance(source.value, str):
        return True
    return isinstance(source, VarExpr) and _SBG_VAR_TYPES21.get(source.name) == "string"


def _sbg_make_for_each(start_token: Token, value_name: str, source: Any, body: List[Any], *, declare_value: bool = True, index_name: Optional[str] = None) -> ForStmt:  # type: ignore[no-redef]
    if index_name is None and _sbg_foreach_is_string_source_patch27(source):
        temp = _sbg_fresh_foreach_temp()
        init = VarDecl(temp, Literal(1), True)
        cond = BinaryExpr(VarExpr(temp), "<=", CallExpr("len", [source]))
        update = AssignStmt(temp, "+=", Literal(1))
        value_expr = CallExpr("letter", [source, VarExpr(temp)])
        prefix: List[Any] = []
        prefix.append(VarDecl(value_name, value_expr, True) if declare_value else AssignStmt(value_name, "=", value_expr))
        out = ForStmt(init, cond, update, [*prefix, *body])
        return _sbg_copy_loc(out, start_token)
    return _prev_make_for_each_patch27(start_token, value_name, source, body, declare_value=declare_value, index_name=index_name)


# Clear the parse-time type registry once per parsed source (a new Lexer is
# created at the start of every parse_source call).
_prev_lexer_init_patch27 = Lexer.__init__


def _lexer_init_patch27(self: Lexer, text: str, filename: str = "<source>"):
    _SBG_VAR_TYPES21.clear()
    return _prev_lexer_init_patch27(self, text, filename)


Lexer.__init__ = _lexer_init_patch27  # type: ignore[method-assign]


# C++ allows a single statement (no braces) as the body of if/else/while/for.
# The core parser hard-requires `{`, which rejects perfectly ordinary code like
#   for (char c : s) if (c == 'a') n++;
# parse_block is the single choke point used by all statement bodies, so accept
# a lone statement there. Brace bodies behave exactly as before.
_prev_parse_block_patch27 = Parser.parse_block


def _parser_parse_block_patch27(self: Parser) -> List[Any]:
    if not self.at("{"):
        return [self.parse_statement()]
    return _prev_parse_block_patch27(self)


Parser.parse_block = _parser_parse_block_patch27  # type: ignore[method-assign]

