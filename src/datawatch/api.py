"""FastAPI service and the web UI."""

from __future__ import annotations

import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .config import load_config
from .obs import Metrics, get_logger, log_event
from .service import ACTIONS, Service

WEB = Path(__file__).parent / "web"


class ActionRequest(BaseModel):
    action: str
    days: int = 3
    note: str = ""
    user: str = "owner"


def default_service() -> Service:
    cfg = load_config()
    data = Path(os.environ.get("DATAWATCH_DATA", "data"))
    scale = float(os.environ.get("DATAWATCH_SCALE", "1.0"))
    return Service(cfg, data / "warehouse.duckdb", scale=scale, state_path=data / "state.json",
                   rebuild=os.environ.get("DATAWATCH_REBUILD", "0") == "1")


def create_app(service: Service | None = None) -> FastAPI:
    log = get_logger()
    metrics = Metrics()
    holder: dict[str, Service] = {}

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        t0 = time.perf_counter()
        holder["svc"] = service or default_service()
        svc = holder["svc"]
        log_event(log, "service ready", seconds=round(time.perf_counter() - t0, 2), tables=len(svc.cfg.tables),
                  alerts=len(svc.learned.alerts), incidents=len(svc.incidents), db=str(svc.db_path))
        yield

    app = FastAPI(title="datawatch", version="1.0.0", lifespan=lifespan)

    def svc() -> Service:
        if "svc" not in holder:
            raise HTTPException(503, "starting")
        return holder["svc"]

    @app.middleware("http")
    async def observe(request: Request, call_next):
        t0 = time.perf_counter()
        response = await call_next(request)
        route = request.scope.get("route")
        path = getattr(route, "path", "unmatched")
        dt = time.perf_counter() - t0
        metrics.observe_request(path, response.status_code, dt)
        if path.startswith("/api"):
            log_event(log, "request", method=request.method, route=path, status=response.status_code,
                      ms=round(dt * 1000, 1))
        return response

    @app.get("/healthz")
    def healthz() -> dict:
        s = svc()
        return {"status": "ok", "tables": len(s.cfg.tables), "days": s.cfg.days, "incidents": len(s.incidents)}

    @app.get("/metrics", response_class=PlainTextResponse)
    def prom() -> str:
        s = svc()
        ev = s.baseline_eval
        status = {}
        for row in s.table_status(s.last_day):
            status[row["status"]] = status.get(row["status"], 0) + 1
        open_by_status = {}
        for inc in s.incidents:
            open_by_status[inc.status] = open_by_status.get(inc.status, 0) + 1
        gauges = [
            ("datawatch_tables", "Monitored tables by health status on the latest day.",
             [({"status": k}, v) for k, v in sorted(status.items())]),
            ("datawatch_incidents", "Incidents by status.",
             [({"status": k}, v) for k, v in sorted(open_by_status.items())]),
            ("datawatch_alerts", "Alerts over the simulated period.", [({"mode": "learned"}, len(s.learned.alerts)),
                                                                       ({"mode": "static"}, len(s.static.alerts))]),
            ("datawatch_detection_rate", "Share of planted incidents detected.",
             [({"mode": m}, ev[m]["detection_rate"]) for m in ("learned", "static")]),
            ("datawatch_false_alerts_per_table_week", "False alerts per table per week.",
             [({"mode": m}, ev[m]["false_alerts_per_table_week"]) for m in ("learned", "static")]),
        ]
        return metrics.render(gauges)

    @app.get("/api/summary")
    def summary() -> dict:
        s = svc()
        return {"company": s.cfg.company, "start_date": str(s.cfg.start_date), "days": s.cfg.days,
                "last_day": s.last_day, "tables": len(s.cfg.tables), "timeline": s.timeline(),
                "incidents": len(s.incidents), "alerts": len(s.learned.alerts), "scale": s.scale}

    @app.get("/api/tables")
    def tables(day: int | None = Query(None, ge=0)) -> dict:
        s = svc()
        d = s.last_day if day is None else min(day, s.last_day)
        return {"day": d, "date": str(s.cfg.day_date(d)), "tables": s.table_status(d)}

    @app.get("/api/tables/{name}")
    def table(name: str) -> dict:
        s = svc()
        if name not in s.cfg.tables:
            raise HTTPException(404, f"unknown table {name}")
        return s.table_detail(name)

    @app.get("/api/incidents")
    def incidents(status: str | None = None) -> dict:
        return {"incidents": svc().incident_list(status)}

    @app.get("/api/incidents/{incident_id}")
    def incident(incident_id: str) -> dict:
        try:
            return svc().incident(incident_id)
        except KeyError as e:
            raise HTTPException(404, f"unknown incident {incident_id}") from e

    @app.post("/api/incidents/{incident_id}/actions")
    def act(incident_id: str, req: ActionRequest) -> dict:
        if req.action not in ACTIONS:
            raise HTTPException(422, f"action must be one of {', '.join(ACTIONS)}")
        try:
            out = svc().act(incident_id, req.action, days=req.days, note=req.note, user=req.user)
        except KeyError as e:
            raise HTTPException(404, f"unknown incident {incident_id}") from e
        metrics.inc_action(req.action)
        log_event(log, "incident action", incident=incident_id, action=req.action, user=req.user)
        return out

    @app.get("/api/evaluation")
    def evaluation() -> dict:
        return svc().baseline_eval

    @app.get("/api/lineage")
    def lineage() -> dict:
        return svc().lineage()

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(WEB / "index.html")

    app.mount("/static", StaticFiles(directory=WEB), name="static")
    return app
