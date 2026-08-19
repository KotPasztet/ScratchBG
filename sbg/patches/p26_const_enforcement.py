# Patch 26: `const` enforcement (BUGS_REPORT #9).
#
# `const int MAX = 10;` (C++ style) and `const x = 5;` (SBG style) now produce
# VarDecl(mutable=False) -- p21a strips the `const` prefix from the type string
# and flags the declaration. This pass walks the fully lowered program (after
# the whole patch chain, so names are final and mangling-consistent) and raises
# CompileError on any assignment to a const variable:
#   * `MAX = 5;`, `MAX += 1;`, `MAX++;` (all AssignStmt forms)
#   * compound/lvalue targets (`MAX.f = ...`, `vec[i] = ...` when MAX is a list)
#   * `cin >> MAX;` (lowered by p20 to a BlockStmt of AssignStmt)
# Shadowing is safe: the p15 mangler gives block-local declarations fresh
# `__loc_N_` names, so a local `int MAX` never collides with a const elsewhere.
#
# Limitation: `const vector<T>` is not enforced (lists have no mutable flag);
# only scalar/struct-variable consts are checked.

from sbg.errors import CompileError


def _sbg_const_err26(st: Any, name: str) -> "CompileError":
    # Demangle `__loc_N_name` back to the source-level name for a readable
    # diagnostic (the mangler always embeds the original identifier as the
    # suffix; sanitize only replaces non-word characters).
    disp = re.sub(r"^__loc_\d+_", "", name)
    err = CompileError(f"cannot assign to const variable '{disp}'")
    for attr in ("filename", "line", "col"):
        if hasattr(st, attr):
            setattr(err, attr, getattr(st, attr))
    return err


def _sbg_collect_consts26(body: List[Any], out: set) -> None:
    for st in body:
        if isinstance(st, VarDecl) and st.mutable is False:
            out.add(st.name)
        if isinstance(st, ProcDecl):
            _sbg_collect_consts26(st.body, out)
        elif isinstance(st, EventDecl):
            _sbg_collect_consts26(st.body, out)
        elif isinstance(st, TargetDecl):
            _sbg_collect_consts26(st.body, out)
        elif isinstance(st, BlockStmt):
            _sbg_collect_consts26(st.body, out)
        elif isinstance(st, IfStmt):
            _sbg_collect_consts26(st.then_body, out)
            if st.else_body is not None:
                _sbg_collect_consts26(st.else_body, out)
        elif isinstance(st, (RepeatStmt, ForeverStmt, WhileStmt)):
            _sbg_collect_consts26(st.body, out)
        elif isinstance(st, ForStmt):
            if st.body:
                _sbg_collect_consts26(st.body, out)


def _sbg_check_consts26(body: List[Any], consts: set) -> None:
    for st in body:
        if isinstance(st, AssignStmt):
            if st.name in consts:
                raise _sbg_const_err26(st, st.name)
        elif isinstance(st, LValueAssignStmt):
            t = st.target
            if isinstance(t, VarExpr) and t.name in consts:
                raise _sbg_const_err26(st, t.name)
        if isinstance(st, ProcDecl):
            _sbg_check_consts26(st.body, consts)
        elif isinstance(st, EventDecl):
            _sbg_check_consts26(st.body, consts)
        elif isinstance(st, TargetDecl):
            _sbg_check_consts26(st.body, consts)
        elif isinstance(st, BlockStmt):
            _sbg_check_consts26(st.body, consts)
        elif isinstance(st, IfStmt):
            _sbg_check_consts26(st.then_body, consts)
            if st.else_body is not None:
                _sbg_check_consts26(st.else_body, consts)
        elif isinstance(st, (RepeatStmt, ForeverStmt, WhileStmt)):
            _sbg_check_consts26(st.body, consts)
        elif isinstance(st, ForStmt):
            if isinstance(st.init, AssignStmt) and st.init.name in consts:
                raise _sbg_const_err26(st.init, st.init.name)
            if isinstance(st.update, AssignStmt) and st.update.name in consts:
                raise _sbg_const_err26(st.update, st.update.name)
            if st.body:
                _sbg_check_consts26(st.body, consts)


_old_parse_source_patch26 = parse_source


def parse_source(text: str, filename: str = "<source>") -> Program:  # type: ignore[no-redef]
    program = _old_parse_source_patch26(text, filename)
    consts: set = set()
    _sbg_collect_consts26(program.body, consts)
    if consts:
        _sbg_check_consts26(program.body, consts)
    return program


_g.parse_source = parse_source
