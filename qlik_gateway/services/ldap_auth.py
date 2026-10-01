"""Login to the UI with an Active Directory account (LDAP bind) and roles from AD groups.

The user's own password is checked by binding to AD as that user; no service account is needed.
Group membership is read with the AD "in chain" rule, so nested groups count too.
"""

import logging
import ssl
from dataclasses import dataclass, field

from ..config import Settings

log = logging.getLogger(__name__)

# AD: all groups the user belongs to, directly or through nested groups
_IN_CHAIN = "1.2.840.113556.1.4.1941"


class LdapUnavailable(Exception):
    """AD could not be reached or answered with an error (not a wrong password)."""


@dataclass
class LdapUser:
    username: str  # sAMAccountName, lower case
    display_name: str = ""
    groups: list[str] = field(default_factory=list)  # CN of every group, nested included


def enabled(settings: Settings) -> bool:
    return bool(settings.ldap_url.strip())


def split_login(login: str) -> str:
    """'HQ\\ivanov', 'ivanov@hq.local' and 'ivanov' all mean sAMAccountName 'ivanov'."""
    login = login.strip()
    if "\\" in login:
        login = login.split("\\", 1)[1]
    if "@" in login:
        login = login.split("@", 1)[0]
    return login.lower()


def _tls(settings: Settings):
    from ldap3 import Tls

    v = settings.ldap_verify_ssl.strip()
    if v.lower() in ("false", "0", "no"):
        return Tls(validate=ssl.CERT_NONE)
    if v.lower() in ("system", "true", "1", "yes", ""):
        return Tls(validate=ssl.CERT_REQUIRED)  # Windows: the machine's certificate store
    return Tls(validate=ssl.CERT_REQUIRED, ca_certs_file=v)


def authenticate(settings: Settings, login: str, password: str) -> LdapUser | None:
    """Returns the user, or None for a wrong login/password. Raises LdapUnavailable if AD is down."""
    from ldap3 import NONE, SUBTREE, Connection, Server, ServerPool
    from ldap3.core.exceptions import LDAPBindError, LDAPException
    from ldap3.utils.conv import escape_filter_chars

    sam = split_login(login)
    # an empty password would be an anonymous bind, which AD accepts: never treat it as a login
    if not sam or not password:
        return None
    bind_user = f"{settings.ldap_domain}\\{sam}" if settings.ldap_domain else login.strip()
    tls = _tls(settings)
    servers = [
        Server(
            u.strip(),
            use_ssl=u.strip().lower().startswith("ldaps://"),
            tls=tls,
            get_info=NONE,
            connect_timeout=settings.ldap_timeout_seconds,
        )
        for u in settings.ldap_url.split(",")
        if u.strip()
    ]
    pool = ServerPool(servers, active=1, exhaust=True) if len(servers) > 1 else servers[0]
    try:
        conn = Connection(pool, user=bind_user, password=password, receive_timeout=settings.ldap_timeout_seconds)
        if settings.ldap_start_tls:
            conn.open()
            conn.start_tls()
        if not conn.bind():
            # 49 = invalidCredentials (wrong password, locked or disabled account)
            if conn.result.get("result") == 49:
                return None
            raise LdapUnavailable(f"bind failed: {conn.result.get('description')} {conn.result.get('message')}")
    except LDAPBindError:
        return None
    except LDAPException as e:
        raise LdapUnavailable(str(e)) from e

    try:
        conn.search(
            settings.ldap_base_dn,
            f"(&(objectClass=user)(sAMAccountName={escape_filter_chars(sam)}))",
            SUBTREE,
            attributes=["displayName", "memberOf"],
        )
        if not conn.entries:
            log.warning("AD user %s authenticated but not found under %s", sam, settings.ldap_base_dn)
            return None
        entry = conn.entries[0]
        dn = entry.entry_dn
        display = str(entry.displayName.value) if "displayName" in entry and entry.displayName.value else sam
        direct = [_cn(g) for g in (entry.memberOf.values if "memberOf" in entry else [])]
        try:
            conn.search(
                settings.ldap_base_dn,
                f"(&(objectClass=group)(member:{_IN_CHAIN}:={escape_filter_chars(dn)}))",
                SUBTREE,
                attributes=["cn"],
            )
            groups = sorted({str(g.cn) for g in conn.entries} | set(direct))
        except LDAPException as e:  # not AD (no "in chain" rule): direct groups only
            log.warning("nested group lookup failed (%s), using memberOf", e)
            groups = sorted(set(direct))
    except LDAPException as e:
        raise LdapUnavailable(str(e)) from e
    finally:
        conn.unbind()
    return LdapUser(username=sam, display_name=display, groups=groups)


def _cn(dn: str) -> str:
    """'CN=QGW-Admins,OU=Groups,DC=hq,DC=local' -> 'QGW-Admins'."""
    first = str(dn).split(",", 1)[0]
    return first.split("=", 1)[1] if "=" in first else first


def _names(raw: str) -> set[str]:
    return {x.strip().lower() for x in raw.replace(";", ",").split(",") if x.strip()}


def resolve_role(settings: Settings, groups: list[str], clients: list) -> tuple[str | None, list[int]]:
    """Role from AD groups: admin > viewer > team (clients whose team groups the user is in)."""
    mine = {g.lower() for g in groups}
    if mine & _names(settings.ldap_admin_groups):
        return "admin", []
    if mine & _names(settings.ldap_viewer_groups):
        return "viewer", []
    team = [c.id for c in clients if mine & {g.lower() for g in (c.ui_groups or [])}]
    if team:
        return "team", team
    return None, []
