# StageBG — C++-style language lowered to vanilla Scratch

StageBG is not "Scratch blocks written as text". It is a C++-style source language that compiles higher-level programming constructs into vanilla Scratch `.sb3` projects.

The goal is professional workflow:

```bash
python3 sbg.py run examples/cpp_struct_flat_generic_demo.sbg --input go --fast
python3 sbg.py compile examples/cpp_struct_flat_generic_demo.sbg build/structs.sb3
```

Open the generated `.sb3` in vanilla Scratch. For Scratch editor Turbo Mode, hold **Shift** and click the **green flag**. StageBG also emits warp/custom blocks where safe, but the editor's global Turbo Mode is controlled by the user, not saved inside the `.sb3`.

## What this project is supposed to be

Bad goal:

```text
Scratch block names written as text
```

Correct goal:

```text
C++-style code -> compiler lowering -> vanilla Scratch blocks
```

Example:

```cpp
#include <bits/stdc++.h>
using namespace std;

struct Edge {
    int to;
    int cost;
};

vector<vector<Edge>> graph;

int sum_edges(vector<Edge> edges) {
    int s = 0;
    for (Edge e : edges) {
        s += e.cost;
    }
    return s;
}

int main() {
    graph.resize(3);

    Edge e;
    e.to = 1;
    e.cost = 7;
    graph[0].push_back(e);

    return sum_edges(graph[0]);
}
```

Scratch has no structs, no nested vectors, no local variables, no return values, and no references. StageBG lowers those concepts into ordinary Scratch lists, variables, custom blocks, and generated helper logic.

## The optimizer: `-O0` … `-O3`

StageBG is gaining a structured IR + optimizer that sits between the lowered AST and Scratch block emission. It is exposed through `-O` / `--opt-level` on both `compile` and `run`:

```bash
python3 sbg.py compile examples/main.sbg -O3
python3 sbg.py run examples/main.sbg -O3
```

`--opt-level` takes `{0,1,2,3}` and defaults to `0`. The levels are strictly cumulative:

| Level | Behavior |
|---|---|
| `-O0` (default) | Today's compiler, unchanged. The generated `project.json` is **byte-identical** to a build without the optimizer — patch-order effects, warp mutations, and the Terminal monitor id included. |
| `-O1` | Safe, intra-procedure passes: constant folding/propagation, strength-reduction of the nested-vector `at0`/`vec_size` string-scanner helpers, join/string-concat fusion, and dead-code elimination. |
| `-O2` | Adds structural memory passes: struct SROA + dead-field elimination, dead list/variable elimination, small-proc inlining under a block budget, and loop-invariant hoisting. |
| `-O3` | Aggressive and Scratch-cost-targeted: SoA flat-list fusion for `vector<vector<Struct>>`, bounded vector-to-scalar promotion, warp/turbo placement analysis, and terminal-output minimization. |

`run` keeps its compile-then-run guarantee and passes the level through, so `sbg run -O3` verifies the optimized project still compiles to Scratch before executing natively.

Key passes, and the vanilla-Scratch cost each one attacks:

- **struct SROA / dead-field elimination** — removes unused flattened `name.field` variables (each variable costs an init block, a monitor, and storage);
- **vector / `vector<Struct>` SoA + flat lists** — row-size elision and index demotion for the nested-vector flat field tables;
- **dead list/variable elimination** — drops lists and variables whose contents are never read, while never touching `Terminal`, the embedded-file tables, Action return vars, or anything the user exposes via `showList`/`showVariable`;
- **constant folding + DCE** — removes constant reporters and dead control-flow stacks;
- **small-proc inlining with a block budget** — inlines only when the block count strictly decreases (never a size increase), and never across warp/timing boundaries;
- **loop-invariant hoisting** — moves invariant work out of `forever`/`while`/`repeat`, the dominant per-frame win in game loops;
- **string/join fusion** — collapses `join` chains at compile time;
- **warp/turbo placement** — keeps warp on pure custom blocks and records which procs are pure, giving `--no-turbo` a factual basis without de-warping hot pure procs;
- **terminal-output minimization** — batching of consecutive `Terminal` appends.

The philosophy: this is **not a GCC copy**. The optimizer targets vanilla Scratch's real costs — block count, list/variable count, monitor count, warp/turbo placement, terminal output volume, and per-frame cost. `-O3` never changes program semantics: for every program, the native runner's output at `-O3` is identical to `-O0`.

