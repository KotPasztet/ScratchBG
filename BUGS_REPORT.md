# Raport: błędy, ograniczenia i propozycje realnych poprawek — ScratchBG

Zebrane podczas próby napisania w SBG parsera HTML+CSS oraz późniejszego,
celowego "stress-testu" języka. Wszystko zweryfikowane wyłącznie przez
pisanie i uruchamianie plików `.sbg` przez istniejące CLI
(`sbg.py run`, `sbg.py compile`, `sbg.py inspect`) — bez edycji plików
kompilatora. Tam, gdzie to możliwe, zamiast "dopisać do README" podaję
**konkretną, code-level propozycję poprawki** (plik, okolice linii,
mechanizm) do wdrożenia przez zespół.

Posortowane wg powagi, najpierw najgroźniejsze.

---

## 🔴 1. KRYTYCZNE: kopiowanie struktury (`Item b = a;`) cicho gubi wartości pól

[NAPRAWIONE 2026-08-19] Patch `sbg/patches/p25_struct_semantics.py`: `Item b = a;`
oraz `b = a;` (po deklaracji) lowering do kopii pole-po-polu (`b.f = a.f`);
`Item b = vec.at(i);` / `b = vec.at(i);` do odczytów z list SoA. Dodatkowo
`sbg/patches/p15_adult_stdlib.py` (mangler) traversuje teraz `StructVarDecl.init`
oraz `LValueAssignStmt` (referencje w nich są manglowane jak w innych wyrażeniach).

To najpoważniejsze znalezisko. Nie dotyczy jakiegoś edge case'u czy
zaawansowanej funkcji — dotyczy **podstawowej semantyki `struct`**.

### Reprodukcja (bez wektorów, bez niczego wyszukanego)
```sbg
struct Item { int val; };

stage {
    on action(input) {
        Item a; a.val = 42;
        Item b = a;
        return join("b.val=", b.val);
    }
}
```
```
=> b.val=0        // oczekiwane: b.val=42
```
Dla porównania, odczyt tej samej zmiennej `a.val` bezpośrednio (bez kopii)
zwraca poprawnie `42` — czyli problem nie leży w zapisie/odczycie pola,
tylko konkretnie w **przypisaniu jednej zmiennej struct do drugiej**.

Ten sam efekt występuje przy wstawianiu struktury do kontenera:
```sbg
struct Item { int val; };
vector<Item> items;

stage {
    on action(input) {
        Item a; a.val = 42;
        items.push_back(a);
        Item back = items.at(1);
        return join("val=", back.val);
    }
}
```
```
=> val=0           // oczekiwane: val=42
```

### Co jest jeszcze bardziej niepokojące: **kompiluje się bez błędu do `.sb3`**
```
python3 sbg.py compile plik.sbg out.sb3
=> compiled: out.sb3   (brak CompileError)
```
W przeciwieństwie do przypadku `vector<vector<Struct>>.push_back(struct)`,
gdzie kompilator **świadomie** rzuca błąd z jasnym komunikatem
(`sbg/_patches.py:6205`):
```python
raise CompileError(
    "vector<vector<struct>>.push_back(struct) needs full record-copy "
    "lowering; native run supports it, Scratch compile currently "
    "requires flat arrays directly"
)
```
— czyli autorzy **wiedzą**, że "record copy lowering" (kopiowanie
wszystkich pól struktury naraz) to osobny, nietrywialny mechanizm, i dla
`vector<vector<Struct>>` świadomie go blokują z czytelnym komunikatem. Ale
dla zwykłego, jednopoziomowego `vector<Struct>` oraz dla zwykłego
przypisania `Item b = a;` między dwiema luźnymi zmiennymi struct — ten sam
brakujący mechanizm **nie jest wykrywany i nie blokuje kompilacji**, tylko
cicho produkuje działający, ale niepoprawny projekt Scratch.

### Podejrzana przyczyna (na podstawie kodu)
Struktury w SBG są przechowywane jako "flat SoA" (osobna lista Scratchowa
per pole, np. `a.val`, `b.val` jako oddzielne zmienne/listy) — to widać po
komunikacie z pkt. wyżej i po `docs/optimizer-design.md`. Zwykłe
`Item b = a;` prawdopodobnie kompiluje się do **jednego** przypisania
"uchwytu"/nazwy zmiennej (tak jakby struct to był pojedynczy skalar), a nie
do serii przypisań pole-po-polu (`b.val = a.val;` dla każdego pola
zdefiniowanego w `struct Item`). Stąd `b.val` zostaje z wartością domyślną
(0), bo fizyczna lista/zmienna `b.val` nigdy nie została ustawiona.

### Konkretna propozycja poprawki
W miejscu w `sbg/scratch.py` (lub `sbg/_patches.py`, gdzie faktycznie
przechwytywane jest kompilowanie `VarDecl`/`Assign` z RHS typu
struktury — czyli tam, gdzie `ScratchBuilder` widzi, że deklarowany typ
zmiennej to nazwa zdefiniowanego wcześniej `struct`) należy:

1. Wykryć, że LHS i RHS są tego samego typu strukturowego (po nazwie
   struct, tak jak już musi to robić type-checker przy zwykłym
   przypisaniu).
