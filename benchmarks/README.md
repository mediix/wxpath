# benchmarks/

Standardized crawl-performance harness for wxpath.

Drives wxpath through workloads grounded in the crawler-metric literature
(Mercator, IRLbot, Cho/García-Molina, Heritrix, Common Crawl) and emits a
uniform report card so future P0/P1 instrumentation work is measurable as a
delta against today's baseline.

## Install

```bash
pip install -e ".[bench]"
```

## Quick start

```bash
wxpath-bench list                          # list bundled workloads
wxpath-bench show single_host              # show resolved YAML + canonical hash
wxpath-bench run  single_host --max-urls 50
```

Output lands at `benchmarks/results/<UTC-timestamp>__<workload>/`, containing
`report.txt`, `report.json`, `raw.jsonl`, `meta.json`, `workload.snapshot.yaml`.

## Workloads

| Workload | Runnable in v0 | Purpose |
|---|---|---|
| `local_fixture` | yes | **Reproducible engine baseline.** Deep-crawl a deterministic in-process server over loopback. No network. |
| `single_host` | yes | Live field run: depth crawl one site (docs.python.org). Realistic but network-bound and non-reproducible. |
| `thousand_host` | stub | Broad 1k-domain crawl (Nutch/StormCrawler reference). Needs manifest loader. |
| `cc_sample` | stub | 100k-URL random sample from Common Crawl. Needs S3 manifest. |
| `anti_bot` | stub | 10-tier anti-bot stress test. Needs proxy/render integration. |
| `freshness` | stub | Incremental recrawl; F(p,t) / A(p,t). Needs recrawl loop in wxpath. |

## Reproducible engine baseline vs live field run

Two of the runnable workloads answer different questions:

- **`local_fixture` — reproducible engine baseline (use this for regression tracking).**
  `benchmarks/server.py` stands up a deterministic loopback server serving a
  synthetic BFS tree of pages, then deep-crawls it with `url('<base>')///url(//@href)`.
  Because the link graph, page sizes, and extractable elements are fixed, the
  coverage and extraction numbers are **identical on every run** — they are
  pinned by the corpus (`page_count`, `fan_out`, `filler_bytes` in the workload,
  captured in `meta.json`'s `workload_hash`). Loopback latency is ≈ 0 and
  politeness sleeps are disabled (`respect_robots: false`, throttle delays `0`),
  so `pages/s` and `bytes/s` reflect wxpath's own cost: task scheduling, HTML
  parse, and XPath evaluation. Any change in the deterministic fields signals a
  correctness regression; a change in throughput signals a performance one.

  ```bash
  wxpath-bench run local_fixture --out benchmarks/results
  ```

- **`single_host` — live field run.** Crawls the real docs.python.org. Useful
  for a realism sanity check, but the numbers are dominated by network latency,
  the remote server, robots/throttling, and time of day — **not reproducible**.
  Do not use it for regression tracking.

### Reading the baseline & comparing runs

For a fixed corpus, the deterministic fields must match across runs: `pages`,
`unique URLs (observed)`, `depth distribution`, `selector hit rate`,
`extracted (total)`. Crucially, the **seed/root page yields no extracted value**
(a literal `url('...')` is a crawl, not an extraction), so for an `N`-page
corpus expect `pages = N` and `extracted (total) = N - 1`.

Throughput is the noisy part. Compare it as a **median over N repeats** rather
than a single number:

```bash
for i in $(seq 1 5); do wxpath-bench run local_fixture --out benchmarks/results; done
# then diff effective_pages_per_s across the report.json files (median, p10/p90).
```

Discarding one warmup run first (DNS cache, connector pool) tightens the
distribution. An in-harness `--repeat/--warmup` aggregator is a v1 follow-up;
for now `runner.py`/`report.py` are unchanged and you repeat at the shell.

## Report card

A run prints (and writes to `report.txt`) a single fixed-width box covering:

- **Throughput**  — effective pages/s, bytes/s
- **Latency**    — p50/p95/p99 ms, EWMA, tail-ratio
- **Reliability**— success %, status histogram, retries, top error hosts
- **Coverage**   — unique URLs (HLL), unique content (HLL), hosts, depth dist
- **Extraction** — selector hit rate, extractions/URL, parse failures
- **Pending instrumentation** — metrics that need wxpath changes (visible
  with `<pending>` and the reason). `--strict` fails the run when any are
  present; in v0 all six are always pending, so `--strict` always fails today.
  It is a forward-looking gate for when that instrumentation lands — leave it
  off for normal baseline runs.

## Architecture (one paragraph)

`runner.py` registers a `BenchHook` (defined in `collectors.py`) on wxpath's
global hook registry, then calls `WxPathDriver` (in `driver.py`) which iterates
`wxpath_async_blocking_iter` against the workload's seeds with `settings_overrides`
applied. The hook captures per-URL latency, depth, byte size, content SHA-1 and
extraction counts into a `Recorder`. After the iterator returns, `runner.py`
snapshots `crawler._stats` (`StatsSnapshot`) and feeds both Recorder + StatsSnapshot
into `report.build_card`, which honors strict ownership (no double-counting) and
marks unimplementable metrics as `<pending>`.

## Adding a workload

1. Drop `myworkload.yaml` into `benchmarks/workloads/` matching the schema in
   any existing file. Set `runnable: true` and `kind: single_host` for v0.
2. (Optional) Add a seed file to `benchmarks/corpora/`.
3. `wxpath-bench run myworkload`.

## Adding a driver (future)

Implement the `Driver` protocol in `driver.py`:

```python
class Driver(Protocol):
    name: str
    version: str
    def run(self, workload: Workload, on_event: Callable[[Event], None]) -> DriverResult: ...
```

A future `ScrapyDriver` / `Crawl4AIDriver` slots in here so the same workloads
produce comparable cards across engines.

## Limitations (v0)

- Latency quantiles use a 200k-sample reservoir with `numpy.percentile`.
  DDSketch will replace this once wxpath emits per-URL latency samples.
- Several metrics in "Pending instrumentation" require changes in `src/wxpath/`;
  this harness imposes ZERO changes on the wxpath package.
- The wxpath hook registry has no public deregister. The runner pokes
  `registry._global_hooks` in `try/finally`. Refuses to start if another
  `BenchHook` instance is already registered. Subprocess-per-run is the v1 path
  if matrix benchmarks are added.
- Wall-clock limit is cooperative between yields; one in-flight request may
  overrun. Overrun is recorded in `meta.json` as `limit_overrun_s`.
