import logging
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import select, text
from starlette.middleware.sessions import SessionMiddleware

from .api import admin, v1
from .api.deps import client_ip, initiator_meta
from .config import Settings, get_settings
from .db import get_engine, init_engine, session_scope
from .models import AdminUser
from .qlik import make_backend
from .security import hash_password
from .services.audit import audit_queue, qlik_call_hook
from .services.errors import ServiceError
from .services.metrics import API_REQUESTS

log = logging.getLogger("qlik_gateway")


class UTF8JSONResponse(JSONResponse):
    # explicit charset: Windows PowerShell 5.1 otherwise decodes UTF-8 bodies as Latin-1
    media_type = "application/json; charset=utf-8"


def _bootstrap_admin(settings: Settings) -> None:
    if not (settings.bootstrap_admin_user and settings.bootstrap_admin_password):
        return
    with session_scope() as db:
        if db.scalars(select(AdminUser)).first() is None:
            db.add(
                AdminUser(
                    username=settings.bootstrap_admin_user,
                    password_hash=hash_password(settings.bootstrap_admin_password),
                )
            )
            log.info("bootstrap admin '%s' created", settings.bootstrap_admin_user)


def create_app(settings: Settings | None = None, backend=None) -> FastAPI:
    settings = settings or get_settings()
    init_engine(settings.database_url)
    _bootstrap_admin(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        worker = None
        if settings.embedded_worker:
            from .worker import Coordinator

            worker = Coordinator(settings, app.state.backend)
            threading.Thread(target=worker.run_forever, name="coordinator", daemon=True).start()
        yield
        if worker:
            worker.stop()
        audit_queue.flush()
        app.state.backend.close()

    app = FastAPI(
        default_response_class=UTF8JSONResponse,
        title="Qlik Gateway",
        version="0.1.0",
        description="Buffer service between external schedulers (Airflow, platform teams) and Qlik Sense.",
        lifespan=lifespan,
    )
    app.state.backend = backend or make_backend(settings, qlik_call_hook)

    @app.middleware("http")
    async def audit_api_calls(request: Request, call_next):
        """Every call to /api is recorded: who, from where, what, result, latency, initiator metadata."""
        if not request.url.path.startswith("/api/"):
            return await call_next(request)
        t0 = time.perf_counter()
        response = await call_next(request)
        latency = (time.perf_counter() - t0) * 1000
        client = getattr(request.state, "client", None) or {}
        client_name = client.get("name", "anonymous")
        extra = getattr(request.state, "audit", None) or {}
        status = response.status_code
        outcome = (
            "ok" if status < 400 else "limited" if status == 429 else "denied" if status in (401, 403, 423) else "error"
        )
        action = extra.get("action") or request.url.path.removeprefix("/api/v1/").split("/")[0]
        audit_queue.put(
            actor_type="client",
            actor=client_name,
            client_id=client.get("id"),
            action=f"api.{action}",
            method=request.method,
            path=str(request.url.path)[:500],
            status_code=status,
            outcome=outcome,
            latency_ms=latency,
            ip=client_ip(request),
            user_agent=(request.headers.get("user-agent") or "")[:500],
            task_id=extra.get("task_id"),
            execution_id=extra.get("execution_id"),
            meta=initiator_meta(request),
            message=extra.get("message") or extra.get("error") or "",
        )
        API_REQUESTS.labels(client_name, action, outcome).inc()
        return response

    @app.exception_handler(ServiceError)
    async def service_error(request: Request, exc: ServiceError):
        if isinstance(getattr(request.state, "audit", None), dict):
            request.state.audit["error"] = f"{exc.code}: {exc.message}"
        return UTF8JSONResponse({"error": exc.code, "message": exc.message}, status_code=exc.status_code)

    @app.exception_handler(admin.NotLoggedIn)
    async def not_logged_in(request: Request, exc):
        return RedirectResponse("/ui/login", status_code=303)

    # SessionMiddleware must wrap the audit middleware so the UI has request.session
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        session_cookie="qgw_admin",
        same_site="strict",
        https_only=settings.session_https_only,
        max_age=8 * 3600,
    )

    app.include_router(v1.router)
    app.include_router(admin.router)
    app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")

    @app.get("/", include_in_schema=False)
    def root():
        return RedirectResponse("/ui/")

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        with get_engine().connect() as c:
            c.execute(text("select 1"))
        return {"status": "ok", "qlik_mode": settings.qlik_mode}

    @app.get("/metrics", include_in_schema=False)
    def metrics():
        return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return app


def app_factory() -> FastAPI:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    return create_app()
