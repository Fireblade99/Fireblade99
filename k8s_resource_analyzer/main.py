#!/usr/bin/env python3
"""
k8s-resource-analyzer
=====================
Finds over-provisioned (and under-provisioned) Kubernetes workloads by
comparing resource requests/limits against the **peak** observed CPU and
memory usage stored in Prometheus.

Key features
------------
* Groups pods from the same logical workload regardless of random suffixes
  (Deployments, Jobs, Airflow tasks, StatefulSets).
* Uses MAX over a configurable lookback window – not an average – so a
  workload that spiked once still gets caught.
* Covers pods already deleted but still present in Prometheus history.
* Per-run view: every pod (Airflow task try, Job run) with its request vs
  actual average/peak usage and reserved-but-idle resource-hours.
* Gentle on the backend: the lookback window is queried in chunks
  (default 24h) one after another, aggregated server-side.
* Outputs a rich colour table, JSON or an Excel report.

Usage
-----
    python main.py --prometheus-url http://prometheus:9090
    python main.py --config config.yaml --namespace airflow --output json
    python main.py --all --sort-by namespace
    python main.py -c config.yaml --check          # what metrics/labels exist
    python main.py -c config.yaml --no-k8s -o excel
"""

import argparse
import datetime
import logging
import os
import sys

from src.config import Config
from src.core.analyzer import ResourceAnalyzer
from src.pipeline import build_recommender
from src.reporters.console import print_recommendations
from src.reporters.json_reporter import to_json
from src.reporters import excel_reporter, email_sender
from src.utils import fmt_bytes

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("k8s-analyzer")


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--config", "-c",
        default="config.yaml",
        metavar="FILE",
        help="Path to config YAML (default: config.yaml)",
    )
    p.add_argument(
        "--prometheus-url",
        metavar="URL",
        help="Prometheus base URL, e.g. http://prometheus:9090",
    )
    p.add_argument(
        "--namespace", "-n",
        action="append",
        dest="namespaces",
        metavar="NS",
        help="Namespace to analyse (repeat for multiple; default: all)",
    )
    p.add_argument(
        "--lookback-days",
        type=int,
        metavar="N",
        help="Days to look back in Prometheus history (default: 7)",
    )
    p.add_argument(
        "--waste-threshold",
        type=float,
        metavar="RATIO",
        help="Fraction of request that must be wasted to flag (e.g. 0.5 = 50%%)",
    )
    p.add_argument(
        "--output", "-o",
        choices=["table", "json", "excel"],
        help="Output format (default: table)",
    )
    p.add_argument(
        "--excel-path",
        metavar="FILE",
        default=None,
        help="Path for the Excel report (default: auto-named in current dir)",
    )
    p.add_argument(
        "--send-email",
        action="store_true",
        help="Send Excel report via email (uses [email] section in config)",
    )
    p.add_argument(
        "--all",
        action="store_true",
        help="Show all workloads, not only wasteful/risky ones",
    )
    p.add_argument(
        "--sort-by",
        choices=["memory_waste", "cpu_waste", "namespace"],
        help="Column to sort by (default: memory_waste)",
    )
    p.add_argument(
        "--in-cluster",
        action="store_true",
        help="Load Kubernetes config from the service-account (inside a pod)",
    )
    p.add_argument(
        "--kubeconfig",
        metavar="FILE",
        help="Path to kubeconfig file (overrides KUBECONFIG env var)",
    )
    p.add_argument(
        "--no-k8s",
        action="store_true",
        help="Do not call the Kubernetes API; take requests/limits from kube-state-metrics only",
    )
    p.add_argument(
        "--step",
        metavar="DUR",
        help="Resolution of usage samples, e.g. 1m, 5m (default: 5m)",
    )
    p.add_argument(
        "--chunk-hours",
        type=int,
        metavar="H",
        help="Query the lookback window in chunks of H hours (default: 24)",
    )
    p.add_argument(
        "--pause",
        type=float,
        metavar="SEC",
        help="Pause between backend requests in seconds (default: 1)",
    )
    p.add_argument(
        "--use-proxy",
        action="store_true",
        help="Honour HTTP(S)_PROXY env vars for Prometheus requests (ignored by default)",
    )
    p.add_argument(
        "--check",
        action="store_true",
        help="Only check which metrics and Airflow labels the backend has, then exit",
    )
    p.add_argument(
        "--debug",
        action="store_true",
        help="Enable verbose debug logging",
    )
    return p


