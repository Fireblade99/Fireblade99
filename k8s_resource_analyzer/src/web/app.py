"""
Web UI: shows the last report per cluster and refreshes it on a schedule.

The page never queries Prometheus itself. A background thread runs the
analysis of every cluster every ``web.refresh_interval_hours`` (and on the
"Refresh" button, at most once per ``web.min_manual_refresh_minutes``).
Only one analysis runs at a time, whatever the number of clusters.

Each cluster keeps its report in ``<data_dir>/<cluster id>/`` (report.json +
report.xlsx), so it survives restarts. The cluster list (name, URL,
namespaces) comes from config.yaml and can be edited in the UI; the edited
list is stored in ``<data_dir>/clusters.json`` and wins over config.yaml.
"""

import copy
import json
import logging
import os
import re
import secrets
import shutil
import threading
import time
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

from fastapi import Body, Depends, FastAPI, HTTPException, Query, Response, status
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from ..clients.prom_client import mask_url
from ..config import ClusterConfig, Config
from ..pipeline import run_analysis
from ..reporters import excel_reporter
from .snapshot import build_snapshot

logger = logging.getLogger(__name__)

_STATIC = os.path.join(os.path.dirname(__file__), "static")
_NS_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")


def cluster_id(name: str) -> str:
    """Directory-safe id derived from the cluster name."""
    return re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-") or "default"


