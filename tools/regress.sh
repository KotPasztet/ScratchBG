#!/usr/bin/env bash
# Regression harness for StageBG: compile + natively run every example and
# compare against the golden outputs captured from the last known-good build.
#
#   usage: bash tools/regress.sh [golden.txt]
#
# Exit 0 iff (a) every example compiles to .sb3 and (b) every example's native
# run output matches the golden file. Pass a custom golden file to rebaseline.
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GOLDEN="${1:-$ROOT/tools/golden.txt}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

fail=0

echo "== compile =="
for f in "$ROOT"/examples/*.sbg; do
  n="$(basename "$f" .sbg)"
  if python3 "$ROOT/sbg.py" compile "$f" "$WORK/$n.sb3" >"$WORK/$n.compile.log" 2>&1; then
    printf '  OK   %s\n' "$n"
  else
    printf '  FAIL %s -> %s\n' "$n" "$(tail -1 "$WORK/$n.compile.log")"
    fail=1
  fi
done

echo "== run (native) =="
: >"$WORK/actual.txt"
for f in "$ROOT"/examples/*.sbg; do
  n="$(basename "$f" .sbg)"
  # Normalize the native run output: cap at the first 3 lines, drop the
  # `> go` prompt echo, strip the `=> ` action-return marker, and strip the
  # volatile `dt=... fps=...` timing tail that turbo_dt_demo emits.
  out="$(python3 "$ROOT/sbg.py" run "$f" --input go --fast 2>&1 | head -3 | sed -E -e '/^> go[[:space:]]*$/d' -e 's/^=>[[:space:]]*//' -e 's/ dt=[0-9.eE+-]+ fps=[0-9.eE+-]+//' | tr '\n' ' ' | sed -E 's/[[:space:]]+$//')"
  printf '%s :: %s\n' "$n" "$out" >>"$WORK/actual.txt"
done

if ! diff -u "$GOLDEN" "$WORK/actual.txt"; then
  echo "run output diverged from golden file"
  fail=1
fi

if [ "$fail" -eq 0 ]; then
  echo "PASS: compile + run match golden."
else
  echo "FAIL: see above."
fi
exit "$fail"
