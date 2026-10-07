import socket
import time

import requests

try:
    from ._compat import AirflowException, BaseHook
except ImportError:  # parsed on its own by the DAG processor (not covered by .airflowignore)
    import os
    import sys

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from qlik_gateway_provider._compat import AirflowException, BaseHook

TERMINAL = {"SUCCESS", "FAILED", "ABORTED", "SKIPPED", "CANCELLED", "START_ERROR", "LOST", "TIMEOUT"}


def initiator_headers(context: dict | None) -> dict:
    """Who is calling: sent with every request and stored by the gateway in its audit log."""
    headers = {"X-Airflow-Host": socket.gethostname()}
    if not context:
        return headers
    ti = context.get("ti") or context.get("task_instance")
    dag = context.get("dag")
    values = {
        "Dag-Id": getattr(ti, "dag_id", None) or getattr(dag, "dag_id", None),
        "Task-Id": getattr(ti, "task_id", None),
        "Run-Id": context.get("run_id") or getattr(ti, "run_id", None),
        "Try-Number": getattr(ti, "try_number", None),
        "Map-Index": getattr(ti, "map_index", None),
        "Owner": getattr(getattr(ti, "task", None), "owner", None) or getattr(dag, "owner", None),
    }
    headers.update({f"X-Airflow-{k}": str(v) for k, v in values.items() if v is not None})
    return headers


class QlikGatewayHook(BaseHook):
    conn_name_attr = "gateway_conn_id"
    default_conn_name = "qlik_gateway_default"
    conn_type = "http"
    hook_name = "Qlik Gateway"

    def __init__(self, gateway_conn_id: str = default_conn_name, context: dict | None = None, timeout: float = 90):
        super().__init__()
        self.gateway_conn_id = gateway_conn_id
        self.context = context
        self.timeout = timeout
        self._session: requests.Session | None = None
        self._base: str | None = None

    # --------------------------------------------------------------------------------
    def base_url(self) -> str:
        if self._base is None:
            conn = self.get_connection(self.gateway_conn_id)
            host = conn.host or ""
            if not host.startswith("http"):
                # like Airflow's HttpHook: the Schema field is the protocol, http by default
                host = f"{conn.schema or 'http'}://{host}"
            if conn.port:
                host = f"{host.rstrip('/')}:{conn.port}"
            self._base = host.rstrip("/")
            self._token = conn.password
            extra = conn.extra_dejson or {}
            self._verify = extra.get("verify", True)
        return self._base

    def session(self) -> requests.Session:
        if self._session is None:
            base = self.base_url()
            s = requests.Session()
            s.headers.update({"Authorization": f"Bearer {self._token}", "User-Agent": "airflow-qlik-gateway/0.1"})
            s.headers.update(initiator_headers(self.context))
            s.verify = self._verify
            self._session = s
            self.log.info("Qlik Gateway: %s", base)
        return self._session

    def _call(self, method: str, path: str, *, retries: int = 3, accept_errors: tuple = (), **kwargs) -> dict:
        url = f"{self.base_url()}/api/v1{path}"
        for attempt in range(1, retries + 1):
            try:
                resp = self.session().request(method, url, timeout=self.timeout, **kwargs)
            except requests.RequestException as e:
                if attempt == retries:
                    raise AirflowException(f"Qlik Gateway unreachable: {e}") from e
                time.sleep(5 * attempt)
                continue
            if resp.status_code in (502, 503, 504) and attempt < retries:
                time.sleep(5 * attempt)
                continue
            if resp.status_code >= 400:
                try:
                    body = resp.json()
                    if body.get("error") in accept_errors:
                        return body  # an expected business answer, e.g. already_running
                    msg = f"{body.get('error')}: {body.get('message')}"
                except ValueError:
                    msg = resp.text[:500]
                raise AirflowException(f"Qlik Gateway {method} {path} -> HTTP {resp.status_code} {msg}")
            return resp.json()
        raise AirflowException("unreachable")

    # --- gateway actions -------------------------------------------------------------
    def whoami(self) -> dict:
        return self._call("GET", "/whoami")

    def get_task_info(self, qlik_task_id: str) -> dict:
        return self._call("GET", f"/tasks/{qlik_task_id}")

    def start_task(
        self,
        qlik_task_id: str,
        *,
        on_active: str | None = None,
        if_running: str | None = None,
        dedupe: bool | None = None,
        meta: dict | None = None,
    ) -> dict:
        """on_active: reuse | queue | reject | None (the gateway's default, reject); the gateway
        administrator may force another value. A rejected start returns {"error": "already_running", ...}.
        if_running / dedupe are the legacy names (attach=reuse, fresh/queue=queue, skip=reject)."""
        body: dict = {"meta": meta or {}}
        if on_active:
            body["on_active"] = on_active
        elif if_running:
            body["if_running"] = if_running
        elif dedupe is not None:
            body["dedupe"] = dedupe
        # POST is not retried blindly: a retry after a timeout could start the task twice
        # (identical requests collapse in the gateway anyway).
        return self._call(
            "POST",
            f"/tasks/{qlik_task_id}/start",
            retries=1,
            accept_errors=("already_running",),
            json=body,
        )

    def get_state(self, execution_id: int, wait: int = 0) -> dict:
        return self._call("GET", f"/executions/{execution_id}", params={"wait": wait})

    def get_details(self, execution_id: int) -> dict:
        return self._call("GET", f"/executions/{execution_id}/details")

    def get_log(self, execution_id: int) -> str:
        return self._call("GET", f"/executions/{execution_id}/log").get("log", "")

    def cancel(self, execution_id: int) -> dict:
        return self._call("POST", f"/executions/{execution_id}/cancel", retries=1)

    def wait_for_completion(self, execution_id: int, *, poll_interval: int = 30, timeout: float | None = None) -> dict:
        """Long-polls the gateway (cheap: the gateway answers from its own DB, not from Qlik)."""
        deadline = time.monotonic() + timeout if timeout else None
        while True:
            state = self.get_state(execution_id, wait=min(max(poll_interval, 1), 60))
            if state["status"] in TERMINAL:
                return state
            self.log.info("Qlik execution %s: %s", execution_id, state["status"])
            if deadline and time.monotonic() > deadline:
                raise AirflowException(f"Timed out waiting for Qlik execution {execution_id}")