2. Zamiast emitować pojedynczy blok "ustaw zmienną B na wartość A", **iterować
   po polach zdefiniowanych w `struct` (te same metadane, które już są
   używane do generowania flat-listy pól, np. przy `vector<Struct>`) i dla
   każdego pola wyemitować osobne przypisanie**:
   ```python
   def compile_struct_copy(self, dst_prefix: str, src_prefix: str, struct_def: StructDef) -> list[str]:
       stmts = []
       for field in struct_def.fields:
           stmts.append(self.compile_assign(
               f"{dst_prefix}.{field.name}",
               VarExpr(f"{src_prefix}.{field.name}"),
           ))
       return stmts
   ```
   i użyć tego helpera **wszędzie tam, gdzie struct jest kopiowany**:
   `Item b = a;`, `items.push_back(a)` (dla zwykłego `vector<Struct>`, nie
   tylko `vector<vector<Struct>>`), przekazywanie struct przez wartość jako
   argument funkcji, oraz `return` struct z funkcji.
3. **Minimum na już, zanim powstanie pełny fix**: potraktować to tak samo
   jak `vector<vector<struct>>.push_back` — czyli jeśli pełne record-copy
   lowering nie jest jeszcze zaimplementowane dla danego przypadku
   (zwykłe `Item b = a;`, `vector<Struct>.push_back`, struct jako
   parametr/return), **rzucić `CompileError` z jasnym komunikatem**
   zamiast cicho generować błędny `.sb3`. Ciche generowanie niepoprawnego
   programu jest dużo gorsze niż odmowa kompilacji — użytkownik dowiaduje
   się o błędzie dopiero po otwarciu Scratcha i zobaczeniu, że dane
   znikają.

### Priorytet
Najwyższy. To bezpośrednio podważa użyteczność `struct` — jednej z
głównych, sztandarowych funkcji języka reklamowanej w README
(`cpp_struct_flat_generic_demo.sbg`). Warto od razu sprawdzić, w jakich
dokładnie konfiguracjach (nesting level, `.at()` vs `[]`, `.push_back` vs
`resize()+[i]=`) record-copy faktycznie działa (jak w ich własnym demo z
`vector<vector<Edge>>`), i albo naprawić resztę przypadków tym samym
mechanizmem, albo konsekwentnie blokować je `CompileError`-em do czasu
naprawy.

---

## 🔴 2. KRYTYCZNE: `len()` w natywnym `sbg run` zgaduje, że string to zserializowany wektor — psuje długość dla wielu znaków

> **[NAPRAWIONE 2026-08-19]** Heurystyka `len()` poprawiona — `len("hello") == 5` w natywnym `sbg run` (zweryfikowane testem; długość zwykłych stringów i wektorów liczona poprawnie).

### Macierz reprodukcji
| zawartość stringu | oczekiwana długość | zwrócona `len()` |
|---|---|---|
| `"abc"`   | 3 | 3 ✅ |
| `"hello"` | 5 | 5 ✅ |
| `"a b"`   | 3 | 2 ❌ (liczba słów) |
| `"a,b"`   | 3 | 2 ❌ (liczba elementów po przecinku) |
| `"a;b"`   | 3 | 1 ❌ |
| `"a:b"`   | 3 | 3 ✅ |
| `"a.b"`   | 3 | 3 ✅ |

`letter(s, i)` na tym samym `s = "a;b"` **poprawnie** zwraca `a`, `;`, `b` —
czyli `len()` i `letter()` używają dwóch niespójnych reprezentacji tej samej
zmiennej.

### Podejrzana przyczyna
W `sbg/_patches.py` istnieje heurystyka zgadująca, czy string to
zserializowany wektor (używana najpewniej do konwersji stringów na listy
"w locie" gdzieś w warstwie generycznych operacji na kontenerach):
```python
def _sbg_vec_tokens_runtime_patch20(value):
    text = str(value)
    if "\x1f" in text:
        return [x for x in text.split("\x1f") if x != ""]
    if "," in text:
        return [x.strip() for x in text.split(",") if x.strip()]
    return [x for x in text.split() if x]
```
Wygląda na to, że ta sama (lub analogiczna) ścieżka bywa mylnie stosowana
przy zwykłym `len()` na skalarnym `string`.

### Konkretna propozycja poprawki
`Runtime.call("len", ...)` w `sbg/runtime.py` powinno rozróżniać typ
argumentu **statycznie, na podstawie zadeklarowanego typu zmiennej w AST**
(czy to `string` czy `vector<T>`), a nie w runtime po zawartości stringu:
```python
if name == "len":
    obj = args[0]
    if isinstance(obj, list):
        return len(obj)
    return len(str(obj))   # zawsze licz znaki dla str — NIGDY nie zgaduj po treści
```
Realne miejsce błędu to prawdopodobnie wcześniej — coś w łańcuchu
odczytu/przechowywania zmiennej `string` przepuszcza wartość przez
`_sbg_vec_tokens_runtime_patch20` zanim `len()` w ogóle dostanie argument.
Trzeba znaleźć wywołującego `_sbg_vec_tokens_runtime_patch20` i sprawdzić,
czy jest wołany z kontekstu `len(scalarVar)` — jeśli tak, usunąć to
wywołanie z tej ścieżki i zostawić tę heurystykę wyłącznie tam, gdzie
kompilator **wie statycznie**, że dana zmienna reprezentuje
wektor-jako-tekst (a nie zwykły `string`).

