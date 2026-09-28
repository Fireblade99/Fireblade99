try:  # Airflow 3
    from airflow.sdk import BaseHook, BaseOperator, BaseSensorOperator
except ImportError:  # Airflow 2.x
    from airflow.hooks.base import BaseHook
    from airflow.models import BaseOperator
    from airflow.sensors.base import BaseSensorOperator

from airflow.exceptions import AirflowException

__all__ = ["AirflowException", "BaseHook", "BaseOperator", "BaseSensorOperator"]
