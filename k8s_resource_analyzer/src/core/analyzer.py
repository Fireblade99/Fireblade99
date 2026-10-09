"""
Main analysis orchestrator.

Steps
-----
1. Fetch current pod resource requests/limits from the Kubernetes API.
2. Fetch peak CPU and memory usage from Prometheus (covers historical pods too).
3. Group everything by workload base-name (namespace + base_name + container).
4. Return a list of ``WorkloadGroup`` objects ready for the recommender.
"""

import logging
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

from ..clients.k8s_client import K8sClient
from ..clients.prom_client import PrometheusClient
from ..config import Config
from .grouper import WorkloadGroup, extract_base_name

logger = logging.getLogger(__name__)

GroupKey = Tuple[str, str, str]  # (namespace, base_name, container)


class ResourceAnalyzer:
    def __init__(self, config: Config) -> None:
        self.cfg = config
        self._k8s = K8sClient(
            in_cluster=config.kubernetes.in_cluster,
            kubeconfig=config.kubernetes.kubeconfig,
        )
        self._prom = PrometheusClient(url=config.prometheus.url)

    # ------------------------------------------------------------------

    def analyze(self) -> List[WorkloadGroup]:
        """Run full analysis and return grouped workloads with usage data."""
        cfg = self.cfg

        # ── 1. Kubernetes data ─────────────────────────────────────────
        logger.info("Fetching pod resources from Kubernetes …")
        pod_resources = self._k8s.get_pod_resources(
            namespaces=cfg.analysis.namespaces or None,
            exclude_namespaces=cfg.analysis.exclude_namespaces,
        )
        logger.info(
            "Fetched %d containers across %d pods",
            len(pod_resources),
            len({pr.pod_name for pr in pod_resources}),
        )

        groups: Dict[GroupKey, WorkloadGroup] = {}
        excluded_containers = set(cfg.analysis.exclude_containers or [])

        for pr in pod_resources:
            if pr.container_name in excluded_containers:
                continue
            base = extract_base_name(pr.pod_name)
            key: GroupKey = (pr.namespace, base, pr.container_name)

            if key not in groups:
                groups[key] = WorkloadGroup(
                    namespace=pr.namespace,
                    base_name=base,
                    container=pr.container_name,
                )

            g = groups[key]
            if pr.pod_name not in g.pod_names:
                g.pod_names.append(pr.pod_name)

            # Keep the latest non-None values (all replicas should be identical)
            if pr.cpu_request is not None:
                g.cpu_request = pr.cpu_request
            if pr.memory_request is not None:
                g.memory_request = pr.memory_request
            if pr.cpu_limit is not None:
                g.cpu_limit = pr.cpu_limit
            if pr.memory_limit is not None:
                g.memory_limit = pr.memory_limit

        # ── 2. Prometheus data ─────────────────────────────────────────
        logger.info(
            "Fetching max CPU/memory from Prometheus (lookback=%dd) …",
            cfg.prometheus.lookback_days,
        )
        prom_kwargs = dict(
            lookback_days=cfg.prometheus.lookback_days,
            step=cfg.prometheus.step,
            namespaces=cfg.analysis.namespaces or None,
        )
        try:
            prom_cpu = self._prom.get_max_cpu_usage(**prom_kwargs)
            prom_mem = self._prom.get_max_memory_usage(**prom_kwargs)
        except Exception as exc:
            raise RuntimeError(f"Prometheus query failed: {exc}") from exc

        logger.info(
            "Prometheus returned %d CPU series and %d memory series",
            len(prom_cpu),
            len(prom_mem),
        )

        # ── 2b. Requests/limits from kube-state-metrics ────────────────
        # This covers historical pods no longer visible in the K8s API.
        logger.info("Fetching historical requests/limits from kube-state-metrics …")
        try:
            ksm_cpu_req = self._prom.get_pod_cpu_requests(**prom_kwargs)
            ksm_mem_req = self._prom.get_pod_memory_requests(**prom_kwargs)
            ksm_cpu_lim = self._prom.get_pod_cpu_limits(**prom_kwargs)
            ksm_mem_lim = self._prom.get_pod_memory_limits(**prom_kwargs)
            logger.info(
                "kube-state-metrics returned %d CPU-request series", len(ksm_cpu_req)
            )
        except Exception as exc:
            logger.warning(
                "kube-state-metrics queries failed (no requests for historical pods): %s", exc
            )
            ksm_cpu_req = ksm_mem_req = ksm_cpu_lim = ksm_mem_lim = {}

        # ── 3. Aggregate Prometheus data per workload group ────────────
        # Use defaultdict so we start at 0.0 and take the running max.
        cpu_max_by_group: Dict[GroupKey, float] = defaultdict(float)
        mem_max_by_group: Dict[GroupKey, float] = defaultdict(float)

        # For requests from kube-state-metrics, value is constant per pod —
        # store last-seen value per group (pod with max value wins, all equal).
        ksm_cpu_req_by_group: Dict[GroupKey, float] = {}
        ksm_mem_req_by_group: Dict[GroupKey, float] = {}
        ksm_cpu_lim_by_group: Dict[GroupKey, float] = {}
        ksm_mem_lim_by_group: Dict[GroupKey, float] = {}

        excluded = set(cfg.analysis.exclude_namespaces or [])
        allowed_ns = set(cfg.analysis.namespaces) if cfg.analysis.namespaces else None

        excluded_containers = set(cfg.analysis.exclude_containers or [])

        def _group_key(ns: str, pod: str, container: str) -> Optional[GroupKey]:
            if ns in excluded:
                return None
            if allowed_ns and ns not in allowed_ns:
                return None
            if container in excluded_containers:
                return None
            return (ns, extract_base_name(pod), container)

        for (ns, pod, container), val in prom_cpu.items():
            key = _group_key(ns, pod, container)
            if key and val > cpu_max_by_group[key]:
                cpu_max_by_group[key] = val

        for (ns, pod, container), val in prom_mem.items():
            key = _group_key(ns, pod, container)
            if key and val > mem_max_by_group[key]:
                mem_max_by_group[key] = val

        for (ns, pod, container), val in ksm_cpu_req.items():
            key = _group_key(ns, pod, container)
            if key:
                ksm_cpu_req_by_group[key] = max(ksm_cpu_req_by_group.get(key, 0.0), val)

        for (ns, pod, container), val in ksm_mem_req.items():
            key = _group_key(ns, pod, container)
            if key:
                ksm_mem_req_by_group[key] = max(ksm_mem_req_by_group.get(key, 0.0), val)

        for (ns, pod, container), val in ksm_cpu_lim.items():
            key = _group_key(ns, pod, container)
            if key:
                ksm_cpu_lim_by_group[key] = max(ksm_cpu_lim_by_group.get(key, 0.0), val)

        for (ns, pod, container), val in ksm_mem_lim.items():
            key = _group_key(ns, pod, container)
            if key:
                ksm_mem_lim_by_group[key] = max(ksm_mem_lim_by_group.get(key, 0.0), val)

        # ── 4. Add groups seen in Prometheus but no longer in K8s ──────
        # (Completed jobs / Airflow tasks that have already been deleted)
        all_prom_keys = set(cpu_max_by_group) | set(mem_max_by_group)
        for key in all_prom_keys:
            if key not in groups:
                ns, base, container = key
                groups[key] = WorkloadGroup(
                    namespace=ns,
                    base_name=base,
                    container=container,
                )

        # ── 5. Attach peak usage and fill missing requests/limits ──────
        for key, group in groups.items():
            if key in cpu_max_by_group:
                group.max_cpu_usage = cpu_max_by_group[key]
            if key in mem_max_by_group:
                group.max_memory_usage = mem_max_by_group[key]
            # Fill requests/limits from kube-state-metrics for historical pods
            if group.cpu_request is None and key in ksm_cpu_req_by_group:
                group.cpu_request = ksm_cpu_req_by_group[key]
            if group.memory_request is None and key in ksm_mem_req_by_group:
                group.memory_request = ksm_mem_req_by_group[key]
            if group.cpu_limit is None and key in ksm_cpu_lim_by_group:
                group.cpu_limit = ksm_cpu_lim_by_group[key]
            if group.memory_limit is None and key in ksm_mem_lim_by_group:
                group.memory_limit = ksm_mem_lim_by_group[key]

        return list(groups.values())

