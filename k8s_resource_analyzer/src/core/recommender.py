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
    ) -> None:
        self._cpu_req_buf = cpu_request_buffer
        self._mem_req_buf = memory_request_buffer
        self._cpu_lim_buf = cpu_limit_buffer
        self._mem_lim_buf = memory_limit_buffer
        self._waste_ratio = waste_threshold_ratio
        self._min_cpu = min_waste_cpu_cores
        self._min_mem = min_waste_memory_mb * 1024 * 1024  # → bytes

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
                    reasons.append(
                        f"CPU request is {rec.cpu_waste_ratio:.0%} wasted "
                        f"({group.cpu_request:.3f} cores requested, "
                        f"{group.max_cpu_usage:.3f} cores max used)"
                    )

                if waste < 0:
                    rec.is_risky = True
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

                    reasons.append(
                        f"Memory request is {rec.memory_waste_ratio:.0%} wasted "
                        f"({fmt_bytes(group.memory_request)} requested, "
                        f"{fmt_bytes(group.max_memory_usage)} max used)"
                    )

                if waste < 0:
                    rec.is_risky = True
                    from ..utils import fmt_bytes

                    reasons.append(
                        f"Memory under-provisioned: max usage "
                        f"{fmt_bytes(group.max_memory_usage)} exceeds request "
                        f"{fmt_bytes(group.memory_request)} (OOM risk)"
                    )

        rec.reasons = reasons
        # Mark as wasteful only for positive over-provisioning reasons
        rec.is_wasteful = any(
            "wasted" in r for r in reasons
        )
        return rec

    def process_all(self, groups: List[WorkloadGroup]) -> List[Recommendation]:
        return [self.recommend(g) for g in groups]
