import logging
import re
import time
from typing import Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

# Keyed by (namespace, pod_name, container_name)
SeriesKey = Tuple[str, str, str]
UsageMap = Dict[SeriesKey, float]
# (unix_ts, value) points of one series
Points = List[Tuple[float, float]]
SeriesMap = Dict[SeriesKey, Points]

_BAD_VALUES = ("NaN", "+Inf", "-Inf")


class PrometheusClient:
    """
    Thin wrapper around the Prometheus / VictoriaMetrics HTTP API.

    Gentle on the backend:
    * the lookback window is split into chunks (``chunk_hours``, default 24h)
      that are queried one after another with a pause in between, so a single
      request never scans the whole week;
    * series are aggregated server-side (``max by (namespace, pod, container)``)
      so only one series per container comes back;
    * no subqueries (some backends reject them with 422).
    """

    def __init__(
        self,
        url: str,
        timeout: int = 120,
        chunk_hours: int = 24,
        pause_seconds: float = 1.0,
        use_proxy: bool = False,
        retries: int = 2,
        proxy_url: str = "",
    ) -> None:
        self.url = url.rstrip("/")
        self._timeout = timeout
        self._chunk = max(1, chunk_hours) * 3600
        self._pause = pause_seconds
        self._retries = retries
        self._session = requests.Session()
        # Corporate HTTP(S)_PROXY usually cannot reach in-cluster hosts
        self._session.trust_env = use_proxy
        if proxy_url:
            self._session.proxies = {"http": proxy_url, "https": proxy_url}
        self.requests_made = 0

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    def chunks(self, start: float, end: float) -> List[Tuple[float, float]]:
        """Split [start, end] into consecutive windows of ``chunk_hours``."""
        out = []
        t = start
        while t < end:
            out.append((t, min(t + self._chunk, end)))
            t += self._chunk
        return out

    def range_series(self, promql: str, start: float, end: float, step: int) -> SeriesMap:
        """
        Run ``query_range`` chunk by chunk and return every point per
        (namespace, pod, container), sorted by time.
        """
        output: SeriesMap = {}
        for c_start, c_end in self.chunks(start, end):
            data = self._get(
                "query_range",
                {"query": promql, "start": int(c_start), "end": int(c_end), "step": step},
            )
            for item in data:
                key = _key(item["metric"])
                if key is None:
                    continue
                pts = output.setdefault(key, [])
                for ts, val in item.get("values", []):
                    if val in _BAD_VALUES:
                        continue
                    pts.append((float(ts), float(val)))
        for key, pts in output.items():
            # Neighbouring chunks share their edge timestamp
            output[key] = sorted(dict(pts).items())
        logger.debug("Range query returned %d series", len(output))
        return output

    def instant_max(
        self, promql_tpl: str, start: float, end: float
    ) -> Dict[Tuple[Tuple[str, str], ...], Tuple[Dict[str, str], float]]:
        """
        Evaluate ``promql_tpl`` once per chunk at the chunk end, with ``$window``
        replaced by the chunk length, and keep the max per label set.

        Returns {sorted label items: (labels, max value)}.
        """
        output: Dict[Tuple[Tuple[str, str], ...], Tuple[Dict[str, str], float]] = {}
        for c_start, c_end in self.chunks(start, end):
            window = f"{max(60, int(c_end - c_start))}s"
            data = self._get("query", {"query": promql_tpl.replace("$window", window), "time": int(c_end)})
            for item in data:
                val = item.get("value", [None, "NaN"])[1]
                if val in _BAD_VALUES:
                    continue
                labels = item["metric"]
                k = tuple(sorted(labels.items()))
                prev = output.get(k)
                if prev is None or float(val) > prev[1]:
                    output[k] = (labels, float(val))
        return output

    def instant(self, promql: str) -> List[dict]:
        """Single instant query at "now" (used by --check)."""
        return self._get("query", {"query": promql})

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _get(self, endpoint: str, params: dict) -> list:
        if self.requests_made and self._pause > 0:
            time.sleep(self._pause)
        self.requests_made += 1
        logger.debug("%s %s", endpoint, params)

        last_exc: Optional[Exception] = None
        for attempt in range(self._retries + 1):
            if attempt:
                time.sleep(5 * attempt)
                logger.warning("Retrying %s (attempt %d): %s", endpoint, attempt + 1, last_exc)
            try:
                resp = self._session.post(
                    f"{self.url}/api/v1/{endpoint}", data=params, timeout=self._timeout
                )
            except requests.RequestException as exc:
                last_exc = exc
                continue
            if resp.status_code >= 500 or resp.status_code == 429:
                last_exc = RuntimeError(f"{resp.status_code} {resp.reason}: {resp.text[:300]}")
                continue
            if not resp.ok:
                raise RuntimeError(
                    f"{resp.status_code} {resp.reason} for {endpoint}\n"
                    f"Query : {params.get('query')}\n"
                    f"Detail: {resp.text[:500]}"
                )
            payload = resp.json()
            if payload.get("status") != "success":
                raise RuntimeError(
                    f"Prometheus returned non-success: {payload.get('error', payload)}"
                )
            return payload["data"]["result"]
        raise RuntimeError(f"{endpoint} failed after {self._retries + 1} attempts: {last_exc}")


def _key(labels: Dict[str, str]) -> Optional[SeriesKey]:
    pod = labels.get("pod", "")
    container = labels.get("container", "")
    if not pod or not container:
        return None
    return (labels.get("namespace", ""), pod, container)


def parse_step_seconds(step: str) -> int:
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


def ns_selector(namespaces: Optional[List[str]]) -> str:
    """Build a PromQL label selector fragment for the given namespaces."""
    if not namespaces:
        return ""
    if len(namespaces) == 1:
        return f',namespace="{namespaces[0]}"'
    joined = "|".join(namespaces)
    return f',namespace=~"{joined}"'


_USERINFO_RE = re.compile(r"^(\w+://[^:/@\s]+):[^/\s]*@")


def mask_url(url: str) -> str:
    """Hide the password of http://user:password@host URLs (logs, UI)."""
    return _USERINFO_RE.sub(r"\1:***@", url or "")

