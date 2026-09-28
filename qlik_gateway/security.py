"""Client API tokens and admin passwords."""

import base64
import hashlib
import hmac
import ipaddress
import secrets

TOKEN_PREFIX = "qgw"


def generate_client_token() -> tuple[str, str, str]:
    """Return (full_token, lookup_prefix, hash). Only the hash is stored."""
    lookup = secrets.token_hex(6)
    secret = secrets.token_urlsafe(32)
    token = f"{TOKEN_PREFIX}_{lookup}_{secret}"
    return token, lookup, hash_token(token)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def parse_token_prefix(token: str) -> str | None:
    parts = token.split("_", 2)
    if len(parts) != 3 or parts[0] != TOKEN_PREFIX:
        return None
    return parts[1]


def token_matches(token: str, stored_hash: str) -> bool:
    return hmac.compare_digest(hash_token(token), stored_hash)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(dk).decode()


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt_b64, dk_b64 = stored.split("$")
    except ValueError:
        return False
    if scheme != "scrypt":
        return False
    dk = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt_b64), n=2**14, r=8, p=1)
    return hmac.compare_digest(dk, base64.b64decode(dk_b64))


def ip_allowed(ip: str | None, allowed: list[str]) -> bool:
    if not allowed:
        return True
    if not ip:
        return False
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for entry in allowed:
        try:
            if addr in ipaddress.ip_network(entry.strip(), strict=False):
                return True
        except ValueError:
            continue
    return False
