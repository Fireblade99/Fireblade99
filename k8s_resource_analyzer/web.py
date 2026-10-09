#!/usr/bin/env python3
"""
Web UI for k8s-resource-analyzer.

    python web.py -c config.yaml                 # http://localhost:8080
    python web.py -c config.yaml --once          # run the analysis once and exit

The analysis runs in the background every ``web.refresh_interval_hours``;
the page shows the last stored report (``web.data_dir``).
"""

import argparse
import logging
import os
import sys

from src.config import Config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("k8s-analyzer-web")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", "-c", default=os.environ.get("ANALYZER_CONFIG", "config.yaml"), metavar="FILE",
                   help="Path to config YAML (default: $ANALYZER_CONFIG or config.yaml)")
    p.add_argument("--host", default=os.environ.get("WEB_HOST", "0.0.0.0"))
    p.add_argument("--port", type=int, default=int(os.environ.get("WEB_PORT", "8080")))
    p.add_argument("--data-dir", metavar="DIR", help="Where to keep the last report (overrides web.data_dir)")
    p.add_argument("--once", action="store_true", help="Run the analysis once, store the report and exit")
    args = p.parse_args()

    if os.path.exists(args.config):
        logger.info("Loading config from %s", args.config)
        config = Config.from_file(args.config)
    else:
        logger.warning("Config file %s not found – using built-in defaults", args.config)
        config = Config.default()
    if args.data_dir:
        config.web.data_dir = args.data_dir
    # Env overrides are handy in docker-compose (single-cluster setups without `clusters:`)
    if os.environ.get("PROMETHEUS_URL"):
        config.prometheus.url = os.environ["PROMETHEUS_URL"]
    if os.environ.get("NAMESPACES"):
        config.analysis.namespaces = [n.strip() for n in os.environ["NAMESPACES"].split(",") if n.strip()]

    from src.web.app import ClusterManager, create_app

    if args.once:
        failed = 0
        for cid, r in ClusterManager(config).refreshers.items():
            r.run_sync()
            if r.last_error:
                failed += 1
                logger.error("%s: %s", cid, r.last_error)
        logger.info("Reports stored in %s", config.web.data_dir)
        return 2 if failed else 0

    import uvicorn

    uvicorn.run(create_app(config), host=args.host, port=args.port, log_level="info", access_log=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
