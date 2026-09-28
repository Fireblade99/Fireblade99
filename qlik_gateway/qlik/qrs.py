"""QRS client that talks to Qlik Sense through a dedicated Virtual Proxy with JWT authentication.

Only the gateway holds the JWT signing key. External schedulers never get Qlik credentials,
so revoking their access is a matter of disabling their gateway token.
"""

import logging
import secrets
import string
import threading
import time
from datetime import datetime, timezone

import httpx
import jwt

from ..config import Settings
from .base import CallHook, ExecutionInfo, QlikError, TaskInfo

log = logging.getLogger(__name__)

_QLIK_NULL_DATE = "1753-01-01"
_BULK_CHUNK = 40  # executions per OR-filter, keeps the URL short


def _xrfkey() -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(16))


def parse_qlik_time(value: str | None) -> datetime | None:
    if not value or value.startswith(_QLIK_NULL_DATE):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def _task_from_json(t: dict) -> TaskInfo:
    app = t.get("app") or {}
    stream = app.get("stream") or {}
    cps: dict[str, list[str]] = {}
    for cp in t.get("customProperties") or []:
        name = (cp.get("definition") or {}).get("name")
        if name:
            cps.setdefault(name, []).append(cp.get("value"))
    return TaskInfo(
        id=t["id"],
        name=t.get("name", ""),
        enabled=bool(t.get("enabled", True)),
        app_id=app.get("id", ""),
        app_name=app.get("name", ""),
        stream_name=stream.get("name", "") if stream else "",
        custom_properties=cps,
        tags=[tag.get("name") for tag in t.get("tags") or [] if tag.get("name")],
    )


def _execution_from_json(r: dict) -> ExecutionInfo:
    ref = r.get("fileReferenceID")
    if ref == "00000000-0000-0000-0000-000000000000":
        ref = None
    return ExecutionInfo(
        execution_id=r.get("executionID") or r.get("id"),
        task_id=(r.get("taskID") or ""),
        status_code=int(r.get("status", 0)),
        start_time=parse_qlik_time(r.get("startTime")),
        stop_time=parse_qlik_time(r.get("stopTime")),
        node=r.get("executingNodeName") or None,
        details=[
            {
                "type": d.get("detailsType"),
                "message": d.get("message"),
                "at": d.get("detailCreatedDate"),
            }
            for d in (r.get("details") or [])
        ],
        script_log_ref=ref if r.get("scriptLogAvailable") else None,
    )


