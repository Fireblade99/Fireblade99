from datetime import timedelta

from sqlalchemy import select

from qlik_gateway.db import session_scope
from qlik_gateway.models import AuditLog, Client, Execution, QlikTask, utcnow
from qlik_gateway.services.audit import audit_queue

from .conftest import DISABLED, HR, RISK, SALES


def _finish_all(mock):
    for e in mock.executions.values():
        e["start"] -= timedelta(seconds=10)


def test_full_flow_start_poll_success(http, coordinator, make_client, mock):
    _, h = make_client()
    r = http.post(
        f"/api/v1/tasks/{SALES}/start", headers={**h, "X-Airflow-Dag-Id": "dwh_daily", "X-Airflow-Run-Id": "r1"}
    )
    assert r.status_code == 202, r.text
    eid = r.json()["execution_id"]
    assert r.json()["status"] == "QUEUED"
    assert mock.calls == [("GET", "/qrs/reloadtask/full")]  # the API itself never calls Qlik

    coordinator.tick(force=True)
    assert http.get(f"/api/v1/executions/{eid}", headers=h).json()["status"] in ("STARTING", "RUNNING")
    _finish_all(mock)
    coordinator.tick(force=True)
    st = http.get(f"/api/v1/executions/{eid}", headers=h).json()
    assert st["status"] == "SUCCESS" and st["terminal"] and st["success"]

    d = http.get(f"/api/v1/executions/{eid}/details", headers=h).json()
    assert d["initiator"]["dag_id"] == "dwh_daily" and d["node"]
    assert http.get(f"/api/v1/executions/{eid}/log", headers=h).json()["log"]


def test_failed_reload_reports_error(http, coordinator, make_client, mock):
    _, h = make_client()
    eid = http.post(f"/api/v1/tasks/{RISK}/start", headers=h).json()["execution_id"]
    coordinator.tick(force=True)
    _finish_all(mock)
    coordinator.tick(force=True)
    st = http.get(f"/api/v1/executions/{eid}", headers=h).json()
    assert st["status"] == "FAILED" and "Script error" in st["error"]


def test_state_polling_does_not_hit_qlik(http, coordinator, make_client, mock):
    _, h = make_client()
    eid = http.post(f"/api/v1/tasks/{SALES}/start", headers=h).json()["execution_id"]
    coordinator.tick(force=True)
    before = len(mock.calls)
    for _ in range(20):
        http.get(f"/api/v1/executions/{eid}", headers=h)
    assert len(mock.calls) == before


def test_bulk_poll_one_request_for_many(http, coordinator, make_client, mock, settings):
    settings.max_concurrent_executions = 10
    _, h = make_client()
    for t in (SALES, HR, RISK):
        http.post(f"/api/v1/tasks/{t}/start", headers=h)
    coordinator.tick(force=True)
    mock.calls.clear()
    coordinator.tick(force=True)
    assert [c for c in mock.calls if "executionresult" in c[1]] == [("GET", "/qrs/executionresult/full")]


def test_dedupe_attaches_to_running(http, coordinator, make_client, mock):
    _, h1 = make_client("a")
    _, h2 = make_client("b")
    e1 = http.post(f"/api/v1/tasks/{SALES}/start", headers=h1).json()
    e2 = http.post(f"/api/v1/tasks/{SALES}/start", headers=h2).json()
    assert e2["execution_id"] == e1["execution_id"] and e2["deduplicated"]
    coordinator.tick(force=True)
    assert len([c for c in mock.calls if c[0] == "POST"]) == 1


def test_auth_and_permissions(http, coordinator, make_client):
    assert http.get("/api/v1/whoami").status_code == 401
    assert http.get("/api/v1/whoami", headers={"Authorization": "Bearer qgw_x_y"}).status_code == 401
    _, h = make_client("limited", tasks=(HR,), actions=("state",))
    assert http.post(f"/api/v1/tasks/{HR}/start", headers=h).status_code == 403  # no start action
    _, h2 = make_client("only-hr", tasks=(HR,))
    assert http.post(f"/api/v1/tasks/{SALES}/start", headers=h2).status_code == 403  # foreign task
    assert http.post(f"/api/v1/tasks/{DISABLED}/start", headers=h2).status_code == 403
    assert [t["id"] for t in http.get("/api/v1/tasks", headers=h2).json()] == [HR]


def test_blocking_revokes_immediately(http, coordinator, make_client):
    cid, h = make_client()
    assert http.get("/api/v1/whoami", headers=h).status_code == 200
    with session_scope() as db:
        c = db.get(Client, cid)
        c.enabled = False
        c.blocked_reason = "too many polls"
    r = http.get("/api/v1/whoami", headers=h)
    assert r.status_code == 403 and "too many polls" in r.json()["message"]


def test_blocked_client_queue_is_cancelled_by_worker(http, coordinator, make_client, mock):
    cid, h = make_client()
    eid = http.post(f"/api/v1/tasks/{SALES}/start", headers=h).json()["execution_id"]
    with session_scope() as db:
        db.get(Client, cid).enabled = False
    coordinator.tick(force=True)
    with session_scope() as db:
        assert db.get(Execution, eid).status == "CANCELLED"
    assert not [c for c in mock.calls if c[0] == "POST"]


def test_task_block_and_disabled(http, coordinator, make_client):
    _, h = make_client()
    with session_scope() as db:
        db.get(QlikTask, SALES).blocked = True
    assert http.post(f"/api/v1/tasks/{SALES}/start", headers=h).status_code == 423
    assert http.post(f"/api/v1/tasks/{DISABLED}/start", headers=h).status_code == 409


