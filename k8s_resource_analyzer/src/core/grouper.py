"""
Pod-name grouper
================
Strips random / numeric suffixes from Kubernetes pod names so that pods
belonging to the same logical workload are treated as one group.

Supported patterns
------------------
1. Deployment   → ``my-app-7d4f8c9b6-5xkp2``      base: ``my-app``
2. Job / Airflow → ``daily-ads-clicks-task-6bbvi8rr`` base: ``daily-ads-clicks-task``
3. StatefulSet  → ``redis-0``, ``redis-2``          base: ``redis``

A suffix is considered *random* (heuristic) when it:
- consists of `[a-z0-9]` only, AND
- contains at least one digit AND at least one letter
  (pure words like "deployment" or "backend" are **not** stripped).
"""

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, List, Optional, Tuple

if TYPE_CHECKING:
    from .runs import PodRun

# ------------------------------------------------------------------
# Compiled patterns – order matters: most specific first
# ------------------------------------------------------------------

# Deployment:  base  -  <rs_hash 6-12>  -  <pod_hash 5>
_DEPLOY_RE = re.compile(r"^(.*)-([a-z0-9]{6,12})-([a-z0-9]{5})$")

# Job / CronJob / Airflow:  base  -  <random 5-12>
_JOB_RE = re.compile(r"^(.*)-([a-z0-9]{5,12})$")

# StatefulSet:  base  -  <integer>
_STS_RE = re.compile(r"^(.*)-(\d+)$")


def _is_random(s: str) -> bool:
    """
    Return True when *s* looks like a random hash rather than a meaningful word.

    Rules (all chars must be lowercase alphanumeric):
    - Exactly 5 chars  → always random (standard k8s pod-hash)
    - Exactly 8 chars  → always random (Airflow-style suffix, can be all-letter)
    - Other lengths    → must contain both letters AND digits (avoids stripping
                         meaningful words like 'backend', 'frontend', etc.)
    """
    if not s.islower() and not s.isalnum():
        return False
    if len(s) == 5:
        return True
    if len(s) == 8:
        return True
    return any(c.isdigit() for c in s) and any(c.isalpha() for c in s)


def extract_base_name(pod_name: str) -> str:
    """
    Return the workload base name for *pod_name*.

    Examples::

        extract_base_name("daily-ads-clicks-task-6bbvi8rr") == "daily-ads-clicks-task"
        extract_base_name("my-app-7d4f8c9b6-5xkp2")        == "my-app"
        extract_base_name("redis-0")                        == "redis"
        extract_base_name("nginx")                          == "nginx"
    """
    # 1) Deployment pattern (two random segments)
    m = _DEPLOY_RE.match(pod_name)
    if m:
        base, rs_hash, pod_hash = m.group(1), m.group(2), m.group(3)
        if _is_random(rs_hash) and _is_random(pod_hash) and len(base) >= 2:
            return base

    # 2) Job / Airflow pattern (one random segment)
    m = _JOB_RE.match(pod_name)
    if m:
        base, suffix = m.group(1), m.group(2)
        if _is_random(suffix) and len(base) >= 2:
            return base

    # 3) StatefulSet pattern (numeric index)
    m = _STS_RE.match(pod_name)
    if m:
        base = m.group(1)
        if len(base) >= 2:
            return base

    return pod_name


# ------------------------------------------------------------------
# Data model
# ------------------------------------------------------------------

@dataclass
class WorkloadGroup:
    """A logical workload aggregated from one or more pods."""

    namespace: str
    base_name: str
    container: str

    # All pod names seen (current K8s state + historical Prometheus data)
    pod_names: List[str] = field(default_factory=list)

    # Resource requests/limits – taken from the latest running pod
    cpu_request: Optional[float] = None    # cores
    memory_request: Optional[float] = None  # bytes
    cpu_limit: Optional[float] = None      # cores
    memory_limit: Optional[float] = None   # bytes

    # Peak observed usage across all pods in this group over the lookback window
    max_cpu_usage: Optional[float] = None    # cores
    max_memory_usage: Optional[float] = None  # bytes

    # Every run (pod) of this workload seen in the lookback window
    runs: List["PodRun"] = field(default_factory=list)

    @property
    def key(self) -> Tuple[str, str, str]:
        return (self.namespace, self.base_name, self.container)

    # ── Aggregates over runs ───────────────────────────────────────────

    @property
    def dag_id(self) -> str:
        return next((r.dag_id for r in reversed(self.runs) if r.dag_id), "")

    @property
    def task_id(self) -> str:
        return next((r.task_id for r in reversed(self.runs) if r.task_id), "")

    @property
    def runtime_hours(self) -> float:
        return sum(r.duration_hours for r in self.runs)

    @property
    def avg_cpu_usage(self) -> Optional[float]:
        """Average CPU over all runs, weighted by run duration."""
        return _weighted(self.runs, "cpu_avg")

    @property
    def avg_memory_usage(self) -> Optional[float]:
        """Average memory over all runs, weighted by run duration."""
        return _weighted(self.runs, "mem_avg")

    @property
    def cpu_requested_core_hours(self) -> float:
        return sum(r.cpu_requested_core_hours for r in self.runs)

    @property
    def cpu_idle_core_hours(self) -> float:
        return sum(r.cpu_idle_core_hours for r in self.runs)

    @property
    def mem_requested_gib_hours(self) -> float:
        return sum(r.mem_requested_gib_hours for r in self.runs)

    @property
    def mem_idle_gib_hours(self) -> float:
        return sum(r.mem_idle_gib_hours for r in self.runs)

    @property
    def oom_runs(self) -> int:
        return sum(1 for r in self.runs if r.oom_killed)

    @property
    def request_changed(self) -> bool:
        """True when the developer changed requests during the window."""
        cpu = {r.cpu_request for r in self.runs if r.cpu_request is not None}
        mem = {r.mem_request for r in self.runs if r.mem_request is not None}
        return len(cpu) > 1 or len(mem) > 1


def _weighted(runs: List["PodRun"], attr: str) -> Optional[float]:
    pairs = [(getattr(r, attr), r.duration_hours) for r in runs if getattr(r, attr) is not None]
    if not pairs:
        return None
    total_w = sum(w for _, w in pairs)
    if total_w <= 0:
        return sum(v for v, _ in pairs) / len(pairs)
    return sum(v * w for v, w in pairs) / total_w
