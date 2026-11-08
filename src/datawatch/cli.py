"""Command line: build the warehouse, run the evaluation and CI gate, serve the API and UI."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path

from .config import load_config


def _table(rows: list[list[str]], header: list[str]) -> str:
    out = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    out += ["| " + " | ".join(r) + " |" for r in rows]
    return "\n".join(out)


def report_markdown(ev: dict) -> str:
    def h(v):
        return "missed" if v is None else f"{v:.1f} h"

    rows = [[r["kind"], f"`{r['table']}`", "yes" if r["learned_detected"] else "no", h(r["learned_ttd_hours"]),
             "yes" if r["static_detected"] else "no", h(r["static_ttd_hours"]),
             "yes" if r["root_cause_correct"] else "no", f"{r['tables_flagged']} / {r['tables_affected']}"]
            for r in ev["by_type"]]
    t1 = _table(rows, ["Incident type", "Source table", "Learned: detected", "Learned: time to detect",
                       "Static: detected", "Static: time to detect", "Root cause correct",
                       "Tables flagged / downstream reach"])
    L, S = ev["learned"], ev["static"]
    summary = [
        ["Detection rate", f"{L['detection_rate']:.0%}", f"{S['detection_rate']:.0%}"],
        ["Median time to detection", h(L["median_ttd_hours"]), h(S["median_ttd_hours"])],
        ["Alerts (table, monitor, day)", str(L["alerts"]), str(S["alerts"])],
        ["False alerts", str(L["false_alerts"]), str(S["false_alerts"])],
        ["False alerts per table per week", f"{L['false_alerts_per_table_week']:.3f}",
         f"{S['false_alerts_per_table_week']:.3f}"],
        ["Incidents after grouping", str(L["incident_count"]), str(S["incident_count"])],
        ["Incidents with no planted cause", str(L["false_incidents"]), str(S["false_incidents"])],
        ["Root cause correct", f"{L['root_cause_accuracy']:.0%}", f"{S['root_cause_accuracy']:.0%}"],
        ["Alert volume removed by grouping", f"{L['alert_reduction']:.0%}", f"{S['alert_reduction']:.0%}"],
        ["Pages, tickets, and updates sent", str(ev["notifications"]["learned"]), str(ev["notifications"]["static"])],
    ]
    t2 = _table(summary, ["Metric", "Learned baselines", "Static thresholds"])
    return t1 + "\n\n" + t2 + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="datawatch")
    ap.add_argument("--config", default=None)
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="generate the synthetic warehouse")
    b.add_argument("--db", default="data/warehouse.duckdb")
    b.add_argument("--scale", type=float, default=1.0)
    e = sub.add_parser("eval", help="score learned baselines and static thresholds against planted incidents")
    e.add_argument("--db", default="data/warehouse.duckdb")
    e.add_argument("--scale", type=float, default=1.0)
    e.add_argument("--rebuild", action="store_true")
    e.add_argument("--out", default=None, help="write the full result as JSON")
    e.add_argument("--markdown", default=None, help="write the README tables as Markdown")
    e.add_argument("--gate", action="store_true", help="exit 1 unless every planted incident is detected")
    e.add_argument("--max-false-per-table-week", type=float, default=0.1)
    s = sub.add_parser("serve", help="run the API and web UI")
    s.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    s.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8855")))
    hc = sub.add_parser("healthcheck", help="exit 0 if /healthz answers")
    hc.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8855")))
    a = ap.parse_args(argv)
    if a.config:
        os.environ["DATAWATCH_CONFIG"] = a.config

    if a.cmd == "build":
        from .generate import build

        print(json.dumps(build(load_config(), a.db, scale=a.scale)))
        return 0
    if a.cmd == "eval":
        from . import evaluate
        from .service import Service

        svc = Service(load_config(), a.db, scale=a.scale, rebuild=a.rebuild or not Path(a.db).exists())
        ev = svc.baseline_eval
        md = report_markdown(ev)
        print(md)
        if a.out:
            Path(a.out).parent.mkdir(parents=True, exist_ok=True)
            Path(a.out).write_text(json.dumps(ev, indent=1, default=str))
        if a.markdown:
            Path(a.markdown).write_text(md)
        if a.gate:
            problems = evaluate.gate(ev, a.max_false_per_table_week)
            for p in problems:
                print(f"GATE FAIL: {p}", file=sys.stderr)
            if problems:
                return 1
            print(f"gate passed: {len(ev['planted'])} of {len(ev['planted'])} planted incidents detected, "
                  f"{ev['learned']['false_alerts_per_table_week']:.3f} false alerts per table per week "
                  f"(limit {a.max_false_per_table_week})")
        return 0
    if a.cmd == "serve":
        import uvicorn

        from .api import create_app

        uvicorn.run(create_app(), host=a.host, port=a.port, log_level="warning")
        return 0
    if a.cmd == "healthcheck":
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{a.port}/healthz", timeout=3)
        except Exception:
            return 1
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
