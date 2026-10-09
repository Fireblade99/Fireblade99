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
from typing import List, Optional, Tuple

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

    @property
    def key(self) -> Tuple[str, str, str]:
        return (self.namespace, self.base_name, self.container)
