"""Split form of the former single ~7600-line ``sbg/_patches.py``.

WHY THIS EXISTS (read before touching anything here)
------------------------------------------------------
The old ``_patches.py`` was one giant file because its ~40 monkeypatch
"layers" (``patch9`` through ``patch24``) chain onto each other by *bare
module-level name*, not just by rewriting class attributes. Examples of the
pattern used throughout:

    _old_builder_lower_expr_patch18 = _builder_lower_expr
    def _builder_lower_expr_patch18(self, expr): ...
    _builder_lower_expr = _builder_lower_expr_patch18

If ``_builder_lower_expr`` were split across separate, independently
*imported* Python modules, each module would get its own private global
namespace, and a later file's ``_builder_lower_expr = ...`` reassignment
would NOT be visible to a still-earlier file's code that already looked up
``_builder_lower_expr`` as a bare name at call time from its own module
globals. That would silently break the patch chain -- exactly the kind of
bug this codebase has been bitten by before (see kontekst.md, section
"Kluczowa pułapka").

So: this file does NOT `import` the split files as normal Python modules.
It ``exec()``s each one, in the original patch order, into **this module's
own globals()** -- i.e. every file below runs as if it were physically
pasted here in sequence, exactly like the old single file. Behavior is
therefore 100% identical to the pre-split ``_patches.py``; only the on-disk
organization changed, so a human (or another model) can open one ~200-1300
line file for one concern instead of scrolling a 7600-line file.

If you add a new patch: add a new ``pNN_description.py`` file under this
directory (topic-scoped, ideally < ~800 lines) and append its filename to
``_PATCH_FILES`` below, in the position that matches where it should sit in
the chain (usually last, since patches build on everything before them).

Real cross-file/cross-patch renaming or dependency-breaking ("real"
flattening, i.e. making each concern independently importable without the
shared-exec trick) is a separate, much larger project -- see kontekst.md,
"Zadanie A" -- and is intentionally NOT what this split does.
"""
from __future__ import annotations

import pathlib as _pathlib

_PATCH_FILES = [
    "p00_common_imports.py",
    "p09_compiler_return_values.py",
    "p11_terminal_action.py",
    "p12_standard_surface.py",
    "p13_professional_stdlib.py",
    "p14_turbo_runtime.py",
    "p15_adult_stdlib.py",
    "p16_algorithmic_stress.py",
    "p17_language_ergonomics.py",
    "p18_cpp_surface.py",
    "p19_dot_methods.py",
    "p20_cpp_compat.py",
    "p21a_structs_core.py",
    "p21b_lowering_helpers.py",
    "p21c_flat_struct_advanced.py",
    "p22_generic_nested_vector.py",
    "p23_terminal_visibility.py",
    "p24_keyboard_input.py",
]

_here = _pathlib.Path(__file__).parent
for _fname in _PATCH_FILES:
    _path = _here / _fname
    _src = _path.read_text()
    exec(compile(_src, str(_path), "exec"), globals())  # noqa: S102

del _pathlib, _here, _fname, _path, _src
