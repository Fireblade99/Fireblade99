import re

from .conftest import HR, RISK, SALES


def _login(http):
    page = http.get("/ui/login").text
    csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    r = http.post(
        "/ui/login", data={"username": "admin", "password": "admin-pass", "csrf": csrf}, follow_redirects=False
    )
    assert r.status_code == 303 and r.headers["location"] == "/ui/"
    # the session (and its CSRF token) is rotated on login
    return re.search(r'name="csrf" value="([^"]+)"', http.get("/ui/settings").text).group(1)


def test_ui_requires_login(http):
    r = http.get("/ui/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/ui/login"


def test_ui_pages_and_client_lifecycle(http, coordinator, make_client):
    csrf = _login(http)
    r = http.post(
        "/ui/clients/new",
        data={
            "csrf": csrf,
            "name": "platform-team",
            "act_start": "1",
            "act_state": "1",
            "tasks": SALES,
            "requests_per_minute": "10",
            "starts_per_hour": "5",
            "max_concurrent": "1",
            "priority": "50",
        },
    )
    token = re.search(r"(qgw_[A-Za-z0-9_\-]+)", r.text).group(1)
    h = {"Authorization": f"Bearer {token}"}
    assert http.post(f"/api/v1/tasks/{SALES}/start", headers=h).status_code == 202
    coordinator.tick(force=True)

    for url in (
        "/ui/",
        "/ui/clients",
        "/ui/tasks",
        "/ui/executions",
        "/ui/executions/1",
        "/ui/audit",
        "/ui/clients/1",
        "/ui/executions?initiator=x&status=RUNNING",
    ):
        assert http.get(url).status_code == 200, url

    r = http.post("/ui/clients/1/block", data={"csrf": csrf, "reason": "incident", "cancel_queued": "1"})
    assert r.status_code == 200
    assert http.get("/api/v1/whoami", headers=h).status_code == 403
    assert "client.block" in http.get("/ui/audit?actor_type=admin").text

    r = http.post("/ui/clients/1/rotate", data={"csrf": csrf})
    new = re.search(r"(qgw_[A-Za-z0-9_\-]+)", r.text).group(1)
    assert new != token


def test_ui_csrf(http):
    _login(http)
    assert http.post("/ui/dispatch", data={"pause": "1", "csrf": "wrong"}).status_code == 400


def test_audit_filters_and_pagination(http, coordinator, make_client):
    from datetime import timedelta

    from qlik_gateway.db import session_scope
    from qlik_gateway.models import AuditLog, utcnow

    _login(http)
    now = utcnow()
    with session_scope() as db:
        for i in range(120):
            db.add(AuditLog(actor_type="client", actor="team-x", action="api.state", ts=now - timedelta(minutes=i)))
        db.add(
            AuditLog(actor_type="client", actor="old", action="api.start", ts=now - timedelta(days=3), ip="10.1.2.3")
        )

    page = http.get("/ui/audit?actor=team-x").text  # always 50 per page, no page size selector
    assert "Показано 1–50 из 120" in page and "page=3" in page and "Строк на странице" not in page
    last = http.get("/ui/audit?actor=team-x&per_page=20&page=3").text
    assert "Показано 101–120 из 120" in last
    day_ago = (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")
    recent = http.get(f"/ui/audit?date_from={day_ago}&actor_type=client").text
    assert "old" not in recent.split('<table class="sortable">')[1]
    two_days_ago = (now - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M")
    only_old = http.get(f"/ui/audit?date_to={two_days_ago}&actor_type=client").text
    assert "Показано 1–1 из 1" in only_old and "10.1.2.3" in only_old
    assert "Показано 1–1 из 1" in http.get("/ui/audit?ip=10.1.2").text
    assert http.get("/ui/audit?per_page=7&page=999&date_from=garbage").status_code == 200


def test_ui_time_zone(http, coordinator, settings, monkeypatch):
    from datetime import datetime

    from qlik_gateway.api import admin
    from qlik_gateway.db import session_scope
    from qlik_gateway.models import AuditLog

    monkeypatch.setattr(admin, "get_settings", lambda: settings)
    settings.ui_utc_offset_hours = 5
    _login(http)
    with session_scope() as db:
        db.add(AuditLog(actor_type="system", actor="tz-probe", action="x", ts=datetime(2026, 9, 29, 7, 0, 0)))
    page = http.get("/ui/audit?actor=tz-probe").text
    assert "29.09.2026 12:00:00" in page and "admin (UTC+5)" in page  # stored 07:00 UTC -> shown 12:00 UTC+5
    # the filter is typed in UTC+5: 11:59 local = 06:59 UTC -> includes the 07:00 UTC record
    assert "Показано 1–1 из 1" in http.get("/ui/audit?actor=tz-probe&date_from=2026-09-29T11:59").text
    assert "Нет записей" in http.get("/ui/audit?actor=tz-probe&date_from=2026-09-29T12:01").text


def test_settings_runtime_and_roles(http, coordinator, settings):
    from qlik_gateway.db import session_scope
    from qlik_gateway.models import AdminUser
    from qlik_gateway.security import hash_password
    from qlik_gateway.services import runtime

    csrf = _login(http)
    r = http.post(
        "/ui/settings/runtime",
        data={"csrf": csrf, "max_concurrent_executions": "7", "poll_interval_seconds": "45"},
    )
    assert r.status_code == 200 and "Сохранено" in r.text
    with session_scope() as db:
        rt = runtime.effective(db, settings)
        assert (rt.max_concurrent_executions, rt.poll_interval_seconds) == (7, 45)
    bad = http.post(
        "/ui/settings/runtime", data={"csrf": csrf, "max_concurrent_executions": "0", "poll_interval_seconds": "45"}
    )
    assert "Не сохранено" in bad.text
    http.post("/ui/dispatch", data={"csrf": csrf, "pause": "1", "reason": "test"})
    assert "dispatch: paused" in http.get("/ui/").text

    # a viewer sees pages but cannot change anything
    with session_scope() as db:
        db.add(AdminUser(username="viewer", password_hash=hash_password("v-pass"), role="viewer"))
    http.post("/ui/logout")
    page = http.get("/ui/login").text
    token = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    http.post("/ui/login", data={"username": "viewer", "password": "v-pass", "csrf": token})
    home = http.get("/ui/").text
    assert ">viewer<" in home and "/ui/settings" not in home
    assert http.get("/ui/settings").status_code == 403
    assert http.get("/ui/executions").status_code == 200
    csrf_v = re.search(r'name="csrf" value="([^"]+)"', http.get("/ui/login").text)
    token_v = csrf_v.group(1) if csrf_v else ""
    assert http.post("/ui/dispatch", data={"csrf": token_v, "pause": "0"}).status_code in (400, 403)
    tasks = http.get("/ui/tasks").text
    assert "Синхронизировать" not in tasks and f'href="/ui/tasks/{SALES}"' in tasks
    card = http.get(f"/ui/tasks/{SALES}").text  # a viewer opens the task card read-only
    assert "Режим просмотра" in card and "<fieldset" in card and "disabled" in card and "Сохранить" not in card


def test_formats_and_executions_page(http, coordinator, make_client):
    from qlik_gateway.api.admin import fmt_duration

    assert fmt_duration(3) == "00:00:03" and fmt_duration(3725) == "01:02:05" and fmt_duration(None) == "—"
    _, h = make_client()
    for t in (SALES, HR, RISK):
        http.post(f"/api/v1/tasks/{t}/start", headers=h)
    _login(http)
    page = http.get("/ui/executions").text
    assert "Показано 1–3 из 3" in page and "js-range" in page
    from qlik_gateway.api.admin import to_local
    from qlik_gateway.models import utcnow

    today = to_local(utcnow()).strftime("%Y-%m-%d")  # the UI filter is in the UI time zone
    assert "Показано 1–3 из 3" in http.get(f"/ui/executions?date_from={today}&date_to={today}").text
    assert "Нет записей" in http.get("/ui/executions?date_from=2020-01-01&date_to=2020-01-02").text
    edit = http.get(f"/ui/tasks/{SALES}/edit").text
    assert "Минимальный интервал" in edit
    assert http.get("/static/vendor/flatpickr.min.js").status_code == 200


def test_if_running_policy_set_in_ui(http, coordinator, make_client):
    from qlik_gateway.db import session_scope
    from qlik_gateway.models import Client, QlikTask

    csrf = _login(http)
    assert 'name="if_running_policy"' in http.get(f"/ui/tasks/{SALES}/edit").text
    r = http.post(
        f"/ui/tasks/{SALES}",
        data={"csrf": csrf, "min_interval_seconds": "0", "if_running_policy": "queue"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    cid, _ = make_client("team-x")
    page = http.get(f"/ui/clients/{cid}").text
    assert 'name="if_running_policy"' in page
    with session_scope() as db:
        assert db.get(QlikTask, SALES).if_running_policy == "queue"
    http.post(f"/ui/tasks/{SALES}", data={"csrf": csrf, "if_running_policy": "bogus"})
    with session_scope() as db:
        assert db.get(QlikTask, SALES).if_running_policy is None
        assert db.get(Client, cid).if_running_policy is None


def test_login_returns_to_the_requested_page(http):
    r = http.get("/ui/executions/31", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/ui/login?next=%2Fui%2Fexecutions%2F31"
    page = http.get(r.headers["location"]).text
    assert 'name="next" value="/ui/executions/31"' in page
    csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    r = http.post(
        "/ui/login",
        data={"username": "admin", "password": "admin-pass", "csrf": csrf, "next": "/ui/executions/31"},
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"] == "/ui/executions/31"

    from qlik_gateway.api.admin import safe_next

    for bad in ("https://evil.example/ui/", "//evil.example", "/ui//evil.example", "/ui/login", "", "/api/v1/tasks"):
        assert safe_next(bad) == "/ui/"


def test_execution_page_lists_every_start_request(http, coordinator, make_client, mock):
    from qlik_gateway.services.audit import audit_queue

    mock.min_duration = mock.max_duration = 60
    _, a = make_client("test-a")
    _, b = make_client("test-b")
    first = http.post(f"/api/v1/tasks/{SALES}/start", headers={**a, "X-Airflow-Dag-Id": "dag_a"}).json()
    http.post(f"/api/v1/tasks/{SALES}/start", headers=b)  # collapsed (still waiting in the gateway)
    coordinator.tick(force=True)
    http.post(f"/api/v1/tasks/{SALES}/start", headers=b, json={"on_active": "reuse"})
    assert http.post(f"/api/v1/tasks/{SALES}/start", headers=b, json={"meta": {"who": "B"}}).status_code == 409
    audit_queue.flush()
    _login(http)
    page = http.get(f"/ui/executions/{first['execution_id']}").text
    section = page[page.index('id="requests"') : page.index("Сообщения Qlik")]
    assert section.count("test-a") == 1 and section.count("test-b") == 3
    for label in (">created<", ">joined<", ">reused<", ">rejected<"):
        assert label in section
    assert "уже перезагружалась в Qlik" in section and "dag_a" in section and "who:" in section
    assert ">+2</a>" in http.get("/ui/executions").text  # collapsed + reused


def test_critical_actions_confirmation_and_notifications(http, coordinator, make_client, mock):
    from qlik_gateway.db import session_scope
    from qlik_gateway.models import Notification

    mock.min_duration = mock.max_duration = 60
    cid, h = make_client("team-n")
    http.post(f"/api/v1/tasks/{SALES}/start", headers=h)
    csrf = _login(http)
    settings_page = http.get("/ui/settings").text
    # the stop switch asks for STOP and shows what it affects
    assert 'data-confirm-word="STOP"' in settings_page and 'id="impact-dispatch"' in settings_page
    assert "в очереди шлюза: <b>1</b>" in settings_page and "team-n" in settings_page
    client_page = http.get(f"/ui/clients/{cid}").text
    assert 'data-confirm-word="team-n"' in client_page and 'id="impact-client"' in client_page

    http.post("/ui/dispatch", data={"csrf": csrf, "pause": "1", "reason": "incident"})
    http.post(f"/ui/clients/{cid}/block", data={"csrf": csrf, "reason": "leak", "cancel_queued": "1"})
    with session_scope() as db:
        rows = db.query(Notification).order_by(Notification.id).all()
        assert [n.action for n in rows] == ["dispatch.pause", "client.block"]
        assert rows[0].impact["queued"] == 1 and rows[0].impact["clients"] == {"team-n": 1}
        assert rows[1].impact["client"] == "team-n" and rows[1].impact["cancelled"] == 1
        assert rows[0].read_by == ["admin"]  # the author has seen it

    page = http.get("/ui/notifications").text
    assert "dispatch.pause" in page and "incident" in page and "leak" in page
    with session_scope() as db:
        db.add(Notification(actor="someone", action="dispatch.resume", title="x", read_by=["someone"]))
    assert (
        'href="/ui/notifications"' in http.get("/ui/").text
        and '<span class="badge bad">1</span>' in http.get("/ui/").text
    )
    http.post("/ui/notifications/read", data={"csrf": csrf})
    assert '<span class="badge bad">1</span>' not in http.get("/ui/").text
