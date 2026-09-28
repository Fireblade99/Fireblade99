"""Airflow integration with Qlik Gateway.

Business DAGs never talk to Qlik directly: they ask the gateway to run a reload and read the
state the gateway already has. Put this package into the Airflow `plugins/` folder (or install
it as a package) and create an HTTP connection `qlik_gateway_default`:
  host = https://qlik-gateway.company.local   password = <client token issued in the gateway UI>
"""

from .hooks import QlikGatewayHook
from .operators import QlikReloadOperator
from .sensors import QlikExecutionSensor

__all__ = ["QlikGatewayHook", "QlikReloadOperator", "QlikExecutionSensor"]
