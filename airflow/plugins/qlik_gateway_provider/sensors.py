from collections.abc import Sequence

try:
    from ._compat import AirflowException, BaseSensorOperator
    from .hooks import TERMINAL, QlikGatewayHook
except ImportError:  # parsed on its own by the DAG processor (not covered by .airflowignore)
    import os
    import sys

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from qlik_gateway_provider._compat import AirflowException, BaseSensorOperator
    from qlik_gateway_provider.hooks import TERMINAL, QlikGatewayHook


class QlikExecutionSensor(BaseSensorOperator):
    """Waits for a gateway execution (e.g. started by QlikReloadOperator(wait_for_completion=False)).

    Use mode="reschedule" so the sensor does not occupy a worker slot between pokes. Each poke is
    answered by the gateway from its own database, so it creates no load on Qlik.
    """

    template_fields: Sequence[str] = ("execution_id",)

    def __init__(self, *, execution_id, gateway_conn_id: str = QlikGatewayHook.default_conn_name, **kwargs):
        kwargs.setdefault("mode", "reschedule")
        kwargs.setdefault("poke_interval", 60)
        super().__init__(**kwargs)
        self.execution_id = execution_id
        self.gateway_conn_id = gateway_conn_id

    def poke(self, context) -> bool:
        state = QlikGatewayHook(self.gateway_conn_id, context=context).get_state(int(self.execution_id))
        if state["status"] not in TERMINAL:
            return False
        if state["status"] != "SUCCESS":
            reason = state.get("error_detail") or state.get("error")
            url = f" | {state['url']}" if state.get("url") else ""
            raise AirflowException(f"Qlik execution {self.execution_id} ended with {state['status']}: {reason}{url}")
        return True
