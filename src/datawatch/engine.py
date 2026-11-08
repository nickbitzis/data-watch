"""Run every monitor over every series, day by day, in one of two modes.

learned: baselines learned per table and column (baselines.py), warm-up, alerted days kept out of the history,
         mark-as-expected feedback.
static:  the fixed rules a team writes by hand on day one: compare to the trailing 7-day mean with one global
         threshold per monitor, no seasonality, no per-table tuning.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from . import baselines as bl
from .config import Config
from .profile import Key, Profile

MONITORS = ("freshness", "volume", "schema", "distribution", "null_rate", "uniqueness", "referential")
RATE_FLOORS = {"null_rate": 0.02, "uniqueness": 0.002, "referential": 0.005}
STATIC_FRESHNESS_HOURS = 8.0


@dataclass
class Finding:
    table: str
    monitor: str
    metric: str
    day: int
    value: float | None
    expected: float | None
    lower: float | None
    upper: float | None
    message: str


@dataclass
class Alert:
    """All findings for one (table, monitor, day). This is the unit counted as "an alert"."""

    table: str
    monitor: str
    day: int
    findings: list[Finding]
    severity: str
    detected_hour: float

    @property
    def id(self) -> str:
        return f"{self.table}:{self.monitor}:{self.day}"

    def to_dict(self) -> dict:
        return {
            "id": self.id, "table": self.table, "monitor": self.monitor, "day": self.day, "severity": self.severity,
            "detected_hour": round(self.detected_hour, 2),
            "findings": [{"metric": f.metric, "value": _r(f.value), "expected": _r(f.expected),
                          "lower": _r(f.lower), "upper": _r(f.upper), "message": f.message} for f in self.findings],
        }


@dataclass
class Point:
    day: int
    value: float | None
    expected: float | None = None
    lower: float | None = None
    upper: float | None = None
    status: str = "warmup"  # warmup | ok | alert | expected | missing


@dataclass
class RunResult:
    mode: str
    alerts: list[Alert]
    points: dict[Key, list[Point]] = field(default_factory=dict)
    findings: dict[Key, list[Finding]] = field(default_factory=dict)


def _r(v: float | None) -> float | None:
    if v is None or (isinstance(v, float) and math.isinf(v)):
        return None
    return round(v, 6)


def calendar(cfg: Config) -> bl.Calendar:
    """Calendar features precomputed per day (plus a margin) so the hot loop does list lookups only."""
    n = cfg.days + 60
    wd = [cfg.day_date(d).weekday() for d in range(n)]
    me = [cfg.is_month_end(d) for d in range(n)]
    hol = [cfg.is_holiday(d) for d in range(n)]
    return bl.Calendar(wd.__getitem__, me.__getitem__, hol.__getitem__)


class Engine:
    def __init__(self, cfg: Config, profile: Profile, mode: str = "learned",
                 feedback: dict[Key, set[int]] | None = None, sensitivity: str | None = None):
        if mode not in ("learned", "static"):
            raise ValueError(mode)
        self.cfg = cfg
        self.p = profile
        self.mode = mode
        self.feedback = feedback or {}
        self.cal = calendar(cfg)
        self.sensitivity = sensitivity
        m = cfg.monitors
        self.warmup = int(m.get("warmup_days", 21))
        self.fresh_warmup = int(m.get("freshness_warmup_days", 14))
        self.window = int(m.get("window_days", 56))

    def k(self, table: str) -> float:
        if self.sensitivity:
            from .config import SENSITIVITY_K

            return SENSITIVITY_K[self.sensitivity]
        return self.cfg.k_for(table)

    # ---------------------------------------------------------------- driver
    def run(self) -> RunResult:
        result = RunResult(self.mode, [])
        for key in self.all_keys():
            self._score(key, result)
        result.alerts = self._alerts([f for fs in result.findings.values() for f in fs])
        return result

    def rescore(self, result: RunResult, keys: set[Key]) -> RunResult:
        """Recompute only `keys` (after feedback on an incident) and rebuild the alert list."""
        for key in keys:
            self._score(key, result)
        result.alerts = self._alerts([f for fs in result.findings.values() for f in fs])
        return result

    def all_keys(self) -> list[Key]:
        keys = [k for k in self.p.series if k[0] in self.cfg.tables and not self._skip(k)]
        keys += [(t, "distribution", f"{c}.psi") for (t, c) in self.p.categories if t in self.cfg.tables]
        keys += [(t, "schema", "columns") for t in self.p.schemas if t in self.cfg.tables]
        return keys

    def _score(self, key: Key, result: RunResult) -> None:
        table, monitor, metric = key
        if monitor == "schema":
            result.findings[key] = self._schema(table, self.p.schemas[table])
            return
        if metric.endswith(".psi"):
            pts, fnd = self._psi(key, self.p.categories[(table, metric[:-4])])
        else:
            pts, fnd = self._series(key, self.p.series[key])
        result.points[key] = pts
        result.findings[key] = fnd

    def _skip(self, key: Key) -> bool:
        """Quantiles of a handful of rows are noise; only profile them where a partition has 50+ rows."""
        if not key[2].endswith((".p50", ".p95")):
            return False
        counts = [v for v in self.p.series.get((key[0], "volume", "row_count"), []) if v is not None]
        if not counts or bl.median(counts) < 50:
            return True
        # A quantile of a column with a few distinct values jumps between them; the mean covers that column.
        early = [v for v in self.p.series[key][: self.warmup] if v is not None]
        return len({round(v, 6) for v in early}) <= 5

    def _arrival(self, table: str, day: int) -> float:
        s = self.p.series.get((table, "freshness", "arrival_hours"))
        v = s[day] if s else None
        return v if v is not None else 0.0

    def _alerts(self, findings: list[Finding]) -> list[Alert]:
        groups: dict[tuple[str, str, int], list[Finding]] = {}
        for f in findings:
            groups.setdefault((f.table, f.monitor, f.day), []).append(f)
        out = []
        for (table, monitor, day), fs in sorted(groups.items(), key=lambda kv: (kv[0][2], kv[0][0], kv[0][1])):
            if monitor == "freshness":
                hour = min(f.upper if f.upper is not None else self._arrival(table, day) for f in fs)
            else:
                hour = self._arrival(table, day)
            out.append(Alert(table, monitor, day, fs, _severity(monitor, fs), hour))
        return out

    # ---------------------------------------------------------------- numeric series
    def _series(self, key: Key, values: list[float | None]) -> tuple[list[Point], list[Finding]]:
        table, monitor, metric = key
        fb = self.feedback.get(key, set()) if self.mode == "learned" else set()
        sizes = self.p.sizes.get(key)
        k = self.k(table)
        transform = "log"
        warm = [v for v in values[: self.warmup] if v is not None]
        if monitor in ("volume", "distribution") and (not warm or min(warm) <= 0 or "rate" in metric):
            transform = "id"
        self._fit: bl.SeasonalFit | None = None
        self._fit_day = -999
        clean: list[tuple[int, float, int]] = []  # (day, value, size)
        pts: list[Point] = []
        out: list[Finding] = []
        for d, v in enumerate(values):
            if v is None:
                pts.append(Point(d, None, status="missing"))
                continue
            hist = [h for h in clean if h[0] >= d - self.window]
            if self.mode == "static":
                b = self._static_bounds(monitor, metric, [h[1] for h in clean[-7:]], v)
            else:
                b = self._learned_bounds(monitor, metric, hist, d, k, transform, fb,
                                         sizes[d] if sizes else 0)
            pt = Point(d, v, *(_bounds_tuple(b)))
            if d in fb:
                pt.status = "expected"
                clean.append((d, v, sizes[d] if sizes else 0))
            elif b is None:
                pt.status = "warmup"
                clean.append((d, v, sizes[d] if sizes else 0))
            elif b.contains(v):
                pt.status = "ok"
                clean.append((d, v, sizes[d] if sizes else 0))
            else:
                pt.status = "alert"
                if self.mode == "static":
                    clean.append((d, v, sizes[d] if sizes else 0))  # static rules have no notion of a clean history
                out.append(Finding(table, monitor, metric, d, v, b.expected, b.lower, b.upper,
                                   _message(monitor, metric, v, b)))
            pts.append(pt)
        return pts, out

    def _learned_bounds(self, monitor: str, metric: str, hist: list[tuple[int, float, int]], d: int, k: float,
                        transform: str, fb: set[int], n_today: int) -> bl.Bounds | None:
        days = [h[0] for h in hist]
        vals = [h[1] for h in hist]
        # Feedback: accepted points join the history. When two or more days were accepted, the owner is saying
        # the level changed, so older history is shifted to the accepted level (see baselines.relevel).
        prior_fb = [x for x in fb if x < d]
        sizes = [h[2] for h in hist]
        if len(prior_fb) >= 2:
            c = min(prior_fb)
            pairs = list(zip(days, vals, strict=True))
            bd = [dd for dd, _ in pairs if dd < c]
            ad = [dd for dd, _ in pairs if dd >= c]
            before = [v for dd, v in pairs if dd < c]
            after = [v for dd, v in pairs if dd >= c]
            tr = transform if monitor in ("volume", "distribution") else "id"
            vals = bl.relevel(before, after, tr, bd, ad, self.cal.weekday) + after
        if monitor == "freshness":
            return bl.upper_bounds(vals, k, floor=1.0, min_scale=0.25, warmup=self.fresh_warmup)
        if monitor in RATE_FLOORS:
            return bl.rate_bounds(vals, sizes, n_today, k, floor=RATE_FLOORS[monitor], warmup=self.fresh_warmup)
        if metric.endswith("distinct_ratio"):
            return bl.rate_bounds(vals, sizes, n_today, k, floor=0.05, two_sided=True, warmup=self.fresh_warmup)
        if len(vals) < self.warmup:
            return None
        # Baselines are refit weekly (and whenever feedback changes the history) and scored daily.
        if self._fit is None or d - self._fit_day >= 7 or prior_fb:
            min_scale = 0.03
            med = abs(bl.median(vals))
            if transform == "id":
                min_scale = 0.03 * med + 1e-6
            if all(float(v).is_integer() for v in vals):
                # Counts and integer quantiles move in steps of 1, so a band narrower than one step is noise.
                min_scale = max(min_scale, math.log(1 + 1 / max(med, 1)) if transform == "log" else 1.0)
            self._fit = bl.fit_seasonal(days, vals, self.cal, transform=transform, min_scale=min_scale)
            self._fit_day = d
        return bl.predict_seasonal(self._fit, days, vals, d, self.cal, k)

    def _static_bounds(self, monitor: str, metric: str, last7: list[float], v: float) -> bl.Bounds | None:
        if monitor == "freshness":
            return bl.Bounds(STATIC_FRESHNESS_HOURS / 2, -math.inf, STATIC_FRESHNESS_HOURS, "static")
        if monitor == "uniqueness":
            return bl.Bounds(0.0, -math.inf, 0.0, "static")
        if monitor == "referential":
            return bl.Bounds(0.0, -math.inf, 0.01, "static")
        if len(last7) < 7:
            return None
        m = sum(last7) / len(last7)
        if monitor == "null_rate":
            return bl.Bounds(m, -math.inf, m + 0.05, "static")
        if metric.endswith("distinct_ratio"):
            return bl.Bounds(m, m - 0.1, m + 0.1, "static")
        tol = 0.3 if monitor == "volume" else 0.25
        width = abs(m) * tol if m else tol
        return bl.Bounds(m, m - width, m + width, "static")

    # ---------------------------------------------------------------- categorical PSI
    def _psi(self, key: Key, dists: list[dict[str, int]]) -> tuple[list[Point], list[Finding]]:
        table, monitor, metric = key
        fb = self.feedback.get(key, set()) if self.mode == "learned" else set()
        k = self.k(table)
        clean_days: list[int] = []
        psi_hist: list[float] = []
        pts: list[Point] = []
        out: list[Finding] = []
        for d, cur in enumerate(dists):
            if not cur:
                pts.append(Point(d, None, status="missing"))
                continue
            span = 7 if self.mode == "static" else 28
            base_days = (list(range(max(0, d - 7), d)) if self.mode == "static" else clean_days[-span:])
            if len(base_days) < 7:
                pts.append(Point(d, None, status="warmup"))
                clean_days.append(d)
                continue
            pooled: dict[str, float] = {}
            for bd in base_days:
                for c, n in dists[bd].items():
                    pooled[c] = pooled.get(c, 0) + n
            v = bl.psi(pooled, cur)
            if self.mode == "static":
                b = bl.Bounds(0.0, -math.inf, 0.25, "static")
            else:
                b = bl.upper_bounds(psi_hist[-self.window:], k, floor=0.1, min_scale=0.02, warmup=7)
                if b is not None:
                    noise = bl.psi_noise(sum(cur.values()), int(sum(pooled.values())), len(set(pooled) | set(cur)))
                    b.upper = max(b.upper, noise)
            pt = Point(d, v, *(_bounds_tuple(b)))
            if d in fb or b is None or b.contains(v):
                pt.status = "expected" if d in fb else ("warmup" if b is None else "ok")
                clean_days.append(d)
                psi_hist.append(v)
            else:
                pt.status = "alert"
                top = max(cur, key=lambda c: abs(cur[c] / sum(cur.values()) - pooled.get(c, 0) / sum(pooled.values())))
                out.append(Finding(table, monitor, metric, d, v, b.expected, b.lower, b.upper,
                                   f"category mix shifted, PSI {v:.2f} above {b.upper:.2f} (largest change: {top})"))
            pts.append(pt)
        return pts, out

    # ---------------------------------------------------------------- schema
    def _schema(self, table: str, snaps: list[dict[str, str]]) -> list[Finding]:
        out = []
        fb = self.feedback.get((table, "schema", "columns"), set()) if self.mode == "learned" else set()
        for d in range(1, len(snaps)):
            prev, cur = snaps[d - 1], snaps[d]
            if not prev or not cur or prev == cur or d in fb:
                continue
            removed = sorted(set(prev) - set(cur))
            added = sorted(set(cur) - set(prev))
            changed = sorted(c for c in set(prev) & set(cur) if prev[c] != cur[c])
            parts = [f"removed {c}" for c in removed] + [f"added {c}" for c in added]
            parts += [f"{c} {prev[c]} to {cur[c]}" for c in changed]
            out.append(Finding(table, "schema", "columns", d, float(len(parts)), None, None, None,
                               "schema changed: " + ", ".join(parts)))
        return out


def schema_diff(prev: dict[str, str], cur: dict[str, str]) -> dict[str, list]:
    return {
        "removed": sorted(set(prev) - set(cur)),
        "added": sorted(set(cur) - set(prev)),
        "changed": sorted((c, prev[c], cur[c]) for c in set(prev) & set(cur) if prev[c] != cur[c]),
    }


def _bounds_tuple(b: bl.Bounds | None) -> tuple:
    if b is None:
        return (None, None, None)
    return (b.expected, None if math.isinf(b.lower) else b.lower, None if math.isinf(b.upper) else b.upper)


def _severity(monitor: str, fs: list[Finding]) -> str:
    if monitor in ("schema", "referential", "uniqueness", "freshness"):
        return "high"
    for f in fs:
        if f.value is not None and f.expected and f.value > 0 and f.expected > 0:
            if abs(math.log(f.value / f.expected)) > math.log(1.5):
                return "high"
    return "medium"


def _fmt(v: float) -> str:
    if abs(v) >= 1000:
        return f"{v:,.0f}"
    if abs(v) >= 10:
        return f"{v:.1f}"
    return f"{v:.3g}"


def _message(monitor: str, metric: str, v: float, b: bl.Bounds) -> str:
    lo = "" if math.isinf(b.lower) else _fmt(b.lower)
    hi = "" if math.isinf(b.upper) else _fmt(b.upper)
    band = f"{lo} to {hi}" if lo and hi else (f"at most {hi}" if hi else f"at least {lo}")
    if monitor == "freshness":
        return f"arrived {v:.1f} h after midnight, expected by {hi} h"
    if monitor == "volume":
        return f"{_fmt(v)} rows, expected {band}"
    if monitor in RATE_FLOORS:
        limit = f"at most {b.upper:.1%}" if hi else band
        return f"{metric} {monitor.replace('_', ' ')} {v:.1%}, expected {limit}"
    return f"{metric} {_fmt(v)}, expected {band}"
