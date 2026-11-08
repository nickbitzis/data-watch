import math

from conftest import weekly_series

from datawatch.engine import Engine, schema_diff
from datawatch.profile import Profile

T = "raw_orders"  # any declared table; the series below are hand-built


def run(cfg, series=None, sizes=None, categories=None, schemas=None, mode="learned", feedback=None, sensitivity=None):
    p = Profile(days=cfg.days, series=series or {}, sizes=sizes or {}, categories=categories or {},
                schemas=schemas or {})
    p.series.setdefault((T, "freshness", "arrival_hours"), [2.0] * cfg.days)
    return Engine(cfg, p, mode, feedback, sensitivity).run()


def vol(values):
    return {(T, "volume", "row_count"): values}


def days_of(result, monitor):
    return sorted({a.day for a in result.alerts if a.monitor == monitor})


def test_seasonal_volume_has_no_alerts(cfg):
    assert days_of(run(cfg, vol(weekly_series(seed=11))), "volume") == []


def test_volume_drop_and_spike_detected(cfg):
    v = weekly_series(seed=12)
    v[50] *= 0.35
    v[80] *= 2.0
    assert days_of(run(cfg, vol(v)), "volume") == [50, 80]


def test_no_alerts_during_warmup(cfg):
    v = weekly_series(seed=13)
    v[10] *= 0.1
    r = run(cfg, vol(v))
    assert days_of(r, "volume") == []
    assert r.points[(T, "volume", "row_count")][10].status == "warmup"


def test_alerted_days_stay_out_of_the_baseline(cfg):
    v = weekly_series(seed=14)
    for d in range(60, 64):
        v[d] *= 0.3
    assert days_of(run(cfg, vol(v)), "volume") == [60, 61, 62, 63]


def test_static_rules_alert_on_weekends(cfg):
    r = run(cfg, vol(weekly_series(seed=11)), mode="static")
    assert len(days_of(r, "volume")) > 20


def test_freshness_late_load_and_detection_time(cfg):
    arr = [3.0 + 0.1 * math.sin(d) for d in range(cfg.days)]
    arr[70] = 13.0
    r = run(cfg, {(T, "freshness", "arrival_hours"): arr})
    a = [x for x in r.alerts if x.monitor == "freshness"]
    assert [x.day for x in a] == [70]
    assert 4.0 <= a[0].detected_hour < 6.0  # fires at the learned deadline, not when the data finally lands
    assert a[0].severity == "high"


def test_static_freshness_fires_at_fixed_deadline(cfg):
    arr = [3.0] * cfg.days
    arr[70] = 13.0
    r = run(cfg, {(T, "freshness", "arrival_hours"): arr}, mode="static")
    a = [x for x in r.alerts if x.monitor == "freshness"]
    assert a[0].detected_hour == 8.0


def test_null_rate_spike(cfg):
    key = (T, "null_rate", "email")
    vals = [0.01 if d % 3 else 0.0 for d in range(cfg.days)]
    vals[90] = 0.35
    r = run(cfg, {key: vals}, sizes={key: [80] * cfg.days})
    assert days_of(r, "null_rate") == [90]


def test_uniqueness_duplicate_load(cfg):
    key = (T, "uniqueness", "order_id")
    vals = [0.0] * cfg.days
    vals[66] = 0.5
    r = run(cfg, {key: vals}, sizes={key: [500] * cfg.days})
    assert days_of(r, "uniqueness") == [66]


def test_referential_orphans(cfg):
    key = (T, "referential", "customer_id->stg_customers.customer_id")
    vals = [0.0] * cfg.days
    vals[100] = 0.4
    vals[101] = 0.4
    r = run(cfg, {key: vals}, sizes={key: [12] * cfg.days})
    assert days_of(r, "referential") == [100, 101]


def test_distribution_mean_shift_cents(cfg):
    key = (T, "distribution", "amount.mean")
    vals = [80.0 * (1 + 0.02 * math.sin(d)) for d in range(cfg.days)]
    vals[74] *= 100
    r = run(cfg, {**vol([500.0] * cfg.days), key: vals})
    assert days_of(r, "distribution") == [74]


def test_psi_flags_category_shift_not_noise(cfg):
    base = [{"card": 700 + d % 5, "paypal": 200, "giftcard": 50, "applepay": 50} for d in range(cfg.days)]
    base[90] = {"card": 200, "paypal": 700, "giftcard": 50, "applepay": 50}
    r = run(cfg, categories={(T, "method"): base})
    assert days_of(r, "distribution") == [90]


def test_schema_change_alert_and_diff(cfg):
    snaps = [{"a": "INTEGER", "b": "VARCHAR"} for _ in range(cfg.days)]
    for d in range(58, cfg.days):
        snaps[d] = {"a": "VARCHAR", "b": "VARCHAR"}
    r = run(cfg, schemas={T: snaps})
    a = [x for x in r.alerts if x.monitor == "schema"]
    assert [x.day for x in a] == [58]
    assert "a INTEGER to VARCHAR" in a[0].findings[0].message
    d = schema_diff({"a": "INTEGER", "x": "DATE"}, {"a": "VARCHAR", "y": "DATE"})
    assert d == {"removed": ["x"], "added": ["y"], "changed": [("a", "INTEGER", "VARCHAR")]}


def test_mark_as_expected_suppresses_and_relearns(cfg):
    v = weekly_series(seed=15)
    for d in range(60, cfg.days):
        v[d] *= 2.0  # a planned launch doubles traffic for good
    key = (T, "volume", "row_count")
    before = run(cfg, {key: v})
    assert len(days_of(before, "volume")) >= 28  # without feedback it alerts until the clean history ages out
    after = run(cfg, {key: v}, feedback={key: {60, 61}})
    assert days_of(after, "volume") == []
    assert after.points[key][60].status == "expected"


def test_one_day_expected_spike_does_not_move_baseline(cfg):
    v = weekly_series(seed=17)
    v[70] *= 2.0
    key = (T, "volume", "row_count")
    r = run(cfg, {key: v}, feedback={key: {70}})
    assert days_of(r, "volume") == []
    assert r.points[key][70].status == "expected"


def test_sensitivity_high_alerts_at_least_as_much_as_low(cfg):
    v = weekly_series(seed=16, noise=0.06)
    v[70] *= 0.7
    high = run(cfg, vol(v), sensitivity="high")
    low = run(cfg, vol(v), sensitivity="low")
    assert set(days_of(low, "volume")) <= set(days_of(high, "volume"))
    assert 70 in days_of(high, "volume")


def test_quantiles_skipped_for_small_tables(cfg):
    key = (T, "distribution", "amount.p95")
    r = run(cfg, {**vol([4.0] * cfg.days), key: [10.0] * cfg.days})
    assert key not in r.points


def test_alert_to_dict_has_findings(cfg):
    v = weekly_series(seed=12)
    v[50] *= 0.35
    a = run(cfg, vol(v)).alerts[0].to_dict()
    assert a["id"] == f"{T}:volume:50" and a["findings"][0]["metric"] == "row_count"
    assert "rows, expected" in a["findings"][0]["message"]
