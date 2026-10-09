"""
Report snapshot for the web UI: plain JSON with numbers in cores / GiB and
timestamps in unix seconds (the browser formats them in its own time zone).
"""

import time
from typing import Any, Dict, List, Optional

from ..config import Config
from ..core.recommender import Recommendation
from ..core.runs import GIB


def _r(v: Optional[float], digits: int = 3) -> Optional[float]:
    return None if v is None else round(v, digits)


def _gib(b: Optional[float]) -> Optional[float]:
    return None if b is None else round(b / GIB, 3)


def _status(rec: Recommendation) -> str:
    if rec.is_risky and rec.is_wasteful:
        return "risky+waste"
    if rec.is_risky:
        return "under"
    if rec.is_wasteful:
        return "over"
    return "ok"


def _positive(v: Optional[float]) -> Optional[float]:
    return v if v is not None and v > 0 else None


def build_snapshot(
    recommendations: List[Recommendation],
    config: Config,
    started_at: float,
    finished_at: Optional[float] = None,
) -> Dict[str, Any]:
    workloads: List[Dict[str, Any]] = []
    runs: List[Dict[str, Any]] = []

    for wid, rec in enumerate(recommendations):
        g = rec.group
        workloads.append({
            "id": wid,
            "namespace": g.namespace,
            "workload": g.base_name,
            "dag_id": g.dag_id,
            "task_id": g.task_id,
            "container": g.container,
            "runs": len(g.runs) or len(g.pod_names),
            "runtime_h": _r(g.runtime_hours, 2),
            "cpu_req": _r(g.cpu_request),
            "cpu_avg": _r(g.avg_cpu_usage),
            "cpu_max": _r(g.max_cpu_usage),
            "cpu_rec": _r(rec.recommended_cpu_request),
            "cpu_waste": _r(_positive(rec.cpu_waste_cores)),
            "cpu_waste_pct": _r(_positive(rec.cpu_waste_ratio)),
            "mem_req": _gib(g.memory_request),
            "mem_avg": _gib(g.avg_memory_usage),
            "mem_max": _gib(g.max_memory_usage),
            "mem_rec": _gib(rec.recommended_memory_request),
            "mem_waste": _gib(_positive(rec.memory_waste_bytes)),
            "mem_waste_pct": _r(_positive(rec.memory_waste_ratio)),
            "cpu_idle_ch": _r(g.cpu_idle_core_hours, 2),
            "mem_idle_gh": _r(g.mem_idle_gib_hours, 2),
            "oom_runs": g.oom_runs,
            "request_changed": g.request_changed,
            "status": _status(rec),
            "notes": rec.reasons,
        })
        for run in g.runs:
            runs.append({
                "wid": wid,
                "pod": run.pod,
                "run_id": run.run_id,
                "try": run.try_number,
                "start": int(run.start),
                "end": int(run.end),
                "duration_h": _r(run.duration_hours, 3),
                "cpu_req": _r(run.cpu_request),
                "cpu_lim": _r(run.cpu_limit),
                "cpu_avg": _r(run.cpu_avg),
                "cpu_max": _r(run.cpu_max),
                "cpu_over": _r(run.cpu_over),
                "cpu_over_pct": _r(run.cpu_over_ratio),
                "mem_req": _gib(run.mem_request),
                "mem_lim": _gib(run.mem_limit),
                "mem_avg": _gib(run.mem_avg),
                "mem_max": _gib(run.mem_max),
                "mem_over": _gib(run.mem_over),
                "mem_over_pct": _r(run.mem_over_ratio),
                "cpu_idle_ch": _r(run.cpu_idle_core_hours, 3),
                "mem_idle_gh": _r(run.mem_idle_gib_hours, 3),
                "oom": run.oom_killed,
            })

    all_runs = [run for rec in recommendations for run in rec.group.runs]
    flagged = [w for w in workloads if w["status"] != "ok"]
    summary = {
        "workloads": len(workloads),
        "runs": len(all_runs),
        "over": sum(1 for w in workloads if w["status"] in ("over", "risky+waste")),
        "under": sum(1 for w in workloads if w["status"] in ("under", "risky+waste")),
        "oom_runs": sum(1 for r in all_runs if r.oom_killed),
        "mem_waste_gib": round(sum(w["mem_waste"] or 0 for w in flagged if w["status"] != "under"), 1),
        "cpu_waste_cores": round(sum(w["cpu_waste"] or 0 for w in flagged if w["status"] != "under"), 2),
        "cpu_req_ch": round(sum(r.cpu_requested_core_hours for r in all_runs), 1),
        "cpu_used_ch": round(sum(r.cpu_used_core_hours for r in all_runs), 1),
        "cpu_idle_ch": round(sum(r.cpu_idle_core_hours for r in all_runs), 1),
        "mem_req_gh": round(sum(r.mem_requested_gib_hours for r in all_runs), 1),
        "mem_used_gh": round(sum(r.mem_used_gib_hours for r in all_runs), 1),
        "mem_idle_gh": round(sum(r.mem_idle_gib_hours for r in all_runs), 1),
    }
    return {
        "generated_at": int(finished_at or time.time()),
        "started_at": int(started_at),
        "lookback_days": config.prometheus.lookback_days,
        "namespaces": list(config.analysis.namespaces or []),
        "source": config.prometheus.url,
        "waste_threshold": config.analysis.waste_threshold_ratio,
        "request_buffer": config.analysis.memory_request_buffer,
        "summary": summary,
        "workloads": workloads,
        "runs": runs,
    }
