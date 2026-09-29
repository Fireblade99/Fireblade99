"""Coordinator logic: accept requests, dispatch them to Qlik, poll Qlik in bulk, keep history."""

import logging
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..models import Client, ExecStatus, Execution, NodeHealth, QlikTask, utcnow
from ..qlik import QlikBackend, QlikError, map_qlik_status
from .audit import audit
from .errors import ServiceError
from .kv import dispatch_paused
from .metrics import EXECUTIONS_ACTIVE, EXECUTIONS_FINISHED

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------------------
# permissions
# --------------------------------------------------------------------------------------
def client_can_task(client: Client, task_id: str, task: QlikTask | None = None) -> bool:
    """Access is granted in the gateway UI (task list or "*") or in QMC via the client property."""
    allowed = client.allowed_tasks or []
    if "*" in allowed or task_id in allowed:
        return True
    return task is not None and client.name in granted_in_qlik(task)


def granted_in_qlik(task: QlikTask) -> list[str]:
    """Client names listed in the task's app/task custom property (e.g. GatewayClient)."""
    prop = get_settings().qlik_client_custom_property
    return list((task.custom_properties or {}).get(prop, [])) if prop else []


def require_action(client: Client, action: str) -> None:
    if action not in (client.allowed_actions or []):
        raise ServiceError(403, "action_forbidden", f"Client '{client.name}' is not allowed to '{action}'")


def require_task(db: Session, client: Client, task_id: str) -> QlikTask:
    task = db.get(QlikTask, task_id)
    if not client_can_task(client, task_id, task):
        raise ServiceError(403, "task_forbidden", f"Client '{client.name}' has no access to task {task_id}")
    if task is None or not task.present_in_qlik:
        raise ServiceError(404, "task_not_found", f"Task {task_id} is not in the gateway catalog")
    return task


def visible_tasks(db: Session, client: Client) -> list[QlikTask]:
    q = select(QlikTask).where(QlikTask.present_in_qlik.is_(True)).order_by(QlikTask.name)
    return [t for t in db.scalars(q) if client_can_task(client, t.id, t)]


# --------------------------------------------------------------------------------------
# submit
# --------------------------------------------------------------------------------------
def active_execution_for_task(db: Session, task_id: str) -> Execution | None:
    return db.scalars(
        select(Execution)
        .where(Execution.task_id == task_id, Execution.status.in_(ExecStatus.ACTIVE))
        .order_by(Execution.id)
        .limit(1)
    ).first()


def submit(
    db: Session,
    client: Client,
    task_id: str,
    *,
    initiator: dict,
    caller_ip: str | None,
    priority: int | None = None,
    dedupe: bool = True,
) -> tuple[Execution, bool]:
    """Accept a start request. Returns (execution, deduplicated)."""
    require_action(client, "start")
    task = require_task(db, client, task_id)

    if task.blocked:
        raise ServiceError(423, "task_blocked", f"Task is blocked by administrator: {task.blocked_reason or '-'}")
    if not task.enabled_in_qlik:
        raise ServiceError(409, "task_disabled", "Task is disabled in Qlik")

    # The same reload running twice only wastes engine resources: attach to the existing one.
    existing = active_execution_for_task(db, task_id)
    if existing is not None and dedupe:
        existing.dedup_hits += 1
        return existing, True

    now = utcnow()
    starts_last_hour = db.scalar(
        select(func.count(Execution.id)).where(
            Execution.client_id == client.id,
            Execution.created_at >= now - timedelta(hours=1),
            Execution.status != ExecStatus.CANCELLED,
        )
    )
    if client.starts_per_hour and starts_last_hour >= client.starts_per_hour:
        raise ServiceError(429, "starts_limit", f"Limit of {client.starts_per_hour} starts per hour reached")

    if task.min_interval_seconds:
        last = db.scalar(select(func.max(Execution.created_at)).where(Execution.task_id == task_id))
        if last and (now - last).total_seconds() < task.min_interval_seconds:
            raise ServiceError(
                429, "task_min_interval", f"Task may be started at most once per {task.min_interval_seconds}s"
            )

    execution = Execution(
        client_id=client.id,
        task_id=task_id,
        task_name=task.name,
        status=ExecStatus.QUEUED,
        priority=client.priority if priority is None else max(priority, client.priority),
        initiator=initiator,
        caller_ip=caller_ip,
        created_at=now,
    )
    db.add(execution)
    db.flush()
    return execution, False


