"""Demo of how Qlik Gateway handles several requests to reload the same Qlik task (for the business).

Two "teams" = two gateway clients = two Airflow connections:
  qlik_gateway_default  - team A (token of client A)
  qlik_gateway_team_b   - team B (token of client B, both clients must be allowed to start the task)

1. idle_requests        - the task is idle: A, B and A again ask for a reload at the same moment.
                          They collapse into ONE gateway execution, whoever sent them.
2. wait_until_reloading - waits until that run is reloading in Qlik.
3. While it reloads, each team asks again, with a different on_active (requests only, no waiting,
   so they all arrive while the first run is reloading):
     team_b_reuse   on_active="reuse"  -> gets the reloading run (same id) + warning               (green)
     team_a_queue   on_active="queue"  -> one new run after the active one                          (green)
     team_b_queue   on_active="queue"  -> collapses into that same new run                          (green)
     team_b_reject  default "reject"   -> conflict: reason + active run + link; marked skipped      (pink)
4. summary              - a table of who got what with links to the gateway pages, then waits for
                          both reloads (the first run and the queued one) and shows their result.

Use a TEST task that reloads for 1+ minute: it is reloaded twice (the first run and the queued one).
Trigger with "Trigger DAG w/ config" to set the task id.
"""

import os
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from qlik_gateway_provider import QlikGatewayHook, QlikReloadOperator  # noqa: E402

try:  # Airflow 3
    from airflow.sdk import DAG, Param, task
except ImportError:  # Airflow 2
    from airflow import DAG
    from airflow.decorators import task
    from airflow.models.param import Param

try:
    from airflow.exceptions import AirflowException
except ImportError:  # pragma: no cover
    AirflowException = RuntimeError

CONN_A = "{{ params.conn_team_a }}"
CONN_B = "{{ params.conn_team_b }}"
PHASE_2 = ("team_b_reuse", "team_a_queue", "team_b_queue", "team_b_reject")

with DAG(
    dag_id="qlik_gateway_on_active_demo",
    start_date=datetime(2026, 1, 1),
    schedule=None,
    catchup=False,
    max_active_runs=1,
    tags=["qlik", "gateway", "demo"],
    params={
        "qlik_task_id": Param("462fc8e2-5add-4c14-b2b4-28171e890e66", type="string", description="TEST Qlik task id"),
        "conn_team_a": Param("qlik_gateway_default", type="string", description="Connection of team A"),
        "conn_team_b": Param("qlik_gateway_team_b", type="string", description="Connection of team B"),
    },
    default_args={"owner": "bi-platform", "retries": 0},
) as dag:

    @task
    def idle_requests(**context) -> int:
        """Three requests at the same moment while the task is idle -> one execution id."""
        p = context["params"]
        a = QlikGatewayHook(p["conn_team_a"], context=context)
        b = QlikGatewayHook(p["conn_team_b"], context=context)
        rows = [
            ("team A", "(default)", a.start_task(p["qlik_task_id"], meta={"step": "1. idle, team A"})),
            ("team B", "reject", b.start_task(p["qlik_task_id"], on_active="reject", meta={"step": "1. idle, team B"})),
            (
                "team A",
                "queue",
                a.start_task(p["qlik_task_id"], on_active="queue", meta={"step": "1. idle, team A again"}),
            ),
        ]
        for who, asked, r in rows:
            got = r.get("execution_id") or f"{r.get('error')}: {r.get('message')}"
            print(f"  {who:7} on_active={asked:10} -> execution {got}  deduplicated={r.get('deduplicated')}")
        if rows[0][2].get("error"):
            raise AirflowException(
                "The task is already reloading in Qlik, so this is not the 'idle' case. Wait until it finishes "
                f"and trigger the demo again. Active run: {rows[0][2].get('running_execution', {}).get('url')}"
            )
        ids = {r.get("execution_id") for _, _, r in rows}
        if len(ids) != 1:
            raise AirflowException(f"Expected one execution for all requests, got {ids}")
        first = rows[0][2]["execution_id"]
        print(f"\nAll three requests got ONE execution: {first}  {rows[0][2].get('url')}")
        return first

    @task
    def wait_until_reloading(first: int, **context) -> int:
        hook = QlikGatewayHook(context["params"]["conn_team_a"], context=context)
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            status = hook.get_state(first)["status"]
            if status in ("STARTING", "RUNNING"):
                print(f"Execution {first} is {status} in Qlik")
                return first
            if status != "QUEUED":
                raise AirflowException(f"Execution {first} is {status}: use a task that reloads for 1+ minute")
            time.sleep(3)
        raise AirflowException(f"Execution {first} did not start in 5 minutes")

    @task(trigger_rule="all_done")
    def summary(first: int | None = None, **context):
        ti = context["ti"]

        def x(task_id, key):
            try:
                return ti.xcom_pull(task_ids=task_id, key=key)
            except Exception:  # noqa: BLE001
                return None

        expected = {
            "team_b_reuse": "same id as step 1 (the reloading run) + warning",
            "team_a_queue": "NEW id: one run after the active one",
            "team_b_queue": "the same NEW id as team_a_queue (collapsed)",
            "team_b_reject": "skipped: conflict + the active run + link",
        }
        lines = [f"Step 1: three requests while the task was idle -> one execution {first}", ""]
        for t in PHASE_2:
            eid = x(t, "execution_id")
            run = x(t, "running_execution") or {}
            warnings = ", ".join(w.get("code", "") for w in (x(t, "warnings") or []))
            got = f"execution {eid}" if eid else f"rejected, active run {run.get('execution_id')}"
            lines.append(f"{t:14} on_active={x(t, 'on_active') or '-':7} -> {got:28} {warnings}")
            lines.append(f"{'':14} expected: {expected[t]}")
            url = x(t, "execution_url") or run.get("url")
            if url:
                lines.append(f"{'':14} {url}#requests")
        reuse, qa, qb = (
            x("team_b_reuse", "execution_id"),
            x("team_a_queue", "execution_id"),
            x("team_b_queue", "execution_id"),
        )
        checks = {
            "reuse got the reloading run": reuse == first,
            "queue made one new run": qa is not None and qa != first,
            "both queue requests share it": qa is not None and qa == qb,
            "reject did not start anything": x("team_b_reject", "execution_id") is None,
        }
        lines.append("")
        lines += [f"{'PASS' if ok else 'FAIL'}  {name}" for name, ok in checks.items()]
        print("\n".join(lines))
        if not all(checks.values()):
            raise AirflowException("Some checks failed, see the table above")

        hook = QlikGatewayHook(context["params"]["conn_team_a"], context=context)
        for eid in (first, qa):
            state = hook.wait_for_completion(eid, poll_interval=30, timeout=3600)
            print(f"Reload of execution {eid}: {state['status']}  {state.get('url')}#requests")

    first = idle_requests()
    reloading = wait_until_reloading(first)

    common = {"qlik_task_id": "{{ params.qlik_task_id }}", "wait_for_completion": False, "preflight": False}
    phase_2 = [
        QlikReloadOperator(task_id="team_b_reuse", gateway_conn_id=CONN_B, on_active="reuse", **common),
        QlikReloadOperator(task_id="team_a_queue", gateway_conn_id=CONN_A, on_active="queue", **common),
        QlikReloadOperator(task_id="team_b_queue", gateway_conn_id=CONN_B, on_active="queue", **common),
        # default on_active = reject; on_reject="skip" instead of failing keeps the demo run green
        QlikReloadOperator(task_id="team_b_reject", gateway_conn_id=CONN_B, on_reject="skip", **common),
    ]
    reloading >> phase_2 >> summary(first)
