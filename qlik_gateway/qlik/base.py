from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

# QRS ExecutionResult / ExecutionSession status codes
QLIK_STATUS = {
    0: "NeverStarted",
    1: "Triggered",
    2: "Started",
    3: "Queued",
    4: "AbortInitiated",
    5: "Aborting",
    6: "Aborted",
    7: "FinishedSuccess",
    8: "FinishedFail",
    9: "Skipped",
    10: "Retry",
    11: "Error",
    12: "Reset",
}


def map_qlik_status(code: int) -> str:
    from ..models import ExecStatus

    if code in (0, 1, 3):
        return ExecStatus.STARTING
    if code in (2, 4, 5, 10):
        return ExecStatus.RUNNING
    if code in (6, 12):
        return ExecStatus.ABORTED
    if code == 7:
        return ExecStatus.SUCCESS
    if code in (8, 11):
        return ExecStatus.FAILED
    if code == 9:
        return ExecStatus.SKIPPED
    return ExecStatus.RUNNING


@dataclass
class TaskInfo:
    id: str
    name: str
    enabled: bool = True
    app_id: str = ""
    app_name: str = ""
    stream_name: str = ""
    custom_properties: dict = field(default_factory=dict)
    tags: list = field(default_factory=list)


@dataclass
class ExecutionInfo:
    execution_id: str
    task_id: str
    status_code: int
    start_time: datetime | None = None
    stop_time: datetime | None = None
    node: str | None = None
    details: list = field(default_factory=list)
    script_log_ref: str | None = None

    @property
    def status_text(self) -> str:
        return QLIK_STATUS.get(self.status_code, str(self.status_code))


# (method, path, http_status | None, latency_ms, error | None)
CallHook = Callable[[str, str, int | None, float, str | None], None]


class QlikError(Exception):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class QlikBackend(Protocol):
    call_hook: CallHook | None

    def start_task(self, task_id: str) -> str: ...

    def stop_task(self, task_id: str) -> None: ...

    def get_execution_results(self, execution_ids: list[str]) -> dict[str, ExecutionInfo]: ...

    def list_reload_tasks(self) -> list[TaskInfo]: ...

    def get_task(self, task_id: str) -> TaskInfo | None: ...

    def get_script_log(self, task_id: str, file_ref_id: str) -> str: ...

    def engine_health(self, url: str) -> dict: ...

    def close(self) -> None: ...
