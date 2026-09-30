"""Smoke test of the Qlik Gateway integration. Trigger manually ("Trigger DAG w/ config" to change params).

1. reload_and_wait - checks the token and the task (built into the operator), starts a reload and waits
2. start_reload    - starts without waiting ...
3. wait_reload     - ... and waits with a sensor in reschedule mode (no worker slot between pokes)

Needs the connection `qlik_gateway_default` (HTTP, host/port of the gateway, password = client token).
"""

import os
import sys
from datetime import datetime

# The provider may sit next to this file (e.g. dags/qlik_gateway_provider/ in a DAG bundle
# subfolder that is not on sys.path): make it importable either way.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from qlik_gateway_provider import QlikExecutionSensor, QlikReloadOperator  # noqa: E402

try:  # Airflow 3
    from airflow.sdk import DAG, Param
except ImportError:  # Airflow 2
    from airflow import DAG
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
    reload_and_wait = QlikReloadOperator(
        task_id="reload_and_wait",
        qlik_task_id="{{ params.qlik_task_id }}",
        poll_interval=15,
        # fresh (default): never join a reload that started before this DAG's data was ready;
        # attach / queue / skip are the other options, see README "if_running"
        if_running="fresh",
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

    reload_and_wait >> start_reload >> wait_reload
