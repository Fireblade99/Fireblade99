"""Smoke test of the Qlik Gateway integration. Trigger manually ("Trigger DAG w/ config" to change params).

1. check_gateway     - token works, shows the client's rights and the task as the gateway sees it
2. reload_and_wait   - start a reload and wait in the worker (simplest mode)
3. start_reload      - start without waiting ...
4. wait_reload       - ... and wait with a sensor in reschedule mode (no worker slot between pokes)

Needs the connection `qlik_gateway_default` (HTTP, host/port of the gateway, password = client token).
"""

import os
import sys
from datetime import datetime

# The provider may sit next to this file (e.g. dags/qlik_gateway_provider/ in a DAG bundle
# subfolder that is not on sys.path): make it importable either way.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from qlik_gateway_provider import QlikExecutionSensor, QlikGatewayHook, QlikReloadOperator  # noqa: E402

try:  # Airflow 3
    from airflow.sdk import DAG, Param, task
except ImportError:  # Airflow 2
    from airflow import DAG
    from airflow.decorators import task
    from airflow.models.param import Param


with DAG(
    dag_id="qlik_gateway_smoke",
    start_date=datetime(2026, 1, 1),
    schedule=None,
    catchup=False,
    tags=["qlik", "gateway", "test"],
    params={
        "qlik_task_id": Param("462fc8e2-5add-4c14-b2b4-28171e890e66", type="string", description="Qlik reload task id"),
    },
    default_args={"owner": "bi-platform", "retries": 0},
) as dag:

    @task
    def check_gateway(params=None, **context):
        hook = QlikGatewayHook(context=context)
        me = hook.whoami()
        print(f"client: {me['client']}, actions: {me['allowed_actions']}, limits: {me['limits']}")
        info = hook.get_task_info(params["qlik_task_id"])
        print(f"task: {info['name']} | app: {info['app_name']} | enabled in Qlik: {info['enabled_in_qlik']}")
        print(f"recent executions: {[(e['execution_id'], e['status']) for e in info['recent_executions']]}")
        if info["blocked"] or not info["enabled_in_qlik"]:
            raise ValueError("task is blocked in the gateway or disabled in Qlik")

    reload_and_wait = QlikReloadOperator(
        task_id="reload_and_wait",
        qlik_task_id="{{ params.qlik_task_id }}",
        poll_interval=15,
    )

    start_reload = QlikReloadOperator(
        task_id="start_reload",
        qlik_task_id="{{ params.qlik_task_id }}",
        wait_for_completion=False,
    )

    wait_reload = QlikExecutionSensor(
        task_id="wait_reload",
        execution_id="{{ ti.xcom_pull(task_ids='start_reload', key='execution_id') }}",
        poke_interval=30,
        timeout=3600,
    )

    check_gateway() >> reload_and_wait >> start_reload >> wait_reload
