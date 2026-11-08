"""Learned baselines. Pure functions on plain lists so each one is easy to test on a hand-built series.

All bounds use medians and the median absolute deviation (MAD), never means and standard deviations,
so one bad day in the history does not widen the band for the next month.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass

MAD_TO_SD = 1.4826


@dataclass
class Bounds:
    expected: float
    lower: float
    upper: float
    model: str  # flat, weekly, rate, psi

    def contains(self, v: float) -> bool:
        return self.lower <= v <= self.upper


@dataclass
class Calendar:
    """Day features the baselines use: weekday, last day of month, declared holiday."""

    weekday: Callable[[int], int]
    month_end: Callable[[int], bool]
    holiday: Callable[[int], bool]

    def special(self, day: int) -> bool:
        return self.month_end(day) or self.holiday(day)


def median(xs: list[float]) -> float:
    s = sorted(xs)
    n = len(s)
    if n == 0:
        raise ValueError("median of empty list")
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2


def mad(xs: list[float]) -> float:
    """Median absolute deviation scaled to match a standard deviation for normal data."""
    if len(xs) < 2:
        return 0.0
    m = median(xs)
    return MAD_TO_SD * median([abs(x - m) for x in xs])


def _fwd(transform: str) -> Callable[[float], float]:
    """`log` makes bands relative (a 30% drop is the same size at 100 rows or 100,000 rows)."""
    return (lambda v: math.log(max(v, 1e-9))) if transform == "log" else (lambda v: v)


def _inv(transform: str) -> Callable[[float], float]:
    return math.exp if transform == "log" else (lambda v: v)


@dataclass
class SeasonalFit:
    """What a seasonal baseline learned: flat or weekly, noise scale, and the month-end effect."""

    model: str
    scale: float
    transform: str
    month_end_uplift: float | None
    month_end_seen: int
    drift: float = 0.0  # change per day in the transformed space, from week-over-week differences


def _points(days: list[int], values: list[float], cal: Calendar, transform: str) -> list[tuple[int, float]]:
    f = _fwd(transform)
    return [(d, f(v)) for d, v in zip(days, values, strict=True) if not cal.special(d)]


def _expected(model: str, pts: list[tuple[int, float]], target: int, cal: Calendar, drift: float = 0.0) -> float:
    """Recent level (last 4 same weekdays, or last 14 days), carried forward to `target` by the learned drift."""
    if model == "weekly":
        w = cal.weekday(target)
        same = [(d, v) for d, v in pts if d < target and cal.weekday(d) == w][-4:]
        if same:
            lag = target - sum(d for d, _ in same) / len(same)
            return median([v for _, v in same]) + drift * lag
    recent = [(d, v) for d, v in pts if d < target][-14:]
    if not recent:
        return median([v for _, v in pts])
    lag = target - sum(d for d, _ in recent) / len(recent)
    return median([v for _, v in recent]) + drift * lag


def fit_seasonal(days: list[int], values: list[float], cal: Calendar, *, transform: str = "log",
                 min_scale: float = 0.03) -> SeasonalFit:
    """Choose flat or day-of-week by which leaves smaller residuals, and learn the month-end effect."""
    pts = _points(days, values, cal, transform)
    if len(pts) < 7:
        f = _fwd(transform)
        pts = [(d, f(v)) for d, v in zip(days, values, strict=True)]
    flat_med = median([v for _, v in pts])
    flat_resid = [v - flat_med for _, v in pts]
    by_dow: dict[int, list[float]] = {}
    for d, v in pts:
        by_dow.setdefault(cal.weekday(d), []).append(v)
    model, resid = "flat", flat_resid
    if len(by_dow) == 7 and all(len(v) >= 2 for v in by_dow.values()):
        dow_med = {w: median(v) for w, v in by_dow.items()}
        weekly_resid = [v - dow_med[cal.weekday(d)] for d, v in pts]
        if mad(weekly_resid) < 0.8 * mad(flat_resid):
            model, resid = "weekly", weekly_resid
    by_day = dict(pts)
    weekly_diffs = [v - by_day[d - 7] for d, v in pts if d - 7 in by_day]
    drift = median(weekly_diffs) / 7 if len(weekly_diffs) >= 7 else 0.0
    f = _fwd(transform)
    prior = [(d, f(v)) for d, v in zip(days, values, strict=True) if cal.month_end(d) and not cal.holiday(d)]
    uplift = median([v - _expected(model, pts, d, cal, drift) for d, v in prior]) if prior else None
    # In-sample residuals understate the error of a forecast from a few recent points, so inflate by 1.25.
    return SeasonalFit(model, max(1.25 * mad(resid), min_scale), transform, uplift, len(prior), drift)


def predict_seasonal(fit: SeasonalFit, days: list[int], values: list[float], day: int, cal: Calendar,
                     k: float) -> Bounds:
    """Expected range for `day`: recent same-weekday level (or recent level), plus calendar adjustments."""
    pts = _points(days, values, cal, fit.transform) or _points(days, values, Calendar(cal.weekday, _no, _no),
                                                                  fit.transform)
    scale = fit.scale
    exp = _expected(fit.model, pts, day, cal, fit.drift)
    lo, hi = exp - k * scale, exp + k * scale
    is_log = fit.transform == "log"
    if cal.month_end(day):
        if fit.month_end_uplift is not None:
            exp += fit.month_end_uplift
            widen = 1.5 if fit.month_end_seen == 1 else 1.2
            lo, hi = exp - widen * k * scale, exp + widen * k * scale
        else:
            extra = max(k * scale, math.log(1.8)) if is_log else 2 * k * scale
            lo, hi = lo - extra, hi + extra
    if cal.holiday(day):
        lo -= max(2 * k * scale, math.log(3.0)) if is_log else 2 * k * scale
        hi += k * scale
    inv = _inv(fit.transform)
    return Bounds(inv(exp), inv(lo), inv(hi), fit.model)


def _no(_day: int) -> bool:
    return False


def seasonal_bounds(days: list[int], values: list[float], day: int, cal: Calendar, k: float, *,
                    transform: str = "log", min_scale: float = 0.03, warmup: int = 21) -> Bounds | None:
    """Fit and predict in one call. Returns None while warming up (fewer than `warmup` clean points)."""
    if len(values) < warmup:
        return None
    fit = fit_seasonal(days, values, cal, transform=transform, min_scale=min_scale)
    return predict_seasonal(fit, days, values, day, cal, k)


def rate_bounds(values: list[float], sizes: list[int], n_today: int, k: float, *, floor: float,
                two_sided: bool = False, warmup: int = 14) -> Bounds | None:
    """Bounds for a rate (null rate, duplicate rate, orphan rate).

    Expected is the pooled rate over the history. The noise is the larger of the history's MAD and the binomial
    noise at today's sample size, so a table with 60 rows a day gets a wider band than one with 6,000.
    """
    if len(values) < warmup:
        return None
    total = sum(sizes)
    pooled = sum(v * n for v, n in zip(values, sizes, strict=True)) / total if total else median(values)
    p = max(pooled, 0.5 / max(n_today, 1))
    binom = math.sqrt(p * (1 - p) / n_today) if n_today else 1.0
    s = max(mad(values), binom)
    width = max(k * s, floor)
    lower = max(0.0, pooled - width) if two_sided else -math.inf
    return Bounds(pooled, lower, min(1.0, pooled + width), "rate")


def chi2_quantile(df: int, z: float = 3.72) -> float:
    """Wilson-Hilferty approximation of a chi-square quantile; z=3.72 is the 99.99th percentile."""
    df = max(df, 1)
    a = 2 / (9 * df)
    return df * (1 - a + z * math.sqrt(a)) ** 3


def psi_noise(n_current: int, n_baseline: int, categories: int) -> float:
    """PSI you would see from sampling alone. Small samples or many categories need a higher threshold."""
    if n_current <= 0:
        return math.inf
    return chi2_quantile(categories - 1) * (1 / n_current + 1 / max(n_baseline, 1))


def upper_bounds(values: list[float], k: float, *, floor: float, min_scale: float, warmup: int) -> Bounds | None:
    """One-sided bound for metrics where only high values are bad: arrival hours, PSI."""
    if len(values) < warmup:
        return None
    med = median(values)
    s = max(mad(values), min_scale)
    return Bounds(med, -math.inf, med + max(k * s, floor), "flat")


def psi(baseline: dict[str, float], current: dict[str, float], smoothing: float = 0.5) -> float:
    """Population stability index between two categorical distributions given as counts.

    Adds `smoothing` to every category count so a category missing from a small day does not explode the index.
    """
    cats = set(baseline) | set(current)
    bt = sum(baseline.values()) + smoothing * len(cats)
    ct = sum(current.values()) + smoothing * len(cats)
    total = 0.0
    for c in cats:
        p = (baseline.get(c, 0) + smoothing) / bt
        q = (current.get(c, 0) + smoothing) / ct
        total += (q - p) * math.log(q / p)
    return total


def relevel(before: list[float], after: list[float], transform: str = "log", before_days: list[int] | None = None,
            after_days: list[int] | None = None, weekday: Callable[[int], int] | None = None) -> list[float]:
    """Mark-as-expected feedback: shift older history to the level of the points the owner accepted.

    The offset is the median residual of the accepted points against the old level, in the transformed space.
    With days and a weekday function, each accepted point is compared with the same weekday, so the weekday
    pattern learned from the older history is kept.
    """
    if not after or not before:
        return before
    f, inv = _fwd(transform), _inv(transform)
    yb = [f(v) for v in before]
    ya = [f(v) for v in after]
    flat = median(yb[-14:])
    resid = []
    for i, v in enumerate(ya):
        ref = flat
        if before_days and after_days and weekday:
            same = [y for d, y in zip(before_days, yb, strict=True) if weekday(d) == weekday(after_days[i])][-4:]
            ref = median(same) if same else flat
        resid.append(v - ref)
    offset = median(resid)
    return [inv(v + offset) for v in yb]
