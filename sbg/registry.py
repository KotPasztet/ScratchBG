"""Per-``Program`` mutable state used during parsing/compilation.

Before this refactor, StageBG kept the struct/vector type tables and the
``foreach`` temp-variable counter as *process-global* module-level
dictionaries inside ``_patches.py`` (``_SBG_STRUCT_DEFS21``,
``_SBG_FLAT_VECTOR_TYPES21``, ``_SBG_STRUCT_DEFAULT_BASE21``,
``_SBG_NESTED_VECTOR_NAMES21``, ``_sbg_foreach_parse_counter``). That meant
state from one ``parse_source``/``compile`` call could leak into the next
call within the same process (e.g. compiling two different programs
back-to-back in a long-lived process, as the ``run``/``watch`` CLI modes
and any embedder do).

``ProgramRegistry`` replaces all of that with a single container that is
created fresh per parse and threaded through the ``Parser`` (and from
there attached to the resulting ``Program``, mirroring what the compiler
already did on its own end via ``setattr(program, "sbg_struct_defs", ...)``).
"""
from __future__ import annotations

from typing import Dict, List, Set, Tuple


class ProgramRegistry:
    """Struct/vector type tables + foreach-temp counter for a single parse."""

    __slots__ = (
        "struct_defs",
        "flat_vector_types",
        "struct_default_base",
        "nested_vector_names",
        "_foreach_counter",
    )

    def __init__(self) -> None:
        # struct name -> [(field_type, field_name), ...]
        self.struct_defs: Dict[str, List[Tuple[str, str]]] = {}
        # flat vector<vector<Struct>> "base" name -> struct name
        self.flat_vector_types: Dict[str, str] = {}
        # struct name -> the flat-vector base name it defaults to (inverse-ish
        # of flat_vector_types, first-seen wins, same as the old module code)
        self.struct_default_base: Dict[str, str] = {}
        # names declared as vector<vector<T>> of a non-struct T
        self.nested_vector_names: Set[str] = set()
        self._foreach_counter: int = 0

    def fresh_foreach_temp(self) -> str:
        """Return a unique temp-variable name for a desugared `foreach` loop."""
        self._foreach_counter += 1
        return f"__sbg_foreach_i_{self._foreach_counter}"

    def note_flat_vector_type(self, base: str, struct_name: str) -> None:
        """Record that `base` is a flattened vector<vector<struct_name>>."""
        self.flat_vector_types[base] = struct_name
        if struct_name and struct_name not in self.struct_default_base:
            self.struct_default_base[struct_name] = base
