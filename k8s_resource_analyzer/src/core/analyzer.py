"""
Main analysis orchestrator.

Steps
-----
1. (optional) Fetch current pod resource requests/limits from the Kubernetes API.
2. Fetch CPU and memory usage of every pod (= run) from Prometheus /
   VictoriaMetrics, chunk by chunk, and turn it into ``PodRun`` records.
3. Attach requests/limits, Airflow labels and OOM kills from kube-state-metrics.
4. Group runs by workload base-name (namespace + base_name + container).
"""

import logging
import time
from typing import Dict, List, Optional, Tuple

from ..clients.prom_client import PrometheusClient, SeriesKey, ns_selector, parse_step_seconds
from ..config import Config
from .grouper import WorkloadGroup, extract_base_name
from .runs import PodRun, build_runs

logger = logging.getLogger(__name__)

GroupKey = Tuple[str, str, str]  # (namespace, base_name, container)

_USAGE_SEL = 'container!="",container!="POD"'


class ResourceAnalyzer:
    def __init__(self, config: Config) -> None:
        self.cfg = config
        self._k8s = None
        if config.kubernetes.enabled:
            from ..clients.k8s_client import K8sClient  # needs the kubernetes package

            self._k8s = K8sClient(
                in_cluster=config.kubernetes.in_cluster,
                kubeconfig=config.kubernetes.kubeconfig,
            )
        p = config.prometheus
        self._prom = PrometheusClient(
            url=p.url,
            timeout=p.timeout,
            chunk_hours=p.chunk_hours,
            pause_seconds=p.pause_seconds,
            use_proxy=p.use_proxy,
            proxy_url=p.proxy_url,
        )

    # ------------------------------------------------------------------

    def analyze(self) -> List[WorkloadGroup]:
        """Run full analysis and return grouped workloads with usage data."""
        cfg = self.cfg
        groups: Dict[GroupKey, WorkloadGroup] = {}

        # ── 1. Kubernetes data (live pods only) ────────────────────────
        if self._k8s is not None:
            self._load_k8s(groups)

        # ── 2. Usage per run ───────────────────────────────────────────
        runs = self._load_runs()

        # ── 3. kube-state-metrics: requests/limits, labels, OOM ────────
        self._attach_requests(runs)
        self._attach_labels(runs)
        self._attach_oom(runs)

        # ── 4. Group runs by workload ──────────────────────────────────
        for run in sorted(runs.values(), key=lambda r: r.start):
            key: GroupKey = (run.namespace, run.base_name, run.container)
            g = groups.get(key)
            if g is None:
                g = groups[key] = WorkloadGroup(
                    namespace=run.namespace, base_name=run.base_name, container=run.container
                )
            g.runs.append(run)
            if run.pod not in g.pod_names:
                g.pod_names.append(run.pod)

        for g in groups.values():
            _fill_group(g)

        logger.info(
            "Prometheus: %d runs in %d workload groups (%d HTTP requests)",
            len(runs), len(groups), self._prom.requests_made,
        )
        return list(groups.values())

    # ------------------------------------------------------------------
    # Steps
    # ------------------------------------------------------------------

    def _load_k8s(self, groups: Dict[GroupKey, WorkloadGroup]) -> None:
        cfg = self.cfg
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
        excluded_containers = set(cfg.analysis.exclude_containers or [])
        for pr in pod_resources:
            if pr.container_name in excluded_containers:
                continue
            base = extract_base_name(pr.pod_name)
            key: GroupKey = (pr.namespace, base, pr.container_name)
            g = groups.get(key)
            if g is None:
                g = groups[key] = WorkloadGroup(
                    namespace=pr.namespace, base_name=base, container=pr.container_name
                )
            if pr.pod_name not in g.pod_names:
                g.pod_names.append(pr.pod_name)
            # Live values win over history: this is what the next run will request
            if pr.cpu_request is not None:
                g.cpu_request = pr.cpu_request
            if pr.memory_request is not None:
                g.memory_request = pr.memory_request
            if pr.cpu_limit is not None:
                g.cpu_limit = pr.cpu_limit
            if pr.memory_limit is not None:
                g.memory_limit = pr.memory_limit

    def _window(self) -> Tuple[float, float]:
        end = time.time()
        return end - self.cfg.prometheus.lookback_days * 86400, end

    def _load_runs(self) -> Dict[SeriesKey, PodRun]:
        cfg = self.cfg
        start, end = self._window()
        step = cfg.prometheus.step
        step_s = parse_step_seconds(step)
        sel = _USAGE_SEL + ns_selector(cfg.analysis.namespaces or None)
        by = "namespace, pod, container"

        logger.info(
            "Fetching CPU/memory per run from Prometheus (lookback=%dd, step=%s, chunk=%dh) …",
            cfg.prometheus.lookback_days, step, cfg.prometheus.chunk_hours,
        )
        try:
            # rate/max_over_time over one step: every raw sample is covered,
            # so short spikes between steps are not lost
            cpu = self._prom.range_series(
                f"max by ({by}) (rate(container_cpu_usage_seconds_total{{{sel}}}[{step}]))",
                start, end, step_s,
            )
            mem = self._prom.range_series(
                f"max by ({by}) (max_over_time(container_memory_working_set_bytes{{{sel}}}[{step}]))",
                start, end, step_s,
            )
        except Exception as exc:
            raise RuntimeError(f"Prometheus query failed: {exc}") from exc
        logger.info("Prometheus returned %d CPU series and %d memory series", len(cpu), len(mem))

        runs = build_runs(cpu, mem, step_s, extract_base_name)
        return {k: r for k, r in runs.items() if self._wanted(*k)}

    def _attach_requests(self, runs: Dict[SeriesKey, PodRun]) -> None:
        logger.info("Fetching requests/limits from kube-state-metrics …")
        start, end = self._window()
        ns = ns_selector(self.cfg.analysis.namespaces or None)
        for kind, cpu_attr, mem_attr in (
            ("requests", "cpu_request", "mem_request"),
            ("limits", "cpu_limit", "mem_limit"),
        ):
            query = (
                f"max by (namespace, pod, container, resource) (max_over_time("
                f'kube_pod_container_resource_{kind}{{resource=~"cpu|memory",container!=""{ns}}}[$window]))'
            )
            try:
                result = self._prom.instant_max(query, start, end)
            except Exception as exc:
                logger.warning("kube-state-metrics %s query failed: %s", kind, exc)
                continue
            for labels, val in result.values():
                run = runs.get(_series_key(labels))
                if run is not None:
                    setattr(run, cpu_attr if labels.get("resource") == "cpu" else mem_attr, val)
        missing = sum(1 for r in runs.values() if r.cpu_request is None and r.mem_request is None)
        if missing:
            logger.info("%d runs have no requests in kube-state-metrics", missing)

    def _attach_labels(self, runs: Dict[SeriesKey, PodRun]) -> None:
        names = {k: v for k, v in (self.cfg.analysis.airflow_labels or {}).items() if v}
        if not names or not runs:
            return
        start, end = self._window()
        ns = ns_selector(self.cfg.analysis.namespaces or None)
        query = (
            f"max by (namespace, pod, {', '.join(names.values())}) "
            f'(max_over_time(kube_pod_labels{{pod!=""{ns}}}[$window]))'
        )
        try:
            result = self._prom.instant_max(query, start, end)
        except Exception as exc:
            logger.warning("kube_pod_labels query failed (no DAG/task columns): %s", exc)
            return
        by_pod: Dict[Tuple[str, str], Dict[str, str]] = {}
        for labels, _ in result.values():
            values = {attr: labels.get(lbl, "") for attr, lbl in names.items()}
            if any(values.values()):
                by_pod[(labels.get("namespace", ""), labels.get("pod", ""))] = values
        for run in runs.values():
            for attr, val in by_pod.get((run.namespace, run.pod), {}).items():
                setattr(run, attr, val)
        logger.info("Airflow labels found for %d pods", len(by_pod))

    def _attach_oom(self, runs: Dict[SeriesKey, PodRun]) -> None:
        """
        A run is OOM-killed if any of these saw it:
        * terminated_reason      – the container ended with OOMKilled and was not
                                   restarted (Airflow task pods, Jobs);
        * last_terminated_reason – it was OOM-killed and restarted (Deployments);
        * cAdvisor OOM counter   – the kernel OOM-killed a process in the container.
        The memory peak of such runs is underestimated: the spike to the limit
        happens between scrapes.
        """
        start, end = self._window()
        ns = ns_selector(self.cfg.analysis.namespaces or None)
        by = "max by (namespace, pod, container)"
        queries = {
            "terminated": f'{by} (max_over_time(kube_pod_container_status_terminated_reason'
                          f'{{reason="OOMKilled"{ns}}}[$window]))',
            "last_terminated": f'{by} (max_over_time(kube_pod_container_status_last_terminated_reason'
                               f'{{reason="OOMKilled"{ns}}}[$window]))',
            "oom_events": f'{by} (increase(container_oom_events_total{{container!=""{ns}}}[$window]))',
        }
        found = 0
        for name, query in queries.items():
            try:
                result = self._prom.instant_max(query, start, end)
            except Exception as exc:
                logger.warning("OOM query %s failed: %s", name, exc)
                continue
            for labels, val in result.values():
                run = runs.get(_series_key(labels))
                if run is not None and val > 0 and not run.oom_killed:
                    run.oom_killed = True
                    found += 1
        if found:
            logger.info("OOMKilled runs: %d", found)

    # ------------------------------------------------------------------

    def _wanted(self, ns: str, pod: str, container: str) -> bool:
        a = self.cfg.analysis
        if ns in set(a.exclude_namespaces or []):
            return False
        if a.namespaces and ns not in a.namespaces:
            return False
        return container not in set(a.exclude_containers or [])


def _series_key(labels: Dict[str, str]) -> SeriesKey:
    return (labels.get("namespace", ""), labels.get("pod", ""), labels.get("container", ""))


def _fill_group(g: WorkloadGroup) -> None:
    """Peak usage over all runs; requests/limits from the latest run unless live K8s set them."""
    cpu = [r.cpu_max for r in g.runs if r.cpu_max is not None]
    mem = [r.mem_max for r in g.runs if r.mem_max is not None]
    g.max_cpu_usage = max(cpu) if cpu else None
    g.max_memory_usage = max(mem) if mem else None

    def latest(attr: str) -> Optional[float]:
        return next((getattr(r, attr) for r in reversed(g.runs) if getattr(r, attr) is not None), None)

    if g.cpu_request is None:
        g.cpu_request = latest("cpu_request")
    if g.memory_request is None:
        g.memory_request = latest("mem_request")
    if g.cpu_limit is None:
        g.cpu_limit = latest("cpu_limit")
    if g.memory_limit is None:
        g.memory_limit = latest("mem_limit")
