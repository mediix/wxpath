"""wxpath benchmark harness.

Drives wxpath through standardized crawl workloads and emits a uniform report
card so that future instrumentation work (P0/P1 in the metric catalogue) is
measurable as a delta against this baseline.
"""

__version__ = '0.1.0'

from benchmarks.runner import run_workload

__all__ = ['run_workload', '__version__']
