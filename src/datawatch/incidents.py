"""Turn alerts into incidents: group by lineage, pick a root cause, dedup across days, route to owners.

Grouping rule: on a given day, two alerted tables belong together when one is upstream of the other on the
lineage graph. A group joins an open incident from the last `dedup_gap_days` days when they share a table or
one's tables sit downstream of the other's root. The root cause candidate is the most upstream alerted table: the
one with no alerted ancestor in the incident, preferring the one that explains the most alerted descendants.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import Config
from .engine import Alert

STATUSES = ("open", "acknowledged", "snoozed", "resolved", "expected", "recovered")


@dataclass
class Incident:
    id: str
    first_day: int
    last_day: int
    alerts: list[Alert] = field(default_factory=list)
    root_table: str = ""
    root_candidates: list[str] = field(default_factory=list)
    root_reason: str = ""
    severity: str = "medium"
    routing: dict = field(default_factory=dict)
    status: str = "open"
    snoozed_until: int | None = None
    notes: list[dict] = field(default_factory=list)

    @property
    def tables(self) -> set[str]:
        return {a.table for a in self.alerts}

    @property
    def monitors(self) -> list[str]:
        return sorted({a.monitor for a in self.alerts})

    def summary(self, cfg: Config) -> dict:
        mons = ", ".join(self.monitors)
        n = len(self.tables)
        title = f"{mons} anomaly on {self.root_table}" + (f" and {n - 1} downstream tables" if n > 1 else "")
        return {
            "id": self.id, "title": title, "first_day": self.first_day, "last_day": self.last_day,
            "first_date": str(cfg.day_date(self.first_day)), "root_table": self.root_table,
            "root_candidates": self.root_candidates, "root_reason": self.root_reason, "severity": self.severity,
            "status": self.status, "snoozed_until": self.snoozed_until, "monitors": self.monitors,
            "tables": sorted(self.tables, key=lambda t: (cfg.depth[t], t)), "alert_count": len(self.alerts),
            "routing": self.routing,
        }


def _component_groups(cfg: Config, tables: set[str]) -> list[set[str]]:
    parent = {t: t for t in tables}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for t in tables:
        for other in cfg.descendants(t) & tables:
            parent[find(other)] = find(t)
    groups: dict[str, set[str]] = {}
    for t in tables:
        groups.setdefault(find(t), set()).add(t)
    return sorted(groups.values(), key=lambda g: min(g))


def _related(cfg: Config, group: set[str], inc: Incident) -> bool:
    tabs = inc.tables
    if group & tabs:
        return True
    down = cfg.descendants(inc.root_table)
    return any(t in down or inc.root_table in cfg.descendants(t) for t in group)


def choose_root(cfg: Config, alerts: list[Alert]) -> tuple[str, list[str], str]:
    """Most upstream anomalous table. Ties go to the table with more alerted descendants, then the earliest
    alert, then the shallowest layer."""
    tables = {a.table for a in alerts}
    first = {}
    for a in alerts:
        first[a.table] = min(first.get(a.table, (a.day, a.detected_hour)), (a.day, a.detected_hour))
    cands = [t for t in tables if not (cfg.ancestors(t) & tables)]
    cands.sort(key=lambda t: (-len(cfg.descendants(t) & tables), first[t], cfg.depth[t], t))
    root = cands[0]
    explained = len(cfg.descendants(root) & tables)
    reason = (f"{root} has no anomalous upstream table and {explained} of the {len(tables) - 1} other alerted "
              f"tables are downstream of it" if len(tables) > 1 else f"only {root} is anomalous")
    return root, cands, reason


def group_alerts(cfg: Config, alerts: list[Alert], gap: int | None = None) -> list[Incident]:
    gap = int(cfg.monitors.get("dedup_gap_days", 2)) if gap is None else gap
    incidents: list[Incident] = []
    by_day: dict[int, list[Alert]] = {}
    for a in alerts:
        by_day.setdefault(a.day, []).append(a)
    for day in sorted(by_day):
        day_alerts = by_day[day]
        for group in _component_groups(cfg, {a.table for a in day_alerts}):
            group_alerts_ = [a for a in day_alerts if a.table in group]
            match = None
            for inc in reversed(incidents):
                if inc.last_day >= day - gap and _related(cfg, group, inc):
                    match = inc
                    break
            if match is None:
                root, _, _ = choose_root(cfg, group_alerts_)
                match = Incident(id=f"INC-{day:03d}-{root}", first_day=day, last_day=day)
                incidents.append(match)
            match.alerts.extend(group_alerts_)
            match.last_day = day
            match.root_table, match.root_candidates, match.root_reason = choose_root(cfg, match.alerts)
    for inc in incidents:
        inc.severity = "high" if any(a.severity == "high" for a in inc.alerts) else "medium"
        inc.routing = route(cfg, inc)
    return incidents


def _matches(rule_match: dict, ctx: dict) -> bool:
    return all(ctx.get(k) == v for k, v in rule_match.items())


def route(cfg: Config, inc: Incident) -> dict:
    """First matching rule wins. The root cause owner is assigned; owners of affected tables are notified."""
    root = cfg.tables[inc.root_table]
    critical = sorted(t for t in inc.tables if cfg.tables[t].critical)
    ctx = {"critical": bool(critical), "layer": root.layer, "severity": inc.severity}
    rule = next(r for r in cfg.routing if _matches(r.get("match", {}), ctx))
    owner = cfg.owners[root.owner]
    action = rule["action"]
    return {
        "rule": rule["name"], "action": action, "priority": rule.get("priority", "P1" if action == "page" else "P3"),
        "assignee": root.owner, "target": owner["pager"] if action == "page" else owner["queue"],
        "notify": sorted({cfg.tables[t].owner for t in inc.tables} - {root.owner}),
        "slack": owner["slack"], "critical_tables": critical,
    }


def apply_state(incidents: list[Incident], state: dict[str, dict], as_of: int, recover_after: int = 3) -> None:
    """Overlay owner actions (acknowledge, snooze, resolve, expected) and auto-recover quiet incidents."""
    for inc in incidents:
        st = state.get(inc.id, {})
        inc.notes = list(st.get("notes", []))
        status = st.get("status")
        if status == "snoozed" and st.get("snoozed_until") is not None and st["snoozed_until"] < as_of:
            status = None
        if status:
            inc.status = status
            inc.snoozed_until = st.get("snoozed_until")
        elif inc.last_day < as_of - recover_after:
            inc.status = "recovered"
        else:
            inc.status = "open"


def notifications(incidents: list[Incident], state: dict[str, dict] | None = None) -> list[dict]:
    """What owners receive: one page or ticket per incident, not one per alert. Snoozed incidents send nothing
    for the days they are snoozed."""
    out = []
    state = state or {}
    for inc in incidents:
        snooze_until = state.get(inc.id, {}).get("snoozed_until")
        out.append({"incident": inc.id, "day": inc.first_day, "kind": inc.routing["action"],
                    "target": inc.routing["target"]})
        for day in sorted({a.day for a in inc.alerts} - {inc.first_day}):
            if snooze_until is not None and day <= snooze_until:
                continue
            out.append({"incident": inc.id, "day": day, "kind": "update", "target": inc.routing["slack"]})
    return out
