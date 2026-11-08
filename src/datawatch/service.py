"""The running system: warehouse, profile, both monitoring runs, incidents, owner actions, and the evaluation."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import duckdb

from . import evaluate
from .config import Config
from .engine import Engine, RunResult
from .generate import build, load_planted
from .incidents import Incident, apply_state, group_alerts, notifications
from .profile import Key, profile_warehouse

ACTIONS = ("acknowledge", "snooze", "resolve", "expected", "reopen")


class Service:
    def __init__(self, cfg: Config, db_path: Path | str, scale: float = 1.0, state_path: Path | str | None = None,
                 rebuild: bool = False):
        self.cfg = cfg
        self.db_path = Path(db_path)
        self.lock = threading.Lock()
        if rebuild or not self.db_path.exists():
            self.build_stats = build(cfg, self.db_path, scale=scale)
        else:
            self.build_stats = {"path": str(self.db_path), "reused": True}
        con = duckdb.connect(str(self.db_path), read_only=True)
        try:
            self.profile = profile_warehouse(con, cfg)
            self.planted = load_planted(con)
            self.scale = con.execute("SELECT scale FROM _dw_meta").fetchone()[0]
        finally:
            con.close()
        self.state_path = Path(state_path) if state_path else None
        self.state: dict[str, dict] = {}
        self.feedback: dict[Key, set[int]] = {}
        self.expected_incidents: dict[str, dict] = {}
        if self.state_path and self.state_path.exists():
            saved = json.loads(self.state_path.read_text())
            self.state = saved.get("state", {})
            self.feedback = {tuple(k.split("|")): set(v) for k, v in saved.get("feedback", {}).items()}
            self.expected_incidents = saved.get("expected_incidents", {})
        self.learned_engine = Engine(cfg, self.profile, "learned", self.feedback)
        self.learned: RunResult = self.learned_engine.run()
        self.static: RunResult = Engine(cfg, self.profile, "static").run()
        self.static_incidents = group_alerts(cfg, self.static.alerts)
        self.baseline_eval = self._evaluate()  # the evaluation is fixed at startup, before any owner feedback
        self._regroup()

    @property
    def last_day(self) -> int:
        return self.cfg.days - 1

    def _regroup(self) -> None:
        self.incidents: list[Incident] = group_alerts(self.cfg, self.learned.alerts)
        apply_state(self.incidents, self.state, self.last_day)
        self.by_id = {i.id: i for i in self.incidents}

    def _evaluate(self) -> dict:
        learned_inc = group_alerts(self.cfg, self.learned.alerts)
        learned = evaluate.score(self.cfg, self.profile, self.planted, self.learned, learned_inc)
        static = evaluate.score(self.cfg, self.profile, self.planted, self.static, self.static_incidents)
        return {
            "learned": learned, "static": static, "by_type": evaluate.by_type(learned, static),
            "notifications": {"learned": len(notifications(learned_inc)),
                              "static": len(notifications(self.static_incidents))},
            "planted": [p.to_dict() for p in self.planted], "scale": self.scale,
            "tables": len(self.cfg.tables), "days": self.cfg.days,
        }

    def _save(self) -> None:
        if not self.state_path:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps({
            "state": self.state,
            "feedback": {"|".join(k): sorted(v) for k, v in self.feedback.items()},
            "expected_incidents": self.expected_incidents,
        }, indent=1))

    # ---------------------------------------------------------------- owner actions
    def act(self, incident_id: str, action: str, *, days: int = 3, note: str = "", user: str = "owner") -> dict:
        if action not in ACTIONS:
            raise ValueError(f"unknown action {action}")
        with self.lock:
            inc = self.by_id.get(incident_id)
            if inc is None:
                raise KeyError(incident_id)
            st = self.state.setdefault(incident_id, {"notes": []})
            st["notes"].append({"action": action, "note": note, "user": user, "day": self.last_day})
            if action == "acknowledge":
                st["status"] = "acknowledged"
            elif action == "snooze":
                st["status"] = "snoozed"
                st["snoozed_until"] = inc.last_day + days
            elif action == "resolve":
                st["status"] = "resolved"
            elif action == "reopen":
                st.pop("status", None)
                st.pop("snoozed_until", None)
            elif action == "expected":
                summary = inc.summary(self.cfg)
                keys: set[Key] = set()
                for a in inc.alerts:
                    for f in a.findings:
                        key = (f.table, f.monitor, f.metric)
                        self.feedback.setdefault(key, set()).add(f.day)
                        keys.add(key)
                st["status"] = "expected"
                summary["status"] = "expected"
                summary["notes"] = st["notes"]
                summary["alerts"] = [a.to_dict() for a in inc.alerts]
                self.expected_incidents[incident_id] = summary
                self.learned_engine.feedback = self.feedback
                self.learned_engine.rescore(self.learned, keys)
            self._regroup()
            self._save()
        return self.incident(incident_id)

    # ---------------------------------------------------------------- read models
    def table_status(self, day: int) -> list[dict]:
        alerts_today = {}
        for a in self.learned.alerts:
            if a.day == day:
                alerts_today.setdefault(a.table, []).append(a)
        open_inc = {}
        for inc in self.incidents:
            if inc.first_day <= day <= inc.last_day:
                for t in inc.tables:
                    open_inc[t] = inc.id
        warm = int(self.cfg.monitors.get("warmup_days", 28))
        out = []
        for name in self.cfg.topo_order():
            t = self.cfg.tables[name]
            al = alerts_today.get(name, [])
            if al:
                status = "critical" if any(a.severity == "high" for a in al) else "warning"
            elif day < warm:
                status = "learning"
            else:
                status = "healthy"
            rows = self.profile.series[(name, "volume", "row_count")][day]
            arrival = self.profile.series[(name, "freshness", "arrival_hours")][day]
            out.append({"name": name, "layer": t.layer, "owner": t.owner, "critical": t.critical, "status": status,
                        "alerts": [a.monitor for a in al], "incident": open_inc.get(name), "rows": rows,
                        "arrival_hours": arrival, "depth": self.cfg.depth[name]})
        return out

    def timeline(self) -> list[dict]:
        counts = [0] * self.cfg.days
        for a in self.learned.alerts:
            counts[a.day] += 1
        starts = {}
        for inc in self.incidents:
            starts.setdefault(inc.first_day, []).append(inc.id)
        return [{"day": d, "date": str(self.cfg.day_date(d)), "alerts": counts[d], "incidents": starts.get(d, [])}
                for d in range(self.cfg.days)]

    def table_detail(self, name: str) -> dict:
        t = self.cfg.tables[name]

        def series(key: Key) -> list[dict]:
            return [{"day": p.day, "value": p.value, "expected": p.expected, "lower": p.lower, "upper": p.upper,
                     "status": p.status} for p in self.learned.points.get(key, [])]

        monitors = []
        for key in sorted(self.learned.points):
            if key[0] != name:
                continue
            pts = self.learned.points[key]
            n_alert = sum(1 for p in pts if p.status == "alert")
            monitors.append({"monitor": key[1], "metric": key[2], "alerts": n_alert,
                             "last": next((p.value for p in reversed(pts) if p.value is not None), None)})
        schema = self.profile.schemas.get(name, [])
        changes = []
        for d in range(1, len(schema)):
            if schema[d] != schema[d - 1]:
                changes.append({"day": d, "before": schema[d - 1], "after": schema[d]})
        return {
            "name": name, "layer": t.layer, "owner": t.owner, "owner_contacts": self.cfg.owners[t.owner],
            "critical": t.critical, "description": t.description, "upstream": t.upstream,
            "downstream": self.cfg.children[name], "primary_key": t.primary_key, "foreign_keys": t.foreign_keys,
            "columns": schema[-1] if schema else t.columns,
            "volume": series((name, "volume", "row_count")),
            "freshness": series((name, "freshness", "arrival_hours")),
            "monitors": monitors, "schema_changes": changes,
            "alerts": [a.to_dict() for a in self.learned.alerts if a.table == name],
            "incidents": [i.id for i in self.incidents if name in i.tables],
            "start_date": str(self.cfg.start_date),
        }

    def incident_list(self, status: str | None = None) -> list[dict]:
        out = [i.summary(self.cfg) for i in self.incidents]
        out += [v for k, v in self.expected_incidents.items() if k not in self.by_id]
        if status:
            out = [i for i in out if i["status"] == status]
        return sorted(out, key=lambda i: (-i["first_day"], i["id"]))

    def incident(self, incident_id: str) -> dict:
        inc = self.by_id.get(incident_id)
        if inc is None:
            if incident_id in self.expected_incidents:
                d = dict(self.expected_incidents[incident_id])
                d["lineage"] = self.lineage_view(d["root_table"], set(d["tables"]))
                return d
            raise KeyError(incident_id)
        d = inc.summary(self.cfg)
        d["alerts"] = [a.to_dict() for a in inc.alerts]
        d["notes"] = inc.notes
        d["lineage"] = self.lineage_view(inc.root_table, inc.tables)
        d["notifications"] = [n for n in notifications([inc], self.state)]
        return d

    def lineage_view(self, root: str, affected: set[str]) -> dict:
        """Subgraph for the incident page: the root's direct inputs, the root, and everything downstream of it,
        laid out in columns by lineage depth."""
        nodes = {root} | set(self.cfg.tables[root].upstream) | self.cfg.descendants(root) | affected
        for t in affected:
            nodes |= set(self.cfg.tables[t].upstream)  # other inputs of alerting tables, shown as context
        cols: dict[int, list[str]] = {}
        for n in sorted(nodes, key=lambda n: (self.cfg.depth[n], n)):
            cols.setdefault(self.cfg.depth[n], []).append(n)
        depths = sorted(cols)
        out_nodes = []
        for ci, dep in enumerate(depths):
            for ri, n in enumerate(cols[dep]):
                role = "root" if n == root else ("affected" if n in affected else "context")
                out_nodes.append({"id": n, "col": ci, "row": ri, "layer": self.cfg.tables[n].layer, "role": role,
                                  "critical": self.cfg.tables[n].critical})
        edges = [{"from": u, "to": n} for n in nodes for u in self.cfg.tables[n].upstream if u in nodes]
        return {"nodes": out_nodes, "edges": edges, "columns": len(depths),
                "rows": max(len(v) for v in cols.values())}

    def lineage(self) -> dict:
        return {"tables": [{"name": n, "layer": t.layer, "upstream": t.upstream, "depth": self.cfg.depth[n]}
                           for n, t in self.cfg.tables.items()]}
