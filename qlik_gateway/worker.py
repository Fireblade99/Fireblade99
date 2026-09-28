"""The single coordinator: the only component that starts reloads in Qlik and polls their state.

Run exactly one active instance (`qlik-gateway worker`); a DB lease makes extra replicas idle
standbys, so it is safe to run two for HA.
"""

import logging
import os
import signal
import socket
import threading
import time
from datetime import timedelta

from sqlalchemy import delete

from .config import Settings, get_settings
from .db import session_scope
from .models import KV, AuditLog, NodeHealth, utcnow
from .qlik import QlikError, make_backend
from .services import executions as svc
from .services.audit import audit, qlik_call_hook
from .services.kv import dispatch_paused
from .services.metrics import DISPATCH_PAUSED

log = logging.getLogger("qlik_gateway.worker")

LEASE_KEY = "worker_lease"


class Coordinator:
    def __init__(self, settings: Settings | None = None, backend=None):
        self.s = settings or get_settings()
        self.backend = backend or make_backend(self.s, qlik_call_hook)
        self.instance = f"{socket.gethostname()}:{os.getpid()}:{id(self)}"
        self._stop = threading.Event()
        self._last = {"poll": 0.0, "catalog": 0.0, "health": 0.0, "cleanup": 0.0}

    # --- leadership ---------------------------------------------------------------
    def acquire_lease(self) -> bool:
        now = utcnow()
        with session_scope() as db:
            row = db.get(KV, LEASE_KEY, with_for_update=True)
            if row is None:
                db.add(
                    KV(key=LEASE_KEY, value={"owner": self.instance, "until": _iso(now, self.s.worker_lease_seconds)})
                )
                return True
            owner, until = row.value.get("owner"), row.value.get("until")
            if owner == self.instance or until is None or until < now.isoformat():
                row.value = {"owner": self.instance, "until": _iso(now, self.s.worker_lease_seconds)}
                return True
            return False

    # --- one iteration --------------------------------------------------------------
    def tick(self, force: bool = False) -> dict:
        """Run whatever is due. `force` runs every step (used by tests and the "sync now" button)."""
        now = time.monotonic()
        stats: dict = {}

        if force or now - self._last["catalog"] >= self.s.catalog_sync_interval_seconds:
            self._last["catalog"] = now
            stats["catalog"] = self._safe("catalog sync", lambda db: svc.sync_catalog(db, self.backend))

        stats["dispatched"] = self._safe("dispatch", lambda db: svc.dispatch(db, self.backend, self.s))

        if force or now - self._last["poll"] >= self.s.poll_interval_seconds:
            self._last["poll"] = now
            stats["polled"] = self._safe("poll", lambda db: svc.poll(db, self.backend, self.s))

        urls = [u.strip() for u in self.s.node_health_urls.split(",") if u.strip()]
        if urls and (force or now - self._last["health"] >= self.s.node_health_interval_seconds):
            self._last["health"] = now
            self._safe("node health", lambda db: svc.collect_node_health(db, self.backend, urls))

        if now - self._last["cleanup"] >= 3600:
            self._last["cleanup"] = now
            self._safe("cleanup", self._cleanup)

        with session_scope() as db:
            svc.refresh_active_gauge(db)
            DISPATCH_PAUSED.set(1 if dispatch_paused(db) else 0)
        return stats

    def _safe(self, name: str, fn):
        try:
            with session_scope() as db:
                return fn(db)
        except QlikError as e:
            log.warning("%s: Qlik error: %s", name, e)
            with session_scope() as db:
                audit(db, actor_type="system", actor="worker", action=f"worker.{name}", outcome="error", message=str(e))
        except Exception as e:
            log.exception("%s failed", name)
            with session_scope() as db:
                audit(
                    db, actor_type="system", actor="worker", action=f"worker.{name}", outcome="error", message=repr(e)
                )
        return None

    def _cleanup(self, db) -> None:
        cutoff = utcnow() - timedelta(days=self.s.audit_retention_days)
        db.execute(delete(AuditLog).where(AuditLog.ts < cutoff))
        db.execute(delete(NodeHealth).where(NodeHealth.ts < utcnow() - timedelta(days=14)))

    # --- loop -----------------------------------------------------------------------
    def run_forever(self) -> None:
        log.info("coordinator %s started (qlik_mode=%s)", self.instance, self.s.qlik_mode)
        leader = False
        while not self._stop.is_set():
            try:
                is_leader = self.acquire_lease()
            except Exception:
                log.exception("lease check failed")
                is_leader = False
            if is_leader != leader:
                log.info("coordinator %s is now %s", self.instance, "ACTIVE" if is_leader else "standby")
                leader = is_leader
            if leader:
                self.tick()
            self._stop.wait(self.s.dispatch_interval_seconds)

    def stop(self) -> None:
        self._stop.set()


def _iso(now, seconds: int) -> str:
    return (now + timedelta(seconds=seconds)).isoformat()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    coord = Coordinator()
    signal.signal(signal.SIGTERM, lambda *_: coord.stop())
    signal.signal(signal.SIGINT, lambda *_: coord.stop())
    coord.run_forever()


if __name__ == "__main__":
    main()