def cancel(db: Session, backend: QlikBackend, execution: Execution, *, actor_type: str, actor: str) -> Execution:
    if execution.status == ExecStatus.QUEUED:
        execution.status = ExecStatus.CANCELLED
        execution.finished_at = utcnow()
        execution.error = f"Cancelled by {actor_type} {actor}"
    elif execution.status in ExecStatus.IN_QLIK:
        backend.stop_task(execution.task_id)
        execution.cancel_requested = True
    else:
        raise ServiceError(409, "not_active", f"Execution is already {execution.status}")
    audit(
        db,
        actor_type=actor_type,
        actor=actor,
        action="execution.cancel",
        task_id=execution.task_id,
        execution_id=execution.id,
        message=f"status={execution.status}",
    )
    return execution


# --------------------------------------------------------------------------------------
# dispatch (worker)
# --------------------------------------------------------------------------------------
def dispatch(db: Session, backend: QlikBackend, settings: Settings) -> int:
    """Start queued executions while respecting global/per-client concurrency. Returns started count."""
    if dispatch_paused(db):
        return 0

    in_qlik = db.scalar(select(func.count(Execution.id)).where(Execution.status.in_(ExecStatus.IN_QLIK)))
    free = settings.max_concurrent_executions - in_qlik
    if free <= 0:
        return 0

    per_client = dict(
        db.execute(
            select(Execution.client_id, func.count(Execution.id))
            .where(Execution.status.in_(ExecStatus.IN_QLIK))
            .group_by(Execution.client_id)
        ).all()
    )
    running_tasks = set(db.scalars(select(Execution.task_id).where(Execution.status.in_(ExecStatus.IN_QLIK))))

    queued = db.scalars(
        select(Execution)
        .where(Execution.status == ExecStatus.QUEUED)
        .order_by(Execution.priority, Execution.created_at)
        .limit(200)
    ).all()

    started = 0
    for ex in queued:
        if started >= free:
            break
        client = ex.client
        task = db.get(QlikTask, ex.task_id)
        if not client.enabled:
            _finish(ex, ExecStatus.CANCELLED, error="Client was blocked while the request was queued")
            continue
        if task is None or task.blocked:
            _finish(ex, ExecStatus.CANCELLED, error="Task was blocked while the request was queued")
            continue
        if ex.task_id in running_tasks:
            continue  # wait for the running reload of the same task to finish
        if client.max_concurrent and per_client.get(client.id, 0) >= client.max_concurrent:
            continue

        ex.dispatched_at = utcnow()
        try:
            ex.qlik_execution_id = backend.start_task(ex.task_id)
        except QlikError as e:
            _finish(ex, ExecStatus.START_ERROR, error=str(e))
            audit(
                db,
                actor_type="system",
                actor="worker",
                action="execution.start_failed",
                client_id=client.id,
                task_id=ex.task_id,
                execution_id=ex.id,
                outcome="error",
                message=str(e),
            )
            db.flush()
            continue
        ex.status = ExecStatus.STARTING
        per_client[client.id] = per_client.get(client.id, 0) + 1
        running_tasks.add(ex.task_id)
        started += 1
        audit(
            db,
            actor_type="system",
            actor="worker",
            action="execution.dispatched",
            client_id=client.id,
            task_id=ex.task_id,
            execution_id=ex.id,
            meta={"qlik_execution_id": ex.qlik_execution_id, "initiator": ex.initiator},
        )
        db.flush()
    return started


