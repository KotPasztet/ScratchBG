"""StageBG / SBG: a professional, text-first language for Scratch's Stage/background.

This package is the modular split of the former single-file ``sbg.py``. The
layered monkeypatch chain used to live in one ~7600-line ``sbg/_patches.py``;
it now lives in :mod:`sbg.patches` (split into per-concern files under
``sbg/patches/``, see that package's docstring for why it uses an exec-based
loader instead of ordinary submodule imports) and runs at import time, so the
final patched entry points (``main``, ``parse_source``,
``validate_scratch_project``, ...) are published as soon as the package is
imported. ``sbg._patches`` still exists as a thin backward-compat shim (some
call sites do ``from . import _patches as P``) but contains no logic of its
own anymore.
"""
from . import patches as _patches  # noqa: F401  (importing runs the patch chain)
from .patches import *  # noqa: F401,F403  (re-export the patched public API)

__version__ = VERSION  # noqa: F405

__all__ = ["main", "VERSION", "__version__"]
