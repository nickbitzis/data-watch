import duckdb
import pytest

from datawatch.generate import PLANTED, build, load_planted


@pytest.fixture(scope="module")
def con(warehouse):
    c = duckdb.connect(str(warehouse), read_only=True)
    yield c
    c.close()


def test_every_table_has_120_daily_partitions(con, cfg):
    for name in cfg.tables:
        assert con.execute(f"SELECT count(DISTINCT load_date) FROM {name}").fetchone()[0] == 120, name


def test_physical_schema_matches_declared_columns(con, cfg):
    for name, t in cfg.tables.items():
        live = dict(con.execute("SELECT column_name, data_type FROM information_schema.columns "
                                "WHERE table_name = ?", [name]).fetchall())
        expected = {**t.columns, "load_date": "DATE"}
        if name == "raw_customers":
            expected["postal_code"] = "VARCHAR"  # the planted schema change, now live
        assert live == expected, name


def test_weekend_and_month_end_and_holiday_volume(con):
    rows = dict(con.execute("SELECT day, row_count FROM _dw_load_log WHERE table_name = 'raw_orders'").fetchall())
    weekday = sum(rows[d] for d in range(35, 40)) / 5  # Mon to Fri
    weekend = (rows[40] + rows[41]) / 2
    assert weekend < 0.8 * weekday
    assert rows[60] > 1.3 * rows[59]  # 2026-07-31 month-end sale (a Friday)
    assert rows[33] < 0.6 * rows[26]  # 2026-07-04 versus the Saturday before


def test_planted_incidents_recorded_with_lineage(con, cfg):
    planted = load_planted(con)
    assert [p.id for p in planted] == [p.id for p in sorted(PLANTED, key=lambda p: p.start_day)]
    bug = next(p for p in planted if p.id == "silent_quantity_bug")
    assert len(bug.affected) == 7 and bug.affected[0] == "raw_order_items"


def test_duplicate_load_only_in_raw(con):
    q = "SELECT count(*), count(DISTINCT shipment_id) FROM {} WHERE load_date = DATE '2026-08-06'"
    raw = con.execute(q.format("raw_shipments")).fetchone()
    stg = con.execute(q.format("stg_shipments")).fetchone()
    assert raw[0] == 2 * raw[1] and stg[0] == stg[1]


def test_late_load_in_log_and_propagates(con):
    late = dict(con.execute("SELECT table_name, arrival_hours FROM _dw_load_log WHERE day = 52").fetchall())
    assert late["raw_inventory"] > 12
    assert late["fct_inventory_daily"] > late["stg_inventory"] > late["raw_inventory"]


def test_schema_log_shows_change_on_day_58(con):
    q = ("SELECT data_type FROM _dw_schema_log WHERE table_name = 'raw_customers' AND column_name = 'postal_code' "
         "AND day = ?")
    assert con.execute(q, [57]).fetchone()[0] == "INTEGER"
    assert con.execute(q, [58]).fetchone()[0] == "VARCHAR"


def test_cents_and_quantity_incidents_in_data(con):
    amt = dict(con.execute("SELECT load_date::VARCHAR, avg(amount) FROM raw_payments GROUP BY 1").fetchall())
    assert amt["2026-08-14"] > 50 * amt["2026-08-12"]
    q = con.execute("SELECT max(quantity) FROM raw_order_items WHERE load_date = DATE '2026-09-17'").fetchone()[0]
    assert q == 1


def test_build_is_deterministic(cfg, tmp_path):
    a, b = tmp_path / "a.duckdb", tmp_path / "b.duckdb"
    build(cfg, a, scale=0.05)
    build(cfg, b, scale=0.05)
    q = "SELECT sum(hash(order_id, customer_id, currency)) FROM raw_orders"
    with duckdb.connect(str(a), read_only=True) as ca, duckdb.connect(str(b), read_only=True) as cb:
        assert ca.execute(q).fetchone() == cb.execute(q).fetchone()