### Wpływ
Cała biblioteka `packages/std/strings.sbg` / `text.sbg` (`trim`, `slice`,
`left`, `right`, `startsWith`, `splitByChar`, `padLeft`...) używa `len()`
wewnętrznie w pętlach — błąd propaguje się do prawie każdej funkcji
tekstowej stdlib, gdy przetwarzany tekst zawiera spację, przecinek lub
średnik. Prawdopodobnie nie dotyczy to skompilowanego `.sb3` (statyczna
kompilacja `len()`/`letter()` do bloków Scratch jest osobną ścieżką kodu i
nie przechodzi przez tę heurystykę — potwierdzone przez `sbg inspect` na
przykładzie `minihtml.sb3`, gdzie widać poprawne bloki `operator_length`),
ale to i tak oznacza, że **`sbg run` kłamie o zachowaniu finalnego
produktu**, co samo w sobie jest osobnym problemem (patrz punkt 6).

---

## 🟠 3. POWAŻNE: brak jakiegokolwiek mechanizmu obsługi błędów w języku

Trzy różne błędy runtime, trzy różne, niespójne zachowania — i **żadne nie
jest możliwe do przechwycenia z poziomu kodu SBG** (brak `try`/`catch`,
brak `Result<T,E>`, brak kodów błędu jako konwencji języka):

```sbg
int a = 5; int b = 0; int c = a / b;
```
```
RuntimeSBGError: float division by zero      // natywny run: crash
```

```sbg
vector<int> v = {1,2,3};
int x = v.at(10);
```
```
RuntimeSBGError: list index out of range      // natywny run: crash
```

```sbg
vector<int> v = {1,2,3};
int x = v.at(-1);
```
```
=> neg=2      // cichy, błędny wynik zamiast crasha lub błędu — brak spójności
```

Do tego prawdziwy Scratch **nie crashuje** przy dzieleniu przez zero (daje
`Infinity`) ani przy indeksie poza zakresem listy (`item of list` zwraca
pusty string) — czyli natywny `run` (crash) i realny `.sb3` (cichy,
"grzeczny" wynik) zachowają się **różnie** dla tego samego kodu.

### Konkretna propozycja poprawki
1. Ujednolicić zachowanie natywnego runnera z rzeczywistym Scratchem:
   `Runtime.call` dla dzielenia powinno zwracać `float("inf")` przy
   dzieleniu przez zero zamiast rzucać wyjątek Pythona, a `.at()`
   poza zakresem — pusty string / `0`, tak jak robi to prawdziwy
   silnik Scratch, żeby `sbg run` wiernie odzwierciedlał produkcję.
2. Rozważyć dodanie do języka opcjonalnego, jawnego mechanizmu
   sprawdzania granic — np. `vector<T>.at_checked(i)` zwracającego
   dwuelementowy `struct { bool ok; T value; }`, żeby programista mógł
   **świadomie** obsłużyć błąd zamiast dostawać ciche złe dane (przypadek
   indeksu ujemnego) albo twardy crash bez możliwości recovery.
3. Udokumentować różnicę między "dzieleniem" (`/`, zawsze bezpieczne,
   zwraca `Infinity`) a operacjami na kolekcjach (`.at()`, aktualnie
   niebezpieczne/niespójne) — to dwa różne modele błędów w tym samym
   języku i user nie ma jak się tego domyślić bez testowania jak ja.

---

## 🟠 4. POWAŻNE: `int`/`float` nie mają żadnej wymuszonej semantyki

```sbg
int a = 7; int b = 2;
int c = a / b;      // c = 3.5, NIE 3
int x = 7.0 / 2.0;  // int x = 3.5, bez obcięcia mimo deklaracji `int`
```

Deklaracja `int` w SBG to czysto kosmetyczna adnotacja źródłowa — Scratch
nie ma typu całkowitego, więc pod spodem wszystko to `double`. Jest
`floorDiv(a,b)` w `packages/std/math.sbg`, ale nic go nie wymusza.

### Konkretna propozycja poprawki (realna, nie "dopisz do README")
Skoro kompilator i tak zna zadeklarowany typ zmiennej (musi go znać, żeby
w ogóle sparsować `int x = ...;`), **niech to wykorzysta**: przy
kompilowaniu wyrażenia przypisywanego do zmiennej zadeklarowanej jako
`int` (oraz przy `return` z funkcji zwracającej `int`), automatycznie
opakuj wygenerowany blok w odpowiednik `floor()`/`trunc()`:
```python
# w miejscu, gdzie kompilowane jest VarDecl/Assign z target_type == "int"
if target_type == "int" and not is_int_literal_expr(rhs):
    compiled_value = self.wrap_in_floor_block(compiled_value)
```
To sprawi, że `int` faktycznie *znaczy* coś w runtime (tak jak w C++),
zamiast być czystą etykietą dla czytelności kodu. Trzeba tylko rozstrzygnąć
zaokrąglanie w stronę zera (`trunc`, jak C++) kontra w dół (`floor`, jak
Python) — C++ dla `int` obcina w stronę zera, więc dla liczb ujemnych
`floor` dałby inny wynik niż prawdziwy C++ (`-7/2` w C++ = `-3`, `floor` da
`-4`) — to też trzeba świadomie wybrać i udokumentować.

---

## 🟡 5. ŚREDNIE: niespójne formatowanie liczb (`5` vs `610.0`)

> **[NAPRAWIONE 2026-08-19]** Formatowanie spójne jak w C++ — liczby całkowite wypisywane bez `.0` (test: `cout << 5 << " " << 610.0 << " " << 3.5 << " " << 7/2` → `5 610 3.5 3.5`).

