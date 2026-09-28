import json

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from qlik_gateway.config import Settings
from qlik_gateway.qlik.qrs import QrsJwtClient

TASK = "11111111-1111-1111-1111-111111111111"
E1 = "aaaaaaaa-0000-0000-0000-000000000001"
E2 = "aaaaaaaa-0000-0000-0000-000000000002"


@pytest.fixture
def keys(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path = tmp_path / "key.pem"
    path.write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    return path, key.public_key()


def make_client(keys, handler, calls=None):
    path, _ = keys
    s = Settings(
        qlik_mode="jwt",
        qlik_base_url="https://qlik.test/airflowgw",
        qlik_jwt_private_key_path=str(path),
        qlik_jwt_user_id="svc_gw",
        qlik_jwt_user_directory="CORP",
    )
    hook = (lambda *a: calls.append(a)) if calls is not None else None
    return QrsJwtClient(s, hook, transport=httpx.MockTransport(handler))


def test_jwt_and_xrf_headers_and_start(keys):
    seen = []

    def handler(req: httpx.Request):
        seen.append(req)
        return httpx.Response(201, json={"value": E1})

    calls = []
    c = make_client(keys, handler, calls)
    assert c.start_task(TASK) == E1
    req = seen[0]
    assert req.url.path == f"/airflowgw/qrs/task/{TASK}/start/synchronous"
    assert req.url.params["xrfkey"] == req.headers["X-Qlik-Xrfkey"] and len(req.headers["X-Qlik-Xrfkey"]) == 16
    token = req.headers["Authorization"].removeprefix("Bearer ")
    claims = jwt.decode(token, keys[1], algorithms=["RS256"])
    assert claims["userId"] == "svc_gw" and claims["userDirectory"] == "CORP" and claims["exp"] > claims["iat"]
    assert calls[0][0] == "POST" and calls[0][2] == 201


def test_bulk_execution_results(keys):
    def handler(req: httpx.Request):
        flt = req.url.params["filter"]
        assert flt == f"executionID eq {E1} or executionID eq {E2}"
        return httpx.Response(
            200,
            json=[
                {
                    "executionID": E1,
                    "taskID": TASK,
                    "status": 7,
                    "startTime": "2026-09-28T10:00:00.000Z",
                    "stopTime": "2026-09-28T10:05:00.000Z",
                    "executingNodeName": "sched-01",
                    "scriptLogAvailable": True,
                    "fileReferenceID": "ffffffff-0000-0000-0000-000000000000",
                    "details": [{"detailsType": 2, "message": "done", "detailCreatedDate": "x"}],
                },
                {
                    "executionID": E2,
                    "status": 2,
                    "startTime": "2026-09-28T10:00:00Z",
                    "stopTime": "1753-01-01T00:00:00.000Z",
                },
            ],
        )

    res = make_client(keys, handler).get_execution_results([E1, E2])
    assert res[E1].status_code == 7 and res[E1].node == "sched-01" and res[E1].script_log_ref
    assert (res[E1].stop_time - res[E1].start_time).total_seconds() == 300
    assert res[E2].stop_time is None and res[E2].status_text == "Started"


def test_list_tasks_filters_by_custom_property_on_app_or_task(keys):
    def cp(name, value):
        return {"definition": {"name": name}, "value": value}

    def handler(req: httpx.Request):
        if req.url.path.endswith("/qrs/app/full"):
            assert "customProperties.definition.name eq 'Source'" in req.url.params["filter"]
            return httpx.Response(
                200,
                json=[
                    {"id": "app-a", "customProperties": [cp("Source", "Airflow")]},
                    {"id": "app-x", "customProperties": [cp("Source", "Manual"), cp("Owner", "Airflow")]},
                ],
            )
        return httpx.Response(
            200,
            content=json.dumps(
                [
                    {
                        "id": TASK,
                        "name": "Reload A",
                        "enabled": True,
                        "app": {"id": "app-a", "name": "App A", "stream": {"name": "Finance"}},
                        "tags": [{"name": "dwh"}],
                    },
                    {
                        "id": "t-own-cp",
                        "name": "B",
                        "app": {"id": "app-b"},
                        "customProperties": [cp("Source", "Airflow")],
                    },
                    {"id": "t-mismatch", "name": "X", "app": {"id": "app-x"}},
                ]
            ),
        )

    tasks = make_client(keys, handler).list_reload_tasks()
    assert [t.id for t in tasks] == [TASK, "t-own-cp"]
    assert tasks[0].stream_name == "Finance" and tasks[0].tags == ["dwh"]


def test_http_error_is_qlik_error(keys):
    from qlik_gateway.qlik import QlikError

    c = make_client(keys, lambda req: httpx.Response(403, text="Access denied"))
    with pytest.raises(QlikError) as e:
        c.start_task(TASK)
    assert e.value.status_code == 403