# --------------------------------------------------------------------------------------
# poll (worker)
# --------------------------------------------------------------------------------------
def poll(db: Session, backend: QlikBackend, settings: Settings) -> int:
    """One bulk request to Qlik for all executions it is running for us. Returns updated count."""
    active = db.scalars(
        select(Execution).where(Execution.status.in_(ExecStatus.IN_QLIK), Execution.qlik_execution_id.is_not(None))
    ).all()
    if not active:
        return 0
    results = backend.get_execution_results([e.qlik_execution_id for e in active])
    now = utcnow()
    updated = 0
    for ex in active:
        ex.last_polled_at = now
        info = results.get(ex.qlik_execution_id)
        if info is None:
            age = (now - (ex.dispatched_at or ex.created_at)).total_seconds()
            if age > settings.lost_after_seconds:
                _finish(ex, ExecStatus.LOST, error="Qlik has no execution result for this execution id")
                updated += 1
            continue
        new_status = map_qlik_status(info.status_code)
        ex.qlik_status_code = info.status_code
        ex.qlik_status_text = info.status_text
        ex.node = info.node or ex.node
        ex.qlik_started_at = info.start_time or ex.qlik_started_at
        ex.qlik_stopped_at = info.stop_time or ex.qlik_stopped_at
        ex.details = info.details[-50:]
        ex.script_log_ref = info.script_log_ref or ex.script_log_ref
        if new_status in ExecStatus.TERMINAL:
            error = None
            if new_status != ExecStatus.SUCCESS:
                msgs = [d.get("message") for d in info.details if d.get("message")]
                error = msgs[-1] if msgs else info.status_text
            _finish(ex, new_status, error=error)
        else:
            ex.status = new_status
            started = ex.qlik_started_at or ex.dispatched_at or ex.created_at
            if (now - started).total_seconds() > settings.execution_timeout_seconds:
                _finish(ex, ExecStatus.TIMEOUT, error="Gateway execution timeout exceeded (reload not stopped)")
        updated += 1
    return updated


def _finish(ex: Execution, status: str, *, error: str | None = None) -> None:
    ex.status = status
    ex.finished_at = utcnow()
    if error:
        ex.error = error[:4000]
    EXECUTIONS_FINISHED.labels(ex.client.name if ex.client else "?", status).inc()


def refresh_active_gauge(db: Session) -> None:
    counts = dict(
        db.execute(
            select(Execution.status, func.count(Execution.id))
            .where(Execution.status.in_(ExecStatus.ACTIVE))
            .group_by(Execution.status)
        ).all()
    )
    for st in ExecStatus.ACTIVE:
        EXECUTIONS_ACTIVE.labels(st).set(counts.get(st, 0))


# --------------------------------------------------------------------------------------
# catalog & node health (worker)
# --------------------------------------------------------------------------------------
def sync_catalog(db: Session, backend: QlikBackend) -> int:
    tasks = backend.list_reload_tasks()
    now = utcnow()
    seen = set()
    for t in tasks:
        seen.add(t.id)
        row = db.get(QlikTask, t.id)
        if row is None:
            row = QlikTask(id=t.id)
            db.add(row)
        row.name = t.name
        row.app_id = t.app_id
        row.app_name = t.app_name
        row.stream_name = t.stream_name
        row.enabled_in_qlik = t.enabled
        row.custom_properties = t.custom_properties
        row.tags = t.tags
        row.synced_at = now
        row.present_in_qlik = True
    for row in db.scalars(select(QlikTask).where(QlikTask.id.not_in(seen or {""}))):
        row.present_in_qlik = False
    db.flush()
    return len(tasks)


def collect_node_health(db: Session, backend: QlikBackend, urls: list[str]) -> None:
    for url in urls:
        node = url.split("//", 1)[-1].split("/", 1)[0]
        try:
            h = backend.engine_health(url)
        except QlikError as e:
            db.add(NodeHealth(node=node, ok=False, raw={"error": str(e)}))
            continue
        mem = h.get("mem") or {}
        db.add(
            NodeHealth(
                node=node,
                ok=True,
                cpu_pct=(h.get("cpu") or {}).get("total"),
                mem_committed_mb=mem.get("committed"),
                mem_free_mb=mem.get("free"),
                apps_loaded=len((h.get("apps") or {}).get("loaded_docs") or []),
                sessions_active=(h.get("session") or {}).get("active"),
                saturated=h.get("saturated"),
                raw={k: h.get(k) for k in ("version", "mem", "cpu", "session", "saturated")},
            )
        )
    db.flush()