class QrsJwtClient:
    def __init__(
        self, settings: Settings, call_hook: CallHook | None = None, transport: httpx.BaseTransport | None = None
    ):
        self.s = settings
        self.call_hook = call_hook
        with open(settings.qlik_jwt_private_key_path, "rb") as f:
            self._private_key = f.read()
        self._jwt: str | None = None
        self._jwt_exp = 0.0
        self._lock = threading.Lock()
        # The cookie jar keeps the proxy session, so Qlik does not create a new session per call.
        self._http = httpx.Client(
            base_url=settings.qlik_base_url.rstrip("/"),
            verify=settings.qlik_verify_ssl,
            timeout=settings.qlik_timeout_seconds,
            headers={"User-Agent": "qlik-gateway/0.1"},
            transport=transport,
        )

    # --- auth -------------------------------------------------------------------
    def _token(self) -> str:
        with self._lock:
            now = time.time()
            if self._jwt is None or now > self._jwt_exp - 30:
                ttl = self.s.qlik_jwt_ttl_seconds
                claims = {
                    self.s.qlik_jwt_user_id_attr: self.s.qlik_jwt_user_id,
                    self.s.qlik_jwt_user_directory_attr: self.s.qlik_jwt_user_directory,
                    "iat": int(now),
                    "exp": int(now + ttl),
                }
                if self.s.qlik_jwt_audience:
                    claims["aud"] = self.s.qlik_jwt_audience
                self._jwt = jwt.encode(claims, self._private_key, algorithm=self.s.qlik_jwt_algorithm)
                self._jwt_exp = now + ttl
            return self._jwt

    def _request(self, method: str, path: str, *, params: dict | None = None, json=None) -> httpx.Response:
        key = _xrfkey()
        params = dict(params or {})
        params["xrfkey"] = key
        headers = {"X-Qlik-Xrfkey": key, "Authorization": f"Bearer {self._token()}"}
        t0 = time.perf_counter()
        status = None
        err = None
        try:
            resp = self._http.request(method, path, params=params, json=json, headers=headers)
            status = resp.status_code
            if resp.status_code >= 400:
                err = f"HTTP {resp.status_code}: {resp.text[:500]}"
                raise QlikError(err, resp.status_code)
            return resp
        except httpx.HTTPError as e:
            err = f"{type(e).__name__}: {e}"
            raise QlikError(err) from e
        finally:
            latency = (time.perf_counter() - t0) * 1000
            if self.call_hook:
                try:
                    self.call_hook(method, path, status, latency, err)
                except Exception:  # never break a Qlik call because of auditing
                    log.exception("call hook failed")

    # --- QRS operations ------------------------------------------------------------
    def start_task(self, task_id: str) -> str:
        resp = self._request("POST", f"/qrs/task/{task_id}/start/synchronous")
        value = (resp.json() or {}).get("value")
        if not value:
            raise QlikError(f"Qlik did not return an execution id: {resp.text[:300]}")
        return value

    def stop_task(self, task_id: str) -> None:
        self._request("POST", f"/qrs/task/{task_id}/stop")

    def get_execution_results(self, execution_ids: list[str]) -> dict[str, ExecutionInfo]:
        out: dict[str, ExecutionInfo] = {}
        for i in range(0, len(execution_ids), _BULK_CHUNK):
            chunk = execution_ids[i : i + _BULK_CHUNK]
            flt = " or ".join(f"executionID eq {eid}" for eid in chunk)
            resp = self._request("GET", "/qrs/executionresult/full", params={"filter": flt})
            for r in resp.json() or []:
                info = _execution_from_json(r)
                out[info.execution_id] = info
        return out

    def list_reload_tasks(self) -> list[TaskInfo]:
        """Reload tasks available through the gateway.

        A task qualifies if its app (vendor recommendation) or the task itself carries the
        custom property, e.g. Source=Airflow. Without a configured property every task is listed.
        """
        name, value = self.s.qlik_task_custom_property, self.s.qlik_task_custom_property_value
        resp = self._request("GET", "/qrs/reloadtask/full")
        tasks = [_task_from_json(t) for t in resp.json() or []]
        if not (name and value):
            return tasks
        flt = f"customProperties.definition.name eq '{name}' and customProperties.value eq '{value}'"
        apps = self._request("GET", "/qrs/app/full", params={"filter": flt}).json() or []
        # QRS matches name and value independently, so re-check the pair
        marked_apps = {
            a["id"]
            for a in apps
            if any(
                (cp.get("definition") or {}).get("name") == name and cp.get("value") == value
                for cp in a.get("customProperties") or []
            )
        }
        return [t for t in tasks if t.app_id in marked_apps or value in t.custom_properties.get(name, [])]

    def get_task(self, task_id: str) -> TaskInfo | None:
        try:
            resp = self._request("GET", f"/qrs/reloadtask/{task_id}")
        except QlikError as e:
            if e.status_code == 404:
                return None
            raise
        return _task_from_json(resp.json())

    def get_script_log(self, task_id: str, file_ref_id: str) -> str:
        resp = self._request("GET", f"/qrs/reloadtask/{task_id}/scriptlog", params={"fileReferenceId": file_ref_id})
        ref = (resp.json() or {}).get("value")
        if not ref:
            raise QlikError("Script log is not available")
        resp = self._request("GET", f"/qrs/download/reloadtask/{ref}/scriptlog.txt")
        return resp.content.decode("utf-8", errors="replace")

    def engine_health(self, url: str) -> dict:
        # Absolute URL of a node's engine health check behind the same virtual proxy.
        key = _xrfkey()
        t0 = time.perf_counter()
        status = None
        err = None
        try:
            resp = self._http.get(
                url,
                params={"xrfkey": key},
                headers={"X-Qlik-Xrfkey": key, "Authorization": f"Bearer {self._token()}"},
            )
            status = resp.status_code
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPError as e:
            err = str(e)
            raise QlikError(err, status) from e
        finally:
            if self.call_hook:
                self.call_hook("GET", url, status, (time.perf_counter() - t0) * 1000, err)

    def close(self) -> None:
        self._http.close()