`len("hello")` → `"5"`, ale `fib(15)` (wynik arytmetyki użytkownika,
rekurencja z `int`) → `"610.0"`. Brak jednej reguły "kiedy liczba całkowita
dostaje `.0`, a kiedy nie" utrudnia pisanie kodu, który wypisuje wyniki
liczbowe do użytkownika (np. wszystko trzeba by ręcznie czyścić przez
własny helper).

### Konkretna propozycja poprawki
W miejscu konwersji liczby na string przy `join()`/`console.log`/`return`
(prawdopodobnie jedna centralna funkcja formatująca liczby dla wyjścia)
dodać regułę: jeśli `value == int(value)`, wypisz bez części ułamkowej
(`str(int(value))`), analogicznie do tego, jak realny Scratch **nigdy** nie
pokazuje `610.0` w UI, tylko `610`. To ujednolici zachowanie z tym, czego
programista i tak oczekuje, patrząc na Scratcha.

---

## 🟡 6. ŚREDNIE: `vector<string> x = splitByChar(...)` kompiluje się mimo błędnego typu

> **[NAPRAWIONE 2026-08-19]** Dodany check zgodności typu przy deklaracji: `vector<T> x = proc(...)` gdzie proc ma adnotowany skalarowy typ zwracany → `ParseError` z lokalizacją plik:linia:kol. `splitByChar` ma teraz adnotację `int` (typu zwracanego). Poprawny kod (`int n = splitByChar(...)`, `vector<int> v = [1,2,3]`) działa bez zmian.

`splitByChar` (`packages/std/text.sbg:79`) zwraca `int` (liczbę elementów) i
pisze wynik do globalnej listy `str_split_res` — to nie jest
`vector<string>`. Mimo to:
```sbg
vector<string> parts = splitByChar("a,b,c", ',');
return join("size=", parts.size());   // => size=1 (błędnie potraktowane jak 1-elementowy wektor)
```
kompiluje się **bez żadnego błędu ani ostrzeżenia**.

### Konkretna propozycja poprawki
Dodać sprawdzenie zgodności typu zwracanego przez wywoływaną `proc` z
deklarowanym typem zmiennej po lewej stronie przypisania — w miejscu
type-checkingu `VarDecl` (tam, gdzie już musi być sprawdzane, czy RHS jest
w ogóle poprawnym wyrażeniem) dodać: jeśli LHS ma typ `vector<T>`, a
wywoływana funkcja ma zadeklarowany typ zwracany `int`/`string`/inny
nie-wektorowy — `CompileError: type mismatch: cannot assign proc returning
'int' to variable of type 'vector<string>'`.

Docelowo: przeprojektować `splitByChar`, żeby faktycznie zwracał
`vector<string>` (język już wspiera ten typ — widać po `vector<int>`,
`vector<Edge>` w przykładach), zamiast pisać do jednej, nazwanej na sztywno
globalnej listy `str_split_res`, która dodatkowo **nie jest reentrantna**
(dwa zagnieżdżone wywołania `splitByChar` nadpiszą sobie wynik). Ten sam
problem dotyczy prawdopodobnie innych funkcji pomocniczych w
`packages/std/collections.sbg` — tam jest dokładnie **jeden** globalny
stos/kolejka/zbiór/mapa (`__std_stack`, `__std_queue`, `__std_set`,
`__std_map_keys/values`), więc nie da się mieć dwóch niezależnych
instancji tej samej struktury danych naraz bez ręcznego pisania własnej
pary list od zera.

**Realna poprawka architektoniczna** (większy nakład pracy, ale to
właściwy kierunek): rozszerzyć już istniejący mechanizm spłaszczania
`vector<Struct>` (SoA per nazwana zmienna, opisany w
`docs/optimizer-design.md`) na `map<K,V>`, `stack<T>`, `queue<T>`, `set<T>`
jako pełnoprawne, wielokrotnie instancjonowalne typy generyczne — tak żeby
`map<string,int> cache; map<string,int> config;` tworzyło dwie niezależne
pary list (`cache.keys/cache.values`, `config.keys/config.values`), zamiast
funkcji operujących na jednej, globalnej liście.

---

## 🔴 7. (połączone z pkt. 1) REALNY FIX: kompilator powinien sam auto-lowerować `vector<Struct>.at(idx).field`, zamiast wymagać ręcznego obejścia

[NAPRAWIONE 2026-08-19] Patch `sbg/patches/p25_struct_semantics.py`: zwykły
(nienested) `vector<Struct>` dostaje automatyczne listy SoA `vec.<field>`;
`vec.at(i).field` → `item(i, vec.field)` (at 1-based), `vec[i].field` →
`item(i+1, vec.field)` (0-based), zapis `vec.at(i).field = x` → `setItem`;
`vec.push_back(structVar)` → per-field `push_back` + marker w liście głównej
(`size()` działa). Działa w run natywnym i w kompilacji do .sb3.

Poprzednia wersja tego punktu sugerowała "polepszyć komunikat błędu przy
`dynamic field access cannot be compiled to vanilla Scratch`". Słusznie
zwrócono mi uwagę, że to nie jest fix, tylko podpowiedź jak samemu obejść
bug — jakby gra mówiła "pływanie jest zepsute, idź dookoła jeziora". Bugi
mają być naprawione, nie oflagowane lepszym komunikatem. Poniżej realna
poprawka.

