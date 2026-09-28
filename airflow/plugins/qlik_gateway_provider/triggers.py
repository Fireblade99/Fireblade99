import asyncio
from collections.abc import AsyncIterator
from typing import Any

import httpx

try:
    from airflow.triggers.base import BaseTrigger, TriggerEvent
except ImportError:  # pragma: no cover
    from airflow.sdk.bases.trigger import BaseTrigger, TriggerEvent  # type: ignore

from .hooks import TERMINAL, QlikGatewayHook


class QlikGatewayExecutionTrigger(BaseTrigger):
    """Waits for a gateway execution in the triggerer: no worker slot is held while Qlik reloads."""

    def __init__(self, execution_id: int, gateway_conn_id: str, poll_interval: int = 30):
        super().__init__()
        self.execution_id = execution_id
        self.gateway_conn_id = gateway_conn_id  # the token is read from the connection, never serialized
        self.poll_interval = poll_interval

    def serialize(self) -> tuple[str, dict[str, Any]]:
        return (
            f"{self.__class__.__module__}.{self.__class__.__qualname__}",
            {
                "execution_id": self.execution_id,
                "gateway_conn_id": self.gateway_conn_id,
                "poll_interval": self.poll_interval,
            },
        )

    async def run(self) -> AsyncIterator[TriggerEvent]:
        hook = QlikGatewayHook(self.gateway_conn_id)
        base_url = await asyncio.to_thread(hook.base_url)
        url = f"{base_url}/api/v1/executions/{self.execution_id}"
        wait = min(max(self.poll_interval, 1), 60)
        async with httpx.AsyncClient(
            verify=hook._verify, timeout=wait + 30, headers={"Authorization": f"Bearer {hook._token}"}
        ) as client:
            errors = 0
            while True:
                try:
                    resp = await client.get(url, params={"wait": wait})
                    if resp.status_code >= 400 and resp.status_code not in (429, 502, 503, 504):
                        yield TriggerEvent(
                            {"status": "ERROR", "message": f"HTTP {resp.status_code}: {resp.text[:300]}"}
                        )
                        return
                    if resp.status_code < 400:
                        errors = 0
                        state = resp.json()
                        if state["status"] in TERMINAL:
                            yield TriggerEvent(state)
                            return
                except httpx.HTTPError as e:
                    errors += 1
                    if errors >= 10:
                        yield TriggerEvent({"status": "ERROR", "message": f"Gateway unreachable: {e}"})
                        return
                await asyncio.sleep(5)
