import math

import pytest
from conftest import weekly_series

from datawatch import baselines as bl


def cal(month_ends=(), holidays=()):
    me, hol = set(month_ends), set(holidays)
    return bl.Calendar(lambda d: d % 7, lambda d: d in me, lambda d: d in hol)


def hist(values, upto):
    return list(range(upto)), values[:upto]


def test_median_and_mad():
    assert bl.median([3, 1, 2]) == 2
    assert bl.median([4, 1, 2, 3]) == 2.5
    assert bl.mad([1, 1, 1, 1]) == 0
    assert bl.mad([1, 2, 3, 4, 100]) == pytest.approx(1.4826, rel=1e-3)  # one outlier does not move it


def test_median_of_empty_raises():
    with pytest.raises(ValueError):
        bl.median([])


def test_warmup_returns_none():
    v = weekly_series()
    assert bl.seasonal_bounds(*hist(v, 20), 20, cal(), 4.5, warmup=21) is None
    assert bl.seasonal_bounds(*hist(v, 21), 21, cal(), 4.5, warmup=21) is not None


def test_weekly_model_chosen_for_weekday_weekend_pattern():
    v = weekly_series()
    b = bl.seasonal_bounds(*hist(v, 56), 56, cal(), 4.5)
    assert b.model == "weekly"


def test_flat_model_chosen_without_pattern():
    v = weekly_series(weekend=1.0)
    b = bl.seasonal_bounds(*hist(v, 56), 56, cal(), 4.5)
    assert b.model == "flat"


def test_seasonality_does_not_cause_false_alerts():
    v = weekly_series(seed=3)
    c = cal()
    alerts = [d for d in range(28, 120) if not bl.seasonal_bounds(*hist(v, d), d, c, 4.5).contains(v[d])]
    assert alerts == []


def test_flat_rule_would_alert_on_weekends():
    v = weekly_series(seed=3)
    weekend_days = [d for d in range(28, 120) if d % 7 == 5]
    trailing = [abs(v[d] / (sum(v[d - 7:d]) / 7) - 1) > 0.3 for d in weekend_days]
    assert sum(trailing) > len(weekend_days) / 2


def test_detects_partial_load_on_weekday_and_weekend():
    v = weekly_series(seed=4)
    c = cal()
    for d in (50, 54):  # a Tuesday and a Saturday
        b = bl.seasonal_bounds(*hist(v, d), d, c, 4.5)
        assert not b.contains(v[d] * 0.4)
        assert not b.contains(v[d] * 2.0)


def test_adaptive_band_wider_for_noisier_table():
    quiet = weekly_series(noise=0.02, seed=5)
    noisy = weekly_series(noise=0.12, seed=5)
    bq = bl.seasonal_bounds(*hist(quiet, 60), 60, cal(), 4.5)
    bn = bl.seasonal_bounds(*hist(noisy, 60), 60, cal(), 4.5)
    assert (bn.upper / bn.lower) > 1.5 * (bq.upper / bq.lower)


def test_sensitivity_changes_band_width():
    v = weekly_series()
    lo = bl.seasonal_bounds(*hist(v, 60), 60, cal(), 3.5)
    hi = bl.seasonal_bounds(*hist(v, 60), 60, cal(), 6.0)
    assert hi.upper > lo.upper and hi.lower < lo.lower


def test_first_month_end_gets_a_wide_band():
    v = weekly_series(seed=6)
    d = 59
    v[d] *= 1.5
    plain = bl.seasonal_bounds(*hist(v, d), d, cal(), 4.5)
    me = bl.seasonal_bounds(*hist(v, d), d, cal(month_ends=[d]), 4.5)
    assert not plain.contains(v[d])
    assert me.contains(v[d])


def test_month_end_uplift_is_learned():
    v = weekly_series(seed=7)
    for d in (30, 60, 90):
        v[d] *= 1.6
    c = cal(month_ends=[30, 60, 90])
    fit = bl.fit_seasonal(list(range(90)), v[:90], c)
    assert fit.month_end_seen == 2
    assert math.exp(fit.month_end_uplift) == pytest.approx(1.6, rel=0.12)
    b = bl.predict_seasonal(fit, list(range(90)), v[:90], 90, c, 4.5)
    assert b.contains(v[90])


def test_holiday_widens_lower_bound():
    v = weekly_series(seed=8)
    d = 45
    v[d] *= 0.45
    assert not bl.seasonal_bounds(*hist(v, d), d, cal(), 4.5).contains(v[d])
    assert bl.seasonal_bounds(*hist(v, d), d, cal(holidays=[d]), 4.5).contains(v[d])


def test_drift_follows_growth():
    v = [1000 * (1.01 ** d) for d in range(80)]
    fit = bl.fit_seasonal(list(range(70)), v[:70], cal())
    assert fit.drift == pytest.approx(math.log(1.01), rel=0.05)
    b = bl.predict_seasonal(fit, list(range(70)), v[:70], 70, cal(), 4.5)
    assert b.expected == pytest.approx(v[70], rel=0.02)


def test_rate_bounds_binomial_noise_scales_with_sample_size():
    vals = [0.01] * 30
    small = bl.rate_bounds(vals, [50] * 30, 50, 4.5, floor=0.02)
    big = bl.rate_bounds(vals, [50000] * 30, 50000, 4.5, floor=0.02)
    assert small.upper > big.upper
    assert big.upper == pytest.approx(0.03, abs=0.002)  # the floor


def test_rate_bounds_zero_history_still_has_band():
    b = bl.rate_bounds([0.0] * 20, [100] * 20, 100, 4.5, floor=0.005)
    assert 0.005 <= b.upper < 0.2
    assert not b.contains(0.35)
    assert b.lower == -math.inf


def test_rate_bounds_two_sided_and_warmup():
    assert bl.rate_bounds([0.5] * 5, [100] * 5, 100, 4.5, floor=0.05) is None
    b = bl.rate_bounds([0.5] * 20, [10000] * 20, 10000, 4.5, floor=0.05, two_sided=True)
    assert not b.contains(0.3) and not b.contains(0.7) and b.contains(0.5)


def test_upper_bounds_freshness():
    b = bl.upper_bounds([3.0, 3.1, 2.9, 3.2, 2.8] * 4, 4.5, floor=1.0, min_scale=0.25, warmup=14)
    assert b.contains(3.5)
    assert not b.contains(13.0)
    assert b.upper >= 4.0


def test_psi_identical_is_zero_and_shift_is_large():
    base = {"a": 500, "b": 300, "c": 200}
    assert bl.psi(base, base) == pytest.approx(0, abs=1e-9)
    assert bl.psi(base, {"a": 100, "b": 300, "c": 600}) > 0.5


def test_psi_handles_new_and_missing_categories():
    v = bl.psi({"a": 50, "b": 50}, {"a": 50, "c": 50})
    assert math.isfinite(v) and v > 1


def test_psi_noise_shrinks_with_sample_size():
    assert bl.psi_noise(20, 500, 4) > bl.psi_noise(2000, 50000, 4)
    assert bl.psi_noise(1000, 1000, 10) > bl.psi_noise(1000, 1000, 3)


def test_chi2_quantile_close_to_table():
    assert bl.chi2_quantile(3, z=1.645) == pytest.approx(7.81, rel=0.02)


def test_relevel_moves_history_to_accepted_level():
    before = [100.0] * 20
    out = bl.relevel(before, [200.0, 205.0, 195.0])
    assert out[0] == pytest.approx(200.0, rel=0.03)
    assert bl.relevel(before, []) == before
