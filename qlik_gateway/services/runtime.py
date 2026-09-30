"""Operational parameters an admin can change in the UI without restarting.

Values saved in the UI (KV table) override the ones from .env; the coordinator re-reads them
on every tick.
"""

from dataclasses import dataclass

from sqlalchemy.orm import Session

from ..config import Settings
from .kv import get_value, set_value

KEY = "runtime_settings"

# name -> (min, max)
LIMITS = {
    "max_concurrent_executions": (1, 100),
    "poll_interval_seconds": (5, 600),
}


@dataclass
class Runtime:
    max_concurrent_executions: int
    poll_interval_seconds: float
    overridden: dict


def effective(db: Session, settings: Settings) -> Runtime:
    saved = get_value(db, KEY, {}) or {}
    return Runtime(
        max_concurrent_executions=int(saved.get("max_concurrent_executions", settings.max_concurrent_executions)),
        poll_interval_seconds=float(saved.get("poll_interval_seconds", settings.poll_interval_seconds)),
        overridden=saved,
    )


def save(db: Session, values: dict) -> dict:
    """Validates and stores values; returns what was stored. Raises ValueError on bad input."""
    saved = dict(get_value(db, KEY, {}) or {})
    for name, raw in values.items():
        lo, hi = LIMITS[name]
        v = float(raw)
        if not lo <= v <= hi:
            raise ValueError(f"{name}: допустимо от {lo} до {hi}")
        saved[name] = int(v) if name == "max_concurrent_executions" else v
    set_value(db, KEY, saved)
    return saved


def reset(db: Session) -> None:
    set_value(db, KEY, {})
