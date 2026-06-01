# benchmarks/corpora

Seed URL lists for benchmark workloads. Each `.txt` file is one URL per line,
`#`-prefixed and empty lines ignored. Workloads reference these via:

```yaml
seeds:
  type: file
  path: ../corpora/<name>.txt
```

## Bundled corpora

| File | Use | Size | Site policy |
|------|-----|------|-------------|
| `docs_python_seed.txt` | `single_host` smoke | 10 URLs | docs.python.org — stable, robots-friendly |

## Adding a corpus

1. Drop a `.txt` file in this directory.
2. Reference it from a workload YAML via `seeds.path`.
3. Pick a site whose robots.txt allows your crawl pattern. Avoid corporate
   targets that may rate-limit aggressively unless your workload has
   `respect_robots: true` and a low concurrency.

Large corpora (>10k URLs) should not be committed to git. Use a manifest
file (future) that downloads on demand, or run with `--max-urls` to cap.
