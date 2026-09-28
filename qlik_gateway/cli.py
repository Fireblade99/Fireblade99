"""qlik-gateway CLI: run the API / coordinator, manage admins and clients."""

import argparse
import getpass
import sys

from sqlalchemy import select

from .config import get_settings
from .db import init_engine, session_scope
from .models import AdminUser, Client
from .security import generate_client_token, hash_password
from .services.audit import audit


def cmd_api(args) -> None:
    import uvicorn

    uvicorn.run("qlik_gateway.main:app_factory", factory=True, host=args.host, port=args.port, workers=args.workers)


def cmd_worker(args) -> None:
    from .worker import main

    main()


def cmd_create_admin(args) -> None:
    password = args.password or getpass.getpass("Password: ")
    with session_scope() as db:
        user = db.scalars(select(AdminUser).where(AdminUser.username == args.username)).first()
        if user:
            user.password_hash = hash_password(password)
            user.enabled = True
            print(f"admin '{args.username}' password updated")
        else:
            db.add(AdminUser(username=args.username, password_hash=hash_password(password)))
            print(f"admin '{args.username}' created")
        audit(db, actor_type="admin", actor="cli", action="admin.create", message=args.username)


def cmd_create_client(args) -> None:
    with session_scope() as db:
        if db.scalars(select(Client).where(Client.name == args.name)).first():
            sys.exit(f"client '{args.name}' already exists")
        token, prefix, token_hash = generate_client_token()
        c = Client(
            name=args.name,
            description=args.description,
            owner_contact=args.owner,
            token_prefix=prefix,
            token_hash=token_hash,
            allowed_actions=args.actions.split(","),
            allowed_tasks=args.tasks.split(",") if args.tasks else [],
        )
        db.add(c)
        db.flush()
        audit(db, actor_type="admin", actor="cli", action="client.create", client_id=c.id, meta={"name": c.name})
    print(f"client '{args.name}' created. Token (shown once, store it in an Airflow connection):\n{token}")


def main() -> None:
    p = argparse.ArgumentParser(prog="qlik-gateway")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("api", help="run the HTTP API + admin UI")
    a.add_argument("--host", default="0.0.0.0")
    a.add_argument("--port", type=int, default=8080)
    a.add_argument("--workers", type=int, default=2)
    a.set_defaults(fn=cmd_api)

    w = sub.add_parser("worker", help="run the coordinator (dispatch + bulk polling of Qlik)")
    w.set_defaults(fn=cmd_worker)

    ad = sub.add_parser("create-admin", help="create an admin UI user or reset its password")
    ad.add_argument("username")
    ad.add_argument("--password")
    ad.set_defaults(fn=cmd_create_admin)

    c = sub.add_parser("create-client", help="register an API client and print its token")
    c.add_argument("name")
    c.add_argument("--description", default="")
    c.add_argument("--owner", default="", help="contact of the owning team")
    c.add_argument("--actions", default="start,state,details,info")
    c.add_argument("--tasks", default="", help="comma separated Qlik task ids, or *")
    c.set_defaults(fn=cmd_create_client)

    args = p.parse_args()
    if args.cmd not in ("api", "worker"):
        init_engine(get_settings().database_url)
    args.fn(args)


if __name__ == "__main__":
    main()
