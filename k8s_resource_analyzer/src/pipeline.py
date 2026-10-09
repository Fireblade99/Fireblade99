"""
Analysis pipeline shared by the CLI (main.py) and the web UI:
Prometheus/VictoriaMetrics → workload groups → recommendations.
"""

from typing import List

from .config import Config
from .core.analyzer import ResourceAnalyzer
from .core.recommender import Recommendation, Recommender


def build_recommender(config: Config) -> Recommender:
    a = config.analysis
    return Recommender(
        cpu_request_buffer=a.cpu_request_buffer,
        memory_request_buffer=a.memory_request_buffer,
        cpu_limit_buffer=a.cpu_limit_buffer,
        memory_limit_buffer=a.memory_limit_buffer,
        waste_threshold_ratio=a.waste_threshold_ratio,
        min_waste_cpu_cores=a.min_waste_cpu_cores,
        min_waste_memory_mb=a.min_waste_memory_mb,
        cpu_under_tolerance=a.cpu_under_tolerance,
        memory_min_headroom=a.memory_min_headroom,
        limit_saturation_ratio=a.limit_saturation_ratio,
    )


def run_analysis(config: Config) -> List[Recommendation]:
    """Full run; raises RuntimeError when the backend cannot be queried."""
    groups = ResourceAnalyzer(config).analyze()
    return build_recommender(config).process_all(groups)
