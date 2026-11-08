from conftest import alert

from datawatch.incidents import apply_state, choose_root, group_alerts, notifications, route


def test_lineage_chain_groups_into_one_incident_with_upstream_root(cfg):
    alerts = [alert(t, 108, "distribution") for t in
              ["mart_exec_kpis", "fct_orders", "stg_order_items", "raw_order_items", "int_order_revenue"]]
    inc = group_alerts(cfg, alerts)
    assert len(inc) == 1
    assert inc[0].root_table == "raw_order_items"
    assert inc[0].id == "INC-108-raw_order_items"


def test_unrelated_tables_get_separate_incidents(cfg):
    inc = group_alerts(cfg, [alert("raw_inventory", 52, "freshness"), alert("raw_returns", 52, "referential")])
    assert sorted(i.root_table for i in inc) == ["raw_inventory", "raw_returns"]


def test_root_prefers_table_explaining_most_alerts(cfg):
    alerts = [alert("raw_web_sessions", 45), alert("stg_web_sessions", 45), alert("fct_web_traffic", 45),
              alert("raw_customers", 45), alert("mart_exec_kpis", 45)]
    root, cands, reason = choose_root(cfg, alerts)
    assert root == "raw_web_sessions"
    assert cands == ["raw_web_sessions", "raw_customers"]
    assert "no anomalous upstream" in reason


def test_root_tie_breaks_on_earliest_alert(cfg):
    root, _, _ = choose_root(cfg, [alert("raw_returns", 5, hour=4.0), alert("raw_inventory", 5, hour=3.0)])
    assert root == "raw_inventory"


def test_dedup_across_days_within_gap(cfg):
    alerts = [alert("raw_payments", d, "distribution") for d in (74, 75, 76)] + [alert("fct_payments", 77)]
    inc = group_alerts(cfg, alerts)
    assert len(inc) == 1 and inc[0].first_day == 74 and inc[0].last_day == 77


def test_new_incident_after_gap(cfg):
    inc = group_alerts(cfg, [alert("raw_payments", 74), alert("raw_payments", 80)], gap=2)
    assert len(inc) == 2


def test_root_moves_upstream_when_later_alert_explains_more(cfg):
    inc = group_alerts(cfg, [alert("stg_payments", 74), alert("raw_payments", 75)])
    assert len(inc) == 1 and inc[0].root_table == "raw_payments"


def test_routing_pages_for_critical_marts(cfg):
    inc = group_alerts(cfg, [alert("raw_order_items", 108), alert("mart_finance_summary", 108)])[0]
    r = route(cfg, inc)
    assert r["action"] == "page" and r["target"] == "pd-data-platform"
    assert r["notify"] == ["finance-data"] and r["critical_tables"] == ["mart_finance_summary"]


def test_routing_tickets_for_sources_and_defaults(cfg):
    high = group_alerts(cfg, [alert("raw_returns", 102, "referential", severity="high")])[0]
    assert high.routing["rule"] == "high-severity-source" and high.routing["target"] == "DATA-PLATFORM"
    low = group_alerts(cfg, [alert("fct_support_daily", 41)])[0]
    assert low.routing["rule"] == "default" and low.routing["action"] == "ticket"
    assert low.routing["target"] == "CUSTOMER"


def test_apply_state_statuses(cfg):
    inc = group_alerts(cfg, [alert("raw_returns", 100), alert("raw_inventory", 118), alert("raw_payments", 115)])
    state = {"INC-100-raw_returns": {"status": "acknowledged", "notes": []},
             "INC-115-raw_payments": {"status": "snoozed", "snoozed_until": 116, "notes": []}}
    apply_state(inc, state, as_of=119)
    by = {i.id: i.status for i in inc}
    assert by["INC-100-raw_returns"] == "acknowledged"
    assert by["INC-118-raw_inventory"] == "open"
    assert by["INC-115-raw_payments"] == "recovered"  # snooze expired and it has been quiet


def test_notifications_one_page_per_incident_and_snooze(cfg):
    alerts = [alert(t, d) for d in (74, 75, 76) for t in ("raw_payments", "stg_payments", "fct_payments")]
    inc = group_alerts(cfg, alerts)
    n = notifications(inc)
    assert len(alerts) == 9 and len(inc) == 1
    assert [x["kind"] for x in n] == ["ticket", "update", "update"]
    snoozed = notifications(inc, {inc[0].id: {"snoozed_until": 76}})
    assert len(snoozed) == 1


def test_summary_title(cfg):
    inc = group_alerts(cfg, [alert("raw_inventory", 52, "freshness"), alert("stg_inventory", 52, "freshness")])[0]
    s = inc.summary(cfg)
    assert s["title"] == "freshness anomaly on raw_inventory and 1 downstream tables"
    assert s["tables"] == ["raw_inventory", "stg_inventory"]
