# M4 — Link-Provenance Scoring ("structural foresight") · Implementation Plan

> **Branch:** `feat/m4-link-provenance-scoring` (off `feat/m3-declarative-scoring`)
> **Parent:** [FRONTIER_ROADMAP.md §M4](../FRONTIER_ROADMAP.md#m4--link-provenance-scoring-structural-foresight)
> **Depends on:** M3 (the `priority=` DSL + per-anchor `_eval_score`). M4 enriches the
> *context* a priority expression sees with the link's **provenance** — where it sat
> in the parent DOM — a cheap, deterministic predictor of value that wxpath uniquely
> has at discovery time (it holds the parent tree).

---

## 0. Key finding that reshapes this milestone (corrects the M3 §4.3 note)

The M3 doc deferred "`wx:` functions inside `priority=`" believing they raise
`XPathContextRequired` under `item=`-rooting. **That was a false alarm.** A probe (M4
session) shows the existing scoring evaluator
`elementpath.select(doc_root, expr, item=anchor, parser=WXPathParser)` already supports
`wx:` functions **and** structural axes — the `wx:` functions simply require a **leading
axis** (the calling convention they were built with; see
[test_patches.py](../tests/core/test_patches.py) which always calls `/wx:depth()`, never
bare `wx:depth()`). Bare `wx:depth()` fails because elementpath constant-folds a
zero-arg function with `context=None`; a leading `/` (or `./`) forces dynamic
evaluation. Verified working **today**, no code change:

| `priority=` expression | result (link in `<main>` vs `<footer>`) |
|---|---|
| `10 - /wx:depth()` | depth-relative boost (→ 7 at depth 3) — the M3 §2 boost idiom |
| `count(./ancestor::main)` | `1` / `0` |
| `(if (count(./ancestor::main) > 0) then 10 else 1)` | `10` / `1` (roadmap's boilerplate-demotion idiom) |
| `/wx:backlink()`, `string(.)` | backlink URL / anchor text |

**Consequence:** much of M4's "provenance-aware scoring" wedge is *already expressible*
with plain XPath + the M3 evaluator. M4's real additions are (a) the **computed** signal
that plain XPath can't cheaply express — **`wx:link-density()`** — plus ergonomic
sugar functions, and (b) optional **trap detection**. A `_eval_score` doc-fix to use a
leading axis is **not** needed; we will instead **correct the M3 doc** and add a
regression test proving `wx:` works in `priority=`.

---

## 1. Scope decision (please pick — see §9 question)

M4 in the roadmap bundles two independent payoffs. They have very different blast
radii, so this plan splits them:

- **M4a — Provenance-aware scoring (recommended for this pass).** New `wx:` provenance
  functions computed **on demand** from the anchor element (no stored blob, no model
  change): `wx:link-density()`, `wx:anchor-text()`, `wx:parent-tag()`,
  `wx:ancestor-path()`. Plus a regression test locking in that `wx:`/structural scoring
  works in `priority=` (the §0 finding), and the M3 doc correction. **Touches only
  `patches.py` + tests + docs.** Pure, deterministic, low risk.

- **M4b — Deterministic trap/loop detection (optional this pass / can be its own
  milestone).** A frontier **admission filter** that drops links whose URL-path or
  ancestor-path pattern repeats beyond a threshold (link-farm / infinite-calendar /
  `/a/b/a/b/...` traps), plus `max_link_density`. **Touches every frontier backend
  (`memory`/`sqlite`/`redis`), settings, and `push()` semantics** — materially bigger,
  and somewhat independent of scoring.

**Recommendation:** ship **M4a** now (it's the unique-IP "structural foresight"
scoring the roadmap headlines, and it's small + safe), and take **M4b** as a separate
focused pass. The rest of this plan specs M4a in full and M4b at design level.

---

## 2. M4a — provenance functions (on-demand, no storage)

**Design principle:** a priority expression is evaluated at discovery with the anchor
as context item (M3). Every provenance signal is a **pure function of the anchor and
its live DOM** — so we compute it *on demand inside the `wx:` function*, rather than
capturing a `Provenance` dataclass and threading it onto `CrawlIntent`/`CrawlTask`.
This avoids the roadmap's flagged risk (provenance blob size in SQLite) entirely and
keeps M4a a leaf change.

New functions in [patches.py](../src/wxpath/patches.py), modelled on the existing
`wx_backlink`/`wx_elem` (read `context.item.elem`, the anchor when called as
`./wx:fn()`):

```python
@register_wxpath_function('wx:anchor-text', nargs=0)
def wx_anchor_text(_, context):          # visible text of the anchor element
    el = _ctx_elem(context); return (el.text_content() or '').strip()

@register_wxpath_function('wx:parent-tag', nargs=0)
def wx_parent_tag(_, context):           # nearest sectioning ancestor, else parent tag
    el = _ctx_elem(context)
    SECTIONS = {'main','article','nav','aside','header','footer','section'}
    for anc in el.iterancestors():
        if anc.tag in SECTIONS: return anc.tag
    p = el.getparent(); return p.tag if p is not None else ''

@register_wxpath_function('wx:link-density', nargs=0)
def wx_link_density(_, context):         # links / text-length in the containing block
    el = _ctx_elem(context)
    block = _containing_block(el)        # nearest sectioning/div ancestor (or el)
    n_links = len(block.xpath('.//a'))
    text_len = len((block.text_content() or '').strip())
    return n_links / text_len if text_len else float(n_links)

@register_wxpath_function('wx:ancestor-path', nargs=0)
def wx_ancestor_path(_, context):        # 'html/body/main/div/a' (trap signal input)
    el = _ctx_elem(context)
    tags = [a.tag for a in reversed(list(el.iterancestors()))] + [el.tag]
    return '/'.join(tags)
```

`_ctx_elem(context)` resolves the context item to its lxml element (reusing the
`context.item.elem` / `context.item.parent.elem` logic already in `patches._get_root`),
raising `XPathContextRequired` if called bare (consistent with existing `wx:` funcs).

**DSL ergonomics this unlocks** (all via the unchanged M3 evaluator):

```
(: promote main-content links, demote dense boilerplate blocks :)
/// url( //a/@href,
         priority = (if (./wx:parent-tag() = 'main') then 10 else 1) - ./wx:link-density() )
```

**Determinism (I6).** Every function is a pure function of the served HTML and the
anchor — no clock, no RNG, no network. Same page ⇒ same provenance ⇒ same scores.

---

## 3. M4b — trap detection (design only; deferred unless chosen)

A frontier **admission filter** invoked in `push()` (return `False` to drop), config-gated:

```yaml
http.client.frontier.trap.max_path_repeat: 3     # drop /a/b/a/b/a/... after 3 repeats
http.client.frontier.trap.max_link_density: 0.95 # drop links from near-pure link blocks
```

- **URL-path repeat:** detect a path segment cycle of period ≤ k repeating > `max_path_repeat`
  (pure function of the URL string). The simplest correct version lives in a new
  `frontier/trap.py` helper and is called from each backend's `push()`.
- **Link-density drop:** needs the anchor's `link_density` at push time — which the
  engine *has* at discovery but the frontier does not. Cleanest wiring: the engine
  computes the admission signals at the M3 chokepoint (it has the anchor) and the
  frontier filter reads them — i.e. M4b **does** need a small carry (a couple of floats
  on `CrawlTask`, not a full `Provenance` blob). This is the main reason M4b is heavier
  and why splitting it out keeps M4a clean.

**Acceptance (when built):** on a fixture with a `<main>` grid + `<footer>` block + a
synthetic `/loop/1→/loop/2→…` trap — main links outrank footer links (M4a), the trap is
pruned at the threshold, and a *legitimate* deep chain of equal length is **not**.

---

## 4. Tests (M4a)

`tests/core/test_patches.py` — one test per new function (anchor text, parent-tag for a
`<main>`/`<nav>`/bare-`<div>` link, link-density on a dense vs sparse block,
ancestor-path string).

`tests/core/test_ops.py::TestPriorityScoring` — extend: a `priority=` expression using
`./wx:link-density()` and `./wx:parent-tag()` yields the expected per-anchor scores
(proves provenance functions work *in the scoring path*, not just standalone).

`tests/core/runtime/test_engine.py` — one end-to-end: a page with a `<main>` link and a
`<footer>` link; `priority=(if (./wx:parent-tag()='main') then 10 else 1)` ⇒ the main
link is fetched before the footer link. Plus the §0 **regression**: a `priority=` using
`/wx:depth()` evaluates without error (locks the corrected behavior).

Full suite must stay green; no `priority=` ⇒ still I5 byte-identical (M4a adds no
default-path behavior).

---

## 5. File manifest (M4a)

**Modify**
- `src/wxpath/patches.py` — `_ctx_elem` helper + 4 provenance `wx:` functions.
- `design/M3-declarative-scoring.md` — correct §4.3 note (wx: *does* work in `priority=`
  via leading axis; remove the "deferred to M4" framing, point here).
- `tests/core/test_patches.py`, `test_ops.py`, `runtime/test_engine.py` — as §4.

**No** parser/ops/engine/model/frontier change for M4a (the M3 evaluator already
carries it). M4b, if chosen, adds `frontier/trap.py`, `CrawlTask` admission floats,
backend `push()` filter calls, and settings.

---

## 6. Build order (M4a)

1. `_ctx_elem` + the 4 `wx:` functions in `patches.py`; `test_patches.py` green.
2. Wire into scoring tests (`test_ops.py`, `test_engine.py`); confirm provenance scores
   + the `/wx:depth()` regression.
3. Correct the M3 doc note. Full suite + `local_fixture` I5 spot-check.

---

## 7. Acceptance (M4a)

- [ ] `wx:anchor-text/parent-tag/link-density/ancestor-path` implemented + unit-tested.
- [ ] A `priority=` expression using `./wx:parent-tag()` / `./wx:link-density()` scores
      per-anchor correctly and reorders fetches end-to-end (main before footer).
- [ ] Regression: `priority=10 - /wx:depth()` evaluates (M3-note correction landed).
- [ ] Full suite green; no-`priority=` `local_fixture` still byte-identical (I5).

---

## 8. New invariant (append to DESIGN.md with M4)

```
I (provenance): All link-provenance signals (anchor text, parent/section tag,
   ancestor path, link density) are pure functions of the served HTML and the
   anchor node — no network, clock, or RNG — so provenance-steered crawls remain
   bit-reproducible (extends I6).
```

---

## 9. Decision needed

**Scope:** M4a (provenance scoring) only this pass, or M4a + M4b (also trap detection)?
See §1 — recommendation is M4a now, M4b as a separate focused milestone.
