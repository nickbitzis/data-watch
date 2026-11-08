import re

import pytest
import yaml

from datawatch.config import DEFAULT_CONFIG, load_config
from datawatch.generate import TRANSFORMS


def test_table_counts_by_layer(cfg):
    layers = [t.layer for t in cfg.tables.values()]
    assert len(cfg.tables) == 41
    assert layers.count("ingestion") == 13 and layers.count("staging") == 13 and layers.count("marts") == 15


def test_every_downstream_table_has_a_transform_matching_declared_lineage(cfg):
    names = set(cfg.tables)
    for name, t in cfg.tables.items():
        if not t.upstream:
            assert name not in TRANSFORMS
            continue
        refs = set(re.findall(r"\b(?:FROM|JOIN)\s+(\w+)", TRANSFORMS[name])) & names
        assert refs == set(t.upstream), name


def test_silent_bug_source_has_six_downstream_tables(cfg):
    assert len(cfg.descendants("raw_order_items")) == 6


def test_ancestors_and_depth(cfg):
    assert {"raw_orders", "raw_order_items", "raw_fx_rates"} <= cfg.ancestors("mart_exec_kpis")
    assert cfg.depth["raw_orders"] == 0
    assert cfg.depth["mart_exec_kpis"] > cfg.depth["mart_finance_summary"]
    order = cfg.topo_order()
    for name, t in cfg.tables.items():
        for u in t.upstream:
            assert order.index(u) < order.index(name)


def test_critical_marts_and_owners(cfg):
    assert sorted(n for n, t in cfg.tables.items() if t.critical) == ["mart_exec_kpis", "mart_finance_summary"]
    assert all(t.owner in cfg.owners for t in cfg.tables.values())


def test_column_roles(cfg):
    t = cfg.tables["raw_orders"]
    assert t.column_role("order_id") == "id"
    assert t.column_role("order_ts") == "timestamp"
    assert t.column_role("shipping_fee") == "numeric"
    assert t.column_role("currency") == "categorical"
    assert cfg.tables["raw_customers"].column_role("postal_code") == "code"
    assert cfg.tables["raw_customers"].column_role("email") == "text"


def test_calendar_month_end_and_holiday(cfg):
    assert cfg.is_month_end(29) and not cfg.is_month_end(28)  # 2026-06-30
    assert cfg.is_holiday(33)  # 2026-07-04


def _bad_config(tmp_path, mutate):
    raw = yaml.safe_load(DEFAULT_CONFIG.read_text())
    mutate(raw)
    p = tmp_path / "bad.yml"
    p.write_text(yaml.safe_dump(raw))
    return p


def test_validation_rejects_unknown_upstream(tmp_path):
    p = _bad_config(tmp_path, lambda r: r["tables"]["stg_orders"].update(upstream=["raw_nope"]))
    with pytest.raises(ValueError, match="unknown upstream"):
        load_config(p)


def test_validation_rejects_cycles_and_bad_keys(tmp_path):
    p = _bad_config(tmp_path, lambda r: r["tables"]["raw_orders"].update(upstream=["stg_orders"]))
    with pytest.raises(ValueError, match="cycle"):
        load_config(p)
    p = _bad_config(tmp_path, lambda r: r["tables"]["stg_orders"].update(foreign_keys={"customer_id": "x.y"}))
    with pytest.raises(ValueError, match="foreign key"):
        load_config(p)
