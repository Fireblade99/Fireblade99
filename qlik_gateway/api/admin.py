"""Admin UI: monitoring, clients (tokens, rights, limits, blocking), tasks, executions, audit."""

import secrets
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import Text, case, func, select
from sqlalchemy.orm import Session

from .. import __version__
from ..config import get_settings
from ..db import get_db
from ..models import ACTIONS, AdminUser, AuditLog, Client, ExecStatus, Execution, NodeHealth, QlikTask, utcnow
from ..qlik import QlikError
from ..security import generate_client_token, verify_password
from ..services import executions as svc
from ..services import runtime
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


def ui_offset() -> timedelta:
    return timedelta(hours=get_settings().ui_utc_offset_hours)


def to_local(d: datetime | None) -> datetime | None:
    """UTC (as stored) -> UI time zone."""
    return d + ui_offset() if d else None


def from_local(d: datetime | None) -> datetime | None:
    """UI time zone (as typed by an admin) -> UTC."""
    return d - ui_offset() if d else None


def tz_label() -> str:
    h = get_settings().ui_utc_offset_hours
    if not h:
        return "UTC"
    sign = "+" if h > 0 else "-"
    hours, minutes = divmod(round(abs(h) * 60), 60)
    return f"UTC{sign}{hours}" + (f":{minutes:02d}" if minutes else "")


templates.env.filters["dt"] = lambda d: to_local(d).strftime("%d.%m.%Y %H:%M:%S") if d else "—"
templates.env.filters["dt_input"] = lambda d: to_local(d).strftime("%Y-%m-%dT%H:%M") if d else ""
templates.env.globals["tz_label"] = tz_label
templates.env.globals["app_version"] = __version__  # cache-busting for static files
templates.env.filters["iso_dt"] = lambda s: templates.env.filters["dt"](datetime.fromisoformat(s)) if s else "—"


