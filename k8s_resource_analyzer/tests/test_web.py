"""Web UI: refresh → report.json/xlsx on disk → API, auth, rate limit."""

import pytest
from fastapi.testclient import TestClient

from src.config import Config
from src.web.app import create_app

from .test_runs_report import NS, FakeVM


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    vm = FakeVM()
    monkeypatch.setattr("requests.Session.post", lambda self, *a, **kw: vm.post(*a, **kw))
    monkeypatch.delenv("WEB_USER", raising=False)
    monkeypatch.delenv("WEB_PASSWORD", raising=False)
    c = Config.default()
    c.kubernetes.enabled = False
    c.prometheus.url = "http://vm/select/0/prometheus"
    c.prometheus.pause_seconds = 0
    c.analysis.namespaces = [NS]
    c.web.data_dir = str(tmp_path / "data")
    return c


def test_report_flow(cfg):
    app = create_app(cfg, start_scheduler=False)
    with TestClient(app) as client:
        assert client.get("/healthz").json() == {"status": "ok"}
        assert "Kubernetes Resource Waste" in client.get("/").text

        empty = client.get("/api/report").json()
        assert empty["report"] is None
        assert client.get("/report.xlsx").status_code == 404

        app.state.manager.get(None).run_sync()
        data = client.get("/api/report").json()
        rep, status = data["report"], data["status"]
        assert status["last_error"] is None and not status["running"]
        assert rep["namespaces"] == [NS]
        assert rep["summary"]["runs"] == 4
        names = {w["workload"]: w for w in rep["workloads"]}
        gb = names["gb-data-collect-task"]
        assert gb["status"] == "over" and gb["mem_req"] == 512 and gb["mem_max"] == 7
        assert gb["dag_id"] == "gb_data"
        gb_runs = [r for r in rep["runs"] if r["wid"] == gb["id"]]
        assert len(gb_runs) == 2 and all(r["mem_over"] > 500 for r in gb_runs)
        assert names["risky-task"]["oom_runs"] == 1

        x = client.get("/report.xlsx")
        assert x.status_code == 200 and x.content[:2] == b"PK"
        assert "k8s_waste_report_" in x.headers["content-disposition"]

        # Manual refresh right after a run is rate-limited
        r = client.post("/api/refresh").json()
        assert r["started"] is False and "too soon" in r["reason"]


def test_failed_run_keeps_last_report(cfg, monkeypatch):
    app = create_app(cfg, start_scheduler=False)
    ref = app.state.manager.get(None)
    ref.run_sync()
    first = ref.load()

    def boom(*a, **kw):
        raise RuntimeError("VM is down")

    monkeypatch.setattr("src.web.app.run_analysis", boom)
    ref.run_sync()
    assert "VM is down" in ref.last_error
    assert ref.load() == first


def test_basic_auth(cfg, monkeypatch):
    monkeypatch.setenv("WEB_USER", "admin")
    monkeypatch.setenv("WEB_PASSWORD", "s3cret")
    with TestClient(create_app(cfg, start_scheduler=False)) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/").status_code == 401
        assert client.get("/api/report", auth=("admin", "wrong")).status_code == 401
        assert client.get("/api/report", auth=("admin", "s3cret")).status_code == 200


def test_next_run_schedule(cfg):
    ref = create_app(cfg, start_scheduler=False).state.manager.get(None)
    assert ref.next_run is not None  # no report yet → due now
    ref.run_sync()
    assert ref.next_run == pytest.approx(ref.last_finished + 24 * 3600)


def test_clusters_edit(cfg):
    app = create_app(cfg, start_scheduler=False)
    m = app.state.manager
    with TestClient(app) as client:
        data = client.get("/api/clusters").json()
        assert data["editable"] is True
        assert [(c["id"], c["namespaces"]) for c in data["clusters"]] == [("default", [NS])]

        m.get("default").run_sync()
        assert client.get("/api/report?cluster=default").json()["report"] is not None

        # Add a second cluster; the first one is unchanged and keeps its report
        new = [
            {"name": "default", "url": "http://vm/select/0/prometheus", "namespaces": [NS]},
            {"name": "Prod DE", "url": "http://vm-de/select/0/prometheus/", "namespaces": "a-ns, b-ns"},
        ]
        data = client.put("/api/clusters", json=new).json()
        ids = [c["id"] for c in data["clusters"]]
        assert ids == ["default", "prod-de"]
        assert data["clusters"][1]["url"] == "http://vm-de/select/0/prometheus"
        assert data["clusters"][1]["namespaces"] == ["a-ns", "b-ns"]
        assert client.get("/api/report?cluster=default").json()["report"] is not None
        assert client.get("/api/report?cluster=prod-de").json()["report"] is None
        assert m.get("prod-de").cfg.analysis.namespaces == ["a-ns", "b-ns"]
        assert client.get("/api/report?cluster=nope").status_code == 404

        # Persisted: a new app instance reads clusters.json
        assert [c["id"] for c in create_app(cfg, start_scheduler=False).state.manager.describe()] == ids

        # Changing the namespaces of a cluster drops its stale report
        new[0]["namespaces"] = ["other-ns"]
        client.put("/api/clusters", json=new)
        assert client.get("/api/report?cluster=default").json()["report"] is None

        for bad, msg in (
            ([], "At least one"),
            ([{"name": "x", "url": "vm:8428", "namespaces": []}], "URL must start"),
            ([{"name": "x", "url": "http://vm", "namespaces": ["Bad_NS"]}], "bad namespace"),
            ([{"name": "a", "url": "http://vm"}, {"name": "A", "url": "http://vm"}], "duplicate"),
        ):
            r = client.put("/api/clusters", json=bad)
            assert r.status_code == 422 and msg in r.json()["detail"]


def test_clusters_from_config_and_readonly(cfg):
    from src.config import ClusterConfig

    cfg.clusters = [ClusterConfig(name="msd", url="http://a/select/0/prometheus", namespaces=["x"]),
                    ClusterConfig(name="kz", url="http://b/select/0/prometheus", namespaces=[])]
    cfg.web.allow_edit_clusters = False
    with TestClient(create_app(cfg, start_scheduler=False)) as client:
        data = client.get("/api/clusters").json()
        assert [c["id"] for c in data["clusters"]] == ["msd", "kz"] and data["editable"] is False
        assert client.put("/api/clusters", json=[{"name": "y", "url": "http://y"}]).status_code == 403


def test_one_analysis_at_a_time(cfg):
    import threading
    import time as _t

    from src.config import ClusterConfig

    cfg.clusters = [ClusterConfig(name=n, url="http://vm/select/0/prometheus", namespaces=[NS]) for n in "ab"]
    m = create_app(cfg, start_scheduler=False).state.manager
    active, peak = [0], [0]
    lock = threading.Lock()
    import src.web.app as web_app
    real = web_app.run_analysis

    def slow(c):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        _t.sleep(0.2)
        try:
            return real(c)
        finally:
            with lock:
                active[0] -= 1

    web_app.run_analysis = slow
    try:
        for r in m.refreshers.values():
            assert r.trigger()["started"]
        deadline = _t.time() + 10
        while any(r.running for r in m.refreshers.values()) and _t.time() < deadline:
            _t.sleep(0.05)
    finally:
        web_app.run_analysis = real
    assert peak[0] == 1
    assert all(r.load() for r in m.refreshers.values())
