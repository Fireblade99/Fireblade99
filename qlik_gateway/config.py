"""Service configuration (environment variables with prefix QGW_)."""

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="QGW_", env_file=".env", extra="ignore")

    # --- storage -----------------------------------------------------------
    database_url: str = "sqlite:///./qlik_gateway.db"

    # --- web / admin UI ------------------------------------------------------
    secret_key: str = Field("change-me", description="Signs admin UI session cookies")
    session_https_only: bool = False
    # Bootstrap admin, created on startup if no admins exist yet.
    bootstrap_admin_user: str | None = None
    bootstrap_admin_password: str | None = None

    # --- UI login with Active Directory (empty ldap_url = local accounts only) ---
    # ldaps://dc01.hq.local:636 (several, comma separated, are tried in turn)
    ldap_url: str = ""
    # NetBIOS domain: the user logs in as HQ\login (or just login); empty = the login is used as typed
    ldap_domain: str = ""
    ldap_base_dn: str = ""  # where users and groups are searched, e.g. DC=hq,DC=local
    ldap_start_tls: bool = False  # for ldap:// on port 389
    ldap_verify_ssl: str = "system"  # system (Windows store) / false / path to a CA .pem
    ldap_timeout_seconds: int = 5
    # AD groups (CN, comma separated) -> roles; team groups are set on each client's card in the UI
    ldap_admin_groups: str = "QGW-Admins"
    ldap_viewer_groups: str = "QGW-Viewers"
    # Time zone of the admin UI (display and date inputs), as a fixed offset from UTC in hours.
    # The database and the client API always use UTC.
    ui_utc_offset_hours: float = 5.0
    # Trust X-Forwarded-For from a reverse proxy when recording caller IPs.
    trust_forwarded_for: bool = False

    # --- Qlik connection -------------------------------------------------------
    # "jwt"  - QRS via a dedicated Virtual Proxy with JWT authentication (recommended by vendor)
    # "mock" - in-process fake Qlik for development, demos and tests
    qlik_mode: Literal["jwt", "mock"] = "mock"
    # reload duration range of the fake Qlik, seconds
    mock_min_duration: float = 5
    mock_max_duration: float = 25
    # Base URL of the virtual proxy, e.g. https://qlik.company.local/airflowgw
    qlik_base_url: str = "https://qlik.local/airflowgw"
    # TLS verification of the Qlik certificate:
    #   system - trust the OS certificate store (Windows store: corporate CAs work out of the box)
    #   true   - Python's bundled public CAs (certifi)
    #   false  - no verification (only for a first test)
    #   <path> - PEM file with the CA certificate(s)
    qlik_verify_ssl: str = "system"
    qlik_timeout_seconds: float = 30.0
    # JWT the gateway signs for its own service account (Airflow never sees it).
    qlik_jwt_private_key_path: str = "./secrets/qlik_jwt_private.pem"
    qlik_jwt_algorithm: str = "RS256"
    qlik_jwt_user_id: str = "svc_qlik_gateway"
    qlik_jwt_user_directory: str = "INTERNAL"
    # Attribute names configured in the virtual proxy ("JWT attribute for user ID/user directory").
    qlik_jwt_user_id_attr: str = "userId"
    qlik_jwt_user_directory_attr: str = "userDirectory"
    qlik_jwt_audience: str | None = None
    qlik_jwt_ttl_seconds: int = 300
    # Perimeter: only tasks whose app (or the task itself) has this custom property value are
    # synced to the catalog and can be run through the gateway (e.g. ExternalRun=Yes).
    qlik_task_custom_property: str | None = "ExternalRun"
    qlik_task_custom_property_value: str | None = "Yes"
    # Access per client managed in QMC: a client may run tasks of apps whose property contains the
    # client's name (e.g. GatewayClient=airflow-dwh). Empty = access only via the gateway UI.
    qlik_client_custom_property: str | None = "GatewayClient"

    # --- coordinator (worker) -----------------------------------------------
    embedded_worker: bool = False  # run the worker loop inside the API process (dev only)
    dispatch_interval_seconds: float = 3.0
    # How often the gateway asks Qlik about running executions. ONE bulk request per tick
    # for all active executions, no matter how many clients are waiting.
    poll_interval_seconds: float = 20.0
    catalog_sync_interval_seconds: float = 600.0
    node_health_interval_seconds: float = 60.0
    # Engine health check URLs of the dedicated nodes (optional), comma separated.
    node_health_urls: str = ""
    # Global limit of concurrently running reloads started through the gateway.
    max_concurrent_executions: int = 3
    # An execution that Qlik never reports on is marked LOST after this time.
    lost_after_seconds: int = 900
    # Hard cap for an execution; after it the gateway only marks it TIMEOUT (does not stop the reload).
    execution_timeout_seconds: int = 6 * 3600
    worker_lease_seconds: int = 30
    audit_retention_days: int = 90

    # --- client API ---------------------------------------------------------
    long_poll_max_seconds: int = 60
    # What a start request does while the task is reloading in Qlik, unless the request or an
    # administrator's setting on the task/client says otherwise (see models.ON_ACTIVE).
    default_on_active: Literal["reuse", "queue", "reject"] = "reject"
    # Base URL for links to execution pages in API answers (e.g. http://qse-app10-wp1:8080).
    # Empty = the address the client called the gateway with.
    public_url: str = ""
    default_requests_per_minute: int = 60
    default_starts_per_hour: int = 30
    default_max_concurrent: int = 2


@lru_cache
def get_settings() -> Settings:
    return Settings()
