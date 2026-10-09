"""JSON reporter – outputs structured data for downstream processing."""

import json
from typing import Any, Dict, List, Optional

from ..core.recommender import Recommendation


def _gi(b: Optional[float]) -> Optional[float]:
    if b is None:
        return None
    return round(b / (1024 ** 3), 3)


def _round(v: Optional[float], digits: int = 4) -> Optional[float]:
    if v is None:
        return None
    return round(v, digits)


def to_dict(rec: Recommendation) -> Dict[str, Any]:
    g = rec.group
    return {
        "namespace": g.namespace,
        "workload": g.base_name,
        "container": g.container,
        "pod_count": len(g.pod_names),
        "pod_names": sorted(g.pod_names),
        "status": (
            "risky+wasteful"
            if rec.is_risky and rec.is_wasteful
            else "risky"
            if rec.is_risky
            else "wasteful"
            if rec.is_wasteful
            else "ok"
        ),
        "current_requests": {
            "cpu_cores": _round(g.cpu_request),
            "memory_bytes": g.memory_request,
            "memory_gib": _gi(g.memory_request),
            "cpu_limit_cores": _round(g.cpu_limit),
            "memory_limit_bytes": g.memory_limit,
            "memory_limit_gib": _gi(g.memory_limit),
        },
        "observed_max": {
            "cpu_cores": _round(g.max_cpu_usage),
            "memory_bytes": _round(g.max_memory_usage, 0),
            "memory_gib": _gi(g.max_memory_usage),
        },
        "recommended": {
            "cpu_request_cores": _round(rec.recommended_cpu_request),
            "memory_request_bytes": _round(rec.recommended_memory_request, 0),
            "memory_request_gib": _gi(rec.recommended_memory_request),
            "cpu_limit_cores": _round(rec.recommended_cpu_limit),
            "memory_limit_bytes": _round(rec.recommended_memory_limit, 0),
            "memory_limit_gib": _gi(rec.recommended_memory_limit),
        },
        "waste": {
            "cpu_cores": _round(rec.cpu_waste_cores),
            "cpu_ratio": _round(rec.cpu_waste_ratio, 3),
            "memory_bytes": _round(rec.memory_waste_bytes, 0),
            "memory_gib": _gi(rec.memory_waste_bytes),
            "memory_ratio": _round(rec.memory_waste_ratio, 3),
        },
        "reasons": rec.reasons,
    }


def to_json(
    recommendations: List[Recommendation],
    show_only_waste: bool = True,
    indent: int = 2,
) -> str:
    items = recommendations
    if show_only_waste:
        items = [r for r in items if r.is_wasteful or r.is_risky]
    return json.dumps([to_dict(r) for r in items], indent=indent, ensure_ascii=False)
