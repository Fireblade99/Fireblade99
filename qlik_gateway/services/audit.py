import logging
import threading
from urllib.parse import urlsplit

from sqlalchemy.orm import Session

from ..db import session_scope
from ..models import AuditLog
from .metrics import QLIK_CALLS, QLIK_LATENCY

log = logging.getLogger("qlik_gateway.audit")


def audit(db: Session, *, actor_type: str, action: str, actor: str = "", **fields) -> AuditLog:
    entry = AuditLog(actor_type=actor_type, actor=actor, action=action, **fields)
    db.add(entry)
    level = logging.DEBUG if actor_type == "qlik" and fields.get("outcome", "ok") == "ok" else logging.INFO
    log.log(level, "%s %s %s %s", actor_type, actor, action, fields.get("outcome", "ok"))
    return entry


def _endpoint(path: str) -> str:
    """/qrs/task/<guid>/start/synchronous -> /qrs/task/{id}/start/synchronous (low-cardinality label)."""
    parts = []
    for p in urlsplit(path).path.split("/"):
        parts.append("{id}" if len(p) == 36 and p.count("-") == 4 else p)
    return "/".join(parts)


def qlik_call_hook(method: str, path: str, status: int | None, latency_ms: float, error: str | None) -> None:
    """Records every call the gateway makes to Qlik (separate transaction, never raises)."""
    endpoint = _endpoint(path)
    outcome = "error" if error else "ok"
    QLIK_CALLS.labels(method, endpoint, outcome).inc()
    QLIK_LATENCY.labels(endpoint).observe(latency_ms / 1000)
    audit_queue.put(
        actor_type="qlik",
        actor="gateway",
        action=f"qlik {method} {endpoint}",
        method=method,
        path=path[:500],
        status_code=status,
        outcome=outcome,
        latency_ms=latency_ms,
        message=error or "",
    )


class AuditQueue:
    """Writes audit records from a background thread.

    Keeps the event loop and request transactions free of extra DB writes (and avoids
    lock waits on SQLite when a request calls Qlik while holding its own transaction).
    """

    def __init__(self):
        import queue

        self._q: queue.Queue[dict] = queue.Queue(maxsize=100_000)
        self._thread = None
        self._lock = threading.Lock()

    def _ensure_thread(self) -> None:
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name="audit-writer", daemon=True)
                self._thread.start()

    def put(self, **fields) -> None:
        self._ensure_thread()
        try:
            self._q.put_nowait(fields)
        except Exception:
            log.error("audit queue full, dropping record: %s", fields.get("action"))

    def _run(self) -> None:
        while True:
            batch = [self._q.get()]
            while len(batch) < 500:
                try:
                    batch.append(self._q.get_nowait())
                except Exception:
                    break
            try:
                with session_scope() as db:
                    for fields in batch:
                        audit(db, **fields)
            except Exception:
                log.exception("failed to write %d audit records", len(batch))
            finally:
                for _ in batch:
                    self._q.task_done()

    def flush(self) -> None:
        self._q.join()


audit_queue = AuditQueue()
