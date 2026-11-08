"""Profile every table partition once with SQL. The monitors then work on plain Python series.

A series key is (table, monitor, metric). Values are one float (or None) per day.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import duckdb

from .config import Config

Key = tuple[str, str, str]


@dataclass
class Profile:
    days: int
    series: dict[Key, list[float | None]] = field(default_factory=dict)
    sizes: dict[Key, list[int]] = field(default_factory=dict)  # sample size behind a rate, for binomial noise
    categories: dict[tuple[str, str], list[dict[str, int]]] = field(default_factory=dict)
    schemas: dict[str, list[dict[str, str]]] = field(default_factory=dict)
    loaded_at: dict[str, list[datetime | None]] = field(default_factory=dict)

    def keys_for(self, table: str) -> list[Key]:
        return [k for k in self.series if k[0] == table]


def profile_warehouse(con: duckdb.DuckDBPyConnection, cfg: Config) -> Profile:
    prof = Profile(days=cfg.days)
    day_of = {cfg.day_date(d): d for d in range(cfg.days)}

    def empty() -> list:
        return [None] * cfg.days

    for name, d, loaded_at, hours in con.execute(
        "SELECT table_name, day, loaded_at, arrival_hours FROM _dw_load_log"
    ).fetchall():
        prof.series.setdefault((name, "freshness", "arrival_hours"), empty())[d] = hours
        prof.loaded_at.setdefault(name, empty())[d] = loaded_at

    for name, d, col, typ in con.execute(
        "SELECT table_name, day, column_name, data_type FROM _dw_schema_log ORDER BY table_name, day, ordinal"
    ).fetchall():
        snaps = prof.schemas.setdefault(name, [{} for _ in range(cfg.days)])
        snaps[d][col] = typ

    for name, t in cfg.tables.items():
        live = {r[0] for r in con.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = ?", [name]).fetchall()}
        cols = [c for c in t.columns if c in live]
        exprs = ["count(*)"]
        plan: list[tuple[str, str, str]] = []  # (monitor, metric, kind)
        for c in cols:
            role = t.column_role(c)
            exprs.append(f"count({c})")
            plan.append(("null_rate", c, "nn"))
            if role == "numeric" and t.columns[c] in ("BIGINT", "INTEGER", "DOUBLE"):
                exprs += [f"avg({c})", f"quantile_disc({c}, 0.5)", f"quantile_disc({c}, 0.95)"]
                plan += [("distribution", f"{c}.mean", "v"), ("distribution", f"{c}.p50", "v"),
                         ("distribution", f"{c}.p95", "v")]
            elif role == "text":
                exprs.append(f"count(DISTINCT {c})")
                plan.append(("distribution", f"{c}.distinct_ratio", "distinct"))
        if t.primary_key and t.primary_key in cols:
            exprs.append(f"count(DISTINCT {t.primary_key})")
            plan.append(("uniqueness", t.primary_key, "unique"))
        rows = con.execute(f"SELECT load_date, {', '.join(exprs)} FROM {name} GROUP BY load_date").fetchall()
        for row in rows:
            d = day_of.get(row[0])
            if d is None:
                continue
            n = row[1]
            prof.series.setdefault((name, "volume", "row_count"), empty())[d] = float(n)
            last_nn = n
            for (monitor, metric, kind), v in zip(plan, row[2:], strict=True):
                key = (name, monitor, metric)
                s = prof.series.setdefault(key, empty())
                size = prof.sizes.setdefault(key, [0] * cfg.days)
                if kind == "nn":
                    s[d] = 1 - v / n if n else None
                    size[d] = n
                    last_nn = v
                elif kind == "distinct":
                    s[d] = v / last_nn if last_nn else None
                    size[d] = last_nn
                elif kind == "unique":
                    s[d] = 1 - v / n if n else None
                    size[d] = n
                else:
                    s[d] = float(v) if v is not None else None
        for c in cols:
            if t.column_role(c) != "categorical":
                continue
            dist = prof.categories.setdefault((name, c), [{} for _ in range(cfg.days)])
            for ld, val, cnt in con.execute(
                f"SELECT load_date, CAST({c} AS VARCHAR), count(*) FROM {name} GROUP BY ALL"
            ).fetchall():
                if ld in day_of:
                    dist[day_of[ld]]["(null)" if val is None else val] = cnt
        for col, ref in t.foreign_keys.items():
            ptable, pcol = ref.split(".")
            key = (name, "referential", f"{col}->{ref}")
            s = prof.series.setdefault(key, empty())
            size = prof.sizes.setdefault(key, [0] * cfg.days)
            for ld, nn, orphans in con.execute(f"""
                SELECT c.load_date, count(c.{col}), count(c.{col}) FILTER (WHERE p.k IS NULL)
                FROM {name} c LEFT JOIN (SELECT {pcol} AS k, min(load_date) AS first_date FROM {ptable} GROUP BY 1) p
                  ON p.k = c.{col} AND p.first_date <= c.load_date
                GROUP BY c.load_date""").fetchall():
                if ld in day_of:
                    s[day_of[ld]] = orphans / nn if nn else 0.0
                    size[day_of[ld]] = nn
    return prof
