import re

from .conftest import SALES


def _login(http):
    page = http.get("/ui/login").text
    csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    r = http.post(
        "/ui/login", data={"username": "admin", "password": "admin-pass", "csrf": csrf}, follow_redirects=False
    )
    assert r.status_code == 303 and r.headers["location"] == "/ui/"
    # the session (and its CSRF token) is rotated on login
    return re.search(r'name="csrf" value="([^"]+)"', http.get("/ui/").text).group(1)


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
        for i in range(45):
            db.add(AuditLog(actor_type="client", actor="team-x", action="api.state", ts=now - timedelta(minutes=i)))
        db.add(
            AuditLog(actor_type="client", actor="old", action="api.start", ts=now - timedelta(days=3), ip="10.1.2.3")
        )

    page = http.get("/ui/audit?actor=team-x&per_page=20").text
    assert "Показано 1–20 из 45" in page and "page=3" in page
    last = http.get("/ui/audit?actor=team-x&per_page=20&page=3").text
    assert "Показано 41–45 из 45" in last
    day_ago = (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")
    assert "old" not in http.get(f"/ui/audit?date_from={day_ago}&actor_type=client").text.split("<table>")[1]
    two_days_ago = (now - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M")
    only_old = http.get(f"/ui/audit?date_to={two_days_ago}&actor_type=client").text
    assert "Показано 1–1 из 1" in only_old and "10.1.2.3" in only_old
    assert "Показано 1–1 из 1" in http.get("/ui/audit?ip=10.1.2").text
    assert http.get("/ui/audit?per_page=7&page=999&date_from=garbage").status_code == 200
