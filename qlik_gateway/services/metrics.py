from prometheus_client import Counter, Gauge, Histogram

API_REQUESTS = Counter("qgw_api_requests_total", "Calls to the gateway API", ["client", "action", "outcome"])
QLIK_CALLS = Counter("qgw_qlik_calls_total", "Calls from the gateway to Qlik", ["method", "endpoint", "outcome"])
QLIK_LATENCY = Histogram("qgw_qlik_call_seconds", "Latency of calls to Qlik", ["endpoint"])
EXECUTIONS_FINISHED = Counter("qgw_executions_finished_total", "Finished executions", ["client", "status"])
EXECUTIONS_ACTIVE = Gauge("qgw_executions_active", "Executions currently queued/running", ["status"])
DISPATCH_PAUSED = Gauge("qgw_dispatch_paused", "1 if dispatching to Qlik is paused")
