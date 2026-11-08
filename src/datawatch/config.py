"""Warehouse metadata: tables, owners, lineage graph, routing rules, monitor settings."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, timedelta
from functools import cached_property
from pathlib import Path

import yaml

DEFAULT_CONFIG = Path(__file__).resolve().parents[2] / "config" / "warehouse.yml"
LAYERS = ("ingestion", "staging", "marts")
NUMERIC = {"BIGINT", "INTEGER", "DOUBLE"}
SENSITIVITY_K = {"low": 6.0, "medium": 4.5, "high": 3.5}


def config_path() -> Path:
    return Path(os.environ.get("DATAWATCH_CONFIG", DEFAULT_CONFIG))


@dataclass
class Table:
    name: str
    layer: str
    owner: str
    columns: dict[str, str]
    upstream: list[str] = field(default_factory=list)
    primary_key: str | None = None
    foreign_keys: dict[str, str] = field(default_factory=dict)
    roles: dict[str, str] = field(default_factory=dict)
    critical: bool = False
    description: str = ""
    sensitivity: str | None = None

    def column_role(self, col: str) -> str:
        """id, code, timestamp, numeric, categorical, or text. Decides which monitors apply."""
        if col in self.roles:
            return self.roles[col]
        typ = self.columns[col]
        if col.endswith("_id") or col == self.primary_key:
            return "id"
        if typ == "TIMESTAMP":
            return "timestamp"
        if typ in NUMERIC:
            return "numeric"
        if typ == "BOOLEAN":
            return "categorical"
        return "categorical" if col not in ("email", "sku", "landing_page") else "text"


@dataclass
class Config:
    company: str
    start_date: date
    days: int
    holidays: set[date]
    monitors: dict
    owners: dict[str, dict]
    routing: list[dict]
    tables: dict[str, Table]

    def day_date(self, day: int) -> date:
        return self.start_date + timedelta(days=day)

    def is_holiday(self, day: int) -> bool:
        return self.day_date(day) in self.holidays

    def is_month_end(self, day: int) -> bool:
        return (self.day_date(day) + timedelta(days=1)).day == 1

    def k_for(self, table: str) -> float:
        level = self.tables[table].sensitivity or self.monitors.get("sensitivity", "medium")
        return SENSITIVITY_K[level]

    @cached_property
    def children(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {t: [] for t in self.tables}
        for t in self.tables.values():
            for u in t.upstream:
                out[u].append(t.name)
        return out

    def ancestors(self, name: str) -> set[str]:
        seen: set[str] = set()
        stack = list(self.tables[name].upstream)
        while stack:
            n = stack.pop()
            if n not in seen:
                seen.add(n)
                stack.extend(self.tables[n].upstream)
        return seen

    def descendants(self, name: str) -> set[str]:
        seen: set[str] = set()
        stack = list(self.children[name])
        while stack:
            n = stack.pop()
            if n not in seen:
                seen.add(n)
                stack.extend(self.children[n])
        return seen

    @cached_property
    def depth(self) -> dict[str, int]:
        """Longest path from a source. Used for root cause ordering and the SVG layout."""
        memo: dict[str, int] = {}

        def d(n: str) -> int:
            if n not in memo:
                ups = self.tables[n].upstream
                memo[n] = 0 if not ups else 1 + max(d(u) for u in ups)
            return memo[n]

        return {n: d(n) for n in self.tables}

    def topo_order(self) -> list[str]:
        return sorted(self.tables, key=lambda n: (self.depth[n], n))


def load_config(path: Path | str | None = None) -> Config:
    raw = yaml.safe_load(Path(path or config_path()).read_text())
    tables = {}
    for name, t in raw["tables"].items():
        tables[name] = Table(
            name=name,
            layer=t["layer"],
            owner=t["owner"],
            columns=dict(t["columns"]),
            upstream=list(t.get("upstream", [])),
            primary_key=t.get("primary_key"),
            foreign_keys=dict(t.get("foreign_keys", {})),
            roles=dict(t.get("roles", {})),
            critical=bool(t.get("critical", False)),
            description=t.get("description", ""),
            sensitivity=t.get("sensitivity"),
        )
    cfg = Config(
        company=raw["company"],
        start_date=_as_date(raw["start_date"]),
        days=int(raw["days"]),
        holidays={_as_date(h) for h in raw.get("holidays", [])},
        monitors=dict(raw.get("monitors", {})),
        owners=dict(raw["owners"]),
        routing=list(raw["routing"]),
        tables=tables,
    )
    validate(cfg)
    return cfg


def _as_date(v) -> date:
    return v if isinstance(v, date) else date.fromisoformat(str(v))


def validate(cfg: Config) -> None:
    for t in cfg.tables.values():
        if t.layer not in LAYERS:
            raise ValueError(f"{t.name}: unknown layer {t.layer}")
        if t.owner not in cfg.owners:
            raise ValueError(f"{t.name}: unknown owner {t.owner}")
        for u in t.upstream:
            if u not in cfg.tables:
                raise ValueError(f"{t.name}: unknown upstream {u}")
        for col, ref in t.foreign_keys.items():
            ptable, pcol = ref.split(".")
            if col not in t.columns or ptable not in cfg.tables or pcol not in cfg.tables[ptable].columns:
                raise ValueError(f"{t.name}: bad foreign key {col} -> {ref}")
    for n in cfg.tables:
        if n in cfg.ancestors(n):
            raise ValueError(f"lineage cycle through {n}")
