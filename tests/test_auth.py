"""UI login with Active Directory (the LDAP calls are replaced by a fake) and the "team" role."""

import re

import pytest

from qlik_gateway.db import session_scope
from qlik_gateway.models import AdminUser, Client
from qlik_gateway.services import ldap_auth
from qlik_gateway.services.audit import audit_queue

from .conftest import HR, SALES

AD = {  # login -> (password, groups)
    "ivanov": ("pw", ["QGW-Admins"]),
    "petrov": ("pw", ["Domain Users", "QGW-Viewers"]),
    "sidorov": ("pw", ["QGW-Team-DWH"]),
    "nobody": ("pw", ["Domain Users"]),
}


@pytest.fixture
def ad(monkeypatch, settings):
    settings.ldap_url = "ldaps://dc01.test:636"
    state = {"down": False}

    def fake(_settings, login, password):
        if state["down"]:
            raise ldap_auth.LdapUnavailable("dc01.test: connection refused")
        sam = ldap_auth.split_login(login)
        if sam not in AD or AD[sam][0] != password or not password:
            return None
        return ldap_auth.LdapUser(username=sam, display_name=sam.title(), groups=AD[sam][1])

    monkeypatch.setattr(ldap_auth, "authenticate", fake)
    monkeypatch.setattr("qlik_gateway.api.admin.get_settings", lambda: settings)
    return state


def login(http, username, password):
    page = http.get("/ui/login").text
    csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    return http.post(
        "/ui/login", data={"username": username, "password": password, "csrf": csrf}, follow_redirects=False
    )


def test_split_login_and_roles(settings):
    assert ldap_auth.split_login("HQ\\Ivanov") == ldap_auth.split_login("ivanov@hq.local") == "ivanov"
    dwh = Client(id=1, name="dwh", ui_groups=["QGW-Team-DWH"])
    ml = Client(id=2, name="ml", ui_groups=["qgw-team-ml", "QGW-Team-DWH"])
    assert ldap_auth.resolve_role(settings, ["qgw-admins"], [dwh]) == ("admin", [])
    assert ldap_auth.resolve_role(settings, ["QGW-Viewers", "QGW-Team-DWH"], [dwh]) == ("viewer", [])
    assert ldap_auth.resolve_role(settings, ["QGW-Team-DWH"], [dwh, ml]) == ("team", [1, 2])
    assert ldap_auth.resolve_role(settings, ["Domain Users"], [dwh, ml]) == (None, [])


def test_ad_login_roles_and_denials(http, ad):
    r = login(http, "HQ\\ivanov", "pw")
    assert r.status_code == 303 and r.headers["location"] == "/ui/"
    assert "Настройки" in http.get("/ui/").text and "Ivanov" in http.get("/ui/").text
    with session_scope() as db:
        u = db.query(AdminUser).filter_by(username="ivanov").one()
        assert u.source == "ad" and u.role == "admin" and u.password_hash == "" and u.last_login_at

    http.post("/ui/logout", data={})
    login(http, "petrov", "pw")
    assert http.get("/ui/settings").status_code == 403  # viewer

    http.cookies.clear()
    assert login(http, "ivanov", "wrong").headers["location"].startswith("/ui/login")
    assert login(http, "ivanov", "").status_code == 422  # an empty password never reaches AD (anonymous bind)
    assert http.get("/ui/", follow_redirects=False).status_code == 303
    r = login(http, "nobody", "pw")
    assert r.headers["location"].startswith("/ui/login")
    assert "ни в одну группу шлюза" in http.get(r.headers["location"]).text


def test_emergency_local_admin_when_ad_is_down(http, ad):
    ad["down"] = True
    r = login(http, "sidorov", "pw")
    assert "AD недоступен" in http.get(r.headers["location"]).text
    assert login(http, "admin", "admin-pass").headers["location"] == "/ui/"  # local, no AD needed
    audit_queue.flush()


