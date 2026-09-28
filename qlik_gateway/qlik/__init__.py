from ..config import Settings
from .base import CallHook, ExecutionInfo, QlikBackend, QlikError, TaskInfo, map_qlik_status

__all__ = ["CallHook", "ExecutionInfo", "QlikBackend", "QlikError", "TaskInfo", "map_qlik_status", "make_backend"]


def make_backend(settings: Settings, call_hook: CallHook | None = None) -> QlikBackend:
    if settings.qlik_mode == "mock":
        from .mock import get_mock

        return get_mock(call_hook)
    from .qrs import QrsJwtClient

    return QrsJwtClient(settings, call_hook)
