"""
Per-run data
============
Every pod is one run (an Airflow task try, a Job execution, a replica).
For each run we keep what the developer requested and what the container
actually used, so the report can show the over-request per run and the
reserved-but-idle resource-hours over the week.
"""

import datetime
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from ..clients.prom_client import Points, SeriesKey

GIB = 1024.0 ** 3


@dataclass
class PodRun:
    namespace: str
    pod: str
    container: str
    base_name: str

    start: float  # unix seconds, first sample
    end: float    # unix seconds, last sample + one step

    cpu_max: Optional[float] = None   # cores
    cpu_avg: Optional[float] = None   # cores
    mem_max: Optional[float] = None   # bytes
    mem_avg: Optional[float] = None   # bytes

    cpu_request: Optional[float] = None  # cores
    cpu_limit: Optional[float] = None    # cores
    mem_request: Optional[float] = None  # bytes
    mem_limit: Optional[float] = None    # bytes

    # Airflow pod labels (only when kube-state-metrics exports them)
    dag_id: str = ""
    task_id: str = ""
    run_id: str = ""
    try_number: str = ""

    oom_killed: bool = False

    # ------------------------------------------------------------------

    @property
    def duration_hours(self) -> float:
        return max(0.0, self.end - self.start) / 3600.0

    @property
    def started_at(self) -> datetime.datetime:
        return datetime.datetime.fromtimestamp(self.start)

    @property
    def ended_at(self) -> datetime.datetime:
        return datetime.datetime.fromtimestamp(self.end)

    # Over-request = request − peak usage (positive → asked for too much)
    @property
    def cpu_over(self) -> Optional[float]:
        return _diff(self.cpu_request, self.cpu_max)

    @property
    def mem_over(self) -> Optional[float]:
        return _diff(self.mem_request, self.mem_max)

    @property
    def cpu_over_ratio(self) -> Optional[float]:
        return _ratio(self.cpu_over, self.cpu_request)

    @property
    def mem_over_ratio(self) -> Optional[float]:
        return _ratio(self.mem_over, self.mem_request)

    # Resource-hours: request × duration and average usage × duration
    @property
    def cpu_requested_core_hours(self) -> float:
        return (self.cpu_request or 0.0) * self.duration_hours

    @property
    def cpu_used_core_hours(self) -> float:
        return (self.cpu_avg or 0.0) * self.duration_hours

    @property
    def mem_requested_gib_hours(self) -> float:
        return (self.mem_request or 0.0) / GIB * self.duration_hours

    @property
    def mem_used_gib_hours(self) -> float:
        return (self.mem_avg or 0.0) / GIB * self.duration_hours

    # Idle = reserved on the node but not used (never negative)
    @property
    def cpu_idle_core_hours(self) -> float:
        return max(0.0, self.cpu_requested_core_hours - self.cpu_used_core_hours)

    @property
    def mem_idle_gib_hours(self) -> float:
        return max(0.0, self.mem_requested_gib_hours - self.mem_used_gib_hours)


def _diff(request: Optional[float], used: Optional[float]) -> Optional[float]:
    if request is None or used is None:
        return None
    return request - used


def _ratio(part: Optional[float], whole: Optional[float]) -> Optional[float]:
    if part is None or not whole:
        return None
    return part / whole


def _stats(points: Points) -> Tuple[float, float]:
    values = [v for _, v in points]
    return max(values), sum(values) / len(values)


def build_runs(
    cpu: Dict[SeriesKey, Points],
    mem: Dict[SeriesKey, Points],
    step_seconds: int,
    base_name_of,
) -> Dict[SeriesKey, PodRun]:
    """One PodRun per (namespace, pod, container) seen in the usage series."""
    runs: Dict[SeriesKey, PodRun] = {}
    for key in set(cpu) | set(mem):
        stamps = [ts for ts, _ in cpu.get(key, [])] + [ts for ts, _ in mem.get(key, [])]
        if not stamps:
            continue
        ns, pod, container = key
        run = PodRun(
            namespace=ns,
            pod=pod,
            container=container,
            base_name=base_name_of(pod),
            # Each point covers the preceding step (rate / max_over_time window)
            start=min(stamps) - step_seconds,
            end=max(stamps),
        )
        if cpu.get(key):
            run.cpu_max, run.cpu_avg = _stats(cpu[key])
        if mem.get(key):
            run.mem_max, run.mem_avg = _stats(mem[key])
        runs[key] = run
    return runs