### `--opt-terminal-batch` (opt-in, O3 only)

The one pass that changes observable output shape (Terminal list item granularity) is **off by default even at `-O3`** and enabled only by `--opt-terminal-batch` on `compile`. Consecutive `Terminal` appends are merged into a single joined append with a sentinel separator, and the native runner's log mirror is taught to split that sentinel, keeping native stdout byte-identical while reducing Scratch list-append blocks and Terminal monitor churn:

```bash
python3 sbg.py compile examples/terminal_visibility_demo.sbg build/t.sb3 -O3 --opt-terminal-batch
```

The authoritative spec for the IR, the full pass catalog, and the regression gates is [`docs/optimizer-design.md`](docs/optimizer-design.md).

## Package layout

The compiler was split from a single `sbg.py` into a clean `sbg/` package. The root `sbg.py` is now a **thin CLI shim** so `python3 sbg.py ...` keeps working exactly as it did before:

- `sbg/globals.py` — shared constants and the late-bound patched entry points (`VERSION`, builtin tables, `Terminal` list identity).
- `sbg/errors.py` — the exception hierarchy (`LexError`, `ParseError`, `CompileError`, `RuntimeSBGError`, `ImportSBGError`, `PackageError`) and diagnostic formatting.
- `sbg/ast.py` — the lexer and AST node dataclasses.
- `sbg/parser.py` — the parser and import resolution; source text → `Program`.
- `sbg/scratch.py` — `ScratchBuilder`, the low-level Scratch block JSON emitter.
- `sbg/compiler.py` — `Compiler` (AST → `.sb3` project dict) plus the `inspect`/`unpack`/`write` helpers.
- `sbg/runtime.py` — the native Python interpreter used by `sbg run`.
- `sbg/packages.py` — the SBG package manager (`sbg pkg init/install/list/remove`).
- `sbg/cli.py` — the argparse CLI surface (`run`/`compile`/`inspect`/`unpack`/`pkg`).
- `sbg/_patches.py` — the layered monkeypatch chain that assembles the final compiler behavior; imported at package import time and re-exporting the patched public API.
- `sbg/__init__.py` — imports the patch chain (which runs on import) and publishes `main` / `VERSION`.

## Terminal API: dynamic visibility and prompt control

StageBG adds runtime control over the fullscreen terminal list and the `ask and wait` input prompt.

```cpp
terminal.hide();        // hide Terminal list monitor
terminal.show();        // show Terminal list monitor
terminal.toggle();

terminal.hidePrompt();  // disable the next ask-and-wait prompt
terminal.showPrompt();  // enable it again
terminal.hideAll();     // hide terminal + disable prompt
terminal.showAll();     // show terminal + enable prompt

int a = terminal.visible();
int b = terminal.promptVisible();
```

Aliases also work through `console`:

```cpp
console.hide();
console.show();
console.hidePrompt();
console.showPrompt();
console.hideAll();
console.showAll();
```

Important vanilla Scratch detail: an already-open `ask and wait` bubble cannot be closed mid-question. `hidePrompt()` prevents the **next** prompt from appearing. This is the clean Scratch-compatible way to pause console input while a program runs its own UI, animation, cutscene, menu, or background process.

Hiding the prompt is a real gate in the generated `.sb3`, not just a native-mode convenience: while input is disabled, the project skips the `ask and wait` block entirely.

Demo:

```bash
python3 sbg.py run examples/terminal_visibility_demo.sbg --input status --fast
python3 sbg.py compile examples/terminal_visibility_demo.sbg build/terminal_visibility_demo.sb3
```

## Generic features implemented

### C++-style surface syntax

Supported subset includes:

```cpp
#include <bits/stdc++.h>
using namespace std;

int x = 3;
double y = 1.5;
string s = "abc";
bool ok = true;
const double pi = 3.14159;

int f(int a, int b) {
    if (a < b) return b;
    return a;
}

int main() {
    for (int i = 0; i < 10; i++) { }
    while (ok) { break; }
    return 0;
}
```

### Structs

```cpp
struct Point {
    int x;
    int y;
};

Point p;
p.x = 10;
p.y = 20;
```

Struct support is generic. Field access is lowered by the compiler. It is not tied to any specific struct name.

### Nested vectors lowered to flat Scratch data

```cpp
vector<vector<int>> mat;
mat.resize(3);
mat[0].push_back(10);
mat[0].push_back(20);
int x = mat[0][1];
```

