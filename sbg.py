#!/usr/bin/env python3
"""StageBG / SBG command-line entry point.

This is a thin shim over the :mod:`sbg` package (see the ``sbg/`` directory).
It exists so ``python3 sbg.py ...`` keeps working exactly as it did before the
single-file module was split into a package.
"""
from sbg import main

if __name__ == "__main__":
    raise SystemExit(main())
