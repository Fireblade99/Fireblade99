from dataclasses import dataclass, field
from typing import List, Optional
import yaml
import os


@dataclass
class PrometheusConfig:
    url: str = "http://prometheus:9090"
    lookback_days: int = 7
    step: str = "5m"


@dataclass
class KubernetesConfig:
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
class Config:
    prometheus: PrometheusConfig = field(default_factory=PrometheusConfig)
    kubernetes: KubernetesConfig = field(default_factory=KubernetesConfig)
    analysis: AnalysisConfig = field(default_factory=AnalysisConfig)
    output: OutputConfig = field(default_factory=OutputConfig)
    email: EmailConfig = field(default_factory=EmailConfig)

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
        return cfg

    @classmethod
    def default(cls) -> "Config":
        return cls()
