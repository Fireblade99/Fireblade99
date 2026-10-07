"""Example: business DAGs run Qlik reloads only through Qlik Gateway.

Airflow never has Qlik credentials or certificates. The gateway:
  * checks this Airflow's token and permissions (which tasks/actions are allowed),
  * queues the reload, starts it on its own behalf with its own concurrency limits,
  * polls Qlik once per tick for ALL running reloads and keeps the history,
  * answers state requests from its DB, logs who/what/when (dag_id, run_id, host...).

Connection `qlik_gateway_default`: type HTTP, host=https://qlik-gateway.company.local, password=<token>.
"""

from datetime import datetime, timedelta

from airflow import DAG

from qlik_gateway_provider import QlikExecutionSensor, QlikReloadOperator

SALES_DWH = "11111111-1111-1111-1111-111111111111"
HR_DASHBOARD = "22222222-2222-2222-2222-222222222222"

with DAG(
    dag_id="example_qlik_reload",
    start_date=datetime(2026, 1, 1),
    schedule="0 6 * * *",
    catchup=False,
    default_args={"owner": "bi-team", "retries": 1, "retry_delay": timedelta(minutes=10)},
    tags=["qlik"],
) as dag:
    # 1. Simplest: start and wait (deferrable -> waits in the triggerer, no worker slot held)
    reload_sales = QlikReloadOperator(
        task_id="reload_sales_dwh",
        qlik_task_id=SALES_DWH,
        deferrable=True,
        poll_interval=60,
    )

    # 2. Fire-and-forget + a sensor in reschedule mode
    start_hr = QlikReloadOperator(task_id="start_hr", qlik_task_id=HR_DASHBOARD, wait_for_completion=False)
    wait_hr = QlikExecutionSensor(
        task_id="wait_hr",
        execution_id="{{ ti.xcom_pull(task_ids='start_hr', key='execution_id') }}",
        poke_interval=120,
        timeout=4 * 3600,
    )

    reload_sales >> start_hr >> wait_hr
