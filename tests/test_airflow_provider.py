"""Runs the Airflow provider against a live gateway (Airflow itself is replaced by small stubs)."""

import asyncio
import logging
import socket
import sys
import threading
import time
import types
from datetime import timedelta
from pathlib import Path

import pytest
import uvicorn

from qlik_gateway.worker import Coordinator

from .conftest import RISK, SALES


class _AirflowException(Exception):
    pass


class _AirflowSkipException(Exception):
    pass


class _TaskDeferred(Exception):
    def __init__(self, trigger, method_name, timeout=None):
        super().__init__(method_name)
        self.trigger, self.method_name, self.timeout = trigger, method_name, timeout


class _Conn:
    def __init__(self, host, password):
        self.host, self.password, self.schema, self.port, self.extra_dejson = host, password, None, None, {}


CONNECTIONS: dict[str, _Conn] = {}


class _BaseHook:
    def __init__(self, *a, **kw):
        self.log = logging.getLogger("hook")

    @classmethod
    def get_connection(cls, conn_id):
        return CONNECTIONS[conn_id]


class _BaseOperator:
    def __init__(self, task_id=None, **kw):
        self.task_id = task_id
        self.log = logging.getLogger("op")

    def defer(self, *, trigger, method_name, timeout=None):
        raise _TaskDeferred(trigger, method_name, timeout)


class _BaseSensor(_BaseOperator):
    def __init__(self, mode=None, poke_interval=None, **kw):
        super().__init__(**kw)


class _BaseTrigger:
    def __init__(self, **kw):
        pass


class _TriggerEvent:
    def __init__(self, payload):
        self.payload = payload


def _install_stubs():
    mods = {
        "airflow": types.ModuleType("airflow"),
        "airflow.exceptions": types.SimpleNamespace(
            AirflowException=_AirflowException, AirflowSkipException=_AirflowSkipException
        ),
        "airflow.hooks": types.ModuleType("airflow.hooks"),
        "airflow.hooks.base": types.SimpleNamespace(BaseHook=_BaseHook),
        "airflow.models": types.SimpleNamespace(BaseOperator=_BaseOperator),
        "airflow.sensors": types.ModuleType("airflow.sensors"),
        "airflow.sensors.base": types.SimpleNamespace(BaseSensorOperator=_BaseSensor),
        "airflow.triggers": types.ModuleType("airflow.triggers"),
        "airflow.triggers.base": types.SimpleNamespace(BaseTrigger=_BaseTrigger, TriggerEvent=_TriggerEvent),
    }
    for name, mod in mods.items():
        sys.modules.setdefault(name, mod)
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "airflow" / "plugins"))


@pytest.fixture
def provider():
    _install_stubs()
    import qlik_gateway_provider

    return qlik_gateway_provider


@pytest.fixture
def live_gateway(app, settings, mock, make_client):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.05)

    coord = Coordinator(settings, mock)
    coord.tick(force=True)
    stop = threading.Event()

    def loop():
        while not stop.is_set():
            coord.tick(force=True)
            # finish mock reloads quickly
            for e in mock.executions.values():
                e["duration"] = 0.5
            time.sleep(0.3)

    threading.Thread(target=loop, daemon=True).start()
    _, headers = make_client("airflow-test")
    CONNECTIONS["qlik_gateway_default"] = _Conn(f"http://127.0.0.1:{port}", headers["Authorization"][7:])
    yield
    stop.set()
    server.should_exit = True


class _TI:
    dag_id, task_id, run_id, try_number, map_index = "dag_x", "reload", "manual__1", 1, -1

    def __init__(self):
        self.xcom = {}

    def xcom_push(self, key, value):
        self.xcom[key] = value


def test_operator_success_and_initiator_is_recorded(provider, live_gateway):
    ti = _TI()
    op = provider.QlikReloadOperator(task_id="reload", qlik_task_id=SALES, poll_interval=1)
    eid = op.execute({"ti": ti, "run_id": "manual__1"})
    assert eid == ti.xcom["execution_id"]
    details = provider.QlikGatewayHook().get_details(eid)
    assert details["status"] == "SUCCESS"
    assert details["initiator"]["dag_id"] == "dag_x" and details["initiator"]["run_id"] == "manual__1"


def test_operator_fails_on_failed_reload(provider, live_gateway):
    op = provider.QlikReloadOperator(task_id="reload", qlik_task_id=RISK, poll_interval=1)
    with pytest.raises(_AirflowException, match="FAILED"):
        op.execute({"ti": _TI()})


def test_forbidden_task_raises(provider, live_gateway):
    op = provider.QlikReloadOperator(task_id="reload", qlik_task_id="no-such-task")
    with pytest.raises(_AirflowException, match="HTTP 404"):
        op.execute({"ti": _TI()})


def test_deferrable_trigger_and_sensor(provider, live_gateway):
    op = provider.QlikReloadOperator(
        task_id="reload", qlik_task_id=SALES, deferrable=True, poll_interval=1, max_wait=timedelta(minutes=1)
    )
    with pytest.raises(_TaskDeferred) as d:
        op.execute({"ti": _TI()})
    trigger = d.value.trigger
    assert "token" not in str(trigger.serialize())

    async def first_event():
        async for ev in trigger.run():
            return ev.payload

    payload = asyncio.run(first_event())
    assert payload["status"] == "SUCCESS"
    assert op.execute_complete({}, payload) == payload["execution_id"]

    sensor = provider.QlikExecutionSensor(task_id="wait", execution_id=str(payload["execution_id"]))
    assert sensor.poke({}) is True


def test_if_running_policies(provider, live_gateway, mock):
    for e in list(mock.executions.values()):
        e["duration"] = 0.5
    import time as _t

    first = provider.QlikReloadOperator(task_id="a", qlik_task_id=SALES, wait_for_completion=False)
    ti1 = _TI()
    first.execute({"ti": ti1})
    # the reload is queued/running now: the three policies
    ti2 = _TI()
    provider.QlikReloadOperator(task_id="b", qlik_task_id=SALES, wait_for_completion=False).execute({"ti": ti2})
    assert ti2.xcom["execution_id"] == ti1.xcom["execution_id"] and ti2.xcom["deduplicated"] is True
    assert ti2.xcom["running_execution"]["own"] is True and ti2.xcom["running_execution"]["initiator"]["dag_id"]

    with pytest.raises(_AirflowSkipException):
        provider.QlikReloadOperator(task_id="c", qlik_task_id=SALES, if_running="skip").execute({"ti": _TI()})

    ti4 = _TI()
    provider.QlikReloadOperator(task_id="d", qlik_task_id=SALES, if_running="queue", wait_for_completion=False).execute(
        {"ti": ti4}
    )
    assert ti4.xcom["execution_id"] != ti1.xcom["execution_id"] and ti4.xcom["deduplicated"] is False
    assert ti4.xcom["running_execution"]["execution_id"] == ti1.xcom["execution_id"]
    _t.sleep(0)
