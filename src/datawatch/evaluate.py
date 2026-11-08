"""Score a monitoring run against the planted incidents.

- Detected: an alert on the incident's source table, from one of the monitors that should catch it, between the
  first bad day and one day after the last.
- Time to detection: hours from when the bad partition landed to the alert. For freshness it counts from when the
  partition should have landed (the table's median arrival) to when the learned or static deadline passed.
- Attributed: an alert on the source table or any lineage descendant from the first bad day to two days after
  the last. Every other alert is a false alert.
- Grouping: alerts (one per table, monitor, and day) versus incidents after lineage grouping and dedup.
"""

from __future__ import annotations

import statistics

from .config import Config
from .engine import Alert, RunResult
from .generate import PlantedIncident
from .incidents import Incident, group_alerts
from .profile import Profile


def attributed(planted: list[PlantedIncident], alert: Alert) -> str | None:
    for p in planted:
        if alert.table in p.affected and p.start_day <= alert.day <= p.end_day + 2:
            return p.id
    return None


def score(cfg: Config, profile: Profile, planted: list[PlantedIncident], run: RunResult,
          incidents: list[Incident] | None = None) -> dict:
    incidents = incidents if incidents is not None else group_alerts(cfg, run.alerts)
    inc_of = {a.id: inc for inc in incidents for a in inc.alerts}
    warmup = int(cfg.monitors.get("warmup_days", 28))
    rows = []
    for p in planted:
        hits = [a for a in run.alerts if a.table == p.table and a.monitor in p.monitors
                and p.start_day <= a.day <= p.end_day + 1]
        hits.sort(key=lambda a: (a.day, a.detected_hour))
        arrivals = [v for v in profile.series[(p.table, "freshness", "arrival_hours")][:warmup] if v is not None]
        expected_arrival = statistics.median(arrivals)
        row = {"id": p.id, "kind": p.kind, "table": p.table, "start_day": p.start_day, "detected": bool(hits),
               "ttd_hours": None, "detected_by": None, "tables_flagged": 0, "tables_affected": len(p.affected),
               "root_cause_correct": None, "incident": None}
        if hits:
            h = hits[0]
            # Freshness counts from when the partition should have landed; other monitors run when it lands.
            actual = profile.series[(p.table, "freshness", "arrival_hours")][p.start_day] or expected_arrival
            start = expected_arrival if h.monitor == "freshness" else actual
            row["ttd_hours"] = round(max(0.0, (h.day - p.start_day) * 24 + h.detected_hour - start), 2)
            row["detected_by"] = h.monitor
            inc = inc_of.get(h.id)
            if inc:
                row["incident"] = inc.id
                row["root_cause_correct"] = inc.root_table == p.table
        row["tables_flagged"] = len({a.table for a in run.alerts if attributed([p], a)})
        rows.append(row)
    false = [a for a in run.alerts if attributed(planted, a) is None]
    weeks = (cfg.days - warmup) / 7
    n_tables = len(cfg.tables)
    per_table: dict[str, int] = {}
    for a in false:
        per_table[a.table] = per_table.get(a.table, 0) + 1
    false_incidents = [i for i in incidents if all(attributed(planted, a) is None for a in i.alerts)]
    detected = [r for r in rows if r["detected"]]
    return {
        "mode": run.mode,
        "incidents": rows,
        "detection_rate": len(detected) / len(rows),
        "median_ttd_hours": statistics.median([r["ttd_hours"] for r in detected]) if detected else None,
        "root_cause_accuracy": (sum(1 for r in detected if r["root_cause_correct"]) / len(detected)) if detected
        else None,
        "alerts": len(run.alerts),
        "false_alerts": len(false),
        "false_alerts_per_table_week": round(len(false) / (n_tables * weeks), 4),
        "noisiest_tables": sorted(per_table.items(), key=lambda kv: -kv[1])[:5],
        "incident_count": len(incidents),
        "false_incidents": len(false_incidents),
        "alert_reduction": round(1 - len(incidents) / len(run.alerts), 4) if run.alerts else 0.0,
        "weeks_scored": round(weeks, 2),
    }


def by_type(learned: dict, static: dict) -> list[dict]:
    s = {r["id"]: r for r in static["incidents"]}
    out = []
    for r in learned["incidents"]:
        o = s[r["id"]]
        out.append({"id": r["id"], "kind": r["kind"], "table": r["table"], "learned_detected": r["detected"],
                    "learned_ttd_hours": r["ttd_hours"], "static_detected": o["detected"],
                    "static_ttd_hours": o["ttd_hours"], "root_cause_correct": r["root_cause_correct"],
                    "tables_flagged": r["tables_flagged"], "tables_affected": r["tables_affected"]})
    return out


def gate(result: dict, max_false_per_table_week: float) -> list[str]:
    """Reasons the CI gate fails. Empty means pass."""
    problems = []
    learned = result["learned"]
    for r in learned["incidents"]:
        if not r["detected"]:
            problems.append(f"planted incident {r['id']} ({r['kind']}) was not detected")
    if learned["false_alerts_per_table_week"] > max_false_per_table_week:
        problems.append(f"false alerts per table per week {learned['false_alerts_per_table_week']:.3f} "
                        f"is above the limit {max_false_per_table_week}")
    return problems
