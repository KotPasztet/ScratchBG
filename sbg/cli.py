from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional

from . import globals as _g
from .errors import SBGError, format_diagnostic
from .ast import Program
from .compiler import Compiler, inspect_sb3, unpack_sb3, write_sb3_project
from .runtime import Runtime
from .packages import install_from_source, list_packages, package_init, remove_package

# =============================================================================
# Native runner compatibility guard
# =============================================================================

def assert_scratch_compatible(program: Program) -> None:
    """Fail before native execution if the same program cannot compile to Scratch.

    This keeps the promise: every SBG program accepted by `sbg run` is also
    accepted by `sbg compile` for the Stage-only .sb3 target. The generated
    project is built in memory only; nothing is written to disk.
    """
    project = Compiler(program).compile()
    _g.validate_scratch_project(project)

# =============================================================================
# CLI
# =============================================================================

def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="sbg", description="StageBG/SBG: professional text code -> Scratch .sb3 compiler")
    ap.add_argument("--version", action="version", version=f"SBG {_g.VERSION}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    runp = sub.add_parser("run", help="run .sbg natively in the console, after verifying it can compile to Scratch")
    runp.add_argument("source")
    runp.add_argument("--fast", action="store_true", help="do not sleep on wait()")
    runp.add_argument("--input", default="", help="input value for one-shot Action(Input) execution")
    runp.add_argument("--terminal", action="store_true", help="start an interactive console loop that mirrors the generated Scratch terminal")
    runp.add_argument("--once", action="store_true", help="run exactly one Action(Input) invocation and exit; this is the default unless --terminal is used")

    comp = sub.add_parser("compile", help="compile .sbg source into a Scratch .sb3 project")
    comp.add_argument("source")
    comp.add_argument("output")
    comp.add_argument("--allow-library", action="store_true", help="allow compiling a file with no on action/on flag/top-level code")
    comp.add_argument("--no-verify", action="store_true", help="skip generated .sb3 structural verification")

    insp = sub.add_parser("inspect", help="inspect an .sb3 file and print JSON stats")
    insp.add_argument("sb3")

    unp = sub.add_parser("unpack", help="unzip an .sb3 project into a directory")
    unp.add_argument("sb3")
    unp.add_argument("out_dir")

    pkg = sub.add_parser("pkg", help="manage SBG libraries/packages")
    pkg_sub = pkg.add_subparsers(dest="pkg_cmd", required=True)

    pkg_init = pkg_sub.add_parser("init", help="create sbgpkg.json and sbg_modules/")
    pkg_init.add_argument("--name", default=None)

    pkg_install = pkg_sub.add_parser("install", help="install a package from .sbg file, folder, URL, zip URL or registry name")
    pkg_install.add_argument("source", help="local .sbg/folder, URL, or package name when --registry is used")
    pkg_install.add_argument("--name", default=None, help="override installed package name")
    pkg_install.add_argument("--registry", default=None, help="registry JSON path/URL for named packages")

    pkg_list = pkg_sub.add_parser("list", help="list installed packages")

    pkg_remove = pkg_sub.add_parser("remove", help="remove an installed package")
    pkg_remove.add_argument("name")

    args = ap.parse_args(argv)
    source_text = ""
    fallback_filename = "<source>"
    try:
        if args.cmd == "run":
            fallback_filename = args.source
            source_text = Path(args.source).read_text(encoding="utf-8")
            program = _g.parse_source(source_text, args.source)
            assert_scratch_compatible(program)
            rt = Runtime(program, fast=args.fast, filename=args.source, source_text=source_text)
            if args.terminal:
                rt.run_scratch_terminal()
            else:
                rt.run_scratch_once(args.input)
            return 0
        if args.cmd == "compile":
            fallback_filename = args.source
            source_text = Path(args.source).read_text(encoding="utf-8")
            program = _g.parse_source(source_text, args.source)
            project = Compiler(program, allow_library=args.allow_library).compile()
            write_sb3_project(project, args.output, verify=not args.no_verify)
            print(f"compiled: {args.output}")
            if args.allow_library:
                print("warning: compiled in --allow-library mode; Action(Input) may intentionally have no body")
            return 0
        if args.cmd == "inspect":
            print(json.dumps(inspect_sb3(args.sb3), ensure_ascii=False, indent=2))
            return 0
        if args.cmd == "unpack":
            unpack_sb3(args.sb3, args.out_dir)
            print(f"unpacked: {args.out_dir}")
            return 0
        if args.cmd == "pkg":
            root = Path.cwd()
            if args.pkg_cmd == "init":
                path = package_init(root, args.name)
                print(f"initialized: {path}")
                return 0
            if args.pkg_cmd == "install":
                result = install_from_source(args.source, root=root, name=args.name, registry=args.registry)
                print(f"installed: {result['name']} -> {result['path']} ({result['main']})")
                return 0
            if args.pkg_cmd == "list":
                rows = list_packages(root)
                if not rows:
                    print("no packages installed")
                else:
                    for row in rows:
                        status = "ok" if row["installed"] else "missing"
                        print(f"{row['name']}@{row['version']}  main={row['main']}  {status}")
                return 0
            if args.pkg_cmd == "remove":
                remove_package(root, args.name)
                print(f"removed: {args.name}")
                return 0
    except SBGError as e:
        print(format_diagnostic(e, source_text=source_text, fallback_filename=fallback_filename), file=sys.stderr)
        return 1
    except OSError as e:
        print(f"FileError: {e}", file=sys.stderr)
        return 1
    return 2

