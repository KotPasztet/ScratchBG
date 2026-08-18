"""Backward-compatible shim for the old ``sbg._patches`` single-file module.

The real patch chain now lives in :mod:`sbg.patches` (a package split into
per-concern files -- see ``sbg/patches/__init__.py`` for why it's a package
of ``exec()``-loaded files instead of ordinary submodules). This module
exists only because a few call sites do ``from . import _patches as P`` and
then reach for underscore-prefixed attributes on ``P`` (e.g.
``sbg/optimizer.py``: ``P._program_with_embedded_files``,
``P._sbg_project_set_warp``) -- a plain ``from .patches import *`` would drop
those, since ``import *`` skips underscore-prefixed names. Instead we copy
**everything** (except dunders) from the already-patched ``sbg.patches``
namespace onto this module, so ``sbg._patches`` is attribute-for-attribute
identical to ``sbg.patches`` after import.

Do not add new code here -- add it to a file under ``sbg/patches/`` instead.
"""
from __future__ import annotations

from . import patches as _patches_pkg

globals().update(
    {k: v for k, v in vars(_patches_pkg).items() if not k.startswith("__")}
)

del _patches_pkg
