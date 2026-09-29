import os

import pytest
from fastapi.testclient import TestClient

from qlik_gateway.config import Settings
from qlik_gateway.db import session_scope
from qlik_gateway.main import create_app
from qlik_gateway.models import Client
from qlik_gateway.qlik.mock import reset_mock
from qlik_gateway.security import generate_client_token
from qlik_gateway.services.ratelimit import limiter
from qlik_gateway.worker import Coordinator

SALES = "11111111-1111-1111-1111-111111111111"
HR = "22222222-2222-2222-2222-222222222222"
RISK = "44444444-4444-4444-4444-444444444444"
DISABLED = "55555555-5555-5555-5555-555555555555"


@pytest.fixture
def settings(tmp_path):
    # QGW_TEST_DATABASE_URL=postgresql+psycopg://... runs the suite against PostgreSQL
    url = os.environ.get("QGW_TEST_DATABASE_URL")
    if url:
        from sqlalchemy import create_engine

        from qlik_gateway import models  # noqa: F401
        from qlik_gateway.db import Base

        eng = create_engine(url)
        Base.metadata.drop_all(eng)
        eng.dispose()
    return Settings(
        database_url=url or f"sqlite:///{tmp_path}/test.db",
        qlik_mode="mock",
        secret_key="test",
        bootstrap_admin_user="admin",
        bootstrap_admin_password="admin-pass",
        max_concurrent_executions=2,
        poll_interval_seconds=0,
        catalog_sync_interval_seconds=0,
        lost_after_seconds=60,
    )


@pytest.fixture
def mock():
    return reset_mock(min_duration=0, max_duration=0)


@pytest.fixture
def app(settings, mock):
    limiter.reset()
    return create_app(settings, backend=mock)


@pytest.fixture
def http(app):
    with TestClient(app) as c:
        yield c


@pytest.fixture
def coordinator(settings, mock, app):
    c = Coordinator(settings, mock)
    c.tick(force=True)  # sync catalog
    return c


@pytest.fixture
def make_client():
    def _make(name="airflow", tasks=("*",), actions=("start", "state", "details", "info", "log", "stop"), **kw):
        token, prefix, h = generate_client_token()
        with session_scope() as db:
            c = Client(
                name=name,
                token_prefix=prefix,
                token_hash=h,
                allowed_tasks=list(tasks),
                allowed_actions=list(actions),
                **kw,
            )
            db.add(c)
            db.flush()
            cid = c.id
        return cid, {"Authorization": f"Bearer {token}"}

    return _make
