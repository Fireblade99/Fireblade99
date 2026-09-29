"""In-process fake Qlik: lets you run the gateway, UI and Airflow DAGs without a Qlik cluster."""

import random
import threading
import uuid
from datetime import datetime, timedelta, timezone

from .base import CallHook, ExecutionInfo, QlikError, TaskInfo

DEMO_TASKS = [
    TaskInfo("11111111-1111-1111-1111-111111111111", "Reload Sales DWH", True, "a1", "Sales DWH", "Finance"),
    TaskInfo("22222222-2222-2222-2222-222222222222", "Reload HR Dashboard", True, "a2", "HR Dashboard", "HR"),
    TaskInfo("33333333-3333-3333-3333-333333333333", "Reload Logistics KPI", True, "a3", "Logistics KPI", "Ops"),
    TaskInfo("44444444-4444-4444-4444-444444444444", "Reload Risk (fails)", True, "a4", "Risk Model", "Risk"),
    TaskInfo("55555555-5555-5555-5555-555555555555", "Reload Marketing", False, "a5", "Marketing", "Sales"),
]
for _t in DEMO_TASKS:
    _t.custom_properties = {"ExternalRun": ["Yes"]}
DEMO_TASKS[0].custom_properties["GatewayClient"] = ["airflow-dwh"]
DEMO_TASKS[1].custom_properties["GatewayClient"] = ["airflow-dwh", "platform-ml"]


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class MockQlik:
    def __init__(self, call_hook: CallHook | None = None, min_duration: float = 5, max_duration: float = 25):
        self.call_hook = call_hook
        self.min_duration = min_duration
        self.max_duration = max_duration
        self._lock = threading.Lock()
        self.tasks = {t.id: t for t in DEMO_TASKS}
        # execution_id -> dict(task_id, start, duration, fail, aborted)
        self.executions: dict[str, dict] = {}
        self.calls: list[tuple[str, str]] = []

    def _hook(self, method: str, path: str, status: int | None = 200, err: str | None = None):
        self.calls.append((method, path))
        if self.call_hook:
            self.call_hook(method, path, status, random.uniform(5, 40), err)

    def start_task(self, task_id: str) -> str:
        path = f"/qrs/task/{task_id}/start/synchronous"
        task = self.tasks.get(task_id)
        if not task:
            self._hook("POST", path, 404, "not found")
            raise QlikError("Task not found", 404)
        with self._lock:
            eid = str(uuid.uuid4())
            self.executions[eid] = {
                "task_id": task_id,
                "start": _now(),
                "duration": random.uniform(self.min_duration, self.max_duration),
                "fail": "fail" in task.name.lower(),
                "aborted": False,
            }
        self._hook("POST", path)
        return eid

    def stop_task(self, task_id: str) -> None:
        with self._lock:
            for e in self.executions.values():
                if e["task_id"] == task_id and not e["aborted"]:
                    e["aborted"] = True
                    e["abort_at"] = _now()
        self._hook("POST", f"/qrs/task/{task_id}/stop")

    def _info(self, eid: str, e: dict) -> ExecutionInfo:
        now = _now()
        elapsed = (now - e["start"]).total_seconds()
        start = e["start"] + timedelta(seconds=1)
        stop = None
        if e["aborted"]:
            code, stop = 6, e["abort_at"]
        elif elapsed < 1:
            code, start = 1, None
        elif elapsed < e["duration"]:
            code = 2
        else:
            code = 8 if e["fail"] else 7
            stop = e["start"] + timedelta(seconds=e["duration"])
        details = [{"type": 2, "message": "Changing task state from NeverStarted to Triggered", "at": None}]
        if code in (6, 7, 8):
            msg = {7: "Reload finished successfully", 8: "Script error: Field not found <X>", 6: "Aborted by user"}[
                code
            ]
            details.append({"type": 2 if code == 7 else 3, "message": msg, "at": None})
        return ExecutionInfo(
            execution_id=eid,
            task_id=e["task_id"],
            status_code=code,
            start_time=start,
            stop_time=stop,
            node=f"qlik-sched-0{hash(eid) % 3 + 1}",
            details=details,
            script_log_ref=eid if code in (6, 7, 8) else None,
        )

    def get_execution_results(self, execution_ids: list[str]) -> dict[str, ExecutionInfo]:
        self._hook("GET", "/qrs/executionresult/full")
        with self._lock:
            return {eid: self._info(eid, self.executions[eid]) for eid in execution_ids if eid in self.executions}

    def list_reload_tasks(self) -> list[TaskInfo]:
        self._hook("GET", "/qrs/reloadtask/full")
        return list(self.tasks.values())

    def get_task(self, task_id: str) -> TaskInfo | None:
        self._hook("GET", f"/qrs/reloadtask/{task_id}")
        return self.tasks.get(task_id)

    def get_script_log(self, task_id: str, file_ref_id: str) -> str:
        self._hook("GET", f"/qrs/reloadtask/{task_id}/scriptlog")
        e = self.executions.get(file_ref_id)
        if not e:
            raise QlikError("Script log is not available", 404)
        lines = [
            f"{e['start']:%Y%m%dT%H%M%S} Execution started.",
            f"{e['start']:%Y%m%dT%H%M%S} LIB CONNECT TO 'dwh';",
            f"{e['start']:%Y%m%dT%H%M%S} Facts << sales 1 234 567 Lines fetched",
            "Script error: Field not found <X>" if e["fail"] else "Execution finished.",
        ]
        return "\n".join(lines)

    def engine_health(self, url: str) -> dict:
        self._hook("GET", url)
        running = sum(1 for e in self.executions.values() if (_now() - e["start"]).total_seconds() < e["duration"])
        return {
            "version": "12.1",
            "mem": {"committed": 20000 + running * 3000 + random.uniform(0, 500), "allocated": 25000, "free": 40000},
            "cpu": {"total": min(99.0, 5 + running * 20 + random.uniform(0, 5))},
            "session": {"active": random.randint(0, 5), "total": 10},
            "apps": {"loaded_docs": ["a"] * running, "active_docs": [], "in_memory_docs": []},
            "saturated": running > 3,
        }

    def close(self) -> None:
        pass


_singleton: MockQlik | None = None
_singleton_lock = threading.Lock()


def get_mock(call_hook: CallHook | None = None) -> MockQlik:
    """One fake cluster per process so the API and the embedded worker see the same state."""
    global _singleton
    with _singleton_lock:
        if _singleton is None:
            _singleton = MockQlik(call_hook)
        elif call_hook is not None:
            _singleton.call_hook = call_hook
        return _singleton


def reset_mock(**kwargs) -> MockQlik:
    global _singleton
    with _singleton_lock:
        _singleton = MockQlik(**kwargs)
        return _singleton