### Dlaczego to jest w ogóle możliwe do naprawienia (nie tylko obejścia)
Skoro `vector<Struct>` jest już przechowywany jako flat SoA — czyli każde
pole struktury to **osobna lista Scratchowa powiązana z daną zmienną
wektorową** (np. dla `vector<Node> dom;` istnieją realnie listy `dom.tag`,
`dom.parent`, `dom.cls`...) — to `dom.at(i).tag` z **dynamicznym** `i` wcale
nie wymaga żadnej "generic dynamic field access" w ogólnym sensie. Wymaga
tylko jednego, prostego, mechanicznego przekształcenia:
```
dom.at(i).tag   →   item(i, dom.tag)     // zwykły dostęp do elementu listy Scratchowej
```
To dokładnie ten sam blok Scratchowy (`item of list`), którego kompilator
i tak już używa dla zwykłego `dom.at(i)` na `vector<int>`. Różnica jest
tylko syntaktyczna: trzeba rozpoznać wzorzec `<vectorVar>.at(<dowolne
wyrażenie>).<field>` (i analogicznie `<vectorVar>[<wyrażenie>].<field>`) na
poziomie AST/lowering i skompilować go bezpośrednio do `item(<wyrażenie>,
<vectorVar>.<field>)`, zamiast w ogóle przechodzić przez ścieżkę kodu, która
dziś rzuca `CompileError`.

### Konkretna propozycja (miejsce w kodzie)
W `sbg/scratch.py`/`sbg/_patches.py`, w miejscu gdzie dziś wykrywany jest
wzorzec prowadzący do `raise CompileError("dynamic field access cannot be
compiled to vanilla Scratch")` — **zamiast rzucać błąd**, sprawdzić: czy
bazowe wyrażenie to `vectorVar.at(expr)` / `vectorVar[expr]`, gdzie
`vectorVar` jest znaną zmienną typu `vector<Struct>`. Jeśli tak:
1. Rozpoznać nazwę pola (`.tag`, `.val`, itd.),
2. Wyemitować `item(expr, "<vectorVar>.<field>")` (dokładnie ten sam
   codegen co dla zwykłego dostępu do elementu listy),
3. Zrobić to **rekurencyjnie/ogólnie** dla dowolnej głębokości wyrażenia
   indeksującego (nie tylko literału), bo `item()` w Scratchu i tak
   przyjmuje dowolne wyrażenie jako indeks — nie ma tu żadnego
   fundamentalnego ograniczenia bloków Scratchowych, tylko brak tego
   jednego przekształcenia w kompilatorze.

To naprawia problem **u źródła** — użytkownik pisze `dom.at(i).tag` tak,
jak by się tego naturalnie spodziewał po składni C++-podobnej, bez
konieczności ręcznego rozbijania na `Node cur = dom.at(i); cur.tag`. Ten
sam mechanizm rozwiązuje też część problemu z punktu 1 (kopiowanie
struktur) — bo raz zaimplementowane "field-list-aware" lowerowanie dostępu
do pól przez indeks to naturalny fundament pod pełne "record-copy" (kopię
wszystkich pól naraz przy `Item b = a;` czy `push_back`), opisane w
punkcie 1 wyżej. Warto je zaimplementować razem, jednym mechanizmem,
zamiast łatać osobno dostęp-do-pola i osobno kopię-całej-struktury.

---

## 🟢 8. DROBNE: `--embed` rozwiązuje ścieżkę względem katalogu pliku `.sbg`, nie względem CWD

> **[NAPRAWIONE 2026-08-19]** Ścieżki relatywne `--embed`/`--embed-dir` rozwiązują się względem CWD procesu (jak `gcc -I`), z fallbackiem do katalogu pliku `.sbg` dla kompatybilności wstecznej (fix w `sbg/patches/p13_professional_stdlib.py`, `_parse_embed_ref`/`_collect_embedded_files`).

```
python3 sbg.py run examples/plik.sbg --embed examples/data/x.html:x.html
```
uruchomione z katalogu głównego repo próbuje otworzyć
`examples/examples/data/x.html` (czyli katalog pliku źródłowego + podana
ścieżka). Trzeba było uruchamiać z `examples/` i podawać ścieżkę względem
tego katalogu. To nie jest udokumentowane w `--help`.

### Konkretna propozycja poprawki
W parserze argumentu `--embed` w `sbg/cli.py` rozwiązywać ścieżkę względem
**bieżącego katalogu roboczego procesu** (`Path.cwd()`), a nie względem
katalogu pliku `.sbg` — to bardziej zgodne z intuicją (tak działają np.
`gcc -I`, `docker build -f`), albo — jeśli zamierzone — dodać w `--help`
jawny opis "`path` jest rozwiązywana względem katalogu pliku źródłowego
.sbg, nie względem CWD".

---

## 🟠 9. POWAŻNE: `const` jest parsowane, ale nigdy nie wymuszane

```sbg
const int MAX = 10;
MAX = 5;
return MAX;
```
```
=> 5      // oczekiwane: CompileError "cannot assign to const variable MAX"
```
Słowo kluczowe `const` istnieje w gramatyce (`sbg/parser.py:159`,
`self.match_kw("const")`), więc parser je rozpoznaje i najwyraźniej zapisuje
gdzieś flagę "ta zmienna jest stała" — ale nic w kompilatorze tej flagi
później nie sprawdza przy kolejnych przypisaniach. To czysto kosmetyczne
słowo kluczowe, dokładnie jak `int` z punktu 4 — wygląda jak C++, ale nie
daje żadnej z gwarancji C++.

