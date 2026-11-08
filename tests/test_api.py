import json
import logging

import pytest
from fastapi.testclient import TestClient

from datawatch.api import create_app
from datawatch.service import Service


@pytest.fixture(scope="module")
def client(service):
    with TestClient(create_app(service)) as c:
        yield c


@pytest.fixture(scope="module")
def fresh(cfg, warehouse, tmp_path_factory):
    """A separate service for tests that change state. Each test acts on a different incident."""
    svc = Service(cfg, warehouse, state_path=tmp_path_factory.mktemp("state") / "state.json")
    with TestClient(create_app(svc)) as c:
        yield c, svc


def test_healthz(client):
    assert client.get("/healthz").json()["status"] == "ok"


def test_metrics_prometheus_format(client):
    client.get("/api/tables")
    text = client.get("/metrics").text
    assert "# TYPE datawatch_http_requests_total counter" in text
    assert 'datawatch_detection_rate{mode="learned"} 1.0' in text
    assert 'datawatch_http_requests_total{route="/api/tables",status="200"}' in text


def test_tables_health_on_incident_day(client):
    d = client.get("/api/tables?day=108").json()
    status = {t["name"]: t["status"] for t in d["tables"]}
    assert len(status) == 41 and d["date"] == "2026-09-17"
    assert status["raw_order_items"] in ("warning", "critical") and status["raw_orders"] == "healthy"
    assert {t["status"] for t in client.get("/api/tables?day=3").json()["tables"]} == {"learning"}


def test_table_detail_has_bands_and_404(client):
    d = client.get("/api/tables/raw_web_sessions").json()
    assert len(d["volume"]) == 120 and d["volume"][60]["upper"] is not None
    assert any(p["status"] == "alert" for p in d["volume"])
    assert d["owner_contacts"]["slack"] == "#data-platform"
    assert client.get("/api/tables/nope").status_code == 404


def test_incident_list_and_detail_with_lineage(client):
    ids = [i["id"] for i in client.get("/api/incidents").json()["incidents"]]
    assert "INC-108-raw_order_items" in ids
    d = client.get("/api/incidents/INC-108-raw_order_items").json()
    roles = {n["id"]: n["role"] for n in d["lineage"]["nodes"]}
    assert roles["raw_order_items"] == "root" and roles["mart_exec_kpis"] == "affected"
    assert d["routing"]["action"] == "page" and d["alert_count"] >= 7
    assert client.get("/api/incidents/INC-000-x").status_code == 404


def test_acknowledge_snooze_resolve_reopen(fresh):
    c, svc = fresh
    iid = "INC-074-raw_payments"
    assert c.post(f"/api/incidents/{iid}/actions", json={"action": "acknowledge"}).json()["status"] == "acknowledged"
    s = c.post(f"/api/incidents/{iid}/actions", json={"action": "snooze", "days": 50}).json()
    assert s["status"] == "snoozed" and s["snoozed_until"] == s["last_day"] + 50
    assert c.post(f"/api/incidents/{iid}/actions", json={"action": "resolve"}).json()["status"] == "resolved"
    r = c.post(f"/api/incidents/{iid}/actions", json={"action": "reopen", "note": "seen again"}).json()
    assert r["status"] == "recovered" and len(r["notes"]) == 4
    assert json.loads(svc.state_path.read_text())["state"][iid]["notes"][-1]["note"] == "seen again"
    assert "datawatch_incident_actions_total" in c.get("/metrics").text


def test_mark_as_expected_feeds_back_into_baseline(fresh):
    c, svc = fresh
    iid = "INC-066-raw_shipments"
    before = len(svc.learned.alerts)
    d = c.post(f"/api/incidents/{iid}/actions", json={"action": "expected", "note": "replayed file on purpose"}).json()
    assert d["status"] == "expected"
    assert len(svc.learned.alerts) < before
    assert not any(a.table == "raw_shipments" and a.day == 66 for a in svc.learned.alerts)
    pts = svc.learned.points[("raw_shipments", "volume", "row_count")]
    assert pts[66].status == "expected"
    listed = {i["id"]: i["status"] for i in c.get("/api/incidents").json()["incidents"]}
    assert listed[iid] == "expected"
    assert c.get(f"/api/incidents/{iid}").json()["lineage"]["nodes"]


def test_state_survives_restart(fresh, cfg, warehouse):
    c, svc = fresh
    c.post("/api/incidents/INC-102-raw_returns/actions", json={"action": "acknowledge"})
    again = Service(cfg, warehouse, state_path=svc.state_path)
    assert again.by_id["INC-102-raw_returns"].status == "acknowledged"


def test_bad_action_and_unknown_incident(client):
    assert client.post("/api/incidents/INC-108-raw_order_items/actions", json={"action": "delete"}).status_code == 422
    assert client.post("/api/incidents/INC-1/actions", json={"action": "acknowledge"}).status_code == 404


def test_evaluation_and_summary(client):
    e = client.get("/api/evaluation").json()
    assert len(e["by_type"]) == 8 and e["learned"]["detection_rate"] == 1.0
    s = client.get("/api/summary").json()
    assert s["tables"] == 41 and len(s["timeline"]) == 120


def test_ui_served(client):
    html = client.get("/").text
    assert "<title>Data Watch</title>" in html
    assert "lineageSvg" in client.get("/static/app.js").text
    assert "prefers-color-scheme: dark" in client.get("/static/styles.css").text


def test_json_request_logs(client, capsys):
    logger = logging.getLogger("datawatch")
    stream = logger.handlers[0].stream
    import io

    buf = io.StringIO()
    logger.handlers[0].stream = buf
    try:
        client.get("/api/incidents")
    finally:
        logger.handlers[0].stream = stream
    line = json.loads(buf.getvalue().strip().splitlines()[-1])
    assert line["msg"] == "request" and line["route"] == "/api/incidents" and line["status"] == 200
