# M3 — Declarative Scoring (DSL surface) · Implementation Plan

> **Branch:** `feat/m3-declarative-scoring` (off `feat/m2-priority-scheduling`)
> **Parent:** [FRONTIER_ROADMAP.md §M3](../FRONTIER_ROADMAP.md#m3--declarative-scoring-dsl-surface)
> **Depends on:** M2 (the frontier now *honors* `CrawlTask.priority`; lower value =
> popped sooner). **M3 is what *sets* it from the DSL** — a `priority=` argument on
> `url(...)` that produces a per-link priority. With no `priority=` anywhere in an
> expression, every task still gets `priority=depth` ⇒ byte-identical to M2/BFS
> (Invariant I5). M3 is the first move that is *observable* without writing Python.

---

## 1. Scope & non-goals

**In scope:**
1. A new `priority=` keyword argument on every `url(...)` discovery form
   (`url(<xpath>)`, `/url`, `//url`, `///url`), parallel to the existing
   `follow=` / `depth=` arguments already tokenized in
   [parser.py](../src/wxpath/core/parser.py).
2. The argument value is either a **numeric constant** (`priority=10`) or an
   **XPath expression evaluated per discovered link, in the context of that link's
   anchor element** (`priority=number(./@data-rank)`).
3. Threading the resulting score from discovery (`ops.py`) → `CrawlIntent.score` →
   `CrawlTask.priority` at the engine enqueue chokepoint.

**Not in scope (later moves):**
- Link **provenance** inputs and the `wx:link-density()` / `wx:parent-tag()` family
  (**M4**) — M3 only wires the *channel*; M4 enriches the *context* the priority
  expression sees. The scoring expression already runs through `WXPathParser`
  (see [patches.py](../src/wxpath/patches.py)), so M4's functions slot in with **zero
  M3 rework**.
- Semantic / embedding scorers (**M5**).
- Canonicalization, host-fairness (**M6/M7**).
- `priority=` **combined with** `follow=`/`depth=` on the same *literal-seed*
  `url('...', follow=…, depth=…, priority=…)` form — deferred to a step 4 (see §8);
  the common, high-value forms are the discovery ops, which M3 covers fully.

---

## 2. The convention decision (load-bearing — ✅ DECIDED)

> **Decision (2026-05-30):** DSL `priority=` means **importance — higher value =
> crawled sooner** — mapped into the engine's min-first space by negation
> (`CrawlTask.priority = -score`). This is option **A** below and matches the
> roadmap examples verbatim. Chosen over a `boost=`-that-subtracts (the M2-doc
> sketch) because the *absolute* form is the more expressive primitive: a relative
> `boost=` cannot express pure-absolute priority, whereas absolute priority can
> express boost-over-depth in-expression once depth is reachable from a scoring
> expression — structural-axis relativity (`priority = 10 - count(./ancestor::*)`)
> works today; the `priority = N - wx:depth()` form lands with **M4** (which wires
> `wx:` functions into the scoring context — see §4.3).

M2 committed the **engine** to *"lower `CrawlTask.priority` value = popped sooner"*
(min-first heap; `priority=depth` ⇒ BFS). M2's plan explicitly deferred the
**DSL-facing** convention to M3:

> *"'priority 0 is most important' can read awkwardly to end users. M3's DSL will
> paper over this … a `boost` that subtracts from priority, or documenting 'lower =
> sooner'. The engine convention stays `lower = sooner`; the DSL is free to present
> sugar."* — [M2-priority-scheduling.md §2](./M2-priority-scheduling.md)

The [roadmap §M3](../FRONTIER_ROADMAP.md#m3--declarative-scoring-dsl-surface) examples
read **higher = more important** (`priority=10` for "gold" product links;
`priority=count(./ancestor::article)`). M3 must reconcile the two. **Resolution (option A, decided — see banner above):**

> **DSL `priority=` expresses *importance*: higher value = crawled sooner.** The
> engine maps it into the existing min-first space by **negation** at the enqueue
> chokepoint: `CrawlTask.priority = -score`. A `priority=10` link (engine priority
> `-10`) is popped before a `priority=1` link (engine priority `-1`).

Rationale: it matches every roadmap example verbatim, keeps the M2 engine convention
untouched, and is the most natural reading of "priority" for end users. The
alternatives (`boost=` sugar, or DSL `priority=` meaning *lower = sooner* to mirror
the engine) were considered and rejected; either remains a one-line change (drop the
negation / rename the token) should we revisit.

**Default / mixing semantics (a direct consequence of the sign choice):**

| Case | `CrawlIntent.score` | `CrawlTask.priority` | Effect |
|---|---|---|---|
| no `priority=` in expression | `None` | `None` → `depth` (`__post_init__`) | **BFS, unchanged (I5)** |
| `priority=10` | `10.0` | `-10` | jumps ahead of unscored/depth tasks |
| `priority=number(./@data-rank)` | per-anchor float | `-rank` | high-rank links first |

Using `None` (not `0.0`) as the "unscored" sentinel is deliberate — it mirrors M2's
`priority: Optional[int] = None` and is what preserves depth-BFS exactly. (The
roadmap sketch wrote `score: float = 0.0`; we deviate for I5 fidelity and document
it.) A consequence worth stating: scored and unscored tasks share one numeric axis,
so an explicit positive `priority=` will outrank any depth-default task. That is the
intended "steer the budget toward what I marked important" behavior.

---

## 3. Grammar & AST

### 3.1 Tokenizer — one new token

[parser.py](../src/wxpath/core/parser.py) `TOKEN_SPEC`: add a `PRIORITY` token
immediately after `DEPTH`, same shape (optional leading comma + optional
whitespace):

```python
("FOLLOW",   r",?\s{,}follow="),
("DEPTH",    r",?\s{,}depth="),
("PRIORITY", r",?\s{,}priority="),   # ← M3
```

Ordering is safe: `priority=` can only be matched by `PRIORITY` (there is no active
`NAME` token; the catch-all `OTHER` is last). XPath words inside the value
(`number`, `count`, …) keep tokenizing char-by-char into the value buffer exactly as
`contains(...)` does today.

### 3.2 New AST node — `Score`

Sibling of `Depth`, but its value may be a number **or** an xpath string, so it
carries both the raw text and a resolved constant (if numeric):

```python
@dataclass
class Score:
    expr: str                      # raw text: "10", "number(./@data-rank)"
    const: float | None = None     # set iff expr is a pure numeric literal
```

Constructed in the arg-capture loop:

```python
expr = priority_expr.strip()
try:
    score_node = Score(expr, const=float(expr))   # priority=10
except ValueError:
    score_node = Score(expr)                       # priority=number(./@data-rank)
```

### 3.3 `capture_url_arg_content` — capture the value

Mirror the existing `reached_depth_token` / `depth_number` machinery: add
`reached_priority_token` + a `priority_expr` accumulator; on the `PRIORITY` token set
the flag (and clear `reached_follow_token`/`reached_depth_token`); accumulate
subsequent tokens into `priority_expr`; after the loop, append the `Score` node built
as in §3.2. The value runs to the next `,` (at paren-balance 1) or the closing `)`,
identical to how `depth=` terminates.

### 3.4 `_specify_call_types` — new arity/type overloads

Add `Score`-bearing cases (the discovery forms are the priority):

```python
# url(<xpath>, priority=…) / url(., priority=…)
(Xpath, Score) | (ContextItem, Score)        -> UrlQuery
# url('lit', priority=…)  (constant only — single seed)
(String, Score)                               -> UrlLiteral
# /url(…, priority=…) , //url(…, priority=…)
(Xpath, Score) | (ContextItem, Score)         -> UrlQuery
# ///url(<xpath>, priority=…)
(Xpath, Score)                                -> UrlCrawl
```

The existing single-arg branches are untouched, so `url(//a/@href)` with no
`priority=` parses exactly as today.

**Risk — grammar ambiguity (roadmap-flagged).** The `follow=`/`depth=` rewrite rules
and the `(String, Xpath, Depth)` permutations are the delicate part. M3 adds `Score`
to the *end* of the arg list only and does **not** reorder existing combos; the
`follow=`+`priority=` 3-arg seed form is explicitly deferred (§8). Parser tests
(§6) pin every new combination *and* assert the old ones are unchanged.

---

## 4. Discovery → score → priority

### 4.1 `dom.py` — return anchors, not just URLs

Today [dom.py](../src/wxpath/core/dom.py) `get_absolute_links_from_elem_and_xpath`
returns bare absolute URL strings — the anchor node (needed to evaluate a per-link
expression) is discarded. Add a **parallel** function (the existing one stays for the
unscored path, so the zero-`priority=` path is provably unchanged):

```python
def get_links_with_anchors_from_elem_and_xpath(elem, xpath):
    """Return [(absolute_url, anchor_element), …] in document order."""
    base_url = getattr(elem, 'base_url', None)
    pairs = []
    for result in elem.xpath(xpath):            # lxml NATIVE — smart strings
        getparent = getattr(result, 'getparent', None)
        anchor = getparent() if getparent is not None else None
        pairs.append((urljoin(base_url, str(result)), anchor))
    return pairs
```

> **Implementation note (improved over the original sketch).** The first draft of
> this plan proposed deriving an "anchor xpath" by *stripping a trailing `/@attr`
> step* and pairing two `xpath3` selections positionally. A probe (recorded in
> the M3 session) showed that is **unsound**: `//a` and `//a/@href` can return
> different counts — an `<a name="top">` with no href appears in the first but not
> the second — so positional `zip` mis-pairs. The shipped approach uses lxml's
> **native** `xpath`, whose attribute/text results are "smart strings"
> (`_ElementUnicodeResult`) carrying `.getparent()` → the owning element. This
> pairs each URL with its real anchor with zero heuristics, in document order, and
> matches `xpath3`'s URL set for standard link selectors. Locked by `test_dom.py`
> (§6), including the href-less-anchor misalignment case.

### 4.2 `models.py` — `CrawlIntent.score`

```python
@dataclass(slots=True)
class CrawlIntent(Intent):
    url: str
    next_segments: list
    score: float | None = None      # ← M3 (None = unscored ⇒ BFS)
    # provenance: "Provenance | None" = None   # ← M4
```

### 4.3 `ops.py` — evaluate the score per anchor

Extend the discovery handlers. A trailing `Score` in `curr_segments[0].args` switches
on the dynamic path:

```python
score_node = _trailing_score(url_call)         # Score | None
if score_node is None:
    pairs = [(u, None) for u in get_absolute_links_from_elem_and_xpath(elem, xp)]
elif score_node.const is not None:
    pairs = [(u, None) for u in get_absolute_links_from_elem_and_xpath(elem, xp)]
else:
    pairs = get_links_with_anchors_from_elem_and_xpath(elem, xp)

for url, anchor in dict_preserving_order(pairs):
    score = (score_node.const if score_node and score_node.const is not None
             else _eval_score(anchor, score_node.expr) if score_node else None)
    yield CrawlIntent(url=url, next_segments=next_segments, score=score)
```

`_eval_score(anchor, expr)` evaluates the expression with the **whole document as
root** but the **context item at the anchor**:
`float(elementpath.select(doc_root, expr, item=anchor, parser=WXPathParser))`
(`doc_root = anchor.getroottree().getroot()`; coerce a node-set/empty result, and on
any failure return a neutral `0.0` rather than aborting the crawl).

> **Correction over the original sketch.** The first draft wrote
> `anchor.xpath3(expr)` — but `elementpath.select(anchor, …)` treats the anchor as
> the *document root*, so `./ancestor::article` (the roadmap's own M3 example!)
> would see **nothing** and silently score 0. Rooting at the document with
> `item=anchor` makes `.` the anchor *and* keeps the ancestor/structural axes
> working. Verified by `test_ops.py::TestPriorityScoring` (`count(./ancestor::nav)`
> → 1 for an in-nav link, 0 otherwise).
>
> **M4 boundary.** `wx:*` functions in a `priority=` expression are **not yet**
> supported (they raise `XPathContextRequired` under `item=`-rooting because they
> expect an elementpath-wrapped context item). Plain XPath — attributes, arithmetic,
> `number()`/`count()`/`string-length()`, and structural axes — is fully supported.
> Wiring `wx:` into the scoring context (so e.g. `priority = 10 - wx:depth()` works)
> is **M4** territory, alongside the new provenance `wx:` functions.

Registrations to add:

- `@register('url', (Xpath, Score))`, `(ContextItem, Score)`, and the `/url`,`//url`
  equivalents → on `_handle_url_eval`.
- `@register('///url', (Xpath, Score))` → on `_handle_url_inf`; the recursive re-wrap
  must **carry the `Score` forward** so deeper levels keep scoring:
  `UrlCrawl('///url', [xpath_arg, score_node, url])`, plus a matching
  `@register('///url', (Xpath, Score, str))` overload on `_handle_url_inf_and_xpath`.
- `@register('url', (String, Score))` → on `_handle_url_str_lit` (constant seed score).

### 4.4 `engine.py` — score → priority at the chokepoint

The single enqueue site in `_process_pipeline`
([engine.py](../src/wxpath/core/runtime/engine.py) `isinstance(intent, CrawlIntent)`):

```python
priority = (-intent.score) if intent.score is not None else None   # §2 negation
pushed = await frontier.push(
    CrawlTask(
        elem=None, url=intent.url, segments=intent.next_segments,
        depth=next_depth, backlink=task.url,
        priority=priority,                 # ← None ⇒ depth (BFS); else -score
    )
)
```

That is the **only** engine edit. No change to `submitter()`, `is_terminal()`, or the
frontier backends (M2 already orders by `priority`).

---

## 5. Determinism & invariants

- Same page + same `priority=` expression ⇒ same anchors ⇒ same scores ⇒ same
  priorities ⇒ same pop order. No clock, no RNG, no network in the scoring path.
- **I5 preserved exactly:** with no `priority=`, every `CrawlIntent.score` is `None`,
  every `CrawlTask.priority` falls back to `depth`, and the `local_fixture` gate (512
  pages, `d0:1 d1:4 d2:16 d3:64 d4:256 d5:171`, 511 extractions) stays byte-identical.
- **New invariant to append to [DESIGN.md](../DESIGN.md) / roadmap:**
  > *I4 (THE WEDGE, partial): a `priority=` expression affects traversal **order**
  > only, never extracted **data**. For any visited page, its `map{…}`/extraction
  > output is identical regardless of `priority=`.* (Full I4 lands with M5; M3
  > establishes it for deterministic scoring.)

No new settings keys — scoring is expressed *in the DSL*, not in config.

---

## 6. Tests

**`tests/core/test_parser.py`** (extend):
- `url(//a/@href, priority=10)` → `UrlQuery` args `[Xpath('//a/@href'), Score('10', const=10.0)]`.
- `///url(//a/@href, priority=number(./@data-rank))` → `UrlCrawl` with `Score(expr=…, const=None)`.
- `/url(@href, priority=count(./ancestor::article))` and `//url(...)` variants.
- **Regression:** `url(//a/@href)`, `url('...', follow=…, depth=2)` parse byte-identical to pre-M3 (no `Score` injected).

**`tests/core/test_dom.py`** (new): `get_links_with_anchors_from_elem_and_xpath` on a
small fixture — `@href`, `text()`, element-selecting xpath, and relative→absolute;
assert `(url, anchor)` pairing + order.

**`tests/core/test_ops.py`** (extend): a fixture page whose anchors carry `@data-rank`
→ assert `CrawlIntent.score` matches `@data-rank` per link; constant `priority=N`
applies `N` to all; no `priority=` ⇒ `score is None`.

**`tests/core/runtime/test_engine_*` (extend):** the roadmap acceptance — fixture
where anchors carry `@data-rank`; under a tight `max_urls`, assert high-rank pages are
**popped/fetched before** low-rank ones; and a twin run proves identical order
(determinism) and identical extracted records (I4).

**Regression gate:** full suite green + `wxpath-bench run local_fixture` reproduces the
pre-M3 baseline (dark-ship when no `priority=` is present).

---

## 7. File manifest

**Modify**
- `src/wxpath/core/parser.py` — `PRIORITY` token; `Score` node; `capture_url_arg_content`
  priority capture; `_specify_call_types` `Score` overloads.
- `src/wxpath/core/dom.py` — `get_links_with_anchors_from_elem_and_xpath` + `_strip_trailing_value_step`.
- `src/wxpath/core/models.py` — `CrawlIntent.score: float | None = None`.
- `src/wxpath/core/ops.py` — `Score` import; score evaluation + new `@register` overloads
  on `_handle_url_eval` / `_handle_url_inf` / `_handle_url_inf_and_xpath` / `_handle_url_str_lit`.
- `src/wxpath/core/runtime/engine.py` — score → `priority` (negated) at the enqueue chokepoint.

**Add**
- `tests/core/test_dom.py`; extend `test_parser.py`, `test_ops.py`, `test_engine_*`.

**No** change to frontier backends, settings, or the crawler/throttler.

---

## 8. Suggested build order (detachable steps)

1. **Parser** — `PRIORITY` token + `Score` node + capture + `_specify_call_types`;
   parser tests green (pure-syntax, no runtime).
2. **Model + dom** — `CrawlIntent.score`; `get_links_with_anchors_…`; dom test green.
3. **Ops + engine** — score evaluation, registrations, chokepoint negation; ops +
   engine tests green; **I5 regression** (`local_fixture`) byte-identical.
4. *(optional)* **`follow=`+`priority=` seed combos** — the deferred 3-arg literal
   forms from §1/§3.4, if wanted, behind their own parser tests.

---

## 9. Acceptance — ✅ IMPLEMENTED

- [x] `url(//a/@href, priority=…)`, `/url`/`//url`/`///url`, and the literal-seed
      `url('…', priority=…)` parse for both constant and xpath values
      (`tests/core/test_parser.py::TestPriorityScoring`, 11 tests).
- [x] On a `@data-rank` fixture, high-rank pages are **fetched before** low-rank ones
      (`test_engine.py::test_engine_priority_orders_fetches`: order high→mid→low vs
      document order low→high→mid). Constant `priority=` ties ⇒ BFS order; structural
      axis `count(./ancestor::nav)` scores correctly (`test_ops.py`).
- [x] **I4** — twin run identical order **and** identical extracted `map{}` data;
      order differs from BFS, data does not
      (`test_engine.py::test_engine_priority_changes_order_not_data`,
      `test_engine_priority_is_deterministic`).
- [x] **I5** — no `priority=` ⇒ `wxpath-bench run local_fixture` byte-identical to the
      M2 baseline: `d0:1 d1:4 d2:16 d3:64 d4:256 d5:171`, 511 extractions.
- [x] Full suite green (269 tests, +11 for M3). Convention (§2) = `priority=`, higher
      = sooner, negated (option A), confirmed with the user 2026-05-30.
- [ ] *(deferred)* `follow=`+`priority=` seed combos (§8 step 4); `wx:` functions in
      `priority=` (folds into M4 — see §4.3).
