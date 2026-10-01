from sqlalchemy.orm import Session

from ..models import KV

DISPATCH_PAUSED = "dispatch_paused"


def get_value(db: Session, key: str, default=None):
    row = db.get(KV, key)
    return row.value.get("v", default) if row else default


def set_value(db: Session, key: str, value) -> None:
    row = db.get(KV, key)
    if row is None:
        db.add(KV(key=key, value={"v": value}))
    else:
        row.value = {"v": value}
    db.flush()


def dispatch_paused(db: Session) -> dict | None:
    """Returns {"by":..., "reason":..., "at":...} if dispatching to Qlik is paused."""
    return get_value(db, DISPATCH_PAUSED)