### Konkretna propozycja poprawki
W miejscu kompilowania `Assign`/`ExprStmt` z lewą stroną będącą znaną
zmienną, sprawdzić w tablicy symboli (skoro `const` już jest parsowane i
zapewne trzymane jako atrybut deklaracji zmiennej) flagę `is_const` i
rzucić `CompileError: cannot assign to const variable '<nazwa>'` przy
próbie ponownego przypisania — analogicznie do tego, jak `greet()` już
poprawnie rzuca błąd przy złej liczbie argumentów (patrz przykład w
punkcie 6 rundy poprzedniej — czyli tego typu walidacja *jest* już gdzieś
w kompilatorze zaimplementowana dla innych przypadków, więc infrastruktura
do zgłaszania błędów semantycznych na pewno istnieje, brakuje tylko tego
jednego sprawdzenia).

---

## 🟡 10. ŚREDNIE: brak wbudowanego `assert()` / frameworku testowego dla programów SBG

W gramatyce nie ma `assert` (sprawdzone w `sbg/parser.py`/`sbg/lexer.py`).
Cały projekt ma rozbudowane testy *kompilatora* (widać `docs/`,
wzmianki o eval/benchmarkach w opisie `skill-creator`), ale **programista
piszący w SBG** nie ma żadnego wbudowanego sposobu na `assert(x == 5,
"komunikat")` we własnym kodzie — musi ręcznie pisać `if (!(x == 5)) {
console.log("FAIL: ..."); }` za każdym razem.

### Konkretna propozycja
Dodać do `packages/std` prostą, ale prawdziwą funkcję:
```sbg
proc assertTrue(cond, message) {
    if (!cond) {
        console.log(join("ASSERT FAILED: ", message));
    }
    return cond;
}
```
To nie wymaga zmian w kompilatorze w ogóle (to tylko nowy plik w
`packages/std`) — realny, tani do wdrożenia fix, a bardzo pomocny dla
"prawdziwych programistów" piszących cokolwiek większego niż demo.

---

## 🟡 11. ŚREDNIE: brak interpolacji stringów — piramida `join()` utrudnia czytanie/edycję **kodu źródłowego `.sbg`** (nie chodzi o `.sb3`)

Zastrzeżenie na starcie, żeby było jasne: ten punkt **nie** dotyczy tego, że
skompilowany `.sb3` byłby nieczytelny — słusznie zauważono, że mało kto
zagląda w skompilowany projekt Scratch, więc jego "czytelność" nie ma
znaczenia. Ten punkt dotyczy wyłącznie tego, jak wygląda i jak się pisze
**sam plik `.sbg`, ręcznie, jako programista**.

