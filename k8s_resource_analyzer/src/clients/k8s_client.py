import logging
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from kubernetes import client, config as k8s_config
from kubernetes.client.exceptions import ApiException

logger = logging.getLogger(__name__)


@dataclass
class ContainerResources:
    namespace: str
    pod_name: str
    container_name: str
    cpu_request: Optional[float]    # cores
    memory_request: Optional[float]  # bytes
    cpu_limit: Optional[float]       # cores
    memory_limit: Optional[float]    # bytes


def _parse_cpu(value: Optional[str]) -> Optional[float]:
    """Parse a Kubernetes CPU string to float cores."""
    if not value:
        return None
    value = value.strip()
    if value.endswith("m"):
        return float(value[:-1]) / 1000.0
    return float(value)


def _parse_memory(value: Optional[str]) -> Optional[float]:
    """Parse a Kubernetes memory string to float bytes."""
    if not value:
        return None
    value = value.strip()
    units: Dict[str, float] = {
        "Ki": 1024.0,
        "Mi": 1024.0 ** 2,
        "Gi": 1024.0 ** 3,
        "Ti": 1024.0 ** 4,
        "Pi": 1024.0 ** 5,
        "K":  1000.0,
        "M":  1000.0 ** 2,
        "G":  1000.0 ** 3,
        "T":  1000.0 ** 4,
    }
    for suffix, multiplier in units.items():
        if value.endswith(suffix):
            return float(value[: -len(suffix)]) * multiplier
    return float(value)


class K8sClient:
    def __init__(
        self,
        in_cluster: bool = False,
        kubeconfig: Optional[str] = None,
    ) -> None:
        try:
            if in_cluster:
                k8s_config.load_incluster_config()
            else:
                k8s_config.load_kube_config(config_file=kubeconfig)
        except Exception as exc:
            raise RuntimeError(f"Failed to load kubeconfig: {exc}") from exc
        self._v1 = client.CoreV1Api()

    def get_pod_resources(
        self,
        namespaces: Optional[List[str]] = None,
        exclude_namespaces: Optional[List[str]] = None,
    ) -> List[ContainerResources]:
        """
        Return resource requests/limits for every container in every
        non-terminal pod.  If *namespaces* is empty/None, all namespaces
        are queried.
        """
        excluded = set(exclude_namespaces or [])
        result: List[ContainerResources] = []

        try:
            if namespaces:
                raw_pods: list = []
                for ns in namespaces:
                    raw_pods.extend(
                        self._v1.list_namespaced_pod(namespace=ns).items
                    )
            else:
                raw_pods = self._v1.list_pod_for_all_namespaces().items
        except ApiException as exc:
            raise RuntimeError(f"Kubernetes API error: {exc}") from exc

        for pod in raw_pods:
            ns: str = pod.metadata.namespace
            if ns in excluded:
                continue
            # Skip pods that have fully terminated
            if pod.status.phase in ("Failed", "Unknown"):
                continue

            for container in pod.spec.containers or []:
                res = container.resources or client.V1ResourceRequirements()
                requests: Dict[str, str] = res.requests or {}
                limits: Dict[str, str] = res.limits or {}

                result.append(
                    ContainerResources(
                        namespace=ns,
                        pod_name=pod.metadata.name,
                        container_name=container.name,
                        cpu_request=_parse_cpu(requests.get("cpu")),
                        memory_request=_parse_memory(requests.get("memory")),
                        cpu_limit=_parse_cpu(limits.get("cpu")),
                        memory_limit=_parse_memory(limits.get("memory")),
                    )
                )

        return result