def fmt_duration(seconds: float | None) -> str:
    """3 -> 00:00:03, 3725 -> 01:02:05 (days are folded into hours)."""
    if seconds is None:
        return "—"
    total = max(0, int(round(seconds)))
    h, rest = divmod(total, 3600)
    m, s = divmod(rest, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


templates.env.filters["dur"] = fmt_duration


class NotLoggedIn(Exception):
    pass


def admin_user(request: Request) -> str:
    user = request.session.get("admin")
    if not user or not request.session.get("role"):  # sessions from before roles existed: log in again
        raise NotLoggedIn()
    return user


def is_admin(request: Request) -> bool:
    return request.session.get("role") == "admin"


def require_admin(request: Request, user: str = Depends(admin_user)) -> str:
    """Actions that change anything; a viewer only looks."""
    if not is_admin(request):
        raise HTTPException(403, "Недостаточно прав: действие доступно только администратору")
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
    ctx["is_admin"] = is_admin(request)
    ctx["role"] = request.session.get("role")
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
    request.session["role"] = user.role or "admin"
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
    ok_expr = func.sum(case((Execution.status == ExecStatus.SUCCESS, 1), else_=0))
    starts = {
        r.client_id: r
        for r in db.execute(
            select(
                Execution.client_id,
                func.count(Execution.id).label("starts"),
                ok_expr.label("ok"),
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
                "ok": (s.ok or 0) if s else 0,
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
    durations: dict[str, list[float]] = {}
    per_node: dict[str, dict] = {}
    for ex in db.scalars(select(Execution).where(Execution.created_at >= day, Execution.node.is_not(None))):
        n = per_node.setdefault(ex.node, {"runs": 0, "ok": 0, "fails": 0, "busy": 0.0, "running": 0})
        n["runs"] += 1
        if ex.status == ExecStatus.SUCCESS:
            n["ok"] += 1
        elif ex.status in ExecStatus.ACTIVE:
            n["running"] += 1
        else:
            n["fails"] += 1
        if ex.qlik_stopped_at:
            durations.setdefault(ex.task_id, []).append(ex.duration_seconds or 0)
            n["busy"] += ex.duration_seconds or 0

    qlik_calls_hour = db.scalar(
        select(func.count(AuditLog.id)).where(AuditLog.actor_type == "qlik", AuditLog.ts >= hour)
    )
    qlik_errors_hour = db.scalar(
        select(func.count(AuditLog.id)).where(
            AuditLog.actor_type == "qlik", AuditLog.ts >= hour, AuditLog.outcome != "ok"
        )
    )

    latest_ids = select(func.max(NodeHealth.id)).group_by(NodeHealth.node)
    health = {h.node: h for h in db.scalars(select(NodeHealth).where(NodeHealth.id.in_(latest_ids)))}
    # one row per node: executions in 24h (from our history) + last engine health snapshot, if configured
    nodes = [
        {"node": name, "stats": per_node.get(name), "health": health.get(name)}
        for name in sorted(set(per_node) | set(health), key=lambda x: x.lower())
    ]

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
        rt=runtime.effective(db, get_settings()),
    )


# ------------------------------------------------------------------------------------------
# settings (admin only)
# ------------------------------------------------------------------------------------------
@router.get("/settings")
def settings_page(request: Request, user: str = Depends(require_admin), db: Session = Depends(get_db)):
    s = get_settings()
    return render(
        request,
        "settings.html",
        paused=dispatch_paused(db),
        rt=runtime.effective(db, s),
        env={
            "max_concurrent_executions": s.max_concurrent_executions,
            "poll_interval_seconds": s.poll_interval_seconds,
        },
        limits=runtime.LIMITS,
        users=db.scalars(select(AdminUser).order_by(AdminUser.username)).all(),
    )


@router.post("/dispatch", dependencies=[Depends(check_csrf)])
def toggle_dispatch(
    request: Request,
    pause: str = Form(...),
    reason: str = Form(""),
    user: str = Depends(require_admin),
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
    return back("/ui/settings")


@router.post("/settings/runtime", dependencies=[Depends(check_csrf)])
def save_runtime(
    request: Request,
    max_concurrent_executions: str = Form(...),
    poll_interval_seconds: str = Form(...),
    reset: str = Form(""),
    user: str = Depends(require_admin),
    db: Session = Depends(get_db),
):
    before = runtime.effective(db, get_settings())
    if reset:
        runtime.reset(db)
        admin_audit(db, request, "settings.reset")
        flash(request, "Параметры сброшены к значениям из .env")
        return back("/ui/settings")
    try:
        saved = runtime.save(
            db,
            {"max_concurrent_executions": max_concurrent_executions, "poll_interval_seconds": poll_interval_seconds},
        )
    except ValueError as e:
        flash(request, f"Не сохранено: {e}", "bad")
        return back("/ui/settings")
    admin_audit(
        db,
        request,
        "settings.update",
        meta={
            "before": {
                "max_concurrent_executions": before.max_concurrent_executions,
                "poll_interval_seconds": before.poll_interval_seconds,
            },
            "after": saved,
        },
    )
    flash(request, "Сохранено. Координатор применит параметры на следующем такте (в течение нескольких секунд).")
    return back("/ui/settings")


# ------------------------------------------------------------------------------------------
# clients
# ------------------------------------------------------------------------------------------
def _parse_list(raw: str) -> list[str]:
    return [x.strip() for x in raw.replace(",", "\n").splitlines() if x.strip()]


@router.get("/clients")
def clients_page(request: Request, user: str = Depends(admin_user), db: Session = Depends(get_db)):
    return render(request, "clients.html", clients=db.scalars(select(Client).order_by(Client.name)).all())


@router.get("/clients/new")
def client_new_page(request: Request, user: str = Depends(require_admin), db: Session = Depends(get_db)):
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
    c.token_expires_at = from_local(datetime.fromisoformat(exp)) if exp else None


@router.post("/clients/new", dependencies=[Depends(check_csrf)])
async def client_create(request: Request, user: str = Depends(require_admin), db: Session = Depends(get_db)):
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
    client_id: int, request: Request, user: str = Depends(require_admin), db: Session = Depends(get_db)
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
    user: str = Depends(require_admin),
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
def client_unblock(client_id: int, request: Request, user: str = Depends(require_admin), db: Session = Depends(get_db)):
    c = db.get(Client, client_id) or _404()
    c.enabled = True
    c.blocked_reason = ""
    admin_audit(db, request, "client.unblock", client_id=c.id)
    flash(request, "Клиент разблокирован")
    return back(f"/ui/clients/{client_id}")


@router.post("/clients/{client_id}/rotate", dependencies=[Depends(check_csrf)])
def client_rotate(client_id: int, request: Request, user: str = Depends(require_admin), db: Session = Depends(get_db)):
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
    request: Request, user: str = Depends(require_admin), db: Session = Depends(get_db), backend=Depends(get_backend)
):
    try:
        n = svc.sync_catalog(db, backend)
        flash(request, f"Каталог обновлён из Qlik: {n} задач")
        admin_audit(db, request, "tasks.sync", message=str(n))
    except QlikError as e:
        flash(request, f"Ошибка Qlik: {e}", "bad")
    return back("/ui/tasks")


@router.get("/tasks/{task_id}/edit")
def task_edit_page(task_id: str, request: Request, user: str = Depends(require_admin), db: Session = Depends(get_db)):
    t = db.get(QlikTask, task_id) or _404()
    last_runs = db.scalars(
        select(Execution).where(Execution.task_id == task_id).order_by(Execution.id.desc()).limit(10)
    ).all()
    clients = db.scalars(select(Client)).all()
    return render(
        request,
        "task_edit.html",
        t=t,
        last_runs=last_runs,
        users=[c.name for c in clients if svc.client_can_task(c, t.id, t)],
    )


@router.post("/tasks/{task_id}", dependencies=[Depends(check_csrf)])
def task_update(
    task_id: str,
    request: Request,
    blocked: str = Form(""),
    blocked_reason: str = Form(""),
    min_interval_seconds: int = Form(0),
    user: str = Depends(require_admin),
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
    flash(request, f"Задача «{t.name}» сохранена")
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
    date_from: str = "",
    date_to: str = "",
    per_page: int = 50,
    page: int = 1,
    user: str = Depends(admin_user),
    db: Session = Depends(get_db),
):
    conds = []
    if client_id.isdigit():
        conds.append(Execution.client_id == int(client_id))
    if status:
        conds.append(Execution.status == status)
    if task:
        t = task.strip().lstrip("#")
        conds.append(
            (Execution.id == int(t))
            if t.isdigit()
            else (Execution.task_id.ilike(f"{t}%") | Execution.task_name.ilike(f"%{t}%"))
        )
    if initiator:
        conds.append(func.lower(func.cast(Execution.initiator, Text())).like(f"%{initiator.lower()}%"))
    conds += _date_conds(Execution.created_at, date_from, date_to)

    f = {
        "client_id": client_id,
        "status": status,
        "task": task,
        "initiator": initiator,
        "date_from": date_from,
        "date_to": date_to,
        "per_page": per_page,
    }
    page_ctx = _paginate(db, Execution, conds, f, page, per_page)
    return render(
        request,
        "executions.html",
        clients=db.scalars(select(Client).order_by(Client.name)).all(),
        statuses=[*ExecStatus.ACTIVE, *ExecStatus.TERMINAL],
        **page_ctx,
    )


def _date_conds(column, date_from: str, date_to: str) -> list:
    """Date range typed in the UI time zone. A bare date in date_to means the whole day."""
    conds = []
    dt_from = from_local(_parse_dt(date_from))
    if dt_from:
        conds.append(column >= dt_from)
    dt_to = from_local(_parse_dt(date_to))
    if dt_to:
        whole_day = len(date_to.strip()) == 10
        conds.append(column < dt_to + (timedelta(days=1) if whole_day else timedelta(minutes=1)))
    return conds


def _paginate(db: Session, model, conds: list, f: dict, page: int, per_page: int) -> dict:
    per_page = per_page if per_page in PAGE_SIZES else 50
    f["per_page"] = per_page
    total = db.scalar(select(func.count(model.id)).where(*conds))
    pages = max(1, -(-total // per_page))
    page = min(max(1, page), pages)
    rows = db.scalars(
        select(model).where(*conds).order_by(model.id.desc()).offset((page - 1) * per_page).limit(per_page)
    ).all()
    now = to_local(utcnow())
    keep = {k: v for k, v in f.items() if v and k not in ("date_from", "date_to")}
    presets = [
        (label, urlencode({**keep, "date_from": _fmt_dt(now - delta)}))
        for label, delta in (
            ("15 минут", timedelta(minutes=15)),
            ("час", timedelta(hours=1)),
            ("сутки", timedelta(days=1)),
            ("неделя", timedelta(days=7)),
        )
    ]
    return {
        "rows": rows,
        "f": f,
        "total": total,
        "page": page,
        "pages": pages,
        "per_page": per_page,
        "page_sizes": PAGE_SIZES,
        "qs": urlencode({k: v for k, v in f.items() if v}),
        "qs_base": urlencode({k: v for k, v in f.items() if v and k != "per_page"}),
        "presets": presets,
        "first_row": (page - 1) * per_page + 1 if total else 0,
        "last_row": min(page * per_page, total),
    }


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
    user: str = Depends(require_admin),
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
    ip: str = "",
    target: str = "",
    date_from: str = "",
    date_to: str = "",
    per_page: int = 50,
    page: int = 1,
    user: str = Depends(admin_user),
    db: Session = Depends(get_db),
):
    conds = []
    if actor_type:
        conds.append(AuditLog.actor_type == actor_type)
    if actor:
        conds.append(AuditLog.actor.ilike(f"%{actor}%"))
    if outcome:
        conds.append(AuditLog.outcome == outcome)
    if action:
        conds.append(AuditLog.action.ilike(f"%{action}%"))
    if ip:
        conds.append(AuditLog.ip.ilike(f"%{ip}%"))
    if target:  # task id (or its beginning) or execution number
        t = target.strip().lstrip("#")
        conds.append((AuditLog.execution_id == int(t)) if t.isdigit() else AuditLog.task_id.ilike(f"{t}%"))
    conds += _date_conds(AuditLog.ts, date_from, date_to)
    f = {
        "actor_type": actor_type,
        "actor": actor,
        "outcome": outcome,
        "action": action,
        "ip": ip,
        "target": target,
        "date_from": date_from,
        "date_to": date_to,
        "per_page": per_page,
    }
    return render(request, "audit.html", **_paginate(db, AuditLog, conds, f, page, per_page))


PAGE_SIZES = (20, 50, 100)


def _parse_dt(value: str) -> datetime | None:
    """'YYYY-MM-DD' or 'YYYY-MM-DDTHH:MM' in the UI time zone (converted by the caller)."""
    try:
        return datetime.fromisoformat(value.strip()) if value.strip() else None
    except ValueError:
        return None


def _fmt_dt(d: datetime) -> str:
    return d.strftime("%Y-%m-%dT%H:%M")


def _404():
    raise HTTPException(404, "Not found")
