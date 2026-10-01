"""Coordinator logic: accept requests, dispatch them to Qlik, poll Qlik in bulk, keep history."""

import logging
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..models import ON_ACTIVE, Client, ExecStatus, Execution, NodeHealth, QlikTask, normalize_on_active, utcnow
from ..qlik import QlikBackend, QlikError, map_qlik_status
from . import runtime
from .audit import audit
from .errors import ServiceError
from .kv import dispatch_paused
from .metrics import EXECUTIONS_ACTIVE, EXECUTIONS_FINISHED
from .scriptlog import extract_script_error

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------------------
# permissions
# --------------------------------------------------------------------------------------
def client_can_task(client: Client, task_id: str, task: QlikTask | None = None) -> bool:
    """Access is granted in the gateway UI (task list or "*") or in QMC via the client property."""
    allowed = client.allowed_tasks or []
    if "*" in allowed or task_id in allowed:
        return True
    return task is not None and client.name.lower() in {n.strip().lower() for n in granted_in_qlik(task) if n}


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


def queued_execution_for_task(db: Session, task_id: str) -> Execution | None:
    """An execution that has not been sent to Qlik yet: when it starts, it reads the data as of then."""
    return db.scalars(
        select(Execution)
        .where(Execution.task_id == task_id, Execution.status == ExecStatus.QUEUED)
        .order_by(Execution.id)
        .limit(1)
    ).first()


@dataclass
class SubmitResult:
    execution: Execution | None = None
    deduplicated: bool = False  # the request was collapsed into an existing execution
    on_active: str = "reject"  # the policy actually applied
    requested: str | None = None  # what the client asked for
    source: str = "default"  # where the applied policy came from: request / default / client / task
    running: Execution | None = None  # the active execution (collapsed into / reused / queued behind)
    warnings: list[dict] = field(default_factory=list)


def resolve_on_active(client: Client, task: QlikTask, requested: str | None) -> tuple[str, str]:
    """An administrator's setting on the task wins over the one on the client, which wins over the request."""
    if normalize_on_active(task.if_running_policy):
        return normalize_on_active(task.if_running_policy), "task"
    if normalize_on_active(client.if_running_policy):
        return normalize_on_active(client.if_running_policy), "client"
    if requested:
        return requested, "request"
    return get_settings().default_on_active, "default"


def submit(
    db: Session,
    client: Client,
    task_id: str,
    *,
    initiator: dict,
    caller_ip: str | None,
    priority: int | None = None,
    on_active: str | None = None,
    running_info=None,
) -> SubmitResult:
    """Accept a start request.

    1. The task is not reloading and a run of it is waiting in the gateway queue: the request
       collapses into that run, whoever sent it (one execution id for everybody).
    2. The task is reloading in Qlik - on_active (for any client and initiator of the active run):
       "reuse"  - return the active execution (warning: it may not contain data prepared after it started)
       "queue"  - queue a run that starts after the active one; identical requests collapse into it (1.)
       "reject" - 409 already_running with the reason and the active execution (default)
    An administrator may force on_active per task or per client; the answer says which one applied.
    Legacy if_running values (attach/fresh/queue/skip) are accepted.
    """
    requested = normalize_on_active(on_active) if on_active else None
    if on_active and requested is None:
        raise ServiceError(422, "bad_on_active", "on_active must be one of: " + ", ".join(ON_ACTIVE))
    require_action(client, "start")
    task = require_task(db, client, task_id)

    if task.blocked:
        raise ServiceError(423, "task_blocked", f"Task is blocked by administrator: {task.blocked_reason or '-'}")
    if not task.enabled_in_qlik:
        raise ServiceError(409, "task_disabled", "Task is disabled in Qlik")

    policy, source = resolve_on_active(client, task, requested)
    result = SubmitResult(on_active=policy, requested=requested, source=source)
    if requested and policy != requested:
        result.warnings.append(
            {
                "code": "policy_overridden",
                "message": f"on_active={requested} was replaced by {policy}, set by the administrator on the {source}",
            }
        )

    # 1. a run that has not reached Qlik yet reads the data as of its start: everybody shares it
    waiting = queued_execution_for_task(db, task_id)
    if waiting is not None:
        waiting.dedup_hits += 1
        result.execution, result.deduplicated, result.running = waiting, True, waiting
        return result

    # 2. the task is reloading in Qlik
    active = active_execution_for_task(db, task_id)
    if active is not None:
        if policy == "reuse":
            active.dedup_hits += 1
            result.execution, result.deduplicated, result.running = active, True, active
            since = f" at {active.qlik_started_at:%Y-%m-%d %H:%M:%S} UTC" if active.qlik_started_at else ""
            result.warnings.append(
                {
                    "code": "reused_active_run",
                    "message": f"Returned execution {active.id}, which was already reloading in Qlik{since}, "
                    "before this request: data prepared after that may not be loaded by it",
                }
            )
            return result
        if policy == "reject":
            raise ServiceError(
                409,
                "already_running",
                f"Task is already {active.status.lower()} in Qlik (execution {active.id}); nothing was started. "
                "Wait for it and retry, or ask with on_active=queue / reuse",
                {
                    "on_active": policy,
                    "policy_source": source,
                    "running_execution": running_info(active) if running_info else {"execution_id": active.id},
                },
            )
        running = active  # queue: a new run after the active one
    else:
        running = None

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
    result.execution = execution
    if running is not None:
        result.running = running
        result.warnings.append(
            {
                "code": "queued_behind_active",
                "message": f"Task is already {running.status.lower()} in Qlik (execution {running.id}); "
                f"execution {execution.id} will start after it finishes",
            }
        )
    return result


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
    free = runtime.effective(db, settings).max_concurrent_executions - in_qlik
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
                detail = _script_error(backend, ex)
                if detail:
                    ex.error_detail = detail
                    error = detail.splitlines()[0]
            _finish(ex, new_status, error=error)
        else:
            ex.status = new_status
            started = ex.qlik_started_at or ex.dispatched_at or ex.created_at
            if (now - started).total_seconds() > settings.execution_timeout_seconds:
                _finish(ex, ExecStatus.TIMEOUT, error="Gateway execution timeout exceeded (reload not stopped)")
        updated += 1
    return updated


def _script_error(backend: QlikBackend, ex: Execution) -> str | None:
    """The real reason of a failed reload from its script log (one extra Qlik call per failure)."""
    if not ex.script_log_ref:
        return None
    try:
        return extract_script_error(backend.get_script_log(ex.task_id, ex.script_log_ref))
    except QlikError as e:
        log.info("script log of execution %s is not available: %s", ex.id, e)
        return None


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
