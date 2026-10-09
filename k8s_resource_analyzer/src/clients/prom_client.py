import logging
import time
from typing import Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

# Keyed by (namespace, pod_name, container_name)
UsageMap = Dict[Tuple[str, str, str], float]


class PrometheusClient:
    """
    Thin wrapper around the Prometheus / VictoriaMetrics HTTP API.

    Uses query_range + client-side max to avoid subquery syntax that some
    backends (VictoriaMetrics, older Prometheus) reject with 422.
    """

    def __init__(self, url: str, timeout: int = 120) -> None:
        self.url = url.rstrip("/")
        self._timeout = timeout
        self._session = requests.Session()

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    def test_connection(self) -> bool:
        try:
            resp = self._session.get(
                f"{self.url}/-/ready", timeout=5, allow_redirects=True
            )
            return resp.status_code == 200
        except Exception:
            return False

    def get_max_cpu_usage(
        self,
        lookback_days: int = 7,
        step: str = "1h",
        namespaces: Optional[List[str]] = None,
    ) -> UsageMap:
        """
        Return the **maximum** CPU usage (cores) per container observed
        over *lookback_days*. Rate window matches step to keep data volume low.
        """
        ns_filter = _ns_selector(namespaces)
        query = (
            f'rate(container_cpu_usage_seconds_total'
            f'{{container!="",container!="POD"{ns_filter}}}[{step}])'
        )
        return self._fetch_range_max(query, lookback_days, step)

    def get_max_memory_usage(
        self,
        lookback_days: int = 7,
        step: str = "1h",
        namespaces: Optional[List[str]] = None,
    ) -> UsageMap:
        """
        Return the **maximum** memory working-set (bytes) per container
        observed over *lookback_days*.
        """
        ns_filter = _ns_selector(namespaces)
        query = (
            f'container_memory_working_set_bytes'
            f'{{container!="",container!="POD"{ns_filter}}}'
        )
        return self._fetch_range_max(query, lookback_days, step)

    def get_pod_cpu_requests(
        self,
        lookback_days: int = 7,
        step: str = "1h",
        namespaces: Optional[List[str]] = None,
    ) -> UsageMap:
        """
        Return CPU requests (cores) per pod/container from kube-state-metrics.
        Covers historical pods that no longer exist in the K8s API.
        """
        ns_filter = _ns_selector(namespaces)
        query = (
            f'kube_pod_container_resource_requests'
            f'{{resource="cpu",container!=""{ns_filter}}}'
        )
        return self._fetch_range_max(query, lookback_days, step)

    def get_pod_memory_requests(
        self,
        lookback_days: int = 7,
        step: str = "1h",
        namespaces: Optional[List[str]] = None,
    ) -> UsageMap:
        """
        Return memory requests (bytes) per pod/container from kube-state-metrics.
        Covers historical pods that no longer exist in the K8s API.
        """
        ns_filter = _ns_selector(namespaces)
        query = (
            f'kube_pod_container_resource_requests'
            f'{{resource="memory",container!=""{ns_filter}}}'
        )
        return self._fetch_range_max(query, lookback_days, step)

    def get_pod_cpu_limits(
        self,
        lookback_days: int = 7,
        step: str = "1h",
        namespaces: Optional[List[str]] = None,
    ) -> UsageMap:
        """Return CPU limits (cores) per pod/container from kube-state-metrics."""
        ns_filter = _ns_selector(namespaces)
        query = (
            f'kube_pod_container_resource_limits'
            f'{{resource="cpu",container!=""{ns_filter}}}'
        )
        return self._fetch_range_max(query, lookback_days, step)

    def get_pod_memory_limits(
        self,
        lookback_days: int = 7,
        step: str = "1h",
        namespaces: Optional[List[str]] = None,
    ) -> UsageMap:
        """Return memory limits (bytes) per pod/container from kube-state-metrics."""
        ns_filter = _ns_selector(namespaces)
        query = (
            f'kube_pod_container_resource_limits'
            f'{{resource="memory",container!=""{ns_filter}}}'
        )
        return self._fetch_range_max(query, lookback_days, step)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _fetch_range_max(
        self,
        promql: str,
        lookback_days: int,
        step: str,
    ) -> UsageMap:
        """
        Query the range API over [now-lookback, now] and return the per-series
        maximum value, keyed by (namespace, pod, container).
        """
        end = int(time.time())
        start = end - lookback_days * 86400

        step_seconds = _parse_step_seconds(step)
        logger.debug(
            "PromQL: %s  start=%s end=%s step=%ds", promql, start, end, step_seconds
        )

        resp = self._session.get(
            f"{self.url}/api/v1/query_range",
            params={
                "query": promql,
                "start": start,
                "end": end,
                "step": step_seconds,
            },
            timeout=self._timeout,
        )
        if not resp.ok:
            raise RuntimeError(
                f"{resp.status_code} {resp.reason} for query_range\n"
                f"Query : {promql}\n"
                f"Detail: {resp.text[:500]}"
            )
        payload = resp.json()
        if payload.get("status") != "success":
            raise RuntimeError(
                f"Prometheus returned non-success: {payload.get('error', payload)}"
            )

        output: UsageMap = {}
        for item in payload["data"]["result"]:
            labels = item["metric"]
            ns = labels.get("namespace", "")
            pod = labels.get("pod", "")
            container = labels.get("container", "")
            if not pod or not container:
                continue
            values = item.get("values", [])
            if not values:
                continue
            max_val = max(
                float(v[1]) for v in values if v[1] not in ("NaN", "+Inf", "-Inf")
            )
            key = (ns, pod, container)
            output[key] = max(output.get(key, 0.0), max_val)

        logger.debug("Range query returned %d series", len(output))
        return output


def _parse_step_seconds(step: str) -> int:
    """Convert a duration string like '5m', '1h', '30s' to seconds."""
    step = step.strip()
    if step.endswith("s"):
        return int(step[:-1])
    if step.endswith("m"):
        return int(step[:-1]) * 60
    if step.endswith("h"):
        return int(step[:-1]) * 3600
    if step.endswith("d"):
        return int(step[:-1]) * 86400
    return int(step)  # assume already seconds


def _ns_selector(namespaces: Optional[List[str]]) -> str:
    """Build a PromQL label selector fragment for the given namespaces."""
    if not namespaces:
        return ""
    if len(namespaces) == 1:
        return f',namespace="{namespaces[0]}"'
    joined = "|".join(namespaces)
    return f',namespace=~"{joined}"'
