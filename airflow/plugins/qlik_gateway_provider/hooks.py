import socket
import time

import requests

from ._compat import AirflowException, BaseHook

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
                host = f"{conn.schema or 'https'}://{host}"
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

    def _call(self, method: str, path: str, *, retries: int = 3, **kwargs) -> dict:
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

    def start_task(self, qlik_task_id: str, *, dedupe: bool = True, meta: dict | None = None) -> dict:
        # POST is not retried blindly: a retry after a timeout could start the task twice
        # (the gateway dedupes by default anyway).
        return self._call(
            "POST", f"/tasks/{qlik_task_id}/start", retries=1, json={"dedupe": dedupe, "meta": meta or {}}
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
