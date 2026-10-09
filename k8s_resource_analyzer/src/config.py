from dataclasses import dataclass, field
from typing import Dict, List, Optional
import yaml
import os


@dataclass
class PrometheusConfig:
    url: str = "http://prometheus:9090"
    lookback_days: int = 7
    step: str = "5m"
    # Load control: the window is queried in chunks of this size, one by one
    chunk_hours: int = 24
    pause_seconds: float = 1.0
    timeout: int = 120
    # False: ignore HTTP(S)_PROXY env vars (they usually can't reach the cluster)
    use_proxy: bool = False
    # Explicit proxy for this backend only, e.g. http://proxy.corp:3128 (wins over use_proxy)
    proxy_url: str = ""


@dataclass
class KubernetesConfig:
    # False: take requests/limits only from kube-state-metrics (no kubeconfig needed)
    enabled: bool = True
    in_cluster: bool = False
    kubeconfig: Optional[str] = None


@dataclass
class AnalysisConfig:
    namespaces: List[str] = field(default_factory=list)
    exclude_namespaces: List[str] = field(
        default_factory=lambda: ["kube-system", "kube-public", "kube-node-lease"]
    )
    cpu_request_buffer: float = 0.20
    memory_request_buffer: float = 0.20
    cpu_limit_buffer: float = 0.50
    memory_limit_buffer: float = 0.50
    exclude_containers: List[str] = field(default_factory=list)
    waste_threshold_ratio: float = 0.50
    min_waste_cpu_cores: float = 0.10
    min_waste_memory_mb: float = 100.0
    # CPU is compressible: a peak up to this much above the request is not a shortage
    cpu_under_tolerance: float = 0.20
    # kube-state-metrics label names of the Airflow pod labels
    # (empty values are fine: the columns stay blank)
    airflow_labels: Dict[str, str] = field(
        default_factory=lambda: {
            "dag_id": "label_dag_id",
            "task_id": "label_task_id",
            "run_id": "label_run_id",
            "try_number": "label_try_number",
        }
    )


@dataclass
class OutputConfig:
    format: str = "table"
    show_only_waste: bool = True
    sort_by: str = "memory_waste"


@dataclass
class EmailConfig:
    enabled: bool = False
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""  # prefer env var SMTP_PASSWORD
    use_tls: bool = True
    sender: str = ""
    recipients: List[str] = field(default_factory=list)
    subject: str = ""


@dataclass
class ClusterConfig:
    """One analysed cluster: its VictoriaMetrics/Prometheus URL and namespaces."""

    name: str = "default"
    # Basic auth (vmauth) can go into the URL: http://user:password@host/...
    url: str = ""
    namespaces: List[str] = field(default_factory=list)
    # Only when the backend is reachable through a proxy, e.g. http://proxy.corp:3128
    proxy: str = ""


@dataclass
class WebConfig:
    # Where the last report (report.json + report.xlsx) is kept between restarts
    data_dir: str = "./data"
    # Scheduled analysis; the UI never queries the backend itself
    refresh_interval_hours: float = 24.0
    # The "Refresh" button is ignored if the last run started less than N minutes ago
    min_manual_refresh_minutes: int = 10
    # Optional HTTP basic auth (prefer env WEB_USER / WEB_PASSWORD)
    auth_user: str = ""
    auth_password: str = ""
    # Allow adding/editing clusters from the UI (stored in <data_dir>/clusters.json)
    allow_edit_clusters: bool = True


@dataclass
class Config:
    prometheus: PrometheusConfig = field(default_factory=PrometheusConfig)
    kubernetes: KubernetesConfig = field(default_factory=KubernetesConfig)
    analysis: AnalysisConfig = field(default_factory=AnalysisConfig)
    output: OutputConfig = field(default_factory=OutputConfig)
    email: EmailConfig = field(default_factory=EmailConfig)
    web: WebConfig = field(default_factory=WebConfig)
    # Web UI: several clusters; empty → one cluster from prometheus.url + analysis.namespaces
    clusters: List[ClusterConfig] = field(default_factory=list)

    @classmethod
    def from_file(cls, path: str) -> "Config":
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        cfg = cls()
        if "prometheus" in data:
            cfg.prometheus = PrometheusConfig(**data["prometheus"])
        if "kubernetes" in data:
            cfg.kubernetes = KubernetesConfig(**data["kubernetes"])
        if "analysis" in data:
            cfg.analysis = AnalysisConfig(**data["analysis"])
        if "output" in data:
            cfg.output = OutputConfig(**data["output"])
        if "email" in data:
            cfg.email = EmailConfig(**data["email"])
        if "web" in data:
            cfg.web = WebConfig(**data["web"])
        if data.get("clusters"):
            cfg.clusters = [ClusterConfig(**c) for c in data["clusters"]]
        return cfg

    @classmethod
    def default(cls) -> "Config":
        return cls()
