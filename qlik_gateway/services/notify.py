"""Critical actions (stops, blocks, cancels, token rotation): the impact they have, computed both
for the confirmation dialog before the action and for the notification stored after it."""

from collections import Counter

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import AuditLog, Client, ExecStatus, Execution, Notification, QlikTask
from . import executions as svc


def _active(db: Session, *conds) -> list[Execution]:
    return db.scalars(select(Execution).where(Execution.status.in_(ExecStatus.ACTIVE), *conds)).all()


def _waiting_clients(db: Session, ex: Execution) -> list[str]:
    """Clients whose start requests ended up on this run (created it, joined it, reused it)."""
    ids = set(
        db.scalars(
            select(AuditLog.client_id).where(
                AuditLog.execution_id == ex.id, AuditLog.action == "api.start", AuditLog.outcome == "ok"
            )
        )
    ) | {ex.client_id}
    return sorted(c.name for c in db.scalars(select(Client).where(Client.id.in_(ids))))


def dispatch_impact(db: Session) -> dict:
    active = _active(db)
    queued = [e for e in active if e.status == ExecStatus.QUEUED]
    by_client = Counter(e.client.name for e in queued)
    return {
        "queued": len(queued),
        "in_qlik": len(active) - len(queued),
        "tasks": len({e.task_id for e in queued}),
        "clients": dict(by_client.most_common()),
    }


def client_impact(db: Session, client: Client) -> dict:
    active = _active(db, Execution.client_id == client.id)
    return {
        "client": client.name,
        "queued": sum(e.status == ExecStatus.QUEUED for e in active),
        "in_qlik": sum(e.status in ExecStatus.IN_QLIK for e in active),
        "shared": sum(1 for e in active if e.dedup_hits),  # runs other requests are waiting for too
        "tasks": len(svc.visible_tasks(db, client)),
    }


def task_impact(db: Session, task: QlikTask) -> dict:
    active = _active(db, Execution.task_id == task.id)
    return {
        "task": task.name,
        "queued": sum(e.status == ExecStatus.QUEUED for e in active),
        "in_qlik": sum(e.status in ExecStatus.IN_QLIK for e in active),
        "clients": sorted(c.name for c in db.scalars(select(Client)) if svc.client_can_task(c, task.id, task)),
    }


def execution_impact(db: Session, ex: Execution) -> dict:
    return {
        "execution": ex.id,
        "task": ex.task_name,
        "status": ex.status,
        "requests": 1 + (ex.dedup_hits or 0),
        "clients": _waiting_clients(db, ex),
    }


def notify(
    db: Session,
    *,
    actor: str,
    action: str,
    title: str,
    reason: str = "",
    impact: dict | None = None,
    link: str = "",
    severity: str = "warn",
) -> Notification:
    n = Notification(
        actor=actor,
        action=action,
        title=title,
        reason=reason or "",
        impact=impact or {},
        link=link,
        severity=severity,
        read_by=[actor],  # the author has seen it
    )
    db.add(n)
    return n


def unread_count(db: Session, username: str) -> int:
    rows = db.scalars(select(Notification.read_by).order_by(Notification.id.desc()).limit(200))
    return sum(1 for r in rows if username not in (r or []))