# ──────────────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────────────

def main() -> int:
    args = _build_parser().parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    # Load base config
    if os.path.exists(args.config):
        logger.info("Loading config from %s", args.config)
        config = Config.from_file(args.config)
    else:
        logger.info("Config file not found – using built-in defaults")
        config = Config.default()

    # Apply CLI overrides
    if args.prometheus_url:
        config.prometheus.url = args.prometheus_url
    if args.namespaces:
        config.analysis.namespaces = args.namespaces
    if args.lookback_days is not None:
        config.prometheus.lookback_days = args.lookback_days
    if args.waste_threshold is not None:
        config.analysis.waste_threshold_ratio = args.waste_threshold
    if args.output:
        config.output.format = args.output
    if args.all:
        config.output.show_only_waste = False
    if args.sort_by:
        config.output.sort_by = args.sort_by
    if args.send_email:
        config.email.enabled = True
    if args.in_cluster:
        config.kubernetes.in_cluster = True
    if args.kubeconfig:
        config.kubernetes.kubeconfig = args.kubeconfig
    if args.no_k8s:
        config.kubernetes.enabled = False
    if args.step:
        config.prometheus.step = args.step
    if args.chunk_hours is not None:
        config.prometheus.chunk_hours = args.chunk_hours
    if args.pause is not None:
        config.prometheus.pause_seconds = args.pause
    if args.use_proxy:
        config.prometheus.use_proxy = True

    if args.check:
        from src.check import run_check

        return run_check(config)

    # ── Analysis ──────────────────────────────────────────────────────
    try:
        analyzer = ResourceAnalyzer(config)
        groups = analyzer.analyze()
    except RuntimeError as exc:
        logger.error("%s", exc)
        return 2

    logger.info("Analysis complete: %d workload groups", len(groups))

    # ── Recommendations ───────────────────────────────────────────────
    recommendations = build_recommender(config).process_all(groups)

    # ── Output ────────────────────────────────────────────────────────
    show_only_waste = config.output.show_only_waste
    sort_by = config.output.sort_by

    if config.output.format == "json":
        print(to_json(recommendations, show_only_waste=show_only_waste))
    elif config.output.format == "excel" or config.email.enabled:
        _output_excel(args, config, recommendations, show_only_waste)
    else:
        print_recommendations(
            recommendations,
            show_only_waste=show_only_waste,
            sort_by=sort_by,
        )

    # Non-zero exit code when wasteful workloads were found (useful in CI)
    has_issues = any(r.is_wasteful or r.is_risky for r in recommendations)
    return 1 if has_issues else 0


def _output_excel(args, config, recommendations, show_only_waste: bool) -> None:
    """Generate Excel file and optionally send by email."""
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M")
    default_path = f"k8s_waste_report_{ts}.xlsx"
    xlsx_path = getattr(args, "excel_path", None) or default_path

    excel_reporter.generate(
        recommendations,
        output_path=xlsx_path,
        show_only_waste=show_only_waste,
        lookback_days=config.prometheus.lookback_days,
    )
    logger.info("Excel report saved: %s", xlsx_path)

    if config.email.enabled:
        wasteful = [r for r in recommendations if r.is_wasteful]
        total_mem = sum(
            (r.memory_waste_bytes or 0) for r in wasteful if (r.memory_waste_bytes or 0) > 0
        )
        # SMTP password: prefer env var over config to avoid storing secrets
        password = os.environ.get("SMTP_PASSWORD") or config.email.smtp_password
        try:
            email_sender.send_report(
                xlsx_path=xlsx_path,
                recipients=config.email.recipients,
                smtp_host=config.email.smtp_host,
                smtp_port=config.email.smtp_port,
                smtp_user=config.email.smtp_user,
                smtp_password=password,
                use_tls=config.email.use_tls,
                sender=config.email.sender,
                subject=config.email.subject,
                lookback_days=config.prometheus.lookback_days,
                total_mem_waste=fmt_bytes(total_mem),
                wasteful_count=len(wasteful),
            )
        except Exception as exc:
            logger.error("Failed to send email: %s", exc)


if __name__ == "__main__":
    sys.exit(main())
