from collections.abc import Sequence
from datetime import timedelta

from ._compat import AirflowException, BaseOperator
from .hooks import QlikGatewayHook


class QlikReloadOperator(BaseOperator):
    """Runs a Qlik Sense reload task through Qlik Gateway and (optionally) waits for its result.

    :param qlik_task_id: Qlik reload task id (must be allowed for this client in the gateway)
    :param wait_for_completion: wait until the reload finishes and fail the task if it did not succeed
    :param deferrable: wait in the triggerer instead of holding a worker slot
    :param dedupe: if the same reload is already queued/running, attach to it instead of starting a new one
    :param poll_interval: long-poll period against the gateway (the gateway itself polls Qlik)
    :param max_wait: give up waiting after this long (the reload itself keeps running)
    :param cancel_on_kill: stop the reload if the Airflow task is killed (only if it was started by this task)
    :param push_log: put Qlik's script log into the Airflow task log when the reload fails
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
        dedupe: bool = True,
        poll_interval: int = 30,
        max_wait: timedelta = timedelta(hours=6),
        cancel_on_kill: bool = True,
        push_log: bool = True,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.qlik_task_id = qlik_task_id
        self.gateway_conn_id = gateway_conn_id
        self.wait_for_completion = wait_for_completion
        self.deferrable = deferrable
        self.dedupe = dedupe
        self.poll_interval = poll_interval
        self.max_wait = max_wait
        self.cancel_on_kill = cancel_on_kill
        self.push_log = push_log
        self._execution_id: int | None = None
        self._owned = False
        self._hook: QlikGatewayHook | None = None

    def execute(self, context):
        self._hook = hook = QlikGatewayHook(self.gateway_conn_id, context=context)
        started = hook.start_task(self.qlik_task_id, dedupe=self.dedupe)
        self._execution_id = started["execution_id"]
        self._owned = not started.get("deduplicated")
        self.log.info(
            "Qlik task %s -> gateway execution %s (%s%s)",
            self.qlik_task_id,
            self._execution_id,
            started["status"],
            ", attached to an already running reload" if started.get("deduplicated") else "",
        )
        context["ti"].xcom_push(key="execution_id", value=self._execution_id)
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
        reason = state.get("error") or state.get("message")
        raise AirflowException(f"Qlik reload {self.qlik_task_id} ended with {state.get('status')}: {reason}")

    def on_kill(self):
        if self.cancel_on_kill and self._owned and self._execution_id and self._hook:
            self.log.info("Cancelling gateway execution %s", self._execution_id)
            try:
                self._hook.cancel(self._execution_id)
            except AirflowException as e:
                self.log.warning("Cancel failed: %s", e)
