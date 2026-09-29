"""Admin UI: monitoring, clients (tokens, rights, limits, blocking), tasks, executions, audit."""

import secrets
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import Text, case, func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..models import ACTIONS, AdminUser, AuditLog, Client, ExecStatus, Execution, NodeHealth, QlikTask, utcnow
from ..qlik import QlikError
from ..security import generate_client_token, verify_password
from ..services import executions as svc
from ..services.audit import audit
from ..services.errors import ServiceError
from ..services.kv import DISPATCH_PAUSED, dispatch_paused, set_value
from .deps import client_ip, get_backend

templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))
router = APIRouter(prefix="/ui", include_in_schema=False)

STATUS_CLASS = {
    ExecStatus.QUEUED: "muted",
    ExecStatus.STARTING: "info",
    ExecStatus.RUNNING: "info",
    ExecStatus.SUCCESS: "ok",
    ExecStatus.SKIPPED: "muted",
    ExecStatus.CANCELLED: "muted",
}
templates.env.globals["status_class"] = lambda s: STATUS_CLASS.get(s, "bad")
templates.env.filters["dt"] = lambda d: d.strftime("%Y-%m-%d %H:%M:%S") if d else "—"
templates.env.filters["dur"] = lambda s: "—" if s is None else (f"{s:.0f}с" if s < 120 else f"{s / 60:.1f}м")


class NotLoggedIn(Exception):
    pass


def admin_user(request: Request) -> str:
    user = request.session.get("admin")
    if not user:
        raise NotLoggedIn()
    return user


def csrf_token(request: Request) -> str:
    tok = request.session.get("csrf")
    if not tok:
        tok = secrets.token_urlsafe(24)
        request.session["csrf"] = tok
    return tok


async def check_csrf(request: Request) -> None:
    if request.method == "POST":
        form = await request.form()
        if not secrets.compare_digest(str(form.get("csrf", "")), request.session.get("csrf", "")):
            raise HTTPException(400, "CSRF token mismatch, reload the page")


def render(request: Request, name: str, **ctx) -> HTMLResponse:
    ctx.setdefault("admin", request.session.get("admin"))
    ctx["csrf"] = csrf_token(request)
    ctx["flash"] = request.session.pop("flash", None)
    return templates.TemplateResponse(request, name, ctx)


def flash(request: Request, msg: str, kind: str = "ok") -> None:
    request.session["flash"] = {"msg": msg, "kind": kind}