For structs:

```cpp
struct Edge { int to; int cost; };
vector<vector<Edge>> graph;
graph.resize(5);
graph[0].push_back(edge);
int c = graph[0][0].cost;
```

Internally this becomes Scratch lists such as row metadata plus flat field tables. The public syntax stays C++-like.

### Methods instead of procedural wrappers

Use dot syntax in normal code:

```cpp
v.push_back(x);
v.pop_back();
v.size();
v.empty();
v.sort();
v.reverse();
v.lower_bound(x);
v.binary_search(x);

pq.push(priority, value);
pq.top();
pq.pop();
pq.empty();

dsu.init(n);
dsu.find(x);
dsu.unite(a, b);
dsu.same(a, b);

fw.init(n);
fw.add(i, delta);
fw.sum(i);
fw.range(l, r);
```

The older underscore functions are internal ABI/compatibility helpers. Public examples should use dot methods.

### Comments preserved into Scratch

```cpp
// line comment
/* block comment */
```

The compiler keeps source comments and emits Scratch comments where possible, so the loaded project is easier to inspect.

### Layout formatting

The compiler places generated top-level scripts in columns/rows instead of dumping every block at the same coordinates. The goal is that opening the `.sb3` in Scratch is debuggable, not a pile of blocks.

## Libraries

### `std`

Core runtime helpers, console, strings, vectors, random, time/delta, files, pen, sprite control.

```cpp
#include <std>
```

or:

```cpp
import "std";
```

### `bits`

Algorithmic containers and helpers similar in spirit to `bits/stdc++.h`, but implemented in vanilla Scratch-compatible SBG.

```cpp
#include <bits/stdc++.h>
```

Includes generic structures such as priority queue, deque, stack, queue, DSU, Fenwick tree, sorting/search helpers, math helpers.

## Compile-time files

Scratch cannot read arbitrary files at runtime, so StageBG embeds files into Scratch lists while compiling:

```bash
python3 sbg.py compile examples/files_kv_demo.sbg build/files.sb3 --embed data/config.txt:config.txt
python3 sbg.py run examples/files_kv_demo.sbg --input go --embed data/config.txt:config.txt
```

Then code can read from the embedded virtual file table.

## Keyboard / key handling

StageBG supports vanilla Scratch keyboard input in two forms.

### Polling pressed keys

```cpp
on action(input) {
    if (keyboard.pressed("space")) {
        return "space is currently pressed";
    }

    if (keys.down("left arrow")) {
        return "left arrow is down";
    }

    return "no key";
}
```

Aliases:

```cpp
keyPressed("space");
keyboard.pressed("space");
keys.down("left arrow");
key.isPressed("a");
```

This compiles to vanilla Scratch `key [x] pressed?` sensing blocks.

### Key event hats

```cpp
on key "space" {
    log("space pressed");
}

on key("left arrow") {
    log("left pressed");
}

sprite Player {
    on key "right arrow" {
        changeX(10);
    }
}
```

This compiles to vanilla Scratch `when [x] key pressed` hats. It works on Stage and on generated empty sprites.

Supported normalized names include:

```text
space, any, up arrow, down arrow, left arrow, right arrow, enter
```

`up`, `down`, `left`, `right`, `up_arrow`, `left_arrow`, etc. are accepted as aliases and lowered to Scratch menu names.

### Native runner key simulation

The native Python runner is headless, so it cannot read live keyboard state while running a Scratch-style polling expression. For tests, set `SBG_KEYS`:

```bash
SBG_KEYS="space,left arrow" python3 sbg.py run examples/keyboard_demo.sbg --input go --fast
```

The same code compiles to real live keyboard sensing in Scratch.

## Vanilla Scratch limits

StageBG should fail with a compiler error when something cannot be represented safely in vanilla Scratch. Current target is a practical C++ subset, not full ISO C++.

Unsupported or limited:

- raw pointers,
- references with aliasing semantics,
- inheritance,
- exceptions,
- full templates,
- arbitrary STL internals,
- true runtime file I/O,
- true object memory model.

Supported direction:

- simple C++-style algorithmic code,
- `vector`, nested `vector`, struct values,
- returns,
- loops,
- `break` / `continue`,
- methods via dot syntax,
- std/bits style libraries,
- dynamic terminal/prompt control,
- keyboard polling and key event hats,
- vanilla Scratch compilation.