def test_rate_limit(http, coordinator, make_client):
    _, h = make_client(requests_per_minute=3)
    codes = [http.get("/api/v1/whoami", headers=h).status_code for _ in range(5)]
    assert codes == [200, 200, 200, 429, 429]


def test_starts_per_hour_limit(http, coordinator, make_client):
    _, h = make_client(starts_per_hour=1)
    assert http.post(f"/api/v1/tasks/{SALES}/start", headers=h).status_code == 202
    assert http.post(f"/api/v1/tasks/{HR}/start", headers=h).status_code == 429


def test_global_and_client_concurrency(http, coordinator, make_client, mock, settings):
    settings.max_concurrent_executions = 1
    _, h = make_client()
    a = http.post(f"/api/v1/tasks/{SALES}/start", headers=h).json()["execution_id"]
    b = http.post(f"/api/v1/tasks/{HR}/start", headers=h).json()["execution_id"]
    coordinator.tick(force=True)
    with session_scope() as db:
        assert db.get(Execution, a).status != "QUEUED"
        assert db.get(Execution, b).status == "QUEUED"
    _finish_all(mock)
    coordinator.tick(force=True)  # poll finishes a, dispatch happens before poll -> next tick starts b
    coordinator.tick(force=True)
    with session_scope() as db:
        assert db.get(Execution, b).status != "QUEUED"


def test_pause_dispatch(http, coordinator, make_client, mock):
    from qlik_gateway.services.kv import DISPATCH_PAUSED, set_value

    _, h = make_client()
    eid = http.post(f"/api/v1/tasks/{SALES}/start", headers=h).json()["execution_id"]
    with session_scope() as db:
        set_value(db, DISPATCH_PAUSED, {"by": "admin", "reason": "incident"})
    coordinator.tick(force=True)
    with session_scope() as db:
        assert db.get(Execution, eid).status == "QUEUED"
        set_value(db, DISPATCH_PAUSED, None)
    coordinator.tick(force=True)
    with session_scope() as db:
        assert db.get(Execution, eid).status != "QUEUED"


def test_cancel_queued_and_stop_running(http, coordinator, make_client, mock):
    _, h = make_client()
    e1 = http.post(f"/api/v1/tasks/{SALES}/start", headers=h).json()["execution_id"]
    assert http.post(f"/api/v1/executions/{e1}/cancel", headers=h).json()["status"] == "CANCELLED"
    e2 = http.post(f"/api/v1/tasks/{HR}/start", headers=h).json()["execution_id"]
    coordinator.tick(force=True)
    http.post(f"/api/v1/executions/{e2}/cancel", headers=h)
    assert ("POST", f"/qrs/task/{HR}/stop") in mock.calls
    coordinator.tick(force=True)
    assert http.get(f"/api/v1/executions/{e2}", headers=h).json()["status"] == "ABORTED"


def test_audit_records_who_and_initiator(http, coordinator, make_client):
    _, h = make_client("airflow-prod")
    http.post(f"/api/v1/tasks/{SALES}/start", headers={**h, "X-Airflow-Dag-Id": "sales", "User-Agent": "airflow/2.9"})
    http.get("/api/v1/whoami", headers={"Authorization": "Bearer bad"})
    audit_queue.flush()
    with session_scope() as db:
        rows = db.scalars(select(AuditLog).where(AuditLog.actor_type == "client").order_by(AuditLog.id)).all()
    start = rows[0]
    assert start.actor == "airflow-prod" and start.action == "api.start" and start.task_id == SALES
    assert start.meta["dag_id"] == "sales" and start.user_agent == "airflow/2.9" and start.execution_id
    assert rows[1].actor == "anonymous" and rows[1].outcome == "denied"


def test_lost_execution(http, coordinator, make_client, mock, settings):
    _, h = make_client()
    eid = http.post(f"/api/v1/tasks/{SALES}/start", headers=h).json()["execution_id"]
    coordinator.tick(force=True)
    mock.executions.clear()
    with session_scope() as db:
        db.get(Execution, eid).dispatched_at = utcnow() - timedelta(seconds=120)
    coordinator.tick(force=True)
    assert http.get(f"/api/v1/executions/{eid}", headers=h).json()["status"] == "LOST"


def test_long_poll_returns_terminal(http, coordinator, make_client, mock):
    _, h = make_client()
    eid = http.post(f"/api/v1/tasks/{SALES}/start", headers=h).json()["execution_id"]
    coordinator.tick(force=True)
    _finish_all(mock)
    coordinator.tick(force=True)
    assert http.get(f"/api/v1/executions/{eid}?wait=30", headers=h).json()["status"] == "SUCCESS"


def test_access_granted_via_qlik_custom_property(http, coordinator, make_client):
    # mock: SALES has GatewayClient=airflow-dwh, HR has airflow-dwh and platform-ml
    _, dwh = make_client("airflow-dwh", tasks=())
    _, ml = make_client("platform-ml", tasks=())
    assert http.post(f"/api/v1/tasks/{SALES}/start", headers=dwh).status_code == 202
    assert http.post(f"/api/v1/tasks/{SALES}/start", headers=ml).status_code == 403
    assert {t["id"] for t in http.get("/api/v1/tasks", headers=ml).json()} == {HR}
    assert {t["id"] for t in http.get("/api/v1/tasks", headers=dwh).json()} == {SALES, HR}
