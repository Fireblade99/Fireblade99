"""
Recommendation engine.

For each workload group the recommender:
- Computes recommended request  = max_usage × (1 + request_buffer)
- Computes recommended limit    = max_usage × (1 + limit_buffer)
- Flags waste when request >> max_usage (over-provisioned)
- Flags risk  when max_usage  > request (under-provisioned / OOM risk)
"""

from dataclasses import dataclass, field
from typing import List, Optional

from ..utils import fmt_bytes as _fmt_bytes
from .grouper import WorkloadGroup


@dataclass
class Recommendation:
    group: WorkloadGroup

    # Recommended values (None if no Prometheus data available)
    recommended_cpu_request: Optional[float] = None    # cores
    recommended_memory_request: Optional[float] = None  # bytes
    recommended_cpu_limit: Optional[float] = None       # cores
    recommended_memory_limit: Optional[float] = None    # bytes

    # Waste = request − max_usage  (positive → over-provisioned)
    cpu_waste_cores: Optional[float] = None
    memory_waste_bytes: Optional[float] = None
    cpu_waste_ratio: Optional[float] = None      # waste / request
    memory_waste_ratio: Optional[float] = None   # waste / request

    is_wasteful: bool = False      # over-provisioned beyond threshold
    is_risky: bool = False         # under-provisioned (OOM / throttle risk)

    # Per-resource verdict: "down" (request can be cut), "up" (needs more), "ok"
    cpu_action: str = "ok"
    memory_action: str = "ok"
    # Why "up": "over_request", "near_request" (no headroom), "at_limit", "oom"
    cpu_flag: str = ""
    memory_flag: str = ""
    reasons: List[str] = field(default_factory=list)

    # ------------------------------------------------------------------
    # Convenience properties
    # ------------------------------------------------------------------

    @property
    def memory_waste_gib(self) -> Optional[float]:
        return (
            self.memory_waste_bytes / (1024 ** 3)
            if self.memory_waste_bytes is not None
            else None
        )

    @property
    def memory_request_gib(self) -> Optional[float]:
        return (
            self.group.memory_request / (1024 ** 3)
            if self.group.memory_request is not None
            else None
        )

    @property
    def max_memory_gib(self) -> Optional[float]:
        return (
            self.group.max_memory_usage / (1024 ** 3)
            if self.group.max_memory_usage is not None
            else None
        )

    @property
    def recommended_memory_gib(self) -> Optional[float]:
        return (
            self.recommended_memory_request / (1024 ** 3)
            if self.recommended_memory_request is not None
            else None
        )


