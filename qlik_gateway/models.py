from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# Actions a client can be granted. They map 1:1 to what the gateway does on its own
# behalf in Qlik (post task, get state, get details, get info) plus log/stop.
ACTIONS = ("start", "state", "details", "info", "log", "stop")


class ExecStatus:
    QUEUED = "QUEUED"  # accepted by the gateway, waiting for a free slot
    STARTING = "STARTING"  # start sent to Qlik / Qlik has it triggered or queued
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    ABORTED = "ABORTED"
    SKIPPED = "SKIPPED"
    CANCELLED = "CANCELLED"  # cancelled in the gateway before it reached Qlik
    START_ERROR = "START_ERROR"  # Qlik rejected the start call
    LOST = "LOST"  # Qlik never reported on it
    TIMEOUT = "TIMEOUT"

    ACTIVE = (QUEUED, STARTING, RUNNING)
    IN_QLIK = (STARTING, RUNNING)
    TERMINAL = (SUCCESS, FAILED, ABORTED, SKIPPED, CANCELLED, START_ERROR, LOST, TIMEOUT)


class Client(Base):
    """An external system allowed to call the gateway (an Airflow instance, a platform team...)."""

    __tablename__ = "clients"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    description: Mapped[str] = mapped_column(Text, default="")
    owner_contact: Mapped[str] = mapped_column(String(200), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    blocked_reason: Mapped[str] = mapped_column(Text, default="")

    token_prefix: Mapped[str] = mapped_column(String(32), index=True)
    token_hash: Mapped[str] = mapped_column(String(128))
    token_created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    token_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # permissions
    allowed_actions: Mapped[list] = mapped_column(JSON, default=lambda: ["start", "state", "details", "info"])
    # list of Qlik task ids, or ["*"] for every task in the catalog
    allowed_tasks: Mapped[list] = mapped_column(JSON, default=list)
    # list of IPs / CIDRs, empty = any
    allowed_ips: Mapped[list] = mapped_column(JSON, default=list)

    # limits
    requests_per_minute: Mapped[int] = mapped_column(Integer, default=60)
    starts_per_hour: Mapped[int] = mapped_column(Integer, default=30)
    max_concurrent: Mapped[int] = mapped_column(Integer, default=2)
    priority: Mapped[int] = mapped_column(Integer, default=100)  # lower = dispatched first

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_seen_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)


class QlikTask(Base):
    """Catalog of reload tasks synced from QRS."""

    __tablename__ = "qlik_tasks"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(500), default="")
    app_id: Mapped[str] = mapped_column(String(64), default="")
    app_name: Mapped[str] = mapped_column(String(500), default="")
    stream_name: Mapped[str] = mapped_column(String(500), default="")
    enabled_in_qlik: Mapped[bool] = mapped_column(Boolean, default=True)
    custom_properties: Mapped[dict] = mapped_column(JSON, default=dict)
    tags: Mapped[list] = mapped_column(JSON, default=list)
    # gateway-side kill switch for a particular task
    blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    blocked_reason: Mapped[str] = mapped_column(Text, default="")
    min_interval_seconds: Mapped[int] = mapped_column(Integer, default=0)
    synced_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    present_in_qlik: Mapped[bool] = mapped_column(Boolean, default=True)


class Execution(Base):
    """One request to run a Qlik task, from acceptance to its final state."""

    __tablename__ = "executions"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"), index=True)
    task_id: Mapped[str] = mapped_column(String(64), index=True)
    task_name: Mapped[str] = mapped_column(String(500), default="")
    status: Mapped[str] = mapped_column(String(20), index=True, default=ExecStatus.QUEUED)
    priority: Mapped[int] = mapped_column(Integer, default=100)

    # who asked (Airflow dag/run/task, host, user...) - free form metadata from headers/body
    initiator: Mapped[dict] = mapped_column(JSON, default=dict)
    caller_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)

    qlik_execution_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    qlik_status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    qlik_status_text: Mapped[str | None] = mapped_column(String(40), nullable=True)
    node: Mapped[str | None] = mapped_column(String(200), nullable=True)
    details: Mapped[list] = mapped_column(JSON, default=list)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    script_log_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    qlik_started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    qlik_stopped_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    # other requests that were merged into this execution because the task was already queued/running
    dedup_hits: Mapped[int] = mapped_column(Integer, default=0)

    client: Mapped[Client] = relationship(lazy="joined")

    @property
    def duration_seconds(self) -> float | None:
        if self.qlik_started_at and self.qlik_stopped_at:
            return (self.qlik_stopped_at - self.qlik_started_at).total_seconds()
        if self.qlik_started_at and self.status in ExecStatus.ACTIVE:
            return (utcnow() - self.qlik_started_at).total_seconds()
        return None

    @property
    def wait_seconds(self) -> float | None:
        if self.dispatched_at:
            return (self.dispatched_at - self.created_at).total_seconds()
        return None


Index("ix_exec_task_status", Execution.task_id, Execution.status)


class AuditLog(Base):
    """Every call to the gateway (API and admin UI) and every call the gateway makes to Qlik."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    # "client" (API call), "admin" (UI action), "qlik" (outgoing call), "system" (worker)
    actor_type: Mapped[str] = mapped_column(String(10), index=True)
    actor: Mapped[str] = mapped_column(String(200), default="", index=True)
    client_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(100), index=True)
    method: Mapped[str | None] = mapped_column(String(10), nullable=True)
    path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    outcome: Mapped[str] = mapped_column(String(20), default="ok", index=True)  # ok / denied / error / limited
    latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(500), nullable=True)
    task_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    execution_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    message: Mapped[str] = mapped_column(Text, default="")


class NodeHealth(Base):
    __tablename__ = "node_health"

    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    node: Mapped[str] = mapped_column(String(200), index=True)
    ok: Mapped[bool] = mapped_column(Boolean, default=True)
    cpu_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    mem_committed_mb: Mapped[float | None] = mapped_column(Float, nullable=True)
    mem_free_mb: Mapped[float | None] = mapped_column(Float, nullable=True)
    apps_loaded: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sessions_active: Mapped[int | None] = mapped_column(Integer, nullable=True)
    saturated: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    raw: Mapped[dict] = mapped_column(JSON, default=dict)


class AdminUser(Base):
    __tablename__ = "admin_users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(100), unique=True)
    password_hash: Mapped[str] = mapped_column(String(300))
    # "admin" - everything; "viewer" - read-only (monitoring, executions, tasks, audit)
    role: Mapped[str] = mapped_column(String(20), default="admin", server_default="admin")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class KV(Base):
    """Small key/value store: global switches, worker lease, last sync times."""

    __tablename__ = "kv"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
