"""Client API: the only way external schedulers talk to Qlik.

The gateway performs every action on its own behalf; state/details/info are served from
the gateway's database, which the coordinator refreshes with one bulk Qlik call per tick.
"""

import asyncio
import time

from fastapi import APIRouter, Depends, Query, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db, session_scope
from ..models import Client, ExecStatus, Execution, QlikTask
from ..qlik import QlikError
from ..services import executions as svc
from ..services.errors import ServiceError
from .deps import client_ip, current_client, get_backend, initiator_meta

router = APIRouter(prefix="/api/v1", tags=["client API"])


class StartRequest(BaseModel):
    dedupe: bool = Field(True, description="Attach to an already queued/running execution of the same task")
    priority: int | None = Field(None, description="Lower is sooner; cannot be better than the client's priority")
    meta: dict = Field(default_factory=dict, description="Free-form initiator metadata stored with the execution")


def task_to_dict(t: QlikTask) -> dict:
    return {
        "id": t.id,
        "name": t.name,
        "app_id": t.app_id,
        "app_name": t.app_name,
        "stream": t.stream_name,
        "enabled_in_qlik": t.enabled_in_qlik,
        "blocked": t.blocked,
        "tags": t.tags,
        "custom_properties": t.custom_properties,
        "synced_at": _iso(t.synced_at),
    }


def exec_state(ex: Execution) -> dict:
    return {
        "execution_id": ex.id,
        "task_id": ex.task_id,
        "task_name": ex.task_name,
        "status": ex.status,
        "terminal": ex.status in ExecStatus.TERMINAL,
        "success": ex.status == ExecStatus.SUCCESS,
        "qlik_status": ex.qlik_status_text,
        "created_at": _iso(ex.created_at),
        "started_at": _iso(ex.qlik_started_at),
        "finished_at": _iso(ex.finished_at),
        "duration_seconds": ex.duration_seconds,
        "error": ex.error,
        "error_detail": ex.error_detail,
        "last_polled_at": _iso(ex.last_polled_at),
    }


def exec_details(ex: Execution, viewer: Client) -> dict:
    d = exec_state(ex)
    own = ex.client_id == viewer.id
    d.update(
        {
            # another client may see the execution (it shares the task, e.g. after dedupe),
            # but never who started it
            "own": own,
            "client": ex.client.name if own else None,
            "initiator": ex.initiator if own else {},
            "priority": ex.priority,
            "qlik_execution_id": ex.qlik_execution_id,
            "qlik_status_code": ex.qlik_status_code,
            "node": ex.node,
            "dispatched_at": _iso(ex.dispatched_at),
            "queue_wait_seconds": ex.wait_seconds,
            "stopped_at": _iso(ex.qlik_stopped_at),
            "details": ex.details,
            "script_log_available": bool(ex.script_log_ref),
            "dedup_hits": ex.dedup_hits,
        }
    )
    return d


def _iso(dt) -> str | None:
    return dt.isoformat() + "Z" if dt else None


def _load_execution(db: Session, client: Client, execution_id: int, request: Request) -> Execution:
    request.state.audit["execution_id"] = execution_id
    ex = db.get(Execution, execution_id)
    if ex is None or (
        ex.client_id != client.id and not svc.client_can_task(client, ex.task_id, db.get(QlikTask, ex.task_id))
    ):
        raise ServiceError(404, "execution_not_found", f"Execution {execution_id} not found")
    request.state.audit["task_id"] = ex.task_id
    return ex


# ------------------------------------------------------------------------------------------
@router.get("/whoami", summary="Who am I and what am I allowed to do")
def whoami(client: Client = Depends(current_client)):
    return {
        "client": client.name,
        "allowed_actions": client.allowed_actions,
        "allowed_tasks": client.allowed_tasks,
        "limits": {
            "requests_per_minute": client.requests_per_minute,
            "starts_per_hour": client.starts_per_hour,
            "max_concurrent": client.max_concurrent,
        },
    }


@router.get("/tasks", summary="Tasks this client may use (getinfo, list)")
def list_tasks(request: Request, client: Client = Depends(current_client), db: Session = Depends(get_db)):
    request.state.audit["action"] = "info"
    svc.require_action(client, "info")
    return [task_to_dict(t) for t in svc.visible_tasks(db, client)]


