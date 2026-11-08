import copy

from conftest import alert

from datawatch import evaluate
from datawatch.cli import report_markdown


def test_every_planted_incident_detected_by_learned_baselines(service):
    L = service.baseline_eval["learned"]
    assert L["detection_rate"] == 1.0
    assert all(r["detected"] for r in L["incidents"])


def test_root_cause_correct_for_every_planted_incident(service):
    assert service.baseline_eval["learned"]["root_cause_accuracy"] == 1.0


def test_false_alert_rate_under_gate_and_far_below_static(service):
    L, S = service.baseline_eval["learned"], service.baseline_eval["static"]
    assert L["false_alerts_per_table_week"] < 0.1
    assert S["false_alerts"] > 10 * L["false_alerts"]


def test_learned_freshness_detects_sooner_than_static(service):
    row = next(r for r in service.baseline_eval["by_type"] if r["id"] == "late_load")
    assert row["learned_ttd_hours"] < row["static_ttd_hours"]


def test_silent_bug_flags_all_seven_tables_in_one_incident(service):
    row = next(r for r in service.baseline_eval["learned"]["incidents"] if r["id"] == "silent_quantity_bug")
    assert row["tables_flagged"] == 7
    inc = service.by_id[row["incident"]]
    assert inc.tables == set(next(p for p in service.planted if p.id == "silent_quantity_bug").affected)


def test_grouping_reduces_alert_volume(service):
    L = service.baseline_eval["learned"]
    assert L["incident_count"] < L["alerts"] / 3
    assert service.baseline_eval["notifications"]["learned"] < L["alerts"]


def test_accounting_adds_up(service):
    L = service.baseline_eval["learned"]
    attributed = [a for a in service.learned.alerts if evaluate.attributed(service.planted, a)]
    assert len(attributed) + L["false_alerts"] == L["alerts"]
    assert L["false_alerts_per_table_week"] == round(L["false_alerts"] / (41 * L["weeks_scored"]), 4) or \
        abs(L["false_alerts_per_table_week"] - L["false_alerts"] / (41 * (120 - 28) / 7)) < 1e-3


def test_attribution_window(service):
    p = next(p for p in service.planted if p.id == "cents_shift")
    assert evaluate.attributed([p], alert("fct_payments", 78)) == "cents_shift"
    assert evaluate.attributed([p], alert("fct_payments", 79)) is None
    assert evaluate.attributed([p], alert("raw_orders", 75)) is None


def test_gate_passes_and_fails(service):
    assert evaluate.gate(service.baseline_eval, 0.1) == []
    bad = copy.deepcopy(service.baseline_eval)
    bad["learned"]["incidents"][0]["detected"] = False
    bad["learned"]["false_alerts_per_table_week"] = 0.5
    problems = evaluate.gate(bad, 0.1)
    assert len(problems) == 2 and "not detected" in problems[0]


def test_markdown_report(service):
    md = report_markdown(service.baseline_eval)
    assert "| silent upstream bug |" in md and "False alerts per table per week" in md
