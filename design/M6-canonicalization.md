# M6 — Canonicalization & Content-Fingerprint Dedup · Implementation Plan

> **Branch:** `feat/m5-hybrid-scorer` (continues the post-M5 line; M0 items already
> landed here)
> **Parent:** [FRONTIER_ROADMAP.md §M6](../FRONTIER_ROADMAP.md#m6--canonicalization--content-fingerprint-dedup)
> **Pattern sibling:** [M4b-trap-detection.md](./M4b-trap-detection.md) — same
> composable-wrapper architecture; this doc follows its shape deliberately.

---

## 0. Scope decision (confirmed with user 2026-06-01)

M6 in the roadmap bundles **two** dedup layers with very different blast radii. This
pass ships only the first; the second is designed here but deferred.

- **Layer 1 — URL canonicalization (THIS PASS).** Normalize each URL to a canonical
  form *before* the dedup check, so `?utm_*`, fragments, trailing slashes, host
  case, and default ports stop spawning duplicate fetches. A **pure function of the
  URL string** + a **composable frontier wrapper** — zero edits to the
  memory/sqlite/redis backends, settings-gated, **off by default** so the default
  crawl stays byte-identical (I5).

- **Layer 2 — content-fingerprint near-dup dedup (DEFERRED).** SimHash/shingle over
  main-content, a fingerprint registry, Hamming-threshold matching. Unlike layer 1
  it is **not** a pure function of the URL — it needs parsed content *and* new
  frontier state (a fingerprint registry), which means touching the `Frontier`
  Protocol and **all three** backends, plus a content extractor. Larger, off the
  critical path, and independently shippable. See §6.

This mirrors M4b, which shipped the cheap URL-only filter and deferred the
DOM-dependent one.

---

## 1. Why a wrapper, not a `push()` edit per backend

Identical reasoning to M4b §1. `Frontier` is a `Protocol`, so a decorator frontier
implements the same interface, canonicalizes in its own `push()`/`seen()`/
`mark_done()`, and delegates the rest. Backends stay untouched; one normalization
call-site; composes with any backend; and when `canonical.enabled is False` the
factory returns the bare backend → **the default object graph is literally
unchanged** → I5 byte-identical with zero overhead.

**Self-consistency (the key correctness argument).** The engine fetches `task.url`
and matches the response by `resp.request.url`; **every** task — seed-derived and
discovered alike — enters through `frontier.push()`. So rewriting `task.url` to its
canonical form *at push* means: (a) dedup keys on the canonical URL, (b) the engine
fetches the canonical URL, and (c) the inflight-match key is that same canonical URL.
No mismatch is possible. There is no separate "fetch the original, dedup the
canonical" path to keep in sync.

```
get_frontier_backend()
  └─ CanonicalizingFrontier(            ← outermost (M6), only if canonical.enabled
       TrapFilterFrontier(              ← M4b, only if trap.enabled
         InMemoryFrontier()))           ← backend, unchanged
```

**Wrapper order — canonicalizer is outermost.** URLs must be normalized *before* the
trap filter and the backend dedup ever see them, so both operate on the canonical
form (e.g. a trap check shouldn't be fooled by a `?utm` variant, and dedup must
collapse variants). The factory wraps trap first, then canonical, leaving canonical
on the outside.

---

## 2. The canonicalizer — `canonicalize()` (`frontier/canonical.py`)

Pure, idempotent, deterministic (no DOM/clock/RNG/network → fits the I6 family).

**Always applied (structural, RFC 3986-equivalent — never configurable):**
- lowercase the scheme;
- lowercase the host (hostnames are case-insensitive §3.2.2), preserving any
  userinfo;
- drop a port equal to the scheme default (`:80` on http, `:443` on https, …).

**Configurable (each can be semantic on some sites):**
- `strip_params` — drop query params whose name matches any glob in the list
  (`utm_*`, `gclid`, `fbclid`, `sessionid`, `msclkid` by default), then **sort the
  survivors** so `?b=2&a=1` and `?a=1&b=2` collapse. An empty list leaves the query
  untouched (and unsorted) — a conservative passthrough.
- `drop_fragment` (default true) — `#fragment` is client-side only.
- `normalize_trailing_slash` (default true) — strip trailing `/` from non-root paths
  (`/p/1/` → `/p/1`; `/` stays `/`).

```python
def canonicalize(url, *, strip_params=(), drop_fragment=True,
                 normalize_trailing_slash=True) -> str | None:
    if not url:
        return url                                  # defensive; engine never pushes url-less
    parsed = urllib.parse.urlsplit(url)
    scheme = parsed.scheme.lower()
    netloc = _normalize_netloc(parsed, scheme)      # lowercase host, keep userinfo, drop default port
    path = parsed.path
    if normalize_trailing_slash and len(path) > 1:
        path = path.rstrip("/") or "/"
    query = parsed.query
    strip_list = tuple(strip_params or ())
    if strip_list and query:
        kept = [(k, v) for k, v in urllib.parse.parse_qsl(query, keep_blank_values=True)
                if not _matches_any(k, strip_list)]
        kept.sort()
        query = urllib.parse.urlencode(kept)
    fragment = "" if drop_fragment else parsed.fragment
    return urllib.parse.urlunsplit((scheme, netloc, path, query, fragment))
```

**Risk guard (roadmap §Risks): never strip unknown params by default.** The default
list is explicit, well-known tracking keys only; an arbitrary `?id=42` is assumed
semantic and survives.

---

## 3. The wrapper (`frontier/canonical.py`, same module)

```python
class CanonicalizingFrontier(Frontier):
    def __init__(self, inner, *, strip_params=DEFAULT_STRIP_PARAMS,
                 drop_fragment=True, normalize_trailing_slash=True):
        self._inner = inner
        self._strip_params = tuple(strip_params or ())
        self._drop_fragment = drop_fragment
        self._normalize_trailing_slash = normalize_trailing_slash
        self.canonicalized = 0                       # observability: URLs rewritten

    async def push(self, task):
        canon = self._canon(task.url)
        if canon != task.url:
            self.canonicalized += 1
            task.url = canon                         # mutate in place — task is fresh & unshared
        return await self._inner.push(task)

    async def mark_done(self, url): return await self._inner.mark_done(self._canon(url))
    async def seen(self, url):      return await self._inner.seen(self._canon(url))
    async def pop(self):            return await self._inner.pop()   # already-canonical
    async def size(self):           return await self._inner.size()
    async def checkpoint(self):     return await self._inner.checkpoint()
    async def close(self):          return await self._inner.close()
```

`CrawlTask` is `@dataclass(slots=True)`; mutating `task.url` after construction does
**not** re-run `__post_init__` (no priority resync surprise). `canonicalized` is a
counter (no silent caps) so a run can report how many URLs were normalized.

---

## 4. Settings + factory

`settings.py` — under `http.client.frontier`:

```python
'canonical': {
    'enabled': False,                  # off by default → I5 byte-identical default crawl
    'strip_params': ['utm_*', 'gclid', 'fbclid', 'sessionid', 'msclkid'],
    'drop_fragment': True,
    'normalize_trailing_slash': True,
},
```

`frontier/__init__.py` — wrap **after** the trap wrap so canonical is outermost:

```python
canonical = FRONTIER_SETTINGS.canonical
if canonical.enabled:
    from wxpath.http.frontier.canonical import CanonicalizingFrontier
    backend = CanonicalizingFrontier(
        backend,
        strip_params=canonical.strip_params,
        drop_fragment=canonical.drop_fragment,
        normalize_trailing_slash=canonical.normalize_trailing_slash,
    )
return backend
```

Lazy import keeps `canonical.py` off the default import path.

---

## 5. Tests

`tests/http/frontier/test_canonical.py` (pure logic, 28 cases): structural
normalization (scheme/host case, default-port strip, userinfo preserved, path case
kept); trailing-slash (single, multiple, root preserved, toggle off); fragment
(default drop, toggle off); param strip (utm glob, named keys, semantic param kept,
case-insensitive match, survivor sort, empty list = passthrough); the four
acceptance variants collapsing to one form; idempotency; falsy passthrough.

`tests/http/frontier/test_canonical_frontier.py` (wrapper, 7 cases): push rewrites
`task.url`; four variants dedup to one push; already-canonical not counted; pop
returns canonical; `seen()` canonicalizes its arg; `mark_done`/`close` delegate;
strip-list configurable.

`tests/http/frontier/test_factory.py` (+3): `canonical.enabled=False` → bare backend;
`=True` → `CanonicalizingFrontier` wrapping the configured backend; both trap+canonical
on → canonical is outermost over trap.

`tests/core/runtime/test_engine.py` (e2e, 2): **acceptance** — a page linking to
`/p/1`, `/p/1?utm_source=x`, `/p/1#section`, `/p/1/` ⇒ with canonical on, **exactly
one** child fetch (`http://test/p/1`); control — with it off, all four variants
fetched (the bug M6 fixes).

I5: `canonical.enabled` defaults `False`; full suite **354 green**; `local_fixture`
byte-identical to the M5 baseline (`d0:1 d1:4 d2:16 d3:64 d4:256 d5:171`, 511
extractions).

---

## 6. Deferred / future (Layer 2 and beyond)

- **Content-fingerprint near-dup dedup.** SimHash (or MinHash shingles) over the
  main-content text (`wx:main-article-text()` in `patches.py` is the natural source),
  a fingerprint registry in the frontier, Hamming-distance threshold (default ~3),
  off by default. Requires extending the `Frontier` Protocol with a
  fingerprint-seen/record pair and implementing it across memory/sqlite/redis (the
  registry must persist with the backend), plus a post-parse fingerprint hook in the
  engine. Settings sketch:
  ```yaml
  http.client.frontier.fingerprint.enabled: false
  http.client.frontier.fingerprint.hamming_threshold: 3
  ```
  This is its own milestone-sized change; do it when duplicate-content (mirror
  domains, print views, pagination chrome serving identical bodies) is shown to
  matter on a real corpus.
- **Query-string trap interplay.** Canonicalization stripping `?page=` would mask the
  query-string trap class noted in M4b §6 — keep them distinct; the strip-list is
  for *tracking* params, not pagination.
- **Per-site canonical overrides.** A host→strip-list map (some sites use `?p=` as a
  tracking param, others as semantic) — only if a need emerges; the global explicit
  list is the safe floor.

---

## 7. File manifest

**Add**
- `src/wxpath/http/frontier/canonical.py` — `canonicalize`, `_normalize_netloc`,
  `_matches_any`, `DEFAULT_STRIP_PARAMS`, `CanonicalizingFrontier`.
- `tests/http/frontier/test_canonical.py`, `test_canonical_frontier.py`
  (+3 in `test_factory.py`, +2 e2e in `test_engine.py`).

**Modify**
- `src/wxpath/settings.py` — `http.client.frontier.canonical` block.
- `src/wxpath/http/frontier/__init__.py` — wrap when `canonical.enabled` (outermost).

**No** change to backends, `CrawlTask`/`CrawlIntent`, ops, engine, or parser.

---

## 8. New invariant (I11, append to FRONTIER_ROADMAP.md)

```
I11 (canonical dedup, M6): URL canonicalization is a pure function of the URL string
    and is applied as a composable frontier wrapper that is absent unless
    http.client.frontier.canonical.enabled. With it disabled the frontier object
    graph is identical to pre-M6, so default crawls remain byte-reproducible
    (extends I5). With it enabled, normalization collapses URL variants of the same
    page to a single fetch — affecting which URLs are fetched, never the data
    extracted from a given page (a realization of I4).
```

---

## 9. Build order (as executed)

1. `canonical.py` pure `canonicalize()` + `test_canonical.py` green.
2. `CanonicalizingFrontier` + wrapper/factory tests; settings block; factory wrap.
3. e2e engine acceptance + control test; full suite + `local_fixture` I5.
4. Append I11 to FRONTIER_ROADMAP.md; offer to commit.