@router.get("/tasks/{task_id}", summary="getinfo: task info + recent executions")
def task_info(task_id: str, request: Request, client: Client = Depends(current_client), db: Session = Depends(get_db)):
    request.state.audit.update(action="info", task_id=task_id)
    svc.require_action(client, "info")
    task = svc.require_task(db, client, task_id)
    recent = db.scalars(
        select(Execution).where(Execution.task_id == task_id).order_by(Execution.id.desc()).limit(10)
    ).all()
    d = task_to_dict(task)
    d["recent_executions"] = [exec_state(e) for e in recent]
    active = svc.active_execution_for_task(db, task_id)
    d["active_execution_id"] = active.id if active else None
    return d


@router.post("/tasks/{task_id}/start", status_code=202, summary="post task: queue a reload")
def start_task(
    task_id: str,
    request: Request,
    body: StartRequest | None = None,
    client: Client = Depends(current_client),
    db: Session = Depends(get_db),
):
    body = body or StartRequest()
    request.state.audit.update(action="start", task_id=task_id)
    initiator = initiator_meta(request)
    initiator.update({k: str(v)[:300] for k, v in (body.meta or {}).items()})
    ex, dedup = svc.submit(
        db,
        client,
        task_id,
        initiator=initiator,
        caller_ip=client_ip(request),
        priority=body.priority,
        dedupe=body.dedupe,
    )
    db.commit()  # the caller must be able to read the execution as soon as it gets the id
    request.state.audit.update(execution_id=ex.id, message="deduplicated" if dedup else "queued")
    out = exec_state(ex)
    out["deduplicated"] = dedup
    return out


@router.get("/executions", summary="get state of recent executions of this client")
def list_executions(
    request: Request,
    task_id: str | None = None,
    status: str | None = None,
    limit: int = Query(50, le=500),
    client: Client = Depends(current_client),
    db: Session = Depends(get_db),
):
    request.state.audit["action"] = "state"
    svc.require_action(client, "state")
    q = select(Execution).where(Execution.client_id == client.id)
    if task_id:
        q = q.where(Execution.task_id == task_id)
    if status:
        q = q.where(Execution.status == status.upper())
    return [exec_state(e) for e in db.scalars(q.order_by(Execution.id.desc()).limit(limit))]


@router.get("/executions/{execution_id}", summary="get state (served from the gateway, never hits Qlik)")
async def execution_state(
    execution_id: int,
    request: Request,
    wait: int = Query(0, ge=0, description="Long-poll: wait up to N seconds for a terminal state"),
    client: Client = Depends(current_client),
):
    request.state.audit["action"] = "state"
    svc.require_action(client, "state")

    def load() -> dict:
        with session_scope() as db:
            c = db.get(Client, client.id)
            return exec_state(_load_execution(db, c, execution_id, request))

    state = await run_in_threadpool(load)
    deadline = time.monotonic() + min(wait, get_settings().long_poll_max_seconds)
    while not state["terminal"] and time.monotonic() < deadline:
        await asyncio.sleep(2)
        state = await run_in_threadpool(load)
    return state


@router.get("/executions/{execution_id}/details", summary="getdetails")
def execution_details(
    execution_id: int, request: Request, client: Client = Depends(current_client), db: Session = Depends(get_db)
):
    request.state.audit["action"] = "details"
    svc.require_action(client, "details")
    return exec_details(_load_execution(db, client, execution_id, request), client)


@router.get("/executions/{execution_id}/log", summary="Qlik script log of a finished execution")
def execution_log(
    execution_id: int,
    request: Request,
    client: Client = Depends(current_client),
    db: Session = Depends(get_db),
    backend=Depends(get_backend),
):
    request.state.audit["action"] = "log"
    svc.require_action(client, "log")
    ex = _load_execution(db, client, execution_id, request)
    if not ex.script_log_ref:
        raise ServiceError(404, "no_log", "Script log is not available (yet)")
    try:
        text = backend.get_script_log(ex.task_id, ex.script_log_ref)
    except QlikError as e:
        raise ServiceError(502, "qlik_error", str(e)) from e
    return {"execution_id": ex.id, "log": text[-200_000:]}


@router.post("/executions/{execution_id}/cancel", summary="Cancel a queued execution or stop a running reload")
def execution_cancel(
    execution_id: int,
    request: Request,
    client: Client = Depends(current_client),
    db: Session = Depends(get_db),
    backend=Depends(get_backend),
):
    request.state.audit["action"] = "stop"
    svc.require_action(client, "stop")
    ex = _load_execution(db, client, execution_id, request)
    try:
        svc.cancel(db, backend, ex, actor_type="client", actor=client.name)
    except QlikError as e:
        raise ServiceError(502, "qlik_error", str(e)) from e
    db.commit()
    return exec_state(ex)