class Recommender:
    def __init__(
        self,
        cpu_request_buffer: float = 0.20,
        memory_request_buffer: float = 0.20,
        cpu_limit_buffer: float = 0.50,
        memory_limit_buffer: float = 0.50,
        waste_threshold_ratio: float = 0.50,
        min_waste_cpu_cores: float = 0.10,
        min_waste_memory_mb: float = 100.0,
        cpu_under_tolerance: float = 0.20,
        memory_min_headroom: float = 0.10,
        limit_saturation_ratio: float = 0.95,
    ) -> None:
        self._cpu_req_buf = cpu_request_buffer
        self._mem_req_buf = memory_request_buffer
        self._cpu_lim_buf = cpu_limit_buffer
        self._mem_lim_buf = memory_limit_buffer
        self._waste_ratio = waste_threshold_ratio
        self._min_cpu = min_waste_cpu_cores
        self._min_mem = min_waste_memory_mb * 1024 * 1024  # → bytes
        # CPU is compressible: bursting a bit above the request is normal,
        # flag it only when the peak is this much above the request
        self._cpu_tol = cpu_under_tolerance
        # Memory peak within this share of the request = no headroom left
        self._mem_headroom = memory_min_headroom
        # Peak at this share of the limit = capped by the limit (OOM / throttling)
        self._limit_ratio = limit_saturation_ratio

    # ------------------------------------------------------------------

    def recommend(self, group: WorkloadGroup) -> Recommendation:
        rec = Recommendation(group=group)
        reasons: List[str] = []

        # ── CPU ────────────────────────────────────────────────────────
        if group.max_cpu_usage is not None:
            rec.recommended_cpu_request = group.max_cpu_usage * (1 + self._cpu_req_buf)
            rec.recommended_cpu_limit = group.max_cpu_usage * (1 + self._cpu_lim_buf)

            if group.cpu_request is not None:
                waste = group.cpu_request - group.max_cpu_usage
                rec.cpu_waste_cores = waste
                if group.cpu_request > 0:
                    rec.cpu_waste_ratio = waste / group.cpu_request

                if (
                    waste > 0
                    and rec.cpu_waste_ratio is not None
                    and rec.cpu_waste_ratio > self._waste_ratio
                    and waste > self._min_cpu
                ):
                    rec.cpu_action = "down"
                    reasons.append(
                        f"CPU request is {rec.cpu_waste_ratio:.0%} wasted "
                        f"({group.cpu_request:.3f} cores requested, "
                        f"{group.max_cpu_usage:.3f} cores max used)"
                    )

                if group.max_cpu_usage > group.cpu_request * (1 + self._cpu_tol):
                    rec.is_risky = True
                    rec.cpu_action = "up"
                    rec.cpu_flag = "over_request"
                    reasons.append(
                        f"CPU under-provisioned: max usage {group.max_cpu_usage:.3f} cores "
                        f"exceeds request {group.cpu_request:.3f} cores (throttle risk)"
                    )

        # ── Memory ────────────────────────────────────────────────────
        if group.max_memory_usage is not None:
            rec.recommended_memory_request = group.max_memory_usage * (1 + self._mem_req_buf)
            rec.recommended_memory_limit = group.max_memory_usage * (1 + self._mem_lim_buf)

            if group.memory_request is not None:
                waste = group.memory_request - group.max_memory_usage
                rec.memory_waste_bytes = waste
                if group.memory_request > 0:
                    rec.memory_waste_ratio = waste / group.memory_request

                if (
                    waste > 0
                    and rec.memory_waste_ratio is not None
                    and rec.memory_waste_ratio > self._waste_ratio
                    and waste > self._min_mem
                ):
                    from ..utils import fmt_bytes  # local import avoids cycles

                    rec.memory_action = "down"
                    reasons.append(
                        f"Memory request is {rec.memory_waste_ratio:.0%} wasted "
                        f"({fmt_bytes(group.memory_request)} requested, "
                        f"{fmt_bytes(group.max_memory_usage)} max used)"
                    )

                if waste < 0:
                    rec.is_risky = True
                    rec.memory_action = "up"
                    rec.memory_flag = "over_request"
                    from ..utils import fmt_bytes

                    reasons.append(
                        f"Memory under-provisioned: max usage "
                        f"{fmt_bytes(group.max_memory_usage)} exceeds request "
                        f"{fmt_bytes(group.memory_request)} (OOM risk)"
                    )

        # ── Saturation: the peak is under the request but there is no room left ──
        g = group
        if g.max_memory_usage is not None and rec.memory_action != "up":
            if g.memory_limit and g.max_memory_usage >= g.memory_limit * self._limit_ratio:
                rec.is_risky = True
                rec.memory_action, rec.memory_flag = "up", "at_limit"
                reasons.append(
                    f"Memory peak {_fmt_bytes(g.max_memory_usage)} is at the limit "
                    f"{_fmt_bytes(g.memory_limit)} (OOM risk)"
                )
            elif g.memory_request and g.max_memory_usage >= g.memory_request * (1 - self._mem_headroom):
                rec.is_risky = True
                rec.memory_action, rec.memory_flag = "up", "near_request"
                reasons.append(
                    f"Memory peak is {g.max_memory_usage / g.memory_request:.0%} of the request "
                    f"{_fmt_bytes(g.memory_request)} (no headroom)"
                )
        if (
            g.max_cpu_usage is not None
            and rec.cpu_action != "up"
            and g.cpu_limit
            and g.max_cpu_usage >= g.cpu_limit * self._limit_ratio
        ):
            rec.is_risky = True
            rec.cpu_action, rec.cpu_flag = "up", "at_limit"
            reasons.append(
                f"CPU peak {g.max_cpu_usage:.3f} cores is at the limit {g.cpu_limit:.3f} (throttled)"
            )

        # ── OOM kills (peak memory before the kill may be under the request) ──
        if group.oom_runs:
            rec.is_risky = True
            rec.memory_action = "up"
            rec.memory_flag = "oom"
            reasons.append(
                f"OOMKilled in {group.oom_runs} of {len(group.runs)} runs "
                f"(memory limit {_fmt_bytes(group.memory_limit)})"
            )

        rec.reasons = reasons
        # Mark as wasteful only for positive over-provisioning reasons
        rec.is_wasteful = any(
            "wasted" in r for r in reasons
        )
        return rec

    def process_all(self, groups: List[WorkloadGroup]) -> List[Recommendation]:
        return [self.recommend(g) for g in groups]
