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

ON_ACTIVE = ("reuse", "queue", "reject")
LEGACY_IF_RUNNING = {"attach": "reuse", "fresh": "queue", "queue": "queue", "skip": "reject"}


class QlikReloadOperator(BaseOperator):
    """Runs a Qlik Sense reload task through Qlik Gateway and (optionally) waits for its result.

    :param qlik_task_id: Qlik reload task id (must be allowed for this client in the gateway)
    :param wait_for_completion: wait until the reload finishes and fail the task if it did not succeed
    :param deferrable: wait in the triggerer instead of holding a worker slot
    :param on_active: what to do if the same Qlik task is already RELOADING in Qlik (started by anyone).
        A run still waiting in the gateway queue is always shared: all requests get one execution id.
        "reject" - (the gateway default, used when None) the gateway refuses with a conflict, the
                   reason and the active run (who/when/link); see on_reject
        "queue"  - run again after the active reload; identical requests collapse into that one run.
                   Use it when this DAG has just prepared data that the reload must pick up
        "reuse"  - wait for the active reload and take its result (a warning is logged: it started
                   before this request and may not contain this DAG's data)
        The gateway administrator may force a value for the task or the client; the one applied,
        warnings and the active run are logged and pushed to XCom
        ("on_active", "warnings", "running_execution", "execution_url").
    :param on_reject: when the gateway rejects the start: "fail" (default) fails this Airflow task so
        it is retried later according to retries / retry_delay; "skip" marks it skipped
    :param if_running: legacy alias of on_active (attach=reuse, fresh/queue=queue, skip=reject)
    :param on_already_running: legacy alias of on_reject
    :param dedupe: legacy alias: True = reuse, False = queue
    :param poll_interval: long-poll period against the gateway (the gateway itself polls Qlik)
    :param max_wait: give up waiting after this long (the reload itself keeps running)
    :param cancel_on_kill: stop the reload if the Airflow task is killed (only if it was started by this task)
    :param push_log: what to put into the Airflow task log when the reload fails:
        "errors" (default) - only the script error (the lines after Qlik's "error occurred") and a link
        to the execution page of the gateway; "full" - also the tail of the script log; False - nothing
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
        on_active: str | None = None,
        on_reject: str = "fail",
        if_running: str | None = None,
        on_already_running: str | None = None,
        dedupe: bool | None = None,
        poll_interval: int = 30,
        max_wait: timedelta = timedelta(hours=6),
        cancel_on_kill: bool = True,
        push_log: str | bool = "errors",
        preflight: bool = True,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.qlik_task_id = qlik_task_id
        self.gateway_conn_id = gateway_conn_id
        self.wait_for_completion = wait_for_completion
        self.deferrable = deferrable
        if on_active is None and if_running is not None:
            if if_running not in LEGACY_IF_RUNNING:
                raise ValueError("if_running must be one of: " + ", ".join(LEGACY_IF_RUNNING))
            on_active = LEGACY_IF_RUNNING[if_running]
        if on_active is None and dedupe is not None:
            on_active = "reuse" if dedupe else "queue"
        if on_active is not None and on_active not in ON_ACTIVE:
            raise ValueError("on_active must be one of: " + ", ".join(ON_ACTIVE))
        on_reject = on_already_running or on_reject
        if on_reject not in ("skip", "fail"):
            raise ValueError("on_reject must be 'fail' or 'skip'")
        if push_log is True:
            push_log = "errors"
        if push_log not in (False, None, "errors", "full"):
            raise ValueError("push_log must be 'errors', 'full' or False")
        self.on_active = on_active
        self.on_reject = on_reject
        self.poll_interval = poll_interval
        self.max_wait = max_wait
        self.cancel_on_kill = cancel_on_kill
        self.push_log = push_log
        self.preflight = preflight
        self._execution_id: int | None = None
        self._url: str | None = None
        self._owned = False
        self._hook: QlikGatewayHook | None = None

    def execute(self, context):
        self._hook = hook = QlikGatewayHook(self.gateway_conn_id, context=context)
        if self.preflight:
            self._preflight(hook)
        started = hook.start_task(self.qlik_task_id, on_active=self.on_active)
        ti = context["ti"]
        running = started.get("running_execution")
        applied = started.get("on_active")
        if applied and self.on_active and applied != self.on_active:
            self.log.info("on_active=%s applied by the gateway (set on the %s)", applied, started.get("policy_source"))
        for w in started.get("warnings") or []:
            self.log.warning("Qlik Gateway: %s: %s", w.get("code"), w.get("message"))
        ti.xcom_push(key="on_active", value=applied)
        ti.xcom_push(key="warnings", value=started.get("warnings") or [])
        ti.xcom_push(key="running_execution", value=running)
        if started.get("error") == "already_running":
            self.log.warning("Qlik task is already reloading: %s", _describe(running))
            msg = f"Rejected by Qlik Gateway: {started.get('message')}"
            if running and running.get("url"):
                msg += f". Active run: {running['url']}"
            if self.on_reject == "skip":
                raise AirflowSkipException(msg)
            raise AirflowException(msg + " (will be retried per retries / retry_delay)")
        self._execution_id = started["execution_id"]
        self._url = started.get("url")
        self._owned = not started.get("deduplicated")
        ti.xcom_push(key="execution_id", value=self._execution_id)
        ti.xcom_push(key="execution_url", value=self._url)
        ti.xcom_push(key="deduplicated", value=bool(started.get("deduplicated")))
        if started.get("deduplicated"):
            self.log.info("Joined an existing run of the Qlik task: %s", _describe(running))
        elif running:
            self.log.info(
                "Qlik task is reloading now (%s); new execution %s queued after it, it will load the current data",
                _describe(running),
                self._execution_id,
            )
        else:
            self.log.info(
                "Qlik task %s -> gateway execution %s (%s)", self.qlik_task_id, self._execution_id, started["status"]
            )
        if self._url:
            self.log.info("Execution page: %s", self._url)
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
        url = state.get("url") or self._url
        if state.get("status") == "SUCCESS":
            self.log.info("Qlik reload finished in %.0fs", state.get("duration_seconds") or 0)
            return eid
        detail = state.get("error_detail")
        if self.push_log:
            # only the error, not the whole script log: logs can be huge; the rest is one click away
            self.log.error(
                "Qlik reload %s: %s\n%s\nDetails and full log: %s",
                eid,
                state.get("status"),
                detail or state.get("error") or state.get("message") or "-",
                url or "-",
            )
        if self.push_log == "full" and eid:
            try:
                self.log.error("Qlik script log (tail):\n%s", hook.get_log(eid)[-20_000:])
            except AirflowException as e:
                self.log.warning("Script log unavailable: %s", e)
        reason = detail or state.get("error") or state.get("message")
        raise AirflowException(
            f"Qlik reload {self.qlik_task_id} ended with {state.get('status')}: {reason}" + (f" | {url}" if url else "")
        )

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