class Refresher:
    """Runs the analysis of one cluster in a background thread."""

    def __init__(self, config: Config, data_dir: str, run_lock: Optional[threading.Lock] = None) -> None:
        self.cfg = config
        self.data_dir = data_dir
        os.makedirs(self.data_dir, exist_ok=True)
        self.json_path = os.path.join(self.data_dir, "report.json")
        self.xlsx_path = os.path.join(self.data_dir, "report.xlsx")
        self._lock = threading.Lock()
        # Shared between clusters: never two analyses at once
        self._run_lock = run_lock or threading.Lock()
        self.running = False
        self.queued = False
        self.last_started: Optional[float] = None
        self.last_finished: Optional[float] = None
        self.last_error: Optional[str] = None
        snap = self.load()
        if snap:
            self.last_started = snap.get("started_at")
            self.last_finished = snap.get("generated_at")

    # ------------------------------------------------------------------

    def load(self) -> Optional[Dict[str, Any]]:
        try:
            with open(self.json_path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    @property
    def next_run(self) -> Optional[float]:
        if self.running:
            return None
        base = self.last_finished or self.last_started
        if base is None:
            return time.time()
        return base + self.cfg.web.refresh_interval_hours * 3600

    def state(self) -> Dict[str, Any]:
        return {
            "running": self.running,
            "queued": self.queued,
            "last_started": self.last_started,
            "last_finished": self.last_finished,
            "last_error": self.last_error,
            "next_run": self.next_run,
            "min_manual_refresh_minutes": self.cfg.web.min_manual_refresh_minutes,
        }

    def trigger(self, manual: bool = False) -> Dict[str, Any]:
        with self._lock:
            if self.running:
                return {"started": False, "reason": "already running"}
            if manual and self.last_started:
                wait = self.last_started + self.cfg.web.min_manual_refresh_minutes * 60 - time.time()
                if wait > 0:
                    return {"started": False, "reason": f"too soon, try again in {int(wait // 60) + 1} min"}
            self.running = True
            self.last_started = time.time()
        threading.Thread(target=self._run, name="analysis", daemon=True).start()
        return {"started": True}

    def run_sync(self) -> None:
        """Run once in the current thread (tests, `web.py --once`)."""
        with self._lock:
            self.running = True
            self.last_started = time.time()
        self._run()

    def _run(self) -> None:
        self.queued = True
        with self._run_lock:
            self.queued = False
            self.last_started = started = time.time()
            try:
                logger.info("Analysis started: %s %s", mask_url(self.cfg.prometheus.url), self.cfg.analysis.namespaces)
                recs = run_analysis(self.cfg)
                snap = build_snapshot(recs, self.cfg, started_at=started)
                tmp_xlsx = self.xlsx_path + ".tmp.xlsx"
                excel_reporter.generate(
                    recs,
                    output_path=tmp_xlsx,
                    show_only_waste=self.cfg.output.show_only_waste,
                    lookback_days=self.cfg.prometheus.lookback_days,
                )
                os.replace(tmp_xlsx, self.xlsx_path)
                tmp_json = self.json_path + ".tmp"
                with open(tmp_json, "w", encoding="utf-8") as f:
                    json.dump(snap, f, ensure_ascii=False, separators=(",", ":"))
                os.replace(tmp_json, self.json_path)
                self.last_error = None
                logger.info("Analysis finished: %d workloads, %d runs",
                            snap["summary"]["workloads"], snap["summary"]["runs"])
            except Exception as exc:  # keep the last good report, show the error in the UI
                logger.exception("Analysis failed")
                self.last_error = f"{type(exc).__name__}: {exc}"
            finally:
                self.last_finished = time.time()
                self.running = False


class ClusterManager:
    """Cluster list + one Refresher per cluster + the scheduler thread."""

    def __init__(self, config: Config) -> None:
        self.base = config
        self.data_dir = config.web.data_dir
        os.makedirs(self.data_dir, exist_ok=True)
        self.store_path = os.path.join(self.data_dir, "clusters.json")
        self._run_lock = threading.Lock()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.clusters: List[ClusterConfig] = []
        self.refreshers: Dict[str, Refresher] = {}
        self._apply(self._load_clusters())

    # ── Cluster list ───────────────────────────────────────────────────

    def _load_clusters(self) -> List[ClusterConfig]:
        try:
            with open(self.store_path, encoding="utf-8") as f:
                return [ClusterConfig(**c) for c in json.load(f)]
        except (OSError, ValueError, TypeError):
            pass
        if self.base.clusters:
            return list(self.base.clusters)
        return [ClusterConfig(
            name="default",
            url=self.base.prometheus.url,
            namespaces=list(self.base.analysis.namespaces or []),
        )]

    def _cluster_config(self, c: ClusterConfig) -> Config:
        cfg = copy.deepcopy(self.base)
        cfg.prometheus.url = c.url
        cfg.prometheus.proxy_url = c.proxy
        cfg.analysis.namespaces = list(c.namespaces)
        cfg.kubernetes.enabled = False  # the web UI works from metrics only
        return cfg

    def _apply(self, clusters: List[ClusterConfig]) -> None:
        new: Dict[str, Refresher] = {}
        for c in clusters:
            cid = cluster_id(c.name)
            old = self.refreshers.get(cid)
            cfg = self._cluster_config(c)
            if old is not None and old.cfg.prometheus.url == c.url and \
                    old.cfg.analysis.namespaces == list(c.namespaces):
                old.cfg.prometheus.proxy_url = c.proxy  # same data, only the route changed
                new[cid] = old
                continue
            data_dir = os.path.join(self.data_dir, cid)
            if old is not None and not old.running:
                # URL or namespaces changed: the old report no longer matches
                shutil.rmtree(data_dir, ignore_errors=True)
            new[cid] = Refresher(cfg, data_dir, self._run_lock)
        self.clusters = clusters
        self.refreshers = new

    def update(self, items: List[Dict[str, Any]]) -> List[ClusterConfig]:
        clusters = validate_clusters(items)
        # The UI gets URLs with the password masked; put the stored one back
        current = {cluster_id(c.name): c for c in self.clusters}
        for c in clusters:
            old = current.get(cluster_id(c.name))
            if old is not None:
                if c.url == mask_url(old.url):
                    c.url = old.url
                if c.proxy == mask_url(old.proxy):
                    c.proxy = old.proxy
        with self._lock:
            self._apply(clusters)
            tmp = self.store_path + ".tmp"
            # May contain passwords from URLs: readable by the service user only
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump([c.__dict__ for c in clusters], f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.store_path)
        return clusters

    def get(self, cid: Optional[str]) -> Refresher:
        if not cid:
            cid = cluster_id(self.clusters[0].name)
        r = self.refreshers.get(cid)
        if r is None:
            raise HTTPException(status_code=404, detail=f"Unknown cluster {cid!r}")
        return r

    def describe(self) -> List[Dict[str, Any]]:
        out = []
        for c in self.clusters:
            cid = cluster_id(c.name)
            out.append({"id": cid, "name": c.name, "url": mask_url(c.url), "namespaces": c.namespaces,
                        "proxy": mask_url(c.proxy), "status": self.refreshers[cid].state()})
        return out

    # ── Scheduler ──────────────────────────────────────────────────────

    def loop(self) -> None:
        while not self._stop.is_set():
            for r in list(self.refreshers.values()):
                nxt = r.next_run
                if nxt is not None and time.time() >= nxt and not r.queued:
                    r.trigger()
            self._stop.wait(30)

    def start(self) -> None:
        threading.Thread(target=self.loop, name="scheduler", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()


def validate_clusters(items: List[Dict[str, Any]]) -> List[ClusterConfig]:
    if not isinstance(items, list) or not items:
        raise HTTPException(status_code=422, detail="At least one cluster is required")
    out: List[ClusterConfig] = []
    seen = set()
    for i, it in enumerate(items, start=1):
        if not isinstance(it, dict):
            raise HTTPException(status_code=422, detail=f"Cluster #{i}: bad format")
        name = str(it.get("name", "")).strip()
        url = str(it.get("url", "")).strip().rstrip("/")
        proxy = str(it.get("proxy", "") or "").strip()
        ns = it.get("namespaces", [])
        if isinstance(ns, str):
            ns = ns.split(",")
        ns = [str(n).strip() for n in ns if str(n).strip()]
        if not name:
            raise HTTPException(status_code=422, detail=f"Cluster #{i}: name is required")
        cid = cluster_id(name)
        if cid in seen:
            raise HTTPException(status_code=422, detail=f"Cluster #{i}: duplicate name {name!r}")
        seen.add(cid)
        if not re.match(r"^https?://[^\s/]+", url):
            raise HTTPException(status_code=422, detail=f"Cluster {name!r}: URL must start with http:// or https://")
        if proxy and not re.match(r"^https?://[^\s/]+", proxy):
            raise HTTPException(status_code=422, detail=f"Cluster {name!r}: proxy must look like http://host:port")
        bad = [n for n in ns if not _NS_RE.match(n)]
        if bad:
            raise HTTPException(status_code=422, detail=f"Cluster {name!r}: bad namespace {bad[0]!r}")
        out.append(ClusterConfig(name=name, url=url, namespaces=ns, proxy=proxy))
    return out


def create_app(config: Config, start_scheduler: bool = True) -> FastAPI:
    manager = ClusterManager(config)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if start_scheduler:
            manager.start()
        yield
        manager.stop()

    app = FastAPI(title="k8s resource analyzer", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.manager = manager

    user = os.environ.get("WEB_USER") or config.web.auth_user
    password = os.environ.get("WEB_PASSWORD") or config.web.auth_password
    basic = HTTPBasic(auto_error=bool(user))

    def auth(creds: Optional[HTTPBasicCredentials] = Depends(basic)) -> None:
        if not user:
            return
        ok = creds is not None and secrets.compare_digest(creds.username, user) and \
            secrets.compare_digest(creds.password, password)
        if not ok:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                headers={"WWW-Authenticate": "Basic"},
            )

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> Dict[str, str]:
        return {"status": "ok"}

    @app.get("/", response_class=HTMLResponse, dependencies=[Depends(auth)])
    def index() -> FileResponse:
        return FileResponse(os.path.join(_STATIC, "index.html"), media_type="text/html")

    @app.get("/api/clusters", dependencies=[Depends(auth)])
    def api_clusters() -> Dict[str, Any]:
        return {"clusters": manager.describe(), "editable": config.web.allow_edit_clusters}

    @app.put("/api/clusters", dependencies=[Depends(auth)])
    def api_clusters_update(items: List[Dict[str, Any]] = Body(...)) -> Dict[str, Any]:
        if not config.web.allow_edit_clusters:
            raise HTTPException(status_code=403, detail="Editing clusters is disabled (web.allow_edit_clusters)")
        manager.update(items)
        return api_clusters()

    @app.get("/api/status", dependencies=[Depends(auth)])
    def api_status(cluster: Optional[str] = Query(None)) -> Dict[str, Any]:
        return manager.get(cluster).state()

    @app.get("/api/report", dependencies=[Depends(auth)])
    def api_report(cluster: Optional[str] = Query(None)) -> Response:
        r = manager.get(cluster)
        try:
            # The file is already JSON (can be a few MB): wrap it without re-parsing
            with open(r.json_path, encoding="utf-8") as f:
                report = f.read()
        except OSError:
            report = "null"
        body = '{"report":' + report + ',"status":' + json.dumps(r.state()) + "}"
        return Response(content=body, media_type="application/json")

    @app.post("/api/refresh", dependencies=[Depends(auth)])
    def api_refresh(cluster: Optional[str] = Query(None)) -> Dict[str, Any]:
        return manager.get(cluster).trigger(manual=True)

    @app.get("/report.xlsx", dependencies=[Depends(auth)])
    def report_xlsx(cluster: Optional[str] = Query(None)) -> FileResponse:
        r = manager.get(cluster)
        if not os.path.exists(r.xlsx_path):
            raise HTTPException(status_code=404, detail="No report yet")
        stamp = time.strftime("%Y%m%d_%H%M", time.localtime(os.path.getmtime(r.xlsx_path)))
        cid = cluster or cluster_id(manager.clusters[0].name)
        return FileResponse(
            r.xlsx_path,
            filename=f"k8s_waste_report_{cid}_{stamp}.xlsx",
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    return app
