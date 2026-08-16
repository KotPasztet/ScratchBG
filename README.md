# StageBG patch23 — C++-style language lowered to vanilla Scratch

StageBG is not “Scratch blocks written as text”. It is a C++-style source language that compiles higher-level programming constructs into vanilla Scratch `.sb3` projects.

The goal is professional workflow:

```bash
python3 sbg_patch23.py run examples/cpp_struct_flat_generic_demo.sbg --input go --fast
python3 sbg_patch23.py compile examples/cpp_struct_flat_generic_demo.sbg build/structs.sb3
```

Open the generated `.sb3` in vanilla Scratch. For Scratch editor Turbo Mode, hold **Shift** and click the **green flag**. StageBG also emits warp/custom blocks where safe, but the editor’s global Turbo Mode is controlled by the user, not saved inside the `.sb3`.

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

## Terminal API: dynamic visibility and prompt control

Patch23 adds runtime control over the fullscreen terminal list and the `ask and wait` input prompt.

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

Demo:

```bash
python3 sbg_patch23.py run examples/terminal_visibility_demo.sbg --input status --fast
python3 sbg_patch23.py compile examples/terminal_visibility_demo.sbg build/terminal_visibility_demo.sb3
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
python3 sbg_patch23.py compile examples/files_kv_demo.sbg build/files.sb3 --embed data/config.txt:config.txt
python3 sbg_patch23.py run examples/files_kv_demo.sbg --input go --embed data/config.txt:config.txt
```

Then code can read from the embedded virtual file table.

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
- vanilla Scratch compilation.

## Patch23 correction

Patch23 adds a real Scratch-compatible input-gate around the generated terminal loop. Hiding the prompt no longer means “just ignore input in native mode”; the generated `.sb3` skips the `ask and wait` block while input is disabled.

## Patch24 — keyboard / key handling

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
SBG_KEYS="space,left arrow" python3 sbg_patch24.py run examples/keyboard_demo.sbg --input go --fast
```

The same code compiles to real live keyboard sensing in Scratch.
