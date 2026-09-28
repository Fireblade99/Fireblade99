from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..models import Client, utcnow
from ..security import ip_allowed, parse_token_prefix, token_matches
from ..services.errors import ServiceError
from ..services.ratelimit import limiter


def client_ip(request: Request) -> str | None:
    if get_settings().trust_forwarded_for:
        fwd = request.headers.get("x-forwarded-for")
        if fwd:
            return fwd.split(",")[0].strip()
    return request.client.host if request.client else None


def initiator_meta(request: Request) -> dict:
    """Who is behind the call: X-Airflow-* / X-Initiator-* headers plus the user agent."""
    meta = {}
    for k, v in request.headers.items():
        lk = k.lower()
        if lk.startswith("x-airflow-") or lk.startswith("x-initiator-"):
            meta[lk.split("-", 2)[2].replace("-", "_")] = v[:300]
    return meta


def current_client(request: Request, db: Session = Depends(get_db)) -> Client:
    request.state.audit = {}
    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else request.headers.get("x-api-key", "").strip()
    if not token:
        raise ServiceError(401, "no_token", "Missing API token (Authorization: Bearer <token>)")
    prefix = parse_token_prefix(token)
    client = db.scalars(select(Client).where(Client.token_prefix == prefix)).first() if prefix else None
    if client is None or not token_matches(token, client.token_hash):
        raise ServiceError(401, "bad_token", "Invalid API token")
    # plain values: the ORM object is expired if the request's transaction is rolled back
    request.state.client = {"id": client.id, "name": client.name}
    if not client.enabled:
        raise ServiceError(403, "client_blocked", f"Client is blocked: {client.blocked_reason or '-'}")
    if client.token_expires_at and client.token_expires_at < utcnow():
        raise ServiceError(401, "token_expired", "API token expired")
    ip = client_ip(request)
    if not ip_allowed(ip, client.allowed_ips or []):
        raise ServiceError(403, "ip_forbidden", f"Calls from {ip} are not allowed for this client")
    if not limiter.hit(client.id, client.requests_per_minute):
        raise ServiceError(429, "rate_limited", f"Rate limit {client.requests_per_minute} requests/minute exceeded")
    client.last_seen_at = utcnow()
    client.last_seen_ip = ip
    db.commit()  # keep the request transaction short; the endpoint starts a fresh one
    return client


def get_backend(request: Request):
    return request.app.state.backend
