from collections.abc import Sequence
from datetime import timedelta

try:
    from ._compat import AirflowException, AirflowSkipException, BaseOperator
    from .hooks import QlikGatewayHook
except ImportError:  # parsed on its own by the DAG processor (not covered by .airflowignore)
    import os
    import sys

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from qlik_gateway_provider._compat import AirflowException, AirflowSkipException, BaseOperator
    from qlik_gateway_provider.hooks import QlikGatewayHook

IF_RUNNING = ("fresh", "attach", "queue", "skip")


class QlikReloadOperator(BaseOperator):
    """Runs a Qlik Sense reload task through Qlik Gateway and (optionally) waits for its result.

    :param qlik_task_id: Qlik reload task id (must be allowed for this client in the gateway)
    :param wait_for_completion: wait until the reload finishes and fail the task if it did not succeed
    :param deferrable: wait in the triggerer instead of holding a worker slot
    :param if_running: what to do if the same Qlik task is already queued/running (started by anyone):
        "fresh" (default) - join a run only if it has not been sent to Qlik yet (it will read the data
                   as of its start); if the reload is already going, queue a new one after it, so the
                   data this DAG prepared is guaranteed to be loaded
        "attach" - wait for whatever run is going and take its result (a warning is logged if it
                   started before this request: it may miss this DAG's data)
        "queue"  - always start a new reload after the current one finishes
        "skip"   - start nothing; see on_already_running
        The gateway administrator may force a policy for the task or the client; the one applied,
        warnings and the running execution (who/when) are logged and pushed to XCom
        ("if_running", "warnings", "running_execution").
    :param on_already_running: with "skip" (requested or forced): "skip" marks this Airflow task
        skipped, "fail" fails it so Airflow retries later according to retries / retry_delay
    :param dedupe: legacy alias: True = if_running="attach", False = if_running="queue"
    :param poll_interval: long-poll period against the gateway (the gateway itself polls Qlik)
    :param max_wait: give up waiting after this long (the reload itself keeps running)
    :param cancel_on_kill: stop the reload if the Airflow task is killed (only if it was started by this task)
    :param push_log: put Qlik's script log into the Airflow task log when the reload fails
    :param preflight: before starting, check the token and the task (client, rights, task name,
        blocked/disabled) and log it - replaces a separate "check gateway" task in DAGs
    """

    template_fields: Sequence[str] = ("qlik_task_id",)
    ui_color = "#009845"

    def __init__(
        self,
        *,
        qlik_task_id: str,
        gateway_conn_id: str = QlikGatewayHook.default_conn_name,
        wait_for_completion: bool = True,
        deferrable: bool = False,
        if_running: str = "fresh",
        on_already_running: str = "skip",
        dedupe: bool | None = None,
        poll_interval: int = 30,
        max_wait: timedelta = timedelta(hours=6),
        cancel_on_kill: bool = True,
        push_log: bool = True,
        preflight: bool = True,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.qlik_task_id = qlik_task_id
        self.gateway_conn_id = gateway_conn_id
        self.wait_for_completion = wait_for_completion
        self.deferrable = deferrable
        if dedupe is not None:
            if_running = "attach" if dedupe else "queue"
        if if_running not in IF_RUNNING:
            raise ValueError("if_running must be one of: " + ", ".join(IF_RUNNING))
        if on_already_running not in ("skip", "fail"):
            raise ValueError("on_already_running must be 'skip' or 'fail'")
        self.if_running = if_running
        self.on_already_running = on_already_running
        self.poll_interval = poll_interval
        self.max_wait = max_wait
        self.cancel_on_kill = cancel_on_kill
        self.push_log = push_log
        self.preflight = preflight
        self._execution_id: int | None = None
        self._owned = False
        self._hook: QlikGatewayHook | None = None

    def execute(self, context):
        self._hook = hook = QlikGatewayHook(self.gateway_conn_id, context=context)
        if self.preflight:
            self._preflight(hook)
        started = hook.start_task(self.qlik_task_id, if_running=self.if_running)
        running = started.get("running_execution")
        applied = started.get("if_running")
        if applied and applied != self.if_running:
            self.log.info("if_running=%s applied by the gateway (set on the %s)", applied, started.get("policy_source"))
        for w in started.get("warnings") or []:
            self.log.warning("Qlik Gateway: %s: %s", w.get("code"), w.get("message"))
        context["ti"].xcom_push(key="if_running", value=applied)
        context["ti"].xcom_push(key="warnings", value=started.get("warnings") or [])
        if started.get("error") == "already_running":
            self.log.info("Qlik task is already running: %s", _describe(running))
            context["ti"].xcom_push(key="running_execution", value=running)
            if self.on_already_running == "fail":
                raise AirflowException(f"{started.get('message')}; will be retried per retries/retry_delay")
            raise AirflowSkipException(f"Skipped: {started.get('message')}")
        self._execution_id = started["execution_id"]
        self._owned = not started.get("deduplicated")
        context["ti"].xcom_push(key="execution_id", value=self._execution_id)
        context["ti"].xcom_push(key="deduplicated", value=bool(started.get("deduplicated")))
        context["ti"].xcom_push(key="running_execution", value=running)
        if started.get("deduplicated"):
            self.log.info("Qlik task is already running, attached to it: %s", _describe(running))
        elif running:
            self.log.info(
                "Qlik task is running now (%s); new execution %s queued after it, it will load the current data",
                _describe(running),
                self._execution_id,
            )
        else:
            self.log.info(
                "Qlik task %s -> gateway execution %s (%s)", self.qlik_task_id, self._execution_id, started["status"]
            )
        if not self.wait_for_completion:
            return self._execution_id

        if self.deferrable:
            from .triggers import QlikGatewayExecutionTrigger

            self.defer(
                trigger=QlikGatewayExecutionTrigger(
                    execution_id=self._execution_id,
                    gateway_conn_id=self.gateway_conn_id,
                    poll_interval=self.poll_interval,
                ),
                method_name="execute_complete",
                timeout=self.max_wait,
            )

        state = hook.wait_for_completion(
            self._execution_id, poll_interval=self.poll_interval, timeout=self.max_wait.total_seconds()
        )
        return self._handle_result(state, hook)

    def _preflight(self, hook: QlikGatewayHook) -> None:
        """Fail early with a clear message instead of a bare HTTP error from the start call."""
        me = hook.whoami()  # 401/403 here = wrong/blocked token or IP not allowed
        self.log.info("Qlik Gateway client '%s', actions: %s", me["client"], ", ".join(me["allowed_actions"]))
        if "start" not in me["allowed_actions"]:
            raise AirflowException(f"Client '{me['client']}' is not allowed to start reloads (no 'start' right)")
        if "info" not in me["allowed_actions"]:
            return  # task details are not visible to this client; the start call will validate the task
        info = hook.get_task_info(self.qlik_task_id)  # 403 = no access to the task, 404 = not in the catalog
        self.log.info("Qlik task '%s' (app '%s')", info["name"], info["app_name"])
        if info.get("blocked"):
            raise AirflowException(f"Qlik task '{info['name']}' is blocked in the gateway")
        if not info.get("enabled_in_qlik", True):
            raise AirflowException(f"Qlik task '{info['name']}' is disabled in Qlik")
        if info.get("active_execution_id"):
            self.log.info("The task is already queued/running (execution %s)", info["active_execution_id"])

    def execute_complete(self, context, event=None):
        hook = QlikGatewayHook(self.gateway_conn_id, context=context)
        return self._handle_result(event or {}, hook)

    def _handle_result(self, state: dict, hook: QlikGatewayHook):
        eid = state.get("execution_id", self._execution_id)
        if state.get("status") == "SUCCESS":
            self.log.info("Qlik reload finished in %.0fs", state.get("duration_seconds") or 0)
            return eid
        if self.push_log and eid:
            try:
                self.log.error("Qlik script log (tail):\n%s", hook.get_log(eid)[-20_000:])
            except AirflowException as e:
                self.log.warning("Script log unavailable: %s", e)
        reason = state.get("error_detail") or state.get("error") or state.get("message")
        raise AirflowException(f"Qlik reload {self.qlik_task_id} ended with {state.get('status')}: {reason}")

    def on_kill(self):
        if self.cancel_on_kill and self._owned and self._execution_id and self._hook:
            self.log.info("Cancelling gateway execution %s", self._execution_id)
            try:
                self._hook.cancel(self._execution_id)
            except AirflowException as e:
                self.log.warning("Cancel failed: %s", e)


def _describe(run: dict | None) -> str:
    """execution 15, RUNNING, started 2026-09-30T06:00:51Z by this client (dag qlik_smoke / run manual__...)."""
    if not run:
        return "-"
    who = "this client" if run.get("own") else "another client"
    ini = run.get("initiator") or {}
    origin = f" (dag {ini.get('dag_id')} / run {ini.get('run_id')})" if ini.get("dag_id") else ""
    when = run.get("started_at") or run.get("created_at")
    return f"execution {run.get('execution_id')}, {run.get('status')}, since {when} by {who}{origin}"