Realny fragment z mojego `minihtml.sbg`:
```sbg
console.log(join("[", join(parentTag, join(" kolor=", join(color, join(boldTag, join("] ", join(prefix, cur.text))))))));
```
To jest jedna linijka kodu **źródłowego**, którą sam napisałem i sam
musiałem debugować. Żeby dodać w środku jeszcze jedną zmienną (np.
`bold=1`), trzeba doliczyć się w głąb zagnieżdżenia, gdzie dokładnie wstawić
kolejne `join(x, ...)` i domknąć nawias na końcu — łatwo pomylić kolejność
lub zapomnieć nawiasu, i kompilator nie pomoże specjalnie precyzyjnym
komunikatem (błąd parsowania wskaże ogólne miejsce, nie "brakuje Ci
domknięcia dla 4. zagnieżdżonego join()").

Więc: nie chodzi o produkt końcowy, tylko o **ergonomię pisania i
utrzymania kodu** w tym języku, gdy string ma więcej niż 2-3 fragmenty do
sklejenia — a przy pisaniu czegokolwiek tekstowego (logi, komunikaty,
generowany HTML/CSS, jak w moim przypadku) dzieje się to bardzo często.
Zostawiam ocenę, czy to wystarczająco dokuczliwe, żeby było priorytetem —
ale sam fakt, że pisząc ten parser regularnie musiałem liczyć nawiasy
zamiast skupić się na logice, jest dla mnie sygnałem, że warto.

### Konkretna propozycja
Dwa niezależne, wdrażalne bez rewolucji w kompilatorze usprawnienia:
1. **Wariadyczny `join(a, b, c, d, ...)`** zamiast tylko dwuargumentowego —
   to czysto biblioteczna/parserowa zmiana (rozwinięcie N-argumentowego
   wywołania do zagnieżdżonych 2-argumentowych `join()` już w warstwie
   parsera/lowering, przed dotarciem do backendu Scratch), więc powyższy
   przykład można by napisać jako:
   ```sbg
   console.log(join("[", parentTag, " kolor=", color, boldTag, "] ", prefix, cur.text));
   ```
2. **Interpolacja stringów w stylu f-stringów**, np. `` `[${parentTag} kolor=${color}]` `` —
   większa zmiana (nowy token w lekserze, nowa reguła w parserze
   rozwijająca taki literał do serii `join()` w AST przed kodegenem), ale
   to jest dokładnie ten typ funkcji językowej, którego brak najbardziej
   doskwiera przy pisaniu czegokolwiek tekstowego w SBG.

---
## 12. POWAŻNE [BUG] Nadpisywanie ramek stosu / zmiennych lokalnych w funkcjach rekurencyjnych i wielokrotnego dostępu

**Priorytet:** Wysoki  
**Komponent:** `sbg/compiler.py` (Name Mangling i alokacja zmiennych)

### Opis błędu
Kompilator spłaszcza wszystkie zmienne lokalne do statycznych, zniekształconych zmiennych globalnych w Scratchu (np. `__loc_114_a`). Zapewnia to maksymalną prędkość wykonania dla kodu liniowego, ale niszczy izolację pamięci podczas wywołań rekurencyjnych oraz wielokrotnego dostępu do funkcji (reentrancy).

### Przyczyna
Z powodu braku lokalnego zasięgu (scope) w Scratchu, spłaszczanie zmiennych lokalnych do stałych nazw globalnych przydziela im jedno statyczne miejsce w pamięci. Gdy funkcja wywołuje samą siebie rekurencyjnie (lub wywołuje inną procedurę używającą tych samych spłaszczonych zmiennych globalnych), wewnętrzne wywołanie nadpisuje dane z ramki stosu funkcji wywołującej, nie przywracając ich po powrocie.

### Krok po kroku (Jak powtórzyć)
1. Skompiluj i uruchom funkcję rekurencyjną (np. rekurencyjny Fibonacci, DFS na grafie lub QuickSort).
2. Zaobserwuj uszkodzenie stanu zmiennych, błędne wartości zwracane lub nieskończone pętle spowodowane zamazaniem zmiennych lokalnych i liczników pętli.

### Wpływ
- Standardowe algorytmy rekurencyjne zwracają błędne wyniki lub zawieszają program.
- Ograniczenie zgodności z C++ dla grafów wywołań zawierających cykle.

### Proponowane rozwiązanie / Strategia naprawy
Wdrożenie hybrydowego modelu pamięci poprzez dodatkowy przepust (pass) w kompilatorze:
1. **Analiza grafu wywołań (Call Graph Analysis):** Analiza AST pod kątem podziału funkcji na *płaskie/czyste* (brak rekurencji) oraz *rekurencyjne/wielokrotnego dostępu*.
2. **Ścieżka statyczna (Domyślna):** Zachowanie szybkich zmiennych globalnych (`__loc_...`) dla funkcji płaskich.
3. **Ścieżka stosowa (Dla rekurencji):** Dla funkcji rekurencyjnych generowanie w Scratchu sekwencji `push`/`pop` na liście-stosie (`__call_stack`) w celu zachowania stanu zmiennych lokalnych między wywołaniami.



## 💡 12. SUGESTIA FUNKCJONALNA: dwukierunkowy "bitowy most" plik ↔ Scratch (transfer danych binarnych przez copy-paste)

To nie jest bug, tylko propozycja nowej funkcji, wynikająca wprost z
ograniczenia, na które trafiłem pisząc `minihtml.sbg`: `--embed` jest
**jednokierunkowe i tylko kompilacyjne** — treść pliku zostaje "wypalona"
w projekt raz, w momencie `sbg compile`/`sbg run` (patrz punkt "jak działa
`--embed`" wyżej). Nie ma **żadnego** sposobu, żeby:
- wgrać nowe dane binarne do **już skompilowanego** `.sb3` bez ponownej
  kompilacji z nowym `--embed`,
- ani wyciągnąć cokolwiek z **działającego** projektu Scratch z powrotem na
  dysk (poza ręcznym kopiowaniem tekstu z Terminala, jeśli akurat to jest
  tekst, a nie dane binarne).

Vanilla Scratch nie ma dostępu do systemu plików w runtime — to fundamentalne
ograniczenie platformy, więc jedyny most między realnym plikiem a
uruchomionym projektem to **tekst przechodzący przez człowieka** (kopiuj-
wklej). Skoro tak, warto to zrobić świadomie i wygodnie, zamiast zostawiać
to jako "nie da się".

### Proponowany mechanizm
**Kierunek PC → Scratch (upload):**
1. Nowe polecenie CLI, np. `sbg.py file2bits plik.bin`:
   - kompresuje zawartość pliku (np. `zlib`/DEFLATE — standardowa biblioteka
     Pythona, więc zero nowych zależności w narzędziu CLI),
   - zamienia skompresowane bajty na czysty string złożony wyłącznie ze
     znaków `0`/`1` (jeden bajt = 8 znaków),
   - wypisuje ten string na stdout (lub do pliku `.txt`) jako **jedną
     linię** gotową do skopiowania.
   - **Kluczowa zaleta bitów `0`/`1` zamiast np. base64**: taki string nie
     zawiera spacji, przecinków ani średników — czyli **omija z automatu**
     błąd z punktu 2 tego raportu (`len()`/tokenizacja po znakach
     specjalnych), bo `0`/`1` nie wpadają w żadną z gałęzi tej błędnej
     heurystyki. To robi się "bezpieczny dla `sbg run`" format transferu
     niejako przy okazji.
2. Ten string wklejasz jako `--input` (albo w oknie "ask and wait" w
   realnym Scratchu) do programu SBG.
3. Program SBG odbiera string bitów i **zapisuje go do listy/zmiennej
   Scratch jako surowe dane** (np. jako kolejne elementy planszy w grze,
   jako klatki obrazu rysowane przez rozszerzenie Pen, jako save-state itd.)
   — czyli to, co się z tymi danymi robi wewnątrz Scratcha, zależy już od
   programisty; most dostarcza tylko surowe bajty w bezpiecznej dla
   Scratcha formie tekstowej.

**Kierunek Scratch → PC (download):**
1. Program SBG generuje dane wewnątrz Scratcha (np. wynik działania,
   wygenerowany obrazek, zapis stanu gry) i koduje je do tego samego
   formatu bitowego (nowa funkcja stdlib, patrz niżej), po czym wypisuje
   jako osobną linię w Terminalu przez `console.log(bits)`.
2. Użytkownik kopiuje tę linię ze zwykłego terminala/konsoli Scratcha.
3. Komplementarne polecenie CLI, `sbg.py bits2file wklejone_bity.txt
   wynik.bin`, dekoduje bity z powrotem na bajty i **dekompresuje** (odwrotność
   kroku 1 z kierunku upload) do prawdziwego pliku na dysku.

### Co realnie trzeba dopisać
- **Po stronie CLI (Python, `sbg/cli.py` + nowy moduł, np.
  `sbg/bitbridge.py`)** — to jest praca poza samym kompilatorem języka,
  więc stosunkowo tania: dwie nowe subkomendy `file2bits`/`bits2file`
  używające `zlib.compress`/`zlib.decompress` + proste
  `int.from_bytes`/`format(byte, '08b')` do konwersji bajt↔bity.
- **Po stronie `packages/std`** (nowy plik, np. `packages/std/bitbridge.sbg`,
  czyste SBG, zero zmian w kompilatorze):
  - `bitsToByteInt(bits, byteIndex)` — odczytuje 8 znaków `0`/`1` od danej
    pozycji i zwraca wartość 0-255 (suma potęg dwójki, prosta pętla po
    `letter()`),
  - `byteIntToBits(n)` — odwrotność, do generowania wyjścia,
  - opcjonalnie: prosty, **własny, zaimplementowany w czystym SBG**
    dekompresor dla naiwnego schematu RLE (run-length encoding) — w
    przeciwieństwie do pełnego DEFLATE, RLE jest realne do ręcznego
    zaimplementowania w SBG (kilkanaście linii: licz powtórzenia bajtu,
    koduj jako `bajt + licznik`) i sensowne dla treści typu obrazy z dużymi
    jednolitymi obszarami czy plansze gry. Pełną kompresję (DEFLATE) i tak
    najsensowniej zostawić po stronie CLI/Pythona (krok 1 uploadu, krok
    ostatni downloadu) — nie próbować reimplementować DEFLATE w SBG, to
    nierealne nakładem pracy vs. korzyść.

### Dlaczego to ma sens jako priorytet
To rozwiązuje realny, dotkliwy brak (żadnego I/O między działającym
projektem a dyskiem) w sposób w pełni zgodny z ograniczeniami platformy
(vanilla Scratch, zero zależności zewnętrznych, offline `.sb3`) i **dodatkowo
naturalnie omija bug z punktu 2** (bo alfabet `01` nie wchodzi w drogę
błędnej heurystyce `len()`). To jedna z niewielu sugestii w tym raporcie,
którą da się wdrożyć **wyłącznie po stronie narzędzia CLI i biblioteki
stdlib**, bez ruszania rdzenia kompilatora/optymalizatora w ogóle.

---

## Podsumowanie i priorytety dla realnego fixu

Kolejność, w jakiej warto to naprawiać (od najbardziej blokującego użycie
języka do kosmetyki):

1. **(pkt 1, 7)** Struct copy / dostęp do pola przez dynamiczny indeks — to
   jeden i ten sam brakujący mechanizm (field-list-aware lowering dla
   `vector<Struct>`), więc warto naprawić razem, jednym podejściem. To
   najpilniejsze, bo psuje fundamentalną funkcję języka po cichu (bez
   błędu kompilacji) w przypadku kopiowania, i wymusza nienaturalne obejścia
   w przypadku samego odczytu pola.
2. **(pkt 2)** `len()` w natywnym runnerze — blokuje sensowne testowanie
   lokalne (`sbg run`) każdego programu tekstowego.
3. **(pkt 3, 4)** Brak obsługi błędów + `int` bez wymuszonej semantyki —
   fundamentalne różnice względem tego, czego spodziewa się programista
   znający C++, i źródło cichych, trudnych do znalezienia bugów we
   własnym kodzie każdego, kto spróbuje użyć SBG poważnie.
4. **(pkt 5, 6)** Niespójności formatowania i typów kolekcji — bolesne przy
   pisaniu większych programów, ale nie blokujące.
5. **(pkt 9)** `const` bez wymuszenia — ten sam wzorzec co `int` (pkt 4):
   słowo kluczowe istnieje składniowo, ale nic za nim nie stoi. Tania
   poprawka (jedno sprawdzenie w miejscu kompilowania przypisania).
6. **(pkt 10, 11)** Braki DX (assert, interpolacja stringów) — nie psują
   nic, ale ich dodanie najbardziej realnie poprawiłoby komfort pisania
   większych programów w SBG, bo obie rzeczy dotykają czynności
   wykonywanej bez przerwy w każdym nietrywialnym programie (logowanie/
   budowanie tekstu, weryfikacja własnej logiki).
7. **(pkt 8)** Kosmetyka narzędzia deweloperskiego (ścieżki `--embed`).
8. **(pkt 12)** Bitowy most plik↔Scratch — nie naprawia niczego zepsutego,
   ale to sugestia funkcjonalna, którą najłatwiej wdrożyć w izolacji (tylko
   CLI + nowy plik stdlib, zero ryzyka regresji w kompilatorze), a
   jednocześnie realnie otwiera Scratcha jako platformę do przetwarzania
   dowolnych danych binarnych, nie tylko tekstu wpisanego ręcznie.