def back(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def admin_audit(db: Session, request: Request, action: str, **fields) -> None:
    audit(
        db,
        actor_type="admin",
        actor=request.session.get("admin", "?"),
        action=action,
        ip=client_ip(request),
        user_agent=(request.headers.get("user-agent") or "")[:500],
        **fields,
    )


# ------------------------------------------------------------------------------------------
# login
# ------------------------------------------------------------------------------------------
@router.get("/login")
def login_page(request: Request):
    return render(request, "login.html")


@router.post("/login", dependencies=[Depends(check_csrf)])
def login(request: Request, username: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    user = db.scalars(select(AdminUser).where(AdminUser.username == username)).first()
    ok = user is not None and user.enabled and verify_password(password, user.password_hash)
    audit(
        db,
        actor_type="admin",
        actor=username,
        action="admin.login",
        outcome="ok" if ok else "denied",
        ip=client_ip(request),
        user_agent=(request.headers.get("user-agent") or "")[:500],
    )
    if not ok:
        flash(request, "Неверный логин или пароль", "bad")
        return back("/ui/login")
    request.session.clear()
    request.session["admin"] = username
    return back("/ui/")


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return back("/ui/login")


# ------------------------------------------------------------------------------------------
# dashboard
# ------------------------------------------------------------------------------------------
@router.get("/")
def dashboard(request: Request, user: str = Depends(admin_user), db: Session = Depends(get_db)):
    now = utcnow()
    day = now - timedelta(hours=24)
    hour = now - timedelta(hours=1)

    by_status = dict(
        db.execute(
            select(Execution.status, func.count(Execution.id))
            .where((Execution.created_at >= day) | Execution.status.in_(ExecStatus.ACTIVE))
            .group_by(Execution.status)
        ).all()
    )
    active = db.scalars(
        select(Execution).where(Execution.status.in_(ExecStatus.ACTIVE)).order_by(Execution.status, Execution.id)
    ).all()

    fail_expr = func.sum(case((Execution.status.not_in([ExecStatus.SUCCESS, *ExecStatus.ACTIVE]), 1), else_=0))
    starts = {
        r.client_id: r
        for r in db.execute(
            select(
                Execution.client_id,
                func.count(Execution.id).label("starts"),
                fail_expr.label("fails"),
                func.sum(Execution.dedup_hits).label("dedup"),
            )
            .where(Execution.created_at >= day)
            .group_by(Execution.client_id)
        )
    }
    calls = {
        r.client_id: r
        for r in db.execute(
            select(
                AuditLog.client_id,
                func.count(AuditLog.id).label("calls"),
                func.sum(case((AuditLog.outcome != "ok", 1), else_=0)).label("rejected"),
            )
            .where(AuditLog.ts >= day, AuditLog.actor_type == "client")
            .group_by(AuditLog.client_id)
        )
    }
    clients = db.scalars(select(Client).order_by(Client.name)).all()
    load = []
    for c in clients:
        s, k = starts.get(c.id), calls.get(c.id)
        load.append(
            {
                "client": c,
                "calls": k.calls if k else 0,
                "rejected": (k.rejected or 0) if k else 0,
                "starts": s.starts if s else 0,
                "fails": (s.fails or 0) if s else 0,
                "dedup": (s.dedup or 0) if s else 0,
            }
        )
    load.sort(key=lambda r: (r["calls"], r["starts"]), reverse=True)

    top_tasks = db.execute(
        select(
            Execution.task_id,
            Execution.task_name,
            func.count(Execution.id).label("runs"),
            fail_expr.label("fails"),
        )
        .where(Execution.created_at >= day)
        .group_by(Execution.task_id, Execution.task_name)
        .order_by(func.count(Execution.id).desc())
        .limit(10)
    ).all()
    durations = {}
    for ex in db.scalars(select(Execution).where(Execution.created_at >= day, Execution.qlik_stopped_at.is_not(None))):
        durations.setdefault(ex.task_id, []).append(ex.duration_seconds or 0)

    qlik_calls_hour = db.scalar(
        select(func.count(AuditLog.id)).where(AuditLog.actor_type == "qlik", AuditLog.ts >= hour)
    )
    qlik_errors_hour = db.scalar(
        select(func.count(AuditLog.id)).where(
            AuditLog.actor_type == "qlik", AuditLog.ts >= hour, AuditLog.outcome != "ok"
        )
    )

    latest_ids = select(func.max(NodeHealth.id)).group_by(NodeHealth.node)
    nodes = db.scalars(select(NodeHealth).where(NodeHealth.id.in_(latest_ids)).order_by(NodeHealth.node)).all()

    return render(
        request,
        "dashboard.html",
        by_status=by_status,
        active=active,
        load=load,
        top_tasks=top_tasks,
        durations={k: (sum(v) / len(v), max(v), sum(v)) for k, v in durations.items()},
        qlik_calls_hour=qlik_calls_hour,
        qlik_errors_hour=qlik_errors_hour,
        nodes=nodes,
        paused=dispatch_paused(db),
        settings=get_settings(),
    )


@router.post("/dispatch", dependencies=[Depends(check_csrf)])
def toggle_dispatch(
    request: Request,
    pause: str = Form(...),
    reason: str = Form(""),
    user: str = Depends(admin_user),
    db: Session = Depends(get_db),
):
    if pause == "1":
        set_value(db, DISPATCH_PAUSED, {"by": user, "reason": reason, "at": utcnow().isoformat()})
        admin_audit(db, request, "dispatch.pause", message=reason)
        flash(request, "Запуск задач в Qlik приостановлен. Новые запросы копятся в очереди.", "warn")
    else:
        set_value(db, DISPATCH_PAUSED, None)
        admin_audit(db, request, "dispatch.resume")
        flash(request, "Запуск задач возобновлён")
    return back("/ui/")


# ------------------------------------------------------------------------------------------
# clients
# ------------------------------------------------------------------------------------------
def _parse_list(raw: str) -> list[str]:
    return [x.strip() for x in raw.replace(",", "\n").splitlines() if x.strip()]


@router.get("/clients")
def clients_page(request: Request, user: str = Depends(admin_user), db: Session = Depends(get_db)):
    return render(request, "clients.html", clients=db.scalars(select(Client).order_by(Client.name)).all())


@router.get("/clients/new")
def client_new_page(request: Request, user: str = Depends(admin_user), db: Session = Depends(get_db)):
    s = get_settings()
    blank = Client(
        name="",
        description="",
        owner_contact="",
        enabled=True,
        allowed_actions=["start", "state", "details", "info"],
        allowed_tasks=[],
        allowed_ips=[],
        requests_per_minute=s.default_requests_per_minute,
        starts_per_hour=s.default_starts_per_hour,
        max_concurrent=s.default_max_concurrent,
        priority=100,
    )
    return render(
        request,
        "client_edit.html",
        c=blank,
        actions=ACTIONS,
        tasks=_all_tasks(db),
        qlik_granted=set(),
        client_prop=get_settings().qlik_client_custom_property,
        is_new=True,
    )


def _all_tasks(db: Session):
    return db.scalars(select(QlikTask).where(QlikTask.present_in_qlik.is_(True)).order_by(QlikTask.name)).all()


def _apply_client_form(c: Client, form) -> None:
    c.description = str(form.get("description", ""))
    c.owner_contact = str(form.get("owner_contact", ""))
    c.allowed_actions = [a for a in ACTIONS if form.get(f"act_{a}")]
    if form.get("all_tasks"):
        c.allowed_tasks = ["*"]
    else:
        c.allowed_tasks = list(dict.fromkeys(form.getlist("tasks") + _parse_list(str(form.get("extra_tasks", "")))))
    c.allowed_ips = _parse_list(str(form.get("allowed_ips", "")))
    c.requests_per_minute = int(form.get("requests_per_minute") or 0)
    c.starts_per_hour = int(form.get("starts_per_hour") or 0)
    c.max_concurrent = int(form.get("max_concurrent") or 0)
    c.priority = int(form.get("priority") or 100)
    exp = str(form.get("token_expires_at", "")).strip()
    c.token_expires_at = datetime.fromisoformat(exp) if exp else None


@router.post("/clients/new", dependencies=[Depends(check_csrf)])
async def client_create(request: Request, user: str = Depends(admin_user), db: Session = Depends(get_db)):
    form = await request.form()
    name = str(form.get("name", "")).strip()
    if not name or db.scalars(select(Client).where(Client.name == name)).first():
        flash(request, "Имя пустое или уже занято", "bad")
        return back("/ui/clients/new")
    token, prefix, token_hash = generate_client_token()
    c = Client(name=name, token_prefix=prefix, token_hash=token_hash, token_created_at=utcnow(), enabled=True)
    _apply_client_form(c, form)
    db.add(c)
    db.flush()
    admin_audit(db, request, "client.create", client_id=c.id, meta={"name": name})
    request.session["new_token"] = {"client_id": c.id, "token": token}
    return back(f"/ui/clients/{c.id}")


@router.get("/clients/{client_id}")
def client_edit_page(client_id: int, request: Request, user: str = Depends(admin_user), db: Session = Depends(get_db)):
    c = db.get(Client, client_id) or _404()
    new_token = request.session.pop("new_token", None)
    if new_token and new_token.get("client_id") != client_id:
        new_token = None
    recent_audit = db.scalars(
        select(AuditLog).where(AuditLog.client_id == client_id).order_by(AuditLog.id.desc()).limit(30)
    ).all()
    tasks = _all_tasks(db)
    return render(
        request,
        "client_edit.html",
        c=c,
        actions=ACTIONS,
        tasks=tasks,
        qlik_granted={t.id for t in tasks if svc.client_can_task(Client(name=c.name, allowed_tasks=[]), t.id, t)},
        client_prop=get_settings().qlik_client_custom_property,
        is_new=False,
        new_token=new_token and new_token["token"],
        recent_audit=recent_audit,
    )


@router.post("/clients/{client_id}", dependencies=[Depends(check_csrf)])
async def client_update(
    client_id: int, request: Request, user: str = Depends(admin_user), db: Session = Depends(get_db)
):
    c = db.get(Client, client_id) or _404()
    form = await request.form()
    before = _client_snapshot(c)
    _apply_client_form(c, form)
    admin_audit(db, request, "client.update", client_id=c.id, meta={"before": before, "after": _client_snapshot(c)})
    flash(request, "Сохранено")
    return back(f"/ui/clients/{client_id}")


def _client_snapshot(c: Client) -> dict:
    return {
        "actions": c.allowed_actions,
        "tasks": c.allowed_tasks,
        "ips": c.allowed_ips,
        "rpm": c.requests_per_minute,
        "sph": c.starts_per_hour,
        "conc": c.max_concurrent,
        "prio": c.priority,
    }


@router.post("/clients/{client_id}/block", dependencies=[Depends(check_csrf)])
def client_block(
    client_id: int,
    request: Request,
    reason: str = Form(""),
    cancel_queued: str = Form(""),
    stop_running: str = Form(""),
    user: str = Depends(admin_user),
    db: Session = Depends(get_db),
    backend=Depends(get_backend),
):
    c = db.get(Client, client_id) or _404()
    c.enabled = False
    c.blocked_reason = reason
    cancelled = stopped = 0
    if cancel_queued or stop_running:
        for ex in db.scalars(
            select(Execution).where(Execution.client_id == c.id, Execution.status.in_(ExecStatus.ACTIVE))
        ):
            if ex.status == ExecStatus.QUEUED and cancel_queued:
                svc.cancel(db, backend, ex, actor_type="admin", actor=user)
                cancelled += 1
            elif ex.status in ExecStatus.IN_QLIK and stop_running:
                try:
                    svc.cancel(db, backend, ex, actor_type="admin", actor=user)
                    stopped += 1
                except QlikError as e:
                    flash(request, f"Не удалось остановить {ex.task_name}: {e}", "bad")
    admin_audit(
        db, request, "client.block", client_id=c.id, message=reason, meta={"cancelled": cancelled, "stopped": stopped}
    )
    request.session.setdefault(
        "flash",
        {"msg": f"Клиент заблокирован. Отменено в очереди: {cancelled}, остановлено: {stopped}", "kind": "warn"},
    )
    return back(f"/ui/clients/{client_id}")


@router.post("/clients/{client_id}/unblock", dependencies=[Depends(check_csrf)])
def client_unblock(client_id: int, request: Request, user: str = Depends(admin_user), db: Session = Depends(get_db)):
    c = db.get(Client, client_id) or _404()
    c.enabled = True
    c.blocked_reason = ""
    admin_audit(db, request, "client.unblock", client_id=c.id)
    flash(request, "Клиент разблокирован")
    return back(f"/ui/clients/{client_id}")


@router.post("/clients/{client_id}/rotate", dependencies=[Depends(check_csrf)])
def client_rotate(client_id: int, request: Request, user: str = Depends(admin_user), db: Session = Depends(get_db)):
    c = db.get(Client, client_id) or _404()
    token, prefix, token_hash = generate_client_token()
    c.token_prefix, c.token_hash, c.token_created_at = prefix, token_hash, utcnow()
    admin_audit(db, request, "client.rotate_token", client_id=c.id)
    request.session["new_token"] = {"client_id": c.id, "token": token}
    return back(f"/ui/clients/{client_id}")


# ------------------------------------------------------------------------------------------
# tasks
# ------------------------------------------------------------------------------------------
@router.get("/tasks")
def tasks_page(request: Request, user: str = Depends(admin_user), db: Session = Depends(get_db)):
    tasks = db.scalars(select(QlikTask).order_by(QlikTask.present_in_qlik.desc(), QlikTask.name)).all()
    last = {
        r.task_id: r
        for r in db.execute(
            select(
                Execution.task_id, func.max(Execution.created_at).label("last"), func.count(Execution.id).label("n")
            ).group_by(Execution.task_id)
        )
    }
    clients = db.scalars(select(Client)).all()
    users = {t.id: [c.name for c in clients if svc.client_can_task(c, t.id, t)] for t in tasks}
    return render(request, "tasks.html", tasks=tasks, last=last, users=users)


@router.post("/tasks/sync", dependencies=[Depends(check_csrf)])
def tasks_sync(
    request: Request, user: str = Depends(admin_user), db: Session = Depends(get_db), backend=Depends(get_backend)
):
    try:
        n = svc.sync_catalog(db, backend)
        flash(request, f"Каталог обновлён из Qlik: {n} задач")
        admin_audit(db, request, "tasks.sync", message=str(n))
    except QlikError as e:
        flash(request, f"Ошибка Qlik: {e}", "bad")
    return back("/ui/tasks")


@router.post("/tasks/{task_id}", dependencies=[Depends(check_csrf)])
def task_update(
    task_id: str,
    request: Request,
    blocked: str = Form(""),
    blocked_reason: str = Form(""),
    min_interval_seconds: int = Form(0),
    user: str = Depends(admin_user),
    db: Session = Depends(get_db),
):
    t = db.get(QlikTask, task_id) or _404()
    t.blocked = bool(blocked)
    t.blocked_reason = blocked_reason if t.blocked else ""
    t.min_interval_seconds = max(0, min_interval_seconds)
    admin_audit(
        db,
        request,
        "task.update",
        task_id=task_id,
        meta={"blocked": t.blocked, "reason": t.blocked_reason, "min_interval": t.min_interval_seconds},
    )
    flash(request, f"Задача «{t.name}» обновлена")
    return back("/ui/tasks")


# ------------------------------------------------------------------------------------------
# executions
# ------------------------------------------------------------------------------------------
@router.get("/executions")
def executions_page(
    request: Request,
    client_id: str = "",
    status: str = "",
    task: str = "",
    initiator: str = "",
    page: int = 1,
    user: str = Depends(admin_user),
    db: Session = Depends(get_db),
):
    q = select(Execution)
    if client_id:
        q = q.where(Execution.client_id == int(client_id))
    if status:
        q = q.where(Execution.status == status)
    if task:
        q = q.where((Execution.task_id == task) | Execution.task_name.ilike(f"%{task}%"))
    if initiator:
        q = q.where(func.lower(func.cast(Execution.initiator, Text())).like(f"%{initiator.lower()}%"))
    per = 100
    rows = db.scalars(q.order_by(Execution.id.desc()).offset((page - 1) * per).limit(per + 1)).all()
    return render(
        request,
        "executions.html",
        rows=rows[:per],
        has_next=len(rows) > per,
        page=page,
        clients=db.scalars(select(Client).order_by(Client.name)).all(),
        statuses=[*ExecStatus.ACTIVE, *ExecStatus.TERMINAL],
        f={"client_id": client_id, "status": status, "task": task, "initiator": initiator},
    )


@router.get("/executions/{execution_id}")
def execution_page(execution_id: int, request: Request, user: str = Depends(admin_user), db: Session = Depends(get_db)):
    ex = db.get(Execution, execution_id) or _404()
    events = db.scalars(select(AuditLog).where(AuditLog.execution_id == execution_id).order_by(AuditLog.id)).all()
    return render(request, "execution.html", ex=ex, events=events, active=ex.status in ExecStatus.ACTIVE)


@router.get("/executions/{execution_id}/log")
def execution_log_page(
    execution_id: int,
    request: Request,
    user: str = Depends(admin_user),
    db: Session = Depends(get_db),
    backend=Depends(get_backend),
):
    ex = db.get(Execution, execution_id) or _404()
    if not ex.script_log_ref:
        text = "Лог скрипта недоступен"
    else:
        try:
            text = backend.get_script_log(ex.task_id, ex.script_log_ref)
        except QlikError as e:
            text = f"Ошибка Qlik: {e}"
    admin_audit(db, request, "execution.log", execution_id=ex.id, task_id=ex.task_id)
    return render(request, "log.html", ex=ex, text=text)


@router.post("/executions/{execution_id}/cancel", dependencies=[Depends(check_csrf)])
def execution_cancel(
    execution_id: int,
    request: Request,
    user: str = Depends(admin_user),
    db: Session = Depends(get_db),
    backend=Depends(get_backend),
):
    ex = db.get(Execution, execution_id) or _404()
    try:
        svc.cancel(db, backend, ex, actor_type="admin", actor=user)
        flash(request, "Отмена отправлена")
    except (ServiceError, QlikError) as e:
        flash(request, str(e), "bad")
    return back(f"/ui/executions/{execution_id}")


# ------------------------------------------------------------------------------------------
# audit
# ------------------------------------------------------------------------------------------
@router.get("/audit")
def audit_page(
    request: Request,
    actor_type: str = "",
    actor: str = "",
    outcome: str = "",
    action: str = "",
    page: int = 1,
    user: str = Depends(admin_user),
    db: Session = Depends(get_db),
):
    q = select(AuditLog)
    if actor_type:
        q = q.where(AuditLog.actor_type == actor_type)
    if actor:
        q = q.where(AuditLog.actor.ilike(f"%{actor}%"))
    if outcome:
        q = q.where(AuditLog.outcome == outcome)
    if action:
        q = q.where(AuditLog.action.ilike(f"%{action}%"))
    per = 200
    rows = db.scalars(q.order_by(AuditLog.id.desc()).offset((page - 1) * per).limit(per + 1)).all()
    return render(
        request,
        "audit.html",
        rows=rows[:per],
        has_next=len(rows) > per,
        page=page,
        f={"actor_type": actor_type, "actor": actor, "outcome": outcome, "action": action},
    )


def _404():
    raise HTTPException(404, "Not found")
