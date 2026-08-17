"""StageBG / SBG: a professional, text-first language for Scratch's Stage/background.

This package is the modular split of the former single-file ``sbg.py``. The
layered monkeypatch chain lives in :mod:`sbg._patches` and runs at import time,
so the final patched entry points (``main``, ``parse_source``,
``validate_scratch_project``, ...) are published as soon as the package is
imported.
"""
from . import _patches  # noqa: F401  (importing runs the patch chain)
from ._patches import *  # noqa: F401,F403  (re-export the patched public API)

__version__ = VERSION  # noqa: F405

__all__ = ["main", "VERSION", "__version__"]