def test_disabled_user_is_logged_out_at_once(http, ad):
    login(http, "petrov", "pw")
    assert http.get("/ui/", follow_redirects=False).status_code == 200
    with session_scope() as db:
        db.query(AdminUser).filter_by(username="petrov").one().enabled = False
    assert http.get("/ui/", follow_redirects=False).status_code == 303
    assert "отключена" in http.get(login(http, "petrov", "pw").headers["location"]).text


def test_team_sees_only_its_clients(http, coordinator, make_client, mock, ad):
    mock.min_duration = mock.max_duration = 60
    dwh_id, dwh = make_client("dwh", tasks=(HR, SALES), ui_groups=["QGW-Team-DWH"])
    ml_id, ml = make_client("ml")
    own = http.post(f"/api/v1/tasks/{HR}/start", headers=dwh).json()["execution_id"]
    foreign = http.post(f"/api/v1/tasks/{SALES}/start", headers=ml).json()["execution_id"]
    coordinator.tick(force=True)  # both reloading
    assert http.post(f"/api/v1/tasks/{SALES}/start", headers=dwh).status_code == 409  # refused by ml's run
    audit_queue.flush()

    login(http, "sidorov", "pw")
    page = http.get("/ui/").text
    assert "команда" in page
    assert "/ui/clients/settings" not in page and http.get("/ui/settings").status_code == 403

    rows = http.get("/ui/executions").text
    assert f">#{own}<" in rows and f">#{foreign}<" not in rows
    assert ">ml<" not in http.get("/ui/clients").text
    assert http.get(f"/ui/clients/{ml_id}").status_code == 404
    assert http.get(f"/ui/clients/{dwh_id}").status_code == 200

    # ml's run: visible only because dwh's request was refused by it; ml's details stay hidden
    other = http.get(f"/ui/executions/{foreign}").text
    assert "запуск другой команды" in other and "отказ 409" in other and ">ml<" not in other
    tasks = http.get("/ui/tasks").text
    assert "Reload HR Dashboard" in tasks and "Reload Risk" not in tasks

    audit = http.get("/ui/audit").text
    assert "dwh" in audit and ">ml<" not in audit and "client: ml" not in audit

    # cancel: own run nobody joined - yes; another team's run - no
    csrf = re.search(r'name="csrf" value="([^"]+)"', http.get(f"/ui/executions/{own}").text).group(1)
    assert http.post(f"/ui/executions/{foreign}/cancel", data={"csrf": csrf}).status_code == 403
    r = http.post(f"/ui/executions/{own}/cancel", data={"csrf": csrf}, follow_redirects=False)
    assert r.status_code == 303


def test_ldap_bind_and_groups_against_mock_directory(monkeypatch, settings):
    """The real ldap3 code path against ldap3's in-memory directory (AD schema)."""
    import ldap3

    real = ldap3.Connection
    user_dn = "CN=Ivan Ivanov,OU=Users,DC=hq,DC=local"

    def mock_connection(server, user=None, password=None, **kw):
        c = real(
            ldap3.Server("dc", get_info=ldap3.OFFLINE_AD_2012_R2),
            user={"HQ\\ivanov": user_dn}.get(user, user),  # AD maps DOMAIN\\login to the DN itself
            password=password,
            client_strategy=ldap3.MOCK_SYNC,
        )
        c.strategy.add_entry(
            user_dn,
            {
                "objectClass": ["top", "person", "organizationalPerson", "user"],
                "sAMAccountName": "ivanov",
                "displayName": "Иван Иванов",
                "userPassword": "Secret1",
                "memberOf": ["CN=QGW-Admins,OU=Groups,DC=hq,DC=local"],
            },
        )
        return c

    monkeypatch.setattr(ldap3, "Connection", mock_connection)
    settings.ldap_url, settings.ldap_domain, settings.ldap_base_dn = "ldaps://dc01:636", "HQ", "DC=hq,DC=local"
    u = ldap_auth.authenticate(settings, "HQ\\Ivanov", "Secret1")
    assert u.username == "ivanov" and u.display_name == "Иван Иванов" and u.groups == ["QGW-Admins"]
    assert ldap_auth.authenticate(settings, "ivanov", "wrong") is None
    assert ldap_auth.authenticate(settings, "ivanov", "") is None
