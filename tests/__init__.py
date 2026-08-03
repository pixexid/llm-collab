# GH-503: authorize the runtime freshness-gate bypass for this test run (per-run
# token + sentinel; see _runtime_gate_testkit). Importing any tests.<module> runs
# this, so focused and discover runs alike get the bypass without a generic env
# switch. Never runs in production (the tests package is not imported there).
from . import _runtime_gate_testkit  # noqa: F401
