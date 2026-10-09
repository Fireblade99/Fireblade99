"""
--check: show which metrics the Prometheus / VictoriaMetrics backend has for
the analysed namespaces, so queries can be debugged before a full run.
Every query is a cheap instant ``count`` over the last hour.
"""

import logging
from typing import List

from .clients.prom_client import PrometheusClient, mask_url, ns_selector
from .config import Config

logger = logging.getLogger(__name__)


def run_check(config: Config) -> int:
    p = config.prometheus
    prom = PrometheusClient(
        url=p.url, timeout=p.timeout, pause_seconds=0, use_proxy=p.use_proxy, retries=0,
        proxy_url=p.proxy_url,
    )
    ns = ns_selector(config.analysis.namespaces or None)
    labels = [v for v in (config.analysis.airflow_labels or {}).values() if v]

    checks = [
        ("cAdvisor CPU", f'container_cpu_usage_seconds_total{{container!=""{ns}}}'),
        ("cAdvisor memory", f'container_memory_working_set_bytes{{container!=""{ns}}}'),
        ("KSM requests", f'kube_pod_container_resource_requests{{container!=""{ns}}}'),
        ("KSM limits", f'kube_pod_container_resource_limits{{container!=""{ns}}}'),
        ("KSM pod labels", f'kube_pod_labels{{pod!=""{ns}}}'),
        ("OOM (terminated)", f'kube_pod_container_status_terminated_reason{{reason="OOMKilled"{ns}}}'),
        ("OOM (restarted)", f'kube_pod_container_status_last_terminated_reason{{reason="OOMKilled"{ns}}}'),
        ("OOM (cAdvisor)", f'container_oom_events_total{{container!=""{ns}}}'),
    ]
    print(f"Backend   : {mask_url(p.url)}")
    print(f"Namespaces: {', '.join(config.analysis.namespaces) or 'all'}")
    proxy = mask_url(p.proxy_url) if p.proxy_url else ("from environment" if p.use_proxy else "disabled")
    print(f"Proxy     : {proxy}\n")

    ok = True
    for title, selector in checks:
        try:
            res = prom.instant(f"count(last_over_time({selector}[1h]))")
            count = int(float(res[0]["value"][1])) if res else 0
            print(f"  {'✓' if count else '✗'} {title:<16} {count:>6} series in the last hour")
            if not count and title.startswith("cAdvisor"):
                ok = False
        except Exception as exc:
            print(f"  ✗ {title:<16} ERROR: {exc}")
            if title == checks[0][0]:
                print("\nBackend is not reachable: check the URL, VPN, and whether a proxy is needed"
                      " (--use-proxy) or must be bypassed (default).")
                return 2
            ok = False

    if labels:
        _check_labels(prom, ns, labels)
    return 0 if ok else 2


def _check_labels(prom: PrometheusClient, ns: str, labels: List[str]) -> None:
    print("\n  Airflow labels in kube_pod_labels (pods that have the label):")
    missing = False
    for lbl in labels:
        try:
            res = prom.instant(f'count(last_over_time(kube_pod_labels{{{lbl}!=""{ns}}}[1h]))')
            count = int(float(res[0]["value"][1])) if res else 0
        except Exception as exc:
            print(f"    ✗ {lbl:<20} ERROR: {exc}")
            continue
        missing = missing or not count
        print(f"    {'✓' if count else '✗'} {lbl:<20} {count:>6} pods")
    if not missing:
        return
    try:
        res = prom.instant(f'topk(1, last_over_time(kube_pod_labels{{pod!=""{ns}}}[1h]))')
    except Exception:
        res = []
    others = sorted(k for k in (res[0]["metric"] if res else {}) if k.startswith("label_"))
    print(f"    label_* on a sample pod: {', '.join(others) or 'none'}")
    print("    kube-state-metrics exports pod labels only if allowed by --metric-labels-allowlist;")
    print("    without them the DAG/Task/Run columns stay empty (workloads are still grouped by pod name).")
