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
