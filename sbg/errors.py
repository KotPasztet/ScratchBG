from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional, Tuple

# =============================================================================
# Errors
# =============================================================================

class SBGError(Exception):
    pass

class LexError(SBGError):
    pass

class ParseError(SBGError):
    pass

class RuntimeSBGError(SBGError):
    pass

class CompileError(SBGError):
    pass

class ImportSBGError(SBGError):
    pass

class PackageError(SBGError):
    pass

def attach_location(exc: Exception, node: Any) -> Exception:
    """Attach source location from an AST node to an SBGError if it has none."""
    if isinstance(exc, SBGError) and getattr(exc, "line", None) is None and node is not None:
        for attr in ("filename", "line", "col"):
            if hasattr(node, attr):
                setattr(exc, attr, getattr(node, attr))
    return exc

def set_location(node: Any, token: Token) -> Any:  # Token is available at runtime after lexer class creation; type hint postponed
    setattr(node, "filename", getattr(token, "filename", None) or "<source>")
    setattr(node, "line", token.line)
    setattr(node, "col", token.col)
    return node

def _parse_location_from_message(message: str) -> Tuple[Optional[str], Optional[int], Optional[int], str]:
    m = re.match(r"^(.*?):(\d+):(\d+):\s*(.*)$", message, flags=re.S)
    if not m:
        return None, None, None, message
    return m.group(1), int(m.group(2)), int(m.group(3)), m.group(4)

def format_diagnostic(error: Exception, *, source_text: str = "", fallback_filename: str = "<source>") -> str:
    """Pretty, compiler-style error output with file/line/column and a caret."""
    raw = str(error)
    filename = getattr(error, "filename", None)
    line = getattr(error, "line", None)
    col = getattr(error, "col", None)
    message = raw
    if line is None or col is None:
        pf, pl, pc, pm = _parse_location_from_message(raw)
        if pl is not None:
            filename = filename or pf
            line, col, message = pl, pc, pm
    filename = filename or fallback_filename
    title = error.__class__.__name__
    if line is None or col is None:
        return f"{title}: {message}"

    out = [f"{title}: {message}", f"  --> {filename}:{line}:{col}"]
    diag_source = source_text
    # When an imported file raises an error, the main file's source_text is not useful.
    # Try to load the exact file named in the diagnostic so the caret still points at
    # the real failing import/library line.
    if filename and filename not in (fallback_filename, "<source>"):
        try:
            diag_source = Path(filename).read_text(encoding="utf-8")
        except OSError:
            pass
    if diag_source:
        lines = diag_source.splitlines()
        if 1 <= int(line) <= len(lines):
            src = lines[int(line) - 1]
            gutter = str(line)
            caret_col = max(1, min(int(col), len(src) + 1))
            out.append(f"   |")
            out.append(f"{gutter:>3} | {src}")
            out.append(f"   | {' ' * (caret_col - 1)}^")
    return "\n".join(out)
